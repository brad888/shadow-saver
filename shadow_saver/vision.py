import base64
import json

import httpx

from .config import Settings


async def identify_wechat_image(media_id: str, settings: Settings, client: httpx.AsyncClient) -> str:
    if not all((settings.wechat_app_id, settings.wechat_app_secret, settings.deepseek_api_key)):
        raise RuntimeError("WeChat media and vision credentials are required")

    token_response = await client.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={
            "grant_type": "client_credential",
            "appid": settings.wechat_app_id,
            "secret": settings.wechat_app_secret,
        },
    )
    token_response.raise_for_status()
    token = token_response.json().get("access_token")
    if not token:
        raise ValueError("WeChat did not return an access token")

    media_response = await client.get(
        "https://api.weixin.qq.com/cgi-bin/media/get",
        params={"access_token": token, "media_id": media_id},
    )
    media_response.raise_for_status()
    image = media_response.content
    if len(image) > 10_000_000:
        raise ValueError("Image exceeds size limit")
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif image.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("WeChat media is not a supported image")

    image_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    vision_response = await client.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        json={
            "model": "deepseek-flash",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "识别图片中的商品，返回且只返回 JSON 对象，格式为 {\"query\":\"品牌 型号 关键规格\"}。无法识别商品时 query 为空字符串。"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
        },
    )
    vision_response.raise_for_status()
    content = vision_response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    query = parsed.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("No usable product description found")
    return query.strip()
