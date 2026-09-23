import asyncio
import base64
import io
import json
import logging
from typing import Dict, Any, Optional, List, Union
import httpx
from PIL import Image
from qcloud_cos import CosConfig, CosS3Client
from app.config import settings

logger = logging.getLogger("tencent_hunyuan")

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

    async def upload_image(self, image_bytes: bytes, filename: str = "reference.png") -> Dict[str, Any]:
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

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(gen_url, headers=headers, json=payload)
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
        cos_cfg = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key, Token=token, Scheme="https")
        cos_client = CosS3Client(cos_cfg)
        cos_client.put_object(Bucket=bucket, Body=image_bytes, Key=key, EnableMD5=False)

        # 4. 获取腾讯服务认证的 realUrl 直链
        real_url = None
        if resource_url:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                dl_resp = await client.get(resource_url, headers=self._get_headers())
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
        if target_model.lower() in ["hy-image-3.5", "hunyuan-image-3.5", "hunyuan-3.5"]:
            target_model = settings.DEFAULT_MODEL

        # 尺寸比例处理
        final_prompt = prompt
        scale = self._map_size_to_scale(size)
        if scale and f"{scale}" not in final_prompt and "比例" not in final_prompt:
            final_prompt = f"{final_prompt}, 画面比例{scale}"

        async with self.lock:
            self.is_busy = True
            multimedia = []

            # 如果提供了底图输入（图生图/参考图），自动将其上传到腾讯云
            if image_input:
                if isinstance(image_input, (str, bytes)):
                    input_list = [image_input]
                elif isinstance(image_input, list):
                    input_list = image_input
                else:
                    input_list = []

                for idx, single_input in enumerate(input_list):
                    if not single_input:
                        continue
                    try:
                        img_bytes = None
                        filename = f"reference_{idx + 1}.png"

                        if isinstance(single_input, bytes):
                            img_bytes = single_input
                        elif isinstance(single_input, str):
                            # 处理 Base64 格式
                            if single_input.startswith("data:") or ";base64," in single_input:
                                base64_data = single_input.split(";base64,")[-1]
                                img_bytes = base64.b64decode(base64_data)
                            elif single_input.startswith("http://") or single_input.startswith("https://"):
                                # 网络图片链接：自动下载后上传到腾讯云
                                async with httpx.AsyncClient(timeout=30.0) as dl_client:
                                    img_resp = await dl_client.get(single_input)
                                    if img_resp.status_code == 200:
                                        img_bytes = img_resp.content
                                    else:
                                        raise RuntimeError(f"下载参考图片[{idx + 1}]失败 HTTP {img_resp.status_code}")
                            else:
                                # 纯 Base64 字符串
                                try:
                                    img_bytes = base64.b64decode(single_input)
                                except Exception:
                                    pass

                        if img_bytes:
                            media_item = await self.upload_image(img_bytes, filename=filename)
                            multimedia.append(media_item)
                    except Exception as upload_err:
                        print(f"[{self.name}] 图生图参考图片[{idx + 1}]上传失败: {upload_err}")
                        raise upload_err

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

            try:
                async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
                    async with client.stream("POST", url, headers=headers, content=raw_data) as response:
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

                if response_format == "b64_json":
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        img_resp = await client.get(img_url)
                        if img_resp.status_code == 200:
                            b64_str = base64.b64encode(img_resp.content).decode("utf-8")
                            result["b64_json"] = b64_str

                self.success_count += 1
                return result

            except Exception as e:
                self.failed_count += 1
                raise e
            finally:
                self.is_busy = False


class HunyuanAccountPool:
    """多账号并发调度池（文生图 + 图生图调度中心）"""
    def __init__(self):
        self.accounts: List[HunyuanAccount] = []
        self._round_robin_idx = 0
        self._pool_lock = asyncio.Lock()
        self.reload_accounts()

    def reload_accounts(self):
        account_configs = settings.load_accounts()
        new_accounts = []
        for conf in account_configs:
            new_accounts.append(
                HunyuanAccount(
                    name=conf["name"],
                    cookie=conf["cookie"],
                    chat_id=conf["chat_id"]
                )
            )
        self.accounts = new_accounts
        print(f"[AccountPool] 成功加载 {len(self.accounts)} 个账号")

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
