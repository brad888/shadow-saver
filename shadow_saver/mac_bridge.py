"""macOS local connector. Native helper is optional and never runs in the backend."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from .desktop import DesktopPoller


class MacDriver:
    def __init__(self,helper,runner=subprocess.run):self.helper=Path(helper);self.runner=runner
    def _call(self,request):
        try:
            result=self.runner([str(self.helper)],input=json.dumps(request,ensure_ascii=False),text=True,
                               capture_output=True,timeout=10,check=True)
            data=json.loads(result.stdout)
            return data if isinstance(data,dict) else {'status':'driver_unavailable'}
        except (OSError,ValueError,subprocess.SubprocessError):return {'status':'driver_unavailable'}
    def snapshot(self,chat_name):return self._call({'action':'snapshot','chat_name':chat_name})
    def doctor(self,chat_name):return self._call({'action':'doctor','chat_name':chat_name})
    def send(self,chat_name,text,snapshot):
        # Display names do not authenticate a recipient; keep Mac output disabled.
        return False


def main(argv=None):
    p=argparse.ArgumentParser(description='Mac微信连接器：仅观察与诊断。Mac发送暂不开放，不能只凭昵称确认收件人。')
    p.add_argument('action',choices=['doctor','observe'])
    p.add_argument('--helper',required=True,type=Path)
    p.add_argument('--state',type=Path,default=Path('.mobile-data/mac-cursor.db'))
    p.add_argument('--config',type=Path)
    args=p.parse_args(argv)
    if sys.platform!='darwin':p.error('Mac UI driver requires macOS; Linux cloud backend remains independent')
    driver=MacDriver(args.helper)
    if args.action=='doctor':
        print(json.dumps(driver.doctor('文件传输助手'),ensure_ascii=False));return 0
    try:
        cfg=json.loads(args.config.read_text()) if args.config else {}
        # First release is deliberately scoped to the selected self-test, no guessed contacts.
        if cfg.get('chat_name','文件传输助手')!='文件传输助手':raise ValueError('unsupported self test target')
        backend=None # Read-only first release: no backend calls from Mac UI.
        poller=DesktopPoller(driver,backend,args.state,chat_name='文件传输助手',user_id=cfg.get('user_id','self-test'),
                             self_test=True,send_enabled=False)
        last=None
        while True:
            status=poller.poll()
            if status!=last:print(json.dumps({'event':'desktop_poll','status':status}),flush=True);last=status
            time.sleep(3)
    except KeyboardInterrupt:return 0
    except (OSError,ValueError,KeyError):
        p.exit(1,'Mac connector unavailable; inspect private configuration and run doctor.\n')


if __name__=='__main__':raise SystemExit(main())
