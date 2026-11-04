import asyncio

import httpx
import pytest

from shadow_saver.config import Settings
from shadow_saver.vision import identify_wechat_image


def test_wechat_media_is_identified_without_evaluating_model_output():
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        if request.url.path == "/cgi-bin/media/get":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage", headers={"content-type": "image/png"})
        if request.url.path == "/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"query":"索尼 WH-1000XM5 耳机"}'}}]})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings("token", "https://example.com", wechat_app_id="app", wechat_app_secret="secret", deepseek_api_key="key")
    query = asyncio.run(identify_wechat_image("media-id", settings, client))
    assert query == "索尼 WH-1000XM5 耳机"
    assert len(calls) == 3
    asyncio.run(client.aclose())


def test_invalid_model_json_is_rejected():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "token"})
        if request.url.path == "/cgi-bin/media/get":
            return httpx.Response(200, content=b"\xff\xd8\xffimage", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings("token", "https://example.com", wechat_app_id="app", wechat_app_secret="secret", deepseek_api_key="key")
    with pytest.raises(ValueError):
        asyncio.run(identify_wechat_image("media-id", settings, client))
    asyncio.run(client.aclose())
