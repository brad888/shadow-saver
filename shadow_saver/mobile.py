"""WeChat-authenticated mobile entry for the existing shopping pilot."""
import hashlib
import hmac
import os
import secrets
import time
from html import escape
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from .app import create_app
from .config import Settings
from .pilot import create_pilot_app


def create_mobile_app(settings=None, config=None, provider=None):
    settings=settings or Settings.from_env()
    if not settings.wechat_app_id or not settings.wechat_encoding_aes_key:
        raise RuntimeError('公众号手机内测必须配置微信安全模式 AppID 和 EncodingAESKey')
    config=dict(os.environ) if config is None else config
    path=config.get('PILOT_DATABASE_PATH') or str(Path(settings.database_path).with_name('pilot.db'))
    app=create_pilot_app(path,config,provider,public_base_url=settings.public_base_url)
    db=app.state.db
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS handoffs (
          token TEXT PRIMARY KEY,msg_id TEXT UNIQUE,owner TEXT NOT NULL,query TEXT NOT NULL,
          expires INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(owner TEXT PRIMARY KEY,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_limits(owner TEXT,action TEXT,window INTEGER,count INTEGER,
          PRIMARY KEY(owner,action,window));''')
    app.state.require_login=True
    def session_valid(owner):
        with db() as c:
            return bool(c.execute('SELECT 1 FROM sessions WHERE owner=? AND expires>?',(owner,int(time.time()))).fetchone())
    app.state.session_valid=session_valid

    def shopping_entry(incoming):
        owner=hmac.new(settings.wechat_token.encode(),('shopping:'+incoming.from_user).encode(),hashlib.sha256).hexdigest()[:48]
        # WeChat retries a MsgId; return the same entry without repeating provider work.
        token=secrets.token_urlsafe(32)
        message_key=hashlib.sha256((incoming.from_user+':'+incoming.msg_id).encode()).hexdigest()
        query=incoming.input_value.strip()[:2000]
        if query in {'帮助','菜单','查询','你好'}: query=''
        with db() as c:
            c.execute('INSERT OR IGNORE INTO handoffs(token,msg_id,owner,query,expires) VALUES (?,?,?,?,?)',
                      (token,message_key,owner,query,int(time.time())+900))
            row=c.execute('SELECT token FROM handoffs WHERE msg_id=?',(message_key,)).fetchone()
        return '/open/'+row['token']

    # Reuse the tested WeChat verification, decryption, encrypted replies and old activity routes.
    direct_reply=None
    if config.get('DIRECT_LINK_ENABLED')=='1':
        from .direct_reply import DirectReplyService
        direct_reply=DirectReplyService(config,db,settings.wechat_token).reply
    callback=create_app(settings,shopping_entry=shopping_entry,direct_reply=direct_reply)
    app.router.routes.extend(r for r in callback.router.routes if getattr(r,'path','') in
                            {'/wechat','/api/result/{query_id}','/result/{query_id}'})

    @app.middleware('http')
    async def limit(request,call_next):
        action=request.url.path
        owner=request.cookies.get('pilot_visitor','')
        if request.method=='POST' and action in {'/api/search','/api/promotions','/api/claims'} and session_valid(owner):
            window=int(time.time())//60
            with db() as c:
                count=c.execute('''INSERT INTO rate_limits VALUES (?,?,?,1)
                  ON CONFLICT(owner,action,window) DO UPDATE SET count=count+1 RETURNING count''',(owner,action,window)).fetchone()['count']
                c.execute('DELETE FROM rate_limits WHERE window<?',(window-2,))
            if count>12:
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail':'操作太频繁，请一分钟后再试'},status_code=429,headers={'Retry-After':'60'})
        response=await call_next(request)
        response.headers['Referrer-Policy']='no-referrer'
        return response

    def entry_page(message, token=None, status=200):
        form=(f'<form method="post" action="/open/{escape(token,quote=True)}"><button>打开本次查询</button></form>' if token else '')
        return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>影省狸 · 查询入口</title>
        <style>body{{font:17px/1.7 system-ui,sans-serif;color:#17362d;background:#f4f5ef;margin:0;padding:24px}}
        main{{max-width:520px;margin:12vh auto;background:white;border-radius:20px;padding:28px}}h1{{font-size:25px}}
        button{{font:inherit;background:#166549;color:white;border:0;border-radius:10px;padding:13px 24px;cursor:pointer}}</style>
        </head><body><main><h1>影省狸 · 商品查询</h1><p>{escape(message)}</p>{form}
        <p>请在微信里操作，不要转发查询入口。</p></main></body></html>''',status_code=status)

    def entry_state(token,request):
        with db() as c:
            row=c.execute('SELECT owner,used,expires FROM handoffs WHERE token=?',(token,)).fetchone()
        if row and row['used'] and row['owner']==request.state.owner and session_valid(row['owner']):
            return row,RedirectResponse('/?entry='+token,status_code=303)
        if not row or row['expires']<=int(time.time()):
            return row,entry_page('首次登录需在收到回复后的15分钟内完成。此入口已过期，请回到公众号重新发送商品链接或名称。',status=410)
        if row['used']:
            return row,entry_page('这个入口已在其他浏览器使用，当前浏览器尚未登录。请回到公众号重新发送商品链接或名称，再打开新回复。',status=410)
        return row,None

    @app.get('/open/{token}')
    async def show_entry(token:str,request:Request):
        row,response=entry_state(token,request)
        if response is not None: return response
        return entry_page('点击下方按钮进入商品查询。首次登录请在收到公众号回复后的15分钟内完成。',token)

    @app.post('/open/{token}')
    async def open_entry(token:str,request:Request):
        row,response=entry_state(token,request)
        if response is not None: return response
        now=int(time.time())
        with db() as c:
            row=c.execute('UPDATE handoffs SET used=1 WHERE token=? AND used=0 AND expires>? RETURNING owner',(token,now)).fetchone()
            if not row: return entry_page('入口状态已变化，请回到公众号重新发送商品链接或名称。',status=410)
            c.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET expires=excluded.expires',(row['owner'],now+2592000))
        request.state.owner=row['owner']
        return RedirectResponse('/?entry='+token,status_code=303)

    @app.get('/api/entry/{token}')
    async def entry_query(token:str,request:Request):
        with db() as c:
            row=c.execute('SELECT query FROM handoffs WHERE token=? AND owner=? AND used=1',(token,request.state.owner)).fetchone()
        if not row: raise HTTPException(404,'查询入口已失效，请重新发送商品信息')
        return {'query':row['query']}
    return app
