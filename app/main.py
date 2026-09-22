import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from app.config import settings
from app.routes import router

app = FastAPI(
    title="Tencent Hunyuan 3.5 To API",
    description="A lightweight OpenAI-compatible API gateway for Tencent Hy AI Studio (Hunyuan Image 3.5).",
    version="1.0.0"
)

# 允许跨域（方便浏览器端或各类 WebUI 直连）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>Tencent Hunyuan 3.5 To API</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 40px; display: flex; justify-content: center; }
            .card { background: #1e293b; border-radius: 12px; padding: 32px; max-width: 600px; width: 100%; box-shadow: 0 10px 25px rgba(0,0,0,0.3); border: 1px solid #334155; }
            h1 { margin-top: 0; color: #38bdf8; font-size: 24px; }
            p { color: #94a3b8; line-height: 1.6; }
            code { background: #0f172a; padding: 3px 8px; border-radius: 6px; color: #a5f3fc; font-family: Consolas, monospace; }
            .tag { display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: bold; background: #0284c7; color: white; margin-bottom: 16px; }
            ul { padding-left: 20px; color: #cbd5e1; }
            li { margin-bottom: 8px; }
            a { color: #38bdf8; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="card">
            <span class="tag">服务运行中</span>
            <h1>Tencent Hunyuan 3.5 To API</h1>
            <p>腾讯混元图像大模型 3.5 (HY-Image-3.5-Preview-4090-Tob-v1.1) 的轻量级 OpenAI 兼容 API 代理已成功启动！</p>
            <h3>支持的端点</h3>
            <ul>
                <li><code>POST /v1/images/generations</code> - OpenAI 图像生成兼容接口</li>
                <li><code>POST /v1/chat/completions</code> - 对话形式直出图片（兼容各类 Chat 客户端）</li>
                <li><code>GET /v1/models</code> - 可用模型列表</li>
                <li><code>GET /docs</code> - <a href="/docs" target="_blank">Swagger 交互式 API 文档</a></li>
            </ul>
        </div>
    </body>
    </html>
    """

def run():
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)

if __name__ == "__main__":
    run()
