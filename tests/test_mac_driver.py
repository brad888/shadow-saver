import json
import subprocess



def test_native_driver_sends_json_over_stdin_never_args_or_shell(tmp_path):
    from shadow_saver.mac_bridge import MacDriver
    calls=[]
    def runner(args,**kwargs):
        calls.append((args,kwargs))
        return subprocess.CompletedProcess(args,0,stdout=json.dumps({'status':'ready','chat_name':'文件传输助手','session_id':'w','messages':[]}))
    binary=tmp_path/'helper';binary.touch()
    driver=MacDriver(binary,runner=runner)
    driver.snapshot('文件传输助手')
    assert calls[0][0]==[str(binary)]
    assert json.loads(calls[0][1]['input'])=={'action':'snapshot','chat_name':'文件传输助手'}
    assert not calls[0][1].get('shell',False)


def test_native_driver_fails_closed_on_bad_output_or_missing_permission(tmp_path):
    from shadow_saver.mac_bridge import MacDriver
    binary=tmp_path/'helper';binary.touch()
    def runner(args,**kwargs):return subprocess.CompletedProcess(args,0,stdout='private malformed response')
    driver=MacDriver(binary,runner=runner)
    assert driver.snapshot('文件传输助手')['status']=='driver_unavailable'
    def permission(args,**kwargs):return subprocess.CompletedProcess(args,0,stdout='{"status":"accessibility_required"}')
    driver=MacDriver(binary,runner=permission)
    assert driver.snapshot('文件传输助手')['status']=='accessibility_required'
    assert driver.send('文件传输助手','secret-message',{}) is False


def test_native_driver_rejects_nonready_snapshot_before_send(tmp_path):
    from shadow_saver.mac_bridge import MacDriver
    binary=tmp_path/'helper';binary.touch()
    def runner(*args,**kwargs):raise AssertionError('must not send')
    driver=MacDriver(binary,runner=runner)
    assert not driver.send('文件传输助手','hello',{'status':'unsupported_ui'})
