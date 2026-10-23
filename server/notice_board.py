"""Latest-only notice with per-reader receipts. Identity is supplied only by labd, not JSON."""
import re
import time
from netlab import db as _db
from netlab.paths import NOTICE_DB as DB

TOKEN = re.compile(r'^[a-f0-9]{32}$')
KEEP_NOTICES = 50
KEEP_READS = 2000
SCHEMA = '''
  CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
    author TEXT NOT NULL, created REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS reads (
    notice INTEGER NOT NULL, reader TEXT NOT NULL, read_at REAL NOT NULL,
    PRIMARY KEY(notice,reader));
'''


def connection():
    return _db.connection(DB, SCHEMA)


def reader(owner, client):
    # Guests share one account; the browser token keeps their receipts apart.
    if owner != 'guest':
        return owner
    if not isinstance(client, str) or not TOKEN.fullmatch(client):
        raise ValueError('브라우저 식별자가 올바르지 않습니다.')
    return 'guest-' + client[:6]


def text(value, label, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f'{label}은 1~{limit}자로 입력하세요.')
    if any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(f'{label}에 허용되지 않는 문자가 있습니다.')
    return value.strip()


def latest(db):
    row = db.execute('SELECT * FROM notices ORDER BY id DESC LIMIT 1').fetchone()
    return dict(id=row['id'], title=row['title'], body=row['body'], created=row['created']) if row else None


def view(db, who):
    notice = latest(db)
    if not notice:
        return dict(ok=True, notice=None, read=False, read_at=None)
    row = db.execute('SELECT read_at FROM reads WHERE notice=? AND reader=?', (notice['id'], who)).fetchone()
    return dict(ok=True, notice=notice, read=bool(row), read_at=row['read_at'] if row else None)


def handle(owner, body):
    if not isinstance(body, dict):
        raise ValueError('잘못된 요청입니다.')
    action = body.get('action', 'get')
    if action not in ('get', 'read'):
        raise ValueError('지원하지 않는 동작입니다.')
    who = reader(owner, body.get('client'))
    with connection() as db:
        if action == 'read':
            notice = latest(db)
            if not notice or body.get('id') != notice['id']:
                raise ValueError('공지가 바뀌었습니다. 새로 고침 후 다시 확인하세요.')
            db.execute('INSERT OR IGNORE INTO reads VALUES (?,?,?)', (notice['id'], who, time.time()))
            db.execute('DELETE FROM reads WHERE notice=? AND rowid NOT IN '
                       '(SELECT rowid FROM reads WHERE notice=? ORDER BY read_at DESC LIMIT ?)',
                       (notice['id'], notice['id'], KEEP_READS))
        return view(db, who)


def publish(author, body):
    if not isinstance(body, dict):
        raise ValueError('잘못된 요청입니다.')
    title = text(body.get('title'), '제목', 100)
    content = text(body.get('body'), '내용', 5000)
    with connection() as db:
        db.execute('INSERT INTO notices(title,body,author,created) VALUES (?,?,?,?)',
                   (title, content, author, time.time()))
        db.execute('DELETE FROM notices WHERE id NOT IN (SELECT id FROM notices ORDER BY id DESC LIMIT ?)',
                   (KEEP_NOTICES,))
        db.execute('DELETE FROM reads WHERE notice NOT IN (SELECT id FROM notices)')
    return status([])


def status(accounts):
    with connection() as db:
        notice = latest(db)
        if not notice:
            return dict(ok=True, notice=None, readers=[], unread=[])
        rows = db.execute('SELECT reader, read_at FROM reads WHERE notice=? ORDER BY read_at',
                          (notice['id'],)).fetchall()
    readers = [dict(reader=r['reader'], read_at=r['read_at']) for r in rows]
    seen = {r['reader'] for r in readers}
    return dict(ok=True, notice=notice, readers=readers,
                unread=sorted(a for a in accounts if a != 'guest' and a not in seen))
