# Tencent Hunyuan 3.5 To API

> 将腾讯云 **Hy AI Studio** 的混元图像大模型 **`HY-Image-3.5-Preview-4090-Tob-v1.1`** 封装为兼容 OpenAI 格式的标准 API 服务。

无需运行庞大笨重的无头浏览器，纯 HTTP/SSE 协议直连，单实例内存占用不到 **30MB**，秒级启动。

---

## ✨ 特性

- 👥 **多账号并发调度池**：支持通过 `configs/accounts.json` 挂载多个腾讯账号，空闲账号优先调度，实现真正的多任务并行生图！
- 🎨 **文生图与多图参考生图**：不仅支持纯文本生图，还支持上传单张或多张参考底图（多图融合/垫图修改/风格迁移，支持多图同时输入）！
- 🖼️ **标准 OpenAI Images API 兼容**：支持 `POST /v1/images/generations` 与 `POST /v1/images/edits`（支持多图上传），可无缝接入 OneAPI、NewAPI、ComfyUI、Dify 等。
- 💬 **标准 OpenAI Chat API 兼容**：支持 `POST /v1/chat/completions`（支持在 NextChat、LobeChat、Cherry Studio 中发送文字或拖拽多张图片直出图）。
- ⚡ **超轻量与高性能**：脱离 Playwright/Puppeteer/Firefox 依赖，纯异步 SSE 流式长连接，开销极小。
- 📐 **多比例支持**：支持 `1:1`、`16:9`、`9:16`、`3:4`、`4:3` 以及标准像素分辨率自动映射。
- 📦 **双格式输出**：支持直接返回腾讯云 COS 高清直链（`url`），或自动拉取转码为 Base64（`b64_json`）。
- 🐋 **开箱即用容器化**：提供轻量 Docker 镜像与 `docker-compose.yml`，一键挂载部署。

---

## 👥 多账号配置与并发扩容

在 `configs/accounts.json` 中配置多个账号，即可实现多并发生成：

```json
[
  {
    "name": "账号1",
    "cookie": "hunyuan_user=...; hunyuan_token=...; hunyuan_source=web;",
    "chat_id": "dap8gfk2c3m4o8kl0ld0",
    "enabled": true
  },
  {
    "name": "账号2",
    "cookie": "hunyuan_user=...; hunyuan_token=...; hunyuan_source=web;",
    "chat_id": "dap8xxxxxxxxxxxxxxx",
    "enabled": true
  }
]
```

> 💡 **热重载支持**：修改 `configs/accounts.json` 后无需重启服务，直接请求 `POST http://localhost:7860/v1/accounts/reload` 即可立即生效！实时并发与空闲状态可通过 `GET http://localhost:7860/health` 查看。

---

## 🚀 快速开始

### 方式一：本地 Python 运行

1. **安装依赖**：
   ```bash
   pip install -r requirements.txt
   ```

2. **配置环境变量**：
   复制 `.env.example` 为 `.env`：
   ```bash
   cp .env.example .env
   ```
   并在 `.env` 中填入你的腾讯 AI Studio `TENCENT_COOKIE` 和 `DEFAULT_CHAT_ID`（获取方法见下文）。

3. **启动服务**：
   ```bash
   python -m app.main
   ```
   服务默认在 `http://127.0.0.1:7860` 启动，可访问 `http://127.0.0.1:7860/docs` 查看 Swagger 接口文档。

---

### 方式二：Docker 一键部署

1. **配置 `.env` 文件**：
   确保根目录下的 `.env` 中已配置正确的 Cookie。

2. **使用 Docker Compose 启动**：
   ```bash
   docker compose up -d --build
   ```

---

## 🔑 如何获取 Cookie 与 Chat ID

1. 使用 Chrome 浏览器打开 [腾讯 Hy AI Studio 混元画图页面](https://aistudio.tencent.com/chat/HunyuanDefault/dap8gfk2c3m4o8kl0ld0?from=modelSquare&modelId=HY-Image-3.5-Preview-4090-Tob-v1.1&from=/visual) 并登录你的账号。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 标签页。
3. 在左侧输入框发送一条画图提示词（如 `一只小猫`）。
4. 在网络请求列表中找到正在请求的会话 ID 条目（例如 `dap8gfk2c3m4o8kl0ld0`）：
   - 在该条目上右键 -> **Copy** -> **Copy as cURL (bash)**。
   - 从命令中提取：
     - Cookie 对应的值填入 `TENCENT_COOKIE`；
     - URL 末尾的字符串（如 `dap8gfk2c3m4o8kl0ld0`）填入 `DEFAULT_CHAT_ID`。

---

## 📡 API 调用示例

### 1. OpenAI Images 风格调用 (`/v1/images/generations`)

#### (1) 纯文本生图
```bash
curl -X POST "http://localhost:7860/v1/images/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "prompt": "赛博朋克风格的未来城市，霓虹灯光，雨夜街道，电影级光影",
    "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
    "size": "16:9",
    "response_format": "url"
  }'
```

#### (2) 多参考图生图（支持多张 URL 或 Base64）
```bash
curl -X POST "http://localhost:7860/v1/images/generations" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "prompt": "结合图一的构图与图二的色彩风格，生成一幅未来梦幻城堡",
    "images": [
      "https://example.com/reference1.png",
      "https://example.com/reference2.png"
    ],
    "size": "16:9"
  }'
```

### 2. OpenAI Edits 风格多图上传 (`/v1/images/edits`)
支持直接上传本地多张图片文件：
```bash
curl -X POST "http://localhost:7860/v1/images/edits" \
  -H "Authorization: Bearer sk-your-key" \
  -F "prompt=将两张图的人物合成在同一张日落海滩背景中" \
  -F "image=@photo1.jpg" \
  -F "image=@photo2.jpg" \
  -F "size=16:9"
```

### 3. OpenAI Chat 风格多图对话 (`/v1/chat/completions`)
支持多模态格式发送文字并附带多张图片直出生图：
```bash
curl -X POST "http://localhost:7860/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "参考这两张图的风格，画一只酷酷的赛博猫咪"},
          {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
          {"type": "image_url", "image_url": {"url": "https://example.com/cyberpunk.png"}}
        ]
      }
    ]
  }'
```

---

## 🛠️ 第三方客户端接入配置

### 1. 接入 OneAPI / NewAPI
- **渠道类型**：选择 `OpenAI`。
- **Base URL**：`http://你的服务器IP:7860`（末尾不加 `/v1`）。
- **密钥**：填写你在 `.env` 中设置的 `API_KEYS`（未设置可填任意字符）。
- **自定义模型**：添加 `HY-Image-3.5-Preview-4090-Tob-v1.1` 或 `hunyuan-image-3.5`。

### 2. 接入 NextChat / LobeChat
- **接口地址**：`http://localhost:7860`
- **自定义模型**：输入 `+HY-Image-3.5-Preview-4090-Tob-v1.1`
- 直接在对话框中发送你想画的画，即可在回复中直接看到生成的图片！

---

## 📄 开源许可
MIT License.
