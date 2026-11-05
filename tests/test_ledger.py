"""Real SQLite ledger tests: historical estimates never authorize payments."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from shadow_saver.ledger import Ledger, LedgerError, OrderConflict


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'ledger.db'
    @contextmanager
    def db():
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()
    with db() as c:
        c.executescript('''
        CREATE TABLE promotions(id TEXT PRIMARY KEY,owner TEXT,platform TEXT,title TEXT,url TEXT,rebate TEXT);
        CREATE TABLE claims(id TEXT PRIMARY KEY,owner TEXT,promotion_id TEXT,platform TEXT,order_no TEXT,status TEXT,created_at INTEGER,UNIQUE(platform,order_no));
        INSERT INTO promotions VALUES ('p','alice','taobao','original title','url','99.99');
        INSERT INTO claims VALUES ('legacy','alice','p','taobao','123456','待人工核账',1);
        ''')
    return db, path


def ready(ledger, amount='13.99'):
    order = ledger.register('alice', 'jd', '998877')
    ledger.transition(order['id'], 'match', 'operator', evidence='affiliate report row 1')
    ledger.transition(order['id'], 'await_settlement', 'operator')
    return ledger.transition(order['id'], 'settle', 'operator', commission=amount, evidence='settlement report 42')


def test_migration_preserves_legacy_and_does_not_pay_estimate(database):
    db, _ = database
    ledger = Ledger(db)
    Ledger(db)  # Restart migration is idempotent.
    old = ledger.get_order('legacy')
    assert old['rebate'] == '99.99'
    assert old['title'] == 'original title'
    assert old['rebate_cents'] == old['paid_cents'] == 0
    assert old['actual_commission_cents'] is None
    assert old['status'] == '待人工核账'
    assert ledger.get_order('missing') is None
    with pytest.raises(LedgerError):
        ledger.transition('legacy', 'record_payment', 'op', amount='99.99', evidence='payment')


def test_registration_owner_dedup_and_unlinked_visibility(database):
    ledger = Ledger(database[0])
    first = ledger.register('bob', 'jd', '54321', title='chat order')
    again = ledger.register('bob', 'jd', '54321', title='different')
    assert first['created'] is True and again['created'] is False
    assert first['id'] == again['id']
    assert again['title'] == 'chat order'
    assert first['status'] == '待人工核账' and first['rebate_cents'] == 0
    with pytest.raises(OrderConflict):
        ledger.register('alice', 'jd', '54321')
    assert [row['id'] for row in ledger.list_orders('bob')] == [first['id']]
    assert len(ledger.list_orders('alice')) == 1
    with pytest.raises(LedgerError):
        ledger.register('bob', 'taobao', '33333', promotion_id='p')
    with pytest.raises(LedgerError):
        ledger.register('alice', 'jd', '33333', promotion_id='p')


def test_concurrent_registration_is_idempotent(database):
    ledger = Ledger(database[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: ledger.register('bob', 'jd', '44444'), range(4)))
    assert len({r['id'] for r in rows}) == 1
    assert sum(r['created'] for r in rows) == 1


@pytest.mark.parametrize('commission,commission_cents,want,status', [('13.99',1399,280,'可返利'),('0.03',3,1,'可返利'),('0',0,0,'无返利'),('0.01',1,0,'无返利')])
def test_actual_settlement_rounds_20_percent_half_up(database, commission, commission_cents, want, status):
    ledger = Ledger(database[0])
    row = ready(ledger, commission)
    assert row['rebate_cents'] == want and row['status'] == status
    assert row['actual_commission_cents'] == commission_cents


@pytest.mark.parametrize('amount', ['-1','NaN','Infinity','1.001','1e100','10000000000000000',True,None,'garbage'])
def test_invalid_amount_cannot_update_settlement(database, amount):
    ledger = Ledger(database[0])
    ledger.transition('legacy', 'match', 'op', evidence='match')
    ledger.transition('legacy', 'await_settlement', 'op')
    count = len(ledger.audit('legacy'))
    with pytest.raises(LedgerError):
        ledger.transition('legacy', 'settle', 'op', commission=amount, evidence='settled')
    assert ledger.get_order('legacy')['status'] == '待结算'
    assert len(ledger.audit('legacy')) == count


def test_transitions_require_actor_evidence_and_predecessor(database):
    ledger = Ledger(database[0])
    for action, kwargs in [('settle', {'commission':'5','evidence':'settled'}),('await_settlement',{}),('match',{}),('match',{'evidence':'  '})]:
        with pytest.raises(LedgerError):
            ledger.transition('legacy', action, 'op', **kwargs)
    with pytest.raises(LedgerError):
        ledger.transition('legacy', 'match', ' ', evidence='proof')
    assert ledger.audit('legacy') == []
    ledger.transition('legacy', 'match', 'op', evidence='proof')
    with pytest.raises(LedgerError):
        ledger.transition('legacy', 'settle', 'op', commission='9', evidence='settled')
    with pytest.raises(LedgerError):
        ledger.transition('missing', 'match', 'op', evidence='proof')


def test_payment_requires_exact_rebate_and_only_records_once(database):
    ledger = Ledger(database[0])
    row = ready(ledger)
    with pytest.raises(LedgerError):
        ledger.transition(row['id'], 'record_payment', 'op', amount='2.79', evidence='receipt')
    paid = ledger.transition(row['id'], 'record_payment', 'op', amount='2.80', evidence='receipt')
    assert paid['status'] == '已返款' and paid['paid_cents'] == 280
    with pytest.raises(LedgerError):
        ledger.transition(row['id'], 'record_payment', 'op', amount='2.80', evidence='duplicate')
    audit = ledger.audit(row['id'])
    assert [entry['action'] for entry in audit] == ['match','await_settlement','settle','record_payment']
    assert audit[-1]['actor'] == 'op' and audit[-1]['evidence'] == 'receipt'
    assert audit[-1]['before']['paid_cents'] == 0
    assert audit[-1]['after']['paid_cents'] == 280


def test_conflicting_payments_are_serialized(database):
    ledger = Ledger(database[0])
    row = ready(ledger)
    def pay(_):
        try:
            return ledger.transition(row['id'], 'record_payment', 'op', amount='2.80', evidence='receipt')['status']
        except LedgerError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(pay, range(2))) == ['rejected','已返款']
    assert len([r for r in ledger.audit(row['id']) if r['action']=='record_payment']) == 1


def test_audit_insert_failure_rolls_back_order_state(database):
    db, _ = database
    ledger = Ledger(db)
    with db() as c:
        c.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON ledger_audit BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.transition('legacy','match','op',evidence='proof')
    assert ledger.get_order('legacy')['status'] == '待人工核账'
    assert ledger.audit('legacy') == []


def test_refund_before_payment_cancels_eligibility(database):
    ledger = Ledger(database[0])
    row = ready(ledger)
    refunded = ledger.transition(row['id'], 'refund', 'op', evidence='full refund report')
    assert refunded['status'] == '已退款' and refunded['rebate_cents'] == 0
    assert refunded['actual_commission_cents'] == 1399
    with pytest.raises(LedgerError):
        ledger.transition(row['id'],'record_payment','op',amount='2.80',evidence='receipt')


def test_refund_after_payment_requires_manual_resolution_preserving_history(database):
    ledger = Ledger(database[0])
    row = ready(ledger)
    ledger.transition(row['id'],'record_payment','op',amount='2.80',evidence='receipt')
    pending = ledger.transition(row['id'],'refund','op',evidence='full refund report')
    assert pending['status'] == '退款待处理' and pending['paid_cents'] == 280
    for kwargs in [{'recovered':'2.81','note':'over limit'}, {'recovered':'-1','note':'negative'}, {'recovered':'0','note':''}]:
        with pytest.raises(LedgerError):
            ledger.transition(row['id'],'resolve_refund','op',evidence='manual record',**kwargs)
    done = ledger.transition(row['id'],'resolve_refund','op',recovered='1.00',evidence='manual receipt',note='remaining amount waived')
    assert done['status'] == '已退款' and done['paid_cents'] == 280
    assert done['recovered_cents'] == 100
    assert ledger.audit(row['id'])[-1]['note'] == 'remaining amount waived'


def test_invalidate_cannot_erase_payment(database):
    ledger = Ledger(database[0])
    assert ledger.transition('legacy','invalidate','op',evidence='not attributed')['status'] == '无效'
    row = ready(ledger)
    ledger.transition(row['id'],'record_payment','op',amount='2.80',evidence='receipt')
    with pytest.raises(LedgerError):
        ledger.transition(row['id'],'invalidate','op',evidence='erase')


def test_cli_requires_actor_and_records_admin_transition(database):
    db, path = database
    Ledger(db)
    script = Path(__file__).resolve().parents[1] / 'scripts/manage_orders.py'
    base = [sys.executable,str(script),'--database',str(path)]
    result = subprocess.run(base+['match','legacy','--evidence','report'],capture_output=True,text=True)
    assert result.returncode != 0
    result = subprocess.run(base+['--actor','local-admin','match','legacy','--evidence','report'],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == '已匹配'
    assert Ledger(db).audit('legacy')[0]['actor'] == 'local-admin'


def test_platform_aliases_find_legacy_and_prevent_duplicate_claim(database):
    ledger = Ledger(database[0])
    existing = ledger.register('alice','淘宝','123456')
    assert existing['id'] == 'legacy' and existing['created'] is False
    first = ledger.register('bob','京东','777777')
    assert ledger.register('bob','jd','777777')['id'] == first['id']
    with pytest.raises(OrderConflict):
        ledger.register('alice','jd','777777')
    assert ledger.register('alice','淘宝','666666',promotion_id='p')['title'] == 'original title'
