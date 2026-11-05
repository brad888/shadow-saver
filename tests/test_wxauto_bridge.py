from types import SimpleNamespace

import httpx


def test_only_configured_friend_messages_are_sent_and_delivery_persists(tmp_path):
    from shadow_saver.wxauto_bridge import DesktopBridge
    requests=[];sent=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'text':'返利规则20%','duplicate':len(requests)>1})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    bridge=DesktopBridge('https://shop.example.com','desktop','x'*32,tmp_path/'state.db',client=client)
    message=SimpleNamespace(id='stable-123',type='text',content='返利规则')
    bridge.receive(message,sent.append,friend_id='local-friend-id',conversation_id='local-private-id')
    bridge.receive(message,sent.append,friend_id='local-friend-id',conversation_id='local-private-id')
    assert sent==['返利规则20%'] and len(requests)==1
    body=__import__('json').loads(requests[0].content)
    assert body['user_id']=='local-friend-id' and body['message_id']=='stable-123'
    assert requests[0].headers['authorization']=='Bearer '+'x'*32
    restarted=DesktopBridge('https://shop.example.com','desktop','x'*32,tmp_path/'state.db',client=client)
    restarted.receive(message,sent.append,friend_id='local-friend-id',conversation_id='local-private-id')
    assert len(sent)==1


def test_ambiguous_send_is_not_retried_and_network_failure_can_retry(tmp_path):
    from shadow_saver.wxauto_bridge import DesktopBridge
    calls=[]
    def handler(request):
        calls.append(1)
        if len(calls)==1:raise httpx.ConnectError('private secret',request=request)
        return httpx.Response(200,json={'text':'reply','duplicate':True})
    bridge=DesktopBridge('https://shop.example.com','desktop','x'*32,tmp_path/'state.db',client=httpx.Client(transport=httpx.MockTransport(handler)))
    message=SimpleNamespace(id='m',type='text',content='帮助')
    def send(text):raise RuntimeError('UI may already have sent')
    assert bridge.receive(message,send,friend_id='f',conversation_id='c')=='retry'
    assert bridge.receive(message,send,friend_id='f',conversation_id='c')=='uncertain'
    assert bridge.receive(message,send,friend_id='f',conversation_id='c')=='uncertain'
    assert len(calls)==2


def test_missing_id_or_nontext_is_not_fabricated_or_sent(tmp_path):
    from shadow_saver.wxauto_bridge import DesktopBridge
    def fail(request):raise AssertionError('must not call backend')
    bridge=DesktopBridge('https://shop.example.com','desktop','x'*32,tmp_path/'state.db',client=httpx.Client(transport=httpx.MockTransport(fail)))
    for msg in [SimpleNamespace(type='text',content='hi'),SimpleNamespace(id='m',type='image',content='path')]:
        assert bridge.receive(msg,lambda text:None,friend_id='f',conversation_id='c')=='ignored'


def test_listener_filters_self_group_and_unexpected_sender(tmp_path):
    from shadow_saver.wxauto_bridge import make_callback
    accepted=[]
    class Friend:
        type='text';content='帮助';id='m';sender='Expected'
    callback=make_callback(SimpleNamespace(receive=lambda *a,**k:accepted.append(k)),Friend,
        friend_id='local-id',conversation_id='private-id',expected_sender='Expected')
    callback(SimpleNamespace(type='text',sender='Expected'),SimpleNamespace(SendMsg=lambda x:None,ChatInfo=lambda:{'chat_type':'friend','chat_name':'Expected'}))
    bad=Friend();bad.sender='unexpected';callback(bad,SimpleNamespace(SendMsg=lambda x:None,ChatInfo=lambda:{'chat_type':'friend','chat_name':'Expected'}))
    callback(Friend(),SimpleNamespace(SendMsg=lambda x:None,ChatInfo=lambda:{'chat_type':'friend','chat_name':'Expected'}))
    assert len(accepted)==1


def test_rotating_bridge_token_does_not_resend_same_delivery(tmp_path):
    from shadow_saver.wxauto_bridge import DesktopBridge
    client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'text':'reply'})))
    message=SimpleNamespace(id='stable',type='text',content='帮助')
    sent=[]
    def send(text):sent.append(text);return True
    first=DesktopBridge('https://shop.example.com','desktop','a'*32,tmp_path/'state.db',client=client)
    assert first.receive(message,send,friend_id='f',conversation_id='c')=='sent'
    second=DesktopBridge('https://shop.example.com','desktop','b'*32,tmp_path/'state.db',client=client)
    second.receive(message,send,friend_id='f',conversation_id='c')
    assert sent==['reply']
