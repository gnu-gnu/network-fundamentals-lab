"""Chapter-aware guide retrieval with SQLite FTS5 and traceable source IDs."""
import json
from pathlib import Path
import re
import sqlite3
import threading

CORPUS = Path(__file__).with_name('knowledge') / 'corpus.json'
LOCK = threading.RLock()
INDEX = None
STAMP = None
DOCUMENTS = {}
REVISION = ''


def sections(text):
    lines = text.splitlines()
    title, start, block, fenced = '문서 소개', 1, [], False
    for number, line in enumerate(lines, 1):
        if line.startswith('```'):
            fenced = not fenced
        if not fenced and re.match(r'^#{1,4}\s+', line):
            if block:
                yield title, start, '\n'.join(block)
            title, start, block = line.lstrip('# ').strip(), number, []
        block.append(line)
        if len('\n'.join(block)) > 4000 and not fenced:
            yield title, start, '\n'.join(block)
            start, block = number + 1, []
    if block:
        yield title, start, '\n'.join(block)


def load():
    global INDEX, STAMP, DOCUMENTS, REVISION
    stat = CORPUS.stat()
    stamp = (str(CORPUS), stat.st_mtime_ns, stat.st_size)
    if STAMP == stamp:
        return
    corpus = json.loads(CORPUS.read_text())
    index = sqlite3.connect(':memory:', check_same_thread=False)
    index.row_factory = sqlite3.Row
    index.execute('CREATE VIRTUAL TABLE snippets USING fts5(doc UNINDEXED, chapter UNINDEXED, kind UNINDEXED, path UNINDEXED, title, line UNINDEXED, text)')
    for document in corpus['documents']:
        for title, line, text in sections(document['text']):
            index.execute('INSERT INTO snippets VALUES (?,?,?,?,?,?,?)',
                          (document['id'], document['chapter'], document['kind'], document['path'], title, line, text))
    if INDEX:
        INDEX.close()
    INDEX, STAMP = index, stamp
    DOCUMENTS = {d['id']: d for d in corpus['documents']}
    REVISION = corpus['revision']


def allow_solution(question):
    if re.search(r'(정답|해설|수정\s*명령|해결\s*방법).{0,15}(말하지|알려주지|보여주지|제외|없이|금지)|힌트만', question):
        return False
    return bool(re.search(r'정답|해설|해결\s*방법|수정\s*명령|구체|고치는\s*방법|명령.{0,8}(알려|보여)|walkthrough|solution', question, re.I))


def display_title(document):
    chapter = document.get('chapter', '')
    kind = document.get('kind')
    if kind == 'topology':
        return (chapter + '장 ' if chapter else '') + '참고 토폴로지'
    if kind == 'wiki':
        heading = next((line.lstrip('# ').strip() for line in document.get('text', '').splitlines() if line.startswith('# ')), '')
        return '개념 해설' + (' · ' + heading if heading else '')
    return (chapter + '장 ' if chapter else '') + ('실습 해설' if kind == 'solution' else '교재')


def relevant(document, chapter):
    if document['chapter'] != (chapter or '')[:2]:
        return False
    if chapter and chapter.startswith('17-') and document['kind'] == 'topology':
        return chapter.split('-', 1)[1] in document['path']
    return True


def retrieve(chapter, question, solution=False, limit=4, budget=14000, help_level=None):
    """Current guide + topology first, then relevant cross-chapter/wiki sections."""
    with LOCK:
        load()
        selected, used = [], 0

        def add(document, title, line, text, cap):
            nonlocal used
            if help_level is not None and help_level < 3:
                if document['kind'] == 'solution':
                    return
                if document['kind'] == 'topology':
                    # Comments often disclose the deliberately broken setting.
                    text = '\n'.join(row.split('#', 1)[0] for row in text.splitlines())
                else:
                    safe = []
                    for heading, _, block in sections(text):
                        if re.search(r'정답|해설|해결|수정|고장|fault|solution|walkthrough', heading, re.I):
                            continue
                        safe.extend(row for row in block.splitlines() if not re.search(
                            r'ip route (add|replace|del)|iptables .* (-A|-D|-F)|고장|정답|의도적|불일치', row))
                    text = '\n'.join(safe)
                text = '\n'.join(row for row in text.splitlines() if not re.search(
                    r'ip route (add|replace|del)|iptables .* (-A|-D|-F)|fix\.sh', row))
            cap = min(cap, budget - used)
            if cap < 150:
                return
            if any(s['document'] == document['id'] and (text in s['text'] or document['kind'] == 'topology') for s in selected):
                return
            chunk = text[:cap]
            selected.append(dict(citation='G' + str(len(selected) + 1), document=document['id'],
                                 path=document['path'], section=title, line=line,
                                 kind=document['kind'], sha256=document['sha256'],
                                 text=chunk, truncated=len(text) > cap, revision=REVISION))
            used += len(chunk)

        for kind, cap in (('guide', min(4000, budget // 3)), ('topology', min(2500, budget // 5))):
            for doc in DOCUMENTS.values():
                if doc['kind'] == kind and relevant(doc, chapter):
                    add(doc, '현재 장 원문' if kind == 'guide' else '참고 토폴로지(현재 상태 아님)', 1, doc['text'], cap)
        # Split adjacent Latin/Korean terms, retaining exact node names and addresses.
        query = re.sub(r'([A-Za-z0-9])([가-힣])|([가-힣])([A-Za-z0-9])', r'\1\3 \2\4', question)
        tokens = re.findall(r'[\w]+', query, re.UNICODE)
        generic = ('현재', '지금', '방금', '상태', '실습', '설명', '문장', '근거', '관찰', '알려', '정리', '이유', '어떻게', '무엇', '뭘', '될까', '정도', '고쳤')
        tokens = [t for t in tokens if not any(t.startswith(word) for word in generic)]
        for needle, aliases in (
            (r'ARP|MAC|이웃', ['ARP', 'MAC']),
            (r'왕복|돌아오|리턴|라우팅', ['라우팅', '왕복', 'route', 'return']),
            (r'MTU|큰\s*패킷|단편|터널', ['MTU', 'PMTUD', 'VXLAN']),
            (r'캐시|DNS|도메인', ['DNS', 'TTL', '캐시']),
            (r'NAT|주소\s*변환|헤어핀', ['NAT', 'SNAT', 'DNAT', '헤어핀'])):
            if re.search(needle, question, re.I):
                tokens.extend(aliases)
        terms = list(dict.fromkeys(t for t in tokens if len(t) >= 2))[:32]
        current = next((d for d in DOCUMENTS.values() if d['kind'] == 'guide' and relevant(d, chapter)), None)
        linked_wikis = [d for d in DOCUMENTS.values() if d['kind'] == 'wiki' and current and current['path'] in d['text']]
        for linked_wiki in linked_wikis[:2]:
            add(linked_wiki, '현재 장과 연결된 개념 Wiki', 1, linked_wiki['text'], 2000)
        if terms:
            match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
            rows = INDEX.execute('SELECT *,bm25(snippets) AS rank FROM snippets WHERE snippets MATCH ? AND (? OR kind != ?) ORDER BY rank LIMIT 80',
                                 (match, int(solution), 'solution')).fetchall()
            requested_chapters = set(re.findall(r'\b(\d{2})\s*장', question))
            for pattern, chapters in (
                (r'ARP|이웃\s*테이블', {'01'}), (r'VLAN|트렁크', {'03'}),
                (r'왕복|리턴|라우팅', {'04', '05', '06', '17'}),
                (r'VXLAN', {'07', '13'}), (r'MTU|PMTUD|단편', {'12', '13', '15'}),
                (r'DNS|도메인|캐시', {'14'}), (r'NAT|SNAT|DNAT|헤어핀', {'09', '10', '11', '16'}),
                (r'OSPF|BGP', {'17'})):
                if re.search(pattern, question, re.I):
                    requested_chapters.update(chapters)
            if chapter:
                # Shared node names (h1/r1) must not pull unrelated lab scenarios.
                permitted = requested_chapters | {chapter[:2]}
                rows = [r for r in rows if r['kind'] == 'wiki' or r['chapter'] in permitted]
            rows = sorted(rows, key=lambda r: (0 if r['chapter'] == (chapter or '')[:2] else 1,
                                               r['rank']))
            wiki = next((r for r in sorted(rows, key=lambda r: r['rank']) if r['kind'] == 'wiki'), None)
            if wiki:
                rows = [wiki] + [r for r in rows if r is not wiki]
            count = min(2, len(linked_wikis))
            for row in rows:
                doc = DOCUMENTS[row['doc']]
                if chapter and chapter.startswith('17-') and doc['kind'] == 'topology' and not relevant(doc, chapter):
                    continue
                before = len(selected)
                add(doc, row['title'], row['line'], row['text'], 2800)
                count += len(selected) - before
                if count >= limit:
                    break
        return selected


def source(identifier):
    with LOCK:
        load()
        return DOCUMENTS.get(identifier)


def source_by_path(path):
    with LOCK:
        load()
        return next((document for document in DOCUMENTS.values() if document['path'] == path), None)
