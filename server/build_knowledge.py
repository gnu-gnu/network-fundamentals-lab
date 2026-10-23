#!/usr/bin/env python3
"""Compile learning packs into deterministic, source-versioned server data.

pack.toml (repo root or packs/*/) + <lesson dir>/lesson.toml
    -> knowledge/packs.json   lesson metadata the tutor runs on
    -> knowledge/corpus.json  guide/solution/definition/wiki text with per-document SHA256
"""
import argparse
import fnmatch
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
import runtimes  # noqa: E402

CORPUS = SERVER / 'knowledge/corpus.json'
PACKS = SERVER / 'knowledge/packs.json'
CATALOG_V2 = SERVER / 'knowledge/v2/catalog.json'   # learning pack v2 (pack_format, M1 pilot lessons)
ASSESSMENTS = ('fault', 'observe', 'exploration')
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')


def fail(where, message):
    raise RuntimeError(f'{where}: {message}')


def text_list(value, where, name):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        fail(where, f'{name} must be a list of strings')
    return list(value)


def manifests(root=None):
    """v1 pack manifests. Format-2 packs (v2-native) are compiled by pack_format only."""
    root = root or ROOT
    found = [root / 'pack.toml'] + sorted((root / 'packs').glob('*/pack.toml'))
    return [p for p in found if p.is_file() and not p.is_symlink()
            and tomllib.loads(p.read_text()).get('format') != 2]


def load_pack(manifest, root=None):
    root = root or ROOT
    where = manifest.relative_to(root).as_posix()
    data = tomllib.loads(manifest.read_text())
    for key in ('id', 'title', 'runtime'):
        if not isinstance(data.get(key), str) or not data[key]:
            fail(where, f'missing {key}')
    if not ID_RE.match(data['id']):
        fail(where, 'invalid pack id')
    runtimes.get(data['runtime'])
    files = data.get('files', {})
    tutor = data.get('tutor', {})
    synonyms = []
    for item in tutor.get('synonyms', []):
        re.compile(item['match'])
        synonyms.append(dict(match=item['match'], terms=text_list(item['terms'], where, 'synonym terms')))
    for pattern in tutor.get('redact', []):
        re.compile(pattern)
    base = manifest.parent.relative_to(root).as_posix()
    return dict(id=data['id'], title=data['title'], runtime=data['runtime'],
                order=int(data.get('order', 100)), root='' if base == '.' else base,
                files=dict(guide=files.get('guide', 'README.md'),
                           solution=files.get('solution', 'WALKTHROUGH.md'),
                           definition=text_list(files.get('definition', []), where, 'files.definition'),
                           definition_label=files.get('definition_label', '정의 파일'),
                           wiki=files.get('wiki')),
                tutor=dict(redact=text_list(tutor.get('redact', []), where, 'tutor.redact'),
                           synonyms=synonyms))


def check_item(item, where, nested=False):
    if not isinstance(item, dict):
        fail(where, 'check must be a table')
    node, argv, expect = item.get('node'), item.get('argv'), item.get('expect')
    if not isinstance(node, str) or not node:
        fail(where, 'check needs a node')
    argv = text_list(argv, where, 'check argv')
    if not isinstance(expect, dict) or sum(k in expect for k in ('contains', 'regex')) != 1:
        fail(where, 'check expect needs exactly one of contains/regex')
    if 'regex' in expect:
        re.compile(expect['regex'])
    out = dict(node=node, argv=argv, timeout=int(item.get('timeout', 30)),
               expect={k: expect[k] for k in ('contains', 'regex', 'stream', 'rc') if k in expect})
    if out['expect'].get('stream', 'both') not in ('both', 'stdout'):
        fail(where, 'expect.stream must be both or stdout')
    if 'rc' in out['expect'] and not all(isinstance(v, int) for v in out['expect']['rc']):
        fail(where, 'expect.rc must be integers')
    if 'label' in item:
        out['label'] = str(item['label'])
    if 'fallback' in item:
        if nested:
            fail(where, 'fallback cannot nest')
        out['fallback'] = check_item(item['fallback'], where, True)
    return out


def probe_item(item, where):
    if not isinstance(item, dict) or not isinstance(item.get('label'), str):
        fail(where, 'probe needs a label')
    node = item.get('node')
    if node is not None and (not isinstance(node, str) or not node):
        fail(where, 'probe node must be a string or omitted')
    return dict(label=item['label'], node=node, argv=text_list(item.get('argv'), where, 'probe argv'))


def load_lessons(pack, directory, seen, root=None):
    root = root or ROOT
    where = (directory / 'lesson.toml').relative_to(root).as_posix()
    data = tomllib.loads((directory / 'lesson.toml').read_text())
    entries = data.pop('lessons', None)
    if entries is not None and (not isinstance(entries, list) or not entries):
        fail(where, 'lessons must be a non-empty list')
    raw = [dict(data, **entry) for entry in entries] if entries else [data]
    lessons = []
    for item in raw:
        lesson_id = item.get('id')
        if not isinstance(lesson_id, str) or not ID_RE.match(lesson_id):
            fail(where, f'invalid lesson id: {lesson_id!r}')
        if lesson_id in seen:
            fail(where, f'duplicate lesson id: {lesson_id}')
        seen.add(lesson_id)
        for key in ('title', 'concept', 'goal'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                fail(where, f'{lesson_id}: missing {key}')
        assessment = item.get('assessment', 'fault')
        if assessment not in ASSESSMENTS:
            fail(where, f'{lesson_id}: assessment must be one of {ASSESSMENTS}')
        checks = [check_item(c, where) for c in item.get('checks', [])]
        if assessment == 'fault' and (not checks or not isinstance(item.get('fault'), str)):
            fail(where, f'{lesson_id}: fault lessons need a fault description and checks')
        number = item.get('number', directory.name[:2] if directory.name[:2].isdigit() else '')
        lesson = dict(id=lesson_id, pack=pack['id'], runtime=pack['runtime'], number=str(number),
                      dir=directory.relative_to(root).as_posix(), title=item['title'],
                      concept=item['concept'], goal=item['goal'], fault=item.get('fault', ''),
                      assessment=assessment, topics=text_list(item.get('topics', []), where, 'topics'),
                      hints=text_list(item.get('hints', []), where, 'hints'), checks=checks,
                      probes=[probe_item(p, where) for p in item.get('probes', [])])
        for topic in lesson['topics']:
            re.compile(topic)
        for key in ('topology',):
            if key in item:
                lesson[key] = item[key]
        runtimes.get(pack['runtime']).compile(lesson, directory)
        if not (directory / pack['files']['guide']).is_file():
            fail(where, f"{lesson_id}: guide missing: {pack['files']['guide']}")
        lessons.append(lesson)
    return lessons


def load_all(root=None):
    root = root or ROOT
    packs, lessons, seen = [], [], set()
    for manifest in manifests(root):
        pack = load_pack(manifest, root)
        if any(p['id'] == pack['id'] for p in packs):
            fail(manifest.relative_to(root).as_posix(), 'duplicate pack id')
        packs.append(pack)
        for lesson_file in sorted(manifest.parent.glob('*/lesson.toml')):
            if lesson_file.is_symlink() or lesson_file.parent.is_symlink():
                fail(lesson_file.relative_to(root).as_posix(), 'symlinks are not allowed')
            lessons.extend(load_lessons(pack, lesson_file.parent, seen, root))
    packs.sort(key=lambda p: (p['order'], p['id']))
    return packs, lessons


def document(path, pack, kind, number):
    if not path.is_file() or path.is_symlink():
        fail(path.relative_to(ROOT).as_posix(), 'knowledge sources must be regular files')
    text = path.read_text()
    relative = path.relative_to(ROOT).as_posix()
    directory = '' if kind == 'wiki' else path.parent.relative_to(ROOT).as_posix()
    return dict(id=hashlib.sha256(relative.encode()).hexdigest()[:24], pack=pack['id'], dir=directory,
                number=number, kind=kind, path=relative,
                sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)


def check_wiki(pack):
    wiki = ROOT / pack['files']['wiki']
    reviewed = json.loads((wiki / 'reviewed-against.json').read_text())
    for page in sorted(wiki.glob('*.md')):
        links = re.findall(r'\]\(\.\./\.\./\.\./([^\)]+)\)', page.read_text())
        baseline = reviewed.get(page.name, {})
        if set(links) != set(baseline):
            raise RuntimeError('Wiki review coverage changed: ' + page.name)
        for source, expected in baseline.items():
            if hashlib.sha256((ROOT / source).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Re-review Wiki after guide changes: ' + page.name + ': ' + source)


def build():
    packs, lessons = load_all()
    documents = []
    for pack in packs:
        directories = []
        for lesson in lessons:
            if lesson['pack'] == pack['id'] and lesson['dir'] not in directories:
                directories.append(lesson['dir'])
        files = pack['files']
        for directory in directories:
            numbers = {l['number'] for l in lessons if l['dir'] == directory}
            number = numbers.pop() if len(numbers) == 1 else ''
            for path in sorted((ROOT / directory).iterdir()):
                if path.name == files['guide']:
                    kind = 'guide'
                elif path.name == files['solution']:
                    kind = 'solution'
                elif any(fnmatch.fnmatch(path.name, pattern) for pattern in files['definition']):
                    kind = 'definition'
                else:
                    continue
                documents.append(document(path, pack, kind, number))
        if files['wiki']:
            check_wiki(pack)
            for path in sorted((ROOT / files['wiki']).glob('*.md')):
                documents.append(document(path, pack, 'wiki', ''))
    revision = hashlib.sha256(json.dumps(documents, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    pack_revision = hashlib.sha256(json.dumps([packs, lessons], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return (dict(version=1, revision=pack_revision, packs=packs, lessons=lessons),
            dict(version=1, revision=revision, documents=documents))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected_packs, expected_corpus = build()
    import pack_format
    expected_catalog = pack_format.build(ROOT)
    outputs = ((PACKS, expected_packs), (CORPUS, expected_corpus), (CATALOG_V2, expected_catalog))
    stale = [str(p) for p, expected in outputs
             if not p.is_file() or json.loads(p.read_text()) != expected]
    if args.check:
        if stale:
            raise SystemExit('Stale: ' + ', '.join(stale) + '; run python3 server/build_knowledge.py')
        print('Packs and knowledge corpus match source')
    else:
        PACKS.parent.mkdir(exist_ok=True)
        PACKS.write_text(json.dumps(expected_packs, ensure_ascii=False, indent=2) + '\n')
        CORPUS.write_text(json.dumps(expected_corpus, ensure_ascii=False, indent=2) + '\n')
        # Write next to the target and rename, so a reader never sees a half-written catalog.
        CATALOG_V2.parent.mkdir(exist_ok=True)
        staging = CATALOG_V2.with_suffix('.json.tmp')
        staging.write_text(json.dumps(expected_catalog, ensure_ascii=False, indent=2) + '\n')
        staging.replace(CATALOG_V2)
        print(f'Built {len(expected_packs["lessons"])} lessons in {len(expected_packs["packs"])} pack(s), '
              f'{len(expected_corpus["documents"])} documents, {len(expected_catalog["lessons"])} v2 lesson(s)')


if __name__ == '__main__':
    main()
