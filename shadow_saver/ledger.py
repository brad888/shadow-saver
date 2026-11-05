"""Local, evidence-backed ledger. Payment actions record receipts; they move no money."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import time
import uuid


class LedgerError(ValueError):
    """Invalid ledger input or state transition."""


class OrderConflict(LedgerError):
    """An order is already registered by another owner."""


_PLATFORMS = {'taobao': ('淘宝', 'taobao'), '淘宝': ('淘宝', 'taobao'),
              'jd': ('京东', 'jd'), '京东': ('京东', 'jd')}


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f'{label} is required')
    return value.strip()


def _cents(value):
    if isinstance(value, bool) or value is None:
        raise LedgerError('amount must be a nonnegative decimal with at most two decimal places')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal('999999999.99') or amount.as_tuple().exponent < -2:
            raise LedgerError('amount must be finite, nonnegative, at most 999999999.99 and have at most two decimal places')
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise LedgerError('invalid amount') from exc


class Ledger:
    def __init__(self, db):
        self.db = db
        # Preserve all legacy fields, estimates, owners and states. Estimates never
        # populate the actual settlement or payment columns.
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in c.execute('PRAGMA table_info(claims)')}
            additions = {
                'title': "TEXT NOT NULL DEFAULT ''",
                'actual_commission_cents': 'INTEGER',
                'rebate_cents': 'INTEGER NOT NULL DEFAULT 0',
                'paid_cents': 'INTEGER NOT NULL DEFAULT 0',
                'recovered_cents': 'INTEGER NOT NULL DEFAULT 0',
                'match_evidence': "TEXT NOT NULL DEFAULT ''",
                'settlement_evidence': "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    c.execute(f'ALTER TABLE claims ADD COLUMN {name} {definition}')
            c.execute('''CREATE TABLE IF NOT EXISTS ledger_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                evidence TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                created_at INTEGER NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS ledger_audit_claim ON ledger_audit(claim_id,id)')

    @staticmethod
    def _fetch(c, claim_id):
        row = c.execute('''SELECT c.*, CASE WHEN c.title != '' THEN c.title ELSE COALESCE(p.title,'') END AS display_title,
            COALESCE(p.rebate,'0.00') AS rebate
            FROM claims c LEFT JOIN promotions p ON p.id=c.promotion_id AND p.owner=c.owner
            WHERE c.id=?''', (claim_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['title'] = result.pop('display_title')
        return result

    def register(self, owner, platform, order_no, promotion_id=None, title=''):
        owner = _text(owner, 'owner')
        platform = _text(platform, 'platform')
        order_no = _text(order_no, 'order_no')
        if platform not in _PLATFORMS:
            raise LedgerError('unsupported platform')
        aliases = _PLATFORMS[platform]
        if not order_no.isascii() or not order_no.isdigit() or len(order_no) > 128:
            raise LedgerError('order_no must contain at most 128 ASCII digits')
        if not isinstance(title, str):
            raise LedgerError('title must be text')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT id,owner FROM claims WHERE platform IN (?,?) AND order_no=?', (*aliases, order_no)).fetchone()
            if existing:
                if existing['owner'] != owner:
                    raise OrderConflict('order already registered')
                return {**self._fetch(c, existing['id']), 'created': False}
            if promotion_id is not None:
                promo = c.execute('SELECT id,platform FROM promotions WHERE id=? AND owner=?', (str(promotion_id), owner)).fetchone()
                if promo is None or promo['platform'] not in aliases:
                    raise LedgerError('invalid promotion')
            claim_id = uuid.uuid4().hex
            c.execute('''INSERT INTO claims(id,owner,promotion_id,platform,order_no,status,created_at,title)
                VALUES(?,?,?,?,?,?,?,?)''', (claim_id, owner, promotion_id, platform, order_no, '待人工核账', int(time.time()), title.strip()))
            return {**self._fetch(c, claim_id), 'created': True}

    def get_order(self, claim_id):
        with self.db() as c:
            return self._fetch(c, claim_id)

    def list_orders(self, owner):
        with self.db() as c:
            rows = c.execute('SELECT id FROM claims WHERE owner=? ORDER BY created_at DESC,rowid DESC', (owner,)).fetchall()
            return [self._fetch(c, row['id']) for row in rows]

    def transition(self, claim_id, action, actor, **kwargs):
        actor = _text(actor, 'actor')
        actions = {'match', 'await_settlement', 'settle', 'record_payment', 'refund', 'resolve_refund', 'invalidate'}
        if action not in actions:
            raise LedgerError('unknown action')
        evidence = '' if action == 'await_settlement' else _text(kwargs.get('evidence'), 'evidence')
        note = kwargs.get('note', '')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                before = self._fetch(c, claim_id)
                if before is None:
                    raise LedgerError('order not found')
                status = before['status']
                allowed = {
                    'match': {'待人工核账'},
                    'await_settlement': {'已匹配'},
                    'settle': {'待结算'},
                    'record_payment': {'可返利'},
                    'refund': {'待人工核账', '已匹配', '待结算', '可返利', '无返利', '已返款'},
                    'resolve_refund': {'退款待处理'},
                    'invalidate': {'待人工核账', '已匹配', '待结算', '可返利', '无返利'},
                }
                if status not in allowed[action]:
                    raise LedgerError('action is not allowed in current state')
                updates = {}
                if action == 'match':
                    updates = {'status': '已匹配', 'match_evidence': evidence}
                elif action == 'await_settlement':
                    if not before['match_evidence']:
                        raise LedgerError('matching evidence is required')
                    updates = {'status': '待结算'}
                elif action == 'settle':
                    if not before['match_evidence']:
                        raise LedgerError('matching evidence is required')
                    commission = _cents(kwargs.get('commission'))
                    rebate = int((Decimal(commission) * Decimal('0.20')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                    updates = {'status': '可返利' if rebate else '无返利', 'actual_commission_cents': commission,
                               'rebate_cents': rebate, 'settlement_evidence': evidence}
                elif action == 'record_payment':
                    amount = _cents(kwargs.get('amount'))
                    if not before['match_evidence'] or not before['settlement_evidence'] or before['actual_commission_cents'] is None:
                        raise LedgerError('matching and settlement evidence are required')
                    if before['paid_cents'] or amount <= 0 or amount != before['rebate_cents']:
                        raise LedgerError('payment must equal unpaid rebate exactly')
                    updates = {'status': '已返款', 'paid_cents': amount}
                elif action == 'refund':
                    updates = {'status': '退款待处理' if before['paid_cents'] else '已退款', 'rebate_cents': 0}
                elif action == 'resolve_refund':
                    recovered = _cents(kwargs.get('recovered'))
                    note = _text(note, 'note')
                    if recovered > before['paid_cents']:
                        raise LedgerError('recovered amount exceeds historical payment')
                    updates = {'status': '已退款', 'recovered_cents': recovered}
                elif action == 'invalidate':
                    if before['paid_cents']:
                        raise LedgerError('paid orders require refund resolution')
                    updates = {'status': '无效', 'rebate_cents': 0}
                c.execute('UPDATE claims SET ' + ','.join(f'{key}=?' for key in updates) + ' WHERE id=?', (*updates.values(), claim_id))
                after = self._fetch(c, claim_id)
                c.execute('''INSERT INTO ledger_audit(claim_id,action,actor,evidence,note,before_json,after_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (claim_id, action, actor, evidence, str(note), json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), int(time.time())))
                return after
            except Exception:
                c.rollback()
                raise

    def audit(self, claim_id):
        with self.db() as c:
            entries = [dict(row) for row in c.execute('SELECT * FROM ledger_audit WHERE claim_id=? ORDER BY id', (claim_id,))]
        for entry in entries:
            entry['before'] = json.loads(entry.pop('before_json'))
            entry['after'] = json.loads(entry.pop('after_json'))
        return entries
