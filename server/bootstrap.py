#!/usr/bin/env python3
"""Ubuntu 24.04 host -> working Netlab. Default is a read-only plan; --apply installs.

Physical disk formatting, global LXD init/reset and remote Access policy changes are
deliberately not performed. Existing Netlab installations use deploy.py --apply.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid

import deploy

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path('/var/lib/netlab-bootstrap')
PACKAGES = ['snapd', 'nginx', 'apache2-utils', 'python3-jwt', 'python3-cryptography', 'python3-venv',
            'iptables', 'iproute2', 'zfsutils-linux', 'curl', 'ca-certificates', 'gnupg']


def run(*args, timeout=1800, input=None):
    print('+', ' '.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], input=input, text=True,
                          check=True, timeout=timeout, capture_output=True).stdout.strip()


def exists(kind, name):
    verb = 'info' if kind == 'image' else 'show'
    return subprocess.run(['lxc', kind, verb, name], capture_output=True).returncode == 0


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with open(temp, 'w') as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, indent=2)
        f.write('\n')
    os.replace(temp, path)


def validate(args):
    subnet = ipaddress.ip_interface(args.subnet)
    if subnet.version != 4 or subnet.network.prefixlen != 24 or int(subnet.ip) % 256 != 1:
        raise RuntimeError('--subnet must be an IPv4 /24 gateway ending in .1')
    if platform.machine() != 'x86_64':
        raise RuntimeError('This release supports x86_64 hosts (FRR labs included)')
    os_release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('Ubuntu 24.04 required')
    if not Path('/dev/kvm').exists():
        raise RuntimeError('/dev/kvm missing: enable hardware/nested virtualization first')
    if not re.fullmatch(r'[1-9][0-9]*GiB', args.storage_size):
        raise RuntimeError('--storage-size must be an integer GiB value')
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.containerlab_version):
        raise RuntimeError('Invalid containerlab version')
    if bool(args.access_team_domain) != bool(args.access_aud):
        raise RuntimeError('Specify both --access-team-domain and --access-aud, or neither')
    if args.access_team_domain and not re.fullmatch(r'[a-z0-9-]+\.cloudflareaccess\.com', args.access_team_domain):
        raise RuntimeError('Invalid Access team domain')
    if args.admin_email and not re.fullmatch(r'[^\s@]+@[^\s@]+', args.admin_email):
        raise RuntimeError('Invalid admin email')
    deploy.validate_source()


def ensure_lxd(args):
    if subprocess.run(['snap', 'list', 'lxd'], capture_output=True).returncode:
        run('snap', 'install', 'lxd', '--channel=5.21/stable')
    run('lxd', 'waitready', '--timeout=120')
    # Never change an existing daemon's listener or its default profile.
    if not exists('storage', 'labpool'):
        size = int(args.storage_size[:-3]) * 1024**3
        if shutil.disk_usage('/var/snap/lxd/common').free < size + 10 * 1024**3:
            raise RuntimeError('Insufficient space: ZFS loop pool plus 10GiB headroom required')
        run('lxc', 'storage', 'create', 'labpool', 'zfs', f'size={args.storage_size}')
    else:
        pool = json.loads(run('lxc', 'query', '/1.0/storage-pools/labpool'))
        if pool['driver'] != 'zfs':
            raise RuntimeError('Existing labpool is not ZFS; refusing to replace it')
    if not exists('network', 'lxdbr0'):
        # Detect routes overlapping the intended lab subnet before creating a bridge.
        routes = json.loads(run('ip', '-j', '-4', 'route'))
        lab = ipaddress.ip_interface(args.subnet).network
        for route in routes:
            dest = route.get('dst', 'default')
            if dest != 'default' and ipaddress.ip_network(dest, strict=False).overlaps(lab):
                raise RuntimeError(f'Network overlap: {dest}')
        run('lxc', 'network', 'create', 'lxdbr0', f'ipv4.address={args.subnet}',
            'ipv4.nat=true', 'ipv6.address=none')
    network = json.loads(run('lxc', 'query', '/1.0/networks/lxdbr0'))
    cfg = network['config']
    if cfg.get('ipv4.address') != args.subnet or cfg.get('ipv6.address') != 'none' or cfg.get('ipv4.nat') != 'true':
        raise RuntimeError('Existing lxdbr0 differs from Netlab requirements; refusing to change it')
    if not exists('profile', 'netlab'):
        run('lxc', 'profile', 'create', 'netlab')
        run('lxc', 'profile', 'device', 'add', 'netlab', 'root', 'disk', 'path=/', 'pool=labpool')
        run('lxc', 'profile', 'device', 'add', 'netlab', 'eth0', 'nic', 'name=eth0',
            'network=lxdbr0', 'security.port_isolation=true')
    profile = json.loads(run('lxc', 'query', '/1.0/profiles/netlab'))
    devices = profile['devices']
    if set(devices) != {'eth0', 'root'} or devices['root'].get('pool') != 'labpool' or \
            devices['eth0'].get('network') != 'lxdbr0' or devices['eth0'].get('security.port_isolation') != 'true':
        raise RuntimeError('Existing netlab profile differs; refusing to overwrite')


def download_deb(version, directory):
    name = f'containerlab_{version}_linux_amd64.deb'
    base = f'https://github.com/srl-labs/containerlab/releases/download/v{version}/'
    checksum_text = run('curl', '-fSL', '--retry', '3', base + 'checksums.txt')
    expected = next((line.split()[0] for line in checksum_text.splitlines()
                     if line.split()[-1].lstrip('*') == name), None)
    if not expected or not re.fullmatch('[0-9a-f]{64}', expected):
        raise RuntimeError('Release checksum entry not found')
    target = directory / 'containerlab.deb'
    run('curl', '-fSL', '--retry', '3', '-o', target, base + name)
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise RuntimeError('containerlab checksum mismatch')
    return target


def pack_labs(output):
    repo = ROOT.parent
    # Explicit curriculum allowlist: never copy server secrets or .git into guests.
    roots = [p for p in repo.iterdir() if p.is_dir() and re.match(r'^\d\d-', p.name)]
    roots += [repo / n for n in ('README.md', 'clab.sh', 'checkpoints')]
    with tarfile.open(output, 'w') as archive:
        def keep(info):
            if info.issym() or info.islnk():
                raise RuntimeError(f'Refusing curriculum symlink: {info.name}')
            if any(part.startswith('clab-') or part == '.git' for part in Path(info.name).parts):
                return None
            return info
        for path in roots:
            archive.add(path, arcname=path.name, filter=keep)


def build_image(args):
    if exists('image', 'netlab'):
        alias = json.loads(run('lxc', 'query', '/1.0/images/aliases/netlab'))
        image = json.loads(run('lxc', 'query', '/1.0/images/' + alias['target']))
        if image.get('type') != 'virtual-machine':
            raise RuntimeError('netlab alias is not a VM image')
        print('Reusing existing netlab image; no alias replacement')
        return
    name = 'netlab-build-' + uuid.uuid4().hex[:10]
    state = STATE_DIR / 'builder.json'
    # Failure leaves the named builder for diagnosis; next run does not delete anything.
    write_json(state, {'instance': name, 'status': 'building'})
    run('lxc', 'launch', 'ubuntu:24.04', name, '--vm', '--profile', 'netlab',
        '-c', 'limits.cpu=2', '-c', 'limits.memory=4GiB', '-d', 'root,size=30GiB')
    for _ in range(120):
        if subprocess.run(['lxc', 'exec', name, '--', 'true'], capture_output=True).returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f'LXD guest agent not ready in {name}')
    run('lxc', 'exec', name, '--', 'cloud-init', 'status', '--wait')
    with tempfile.TemporaryDirectory(prefix='build-', dir=STATE_DIR) as temp:
        temp = Path(temp)
        deb = download_deb(args.containerlab_version, temp)
        archive = temp / 'labs.tar'
        pack_labs(archive)
        for p in [deb, archive] + [ROOT / n for n in
                ('guest-bootstrap.sh', 'apply-shell.sh', 'lab-shell.sh', 'lab-session', 'ttyd-lab')]:
            run('lxc', 'file', 'push', p, f'{name}/root/{p.name}')
        run('lxc', 'exec', name, '--', 'touch', '/root/netlab-build-marker')
        try:
            result = run('lxc', 'exec', name, '--', 'bash', '/root/guest-bootstrap.sh', timeout=3600)
        except subprocess.CalledProcessError as e:
            (STATE_DIR / 'guest-build.log').write_text((e.stdout or '') + (e.stderr or ''))
            raise RuntimeError(f'Guest build failed: {name}; see {STATE_DIR}/guest-build.log') from e
        (STATE_DIR / 'guest-build.log').write_text(result)
        if 'NETLAB_GOLDEN_SMOKE_OK' not in result:
            raise RuntimeError('Guest smoke result missing')
    run('lxc', 'stop', name, '--timeout=60')
    run('lxc', 'publish', name, '--alias', 'netlab', timeout=1800)
    write_json(state, {'instance': name, 'status': 'published'})
    # Exact uniquely-created build VM only; never remove user instances or an old image.
    run('lxc', 'delete', name)


def configure_portal(args):
    marker = STATE_DIR / 'preparing'
    if not deploy.DEST.exists():
        marker.touch()
        deploy.prepare(auth_mode='protected' if args.access_team_domain else 'open')
    elif not marker.exists():
        raise RuntimeError('Existing installation found; use deploy.py --apply')
    cfg_path = Path('/etc/netlab/config.json')
    cfg = json.loads(cfg_path.read_text())
    cfg.update(profile='netlab', pool_size=0, ip_prefix=args.subnet.split('/')[0].rsplit('.', 1)[0] + '.')
    if args.access_team_domain:
        cfg.update(access_team_domain=args.access_team_domain, access_aud=args.access_aud)
    write_json(cfg_path, cfg)
    if args.admin_email:
        users_path = Path('/var/lib/netlab/users.json')
        users = json.loads(users_path.read_text())
        if users and users.get('operator', {}).get('email') != args.admin_email:
            raise RuntimeError('Refusing to replace existing user mappings')
        if not users:
            users['operator'] = dict(email=args.admin_email, role='admin', disabled=False,
                                     created='', last_login='')
            write_json(users_path, users)
    install_firewall()
    dropin = Path('/etc/systemd/system/labd.service.d')
    dropin.mkdir(exist_ok=True)
    (dropin / 'netguard.conf').write_text('[Unit]\nRequires=netlab-bootstrap-netguard.service\nAfter=netlab-bootstrap-netguard.service\n')
    link = Path('/etc/nginx/sites-enabled/netlab')
    if not link.exists():
        link.symlink_to('/etc/nginx/sites-available/netlab')
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'labd')
    run('systemctl', 'enable', '--now', 'netlab-community')
    run('systemctl', 'reload', 'nginx')
    deploy.health()


def install_firewall():
    # Safe to install before building: guests never have a LAN access window.
    deploy.DEST.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'netguard.sh', deploy.DEST / 'netguard.sh')
    (deploy.DEST / 'netguard.sh').chmod(0o755)
    for name in ('netlab-bootstrap-netguard.service', 'netlab-bootstrap-netguard.timer'):
        shutil.copyfile(ROOT / 'systemd' / name, Path('/etc/systemd/system') / name)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'netlab-bootstrap-netguard.service', 'netlab-bootstrap-netguard.timer')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--prepare-restore', action='store_true', help='prepare empty infrastructure only; restore a full backup next')
    p.add_argument('--storage-size', default='100GiB', help='new loop-backed ZFS pool size')
    p.add_argument('--subnet', default='10.200.0.1/24')
    p.add_argument('--containerlab-version', default='0.74.2')
    p.add_argument('--access-team-domain', default='')
    p.add_argument('--access-aud', default='')
    p.add_argument('--admin-email', default='')
    args = p.parse_args()
    validate(args)
    print('Plan: Ubuntu packages → LXD 5.21 → labpool/lxdbr0/netlab profile → firewall → golden VM → portal')
    if not args.apply:
        print('Read-only plan. Add --apply to execute. Default: unauthenticated shared guest portal on :8088.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    import fcntl
    with open('/run/lock/netlab-deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path('/etc/netlab/config.json').exists() and not (STATE_DIR / 'preparing').exists():
            if args.prepare_restore:
                raise RuntimeError('--prepare-restore requires a new host')
            deploy.deploy()
            return
        os.environ['PATH'] += ':/snap/bin'
        os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
        os.environ['NEEDRESTART_MODE'] = 'l'
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', *PACKAGES)
        ensure_lxd(args)
        # Prepare before firewall installation, which creates /opt/netlab-admin.
        if not deploy.DEST.exists():
            (STATE_DIR / 'preparing').touch()
            deploy.prepare(auth_mode='protected' if args.access_team_domain else 'open')
        install_firewall()
        if args.prepare_restore:
            dropin = Path('/etc/systemd/system/labd.service.d')
            dropin.mkdir(exist_ok=True)
            (dropin / 'netguard.conf').write_text('[Unit]\nRequires=netlab-bootstrap-netguard.service\nAfter=netlab-bootstrap-netguard.service\n')
            run('systemctl', 'daemon-reload')
            print('Empty restore target ready. Run netlab-backup restore --dry-run, then restore.')
            return
        build_image(args)
        configure_portal(args)
        (STATE_DIR / 'preparing').unlink(missing_ok=True)
        write_json(STATE_DIR / 'complete.json', {'completed_at': time.time(), 'containerlab': args.containerlab_version})
        print('Installation verified. Default open portal: http://SERVER_IP:8088; Access mode uses loopback.')
        print('Configure Cloudflare Access and Tunnel externally; secrets stay on the host.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as e:
        print((e.stderr or '')[-2000:])
        raise SystemExit(f'Command failed with exit status {e.returncode}')
