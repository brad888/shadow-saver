"""Opt-in original-share conversion. Live V3 access requires approved quota."""
import asyncio
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx

from .pilot import cents, money, validate_url
from .wechat import platform_from_host


def sign_params(params, secret):
    value=''.join(str(k)+str(v) for k,v in sorted(params.items()) if k!='sign')+secret
    return hashlib.md5(value.encode('utf-8')).hexdigest().upper()


def object_value(value):
    if isinstance(value,str):
        try: value=json.loads(value)
        except (ValueError,TypeError): return {}
    return value if isinstance(value,dict) else {}


class ClipboardClient:
    def __init__(self,config,client=None): self.config=config; self.client=client

    async def convert(self,content,platform):
        cfg=self.config
        if not cfg.get('HAODANKU_V3_APP_ID') or not cfg.get('HAODANKU_V3_APP_SECRET'):
            raise ValueError('V3 unavailable')
        pid=cfg.get('JD_PID','')
        if platform=='taobao' and not all(cfg.get(k) for k in ('TAOBAO_PID','HAODANKU_TB_NAME')): raise ValueError('Own account unavailable')
        if platform=='jd' and not re.fullmatch(r'\d+_\d+_\d+',pid): raise ValueError('Own account unavailable')
        params={'method':'analyze.clipboard','app_id':cfg['HAODANKU_V3_APP_ID'],
                'date':datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'),
                'v':'3.0.1','content':content,'platform':1 if platform=='taobao' else 2,
                'is_change':1,'is_unify':0,'tb_name':quote(cfg.get('HAODANKU_TB_NAME','')),
                'tb_pid':cfg.get('TAOBAO_PID',''),'jd_union_id':pid.split('_')[0], 'jd_pid':pid}
        params['sign']=sign_params(params,cfg['HAODANKU_V3_APP_SECRET'])
        if self.client is None:
            async with httpx.AsyncClient(timeout=3) as client: return await self._request(client,params,platform)
        return await self._request(self.client,params,platform)

    async def _request(self,client,params,platform):
        response=await client.post('https://v3.api.haodanku.com/rest',json=params)
        response.raise_for_status(); payload=response.json()
        if str(payload.get('code'))!='200': raise ValueError('Conversion failed')
        data=object_value(payload.get('data'))
        actual=str(data.get('platform') or data.get('plat_type') or '')
        if actual != ('1' if platform=='taobao' else '2'): raise ValueError('Platform mismatch')
        converted=object_value(data.get('trans_url'))
        token=object_value(data.get('trans_tpwd'))
        url=converted.get('coupon_url') or converted.get('item_url') or converted.get('short_url') or ''
        if not isinstance(url,str) or len(url)>1000: raise ValueError('Invalid converted URL')
        validate_url(url,('taobao.com','tmall.com','tb.cn') if platform=='taobao' else ('jd.com','3.cn'))
        if any(secret and secret in url for secret in (self.config.get('HAODANKU_V3_APP_SECRET'),self.config.get('HAODANKU_V3_APP_ID'))):
            raise ValueError('Invalid converted URL')
        password=token.get('password_simple') or token.get('model') or ''
        if not isinstance(password,str) or len(password)>500: raise ValueError('Invalid token')
        return {'platform':platform,'title':str(data.get('item_title') or '商品')[:100], 'url':url,
                'token':password if platform=='taobao' else '', 'price':data.get('item_end_price'),
                'coupon_money':data.get('coupon_money'), 'coupon_start':data.get('coupon_start_time'),
                'coupon_end':data.get('coupon_end_time')}


class DirectReplyService:
    def __init__(self,config,db,identity_secret,converter=None,timeout_seconds=3.2):
        self.config=config;self.db=db;self.secret=identity_secret
        self.converter=converter or ClipboardClient(config);self.timeout=timeout_seconds
        with db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS direct_replies (
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,reply TEXT,created INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS direct_limits (
              owner TEXT,window INTEGER,count INTEGER,PRIMARY KEY(owner,window));''')

    async def reply(self,incoming):
        if incoming.input_type=='image': return '识图暂未开放，请发送淘宝或京东商品分享链接。'
        content=incoming.input_value.strip()
        if len(content)>2000: return '分享内容太长，请仅发送一条商品链接。'
        urls=re.findall(r'https?://[^\s<>]+',content)
        platform=None
        if urls:
            if len(urls)>1: return '请每次只发送一条商品分享链接。'
            try: platform=platform_from_host((urlparse(urls[0]).hostname or '').lower())
            except ValueError: return '商品链接格式不正确，请重新复制分享链接。'
            if platform not in {'taobao','jd'}: return '目前仅支持淘宝和京东商品分享链接。'
            try:
                for url in urls: validate_url(url.rstrip('，。；）】'))
            except ValueError: return '请发送淘宝或京东的 HTTPS 商品分享链接。'
        elif any(mark in content for mark in ('￥','¥','₴','£')):
            platform='taobao'
        else: return '请直接发淘宝或京东商品分享链接，我会在聊天里回复可用的推广链接／淘口令。'
        # Live access is opt-in and requires the separate V3 credentials.
        if not all(self.config.get(k) for k in ('HAODANKU_V3_APP_ID','HAODANKU_V3_APP_SECRET')):
            return '这类分享链接暂未开通解析转链，当前无法生成有效推广链接，请稍后再试。'
        owner=hmac.new(self.secret.encode(),incoming.from_user.encode(),hashlib.sha256).hexdigest()
        key=hashlib.sha256((owner+':'+incoming.msg_id).encode()).hexdigest();now=int(time.time())
        with self.db() as c:
            existing=c.execute('SELECT reply FROM direct_replies WHERE id=?',(key,)).fetchone()
            if existing: return existing['reply'] or '这条链接正在处理，请稍后再试。'
            c.execute('DELETE FROM direct_replies WHERE created<?',(now-86400,))
            c.execute('DELETE FROM direct_limits WHERE window<?',(now//60-2,))
            count=c.execute('''INSERT INTO direct_limits VALUES (?,?,1) ON CONFLICT(owner,window)
              DO UPDATE SET count=count+1 RETURNING count''',(owner,now//60)).fetchone()['count']
            if count>12: return '查询太频繁，请一分钟后再发商品链接。'
            created=c.execute('INSERT OR IGNORE INTO direct_replies VALUES (?,?,NULL,?)',(key,owner,now)).rowcount
            if not created: return '这条链接正在处理，请稍后再试。'
        try:
            result=await asyncio.wait_for(self.converter.convert(content,platform),timeout=self.timeout)
            reply=self.format_reply(result)
        except (TimeoutError,asyncio.TimeoutError): reply='这次转链超时，暂未生成可用链接，请稍后重新发送商品链接。'
        except Exception: reply='暂时无法为这条商品链接生成有效推广链接，请确认商品可推广或稍后重试。'
        with self.db() as c: c.execute('UPDATE direct_replies SET reply=? WHERE id=?',(reply,key))
        return reply

    @staticmethod
    def format_reply(result):
        lines=[result['title']]
        price=money(result.get('price')); coupon=money(result.get('coupon_money'))
        start=money(result.get('coupon_start'));end=money(result.get('coupon_end')); now=money(time.time())
        if price>0 and not (coupon>0 and (start>now or (end>0 and end<now))):
            lines.append('接口参考价 ¥'+cents(price)+'，以平台结算页为准。')
        lines+=['领券／购买：',result['url']]
        if result.get('token'): lines+=['复制淘口令后打开淘宝：',result['token']]
        lines+=['推广链接可能产生佣金；返利为实际可分配佣金的20%，佣金结算后人工核账。']
        reply='\n'.join(lines)
        if len(reply.encode('utf-8'))>2000: raise ValueError('Reply too long')
        return reply
