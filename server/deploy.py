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
def _tree(*patterns):
    found = []
    for pattern in patterns:
        found.extend(sorted(str(p.relative_to(SOURCE)) for p in SOURCE.glob(pattern)
                            if p.is_file() and '__pycache__' not in p.parts))
    return tuple(found)


# 배포 대상. 디렉터리는 glob 으로 모아 모듈·에셋을 추가할 때 목록을 손으로 고치지 않게 한다.
# 소스 트리에 있어도 배포하지 않는 것: tests/, config/ 예제, 이 두 설치 스크립트.
FILES = (
    'labd.py', 'netlab_tutor.py', 'packs.py', 'runtimes.py', 'tutor_chat.py', 'knowledge.py', 'guide_view.py',
    'community_chat.py', 'community_auth.py', 'community_ws.py', 'notice_board.py', 'tutor_models.py',
    'requirements-community.txt',
    'netlab-user', 'netlab-record', 'netlab-backup',
    'lab-session', 'lab-shell.sh', 'ttyd-lab', 'apply-shell.sh', 'netguard.sh', 'guest-bootstrap.sh',
    'nginx-netlab.conf', 'nginx-open.conf', 'lxd-preseed-zfs.yaml',
    'README.md', 'OPERATIONS.md', 'THIRD_PARTY.md',
    'knowledge/corpus.json', 'knowledge/packs.json',
    'packctl.py', 'netlab-runner', 'knowledge/v2/catalog.json',
) + _tree('netlab/*.py', 'templates/*.html', 'assets/*', 'systemd/*', 'pack_format/*.py', 'runner/*.py')

CLI = ('netlab-user', 'netlab-record', 'netlab-backup')
# 이름이 바뀌어 더는 배포하지 않는 파일. 설치 디렉터리에 남아 있으면 지운다.
OBSOLETE = ('netlab-tutor.py', 'tutor_chapters.py')
SHELL = ('netlab-user', 'lab-session', 'lab-shell.sh', 'ttyd-lab', 'apply-shell.sh',
         'netguard.sh', 'guest-bootstrap.sh')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def source_revision():
    """배포한 소스의 git 커밋·브랜치·수정 여부. 어느 커밋이 올라갔는지 추적용.

    git 저장소에서 실행하면 git 에서 읽고, tar 로 옮긴 사본이면 보내는 쪽이 만든
    .release-source.json 을 쓴다. 둘 다 없으면 None.
    """
    try:
        git = ['git', '-C', str(SOURCE)]
        head = subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run([*git, 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run([*git, 'status', '--porcelain', '--', '.'], capture_output=True, text=True).stdout.strip())
        info = {'commit': head, 'branch': branch, 'dirty': dirty}
    except (OSError, subprocess.CalledProcessError):
        try:
            info = json.loads((SOURCE / '.release-source.json').read_text())
            info = {k: info.get(k) for k in ('commit', 'branch', 'dirty', 'tag')}
        except (OSError, ValueError):
            return None
    info['deployed'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    return info


def write_release_source():
    """tar 전송 전에 호출: git 정보를 .release-source.json 으로 남긴다 (--stamp)."""
    info = source_revision()
    if not info:
        raise RuntimeError('Not a git checkout; nothing to stamp')
    tag = subprocess.run(['git', '-C', str(SOURCE), 'describe', '--tags', '--exact-match'], capture_output=True, text=True).stdout.strip()
    info = {'commit': info['commit'], 'branch': info['branch'], 'dirty': info['dirty'], 'tag': tag or None}
    # 대상 서버에는 git 이 없으므로, 운영 커밋이 이 소스에 포함되는지 볼 수 있게 조상 목록을 같이 보낸다.
    info['ancestors'] = run('git', '-C', str(SOURCE), 'rev-list', 'HEAD').split()
    (SOURCE / '.release-source.json').write_text(json.dumps(info, indent=2) + '\n')
    print(f"Stamped {info['branch']}@{info['commit'][:12]}" + (f" tag {tag}" if tag else '') + (' (uncommitted changes)' if info['dirty'] else ''))


def validate_source():
    for name in FILES:
        p = SOURCE / name
        if not p.is_file() or p.is_symlink():
            raise RuntimeError(f'Missing or symlinked source: {name}')
    for name in [f for f in FILES if f.endswith('.py')] + ['bootstrap.py', 'deploy.py', 'netlab-record', 'netlab-backup',
                                                         'netlab-runner']:
        compile((SOURCE / name).read_text(), name, 'exec')
    catalog = json.loads((SOURCE / 'knowledge/v2/catalog.json').read_text())
    for pack_id, pack in catalog['packs'].items():
        for item in pack['bundle']['files']:
            original = SOURCE.parent / item['path']
            if original.is_file() and hashlib.sha256(original.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError('Learning pack v2 catalog is stale; run python3 server/build_knowledge.py')
    for path, doc in catalog.get('documents', {}).items():
        original = SOURCE.parent / path
        if original.is_file() and hashlib.sha256(original.read_bytes()).hexdigest() != doc['sha256']:
            raise RuntimeError('Learning pack v2 catalog is stale; run python3 server/build_knowledge.py')
    if any(lesson['pack'] not in catalog['packs'] for lesson in catalog['lessons'].values()):
        raise RuntimeError('Learning pack v2 catalog is inconsistent; run python3 server/build_knowledge.py')
    corpus = json.loads((SOURCE / 'knowledge/corpus.json').read_text())
    for document in corpus['documents']:
        if hashlib.sha256(document['text'].encode()).hexdigest() != document['sha256']:
            raise RuntimeError('Knowledge document checksum mismatch')
        original = SOURCE.parent / document['path']
        if original.is_file() and hashlib.sha256(original.read_bytes()).hexdigest() != document['sha256']:
            raise RuntimeError('Knowledge corpus is stale; run python3 server/build_knowledge.py')
    compiled = json.loads((SOURCE / 'knowledge/packs.json').read_text())
    pack_ids = {pack['id'] for pack in compiled['packs']}
    if not compiled['lessons'] or any(lesson['pack'] not in pack_ids for lesson in compiled['lessons']):
        raise RuntimeError('Compiled packs are inconsistent; run python3 server/build_knowledge.py')
    if any(document['pack'] not in pack_ids for document in corpus['documents']):
        raise RuntimeError('Knowledge corpus and packs disagree; run python3 server/build_knowledge.py')
    for name in SHELL:
        run('bash', '-n', str(SOURCE / name))
    for p in (SOURCE / 'config').glob('*.example.json'):
        json.loads(p.read_text())
    template = (SOURCE / 'nginx-netlab.conf').read_text()
    for conf in ('nginx-netlab.conf', 'nginx-open.conf'):
        headers = re.findall(r'proxy_set_header X-Lab-Proxy-Auth ([^;]+);', (SOURCE / conf).read_text())
        if not headers or any(h != '"__NETLAB_PROXY_SECRET__"' for h in headers):
            raise RuntimeError('Nginx source must contain placeholders only')


def source_contains(commit):
    """이 소스가 commit 을 조상으로 포함하는가. git 사본이면 git 으로, tar 사본이면 --stamp 의 조상 목록으로 본다.
    판단할 수 없으면 None."""
    git = ['git', '-C', str(SOURCE)]
    try:
        if subprocess.run([*git, 'rev-parse', '--git-dir'], capture_output=True).returncode == 0:
            if subprocess.run([*git, 'cat-file', '-e', f'{commit}^{{commit}}'], capture_output=True).returncode:
                return False
            return subprocess.run([*git, 'merge-base', '--is-ancestor', commit, 'HEAD'],
                                  capture_output=True).returncode == 0
    except OSError:
        pass
    try:
        ancestors = json.loads((SOURCE / '.release-source.json').read_text()).get('ancestors')
    except (OSError, ValueError):
        return None
    return commit in ancestors if isinstance(ancestors, list) else None


def check_running_source(running, source):
    """운영 중인 코드가 git 밖 변경이거나 새 소스에 없는 커밋이면, 덮어써서 그 변경을 잃지 않도록 멈춘다.

    running 은 운영 release-manifest 의 _source, source 는 이번 소스의 source_revision().
    """
    if source is None or source.get('dirty'):
        raise RuntimeError('Source has uncommitted changes or no git record; deploy a committed, stamped checkout '
                           '(python3 server/deploy.py --stamp)')
    if not running:
        print('Note: running release has no source record; skipping overwrite check')
        return
    where = f"{running.get('branch')}@{str(running.get('commit'))[:12]} (deployed {running.get('deployed')})"
    if running.get('dirty'):
        raise RuntimeError(f'Running code was deployed from uncommitted changes: {where}. Commit those changes '
                           'into this source first, or pass --allow-source-overwrite after checking them')
    contained = source_contains(running.get('commit') or '')
    if contained is None:
        raise RuntimeError('Cannot tell whether the running commit is in this source; re-stamp the checkout '
                           '(python3 server/deploy.py --stamp)')
    if not contained:
        raise RuntimeError(f'Running commit {where} is not in this source; deploying would drop its changes. '
                           'Merge it first, or pass --allow-source-overwrite (e.g. for a rollback)')


def running_source():
    try:
        return json.loads((DEST / 'release-manifest.json').read_text()).get('_source')
    except (OSError, ValueError):
        return None


SITE = Path('/etc/nginx/sites-available/netlab')


def check_site(text):
    """The live site must hand labd the static location owner, never the raw request URI."""
    # The legacy X-Original-URI line may linger during a rolling update; labd ignores it.
    if '/_access' in text and 'X-Lab-Auth-Scheme target' not in text:
        raise RuntimeError('Live nginx site predates target-based auth_request; '
                           'update /etc/nginx/sites-available/netlab first (README: 기존 운영 서버 업데이트)')


def preflight(allow_source_overwrite=False):
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
    if SITE.is_file():
        check_site(SITE.read_text())
    if allow_source_overwrite:
        print('Warning: --allow-source-overwrite; running code may be replaced by an unrelated source')
    else:
        check_running_source(running_source(), source_revision())


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
    for name in OBSOLETE:
        stale = DEST / name
        if stale.is_file() and not stale.is_symlink():
            stale.unlink()
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


USER_SNIPPETS = re.compile(r'include\s+(\S+-users\.conf)\s*;')
USER_PANEL = re.compile(r'location /u/([^/\s]+)/ctl/ \{')


def user_route_targets(site):
    """라이브 사이트의 server 블록마다 (접속 주소, 사용자 스니펫) 을 고른다.

    listen 이 포트만 있는 블록은 127.0.0.1 에 더 구체적인 블록이 있으면 127.0.0.2 로 접속해 그 블록에 닿게 한다.
    """
    blocks = re.split(r'(?m)^server\s*\{', site)[1:]
    parsed = []
    for block in blocks:
        listen = re.search(r'listen\s+([^;]+);', block)
        snippet = USER_SNIPPETS.search(block)
        if listen and snippet and 'ssl' not in listen.group(1).split():
            parsed.append((listen.group(1).split()[0], snippet.group(1)))
    specific = {addr.split(':')[0] for addr, _ in parsed if ':' in addr}
    targets = []
    for addr, snippet in parsed:
        if ':' in addr:
            host, port = addr.rsplit(':', 1)
        else:
            host, port = ('127.0.0.2' if '127.0.0.1' in specific else '127.0.0.1'), addr
        targets.append((f'{host}:{port}', snippet))
    return targets


def check_user_routes(site_path=SITE, attempts=20):
    """배포 직후 사용자 경로를 자격 증명 없이 한 번씩 불러 본다. 인증이 걸린 위치는 401, 공개 위치는 200 만 정상이다.

    labd 와 Basic 인증은 자격 증명 없는 요청에 401 을 준다. 그 밖의 응답(5xx·404·nginx 자체의 403)은 사용자별 위치나
    인증 하위 요청이 깨졌다는 뜻이라 실패로 보고 롤백한다(2026-10-24 장애에서는 하위 요청 404 → 500).
    """
    if not site_path.is_file():
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for target, snippet in user_route_targets(site_path.read_text()):
        try:
            text = Path(snippet).read_text()
        except OSError:
            continue
        user = USER_PANEL.search(text)
        if not user:
            continue
        block = text[user.end():text.find('}', user.end())]
        expected = 401 if ('auth_request' in block or 'auth_basic' in block) else 200
        url = f'http://{target}/u/{user.group(1)}/ctl/status'
        status = None
        for _ in range(attempts):
            try:
                with opener.open(urllib.request.Request(url, headers={'Host': 'localhost'}), timeout=3) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            except OSError:
                status = None
            if status == expected:
                break
            time.sleep(1)
        if status != expected:
            raise RuntimeError(f'User route check failed: {Path(snippet).name} /u/<user>/ctl/status -> {status} '
                               f'(expected {expected})')
    print('User routes answer without server errors')


def deploy(allow_source_overwrite=False):
    preflight(allow_source_overwrite)
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
        check_user_routes()
        manifest = {name: hashlib.sha256((DEST / name).read_bytes()).hexdigest() for name in FILES}
        manifest['_source'] = source_revision()
        (DEST / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'Deployed; backup: {backup}')
        if manifest['_source']:
            src = manifest['_source']
            print(f"Source: {src['branch']}@{src['commit'][:12]}" + (f" tag {src['tag']}" if src.get('tag') else '') + (' (uncommitted changes)' if src['dirty'] else ''))
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
    mode.add_argument('--stamp', action='store_true', help='write .release-source.json from git before a tar transfer')
    parser.add_argument('--allow-source-overwrite', action='store_true',
                        help='deploy even if the running code is uncommitted or not in this source (rollbacks)')
    args = parser.parse_args()
    # Serialize deployments, including their backup and rollback stages.
    deployment_lock = None
    if args.apply or args.prepare:
        import fcntl
        if os.geteuid() != 0:
            raise RuntimeError('Run on the server with sudo')
        deployment_lock = open('/run/lock/netlab-deploy.lock', 'a')
        fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.stamp:
        write_release_source()
        return
    validate_source()
    if args.source_check:
        print('Source validation passed')
    elif args.prepare:
        prepare()
    elif args.apply:
        deploy(args.allow_source_overwrite)
    else:
        preflight(args.allow_source_overwrite)
        print('Preflight passed; --apply will briefly restart labd (not VMs, Docker or nginx)')


if __name__ == '__main__':
    main()
