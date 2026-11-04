"""WeChat-authenticated mobile entry for the existing shopping pilot."""
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
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
    callback=create_app(settings,shopping_entry=shopping_entry)
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

    @app.get('/open/{token}')
    async def open_entry(token:str,request:Request):
        now=int(time.time())
        with db() as c:
            row=c.execute('UPDATE handoffs SET used=1 WHERE token=? AND used=0 AND expires>? RETURNING owner',(token,now)).fetchone()
            if not row: raise HTTPException(410,'入口已过期或已使用，请重新向公众号发送商品链接或名称。')
            c.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET expires=excluded.expires',(row['owner'],now+2592000))
        request.state.owner=row['owner']
        return RedirectResponse('/?entry='+token,status_code=303)

    @app.get('/api/entry/{token}')
    async def entry_query(token:str,request:Request):
        with db() as c:
            row=c.execute('SELECT query FROM handoffs WHERE token=? AND owner=? AND used=1 AND expires>?',(token,request.state.owner,int(time.time()))).fetchone()
        if not row: raise HTTPException(404,'查询入口已失效，请重新发送商品信息')
        return {'query':row['query']}
    return app
