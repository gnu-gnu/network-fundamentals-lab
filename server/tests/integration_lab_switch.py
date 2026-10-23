#!/usr/bin/env python3
"""Destructive lab lifecycle check, restricted to an empty disposable VM."""
import argparse
from pathlib import Path
import subprocess


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr)[-3000:])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    args = parser.parse_args()
    instance = args.instance
    if not instance.startswith('netlab-tutor-verify-'):
        raise SystemExit('Only disposable tutor verification VMs are allowed')

    def guest(*command):
        return run('lxc', 'exec', instance, '--', *command)

    if guest('docker', 'ps', '-a', '--filter', 'name=clab-', '--format', '{{.Names}}').strip():
        raise SystemExit('Refusing to touch an existing lab')
    run('lxc', 'file', 'push', '--mode=0644', str(Path(__file__).resolve().parents[1] / 'lab-shell.sh'),
        instance + '/etc/lab-shell.sh')

    def shell(command):
        return guest('sudo', '-H', '-u', 'lab', 'bash', '-ic', 'cd; . /etc/lab-shell.sh; ' + command)

    def ids():
        return guest('docker', 'ps', '--filter', 'name=clab-', '--format', '{{.Names}} {{.ID}}').strip()

    try:
        shell('lab 01')
        before = ids()
        assert 'clab-01-' in before, before
        shell('lab 01')
        assert ids() == before, 'Same chapter recreated containers'
        shell('lab 04')
        current = ids()
        assert current and all(row.startswith('clab-04-') for row in current.splitlines()), current
        before = current
        shell('lab 04')
        assert ids() == before, 'Same chapter recreated containers'
        shell('lab 02')
        current = ids()
        assert current and all(row.startswith('clab-02-') for row in current.splitlines()), current
        shell('unlab')
        assert not ids(), 'Containers remain after unlab'
        print('PASS: 01 -> same 01 preserved -> 04 only -> same 04 preserved -> 02 only -> empty')
    finally:
        shell('unlab')


if __name__ == '__main__':
    main()
