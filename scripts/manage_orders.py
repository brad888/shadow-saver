"""Local administrator CLI for evidence-backed manual accounting; never transfers money."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shadow_saver.ledger import Ledger, LedgerError


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地人工核账。仅录入实际已结算佣金和凭据；预估返利不可作为结算依据。记款只记录已完成付款，不执行转账。')
    parser.add_argument('--database', required=True, help='现有 SQLite 数据库路径')
    parser.add_argument('--actor', required=True, help='实名或可追溯管理员标识')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('list', 'show', 'audit', 'match', 'await_settlement', 'settle', 'record_payment', 'refund', 'resolve_refund', 'invalidate'):
        command = sub.add_parser(action)
        if action == 'list':
            command.add_argument('owner')
        else:
            command.add_argument('claim_id')
        if action in ('match','settle','record_payment','refund','resolve_refund','invalidate'):
            command.add_argument('--evidence', required=True, help='匹配、实际结算、付款或退款凭据引用')
        if action == 'settle':
            command.add_argument('--commission', required=True, help='实际已结算佣金（元），至多两位小数，按20%%计算返利')
        if action == 'record_payment':
            command.add_argument('--amount', required=True, help='实际付款金额（元），必须与应返金额一致')
        if action == 'resolve_refund':
            command.add_argument('--recovered', required=True, help='人工实际追回金额（元），允许零')
            command.add_argument('--note', required=True, help='人工处理结果与未追回部分说明')
    args = parser.parse_args(argv)
    path = Path(args.database).expanduser().resolve()
    if not path.is_file():
        parser.error('database must already exist')
    @contextmanager
    def db():
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()
    try:
        if not args.actor.strip():
            raise LedgerError('actor is required')
        ledger = Ledger(db)
        if args.action == 'list':
            result = ledger.list_orders(args.owner)
        elif args.action == 'show':
            result = ledger.get_order(args.claim_id)
        elif args.action == 'audit':
            result = ledger.audit(args.claim_id)
        else:
            values = {key: value for key, value in vars(args).items() if key in {'evidence','commission','amount','recovered','note'}}
            result = ledger.transition(args.claim_id, args.action, args.actor, **values)
    except (LedgerError, sqlite3.Error) as exc:
        parser.exit(1, f'核账失败: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
