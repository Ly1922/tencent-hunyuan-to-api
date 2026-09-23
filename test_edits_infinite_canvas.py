import io
import time
import httpx
from PIL import Image

def test_infinite_canvas_edits():
    # 模拟无限画布上传 2 张参考底图
    img1 = Image.new("RGB", (64, 64), color="black")
    img2 = Image.new("RGB", (64, 64), color="white")
    
    b1 = io.BytesIO()
    img1.save(b1, format="PNG")
    b1.seek(0)
    
    b2 = io.BytesIO()
    img2.save(b2, format="PNG")
    b2.seek(0)

    # 关键：无限画布在多图时使用的 field 名字就是 'image[]'
    files = [
        ("image[]", ("ref1.png", b1.getvalue(), "image/png")),
        ("image[]", ("ref2.png", b2.getvalue(), "image/png"))
    ]
    data = {
        "prompt": "两件衣服并排展示，黑白撞色",
        "model": "HY-Image-3.5-Preview-4090-Tob-v1.1",
        "size": "1:1",
        "response_format": "b64_json"
    }

    print("--- 正在模拟无限画布发起 POST /v1/images/edits (携带 2 张 image[] 底图) ---")
    start = time.time()
    resp = httpx.post("http://127.0.0.1:7860/v1/images/edits", data=data, files=files, timeout=120.0)
    print(f"Status Code: {resp.status_code} (耗时: {time.time() - start:.2f}s)")
    if resp.status_code == 200:
        res_json = resp.json()
        item = res_json["data"][0]
        has_url = bool(item.get("url"))
        has_b64 = bool(item.get("b64_json"))
        print(f"[SUCCESS] 无限画布多图编辑接口调用成功！")
        print(f"  返回包含 url: {has_url}")
        print(f"  返回包含 b64_json: {has_b64} (长度: {len(item.get('b64_json') or '')})")
    else:
        print("[FAILED] 调用失败:", resp.text)

if __name__ == "__main__":
    test_infinite_canvas_edits()
