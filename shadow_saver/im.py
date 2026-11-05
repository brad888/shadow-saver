"""Transport-independent IM contracts, scoped identities and idempotent dispatch."""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlparse

from .commands import wechat_owner
from .wechat import IncomingMessage, platform_from_host


class IMConflict(ValueError):
    pass


class IMPending(IMConflict):
    pass


class IMRateLimit(ValueError):
    pass


def _identifier(value):
    return isinstance(value,str) and 0<len(value)<=256 and value.strip()==value and not any(ord(c)<32 for c in value)


@dataclass(frozen=True)
class IMMessage:
    channel: str
    account_id: str
    user_id: str
    conversation_id: str
    message_id: str
    kind: str
    content: str

    def __post_init__(self):
        if not isinstance(self.channel,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',self.channel):
            raise ValueError('Invalid channel')
        if not all(_identifier(x) for x in (self.account_id,self.user_id,self.conversation_id,self.message_id)):
            raise ValueError('Invalid message identifier')
        if self.kind not in {'text','link','image'} or not isinstance(self.content,str) or not 0<len(self.content)<=2000:
            raise ValueError('Invalid message content')

    @classmethod
    def wechat(cls,incoming,account_id):
        return cls('wechat_official',account_id,incoming.from_user,incoming.from_user,
                   incoming.msg_id,incoming.input_type,incoming.input_value)


@dataclass(frozen=True)
class IMReply:
    text: str
    duplicate: bool = False


class IMGateway:
    def __init__(self,db,identity_secret,handler,legacy_wechat_account):
        self.db=db;self.secret=identity_secret;self.handler=handler;self.legacy=legacy_wechat_account
        with db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS im_replies (
              id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,reply TEXT,created INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS im_limits (
              owner TEXT,window INTEGER,count INTEGER,PRIMARY KEY(owner,window));''')

    def owner(self,message):
        if message.channel=='wechat_official' and message.account_id==self.legacy:
            return wechat_owner(self.secret,message.user_id)
        value=json.dumps([message.channel,message.account_id,message.user_id],ensure_ascii=False,separators=(',',':'))
        return hmac.new(self.secret.encode(),('im:'+value).encode(),hashlib.sha256).hexdigest()[:48]

    async def receive(self,message):
        owner=self.owner(message)
        scope=json.dumps([message.channel,message.account_id,message.conversation_id,message.message_id],ensure_ascii=False,separators=(',',':'))
        key=hmac.new(self.secret.encode(),scope.encode(),hashlib.sha256).hexdigest()
        fingerprint=hashlib.sha256(json.dumps([message.user_id,message.kind,message.content],ensure_ascii=False).encode()).hexdigest()
        now=int(time.time())
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM im_replies WHERE created<? AND reply IS NOT NULL',(now-86400,))
            c.execute('DELETE FROM im_limits WHERE window<?',(now//60-2,))
            cached=c.execute('SELECT fingerprint,reply,created FROM im_replies WHERE id=?',(key,)).fetchone()
            if cached:
                if cached['fingerprint']!=fingerprint:raise IMConflict('Message ID conflicts with prior content')
                if cached['reply'] is None:
                    if now-cached['created']<30:raise IMPending('Message is processing; retry later')
                    interrupted='消息处理曾中断或超时，无法确认结果。请先发“我的订单”核对；查询链接可重新发送。'
                    c.execute('UPDATE im_replies SET reply=?,created=? WHERE id=?',(interrupted,now,key))
                    return IMReply(interrupted,True)
                return IMReply(cached['reply'],True)
            count=c.execute('''INSERT INTO im_limits VALUES (?,?,1) ON CONFLICT(owner,window)
              DO UPDATE SET count=count+1 RETURNING count''',(owner,now//60)).fetchone()['count']
            if count>20:
                limited=True
            else:
                limited=False
                c.execute('INSERT INTO im_replies VALUES (?,?,NULL,?)',(key,fingerprint,now))
        if limited:raise IMRateLimit('Too many messages')
        match=re.search(r'https?://[^\s<>]+',message.content) if message.kind!='image' else None
        try:platform=platform_from_host((urlparse(match.group()).hostname or '').lower()) if match else None
        except ValueError:platform=None
        incoming=IncomingMessage(message.account_id,owner,key,'link' if match else message.kind,message.content,platform)
        try:
            text=await asyncio.wait_for(self.handler(incoming,message),timeout=4)
            if text is not None and (not isinstance(text,str) or not text or len(text.encode())>8000):
                raise ValueError('Unsupported reply')
        except Exception:
            # Keep a bounded, safe result on retry; never expose provider errors.
            text='消息处理暂时失败，请稍后重新发送。'
        with self.db() as c:
            if text is None:c.execute('DELETE FROM im_replies WHERE id=?',(key,))
            else:c.execute('UPDATE im_replies SET reply=? WHERE id=? AND reply IS NULL',(text,key))
        return IMReply(text) if text is not None else None
