import hashlib
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from wechatpy.crypto import PrpCrypto, WeChatCrypto

from shadow_saver.app import create_app
from shadow_saver.config import Settings


def signed_query(token: str, timestamp: str = "123", nonce: str = "abc") -> str:
    signature = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()
    return f"timestamp={timestamp}&nonce={nonce}&signature={signature}"


def settings(tmp_path):
    return Settings(
        wechat_token="test-token",
        public_base_url="https://example.com",
        database_path=str(tmp_path / "queries.db"),
    )


def test_wechat_server_verification(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    response = client.get(f"/wechat?{signed_query('test-token')}&echostr=hello")
    assert response.status_code == 200
    assert response.text == "hello"
    assert client.get("/wechat?timestamp=123&nonce=abc&signature=bad&echostr=hello").status_code == 403


def test_text_message_creates_query_and_returns_result_link(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    message = b"""<xml><ToUserName><![CDATA[gh_test]]></ToUserName>
      <FromUserName><![CDATA[user123]]></FromUserName><CreateTime>123</CreateTime>
      <MsgType><![CDATA[text]]></MsgType>
      <Content><![CDATA[https://item.jd.com/123456.html]]></Content>
      <MsgId>1001</MsgId></xml>"""
    response = client.post(f"/wechat?{signed_query('test-token')}", content=message)
    assert response.status_code == 200
    assert "text/xml" in response.headers["content-type"]
    assert "已收到" in response.text
    assert "https://example.com/result/" in response.text
    result_url = response.text.split("https://example.com/result/")[1].splitlines()[0]
    result = client.get(f"/api/result/{result_url}")
    assert result.status_code == 200
    assert result.json()["source_platform"] == "jd"
    assert result.json()["offers"] == []


def test_image_message_is_accepted_without_fabricated_offer(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    message = b"""<xml><ToUserName>gh_test</ToUserName><FromUserName>user123</FromUserName>
      <CreateTime>123</CreateTime><MsgType>image</MsgType><MediaId>media123</MediaId>
      <MsgId>1002</MsgId></xml>"""
    response = client.post(f"/wechat?{signed_query('test-token')}", content=message)
    assert response.status_code == 200
    assert "已收到" in response.text
    result_id = response.text.split("https://example.com/result/")[1].splitlines()[0]
    result = client.get(f"/api/result/{result_id}").json()
    assert result["input_type"] == "image"
    assert result["status"] == "unavailable"
    assert result["offers"] == []


def test_shared_link_message_is_accepted(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    message = b"""<xml><ToUserName>gh_test</ToUserName><FromUserName>user123</FromUserName>
      <CreateTime>123</CreateTime><MsgType>link</MsgType><Url>https://item.jd.com/123456.html</Url>
      <Title>Product</Title><MsgId>1003</MsgId></xml>"""
    response = client.post(f"/wechat?{signed_query('test-token')}", content=message)
    assert response.status_code == 200
    result_id = response.text.split("https://example.com/result/")[1].splitlines()[0]
    result = client.get(f"/api/result/{result_id}").json()
    assert result["input_type"] == "link"
    assert result["source_platform"] == "jd"


def test_unsigned_message_and_unknown_result_are_rejected(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.post("/wechat", content=b"<xml/>").status_code == 403
    assert client.get("/api/result/no-such-result").status_code == 404


def test_unsupported_platform_does_not_wait_forever(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    message = b"""<xml><ToUserName>gh_test</ToUserName><FromUserName>user123</FromUserName>
      <CreateTime>123</CreateTime><MsgType>text</MsgType>
      <Content>https://item.taobao.com/item.htm?id=123</Content><MsgId>1004</MsgId></xml>"""
    response = client.post(f"/wechat?{signed_query('test-token')}", content=message)
    result_id = response.text.split("https://example.com/result/")[1].splitlines()[0]
    assert client.get(f"/api/result/{result_id}").json()["status"] == "unavailable"


def test_secure_message_is_decrypted_and_reply_is_encrypted(tmp_path):
    aes_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    app_id = "wx_test_app"
    crypto = WeChatCrypto("test-token", aes_key, app_id)
    config = Settings(
        wechat_token="test-token",
        public_base_url="https://example.com",
        database_path=str(tmp_path / "secure.db"),
        wechat_app_id=app_id,
        wechat_encoding_aes_key=aes_key,
    )
    client = TestClient(create_app(config))
    encrypted_echo = PrpCrypto(crypto.key).encrypt("secure-echo", app_id).decode()
    echo_signature = hashlib.sha1(
        "".join(sorted(("test-token", "123", "abc", encrypted_echo))).encode()
    ).hexdigest()
    challenge = client.get(
        "/wechat", params={"timestamp": "123", "nonce": "abc", "echostr": encrypted_echo,
                           "msg_signature": echo_signature},
    )
    assert challenge.status_code == 200
    assert challenge.text == "secure-echo"
    message = b"""<xml><ToUserName>gh_test</ToUserName><FromUserName>user123</FromUserName>
      <CreateTime>123</CreateTime><MsgType>text</MsgType>
      <Content>https://item.jd.com/123456.html</Content><MsgId>secure-1</MsgId></xml>"""
    encrypted = crypto.encrypt_message(message, "abc", "123")
    signature = ET.fromstring(encrypted).findtext("MsgSignature")
    response = client.post(
        f"/wechat?timestamp=123&nonce=abc&encrypt_type=aes&msg_signature={signature}",
        content=encrypted,
    )
    assert response.status_code == 200
    envelope = ET.fromstring(response.content)
    reply = crypto.decrypt_message(
        response.text, envelope.findtext("MsgSignature"),
        envelope.findtext("TimeStamp"), envelope.findtext("Nonce"),
    )
    assert "https://example.com/result/" in reply
    assert client.post(
        "/wechat?timestamp=123&nonce=abc&encrypt_type=aes&msg_signature=bad",
        content=encrypted,
    ).status_code == 403
    assert client.post(f"/wechat?{signed_query('test-token')}", content=message).status_code == 403


def test_redpacket_keyword_queries_official_activity_and_stores_token(tmp_path, monkeypatch):
    import httpx
    from dataclasses import replace
    from urllib.parse import parse_qsl
    original = httpx.AsyncClient

    def respond(request):
        form = dict(parse_qsl(request.content.decode()))
        if form['method'] == 'taobao.tbk.activity.info.get':
            return httpx.Response(200, json={'tbk_activity_info_get_response': {'data': {
                'page_name': '测试活动', 'page_start_time': '2020-01-01', 'page_end_time': '2099-01-01',
                'click_url': 'https://s.click.taobao.com/official'}}})
        return httpx.Response(200, json={'tbk_tpwd_create_response': {'data': {'model': '￥TEST￥'}}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    config = replace(settings(tmp_path), taobao_app_key='key', taobao_app_secret='secret',
                     taobao_pid='mm_111_222_333', taobao_activity_id='campaign')
    client = TestClient(create_app(config))
    message = '<xml><ToUserName>gh</ToUserName><FromUserName>user</FromUserName><MsgType>text</MsgType><Content>红包</Content><MsgId>activity-1</MsgId></xml>'
    reply = client.post(f"/wechat?{signed_query('test-token')}", content=message.encode())
    result_id = reply.text.split('https://example.com/result/')[1].splitlines()[0]
    result = client.get(f'/api/result/{result_id}').json()
    assert result['status'] == 'ready'
    assert result['offers'][0]['url'] == 'https://s.click.taobao.com/official'
    assert result['offers'][0]['tao_token'] == '￥TEST￥'


def test_redpacket_without_campaign_explains_unavailability(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    message = '<xml><ToUserName>gh</ToUserName><FromUserName>user</FromUserName><MsgType>text</MsgType><Content>红包</Content><MsgId>activity-2</MsgId></xml>'
    reply = client.post(f"/wechat?{signed_query('test-token')}", content=message.encode())
    assert '活动入口暂未开放' in reply.text
    assert 'https://example.com/result/' not in reply.text


def test_fixed_campaign_works_without_api_and_expired_result_hides_link(tmp_path):
    from dataclasses import replace
    from shadow_saver.store import QueryStore
    config = replace(settings(tmp_path), taobao_activity_title='双11超级红包',
        taobao_activity_url='https://s.click.taobao.com/fixed', taobao_activity_token='原样淘口令',
        taobao_activity_expires='2099-01-01T23:59:59+08:00')
    client = TestClient(create_app(config))
    message = '<xml><ToUserName>gh</ToUserName><FromUserName>user</FromUserName><MsgType>text</MsgType><Content>淘宝红包</Content><MsgId>fixed-1</MsgId></xml>'
    reply = client.post(f"/wechat?{signed_query('test-token')}", content=message.encode())
    result_id = reply.text.split('https://example.com/result/')[1].splitlines()[0]
    result = client.get(f'/api/result/{result_id}').json()
    assert result['offers'][0]['tao_token'] == '原样淘口令'
    assert result['offers'][0]['url'] == 'https://s.click.taobao.com/fixed'
    expired = {**result['offers'][0], 'expires_at': '2020-01-01T00:00:00+08:00'}
    QueryStore(config.database_path).set_offers(result_id, [expired])
    assert client.get(f'/api/result/{result_id}').json()['offers'] == []
