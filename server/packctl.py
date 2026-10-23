#!/usr/bin/env python3
"""Learning pack v2 tool (M1).

  packctl.py validate                 compile the v2 catalog in memory and report references
  packctl.py build [--check]          write (or verify) knowledge/v2/catalog.json
  packctl.py bundle --pack P --out F  write the VM bundle tar of pack P from the repository sources
  packctl.py install --instance VM --bundle F [--bundle G ...]
                                      DEVELOPMENT ONLY: install runner + bundles (one release with
                                      every given pack) into a dedicated, unassigned verification VM.
  packctl.py image --alias NAME --bundle F [--bundle G ...] [--base IMAGE] [--source TEXT]
                                      build a new VM image (base image + runner + every catalog pack),
                                      run every solvable lesson in it, publish under a NEW alias and
                                      record fingerprint, runner/protocol and the pack/bundle map.
  packctl.py activate --alias NAME [--legacy]
                                      point new VMs (config `image`) at a recorded image whose bundles
                                      match the server catalog. --legacy allows an image without a
                                      record (rollback to the pre-runner image).
  packctl.py images                   image records, the configured image and VMs per base image
"""
import argparse
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
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
IMAGE_RECORDS = '/var/lib/netlab/images'
ALIAS_RE = re.compile(r'^[a-z][a-z0-9-]{2,62}$')
BUILDER_PREFIX = 'netlab-imgbuild-'
PROTOCOL = 1


class InstallRefused(RuntimeError):
    pass


class ImageError(RuntimeError):
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


def release_archive(code_dir, *bundles):
    """Tar of one release (runner + every bundle) plus release.json listing every file's hash and mode.

    `bundles` are (manifest, recipe, files) triples as returned by pack_format.read_bundle. A release
    replaces the previous one as a whole, so it must carry every pack the VM should have."""
    entries = runner_sources(code_dir)
    packs = [b[0]['pack'] for b in bundles]
    if len(set(packs)) != len(packs):
        raise InstallRefused('같은 팩의 번들을 두 번 줄 수 없습니다.')
    for manifest, recipe, files in bundles:
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


def install(instance, bundle_paths, run=lxc, code_dir=SERVER, catalog_path=CATALOG,
            config_path=CONFIG_PATH, state_path=STATE_PATH):
    if isinstance(bundle_paths, (str, os.PathLike)):
        bundle_paths = [bundle_paths]
    check_target(instance, run, config_path, state_path)
    catalog = json.loads(Path(catalog_path).read_text())
    bundles = []
    for bundle_path in bundle_paths:
        manifest, recipe, files = pack_format.read_bundle(bundle_path)
        pack = manifest['pack']
        if pack not in catalog['packs']:
            raise InstallRefused(f'서버 카탈로그에 없는 팩입니다: {pack}')
        pack_format.read_bundle(bundle_path, catalog['packs'][pack]['bundle']['revision'])
        bundles.append((manifest, recipe, files))
    runner_idle(instance, run)
    release_id, data = release_archive(code_dir, *bundles)
    manifest = bundles[0][0]
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
    return dict(ok=True, instance=instance, release=release_id,
                packs={b[0]['pack']: b[0]['bundle'] for b in bundles})


# ─────────────────────────────── images (M3) ───────────────────────────────

def read_config(config_path=CONFIG_PATH):
    try:
        return json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        raise ImageError('서버 설정을 읽지 못했습니다.') from None


def resolve_image(name, run=lxc):
    """Full fingerprint and type of an image given by alias or fingerprint, or (None, None)."""
    for path in (f'/1.0/images/aliases/{name}', f'/1.0/images/{name}'):
        rc, out, _ = run('query', path, timeout=60)
        if rc != 0:
            continue
        try:
            data = json.loads(out)
        except ValueError:
            continue
        fingerprint = data.get('target') or data.get('fingerprint')
        if fingerprint and 'target' in data:
            rc, out, _ = run('query', f'/1.0/images/{fingerprint}', timeout=60)
            data = json.loads(out) if rc == 0 else {}
        if fingerprint:
            return fingerprint, data.get('type')
    return None, None


def runner_version(code_dir=SERVER):
    listing = {name: hashlib.sha256(data).hexdigest() for name, data, _ in runner_sources(code_dir)}
    return hashlib.sha256(pack_format.canonical(listing)).hexdigest()[:16]


def catalog_bundles(catalog):
    return {pack: entry['bundle']['revision'] for pack, entry in catalog['packs'].items()}


def load_bundles(bundle_paths, catalog):
    """Verified bundles for exactly the catalog's packs: an image carries every pack the server offers."""
    bundles = []
    for bundle_path in bundle_paths:
        manifest, recipe, files = pack_format.read_bundle(bundle_path)
        pack = manifest['pack']
        if pack not in catalog['packs']:
            raise ImageError(f'서버 카탈로그에 없는 팩입니다: {pack}')
        pack_format.read_bundle(bundle_path, catalog['packs'][pack]['bundle']['revision'])
        bundles.append((manifest, recipe, files))
    given = sorted(b[0]['pack'] for b in bundles)
    if given != sorted(catalog['packs']):
        raise ImageError(f'카탈로그의 모든 팩 번들이 필요합니다: 필요 {sorted(catalog["packs"])}, 받음 {given}')
    return bundles


def runner_call(run, instance, request, timeout=900):
    if request.get('action') != 'status':
        request = dict(request, request_id=str(uuid.uuid4()))
    rc, out, err = run('exec', instance, '--', RUNNER_PATH, stdin=json.dumps(dict(protocol=PROTOCOL, **request)).encode(),
                       timeout=timeout)
    try:
        return json.loads(out)
    except ValueError:
        raise ImageError(f"실행기 응답을 읽지 못했습니다({request.get('action')}): {(err or out).strip()[-300:]}") from None


def smoke(run, instance, bundles, lessons=None, say=print, converge=180):
    """Run every solvable lesson the way a learner's web panel does: start → solve → every task check
    must pass within `converge` seconds → stop, with one reset → solve → check retry. Returns
    [{lesson, steps, attempts}]. Any other outcome stops the build."""
    done = []
    for manifest, recipe, _ in bundles:
        for lesson_id, ldef in sorted(recipe['lessons'].items()):
            if not (ldef.get('hooks') or {}).get('solve') or (lessons and lesson_id not in lessons):
                continue
            bundle = manifest['bundle']

            def call(action, **extra):
                status = runner_call(run, instance, dict(action='status'))
                if not status.get('ok') or not status.get('observed'):
                    raise ImageError(f'실행기 상태 이상: {status.get("error")} {status.get("message")}')
                response = runner_call(run, instance, dict(action=action, expected=status['observed'], **extra))
                if not response.get('ok'):
                    raise ImageError(f'{lesson_id} {action} 실패: {response.get("error")} {response.get("message")}')
                return response

            for attempt in (1, 2):
                # One reset retry: some topologies (17 BGP) occasionally come up without a routing daemon on
                # their first deploy in a fresh VM. The attempt count stays in the image record.
                say(f'  smoke {lesson_id}: {"start" if attempt == 1 else "reset (retry)"}')
                if attempt == 1:
                    call('start', target_lesson=lesson_id, expected_bundle=bundle)
                else:
                    call('reset', lesson=lesson_id, expected_bundle=bundle)
                call('solve', lesson=lesson_id, expected_bundle=bundle)
                steps, failed = {}, None
                for step in ldef.get('steps', {}):
                    deadline = time.time() + converge
                    while True:      # routing protocols converge tens of seconds after the fix
                        result = call('check', lesson=lesson_id, expected_bundle=bundle, step=step)['result']
                        if result['status'] == 'pass' or time.time() > deadline:
                            break
                        time.sleep(5)
                    steps[step] = result['status']
                    if result['status'] != 'pass':
                        failed = (f'{lesson_id} {step}: 해설 적용 뒤에도 {result["status"]} '
                                  f'{[i.get("message") or (i.get("detail") or "")[-120:] for i in result.get("items", [])]}')
                        break
                if not failed:
                    break
                say(f'  smoke {lesson_id}: {failed}')
                if attempt == 2:
                    raise ImageError(failed)
            call('stop', lesson=lesson_id)
            say(f'  smoke {lesson_id}: pass {steps} (attempt {attempt})')
            done.append(dict(lesson=lesson_id, steps=steps, attempts=attempt))
    return done


GUEST_PERMISSIONS = ('getent group clab_admins >/dev/null || groupadd --system clab_admins; '
                     'id -nG lab | tr " " "\\n" | grep -qx clab_admins || usermod -aG clab_admins lab')

# Runs inside the builder after the smoke run: leave no learner-visible trace and no machine identity.
GUEST_CLEAN = r"""
set -eu
rm -rf /home/lab/labs
if [ -d /opt/network-lab-pristine ]; then
  rm -rf /home/lab/network-lab
  mkdir -p /home/lab/network-lab
  cp -a /opt/network-lab-pristine/. /home/lab/network-lab/
  chown -R lab:lab /home/lab/network-lab
fi
rm -f /home/lab/.netlab-switch.lock /home/lab/.bash_history /root/.bash_history
rm -f /var/lib/netlab/runner/state.json /var/lib/netlab/runner/installed.json
rm -rf /var/lib/netlab/runner/checks
/opt/netlab/current/runner/netlab-runner --init >/dev/null
cloud-init clean --logs --machine-id
find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*' -delete
find /var/log/lab-sessions -maxdepth 1 -type f -delete 2>/dev/null || true
find /root /home/lab -maxdepth 2 -type f -name authorized_keys -delete
apt-get clean
"""


def write_record(record, records=IMAGE_RECORDS):
    os.makedirs(records, mode=0o700, exist_ok=True)
    path = os.path.join(records, record['alias'] + '.json')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read_record(alias, records=IMAGE_RECORDS):
    try:
        return json.loads(Path(records, alias + '.json').read_text())
    except (OSError, ValueError):
        return None


def builder_ip(run, config):
    """A fixed address above labd's managed range, so a DHCP lease of the builder can never collide with
    the address labd hands a new learner VM while the build runs."""
    prefix, last = config.get('ip_prefix', '10.200.0.'), int(config.get('ip_last', 199))
    rc, out, _ = run('list', '--format', 'json', timeout=120)
    if rc != 0:
        raise ImageError('LXD 인스턴스 목록을 읽지 못했습니다.')
    taken = set()
    for inst in json.loads(out):
        for device in (inst.get('expanded_devices') or {}).values():
            if device.get('ipv4.address'):
                taken.add(device['ipv4.address'])
        for nic in ((inst.get('state') or {}).get('network') or {}).values():
            taken.update(a['address'] for a in nic.get('addresses', []) if a.get('family') == 'inet')
    for n in range(250, last, -1):
        if f'{prefix}{n}' not in taken:
            return f'{prefix}{n}'
    raise ImageError('빌드 VM 에 줄 주소가 없습니다.')


def wait_agent(run, instance, seconds=300):
    deadline = time.time() + seconds
    while time.time() < deadline:
        rc, _, _ = run('exec', instance, '--', 'true', timeout=30)
        if rc == 0:
            return
        time.sleep(3)
    raise ImageError(f'{instance}: LXD 에이전트가 응답하지 않습니다.')


def build_image(alias, bundle_paths, base=None, source='', lessons=None, run=lxc, code_dir=SERVER,
                catalog_path=CATALOG, config_path=CONFIG_PATH, records=IMAGE_RECORDS, say=print):
    """Base image + runner + bundles → smoke-tested image under a new alias. Never replaces an alias
    or deletes an image; a failed build keeps its builder VM for diagnosis and prints its name."""
    if not ALIAS_RE.fullmatch(alias or ''):
        raise ImageError('별칭은 영문 소문자로 시작하는 3~63자의 소문자·숫자·하이픈이어야 합니다.')
    config = read_config(config_path)
    base = base or config.get('image', 'netlab')
    if resolve_image(alias, run)[0] or read_record(alias, records):
        raise ImageError(f'이미 있는 이미지 별칭입니다: {alias}. 새 이름을 쓰세요(기존 별칭은 바꾸지 않습니다).')
    base_fp, base_type = resolve_image(base, run)
    if not base_fp or base_type != 'virtual-machine':
        raise ImageError(f'기반 VM 이미지를 찾지 못했습니다: {base}')
    catalog = json.loads(Path(catalog_path).read_text())
    bundles = load_bundles(bundle_paths, catalog)
    builder = BUILDER_PREFIX + uuid.uuid4().hex[:10]
    profile = config.get('profile', 'default')
    say(f'builder {builder} ← {base} ({base_fp[:12]})')
    ip = builder_ip(run, config)
    rc, _, err = run('init', base, builder, '--vm', '--profile', profile, '-c', 'limits.cpu=2',
                     '-c', 'limits.memory=4GiB', '-d', f"root,size={config.get('disk', '50GiB')}", timeout=600)
    if rc != 0:
        raise ImageError(f'빌드 VM 생성 실패: {err.strip()[-300:]}')
    try:
        rc, _, err = run('config', 'device', 'override', builder, 'eth0', f'ipv4.address={ip}')
        if rc != 0:
            raise ImageError(f'빌드 VM 주소 지정 실패: {err.strip()[-300:]}')
        rc, _, err = run('start', builder, timeout=300)
        if rc != 0:
            raise ImageError(f'빌드 VM 기동 실패: {err.strip()[-300:]}')
        wait_agent(run, builder)
        run('exec', builder, '--', 'cloud-init', 'status', '--wait', timeout=900)   # degraded is fine
        # Same repair labd applies to every VM before handing it out (instances.ensure_lab_permissions):
        # older golden images lack the clab_admins membership containerlab needs for the lab user.
        rc, _, err = run('exec', builder, '--', 'sh', '-c', GUEST_PERMISSIONS, timeout=60)
        if rc != 0:
            raise ImageError(f'실습 계정 권한 정리 실패: {err.strip()[-300:]}')
        rc, _, err = run('file', 'push', '--mode=0644', str(Path(code_dir) / 'lab-shell.sh'),
                         f'{builder}/etc/lab-shell.sh')
        if rc != 0:
            raise ImageError(f'lab-shell.sh 전송 실패: {err.strip()}')
    except BaseException:
        say(f'빌드 실패: 진단용으로 {builder} 를 남겼습니다. 확인 뒤 `lxc delete --force {builder}` 로 지우세요.')
        raise
    return finish_image(builder, alias, bundles, catalog, dict(image=base, fingerprint=base_fp), source=source,
                        lessons=lessons, run=run, code_dir=code_dir, records=records, say=say)


def finish_image(builder, alias, bundles, catalog, base, source='', lessons=None, run=lxc, code_dir=SERVER,
                 records=IMAGE_RECORDS, say=print):
    """Running builder VM → runner + bundles installed, every solvable lesson smoke-tested, identity
    cleaned, published under `alias` with a record. Shared by `packctl image` and bootstrap's golden build.
    Deletes exactly `builder` on success and keeps it on failure."""
    release_id, data = release_archive(code_dir, *bundles)
    try:
        run('config', 'set', builder, f'{INCARNATION_KEY}={uuid.uuid4()}')
        with tempfile.NamedTemporaryFile(suffix='.tar') as tmp:
            tmp.write(data)
            tmp.flush()
            remote = f'/var/tmp/netlab-release-{release_id}.tar'
            rc, _, err = run('file', 'push', '--mode=0600', tmp.name, f'{builder}{remote}')
            if rc != 0:
                raise ImageError(f'릴리스 전송 실패: {err.strip()}')
        rc, out, err = run('exec', builder, '--', 'python3', '-c', GUEST_INSTALL, remote, release_id, '1', timeout=300)
        if rc != 0:
            raise ImageError(f'릴리스 설치 실패: {(err or out).strip()[-300:]}')
        rc, out, err = run('exec', builder, '--', RUNNER_PATH, '--init')
        if rc != 0 or '"ok": true' not in out:
            raise ImageError(f'실행기 초기화 실패: {(err or out).strip()[-300:]}')
        status = runner_call(run, builder, dict(action='status'))
        if status.get('installed') != catalog_bundles(catalog):
            raise ImageError(f'설치된 번들이 카탈로그와 다릅니다: {status.get("installed")}')
        smoked = smoke(run, builder, bundles, lessons, say)
        if not smoked:
            raise ImageError('검증할 레슨(해설 훅이 있는 레슨)이 없습니다.')
        rc, out, err = run('exec', builder, '--', 'sh', '-c', GUEST_CLEAN, timeout=600)
        if rc != 0:
            raise ImageError(f'빌드 VM 정리 실패: {(err or out).strip()[-300:]}')
        run('config', 'unset', builder, INCARNATION_KEY)
        rc, _, err = run('stop', builder, '--timeout=120', timeout=300)
        if rc != 0:
            raise ImageError(f'빌드 VM 정지 실패: {err.strip()[-300:]}')
        bundle_map = catalog_bundles(catalog)
        properties = [f'description=Netlab {alias} runner {runner_version(code_dir)}',
                      f'netlab.release={release_id}', f'netlab.runner.protocol={PROTOCOL}',
                      'netlab.bundles=' + ','.join(f'{k}:{v}' for k, v in sorted(bundle_map.items()))]
        rc, _, err = run('publish', builder, '--alias', alias, *properties, timeout=3600)
        if rc != 0:
            raise ImageError(f'이미지 게시 실패: {err.strip()[-300:]}')
        fingerprint, _ = resolve_image(alias, run)
        if not fingerprint:
            raise ImageError(f'게시한 이미지를 찾지 못했습니다: {alias}')
    except BaseException:
        say(f'빌드 실패: 진단용으로 {builder} 를 남겼습니다. 확인 뒤 `lxc delete --force {builder}` 로 지우세요.')
        raise
    record = dict(alias=alias, fingerprint=fingerprint, base=base,
                  built_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), source=source,
                  release=release_id, runner=dict(version=runner_version(code_dir), protocol=PROTOCOL),
                  catalog_revision=catalog.get('revision'), bundles=bundle_map, smoke=smoked)
    path = write_record(record, records)
    run('delete', builder, timeout=300)     # exactly the builder this build used
    say(f'게시 완료: {alias} = {fingerprint[:12]} · 기록 {path}')
    return record


def write_config_image(alias, config_path=CONFIG_PATH):
    """Atomically change only `image`, keeping the file's mode and owner. labd re-reads the config every
    maintenance tick and on SIGHUP."""
    path = Path(config_path)
    config = json.loads(path.read_text())
    previous = config.get('image', 'netlab')
    config['image'] = alias
    st = path.stat()
    tmp = path.with_name(path.name + '.packctl.tmp')
    with open(tmp, 'w') as f:
        os.fchmod(f.fileno(), st.st_mode & 0o777)
        try:
            os.fchown(f.fileno(), st.st_uid, st.st_gid)
        except PermissionError:
            pass
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return previous


def labd_reload():
    try:
        pid = int(subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'labd'],
                                 capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if pid > 0:
        os.kill(pid, signal.SIGHUP)
        return True
    return False


def activate(alias, legacy=False, run=lxc, catalog_path=CATALOG, config_path=CONFIG_PATH, records=IMAGE_RECORDS):
    """Point new VMs at `alias`. Without --legacy the image must have a record whose fingerprint is the
    alias's current target and whose bundles equal the server catalog (bundle 정합성)."""
    fingerprint, kind = resolve_image(alias, run)
    if not fingerprint or kind != 'virtual-machine':
        raise ImageError(f'VM 이미지를 찾지 못했습니다: {alias}')
    record = read_record(alias, records)
    if not legacy:
        if not record:
            raise ImageError(f'{alias} 의 이미지 기록이 없습니다. packctl image 로 만든 이미지만 연결합니다'
                             '(실행기 없는 이전 이미지로 되돌릴 때는 --legacy).')
        if record['fingerprint'] != fingerprint:
            raise ImageError(f'{alias} 가 기록과 다른 이미지를 가리킵니다(기록 {record["fingerprint"][:12]}, 현재 {fingerprint[:12]}).')
        catalog = json.loads(Path(catalog_path).read_text())
        if record['bundles'] != catalog_bundles(catalog):
            raise ImageError('이미지의 번들이 지금 서버 카탈로그와 다릅니다. 같은 번들로 이미지를 새로 만드세요.')
    previous = write_config_image(alias, config_path)
    return dict(ok=True, image=alias, fingerprint=fingerprint, previous=previous, runner=bool(record) and not legacy)


def images_report(run=lxc, config_path=CONFIG_PATH, state_path=STATE_PATH, records=IMAGE_RECORDS):
    """Records, the configured image and how many managed VMs come from each base image."""
    config = read_config(config_path)
    configured = config.get('image', 'netlab')
    current_fp, _ = resolve_image(configured, run)
    found = []
    for path in sorted(Path(records).glob('*.json')) if Path(records).is_dir() else []:
        try:
            record = json.loads(path.read_text())
        except ValueError:
            continue
        found.append({k: record.get(k) for k in ('alias', 'fingerprint', 'built_at', 'release', 'bundles', 'source')})
    state = json.loads(Path(state_path).read_text())
    by_base = {}
    for kind, names in (('users', [r.get('instance') for r in state.get('users', {}).values()]),
                        ('pool', [p.get('instance') for p in state.get('pool', [])])):
        for name in filter(None, names):
            rc, out, _ = run('config', 'get', name, 'volatile.base_image', timeout=30)
            fp = out.strip() if rc == 0 and out.strip() else 'unknown'
            entry = by_base.setdefault(fp, dict(users=0, pool=0))
            entry[kind] += 1
    recorded = {r['fingerprint']: r['alias'] for r in found if r.get('fingerprint')}
    return dict(configured=configured, fingerprint=current_fp, runner=current_fp in recorded, records=found,
                vms={fp: dict(v, image=recorded.get(fp, 'current' if fp == current_fp else 'legacy'))
                     for fp, v in by_base.items()})


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
    inst.add_argument('--bundle', required=True, action='append')
    image = sub.add_parser('image')
    image.add_argument('--alias', required=True)
    image.add_argument('--bundle', required=True, action='append')
    image.add_argument('--base', help='base image alias or fingerprint (default: the configured image)')
    image.add_argument('--source', default='', help='source revision recorded with the image, e.g. a git commit')
    image.add_argument('--lesson', action='append', help='smoke only these lessons (default: every solvable lesson)')
    act = sub.add_parser('activate')
    act.add_argument('--alias', required=True)
    act.add_argument('--legacy', action='store_true')
    sub.add_parser('images')
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
    if os.geteuid() != 0:
        raise SystemExit('root 로 실행하세요.')
    try:
        if args.command == 'image':
            record = build_image(args.alias, args.bundle, base=args.base, source=args.source, lessons=args.lesson,
                                 say=lambda line: print(line, flush=True))
            print(json.dumps(record, ensure_ascii=False, indent=2))
        elif args.command == 'activate':
            result = activate(args.alias, legacy=args.legacy)
            result['labd_reloaded'] = labd_reload()
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == 'images':
            print(json.dumps(images_report(), ensure_ascii=False, indent=2))
    except (ImageError, pack_format.PackError) as exc:
        raise SystemExit(f'{args.command} 실패: {exc}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
