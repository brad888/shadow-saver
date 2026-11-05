import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from shadow_saver.mobile import create_mobile_app
from shadow_saver.commands import wechat_owner
from test_mobile import config, send

TOKEN='test-bridge-token-that-is-at-least-32-characters'

def bridge_config():
    return {'IM_BRIDGES_JSON':json.dumps([{'id':'desktop','channel':'wechat_personal','account_id':'test-desktop','token':TOKEN}])}

def event(text='帮助',message='1',user='friend-one'):
    return dict(user_id=user,conversation_id='private:'+user,message_id=message,kind='text',content=text)

def test_bridge_is_disabled_by_default_and_bound_to_credential(tmp_path):
    with TestClient(create_mobile_app(config(tmp_path),config={}),base_url='https://shop.example.com') as c:
        assert c.post('/im/desktop/events',json=event()).status_code==404
    with TestClient(create_mobile_app(config(tmp_path),config=bridge_config()),base_url='https://shop.example.com') as c:
        assert c.post('/im/desktop/events',json=event()).status_code==403
        assert c.post('/im/desktop/events?token='+TOKEN,json=event()).status_code==403
        headers={'Authorization':'Bearer '+TOKEN}
        response=c.post('/im/desktop/events',headers=headers,json=event())
        assert response.status_code==200 and '登记订单 京东' in response.json()['text']
        assert c.post('/im/desktop/events',headers=headers,json={**event(),'channel':'wechat_official'}).status_code==422
        assert c.post('/im/desktop/events',headers=headers,json=event('x'*2001)).status_code==422
        assert c.get('/api/claims').status_code==401


def test_channels_share_commands_but_not_orders_and_preserve_legacy_identity(tmp_path):
    app=create_mobile_app(config(tmp_path),config=bridge_config())
    with TestClient(app,base_url='https://shop.example.com') as c:
        headers={'Authorization':'Bearer '+TOKEN}
        body=event('登记订单 京东 123456789012')
        first=c.post('/im/desktop/events',headers=headers,json=body).json()
        assert '登记成功' in first['text'] and not first['duplicate']
        again=c.post('/im/desktop/events',headers=headers,json=body).json()
        assert again['text']==first['text'] and again['duplicate']
        assert c.post('/im/desktop/events',headers=headers,json={**body,'content':'我的订单'}).status_code==409
        mine=c.post('/im/desktop/events',headers=headers,json=event('我的订单','2')).json()['text']
        assert '9012' in mine
        other=c.post('/im/desktop/events',headers=headers,json=event('我的订单','2',user='another')).json()['text']
        assert '9012' not in other
        assert '没有' in send(c,content='我的订单',user='friend-one')
        send(c,content='登记订单 淘宝 987654321012',msgid='official-order',user='friend-one')
        with app.state.db() as db:
            rows=db.execute('SELECT owner FROM claims ORDER BY rowid').fetchall()
        assert rows[1]['owner']==wechat_owner('mobile-test-token','friend-one')
        assert rows[0]['owner']!=rows[1]['owner']


def test_future_channel_uses_same_gateway_without_platform_sdk(tmp_path):
    from shadow_saver.im import IMMessage, IMGateway
    app=create_mobile_app(config(tmp_path),config={})
    calls=[]
    async def handler(incoming,message):
        calls.append(incoming.from_user)
        return 'ok:'+message.channel
    gateway=IMGateway(app.state.db,'secret',handler,legacy_wechat_account='wx-mobile-test')
    def receive(channel,account='account-a',content='hello'):
        return asyncio.run(gateway.receive(IMMessage(channel,account,'user','private','msg','text',content)))
    assert receive('qq').text=='ok:qq'
    assert receive('qq').duplicate
    assert receive('qq','account-b').text=='ok:qq'
    assert receive('alipay').text=='ok:alipay'
    assert len(set(calls))==3
    with pytest.raises(ValueError):receive('qq',content='different')


def test_bridge_images_are_not_treated_as_official_media_ids(tmp_path):
    app=create_mobile_app(config(tmp_path),config=bridge_config())
    with TestClient(app,base_url='https://shop.example.com') as c:
        body={**event(),'kind':'image','content':'untrusted-file-path'}
        r=c.post('/im/desktop/events',headers={'Authorization':'Bearer '+TOKEN},json=body)
        assert r.status_code==200 and '识图暂未开放' in r.json()['text']
        with app.state.db() as db:assert db.execute('SELECT count(*) FROM handoffs').fetchone()[0]==0


@pytest.mark.parametrize('bridges',[
    [{'id':'x','channel':'wechat_personal','account_id':'a','token':'short'}],
    [{'id':'x','channel':'wechat_official','account_id':'wx-mobile-test','token':TOKEN}],
])
def test_unsafe_bridge_configuration_fails_closed(tmp_path,bridges):
    with pytest.raises(ValueError):
        create_mobile_app(config(tmp_path),config={'IM_BRIDGES_JSON':json.dumps(bridges)})


def test_concurrent_retry_reserves_one_business_call(tmp_path):
    from shadow_saver.im import IMGateway, IMMessage, IMPending, IMRateLimit
    app=create_mobile_app(config(tmp_path),config={})
    calls=[]
    async def run():
        started=asyncio.Event();finish=asyncio.Event()
        async def handler(incoming,message):
            calls.append(1);started.set();await finish.wait();return 'done'
        gateway=IMGateway(app.state.db,'secret',handler,'official')
        message=IMMessage('qq','a','u','c','1','text','hi')
        task=asyncio.create_task(gateway.receive(message));await started.wait()
        with pytest.raises(IMPending):await gateway.receive(message)
        finish.set();assert (await task).text=='done'
        assert (await gateway.receive(message)).duplicate
        assert calls==[1]
        for number in range(2,21):
            assert (await gateway.receive(IMMessage('qq','a','u','c',str(number),'text','hi'))).text=='done'
        with pytest.raises(IMRateLimit):
            await gateway.receive(IMMessage('qq','a','u','c','21','text','hi'))
        assert (await gateway.receive(message)).duplicate
    asyncio.run(run())


def test_bridge_login_entry_uses_personal_owner_without_crossing_official(tmp_path):
    app=create_mobile_app(config(tmp_path),config=bridge_config())
    with TestClient(app,base_url='https://shop.example.com') as c:
        headers={'Authorization':'Bearer '+TOKEN}
        registered=c.post('/im/desktop/events',headers=headers,json=event('登记订单 京东 123456789012')).json()
        assert '登记成功' in registered['text']
        text=c.post('/im/desktop/events',headers=headers,json=event('耳机','2')).json()['text']
        link=text.split('https://shop.example.com')[1].splitlines()[0]
        c.post(link)
        rows=c.get('/api/claims').json()
        assert len(rows)==1 and rows[0]['order_no']=='123456789012'
        assert '没有' in send(c,content='我的订单',user='friend-one')


@pytest.mark.parametrize('elapsed',[60,86460])
def test_interrupted_reservation_becomes_safe_reply_instead_of_reexecuting(tmp_path,elapsed):
    from shadow_saver.im import IMGateway, IMMessage
    app=create_mobile_app(config(tmp_path),config={})
    calls=[]
    async def handler(incoming,message):
        calls.append(1)
        raise asyncio.CancelledError()
    gateway=IMGateway(app.state.db,'secret',handler,'official')
    message=IMMessage('qq','a','u','c','1','text','hi')
    with pytest.raises(asyncio.CancelledError):asyncio.run(gateway.receive(message))
    with app.state.db() as db:db.execute('UPDATE im_replies SET created=created-?',(elapsed,))
    reply=asyncio.run(gateway.receive(message))
    assert '中断' in reply.text and reply.duplicate and calls==[1]
