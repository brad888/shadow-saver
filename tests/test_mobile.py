import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from fastapi.testclient import TestClient
from shadow_saver.config import Settings
from shadow_saver.mobile import create_mobile_app
from wechatpy.crypto import WeChatCrypto


def config(tmp_path):
    return Settings(wechat_token='mobile-test-token',public_base_url='https://shop.example.com',database_path=str(tmp_path/'queries.db'),wechat_app_id='wx-mobile-test',wechat_encoding_aes_key='abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG')


def send(client,user='user-one',msgid='1',content='索尼耳机'):
    token='mobile-test-token';timestamp='123';nonce='abc'
    signature=hashlib.sha1(''.join(sorted((token,timestamp,nonce))).encode()).hexdigest()
    xml=f'<xml><ToUserName>gh_test</ToUserName><FromUserName>{user}</FromUserName><CreateTime>123</CreateTime><MsgType>text</MsgType><Content>{content}</Content><MsgId>{msgid}</MsgId></xml>'
    crypto=WeChatCrypto(token,'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG','wx-mobile-test')
    encrypted=crypto.encrypt_message(xml,nonce,timestamp)
    signed=ET.fromstring(encrypted).findtext('MsgSignature')
    r=client.post(f'/wechat?timestamp=123&nonce=abc&encrypt_type=aes&msg_signature={signed}',content=encrypted)
    assert r.status_code==200
    envelope=ET.fromstring(r.text)
    plain=crypto.decrypt_message(r.text,envelope.findtext('MsgSignature'),envelope.findtext('TimeStamp'),envelope.findtext('Nonce'))
    return ET.fromstring(plain).findtext('Content')


def test_wechat_entry_requires_confirmation_and_orders_stay_private(tmp_path):
    app=create_mobile_app(config(tmp_path),config={'HAODANKU_API_KEY':'test'})
    with TestClient(app,base_url='https://shop.example.com') as c:
        assert c.get('/api/claims').status_code==401
        reply=send(c)
        assert 'user-one' not in reply
        link=reply.split('https://shop.example.com')[1].splitlines()[0]
        assert send(c)==reply
        landing=c.post(link,follow_redirects=False)
        assert landing.status_code==303
        assert 'HttpOnly' in landing.headers['set-cookie']
        assert 'Secure' in landing.headers['set-cookie']
        entry=landing.headers['location'].split('entry=')[1]
        assert c.get('/api/entry/'+entry).json()=={'query':'索尼耳机'}
        assert c.get('/api/claims').status_code==200
        assert c.get(link,follow_redirects=False).status_code==303
    with TestClient(app,base_url='https://shop.example.com') as other:
        assert other.get('/api/entry/'+entry).status_code==401


def test_expired_entry_and_image_explanation(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        reply=send(c); link=reply.split('https://shop.example.com')[1].splitlines()[0]
        with app.state.db() as db: db.execute('UPDATE handoffs SET expires=0')
        assert c.get(link).status_code==410


def test_public_rate_limit_and_origin_enforcement(tmp_path):
    app=create_mobile_app(config(tmp_path),config={'HAODANKU_API_KEY':'test'})
    with TestClient(app,base_url='https://shop.example.com') as c:
        link=send(c).split('https://shop.example.com')[1].splitlines()[0]
        c.post(link)
        assert c.post('/api/claims',headers={'Origin':'https://evil.test'},json={}).status_code==403
        assert c.get('/',headers={'Host':'evil.test'}).status_code==400


def test_encrypted_callback_and_image_hint(tmp_path):
    from wechatpy.crypto import WeChatCrypto
    settings=replace(config(tmp_path),wechat_app_id='wx-test',wechat_encoding_aes_key='abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG')
    app=create_mobile_app(settings,config={})
    crypto=WeChatCrypto(settings.wechat_token,settings.wechat_encoding_aes_key,settings.wechat_app_id)
    with TestClient(app,base_url='https://shop.example.com') as client:
        for kind,body in [('text','<Content>https://item.jd.com/12345678.html</Content>'),('image','<MediaId>media-123</MediaId>')]:
            xml=f'<xml><ToUserName>gh_test</ToUserName><FromUserName>user-one</FromUserName><CreateTime>123</CreateTime><MsgType>{kind}</MsgType>{body}<MsgId>{kind}-1</MsgId></xml>'
            encrypted=crypto.encrypt_message(xml,'abc','123')
            signature=ET.fromstring(encrypted).findtext('MsgSignature')
            response=client.post('/wechat',params={'timestamp':'123','nonce':'abc','encrypt_type':'aes','msg_signature':signature},content=encrypted)
            envelope=ET.fromstring(response.text)
            plain=crypto.decrypt_message(response.text,envelope.findtext('MsgSignature'),envelope.findtext('TimeStamp'),envelope.findtext('Nonce'))
            if kind=='text': assert 'https://shop.example.com/open/' in plain
            else: assert '识图暂未开放' in plain
        assert client.post('/wechat?timestamp=123&nonce=abc&encrypt_type=aes&msg_signature=bad',content=encrypted).status_code==403


def test_mobile_search_rate_limit(tmp_path):
    from shadow_saver.pilot import normalize
    class Provider:
        async def search(self,client,keyword,platform): return []
    app=create_mobile_app(config(tmp_path),config={'HAODANKU_API_KEY':'test'},provider=Provider())
    with TestClient(app,base_url='https://shop.example.com') as c:
        link=send(c).split('https://shop.example.com')[1].splitlines()[0]
        c.post(link)
        for _ in range(12): assert c.post('/api/search',json={'query':'耳机'}).status_code==200
        assert c.post('/api/search',json={'query':'耳机'}).status_code==429


def test_two_wechat_users_cannot_read_each_others_entries(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as a, TestClient(app,base_url='https://shop.example.com') as b:
        linka=send(a,'one','1').split('https://shop.example.com')[1].splitlines()[0]
        linkb=send(b,'two','2').split('https://shop.example.com')[1].splitlines()[0]
        response=a.post(linka,follow_redirects=False);b.post(linkb)
        token=response.headers['location'].split('entry=')[1]
        assert b.get('/api/entry/'+token).status_code==404
        assert a.cookies.get('pilot_visitor')!=b.cookies.get('pilot_visitor')


def test_public_mobile_requires_encrypted_wechat_callback(tmp_path):
    import pytest
    with pytest.raises(RuntimeError,match='安全模式'):
        create_mobile_app(replace(config(tmp_path),wechat_app_id='',wechat_encoding_aes_key=''),config={})


def test_preview_does_not_consume_entry_and_same_browser_can_reopen(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        link=send(c).split('https://shop.example.com')[1].splitlines()[0]
        for _ in range(2):
            page=c.get(link,follow_redirects=False)
            assert page.status_code==200
            assert '打开本次查询' in page.text
        with app.state.db() as db: assert db.execute('SELECT used FROM handoffs').fetchone()[0]==0
        assert c.get('/api/claims').status_code==401
        result=c.post(link,follow_redirects=False)
        assert result.status_code==303
        assert c.get(link,follow_redirects=False).status_code==303
    with TestClient(app,base_url='https://shop.example.com') as other:
        page=other.get(link)
        assert page.status_code==410
        assert 'text/html' in page.headers['content-type']
        assert '重新发送' in page.text
        assert other.post(link).status_code==410


def test_expired_entry_has_readable_page(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        link=send(c).split('https://shop.example.com')[1].splitlines()[0]
        with app.state.db() as db: db.execute('UPDATE handoffs SET expires=0')
        page=c.get(link)
        assert page.status_code==410
        assert 'text/html' in page.headers['content-type']
        assert '15分钟' in page.text
        assert '{"detail"' not in page.text


def test_confirmed_browser_can_reopen_after_entry_deadline_but_not_after_session_expires(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        link=send(c).split('https://shop.example.com')[1].splitlines()[0]
        result=c.post(link,follow_redirects=False)
        token=result.headers['location'].split('entry=')[1]
        with app.state.db() as db: db.execute('UPDATE handoffs SET expires=0')
        assert c.get(link,follow_redirects=False).status_code==303
        assert c.get('/api/entry/'+token).json()=={'query':'索尼耳机'}
        with app.state.db() as db: db.execute('UPDATE sessions SET expires=0')
        assert c.get(link).status_code==410
        assert c.get('/api/entry/'+token).status_code==401
