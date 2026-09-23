import time
import uuid
from typing import List, Optional, Union, Dict, Any
from fastapi import APIRouter, HTTPException, Header, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel, Field
from app.config import settings
from app.hunyuan_client import hunyuan_pool

router = APIRouter()

# API Key 校验依赖
async def verify_api_key(authorization: Optional[str] = Header(None)):
    valid_keys = settings.valid_api_keys
    if not valid_keys:
        return True
    
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 请求头")
    
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="无效的 Authorization 格式，应为 Bearer <API_KEY>")
    
    token = parts[1].strip()
    if token not in valid_keys:
        raise HTTPException(status_code=401, detail="API Key 无效或未授权")
    
    return True

# --- OpenAI Images API 模型定义 ---
class ImageGenerateRequest(BaseModel):
    prompt: str = Field(..., description="生图提示词")
    model: Optional[str] = Field(settings.DEFAULT_MODEL, description="模型名称")
    n: Optional[int] = Field(1, description="生成图片张数（单次返回1张）")
    size: Optional[str] = Field("1024x1024", description="图片分辨率或比例 (如 1024x1024, 16:9, 9:16, 4:3, 3:4)")
    quality: Optional[str] = Field("hd", description="清晰度模式: hd (满血2K原画) 或 standard (压缩预览)")
    response_format: Optional[str] = Field("url", description="返回格式: url 或 b64_json")
    image: Optional[Union[str, List[str]]] = Field(None, description="图生图参考图片（支持网络图片URL或Base64，可传单张或多张列表）")
    images: Optional[List[str]] = Field(None, description="多张参考图片列表（支持网络图片URL或Base64数据）")
    user: Optional[str] = None

class ImageItem(BaseModel):
    url: Optional[str] = None
    b64_json: Optional[str] = None
    revised_prompt: Optional[str] = None

class ImageGenerateResponse(BaseModel):
    created: int
    data: List[ImageItem]

# --- OpenAI Chat API 兼容模型定义 ---
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = settings.DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: Optional[bool] = False

# --- 路由实现 ---

@router.post("/v1/images/generations", response_model=ImageGenerateResponse, dependencies=[Depends(verify_api_key)])
async def generate_images(req: ImageGenerateRequest):
    """
    OpenAI 兼容生图端点（同时支持文生图、单图生图、多图融合生图）
    - 纯文本生图：只传 prompt
    - 单图/多图参考生图：在 image 字段传入单张 URL/Base64，或列表格式；也可在 images 字段传入列表
    """
    # 汇总多张参考底图
    all_images = []
    if req.image:
        if isinstance(req.image, list):
            all_images.extend(req.image)
        elif isinstance(req.image, str):
            all_images.append(req.image)
    if req.images:
        all_images.extend(req.images)

    try:
        res = await hunyuan_pool.generate_image(
            prompt=req.prompt,
            size=req.size,
            model=req.model,
            quality=req.quality or "hd",
            response_format=req.response_format or "url",
            image_input=all_images if all_images else None
        )
        
        item = ImageItem(
            url=res.get("url") if req.response_format != "b64_json" else None,
            b64_json=res.get("b64_json") if req.response_format == "b64_json" else None,
            revised_prompt=req.prompt
        )
        
        return ImageGenerateResponse(
            created=int(time.time()),
            data=[item]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")

@router.post("/v1/images/edits", response_model=ImageGenerateResponse, dependencies=[Depends(verify_api_key)])
async def edit_images(
    request: Request,
    prompt: str = Form(..., description="编辑/参考生图提示词"),
    image: Optional[List[UploadFile]] = File(None, description="参考底图文件（支持单张或多张上传）"),
    image_bracket: Optional[List[UploadFile]] = File(None, alias="image[]", description="兼容多图格式 image[]（如无限画布）"),
    images: Optional[List[UploadFile]] = File(None, description="多张参考底图文件列表"),
    images_bracket: Optional[List[UploadFile]] = File(None, alias="images[]", description="兼容多图格式 images[]"),
    model: Optional[str] = Form(settings.DEFAULT_MODEL),
    size: Optional[str] = Form("1024x1024"),
    response_format: Optional[str] = Form("url")
):
    """
    OpenAI 官方标准图生图端点 (Image Edits)，完美兼容多图上传
    - 单图模式：字段名 image
    - 无限画布 (Infinite Canvas) 多图模式：字段名 image[]
    - 其他多图客户端：字段名 images 或 images[]
    """
    upload_files: List[UploadFile] = []
    for flist in [image, image_bracket, images, images_bracket]:
        if flist:
            for f in flist:
                if f and f not in upload_files:
                    upload_files.append(f)

    # 从原生 request.form() 兜底提取任何其它命名的文件
    try:
        form_data = await request.form()
        for key, val in form_data.multi_items():
            if isinstance(val, UploadFile) and val not in upload_files:
                upload_files.append(val)
    except Exception:
        pass

    if not upload_files:
        raise HTTPException(status_code=400, detail="请至少上传一张参考底图 (image, image[] 或 images)")

    image_bytes_list = []
    for f in upload_files:
        content = await f.read()
        if content:
            image_bytes_list.append(content)

    if not image_bytes_list:
        raise HTTPException(status_code=400, detail="上传的参考图片内容为空")

    try:
        res = await hunyuan_pool.generate_image(
            prompt=prompt,
            size=size,
            model=model,
            response_format=response_format or "url",
            image_input=image_bytes_list
        )
        
        item = ImageItem(
            url=res.get("url"),
            b64_json=res.get("b64_json"),
            revised_prompt=prompt
        )
        
        return ImageGenerateResponse(
            created=int(time.time()),
            data=[item]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图生图失败: {str(e)}")

@router.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest):
    """
    OpenAI 兼容 Chat Completions 端点
    支持多模态对话生图（纯文本生图、拖拽单张或多张图片进对话框图生图）
    """
    user_prompt = ""
    image_refs = []

    for msg in reversed(req.messages):
        if msg.role == "user":
            if isinstance(msg.content, str):
                user_prompt = msg.content
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if part.get("type") == "text":
                        user_prompt += part.get("text", "")
                    elif part.get("type") == "image_url":
                        img_info = part.get("image_url", {})
                        if isinstance(img_info, dict):
                            u = img_info.get("url")
                            if u:
                                image_refs.append(u)
                        elif isinstance(img_info, str):
                            image_refs.append(img_info)
            break
            
    if not user_prompt.strip():
        raise HTTPException(status_code=400, detail="未找到用户提示词")

    try:
        res = await hunyuan_pool.generate_image(
            prompt=user_prompt,
            model=req.model,
            image_input=image_refs if image_refs else None
        )
        img_url = res["url"]
        account_used = res.get("account", "default")
        
        type_str = f"多图参考生图({len(image_refs)}张底图)" if len(image_refs) > 1 else ("图生图" if image_refs else "文生图")
        markdown_reply = f"已为你完成{type_str}（由 {account_used} 处理）：\n\n![{user_prompt}]({img_url})\n\n[点击查看高清原图]({img_url})"
        
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": markdown_reply
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(user_prompt),
                "completion_tokens": len(markdown_reply),
                "total_tokens": len(user_prompt) + len(markdown_reply)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")

@router.get("/v1/models")
async def list_models():
    models = [
        {"id": "HY-Image-3.5-Preview-4090-Tob-v1.1", "object": "model", "owned_by": "tencent"},
        {"id": "HY-Image-3.5-Preview-4090-Tob-v1.2", "object": "model", "owned_by": "tencent"},
        {"id": "hunyuan-image-3.5", "object": "model", "owned_by": "tencent"},
        {"id": "dall-e-3", "object": "model", "owned_by": "tencent"}
    ]
    return {"object": "list", "data": models}

@router.get("/health")
async def health_check():
    pool_status = hunyuan_pool.get_status()
    return {
        "status": "healthy",
        "service": "tencent-hunyuan-to-api",
        "pool": pool_status
    }

@router.post("/v1/accounts/reload", dependencies=[Depends(verify_api_key)])
async def reload_accounts():
    hunyuan_pool.reload_accounts()
    return {
        "status": "success",
        "pool": hunyuan_pool.get_status()
    }
