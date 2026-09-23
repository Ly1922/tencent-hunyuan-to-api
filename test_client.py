import httpx
import time

BASE_URL = "http://127.0.0.1:7860"

def test_models():
    print("\n--- 1. 测试 GET /v1/models ---")
    resp = httpx.get(f"{BASE_URL}/v1/models")
    print(f"Status: {resp.status_code}")
    print(resp.json())

def test_image_generation():
    print("\n--- 2. 测试 POST /v1/images/generations (OpenAI DALL-E 风格) ---")
    payload = {
        "prompt": "赛博朋克风格的未来城市，霓虹灯光，雨夜街道，电影级光影",
        "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
        "size": "16:9",
        "response_format": "url"
    }
    start = time.time()
    resp = httpx.post(f"{BASE_URL}/v1/images/generations", json=payload, timeout=120.0)
    print(f"Status: {resp.status_code} (耗时: {time.time() - start:.2f}s)")
    if resp.status_code == 200:
        data = resp.json()
        print("[SUCCESS] 成功生成图片：")
        print(f"图片直链: {data['data'][0]['url']}")
    else:
        print("[FAILED] 生成失败：", resp.text)

def test_chat_completion():
    print("\n--- 3. 测试 POST /v1/chat/completions (Chat 对话直出图片风格) ---")
    payload = {
        "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
        "messages": [
            {"role": "user", "content": "一只金毛寻回犬在草地上奔跑"}
        ]
    }
    start = time.time()
    resp = httpx.post(f"{BASE_URL}/v1/chat/completions", json=payload, timeout=120.0)
    print(f"Status: {resp.status_code} (耗时: {time.time() - start:.2f}s)")
    if resp.status_code == 200:
        data = resp.json()
        print("[SUCCESS] 成功接收回复：")
    else:
        print("[FAILED] 生成失败：", resp.text)

def test_multi_image_generation():
    import base64
    import io
    from PIL import Image

    print("\n--- 4. 测试多图参考生图 (POST /v1/images/generations 传2张底图) ---")
    # 生成两张微型测试图片 Base64
    img1 = Image.new("RGB", (64, 64), color="red")
    img2 = Image.new("RGB", (64, 64), color="blue")
    
    b1 = io.BytesIO()
    img1.save(b1, format="PNG")
    b64_1 = f"data:image/png;base64,{base64.b64encode(b1.getvalue()).decode()}"

    b2 = io.BytesIO()
    img2.save(b2, format="PNG")
    b64_2 = f"data:image/png;base64,{base64.b64encode(b2.getvalue()).decode()}"

    payload = {
        "prompt": "融合参考图一的红色与参考图二的蓝色，生成极光夜空下的现代雕塑",
        "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
        "size": "16:9",
        "images": [b64_1, b64_2],
        "response_format": "url"
    }
    start = time.time()
    resp = httpx.post(f"{BASE_URL}/v1/images/generations", json=payload, timeout=120.0)
    print(f"Status: {resp.status_code} (耗时: {time.time() - start:.2f}s)")
    if resp.status_code == 200:
        data = resp.json()
        print("[SUCCESS] 多图参考生图成功：")
        print(f"图片直链: {data['data'][0]['url']}")
    else:
        print("[FAILED] 多图参考生图失败：", resp.text)

if __name__ == "__main__":
    test_models()
    test_multi_image_generation()

