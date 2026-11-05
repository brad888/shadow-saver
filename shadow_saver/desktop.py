"""Conservative desktop polling over a platform driver; transport stays in IM bridge."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid
import secrets


def proof(snapshot):
    return hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class DesktopPoller:
    def __init__(self,driver,backend,state_path,*,chat_name,user_id,self_test=False,send_enabled=False,self_test_code=None):
        if self_test and chat_name!='文件传输助手':raise ValueError('Self test only supports 文件传输助手')
        self.driver=driver;self.backend=backend;self.chat=chat_name;self.user=user_id
        self.self_test=self_test;self.send_enabled=send_enabled
        self.self_test_code=self_test_code or secrets.token_hex(4)
        self.previous=None;self.session=None;self.epoch=uuid.uuid4().hex;self.sequence=0
        self.path=Path(state_path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS desktop_events(id TEXT PRIMARY KEY,status TEXT,created INTEGER)')

    def _valid(self,snapshot):
        if not isinstance(snapshot,dict) or snapshot.get('status')!='ready' or snapshot.get('chat_name')!=self.chat:
            return False
        if not isinstance(snapshot.get('session_id'),str) or not snapshot['session_id']:return False
        rows=snapshot.get('messages')
        return isinstance(rows,list) and len(rows)<=500 and all(isinstance(row,dict) and
            isinstance(row.get('text'),str) and len(row['text'])<=4000 and
            row.get('direction') in {'self','incoming','unknown'} for row in rows)

    def poll(self):
        current=self.driver.snapshot(self.chat)
        if not self._valid(current) or (self.send_enabled and current.get('identity_verified') is not True):
            self.previous=None;self.session=None
            return 'blocked'
        rows=current['messages']
        tokens=[hashlib.sha256(json.dumps([r['text'],r['direction']],ensure_ascii=False).encode()).hexdigest() for r in rows]
        if not self.previous or current['session_id']!=self.session:
            self.previous=tokens;self.session=current['session_id']
            return 'baseline'
        previous=self.previous
        self.previous=tokens
        if tokens==previous:return 'unchanged'
        # Only accept demonstrable append. Scrolling, OCR fluctuations, virtualized
        # rows and ambiguous repeated sequences create a new baseline, never work.
        if tokens[:len(previous)]!=previous or len(tokens)<=len(previous):return 'baseline'
        new=rows[len(previous):]
        for row in new:
            text=row['text'].strip()
            if self.self_test:
                prefix='/省钱 '+self.self_test_code+' '
                if not text.startswith(prefix):continue
                text=text[len(prefix):].strip()
            elif row['direction']!='incoming':continue
            if not text or text.startswith('[影省狸]'):continue
            if not self.send_enabled:continue
            self.sequence+=1
            message=SimpleNamespace(id=f'{self.epoch}:{self.sequence}',type='text',content=text)
            expected=proof(current)
            def guarded_send(reply):
                latest=self.driver.snapshot(self.chat)
                if not self._valid(latest) or proof(latest)!=expected:return False
                return self.driver.send(self.chat,('[影省狸] '+reply) if self.self_test else reply,latest)
            status=self.backend.receive(message,guarded_send,friend_id=self.user,conversation_id='private:'+self.user)
            with sqlite3.connect(self.path) as c:
                c.execute('INSERT INTO desktop_events VALUES (?,?,?)',(message.id,status,int(time.time())))
        return 'processed' if self.send_enabled else 'observed'
