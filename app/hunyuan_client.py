import asyncio
import base64
import io
import json
import logging
import hashlib
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit
from typing import Dict, Any, Optional, List, Union
import httpx
from PIL import Image
from qcloud_cos import CosConfig, CosS3Client
from app.config import settings

logger = logging.getLogger("tencent_hunyuan")

class GeneratedImageDownloadError(RuntimeError):
    """The image already exists; retry downloading rather than generating again."""


class HunyuanAccount:
    """单个腾讯混元账号实例，支持文生图与图生图"""
    def __init__(self, name: str, cookie: str, chat_id: str):
        self.name = name
        self.cookie = cookie
        self.chat_id = chat_id
        self.lock = asyncio.Lock()
        self.is_busy = False
        self.success_count = 0
        self.failed_count = 0
        self._clients = {}
        self._reference_cache = OrderedDict()
        self._reference_inflight = {}
        self._upload_semaphore = asyncio.Semaphore(settings.UPLOAD_CONCURRENCY)
        self.reference_upload_count = 0
        self.reference_cache_hits = 0

    @asynccontextmanager
    async def _get_client(self, timeout: float = 30.0, follow_redirects: bool = False):
        # Clients are account-local; credentials are always supplied per request.
        client = self._clients.get(follow_redirects)
        if client is None or client.is_closed:
            proxy = settings.PROXY.strip() if settings.PROXY and settings.PROXY.strip() else None
            client = httpx.AsyncClient(proxy=proxy, timeout=timeout,
                follow_redirects=follow_redirects,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=30))
            self._clients[follow_redirects] = client
        yield client

    async def aclose(self):
        uploads = list(self._reference_inflight.values())
        for task in uploads:
            task.cancel()
        if uploads:
            await asyncio.gather(*uploads, return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        self._reference_cache.clear()

    def _cache_expiry(self, url):
        ttl = float(settings.REFERENCE_CACHE_TTL)
        try:
            signing = parse_qs(urlsplit(url).query).get("q-sign-time", [""])[0]
            if signing:
                ttl = min(ttl, float(signing.split(";")[-1]) - time.time() - 5)
        except (ValueError, TypeError):
            ttl = 0
        return time.monotonic() + max(0, ttl)

    async def upload_image(self, image_bytes: bytes, filename: str = "reference.png") -> Dict[str, Any]:
        key = hashlib.sha256(image_bytes).hexdigest()
        now = time.monotonic()
        for expired in [k for k, value in self._reference_cache.items() if value[0] <= now]:
            self._reference_cache.pop(expired, None)
        cached = self._reference_cache.get(key)
        if cached:
            self.reference_cache_hits += 1
            self._reference_cache.move_to_end(key)
            return {**cached[1], "fileName": filename, "name": filename}
        task = self._reference_inflight.get(key)
        if task is None:
            async def upload():
                async with self._upload_semaphore:
                    item = await self._upload_image_uncached(image_bytes, filename)
                    self.reference_upload_count += 1
                    expiry = self._cache_expiry(item["url"])
                    if settings.REFERENCE_CACHE_MAX_ITEMS > 0 and expiry > time.monotonic():
                        self._reference_cache[key] = (expiry, dict(item))
                        while len(self._reference_cache) > settings.REFERENCE_CACHE_MAX_ITEMS:
                            self._reference_cache.popitem(last=False)
                    return item
            task = asyncio.create_task(upload())
            self._reference_inflight[key] = task
            def complete(done):
                if self._reference_inflight.get(key) is done:
                    self._reference_inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(complete)
        else:
            self.reference_cache_hits += 1
        item = await task
        return {**item, "fileName": filename, "name": filename}

    async def _prepare_reference(self, single_input, index):
        if not single_input:
            return None
        if isinstance(single_input, bytes):
            image_bytes = single_input
        elif isinstance(single_input, str) and single_input.startswith(("http://", "https://")):
            async with self._get_client(follow_redirects=True) as client:
                response = await client.get(single_input, timeout=30.0)
                if response.status_code != 200:
                    raise RuntimeError(f"下载参考图片[{index + 1}]失败 HTTP {response.status_code}")
                image_bytes = response.content
        elif isinstance(single_input, str):
            encoded = "".join(single_input.split(";base64,")[-1].split())
            image_bytes = base64.b64decode(encoded, validate=True)
        else:
            raise ValueError("参考图片应为图片数据、URL 或 Base64")
        if not image_bytes:
            raise ValueError("参考图片内容为空")
        return await self.upload_image(image_bytes, f"reference_{index + 1}.png")

    async def _download_generated_image(self, url):
        failure = "网络错误"
        for attempt in range(3):
            try:
                async with self._get_client(follow_redirects=True) as client:
                    response = await client.get(url, timeout=30.0)
                if response.status_code == 200 and response.content:
                    return response.content
                failure = f"HTTP {response.status_code}"
                if response.status_code < 500 and response.status_code != 429:
                    break
            except httpx.HTTPError as error:
                failure = type(error).__name__
            if attempt < 2:
                await asyncio.sleep(0.2 * (attempt + 1))
        raise GeneratedImageDownloadError(f"图片已生成，但成品下载失败：{failure}")

    def _get_base_url(self) -> str:
        return f"https://api.hunyuan.tencent.com/api/new-portal/chat/{self.chat_id}"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Language": "zh",
            "Connection": "keep-alive",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://aistudio.tencent.com",
            "Referer": "https://aistudio.tencent.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "X-AgentID": f"HunyuanDefault/{self.chat_id}",
            "X-Requested-With": "XMLHttpRequest",
            "X-Source": "web",
            "chat_version": "v1",
            "Cookie": self.cookie,
        }

    def _map_size_to_scale(self, size: Optional[str]) -> str:
        if not size:
            return ""
        size = size.lower().strip()
        if size in ["1:1", "1024x1024", "512x512", "2048x2048"]:
            return "1:1"
        elif size in ["16:9", "1792x1024", "1280x720", "1920x1080"]:
            return "16:9"
        elif size in ["9:16", "1024x1792", "720x1280", "1080x1920"]:
            return "9:16"
        elif size in ["4:3", "1024x768"]:
            return "4:3"
        elif size in ["3:4", "768x1024"]:
            return "3:4"
        return ""

    async def _upload_image_uncached(self, image_bytes: bytes, filename: str = "reference.png") -> Dict[str, Any]:
        """
        将本地图片或下载的网络图片上传到腾讯云 COS，返回 multimedia 所需结构
        """
        # 1. 读取尺寸
        width, height = 512, 512
        try:
            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                width, height = pil_img.size
        except Exception:
            pass

        # 2. 获取临时上传凭证
        gen_url = "https://api.hunyuan.tencent.com/api/new-portal/chat/resource/genUploadInfo"
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        payload = {"fileName": filename, "resourceType": "IMAGE"}

        async with self._get_client(timeout=30.0) as client:
            r = await client.post(gen_url, headers=headers, json=payload, timeout=30.0)
            if r.status_code != 200:
                raise RuntimeError(f"获取图片上传凭据失败 HTTP {r.status_code}: {r.text}")
            info = r.json()
            bucket = info.get("bucketName")
            region = info.get("region")
            key = info.get("location")
            secret_id = info.get("encryptTmpSecretId")
            secret_key = info.get("encryptTmpSecretKey")
            token = info.get("encryptToken")
            resource_url = info.get("resourceUrl")

        if not bucket or not key:
            raise RuntimeError("腾讯云未返回有效的存储桶信息")

        # 3. 使用 COS SDK 上传图片数据
        cos_kwargs = {
            "Region": region,
            "SecretId": secret_id,
            "SecretKey": secret_key,
            "Token": token,
            "Scheme": "https"
        }
        if settings.PROXY and settings.PROXY.strip():
            proxy_url = settings.PROXY.strip()
            cos_kwargs["Proxies"] = {"http": proxy_url, "https": proxy_url}
        def put_object():
            client = CosS3Client(CosConfig(**cos_kwargs))
            client.put_object(Bucket=bucket, Body=image_bytes, Key=key, EnableMD5=False)
            return client
        cos_client = await asyncio.to_thread(put_object)

        # 4. 获取腾讯服务认证的 realUrl 直链
        real_url = None
        if resource_url:
            async with self._get_client(follow_redirects=False, timeout=30.0) as client:
                dl_resp = await client.get(resource_url, headers=self._get_headers(), timeout=30.0)
                real_url = dl_resp.headers.get("Location") or dl_resp.headers.get("location")
                if not real_url and dl_resp.status_code == 200:
                    try:
                        real_url = dl_resp.json().get("data", {}).get("realUrl")
                    except Exception:
                        pass

        if not real_url:
            real_url = cos_client.get_presigned_download_url(Bucket=bucket, Key=key, Expired=86400 * 30)

        return {
            "type": "image",
            "docType": "image",
            "url": real_url,
            "fileName": filename,
            "name": filename,
            "size": len(image_bytes),
            "width": width,
            "height": height
        }

    async def execute_generate(
        self,
        prompt: str,
        size: Optional[str] = None,
        model: Optional[str] = None,
        quality: str = "hd",
        response_format: str = "url",
        image_input: Optional[Union[str, bytes, List[Union[str, bytes]]]] = None
    ) -> Dict[str, Any]:
        target_model = model or settings.DEFAULT_MODEL
        if target_model.lower() in ["hy-image-3.5", "hunyuan-image-3.5", "hunyuan-3.5", "dall-e-3", "dalle3", "dall-e", "gemini-3.1-flash-image-preview"]:
            target_model = settings.DEFAULT_MODEL

        # 尺寸比例处理
        final_prompt = prompt
        scale = self._map_size_to_scale(size)
        if scale and f"{scale}" not in final_prompt and "比例" not in final_prompt:
            final_prompt = f"{final_prompt}, 画面比例{scale}"

        queued_at = time.perf_counter()
        async with self.lock:
            self.is_busy = True
            job_started = time.perf_counter()
            timings = {"queue_wait": round(job_started - queued_at, 3)}
            uploads_before, hits_before = self.reference_upload_count, self.reference_cache_hits
            status = "cancelled"
            try:
                multimedia = []

                upload_started = time.perf_counter()
                input_list = image_input if isinstance(image_input, list) else ([image_input] if image_input else [])
                uploads = [asyncio.create_task(self._prepare_reference(value, index))
                           for index, value in enumerate(input_list) if value]
                try:
                    multimedia = [item for item in await asyncio.gather(*uploads) if item]
                except BaseException:
                    for task in uploads:
                        task.cancel()
                    await asyncio.gather(*uploads, return_exceptions=True)
                    raise
                timings["reference_upload"] = round(time.perf_counter() - upload_started, 3)

                payload = {
                    "model": "gpt_175B_0404",
                    "prompt": final_prompt,
                    "plugin": "Adaptive",
                    "displayPrompt": final_prompt,
                    "displayPromptType": 1,
                    "options": {
                        "imageIntention": {
                            "needIntentionModel": True,
                            "backendUpdateFlag": 2,
                            "userIntention": {"scale": ""}
                        }
                    },
                    "targetLang": None,
                    "targetLangLabel": None,
                    "sourceLang": None,
                    "sourceLangLabel": None,
                    "translateModelList": [],
                    "podcast": {"voices": []},
                    "displayImageIntentionLabels": [{"type": "scale", "disPlayValue": "", "startIndex": 0, "endIndex": 0}],
                    "multimedia": multimedia,
                    "agentId": "HunyuanDefault",
                    "supportHint": 1,
                    "version": "v2",
                    "chatModelId": target_model
                }

                image_info = None
                thinking_text = []
                url = self._get_base_url()
                headers = self._get_headers()
                raw_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

                upstream_started = time.perf_counter()
                async with self._get_client(timeout=settings.REQUEST_TIMEOUT) as client:
                    async with client.stream("POST", url, headers=headers, content=raw_data, timeout=settings.REQUEST_TIMEOUT) as response:
                        if response.status_code != 200:
                            body = await response.aread()
                            raise RuntimeError(f"HTTP {response.status_code}: {body.decode('utf-8', errors='ignore')}")

                        current_sse_event = None
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            
                            if line.startswith("event:"):
                                current_sse_event = line[len("event:"):].strip()
                                continue
                            
                            prefix = ""
                            if line.startswith("data:"):
                                prefix = "data:"
                            elif line.startswith("message"):
                                prefix = "message"
                            
                            if prefix:
                                content_str = line[len(prefix):].strip()
                            else:
                                content_str = line.strip()

                            if content_str == "[DONE]":
                                break

                            # 处理显式 error 事件 (例如 event: error \n data: 您今日已达体验限额)
                            if current_sse_event == "error":
                                err_msg = content_str
                                try:
                                    err_json = json.loads(content_str)
                                    err_msg = err_json.get("errorMsg") or err_json.get("message") or err_json.get("msg") or content_str
                                except Exception:
                                    pass
                                raise RuntimeError(f"腾讯服务提示: {err_msg}")

                            try:
                                event = json.loads(content_str)
                                event_type = event.get("type")
                                
                                if event_type == "think":
                                    chunk = event.get("content", "")
                                    if chunk:
                                        thinking_text.append(chunk)

                                elif event_type == "image":
                                    image_info = {
                                        "imageUrlHigh": event.get("imageUrlHigh"),
                                        "imageUrlLow": event.get("imageUrlLow"),
                                        "width": event.get("width"),
                                        "height": event.get("height"),
                                        "modelName": event.get("modelName"),
                                    }
                                    break

                                elif event_type == "text":
                                    msg = event.get("msg", "")
                                    if "错误" in msg or "稍后重试" in msg or "限额" in msg:
                                        raise RuntimeError(f"腾讯服务提示: {msg}")

                                elif event_type == "error":
                                    err_msg = event.get("errorMsg") or event.get("message") or event.get("msg") or "未知错误"
                                    raise RuntimeError(f"腾讯模型返回错误: {err_msg}")

                            except json.JSONDecodeError:
                                if "限额" in content_str or "错误" in content_str or "失败" in content_str:
                                    raise RuntimeError(f"腾讯服务提示: {content_str}")
                                continue

                timings["upstream_generation"] = round(time.perf_counter() - upstream_started, 3)
                if not image_info or not (image_info.get("imageUrlHigh") or image_info.get("imageUrlLow")):
                    raise RuntimeError("生图完成但未找到生成的图片链接")

                # 根据清晰度选择高品质原画 (hd) 或低清预览 (standard)
                if quality == "standard" and image_info.get("imageUrlLow"):
                    img_url = image_info["imageUrlLow"]
                else:
                    img_url = image_info["imageUrlHigh"] or image_info["imageUrlLow"]

                result = {
                    "url": img_url,
                    "width": image_info.get("width"),
                    "height": image_info.get("height"),
                    "quality": "standard" if quality == "standard" else "hd",
                    "thinking": "".join(thinking_text),
                    "model": target_model,
                    "account": self.name
                }

                download_started = time.perf_counter()
                if response_format == "b64_json":
                    data = await self._download_generated_image(img_url)
                    result["b64_json"] = base64.b64encode(data).decode("utf-8")
                timings["image_download"] = round(time.perf_counter() - download_started, 3)
                result["diagnostics"] = {"timings_seconds": timings,
                    "reference_uploads": self.reference_upload_count - uploads_before,
                    "reference_cache_hits": self.reference_cache_hits - hits_before}
                status = "succeeded"
                self.success_count += 1
                return result

            except Exception:
                status = "failed"
                self.failed_count += 1
                self._reference_cache.clear()
                raise
            finally:
                self.is_busy = False
                timings["total"] = round(time.perf_counter() - queued_at, 3)
                print("[HunyuanTiming] " + json.dumps({"status": status,
                    "timings_seconds": timings,
                    "reference_uploads": self.reference_upload_count - uploads_before,
                    "reference_cache_hits": self.reference_cache_hits - hits_before}), flush=True)


class HunyuanAccountPool:
    """多账号并发调度池（文生图 + 图生图调度中心）"""
    def __init__(self):
        self.accounts: List[HunyuanAccount] = []
        self._round_robin_idx = 0
        self._pool_lock = asyncio.Lock()
        self._retired_accounts = []
        self._cleanup_tasks = set()
        self.reload_accounts()

    def reload_accounts(self):
        account_configs = settings.load_accounts()
        old = {(account.name, account.cookie, account.chat_id): account for account in self.accounts}
        new_accounts = []
        for conf in account_configs:
            key = (conf["name"], conf["cookie"], conf["chat_id"])
            account = old.pop(key, None)
            new_accounts.append(account or HunyuanAccount(*key))
        for retired in old.values():
            self._retired_accounts.append(retired)
            try:
                task = asyncio.get_running_loop().create_task(self._close_retired(retired))
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)
            except RuntimeError:
                pass
        self.accounts = new_accounts
        print(f"[AccountPool] 成功加载 {len(self.accounts)} 个账号")
        if settings.PROXY and settings.PROXY.strip():
            print(f"[AccountPool] 启用出站代理: {settings.PROXY.strip()}")
        else:
            print("[AccountPool] 未配置出站代理 (网络直连)")

    async def _close_retired(self, account):
        async with account.lock:
            await account.aclose()
        if account in self._retired_accounts:
            self._retired_accounts.remove(account)

    async def aclose(self):
        if self._cleanup_tasks:
            await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)
        await asyncio.gather(*(account.aclose() for account in self.accounts + self._retired_accounts))

    async def get_account(self) -> HunyuanAccount:
        if not self.accounts:
            raise RuntimeError("账号池为空！请在 configs/accounts.json 或 .env 中配置腾讯 AI Studio Cookie")

        # 优先寻找空闲账号
        for acc in self.accounts:
            if not acc.lock.locked() and not acc.is_busy:
                return acc

        # 全部都在忙，轮询排队
        async with self._pool_lock:
            acc = self.accounts[self._round_robin_idx % len(self.accounts)]
            self._round_robin_idx += 1
            return acc

    async def generate_image(
        self,
        prompt: str,
        size: Optional[str] = None,
        model: Optional[str] = None,
        quality: str = "hd",
        response_format: str = "url",
        image_input: Optional[Union[str, bytes, List[Union[str, bytes]]]] = None
    ) -> Dict[str, Any]:
        """
        调度账号进行生图（支持文生图与图生图），支持多账号故障转移
        """
        last_error = None
        attempts = max(1, len(self.accounts))
        tried_accounts = set()

        for _ in range(attempts):
            acc = await self.get_account()
            if acc.name in tried_accounts and len(tried_accounts) < len(self.accounts):
                remaining = [a for a in self.accounts if a.name not in tried_accounts]
                if remaining:
                    acc = remaining[0]

            tried_accounts.add(acc.name)
            try:
                return await acc.execute_generate(
                    prompt=prompt,
                    size=size,
                    model=model,
                    quality=quality,
                    response_format=response_format,
                    image_input=image_input
                )
            except GeneratedImageDownloadError:
                raise  # A generation already succeeded; do not generate it again on another account.
            except Exception as e:
                last_error = e
                print(f"[AccountPool] 账号 {acc.name} 执行失败: {e}，正在尝试其它账号...")

        raise last_error or RuntimeError("所有账号生图均失败")

    def get_status(self) -> Dict[str, Any]:
        return {
            "total_accounts": len(self.accounts),
            "idle_accounts": sum(1 for a in self.accounts if not a.is_busy),
            "busy_accounts": sum(1 for a in self.accounts if a.is_busy),
            "accounts": [
                {
                    "name": a.name,
                    "chat_id": a.chat_id,
                    "is_busy": a.is_busy,
                    "success_count": a.success_count,
                    "failed_count": a.failed_count
                }
                for a in self.accounts
            ]
        }

hunyuan_pool = HunyuanAccountPool()
hunyuan_client = hunyuan_pool
