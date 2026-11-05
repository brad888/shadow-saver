from types import SimpleNamespace


def snapshot(messages,title='文件传输助手',session='window-1'):
    return {'status':'ready','chat_name':title,'session_id':session,'fingerprint':'snapshot-proof','identity_verified':True,
            'messages':[{'text':text,'direction':direction} for text,direction in messages]}


def test_history_baseline_then_only_new_explicit_self_test_command(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    seen=[]
    driver=SimpleNamespace(snapshot=lambda name:snapshot([('旧消息','self')]),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda message,send,**kwargs:seen.append(message.content) or 'sent')
    poller=DesktopPoller(driver,backend,tmp_path/'cursor.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    assert poller.poll()=='baseline'
    driver.snapshot=lambda name:snapshot([('旧消息','self'),('/省钱 test-code 帮助','self')])
    assert poller.poll()=='processed' and seen==['帮助']
    assert poller.poll()=='unchanged' and seen==['帮助']
    driver.snapshot=lambda name:snapshot([('旧消息','self'),('/省钱 test-code 帮助','self'),('[影省狸] 回复','self')])
    poller.poll();assert seen==['帮助']


def test_repeated_identical_text_is_distinct_when_append_is_observed(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    seen=[];rows=[('/省钱 test-code 帮助','self')]
    driver=SimpleNamespace(snapshot=lambda name:snapshot(rows),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda m,s,**k:seen.append(m.id) or 'sent')
    poller=DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    poller.poll();rows.append(('/省钱 test-code 帮助','self'));poller.poll();rows.append(('/省钱 test-code 帮助','self'));poller.poll()
    assert len(seen)==len(set(seen))==2


def test_window_change_history_reorder_and_restart_never_replay(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    seen=[]
    driver=SimpleNamespace(snapshot=lambda name:snapshot([('base','self')]),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda m,s,**k:seen.append(m) or 'sent')
    def make():return DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    poller=make();poller.poll()
    driver.snapshot=lambda name:snapshot([('/省钱 test-code 帮助','self')],session='window-2')
    assert poller.poll()=='baseline'
    assert make().poll()=='baseline' and seen==[]
    driver.snapshot=lambda name:snapshot([('/省钱 test-code 帮助','self'),('older history','self')],title='Other chat')
    assert poller.poll()=='blocked' and seen==[]


def test_reply_rechecks_exact_snapshot_and_blocks_changed_target(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    sent=[];current=snapshot([('old','self')])
    driver=SimpleNamespace(snapshot=lambda name:current,send=lambda *a:sent.append(a) or True)
    def receive(message,send,**kwargs):
        current['chat_name']='Someone else'
        assert send('reply') is False
        return 'uncertain'
    poller=DesktopPoller(driver,SimpleNamespace(receive=receive),tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    poller.poll();current['messages'].append({'text':'/省钱 test-code 帮助','direction':'self'})
    poller.poll();assert sent==[]


def test_observe_mode_never_sends_to_backend(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    rows=[('old','self')]
    driver=SimpleNamespace(snapshot=lambda name:snapshot(rows),send=lambda *a:False)
    backend=SimpleNamespace(receive=lambda *a,**k:(_ for _ in ()).throw(AssertionError()))
    poller=DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=False,self_test_code='test-code')
    poller.poll();rows.append(('/省钱 test-code 帮助','self'))
    assert poller.poll()=='observed'


def test_empty_loading_frame_does_not_arm_history_replay(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    seen=[];rows=[]
    driver=SimpleNamespace(snapshot=lambda name:snapshot(rows),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda m,s,**k:seen.append(m.content) or 'sent')
    poller=DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    assert poller.poll()=='baseline'
    rows.append(('/省钱 test-code 旧命令','self'))
    assert poller.poll()=='baseline' and seen==[]
    rows.append(('/省钱 test-code 新命令','self'))
    poller.poll();assert seen==['新命令']


def test_unverified_recipient_cannot_reach_backend_even_when_send_requested(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    rows=[('old','self')]
    def current():
        value=snapshot(rows);value['identity_verified']=False;return value
    driver=SimpleNamespace(snapshot=lambda name:current(),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda *a,**k:(_ for _ in ()).throw(AssertionError('unverified recipient')))
    poller=DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='test-code')
    assert poller.poll()=='blocked'


def test_partial_history_with_old_run_code_is_not_a_new_command(tmp_path):
    from shadow_saver.desktop import DesktopPoller
    seen=[];rows=[('anchor','self')]
    driver=SimpleNamespace(snapshot=lambda name:snapshot(rows),send=lambda *a:True)
    backend=SimpleNamespace(receive=lambda m,s,**k:seen.append(m.content) or 'sent')
    poller=DesktopPoller(driver,backend,tmp_path/'c.db',chat_name='文件传输助手',user_id='self',self_test=True,send_enabled=True,self_test_code='new-code')
    poller.poll();rows.append(('/省钱 old-code 历史订单命令','self'));poller.poll()
    assert seen==[]
    rows.append(('/省钱 new-code 帮助','self'));poller.poll()
    assert seen==['帮助']
