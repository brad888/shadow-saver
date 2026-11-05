#!/usr/bin/env python3
"""Read-only diagnostics, consistent backups and explicit conservative recovery."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen


# Explicit fields prevent accidental disclosure of Docker's environment array.
_CONTAINER_FORMAT = '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},"status":{{json .State.Status}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}'
_IMAGE_FORMAT = '{"id":{{json .Id}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}'


def run_command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        # Shell stderr can contain private paths, configuration and URLs.
        raise RuntimeError('Command failed; inspect the service locally using safe fields') from None
    return result.stdout.strip()


def inspect_container(container):
    return json.loads(run_command(['docker', 'inspect', '--format', _CONTAINER_FORMAT, container]))


def probe(base_url):
    """Return readiness only, never a response body or the URL supplied by a caller."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTP(S) base URL without credentials, query or fragment')
    try:
        with urlopen(base_url.rstrip('/')+'/readyz', timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def collect_status(container, local_url, public_url=None, tunnel=None):
    result = {'local_ready': probe(local_url), 'public_ready': probe(public_url) if public_url else None,
              'tunnel_running': None, 'container': None}
    try:
        result['container'] = inspect_container(container)
    except (RuntimeError, ValueError):
        result['container_error'] = 'container_unavailable'
    if tunnel:
        try:
            result['tunnel_running'] = bool(inspect_container(tunnel).get('running'))
        except (RuntimeError, ValueError):
            result['tunnel_running'] = False
    return result


def diagnose(status):
    issues = []
    if not status['local_ready']:
        issues.append('local_service_unready')
    if status['public_ready'] is False:
        issues.append('public_endpoint_unreachable')
        if status.get('tunnel_running') is False:
            issues.append('tunnel_missing')
    if status['public_ready'] is None:
        issues.append('public_endpoint_not_checked')
    return issues or ['healthy']


def backup_database(source, destination):
    """SQLite online backup includes committed WAL pages; source is opened read only."""
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file():
        raise FileNotFoundError('Source database does not exist')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=10)) as live:
            with closing(sqlite3.connect(destination)) as backup:
                live.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise RuntimeError('Backup integrity check failed')
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def recover(container, config_path, repository):
    """Restart the same container only after immutable image and main ancestry checks.

    Docker retains its original configuration and mounts. Never recreate the
    container, overwrite a database, or launch a temporary tunnel here.
    """
    config_path = Path(config_path)
    if not config_path.is_file() or config_path.stat().st_mode & 0o077:
        raise RuntimeError('runtime.env must exist with private permissions (600 or 400)')
    current = inspect_container(container)
    image, revision = current.get('image', ''), current.get('revision', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image) or not re.fullmatch(r'[0-9a-f]{40}', revision or ''):
        raise RuntimeError('Existing container has no verifiable immutable image/revision')
    metadata = json.loads(run_command(['docker', 'image', 'inspect', '--format', _IMAGE_FORMAT, image]))
    if metadata.get('id') != image or metadata.get('revision') != revision:
        raise RuntimeError('Container/image source metadata mismatch')
    # Verify the tracking ref against the remote without mutating the checkout.
    remote = run_command(['git', '-C', str(repository), 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'])
    local = run_command(['git', '-C', str(repository), 'rev-parse', 'origin/main']).strip()
    remote_sha = remote.split()[0] if remote.split() else ''
    if not re.fullmatch(r'[0-9a-f]{40}', remote_sha) or remote_sha != local:
        raise RuntimeError('Refresh origin/main before recovery; remote provenance is unverified')
    run_command(['git', '-C', str(repository), 'merge-base', '--is-ancestor', revision, 'origin/main'])
    run_command(['docker', 'restart', current['id']])
    return {'action': 'restarted_existing_container', 'source_commit': revision, 'image': image}


_EXPLANATIONS = {
    'healthy': 'Local and configured public readiness probes passed.',
    'local_service_unready': 'Check Docker state, database mount permissions and free disk space.',
    'public_endpoint_unreachable': 'Local readiness can pass while DNS, TLS, routing or a tunnel fails; verify the existing domain and callback configuration.',
    'tunnel_missing': 'The selected tunnel container is stopped or missing. Restore the existing named tunnel; a new quick tunnel changes the domain.',
    'public_endpoint_not_checked': 'Pass --public-url to verify the configured callback origin.',
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'doctor', 'backup', 'recover'])
    parser.add_argument('--container', default='shadow-saver')
    parser.add_argument('--local-url', default='http://127.0.0.1:8000')
    parser.add_argument('--public-url')
    parser.add_argument('--tunnel-container')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config', type=Path, default=Path('runtime.env'))
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            if not args.database or not args.output:
                parser.error('backup requires --database and --output')
            backup_database(args.database, args.output)
            result = {'backup': 'complete', 'permissions': '600', 'integrity': 'ok'}
        elif args.command == 'recover':
            result = recover(args.container, args.config, args.repository)
        else:
            result = collect_status(args.container, args.local_url, args.public_url, args.tunnel_container)
            if args.command == 'doctor':
                result['diagnosis'] = [{'code': code, 'explanation': _EXPLANATIONS[code]} for code in diagnose(result)]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError, sqlite3.Error):
        print(json.dumps({'error': 'Operation failed; check container provenance, private config permissions and database paths locally.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
