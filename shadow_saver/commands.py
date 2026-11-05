"""Local WeChat commands; never invoke search, paid parsing or payment APIs."""
import hashlib
import hmac
import re

from .ledger import LedgerError, OrderConflict


def wechat_owner(token, user):
    return hmac.new(token.encode(),('shopping:'+user).encode(),hashlib.sha256).hexdigest()[:48]


def masked_order(number):
    return '*'*max(4,len(number)-4)+number[-4:]


def yuan(cents):
    return f'{cents//100}.{cents%100:02d}'


class ChatCommands:
    def __init__(self,ledger,identity_secret,direct_enabled=False,owner_resolver=None):
        self.ledger=ledger;self.secret=identity_secret;self.direct_enabled=direct_enabled
        self.resolve_owner=owner_resolver or (lambda incoming:wechat_owner(self.secret,incoming.from_user))

    def handle(self,incoming):
        if incoming.input_type!='text': return None
        content=incoming.input_value.strip()
        owner=self.resolve_owner(incoming)
        if content in {'帮助','菜单','你好'}:
            query=('发商品分享链接：直接查询转链。' if self.direct_enabled else
                   '原始分享链接直接转链暂未开放，正在等待解析接口免费额度。')
            return ('影省狸使用帮助\n'+query+'\n'
                    '返利规则：查看核账与返款说明。\n'
                    '登记订单 京东 123456789012\n'
                    '登记订单 淘宝 123456789012\n'
                    '我的订单：查看最近5笔登记和处理状态。\n'
                    '登记不代表已匹配推广订单；返款须实际佣金结算后人工核实。')
        if content in {'返利规则','返利说明'}:
            return ('返利规则\n'
                    '1. 优惠券按购物平台规则领取和使用，实付以结算页为准。\n'
                    '2. 返利按实际可分配且已结算佣金的20%计算，四舍五入到分。\n'
                    '3. 确认收货不等于佣金已结算；登记订单也不代表已匹配。\n'
                    '4. 取消、退款、归因失败或无有效佣金时，可能没有返利。\n'
                    '5. 当前由人工核账、人工返款；没有自动提现或自动打款。\n'
                    '下单后可发“登记订单 京东 订单号”或“登记订单 淘宝 订单号”。')
        if content.startswith('登记订单'):
            parts=content.split()
            if len(parts)!=3:
                return '登记格式：登记订单 京东 123456789012\n或：登记订单 淘宝 123456789012'
            aliases={'淘宝':'淘宝','天猫':'淘宝','TB':'淘宝','tb':'淘宝','京东':'京东','JD':'京东','jd':'京东'}
            platform=aliases.get(parts[1])
            if not platform: return '目前仅支持登记淘宝或京东订单。'
            if not re.fullmatch(r'[0-9]{8,30}',parts[2]): return '订单号格式不正确，请填写8～30位数字订单号。'
            try:
                result=self.ledger.register(owner,platform,parts[2],title='聊天登记订单（待匹配推广记录）')
            except OrderConflict:
                return '这笔订单已登记，暂不能重复登记。请核对订单号；如有疑问需人工核实。'
            except LedgerError:
                return '订单暂时无法登记，请核对格式后重试。'
            prefix='登记成功' if result.get('created',True) else '这笔订单已登记'
            return (f'{prefix}：{platform} {masked_order(parts[2])}\n状态：{result["status"]}\n'
                    '当前仅保存核账线索，金额待核定。核实推广归属与实际佣金结算后，再确认可返利金额。')
        if content=='我的订单':
            rows=self.ledger.list_orders(owner)[:5]
            if not rows: return '你还没有登记订单。\n下单后发送：登记订单 京东 订单号\n或：登记订单 淘宝 订单号'
            lines=['最近登记的订单：']
            for row in rows:
                line=f'{row["platform"]} {masked_order(row["order_no"])} · {row["status"]}'
                if row['status']=='可返利' and row.get('rebate_cents') is not None:
                    line+=' · 可返利 ¥'+yuan(row['rebate_cents'])
                elif row['status']=='已返款' and row.get('paid_cents') is not None:
                    line+=' · 已记录返款 ¥'+yuan(row['paid_cents'])
                elif row['status'] in {'待人工核账','已匹配','待结算'}:
                    line+=' · 金额待核定'
                lines.append(line)
            lines.append('状态和返款记录由人工核实更新；登记金额不等于已收到佣金。')
            return '\n'.join(lines)
        return None
