import time
import uuid
from typing import List, Optional, Union, Dict, Any
from fastapi import APIRouter, HTTPException, Header, Depends, UploadFile, File, Form
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
    image: Optional[str] = Field(None, description="图生图参考图片（支持网络图片URL或Base64数据）")
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
    OpenAI 兼容生图端点（同时支持文生图和图生图）
    - 纯文本生图：只传 prompt
    - 图生图：在 image 字段中传入图片 URL 或 Base64 编码
    """
    try:
        res = await hunyuan_pool.generate_image(
            prompt=req.prompt,
            size=req.size,
            model=req.model,
            quality=req.quality or "hd",
            response_format=req.response_format or "url",
            image_input=req.image
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
    prompt: str = Form(..., description="编辑/参考生图提示词"),
    image: UploadFile = File(..., description="上传的参考底图"),
    model: Optional[str] = Form(settings.DEFAULT_MODEL),
    size: Optional[str] = Form("1024x1024"),
    response_format: Optional[str] = Form("url")
):
    """
    OpenAI 官方标准图生图端点 (Image Edits)
    通过表单直接上传图片文件与提示词，自动上传至腾讯云并执行图生图
    """
    try:
        image_bytes = await image.read()
        res = await hunyuan_pool.generate_image(
            prompt=prompt,
            size=size,
            model=model,
            response_format=response_format or "url",
            image_input=image_bytes
        )
        
        item = ImageItem(
            url=res.get("url") if response_format != "b64_json" else None,
            b64_json=res.get("b64_json") if response_format == "b64_json" else None,
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
    支持多模态对话生图（纯文本对话生图、拖拽图片进对话框图生图）
    """
    user_prompt = ""
    image_ref = None

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
                            image_ref = img_info.get("url")
                        elif isinstance(img_info, str):
                            image_ref = img_info
            break
            
    if not user_prompt.strip():
        raise HTTPException(status_code=400, detail="未找到用户提示词")

    try:
        res = await hunyuan_pool.generate_image(
            prompt=user_prompt,
            model=req.model,
            image_input=image_ref
        )
        img_url = res["url"]
        account_used = res.get("account", "default")
        
        type_str = "图生图" if image_ref else "文生图"
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
