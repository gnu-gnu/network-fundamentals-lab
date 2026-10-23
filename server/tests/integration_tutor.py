#!/usr/bin/env python3
"""Explicit destructive-in-guest integration check, restricted to disposable VMs.

Run on a test manager: python3 server/tests/integration_tutor.py
  --instance netlab-tutor-verify-YYYYMMDD [--chapter 04]
The guest must be a fresh Netlab golden image. Never uses a learner's assigned VM.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tutor_chat import tutor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--chapter', action='append', choices=list(tutor.CHAPTERS))
    args = parser.parse_args()
    if not re.fullmatch(r'netlab-tutor-verify-[a-zA-Z0-9-]+', args.instance):
        parser.error('Only a separately created netlab-tutor-verify-* disposable VM is accepted')
    instance = args.instance
    if tutor.deployed_chapter(instance):
        parser.error('Guest already has an active lab; refusing to replace it')
    rc, _, _ = tutor.lxc_exec(instance, ['docker', 'info'], 20)
    if rc:
        parser.error('Wait for guest Docker to be ready before running this check')
    tutor.lxc_exec(instance, ['bash', '-c', 'getent group clab_admins >/dev/null || groupadd --system clab_admins; usermod -aG clab_admins lab'])

    def shell(command):
        result = subprocess.run(['lxc', 'exec', instance, '--', 'sudo', '-H', '-u', 'lab',
                                 'bash', '-ic', command], capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError((result.stdout + result.stderr)[-2000:])
        return result

    failures = []
    for chapter in args.chapter or sorted(tutor.CHAPTERS):
        try:
            shell('cd "$HOME"; lab ' + chapter.replace('17-', '17 '))
            if tutor.deployed_chapter(instance) != chapter:
                raise RuntimeError('Chapter detection mismatch')
            before = tutor.check(instance, chapter)
            probes = tutor.gather(instance, chapter)
            after = None
            meta = tutor.CHAPTERS[chapter]
            if not meta.get('observational') and meta.get('assessment_mode') != 'exploration':
                if before['ok']:
                    raise RuntimeError('Unexpected initial success')
                # FRR may still be bringing up its control sockets after deploy returns.
                for attempt in range(5):
                    try:
                        shell('cd "$HOME/network-lab"; bash ' + chapter[:2] + '-*/fix.sh')
                        break
                    except RuntimeError:
                        if not chapter.startswith('17') or attempt == 4:
                            raise
                        time.sleep(5)
                until = time.monotonic() + 120
                while time.monotonic() < until:
                    after = tutor.check(instance, chapter)
                    if after['ok']:
                        break
                    time.sleep(3)
                if not after['ok']:
                    raise RuntimeError(json.dumps(after, ensure_ascii=False))
                if chapter == '12':
                    # Repeated checks exercise the already-populated PMTU cache.
                    for _ in range(2):
                        repeated = tutor.check(instance, chapter)
                        if not repeated['ok']:
                            raise RuntimeError('Cached PMTU regression: ' + json.dumps(repeated, ensure_ascii=False))
            print(json.dumps(dict(chapter=chapter, before=before, after=after,
                                  probe_count=len(probes)), ensure_ascii=False), flush=True)
        except Exception as exc:
            failures.append(chapter)
            print(json.dumps(dict(chapter=chapter, error=str(exc)), ensure_ascii=False), flush=True)
        finally:
            shell('unlab')
    if failures:
        raise SystemExit('Failed chapters: ' + ', '.join(failures))


if __name__ == '__main__':
    main()
