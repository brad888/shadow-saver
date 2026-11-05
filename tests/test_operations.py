import json
import logging
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_app(tmp_path, token='ops-secret', broken=False):
    from shadow_saver.operations import install_operations
    @contextmanager
    def db():
        if broken:
            raise sqlite3.OperationalError('private database path secret')
        with sqlite3.connect(tmp_path/'data.db') as conn:
            yield conn
    app = FastAPI()
    @app.get('/open/{token}')
    def entry(token):
        return {'ok': True}
    @app.get('/boom/{token}')
    def boom(token):
        raise RuntimeError('private message secret-key owner-openid')
    install_operations(app, db, {'OPS_STATUS_TOKEN': token, 'SOURCE_COMMIT': 'a'*40})
    return app


def test_liveness_readiness_and_admin_auth(tmp_path):
    with TestClient(make_app(tmp_path)) as c:
        assert c.get('/healthz').json() == {'status': 'alive'}
        assert c.get('/readyz').json() == {'status': 'ready'}
        assert c.get('/ops/status').status_code == 403
        assert c.get('/ops/status?token=ops-secret').status_code == 403
        assert c.get('/ops/status', headers={'Authorization': 'Bearer wrong'}).status_code == 403
        body = c.get('/ops/status', headers={'Authorization': 'Bearer ops-secret'}).json()
        assert body['uptime_seconds'] >= 0
        assert body['source_commit'] == 'a'*40
    with TestClient(make_app(tmp_path, token='')) as c:
        assert c.get('/ops/status').status_code == 404
    with TestClient(make_app(tmp_path, broken=True)) as c:
        assert c.get('/healthz').status_code == 200
        r = c.get('/readyz')
        assert r.status_code == 503
        assert r.json() == {'status': 'unavailable'}


def test_logs_and_metrics_never_include_private_data(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='shadow_saver.operations')
    with TestClient(make_app(tmp_path)) as c:
        c.get('/open/entrance-secret?signature=query-secret', headers={'Authorization': 'Bearer ops-secret'})
        r = c.get('/boom/message-secret')
        assert r.status_code == 500
        assert r.json() == {'detail': 'Internal server error'}
        c.get('/unknown-secret?openid=private')
        stats = c.get('/ops/status', headers={'Authorization': 'Bearer ops-secret'}).json()
    log = '\n'.join(rec.message for rec in caplog.records if rec.name == 'shadow_saver.operations')
    combined = log + json.dumps(stats)
    for secret in ['entrance-secret', 'query-secret', 'message-secret', 'unknown-secret', 'ops-secret', 'secret-key', 'owner-openid']:
        assert secret not in combined
    records = [json.loads(line) for line in log.splitlines()]
    assert any(r['route'] == '/open/{token}' and r['status'] == 200 for r in records)
    assert any(r['route'] == '/boom/{token}' and r['error_category'] == 'internal' for r in records)
    metric = next(row for row in stats['routes'] if row['route'] == '/boom/{token}')
    assert metric['requests'] == metric['errors'] == 1
    assert metric['elapsed_ms_total'] >= 0
    assert stats['database'] == 'ready'


def test_backup_online_includes_wal_and_is_private(tmp_path):
    from scripts.service_ops import backup_database
    path = tmp_path/'live.db'
    with sqlite3.connect(path) as source:
        source.execute('PRAGMA journal_mode=WAL')
        source.execute('CREATE TABLE claims(value TEXT)')
        source.execute('INSERT INTO claims VALUES (?)', ('preserved',))
        source.commit()
        target = backup_database(path, tmp_path/'backup.db')
        with sqlite3.connect(target) as dest:
            assert dest.execute('SELECT value FROM claims').fetchone() == ('preserved',)
            assert dest.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backup_database(path, target)
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path/'missing.db', tmp_path/'new.db')
    assert not (tmp_path/'missing.db').exists()


def test_doctor_distinguishes_public_failure_and_missing_tunnel():
    from scripts.service_ops import diagnose
    assert 'public_endpoint_unreachable' in diagnose({'local_ready': True, 'public_ready': False, 'tunnel_running': True})
    assert 'tunnel_missing' in diagnose({'local_ready': True, 'public_ready': False, 'tunnel_running': False})
    assert diagnose({'local_ready': True, 'public_ready': True, 'tunnel_running': False}) == ['healthy']
    assert 'local_service_unready' in diagnose({'local_ready': False, 'public_ready': False, 'tunnel_running': True})


def test_inspection_only_requests_safe_fields(monkeypatch):
    from scripts import service_ops as ops
    calls = []
    def run(args):
        calls.append(args)
        return json.dumps({'id': 'container', 'image': 'sha256:'+'a'*64, 'running': True, 'status': 'running', 'revision': 'b'*40})
    monkeypatch.setattr(ops, 'run_command', run)
    assert ops.inspect_container('shadow-saver')['running']
    assert '.Config.Env' not in str(calls)
    assert '--format' in calls[0]
    assert '.Image' in str(calls)


def test_recover_only_restarts_existing_verified_main_image(tmp_path, monkeypatch):
    from scripts import service_ops as ops
    env = tmp_path/'runtime.env'
    env.write_text('SECRET=must-never-be-read')
    env.chmod(0o600)
    image = 'sha256:'+'a'*64
    revision = 'b'*40
    calls = []
    monkeypatch.setattr(ops, 'inspect_container', lambda name: {'id': 'container-id', 'image': image, 'revision': revision})
    def run(args):
        calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps({'id': image, 'revision': revision})
        if 'ls-remote' in args:
            return revision+'\trefs/heads/main'
        if 'rev-parse' in args:
            return revision
        return ''
    monkeypatch.setattr(ops, 'run_command', run)
    ops.recover('shadow-saver', env, tmp_path)
    assert calls[-1] == ['docker', 'restart', 'container-id']
    assert any(call[:3] == ['git', '-C', str(tmp_path)] and 'merge-base' in call for call in calls)
    assert all('run' not in call and 'cloudflared' not in call for call in calls)
    calls.clear()
    monkeypatch.setattr(ops, 'inspect_container', lambda name: {'id': 'container-id', 'image': image, 'revision': ''})
    with pytest.raises(RuntimeError):
        ops.recover('shadow-saver', env, tmp_path)
    assert not calls


def test_recover_refuses_revision_outside_remote_main(tmp_path, monkeypatch):
    from scripts import service_ops as ops
    env = tmp_path/'runtime.env'
    env.touch(mode=0o600)
    image = 'sha256:'+'a'*64
    revision = 'b'*40
    monkeypatch.setattr(ops, 'inspect_container', lambda name: {'id': 'cid', 'image': image, 'revision': revision})
    calls = []
    def run(args):
        calls.append(args)
        if args[0] == 'git':
            raise RuntimeError('not in main')
        return json.dumps({'id': image, 'revision': revision})
    monkeypatch.setattr(ops, 'run_command', run)
    with pytest.raises(RuntimeError):
        ops.recover('shadow-saver', env, tmp_path)
    assert not any(c[:2] == ['docker', 'restart'] for c in calls)


def test_network_failure_is_safe_and_status_is_read_only(monkeypatch):
    from scripts import service_ops as ops
    calls = []
    def unavailable(url, timeout):
        calls.append(url)
        raise OSError('private network failure token=secret')
    monkeypatch.setattr(ops, 'urlopen', unavailable)
    monkeypatch.setattr(ops, 'run_command', lambda args: (_ for _ in ()).throw(RuntimeError('unavailable')))
    result = ops.collect_status('app', 'http://127.0.0.1:8000', 'https://existing.example.com', 'existing-tunnel')
    assert result['local_ready'] is result['public_ready'] is False
    assert result['tunnel_running'] is False
    assert 'secret' not in json.dumps(result)
    assert calls == ['http://127.0.0.1:8000/readyz', 'https://existing.example.com/readyz']
    with pytest.raises(ValueError):
        ops.probe('https://host.example?token=private')


def test_recover_refuses_stale_origin_main(tmp_path, monkeypatch):
    from scripts import service_ops as ops
    env = tmp_path/'runtime.env'
    env.touch(mode=0o600)
    image = 'sha256:'+'a'*64
    revision = 'b'*40
    calls = []
    monkeypatch.setattr(ops, 'inspect_container', lambda name: {'id': 'cid', 'image': image, 'revision': revision})
    def run(args):
        calls.append(args)
        if 'ls-remote' in args:
            return 'c'*40+'\trefs/heads/main'
        if 'rev-parse' in args:
            return 'd'*40
        return json.dumps({'id': image, 'revision': revision})
    monkeypatch.setattr(ops, 'run_command', run)
    with pytest.raises(RuntimeError, match='origin/main'):
        ops.recover('shadow-saver', env, tmp_path)
    assert not any(c[:2] == ['docker', 'restart'] for c in calls)


def test_recover_rejects_world_readable_config_before_any_shell(tmp_path, monkeypatch):
    from scripts import service_ops as ops
    env = tmp_path/'runtime.env'
    env.touch()
    env.chmod(0o644)
    monkeypatch.setattr(ops, 'run_command', lambda args: pytest.fail('must refuse before shell'))
    with pytest.raises(RuntimeError, match='private permissions'):
        ops.recover('app', env, tmp_path)


def test_operations_logging_enabled_without_root_configuration(tmp_path, monkeypatch):
    from shadow_saver import operations
    monkeypatch.setattr(operations._LOG, 'level', logging.WARNING)
    make_app(tmp_path)
    assert operations._LOG.isEnabledFor(logging.INFO)
