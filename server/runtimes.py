"""Runtime adapters: how a pack's lessons run inside the learner VM.

Every adapter exposes the same five operations to the tutor:
  compile(lesson, directory)          build time: validate runtime keys, derive names
  current(lxc, instance)              lesson id now running (newest first) or None
  exec(lxc, instance, lesson, node, argv, timeout)
  context(lxc, instance, lesson)      stable string describing the running deployment
The host only ever runs fixed argv through `lxc exec`; adapters never build shell strings.
"""
import json
import posixpath
import re
import packs


class Containerlab:
    name = 'containerlab'
    NAME_RE = re.compile(r'^name:\s*(\S+)', re.M)
    LEGACY_RE = re.compile(r'^clab-(\d{2})-')
    PS_FORMAT = '{{.CreatedAt}}\t{{.Names}}\t{{.Label "clab-topo-file"}}'

    @classmethod
    def compile(cls, lesson, directory):
        topology = lesson.get('topology')
        if not isinstance(topology, str) or not (directory / topology).is_file():
            raise ValueError(f"{lesson['id']}: topology file missing: {topology}")
        match = cls.NAME_RE.search((directory / topology).read_text())
        if not match:
            raise ValueError(f"{lesson['id']}: topology has no name: {topology}")
        lesson['definition'] = posixpath.join(lesson['dir'], topology)
        lesson['prefix'] = 'clab-' + match.group(1)

    @staticmethod
    def exec(lxc, instance, lesson, node, argv, timeout):
        if node is None:
            return lxc(instance, list(argv), timeout)
        return lxc(instance, ['docker', 'exec', lesson['prefix'] + '-' + node, *argv], timeout)

    @classmethod
    def current(cls, lxc, instance):
        """Newest container wins; the clab-topo-file label beats name matching."""
        rc, out, _ = lxc(instance, ['docker', 'ps', '--format', cls.PS_FORMAT], 20)
        if rc != 0:
            return None
        rows = []
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) == 1:
                parts = ['', parts[0]]
            created, name, topology = (parts + ['', ''])[:3]
            rows.append((created, name.strip(), topology.strip()))
        for _, name, topology in sorted(rows, key=lambda row: row[0], reverse=True):
            found = packs.by_definition(topology) if topology else None
            if found:
                return found['id']
            for item in packs.LESSONS.values():
                if item.get('prefix') and name.startswith(item['prefix'] + '-'):
                    return item['id']
            match = cls.LEGACY_RE.match(name)
            if match:
                return match.group(1)     # 메타데이터 없는 실습이라도 번호는 돌려준다
        return None

    @staticmethod
    def context(lxc, instance, lesson):
        rc, names, _ = lxc(instance, ['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], 20)
        if rc != 0:
            return 'unknown'
        lines = names.splitlines()
        if lesson:
            lines = [line for line in lines
                     if any(name.startswith(lesson['prefix'] + '-') for name in line.split())]
        return json.dumps(sorted(lines))


ADAPTERS = {Containerlab.name: Containerlab}


def get(name):
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(f'Unknown runtime: {name}') from None
