#!/usr/bin/env python3
"""Existing-host deployment, or prepare a new host without starting services.

Uses only the standard library. Run from a trusted checkout as root.
Configuration, secrets, users, instances and recordings are never imported from Git.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parent
DEST = Path('/opt/netlab-admin')
UNIT = Path('/etc/systemd/system/labd.service')
FILES = (
    'labd.py', 'netlab-tutor.py', 'tutor_chat.py', 'netlab-user', 'netlab-record', 'netlab-backup',
    'assets/tutor.js', 'assets/tutor.css', 'community_chat.py', 'community_auth.py', 'community_ws.py',
    'requirements-community.txt', 'assets/community-transport.js', 'assets/community-presence.js',
    'assets/community.js', 'assets/community.css', 'notice_board.py', 'assets/notice.js', 'assets/notice.css', 'assets/theme.js', 'assets/terminal-theme.js',
    'tutor_chapters.py',
    'knowledge.py', 'guide_view.py', 'knowledge/corpus.json',
    'lab-session', 'lab-shell.sh', 'ttyd-lab', 'apply-shell.sh',
    'nginx-netlab.conf', 'nginx-open.conf', 'lxd-preseed-zfs.yaml', 'README.md',
    'OPERATIONS.md', 'THIRD_PARTY.md',
    'assets/player.js', 'assets/player.css', 'assets/replay.js', 'assets/replay.css',
    'assets/asciinema-player-LICENSE',
    'systemd/labd.service', 'systemd/netlab-netguard.service',
    'systemd/netlab-community.service',
    'systemd/lxd-docker-fix.service',
    'netguard.sh', 'guest-bootstrap.sh',
    'systemd/netlab-bootstrap-netguard.service', 'systemd/netlab-bootstrap-netguard.timer',
)
CLI = ('netlab-user', 'netlab-record', 'netlab-backup')
SHELL = ('netlab-user', 'lab-session', 'lab-shell.sh', 'ttyd-lab', 'apply-shell.sh',
         'netguard.sh', 'guest-bootstrap.sh')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def validate_source():
    for name in FILES:
        p = SOURCE / name
        if not p.is_file() or p.is_symlink():
            raise RuntimeError(f'Missing or symlinked source: {name}')
    for name in ('labd.py', 'netlab-tutor.py', 'tutor_chat.py', 'tutor_chapters.py', 'netlab-record', 'netlab-backup', 'bootstrap.py', 'deploy.py'):
        compile((SOURCE / name).read_text(), name, 'exec')
    compile((SOURCE / 'knowledge.py').read_text(), 'knowledge.py', 'exec')
    compile((SOURCE / 'community_chat.py').read_text(), 'community_chat.py', 'exec')
    for name in ('community_auth.py', 'community_ws.py'):
        compile((SOURCE / name).read_text(), name, 'exec')
    compile((SOURCE / 'notice_board.py').read_text(), 'notice_board.py', 'exec')
    compile((SOURCE / 'guide_view.py').read_text(), 'guide_view.py', 'exec')
    corpus = json.loads((SOURCE / 'knowledge/corpus.json').read_text())
    for document in corpus['documents']:
        if hashlib.sha256(document['text'].encode()).hexdigest() != document['sha256']:
            raise RuntimeError('Knowledge document checksum mismatch')
        original = SOURCE.parent / document['path']
        if original.is_file() and hashlib.sha256(original.read_bytes()).hexdigest() != document['sha256']:
            raise RuntimeError('Knowledge corpus is stale; run python3 server/build_knowledge.py')
    for name in SHELL:
        run('bash', '-n', str(SOURCE / name))
    for p in (SOURCE / 'config').glob('*.example.json'):
        json.loads(p.read_text())
    template = (SOURCE / 'nginx-netlab.conf').read_text()
    headers = re.findall(r'proxy_set_header X-Lab-Proxy-Auth ([^;]+);', template)
    if len(headers) != 4 or any(h != '"__NETLAB_PROXY_SECRET__"' for h in headers):
        raise RuntimeError('Nginx source must contain placeholders only')
    headers = re.findall(r'proxy_set_header X-Lab-Proxy-Auth ([^;]+);', (SOURCE / 'nginx-open.conf').read_text())
    if headers != ['"__NETLAB_PROXY_SECRET__"']:
        raise RuntimeError('Open Nginx source must contain placeholder only')


def preflight():
    if os.geteuid() != 0:
        raise RuntimeError('Run on the server with sudo')
    for name in ('lxc', 'nginx', 'systemctl', 'bash', 'htpasswd'):
        if not shutil.which(name):
            raise RuntimeError(f'Missing command: {name}')
    run('/usr/bin/python3', '-c', 'import jwt, cryptography')
    cfg = json.loads(Path('/etc/netlab/config.json').read_text())
    for filename in ('users.json', 'state.json'):
        if not isinstance(json.loads((Path('/var/lib/netlab') / filename).read_text()), dict):
            raise RuntimeError(f'Invalid {filename}')
    secret = Path('/etc/netlab/proxy.secret').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', secret):
        raise RuntimeError('Invalid proxy.secret (not printed)')
    if not DEST.is_dir() or not UNIT.exists():
        raise RuntimeError('Existing installation required; see --prepare and README')
    run('lxc', 'image', 'info', cfg.get('image', 'netlab'))
    run('lxc', 'network', 'show', 'lxdbr0')
    run('nginx', '-t')
    run('systemctl', 'is-active', 'labd', 'nginx')


def ensure_community_runtime():
    runtime = Path('/opt/netlab-community-venv')
    python = runtime / 'bin/python'
    try:
        run('/usr/bin/python3', '-m', 'venv', str(runtime))
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Community venv initialization failed; install python3-venv first. ' +
                           ((exc.stdout or '') + (exc.stderr or ''))[-800:]) from exc
    try:
        version = run(str(python), '-c', 'import websockets; print(websockets.__version__)')
    except subprocess.CalledProcessError:
        version = ''
    if version != '15.0.1':
        run(str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(SOURCE / 'requirements-community.txt'))


def install_code():
    for name in FILES:
        dest = DEST / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            raise RuntimeError(f'Refusing symlink destination: {dest}')
        shutil.copyfile(SOURCE / name, dest)
        dest.chmod(0o755 if name in (*SHELL, *CLI) else 0o644)
        os.chown(dest, 0, 0)
    for name in CLI:
        target = Path('/usr/local/bin') / name
        if target.is_symlink():
            raise RuntimeError(f'Refusing symlink CLI: {target}')
        shutil.copyfile(DEST / name, target)
        target.chmod(0o755)
        os.chown(target, 0, 0)
    shutil.copyfile(SOURCE / 'systemd/labd.service', UNIT)
    UNIT.chmod(0o644)
    community_unit = UNIT.with_name('netlab-community.service')
    shutil.copyfile(SOURCE / 'systemd/netlab-community.service', community_unit)
    community_unit.chmod(0o644)


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            # No auth header: the protected service must deny the request.
            with opener.open('http://127.0.0.1:9000/admin/', timeout=2) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        except OSError:
            time.sleep(1)
            continue
        if status != 403:
            raise RuntimeError(f'Unexpected unauthenticated admin status: {status}')
        try:
            with opener.open('http://127.0.0.1:9001/health', timeout=2) as r:
                if r.status != 200:
                    raise RuntimeError('Community service is unhealthy')
        except OSError:
            time.sleep(1)
            continue
        run('systemctl', 'is-active', 'labd', 'nginx', 'netlab-community')
        run('nginx', '-t')
        return
    raise RuntimeError('labd did not become ready')


def deploy():
    preflight()
    ensure_community_runtime()
    backup_root = Path('/var/backups/netlab')
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='release-', dir=backup_root))
    community_unit = UNIT.with_name('netlab-community.service')
    had_community = community_unit.exists()
    if had_community:
        shutil.copy2(community_unit, backup / 'netlab-community.service')
        run('systemctl', 'stop', 'netlab-community')
    # Copy while stopped so deployment and rollback see a stable code directory.
    run('systemctl', 'stop', 'labd')
    saved = False
    try:
        shutil.copytree(DEST, backup / 'code')
        shutil.copy2(UNIT, backup / 'labd.service')
        for name in CLI:
            existing = Path('/usr/local/bin') / name
            if existing.exists():
                shutil.copy2(existing, backup / name)
        saved = True
        install_code()
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', 'labd')
        run('systemctl', 'enable', 'netlab-community')
        run('systemctl', 'restart', 'netlab-community')
        health()
        manifest = {name: hashlib.sha256((DEST / name).read_bytes()).hexdigest() for name in FILES}
        (DEST / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'Deployed; backup: {backup}')
        print('Existing /etc/netlab, live nginx site, network units and VMs preserved.')
    except BaseException:
        if saved:
            run('systemctl', 'stop', 'netlab-community')
            run('systemctl', 'stop', 'labd')
            shutil.copytree(backup / 'code', DEST, dirs_exist_ok=True)
            shutil.copy2(backup / 'labd.service', UNIT)
            if had_community:
                shutil.copy2(backup / 'netlab-community.service', community_unit)
            else:
                community_unit.unlink(missing_ok=True)
            for name in CLI:
                if (backup / name).exists():
                    shutil.copy2(backup / name, Path('/usr/local/bin') / name)
                else:
                    (Path('/usr/local/bin') / name).unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')
        run('systemctl', 'start', 'labd')
        if had_community:
            run('systemctl', 'start', 'netlab-community')
        print(f'Deployment failed; previous code restored where backed up: {backup}')
        raise


def prepare(auth_mode='open'):
    if os.geteuid() != 0:
        raise RuntimeError('Run on the server with sudo')
    targets = [DEST, Path('/etc/netlab'), Path('/var/lib/netlab'), UNIT,
               Path('/etc/nginx/sites-available/netlab'), Path('/etc/nginx/netlab.d')]
    if any(p.exists() or p.is_symlink() for p in targets):
        raise RuntimeError('--prepare is only for a new host; an installation already exists')
    ensure_community_runtime()
    import secrets
    import grp
    gid = grp.getgrnam('www-data').gr_gid
    Path('/etc/netlab/htpasswd').mkdir(parents=True, mode=0o750)
    os.chown('/etc/netlab/htpasswd', 0, gid)
    Path('/var/lib/netlab').mkdir(parents=True, mode=0o700)
    Path('/etc/nginx/netlab.d').mkdir(parents=True)
    shutil.copyfile(SOURCE / 'config/config.example.json', '/etc/netlab/config.json')
    Path('/etc/netlab/config.json').chmod(0o600)
    cfg = json.loads(Path('/etc/netlab/config.json').read_text())
    cfg['auth_mode'] = auth_mode
    Path('/etc/netlab/config.json').write_text(json.dumps(cfg, indent=2) + '\n')
    secret = secrets.token_urlsafe(48)
    Path('/etc/netlab/proxy.secret').write_text(secret + '\n')
    Path('/etc/netlab/proxy.secret').chmod(0o600)
    for name in ('users.json', 'state.json'):
        p = Path('/var/lib/netlab') / name
        p.write_text('{}\n' if name == 'users.json' else '{"users": {}, "pool": []}\n')
        p.chmod(0o600)
    for name in ('lan-users.conf', 'tunnel-users.conf'):
        Path('/etc/nginx/netlab.d', name).write_text('\n')
    install_code()
    template = 'nginx-open.conf' if auth_mode == 'open' else 'nginx-netlab.conf'
    rendered = (SOURCE / template).read_text().replace('__NETLAB_PROXY_SECRET__', secret)
    # Fresh install: keep the emergency Basic Auth listener on loopback, not LAN.
    rendered = rendered.replace('listen 8088 default_server;', 'listen 127.0.0.2:8088;')
    site = Path('/etc/nginx/sites-available/netlab')
    site.write_text(rendered)
    site.chmod(0o600)
    print('Prepared only. Configure LXD image, firewall and enable services per README.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='read-only existing-host preflight (default)')
    mode.add_argument('--apply', action='store_true', help='backup and update existing host')
    mode.add_argument('--prepare', action='store_true', help='prepare a new host; do not start services')
    mode.add_argument('--source-check', action='store_true', help='validate checkout without a server')
    args = parser.parse_args()
    # Serialize deployments, including their backup and rollback stages.
    deployment_lock = None
    if args.apply or args.prepare:
        import fcntl
        if os.geteuid() != 0:
            raise RuntimeError('Run on the server with sudo')
        deployment_lock = open('/run/lock/netlab-deploy.lock', 'a')
        fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    validate_source()
    if args.source_check:
        print('Source validation passed')
    elif args.prepare:
        prepare()
    elif args.apply:
        deploy()
    else:
        preflight()
        print('Preflight passed; --apply will briefly restart labd (not VMs, Docker or nginx)')


if __name__ == '__main__':
    main()
