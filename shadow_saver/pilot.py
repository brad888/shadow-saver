"""Loopback-only, manual-review shopping pilot. No payouts are performed."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parent.parent
SOURCE_HOSTS = ('jd.com', 'taobao.com', 'tmall.com', 'tb.cn', '3.cn')
PROMO_HOSTS = SOURCE_HOSTS + ('click.taobao.com',)


def validate_url(url, hosts=SOURCE_HOSTS):
    p = urlparse(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None,443) or not p.hostname or not any(p.hostname == h or p.hostname.endswith('.'+h) for h in hosts):
        raise ValueError('仅支持淘宝、天猫和京东的 HTTPS 商品链接')
    return url


def money(value):
    try:
        n=Decimal(str(value or 0))
        return n if n.is_finite() and n >= 0 else Decimal(0)
    except InvalidOperation: return Decimal(0)


def cents(n): return str(n.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def normalize(row, platform):
    price=money(row.get('itemendprice') or row.get('itemprice'))
    coupon=money(row.get('couponmoney'))
    start=money(row.get('couponstarttime')); end=money(row.get('couponendtime'))
    now=Decimal(str(time.time()))
    unavailable=coupon > 0 and (start>now or (end>0 and end<now))
    condition=str(row.get('couponinfo') or row.get('couponexplain') or '')
    conditional=any(x in condition for x in ('88VIP','会员','首单','新客','PLUS'))
    commission=money(row.get('commission') if platform=='京东' else row.get('tkmoney'))
    return {'item_id':str(row.get('itemid') or ''),'title':str(row.get('goodsname') or row.get('itemtitle') or ''),
            'platform':platform,'shop':str(row.get('shopname') or ''),'price':cents(price) if price>0 and not unavailable else None,
            'coupon':cents(coupon),'condition':condition[:300], 'conditional':conditional,
            'unavailable':unavailable,'rebate':cents(commission*Decimal('.20')) if not unavailable else '0.00',
            'commission':cents(commission),'coupon_url':str(row.get('couponurl') or '') if not unavailable else ''}


class TitleParser(HTMLParser):
    def __init__(self): super().__init__(); self.active=False; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='title': self.active=True
    def handle_endtag(self,tag):
        if tag=='title': self.active=False
    def handle_data(self,data):
        if self.active: self.parts.append(data)


async def resolve_title(client,url):
    for _ in range(6):
        validate_url(url)
        async with client.stream('GET',url,follow_redirects=False) as response:
            if response.is_redirect:
                url=urljoin(url,response.headers.get('location','')); continue
            response.raise_for_status()
            content=bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content)>2_000_000: raise ValueError('页面过大，请补充商品名称')
            parser=TitleParser(); parser.feed(content.decode('utf-8','replace'))
            title=''.join(parser.parts)
            title=re.split(r'【图片|【价格|[-_]京东| - 天猫| - 淘宝',title)[0].strip()
            if not title or any(s in title for s in ('登录','安全验证','访问受限')): break
            return title[:100]
    raise ValueError('未能读取商品名称，请在下方补充品牌、型号和规格')


class Provider:
    def __init__(self,config): self.config=config
    async def call(self,client,endpoint,params,post=False):
        params={'apikey':self.config.get('HAODANKU_API_KEY',''),**params}
        response=await (client.post('https://v3.api.haodanku.com/'+endpoint,data=params) if post else client.get('https://v3.api.haodanku.com/'+endpoint,params=params))
        response.raise_for_status(); payload=response.json()
        if str(payload.get('code')) not in ('1','200'): raise ValueError('接口暂未返回可用结果或尚无权限')
        return payload.get('data')
    async def search(self,client,keyword,platform):
        data=await self.call(client,'supersearch' if platform=='淘宝' else 'jd_goods_search',{'keyword':quote(keyword),'min_id':1,'back':10})
        if not isinstance(data,list): raise ValueError('接口返回格式异常')
        rows=[normalize(r,platform) for r in data if isinstance(r,dict)]
        # Exclude an explicitly different alphanumeric model (e.g. XM6 for XM5).
        models=re.findall(r'[A-Za-z]+[- ]?\d+[A-Za-z\d-]*',keyword)
        def matches(row):
            title=re.sub(r'[- ]','',row['title']).lower()
            for model in models:
                model=re.sub(r'[- ]','',model).lower()
                prefix=re.match(r'[a-z]+',model).group()
                first=re.search(re.escape(prefix)+r'\d+[a-z\d]*',title)
                if model not in title or (first and first.group()!=model): return False
            return True
        return [r for r in rows if r['item_id'] and matches(r)]
    async def convert(self,client,item,tracking):
        if item['platform']=='京东':
            pid=self.config.get('JD_PID','')
            params={'material_id':item['item_id'],'union_id':pid.split('_')[0],'pid':pid,'subUnionId':tracking}
            if item.get('coupon_url'):
                validate_url(item['coupon_url'])
                params['coupon_url']=item['coupon_url']
            data=await self.call(client,'get_jditems_link',params,True)
            url=data.get('short_url') or data.get('click_url'); token=''
        else:
            data=await self.call(client,'ratesurl',{'itemid':item['item_id'],'pid':self.config.get('TAOBAO_PID',''),'tb_name':self.config.get('HAODANKU_TB_NAME',''),'get_taoword':1,'title':item['title']},True)
            url=data.get('coupon_click_url') or data.get('link') or data.get('item_url'); token=data.get('taoword','')
        validate_url(url or '',PROMO_HOSTS)
        return url,str(token)


def create_pilot_app(database_path=None,config=None,provider=None,public_base_url=''):
    config=dict(os.environ) if config is None else config
    provider=provider or Provider(config)
    database_path=database_path or str(ROOT/'pilot.db')
    @contextmanager
    def db():
        connection=sqlite3.connect(database_path); connection.row_factory=sqlite3.Row
        try:
            with connection: yield connection
        finally: connection.close()
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS searches(id TEXT PRIMARY KEY,owner TEXT,items TEXT);
        CREATE TABLE IF NOT EXISTS promotions(id TEXT PRIMARY KEY,owner TEXT,platform TEXT,title TEXT,url TEXT,rebate TEXT);
        CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,owner TEXT,promotion_id TEXT,platform TEXT,order_no TEXT,status TEXT,created_at INTEGER,UNIQUE(platform,order_no));''')
    os.chmod(database_path,0o600)
    from .ledger import Ledger
    ledger=Ledger(db)
    app=FastAPI(docs_url=None,redoc_url=None); app.state.db=db; app.state.ledger=ledger
    app.state.require_login=False
    app.state.session_valid=lambda owner: False
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver']+([urlparse(public_base_url).hostname] if public_base_url else []))
    @app.middleware('http')
    async def visitor(request,call_next):
        identity=request.cookies.get('pilot_visitor','')
        if not re.fullmatch(r'[a-f0-9]{48}',identity): identity=secrets.token_hex(24)
        request.state.owner=identity
        if app.state.require_login and (request.url.path=='/' or request.url.path.startswith('/api/')) and not request.url.path.startswith('/api/result/'):
            if not app.state.session_valid(identity):
                if request.url.path.startswith('/api/'):
                    return JSONResponse({'detail':'入口已失效，请重新向公众号发送商品信息。'},status_code=401)
                return HTMLResponse('请向影省狸公众号发送商品链接或名称，从回复中的入口打开。',status_code=401)
        if request.method=='POST':
            origin=request.headers.get('origin')
            if origin and origin != (public_base_url or str(request.base_url).rstrip('/')):
                return HTMLResponse('不允许跨站请求',status_code=403)
            if int(request.headers.get('content-length','0'))>8192: return HTMLResponse('请求过大',status_code=413)
        response=await call_next(request)
        response.set_cookie('pilot_visitor',request.state.owner,httponly=True,secure=bool(public_base_url),samesite='lax' if public_base_url else 'strict',max_age=2592000)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    @app.get('/',response_class=HTMLResponse)
    async def index(): return (ROOT/'shadow_saver/static/pilot.html').read_text()
    @app.post('/api/search')
    async def search(request:Request):
        body=await request.json(); raw=str(body.get('query','')).strip(); supplement=str(body.get('name','')).strip()
        if not raw or len(raw)>2000 or len(supplement)>120: raise HTTPException(400,'请输入商品链接或名称，名称不超过120字')
        if not config.get('HAODANKU_API_KEY'): raise HTTPException(503,'尚未配置好单库接口密钥')
        async with httpx.AsyncClient(timeout=25,headers={'User-Agent':'Mozilla/5.0'}) as client:
            keyword=raw
            match=re.search(r'https?://[^\s]+',raw)
            if match:
                try:
                    validate_url(match.group())
                    keyword=supplement or await resolve_title(client,match.group())
                except (ValueError,httpx.HTTPError) as error:
                    raise HTTPException(400,str(error) if isinstance(error,ValueError) else '商品页面暂不可读，请补充商品名称')
            elif len(raw)>120: raise HTTPException(400,'商品名称请控制在120字内')
            results=await asyncio.gather(*(provider.search(client,keyword,p) for p in ('淘宝','京东')),return_exceptions=True)
        items=[]; platforms=[]
        for p,result in zip(('淘宝','京东'),results):
            if isinstance(result,BaseException): platforms.append({'platform':p,'error':'查询暂不可用，请稍后重试（可能涉及接口权限或额度）'})
            else:
                for row in result:
                    row['id']=secrets.token_hex(8); items.append(row)
                platforms.append({'platform':p,'count':len(result)})
        items.sort(key=lambda x: (x['price'] is None,x['conditional'],money(x['price']) if x['price'] else Decimal('Infinity')))
        sid=secrets.token_hex(16)
        with db() as c: c.execute('INSERT INTO searches VALUES (?,?,?)',(sid,request.state.owner,json.dumps(items,ensure_ascii=False)))
        public=[{k:v for k,v in i.items() if k not in ('item_id','coupon_url')} for i in items]
        return {'id':sid,'keyword':keyword,'platforms':platforms,'items':public}
    @app.post('/api/promotions')
    async def promote(request:Request):
        body=await request.json()
        with db() as c: row=c.execute('SELECT items FROM searches WHERE id=? AND owner=?',(str(body.get('search_id','')),request.state.owner)).fetchone()
        if not row: raise HTTPException(404,'查询记录不存在')
        item=next((i for i in json.loads(row['items']) if i['id']==body.get('item_id')),None)
        if not item: raise HTTPException(404,'商品不存在')
        if item['unavailable']: raise HTTPException(400,'优惠尚未生效或已过期，请重新查询')
        pid=secrets.token_hex(16)
        try:
            async with httpx.AsyncClient(timeout=25) as client: url,token=await provider.convert(client,item,pid)
        except Exception: raise HTTPException(502,'推广链接生成失败，请确认授权和接口额度后重试')
        with db() as c: c.execute('INSERT INTO promotions VALUES (?,?,?,?,?,?)',(pid,request.state.owner,item['platform'],item['title'],url,item['rebate']))
        return {'id':pid,'url':url,'token':token,'title':item['title'],'rebate':item['rebate']}
    @app.post('/api/claims')
    async def claim(request:Request):
        body=await request.json(); number=str(body.get('order_no','')).strip()
        if not re.fullmatch(r'\d{8,30}',number): raise HTTPException(400,'请输入8～30位数字订单号')
        with db() as c:
            promo=c.execute('SELECT * FROM promotions WHERE id=? AND owner=?',(str(body.get('promotion_id','')),request.state.owner)).fetchone()
        if not promo: raise HTTPException(404,'请先生成自己的推广链接')
        from .ledger import LedgerError, OrderConflict
        try:
            order=ledger.register(request.state.owner,promo['platform'],number,promotion_id=promo['id'],title=promo['title'])
            if not order.get('created',True): raise HTTPException(409,'该平台订单已登记，请勿重复提交')
        except OrderConflict: raise HTTPException(409,'该平台订单已登记，请勿重复提交')
        except LedgerError: raise HTTPException(400,'订单登记失败，请核对格式')
        return {'id':order['id'],'status':order['status']}
    @app.get('/api/claims')
    async def claims(request:Request):
        fields={'id','platform','order_no','status','created_at','title','rebate',
                'actual_commission_cents','rebate_cents','paid_cents','updated_at'}
        return [{k:v for k,v in row.items() if k in fields} for row in ledger.list_orders(request.state.owner)]
    return app
