import asyncio
import hashlib
import json
from dataclasses import replace

import httpx
from fastapi.testclient import TestClient

from shadow_saver.direct_reply import ClipboardClient, DirectReplyService, sign_params
from shadow_saver.mobile import create_mobile_app
from shadow_saver.wechat import IncomingMessage
from test_mobile import config, send

CONFIG={'HAODANKU_V3_APP_ID':'app-test','HAODANKU_V3_APP_SECRET':'secret-test',
        'HAODANKU_TB_NAME':'测试账号','TAOBAO_PID':'mm_1_2_3','JD_PID':'123_456_789'}


def test_signature_matches_official_sorted_values_and_trailing_secret():
    assert sign_params({'z':0,'a':'中文'},'secret') == hashlib.md5('a中文z0secret'.encode()).hexdigest().upper()


def test_original_clipboard_uses_own_accounts_and_only_converted_fields():
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        return httpx.Response(200,json={'code':200,'data':{'platform':1,'item_title':'原始沙发',
          'item_url':'https://item.taobao.com/item.htm?id=123',
          'trans_url':{'item_url':'https://s.click.taobao.com/own'},'trans_tpwd':{'password_simple':'￥TEST￥'}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await ClipboardClient(CONFIG,c).convert('【淘宝】https://e.tb.cn/test','taobao')
    result=asyncio.run(run())
    assert result['url']=='https://s.click.taobao.com/own'
    assert result['token']=='￥TEST￥'
    body=seen[0]
    assert body['is_change']==1 and body['is_unify']==0
    assert body['tb_pid']=='mm_1_2_3' and body['jd_pid']=='123_456_789'
    assert body['jd_union_id']=='123'
    assert body['content']=='【淘宝】https://e.tb.cn/test'
    assert body['method']=='analyze.clipboard'
    assert 'app_secret' not in body


def test_unconverted_original_url_and_wrong_domain_are_rejected():
    import pytest
    async def run(data):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'code':200,'data':data}))) as c:
            with pytest.raises(ValueError): await ClipboardClient(CONFIG,c).convert('https://e.tb.cn/test','taobao')
    asyncio.run(run({'platform':1,'item_url':'https://item.taobao.com/item.htm?id=123'}))
    asyncio.run(run({'platform':1,'trans_url':{'item_url':'https://evil.test/pay'}}))
    asyncio.run(run({'platform':2,'trans_url':{'item_url':'https://u.jd.com/other'}}))


def test_encrypted_reply_has_direct_links_no_handoff_and_deduplicates(tmp_path):
    class Converter:
        calls=0
        async def convert(self,content,platform):
            self.calls+=1
            return {'title':'原始沙发','url':'https://s.click.taobao.com/own','token':'￥TEST￥','platform':'taobao'}
    converter=Converter()
    app=create_mobile_app(config(tmp_path),config={})
    service=DirectReplyService(CONFIG,app.state.db,'server-secret',converter=converter)
    # Test the same secure callback factory with a direct reply handler, not a live provider.
    from shadow_saver.app import create_app
    callback=TestClient(create_app(config(tmp_path),direct_reply=service.reply),base_url='https://shop.example.com')
    reply=send(callback,content='【淘宝】https://e.tb.cn/test')
    assert 'https://s.click.taobao.com/own' in reply and '￥TEST￥' in reply
    assert '/open/' not in reply and '15分钟' not in reply
    assert send(callback,content='【淘宝】https://e.tb.cn/test')==reply
    assert converter.calls==1


def test_deadline_failure_and_missing_credentials_do_not_expose_errors(tmp_path):
    class Slow:
        async def convert(self,*args): await asyncio.sleep(.1)
    app=create_mobile_app(config(tmp_path),config={})
    message=IncomingMessage('gh','user','slow','link','https://e.tb.cn/test','taobao')
    service=DirectReplyService(CONFIG,app.state.db,'secret',converter=Slow(),timeout_seconds=.01)
    assert '超时' in asyncio.run(service.reply(message))
    missing=DirectReplyService({},app.state.db,'secret')
    message=replace(message,msg_id='missing')
    assert '暂未开通' in asyncio.run(missing.reply(message))


def test_direct_feature_is_disabled_by_default_and_gate_can_be_enabled(tmp_path):
    app=create_mobile_app(config(tmp_path),config={'DIRECT_LINK_ENABLED':'1'})
    with TestClient(app,base_url='https://shop.example.com') as c:
        reply=send(c,content='https://e.tb.cn/test')
        assert '暂未开通' in reply and '/open/' not in reply
        help_reply=send(c,msgid='help',content='你好')
        assert '分享链接' in help_reply and '/open/' not in help_reply


def test_rate_limit_and_error_redaction(tmp_path):
    class Broken:
        calls=0
        async def convert(self,*args):
            self.calls+=1
            raise RuntimeError('secret-test must never appear')
    app=create_mobile_app(config(tmp_path),config={});converter=Broken()
    service=DirectReplyService(CONFIG,app.state.db,'secret',converter=converter)
    async def run():
        for i in range(12):
            msg=IncomingMessage('gh','user',str(i),'link','https://e.tb.cn/test','taobao')
            result=await service.reply(msg)
            assert 'secret-test' not in result and '暂时无法' in result
        result=await service.reply(replace(msg,msg_id='13'))
        assert '太频繁' in result
    asyncio.run(run());assert converter.calls==12


def test_jd_conversion_uses_same_item_conversion_result():
    async def run():
        payload={'code':200,'data':{'plat_type':2,'item_title':'原京东商品','trans_url':'{"item_url":"https://u.jd.com/own"}'}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=payload))) as c:
            result=await ClipboardClient(CONFIG,c).convert('https://item.jd.com/123.html','jd')
            assert result['url']=='https://u.jd.com/own'
            assert result['platform']=='jd' and result['token']==''
    asyncio.run(run())
