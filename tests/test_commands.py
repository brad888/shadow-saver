import hashlib
import hmac
from fastapi.testclient import TestClient
from shadow_saver.commands import ChatCommands, wechat_owner
from shadow_saver.mobile import create_mobile_app
from test_mobile import config, send


def test_commands_work_without_external_keys_and_register_idempotently(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        help_text=send(c,content='帮助')
        assert '登记订单 京东' in help_text and '/open/' not in help_text
        rules=send(c,msgid='rules',content='返利规则')
        assert '20%' in rules and '实际' in rules and '结算' in rules
        first=send(c,msgid='order1',content='登记订单 京东 123456789012')
        assert '登记成功' in first and '待人工核账' in first
        duplicate=send(c,msgid='order2',content='登记订单 京东 123456789012')
        assert '已登记' in duplicate
        mine=send(c,msgid='mine',content='我的订单')
        assert '9012' in mine and '123456789012' not in mine
        assert '金额待核定' in mine
        foreign=send(c,user='other',msgid='other',content='我的订单')
        assert '没有' in foreign and '9012' not in foreign


def test_registration_help_invalid_platform_and_no_admin_commands(tmp_path):
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        assert '登记订单 京东' in send(c,content='登记订单')
        assert '格式' in send(c,msgid='bad',content='登记订单 京东 xyz')
        assert '淘宝或京东' in send(c,msgid='platform',content='登记订单 拼多多 123456789')
        with app.state.db() as db: assert db.execute('SELECT count(*) FROM claims').fetchone()[0]==0


def test_chat_and_web_use_the_same_identity(tmp_path):
    token='mobile-test-token'; user='user-one'
    expected=hmac.new(token.encode(),('shopping:'+user).encode(),hashlib.sha256).hexdigest()[:48]
    assert wechat_owner(token,user)==expected
    app=create_mobile_app(config(tmp_path),config={})
    with TestClient(app,base_url='https://shop.example.com') as c:
        send(c,content='登记订单 淘宝 1234567890')
        link=send(c,msgid='query',content='耳机').split('https://shop.example.com')[1].splitlines()[0]
        c.post(link)
        rows=c.get('/api/claims').json()
        assert len(rows)==1 and rows[0]['order_no']=='1234567890'
        assert c.cookies.get('pilot_visitor')==expected


def test_my_orders_only_uses_actual_approved_money(tmp_path):
    from shadow_saver.ledger import Ledger
    app=create_mobile_app(config(tmp_path),config={})
    ledger=Ledger(app.state.db)
    owner=wechat_owner('mobile-test-token','user-one')
    order=ledger.register(owner,'京东','123456789012')
    ledger.transition(order['id'],'match','tester',evidence='match-proof')
    ledger.transition(order['id'],'await_settlement','tester')
    ledger.transition(order['id'],'settle','tester',commission='13.99',evidence='settlement-proof')
    with TestClient(app,base_url='https://shop.example.com') as c:
        text=send(c,content='我的订单')
        assert '2.80' in text and '可返利' in text


def test_mobile_operations_stays_separate_from_customer_login(tmp_path):
    app=create_mobile_app(config(tmp_path),config={'OPS_STATUS_TOKEN':'test-ops'})
    with TestClient(app,base_url='https://shop.example.com') as c:
        assert c.get('/readyz').json()=={'status':'ready'}
        assert c.get('/healthz').json()=={'status':'alive'}
        assert c.get('/api/claims').status_code==401
        assert c.get('/ops/status').status_code==403
        status=c.get('/ops/status',headers={'Authorization':'Bearer test-ops'})
        assert status.status_code==200
        assert status.json()['database']=='ready'
        send(c,content='登记订单 京东 123456789012')
        entry=send(c,msgid='query',content='耳机').split('https://shop.example.com')[1].splitlines()[0]
        c.post(entry)
        row=c.get('/api/claims').json()[0]
        assert row['actual_commission_cents'] is None
        assert not {'owner','match_evidence','settlement_evidence'} & row.keys()
        assert c.get('/ops/status').status_code==403
