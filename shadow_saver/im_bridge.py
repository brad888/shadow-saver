"""Authenticated event adapter. Each credential is bound to one channel/account."""
import hmac
import json
import re
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .im import IMMessage, IMConflict, IMPending, IMRateLimit


class BridgeEvent(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=False)
    user_id:str=Field(min_length=1,max_length=256)
    conversation_id:str=Field(min_length=1,max_length=256)
    message_id:str=Field(min_length=1,max_length=256)
    kind:Literal['text','link','image']
    content:str=Field(min_length=1,max_length=2000)


def install_bridges(app,gateway,config):
    raw=config.get('IM_BRIDGES_JSON','')
    if not raw:return
    try:
        entries=json.loads(raw)
        if not isinstance(entries,list) or len(entries)>20:raise ValueError()
        bridges={};tokens=set()
        for entry in entries:
            if not isinstance(entry,dict) or set(entry)!={'id','channel','account_id','token'}:raise ValueError()
            bridge_id=entry['id'];token=entry['token']
            if not isinstance(bridge_id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',bridge_id):raise ValueError()
            if not isinstance(token,str) or not 32<=len(token)<=256 or not token.isascii() or token in tokens:raise ValueError()
            IMMessage(entry['channel'],entry['account_id'],'validation','validation','validation','text','validation')
            if entry['channel']=='wechat_official' or bridge_id in bridges:raise ValueError()
            bridges[bridge_id]=entry;tokens.add(token)
    except (TypeError,KeyError,ValueError):
        raise ValueError('Invalid IM bridge configuration') from None

    @app.post('/im/{bridge_id}/events',include_in_schema=False)
    async def event(bridge_id:str,request:Request):
        bridge=bridges.get(bridge_id)
        if not bridge:raise HTTPException(404,'Not found')
        supplied=request.headers.get('authorization','').encode()
        if not hmac.compare_digest(supplied,('Bearer '+bridge['token']).encode()):
            raise HTTPException(403,'Forbidden')
        # Manual validation after auth avoids exposing body validation to unauthenticated clients.
        try:
            data=BridgeEvent.model_validate_json(await request.body())
            message=IMMessage(bridge['channel'],bridge['account_id'],**data.model_dump())
        except ValueError:raise HTTPException(422,'Invalid message') from None
        try:reply=await gateway.receive(message)
        except IMPending:raise HTTPException(409,'Message processing; retry later') from None
        except IMConflict:raise HTTPException(409,'Message identifier conflict') from None
        except IMRateLimit:raise HTTPException(429,'Too many messages',headers={'Retry-After':'60'}) from None
        return {'text':reply.text if reply else '当前通道暂不支持此消息。','duplicate':bool(reply and reply.duplicate)}
