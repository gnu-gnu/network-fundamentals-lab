#!/usr/bin/env python3
"""Learning pack v2 tool (M1).

  packctl.py validate                 compile the v2 catalog in memory and report references
  packctl.py build [--check]          write (or verify) knowledge/v2/catalog.json
  packctl.py bundle --pack P --out F  write the VM bundle tar of pack P from the repository sources
  packctl.py install --instance VM --bundle F
                                      DEVELOPMENT ONLY: install runner + bundle into a dedicated,
                                      unassigned verification VM. Production uses images (M3).
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER))
import pack_format  # noqa: E402

ROOT = SERVER.parent
CATALOG = SERVER / 'knowledge' / 'v2' / 'catalog.json'
CONFIG_PATH = '/etc/netlab/config.json'
STATE_PATH = '/var/lib/netlab/state.json'
VERIFY_KEY = 'user.netlab.verify'
INCARNATION_KEY = 'user.netlab.incarnation'
RUNNER_PATH = '/opt/netlab/current/runner/netlab-runner'
RUNNER_FILES = ('netlab-runner',)
KEEP_RELEASES = 2


class InstallRefused(RuntimeError):
    pass


def lxc(*args, timeout=120, stdin=None):
    p = subprocess.run(['lxc', *args], input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


def runner_sources(code_dir):
    """(archive name, bytes, mode) of the runner: netlab-runner inside the runner package dir."""
    code_dir = Path(code_dir)
    items = [('runner/netlab-runner', (code_dir / 'netlab-runner').read_bytes(), 0o755)]
    for path in sorted((code_dir / 'runner').glob('*.py')):
        items.append(('runner/' + path.name, path.read_bytes(), 0o644))
    return items


def release_archive(code_dir, manifest, recipe, files):
    """Tar of one release (runner + bundle) plus release.json listing every file's hash and mode."""
    entries = runner_sources(code_dir)
    pack = manifest['pack']
    entries.append((f'packs/{pack}/recipe.json', pack_format.canonical(recipe), 0o644))
    entries.append((f'packs/{pack}/manifest.json', pack_format.canonical(manifest), 0o644))
    for path, (data, executable) in sorted(files.items()):
        entries.append((f'packs/{pack}/files/{path}', data, 0o755 if executable else 0o644))
    listing = {name: dict(sha256=hashlib.sha256(data).hexdigest(), mode=mode) for name, data, mode in entries}
    release_id = hashlib.sha256(pack_format.canonical(listing)).hexdigest()[:16]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data, mode in entries + [('release.json', pack_format.canonical(listing), 0o644)]:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, 0
            archive.addfile(info, io.BytesIO(data))
    return release_id, buffer.getvalue()


# Runs inside the VM as root: extract to staging, verify every hash and mode, then switch the
# `current` symlink atomically. Any failure leaves the previous installation untouched.
GUEST_INSTALL = r'''
import hashlib, json, os, shutil, sys, tarfile
tar_path, release_id, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])
base = sys.argv[4] if len(sys.argv) > 4 else '/opt/netlab'   # override only used by tests
releases = os.path.join(base, 'releases')
os.makedirs(releases, mode=0o755, exist_ok=True)
staging = os.path.join(releases, '.staging-' + release_id)
final = os.path.join(releases, release_id)
shutil.rmtree(staging, ignore_errors=True)
os.makedirs(staging, mode=0o755)
try:
    with tarfile.open(tar_path) as archive:
        for info in archive.getmembers():
            name = info.name
            if not info.isfile() or name.startswith('/') or '..' in name.split('/'):
                raise SystemExit('unsafe member: ' + name)
        archive.extractall(staging, filter='data') if hasattr(tarfile, 'data_filter') else archive.extractall(staging)
    listing = json.load(open(os.path.join(staging, 'release.json')))
    seen = set()
    for root, dirs, files in os.walk(staging):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), staging)
            if rel != 'release.json':
                seen.add(rel)
    if seen != set(listing):
        raise SystemExit('file set differs from release.json')
    for rel, meta in listing.items():
        path = os.path.join(staging, rel)
        os.chmod(path, meta['mode'])
        if hashlib.sha256(open(path, 'rb').read()).hexdigest() != meta['sha256']:
            raise SystemExit('hash mismatch: ' + rel)
    if os.path.exists(final):
        shutil.rmtree(staging)
    else:
        os.rename(staging, final)
    link_tmp = os.path.join(base, '.current.' + release_id)
    if os.path.lexists(link_tmp):
        os.unlink(link_tmp)
    os.symlink(os.path.join('releases', release_id), link_tmp)
    os.replace(link_tmp, os.path.join(base, 'current'))
    current = os.path.realpath(os.path.join(base, 'current'))
    entries = sorted((os.path.getmtime(os.path.join(releases, d)), d) for d in os.listdir(releases)
                     if not d.startswith('.') and os.path.join(releases, d) != current)
    for _, d in entries[:-max(keep - 1, 0)] if keep > 1 else entries:
        shutil.rmtree(os.path.join(releases, d), ignore_errors=True)
finally:
    shutil.rmtree(staging, ignore_errors=True)
    try:
        os.unlink(tar_path)
    except OSError:
        pass
print(json.dumps({'ok': True, 'release': release_id}))
'''


def check_target(instance, run=lxc, config_path=CONFIG_PATH, state_path=STATE_PATH):
    """Refuse anything but a dedicated, managed, unassigned VM on a development server."""
    try:
        config = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        raise InstallRefused('서버 설정을 읽지 못했습니다.') from None
    if config.get('environment') != 'development':
        raise InstallRefused('개발 환경(config environment=development)에서만 설치할 수 있습니다. 운영은 이미지로 배포합니다.')
    rc, out, err = run('config', 'get', instance, VERIFY_KEY)
    if rc != 0 or out.strip() != 'true':
        raise InstallRefused(f'{instance} 에 전용 검증 라벨({VERIFY_KEY}=true)이 없습니다.')
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        raise InstallRefused('관리 상태(state.json)를 읽지 못했습니다.') from None
    if any((rec or {}).get('instance') == instance for rec in state.get('users', {}).values()):
        raise InstallRefused(f'{instance} 는 사용자에게 할당되어 있습니다. 할당 전에만 설치합니다.')
    pool = [p for p in state.get('pool', []) if p.get('instance') == instance]
    if not pool:
        raise InstallRefused(f'{instance} 는 labd 가 관리하는 예비 환경이 아닙니다.')
    if pool[0].get('claim') or not pool[0].get('ready'):
        raise InstallRefused(f'{instance} 는 준비 중이거나 배정이 예약된 환경입니다.')
    rc, out, _ = run('list', instance, '--format', 'json')
    try:
        running = rc == 0 and any(i['name'] == instance and i['status'] == 'Running' for i in json.loads(out))
    except ValueError:
        running = False
    if not running:
        raise InstallRefused(f'{instance} 가 실행 중이 아닙니다.')


def runner_idle(instance, run=lxc):
    """If a runner is installed it must be idle with no check or change in progress."""
    rc, _, _ = run('exec', instance, '--', 'test', '-x', RUNNER_PATH)
    if rc != 0:
        return
    rc, out, err = run('exec', instance, '--', RUNNER_PATH, stdin=b'{"protocol":1,"action":"status"}')
    try:
        status = json.loads(out)
    except ValueError:
        raise InstallRefused('설치된 실행기 상태를 읽지 못했습니다. 설치하지 않았습니다.') from None
    if status.get('state') != 'idle' or status.get('check_running'):
        raise InstallRefused(f"실행 중인 실습이나 확인 작업이 있습니다(state={status.get('state')}). 설치하지 않았습니다.")


def install(instance, bundle_path, run=lxc, code_dir=SERVER, catalog_path=CATALOG,
            config_path=CONFIG_PATH, state_path=STATE_PATH):
    check_target(instance, run, config_path, state_path)
    catalog = json.loads(Path(catalog_path).read_text())
    manifest, recipe, files = pack_format.read_bundle(bundle_path)
    pack = manifest['pack']
    if pack not in catalog['packs']:
        raise InstallRefused(f'서버 카탈로그에 없는 팩입니다: {pack}')
    pack_format.read_bundle(bundle_path, catalog['packs'][pack]['bundle']['revision'])
    runner_idle(instance, run)
    release_id, data = release_archive(code_dir, manifest, recipe, files)
    with tempfile.NamedTemporaryFile(suffix='.tar') as tmp:
        tmp.write(data)
        tmp.flush()
        remote = f'/var/tmp/netlab-release-{release_id}.tar'
        rc, _, err = run('file', 'push', '--mode=0600', tmp.name, f'{instance}{remote}')
        if rc != 0:
            raise InstallRefused(f'파일 전송 실패: {err.strip()}')
    rc, out, err = run('exec', instance, '--', 'python3', '-c', GUEST_INSTALL, remote, release_id, str(KEEP_RELEASES),
                       timeout=300)
    if rc != 0:
        raise InstallRefused(f'VM 안 설치 실패(이전 설치 유지): {(err or out).strip()[-500:]}')
    rc, out, err = run('exec', instance, '--', RUNNER_PATH, '--init')
    if rc != 0 or '"ok": true' not in out:
        raise InstallRefused(f'실행기 초기화 실패: {(err or out).strip()[-300:]}')
    rc, out, _ = run('config', 'get', instance, INCARNATION_KEY)
    if rc == 0 and not out.strip():
        import uuid
        run('config', 'set', instance, f'{INCARNATION_KEY}={uuid.uuid4()}')
    return dict(ok=True, instance=instance, release=release_id, pack=pack, bundle=manifest['bundle'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate')
    build = sub.add_parser('build')
    build.add_argument('--check', action='store_true')
    bundle = sub.add_parser('bundle')
    bundle.add_argument('--pack', default='network')
    bundle.add_argument('--out', required=True)
    inst = sub.add_parser('install')
    inst.add_argument('--instance', required=True)
    inst.add_argument('--bundle', required=True)
    args = parser.parse_args(argv)

    if args.command == 'validate':
        catalog = pack_format.build(ROOT)
        for pack_id, pack in catalog['packs'].items():
            print(f"{pack_id}: {len(pack['lessons'])} lesson(s), pack {pack['revision'][:12]}, bundle {pack['bundle']['revision'][:12]}")
            for ref in pack['unverified_references']:
                print('  unverified reference (check on a dedicated VM):', ref)
        return 0
    if args.command == 'build':
        catalog = pack_format.build(ROOT)
        if args.check:
            if not CATALOG.is_file() or json.loads(CATALOG.read_text()) != catalog:
                raise SystemExit('Stale: knowledge/v2/catalog.json; run python3 server/build_knowledge.py')
            print('v2 catalog matches source')
            return 0
        CATALOG.parent.mkdir(exist_ok=True)
        staging = CATALOG.with_suffix('.json.tmp')
        staging.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
        staging.replace(CATALOG)
        print(f"Built {len(catalog['lessons'])} v2 lesson(s)")
        return 0
    if args.command == 'bundle':
        catalog = json.loads(CATALOG.read_text())
        if catalog != pack_format.build(ROOT):
            raise SystemExit('Stale: knowledge/v2/catalog.json; run python3 server/build_knowledge.py')
        manifest = pack_format.write_bundle(ROOT, catalog, args.pack, args.out)
        print(json.dumps(dict(pack=args.pack, bundle=manifest['bundle'], files=len(manifest['files']))))
        return 0
    if args.command == 'install':
        if os.geteuid() != 0:
            raise SystemExit('root 로 실행하세요.')
        try:
            print(json.dumps(install(args.instance, args.bundle), ensure_ascii=False))
        except (InstallRefused, pack_format.PackError) as exc:
            raise SystemExit(f'설치 거부: {exc}')
        return 0


if __name__ == '__main__':
    sys.exit(main())
