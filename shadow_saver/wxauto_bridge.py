"""Optional Windows UI adapter. No wxauto import or desktop access at server startup."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from threading import Lock
from urllib.parse import urlsplit

import httpx


class DesktopBridge:
    def __init__(self,base_url,bridge_id,token,state_path,client=None):
        url=urlsplit(base_url)
        if url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError('HTTPS backend origin required')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',bridge_id) or not 32<=len(token)<=256:
            raise ValueError('Invalid bridge credentials')
        self.url=base_url.rstrip('/')+'/im/'+bridge_id+'/events'
        self.token=token;self.state=Path(state_path);self.lock=Lock()
        self.client=client or httpx.Client(timeout=10,follow_redirects=False)
        self.scope=hashlib.sha256(self.url.encode()).hexdigest()
        self.state.parent.mkdir(parents=True,exist_ok=True)
        self.state.touch(mode=0o600,exist_ok=True);self.state.chmod(0o600)
        with sqlite3.connect(self.state) as c:
            c.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,status TEXT NOT NULL)')

    def receive(self,message,send,*,friend_id,conversation_id):
        msg_id=getattr(message,'id',None)
        if not msg_id or getattr(message,'type',None)!='text':return 'ignored'
        content=getattr(message,'content','')
        if not isinstance(content,str) or not 0<len(content)<=2000:return 'ignored'
        key=hashlib.sha256(json.dumps([self.scope,friend_id,conversation_id,str(msg_id)]).encode()).hexdigest()
        # Serializes UI dispatch. Never retry an uncertain send automatically.
        with self.lock:
            with sqlite3.connect(self.state) as c:
                row=c.execute('SELECT status FROM deliveries WHERE id=?',(key,)).fetchone()
            if row:return row[0]
            try:
                r=self.client.post(self.url,headers={'Authorization':'Bearer '+self.token},json={
                    'user_id':friend_id,'conversation_id':conversation_id,'message_id':str(msg_id),
                    'kind':'text','content':content})
                r.raise_for_status();reply=r.json()['text']
                if not isinstance(reply,str) or not reply or len(reply)>4000:return 'retry'
            except (httpx.HTTPError,ValueError,KeyError):return 'retry'
            with sqlite3.connect(self.state) as c:
                claimed=c.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?)',(key,'uncertain')).rowcount
            if not claimed:return 'uncertain'
            try:
                result=send(reply)
                # UI return values vary by release: only an explicit truthy result
                # marks a confirmed call. An exception/unknown return stays uncertain.
                if not result:return 'uncertain'
            except Exception:return 'uncertain'
            with sqlite3.connect(self.state) as c:c.execute('UPDATE deliveries SET status=? WHERE id=?',('sent',key))
            return 'sent'


def make_callback(bridge,friend_type,*,friend_id,conversation_id,expected_sender):
    def callback(message,chat):
        # Group messages can also be FriendMessage; check the window and sender.
        if not isinstance(message,friend_type) or getattr(message,'sender',None)!=expected_sender:
            return
        try:info=chat.ChatInfo()
        except Exception:return
        if info.get('chat_type')!='friend' or info.get('chat_name')!=expected_sender:return
        bridge.receive(message,chat.SendMsg,friend_id=friend_id,conversation_id=conversation_id)
    return callback


def main(argv=None):
    parser=argparse.ArgumentParser(description='Windows wxauto 私聊连接器。仅监听明确配置的私聊；首次先用专用测试好友，避免主账号。')
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--state',required=True,type=Path)
    args=parser.parse_args(argv)
    if sys.platform!='win32':parser.error('wxauto requires Windows; server IM gateway can run on Mac/Linux')
    try:
        cfg=json.loads(args.config.read_text(encoding='utf-8'))
        library=cfg.get('library','wxauto4')
        if library not in {'wxauto4','wxautox4'}:raise ValueError()
        contacts=cfg['contacts']
        if not isinstance(contacts,list) or not 0<len(contacts)<=10:raise ValueError()
        names=set();ids=set()
        for item in contacts:
            if set(item)!={'chat_name','user_id'} or not all(isinstance(v,str) and v.strip() and len(v)<=100 for v in item.values()):raise ValueError()
            if item['chat_name'] in names or item['user_id'] in ids:raise ValueError()
            names.add(item['chat_name']);ids.add(item['user_id'])
        bridge=DesktopBridge(cfg['base_url'],cfg['bridge_id'],cfg['token'],args.state)
        module=importlib.import_module(library)
        friend_type=importlib.import_module(library+'.msgs').FriendMessage
        wx=module.WeChat()
        for item in contacts:
            callback=make_callback(bridge,friend_type,friend_id=item['user_id'],
                                   conversation_id='private:'+item['user_id'],expected_sender=item['chat_name'])
            wx.AddListenChat(item['chat_name'],callback)
        print('IM bridge listening to configured private chats. No message contents are logged.')
        wx.KeepRunning()
    except Exception:
        parser.exit(1,'Bridge startup failed. Check private configuration, library and desktop compatibility locally.\n')


if __name__=='__main__':raise SystemExit(main())
