#!/usr/bin/env python3
"""Build a deterministic, source-versioned corpus from repository-owned guides."""
import argparse
import hashlib
import json
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parent
OUTPUT = SERVER / 'knowledge/corpus.json'


def build():
    reviewed = json.loads((SERVER / 'knowledge/wiki/reviewed-against.json').read_text())
    for wiki in (SERVER / 'knowledge/wiki').glob('*.md'):
        links = re.findall(r'\]\(\.\./\.\./\.\./([^\)]+)\)', wiki.read_text())
        baseline = reviewed.get(wiki.name, {})
        if set(links) != set(baseline):
            raise RuntimeError('Wiki review coverage changed: ' + wiki.name)
        for source, expected in baseline.items():
            if hashlib.sha256((ROOT / source).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Re-review Wiki after guide changes: ' + wiki.name + ': ' + source)
    paths = []
    for directory in sorted(ROOT.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or not directory.name[:2].isdigit():
            continue
        if not 0 <= int(directory.name[:2]) <= 17:
            continue
        paths.extend(p for p in sorted(directory.iterdir())
                     if p.name in ('README.md', 'WALKTHROUGH.md') or p.name.endswith('.clab.yml'))
    paths.extend(sorted((SERVER / 'knowledge/wiki').glob('*.md')))
    documents = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError('Knowledge sources must be regular files: ' + str(path))
        text = path.read_text()
        relative = path.relative_to(ROOT).as_posix()
        digest = hashlib.sha256(text.encode()).hexdigest()
        wiki = relative.startswith('server/knowledge/wiki/')
        kind = 'wiki' if wiki else 'solution' if path.name == 'WALKTHROUGH.md' else 'topology' if path.name.endswith('.clab.yml') else 'guide'
        documents.append(dict(id=hashlib.sha256(relative.encode()).hexdigest()[:24],
                              chapter='' if wiki else path.parent.name[:2], kind=kind,
                              path=relative, sha256=digest, text=text))
    version = hashlib.sha256(json.dumps(documents, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return dict(version=1, revision=version, documents=documents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = build()
    if args.check:
        if not OUTPUT.is_file() or json.loads(OUTPUT.read_text()) != expected:
            raise SystemExit('Knowledge corpus is stale; run python3 server/build_knowledge.py')
        print('Knowledge corpus matches source guides')
    else:
        OUTPUT.parent.mkdir(exist_ok=True)
        OUTPUT.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + '\n')
        print(f'Built {len(expected["documents"])} documents: {OUTPUT}')


if __name__ == '__main__':
    main()
