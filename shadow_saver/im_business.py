"""Shared shopping workflows; no platform SDK, XML or desktop UI operations."""
import hashlib
import secrets
import time

from .commands import ChatCommands
from .taobao import fixed_activity

ACTIVITY_WORDS={'红包','淘宝红包','领红包','活动','淘宝活动'}


class ShoppingMessages:
    def __init__(self,settings,db,ledger,direct_reply=None):
        self.settings=settings;self.db=db;self.direct_reply=direct_reply
        self.commands=ChatCommands(ledger,settings.wechat_token,direct_enabled=bool(direct_reply),
                                  owner_resolver=lambda incoming:incoming.from_user)

    def shopping_entry(self,incoming):
        # from_user is the resolved business owner, never a display nickname.
        owner=incoming.from_user
        message_key=hashlib.sha256((owner+':'+incoming.msg_id).encode()).hexdigest()
        query=incoming.input_value.strip()[:2000]
        if query in {'帮助','菜单','查询','你好'}:query=''
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO handoffs(token,msg_id,owner,query,expires) VALUES (?,?,?,?,?)',
                      (secrets.token_urlsafe(32),message_key,owner,query,int(time.time())+900))
            row=c.execute('SELECT token FROM handoffs WHERE msg_id=?',(message_key,)).fetchone()
        return '/open/'+row['token']

    async def __call__(self,incoming,message):
        content=self.commands.handle(incoming)
        if content is not None:return content
        settings=self.settings
        if incoming.input_value.strip() in ACTIVITY_WORDS:
            if message.channel=='wechat_official':return None # Existing official activity result page.
            offers=fixed_activity(settings.taobao_activity_title,settings.taobao_activity_url,
                                  settings.taobao_activity_token,settings.taobao_activity_expires)
            if not offers:return '活动入口暂未开放，请稍后再试。'
            offer=offers[0]
            return offer['title']+'\n'+offer['url']+'\n'+offer.get('tao_token','')
        if self.direct_reply:return await self.direct_reply(incoming)
        if incoming.input_type=='image':
            return '识图暂未开放，请发商品链接，或品牌、型号和规格，例如：索尼 WH-1000XM5 深夜蓝。'
        entry=self.shopping_entry(incoming)
        return (f'点击查询商品优惠：\n{settings.public_base_url}{entry}\n'
                '首次打开后请点“打开本次查询”，在收到回复后15分钟内完成登录。同一浏览器登录后可重复打开，请勿转发。优惠以平台结算页为准，返利需佣金结算后人工核实。')
