"""Small authenticated common room. Identity is supplied only by labd, not JSON."""
import re
import time
from netlab import db as _db
from netlab.paths import COMMUNITY_DB as DB

TOKEN = re.compile(r'^[a-f0-9]{32}$')
SCHEMA = '''
  CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, client TEXT NOT NULL,
    request TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,
    UNIQUE(owner,client,request));
  CREATE TABLE IF NOT EXISTS presence (
    owner TEXT NOT NULL, client TEXT NOT NULL, seen REAL NOT NULL,
    PRIMARY KEY(owner,client));
  CREATE TABLE IF NOT EXISTS limits (
    scope TEXT NOT NULL, period INTEGER NOT NULL, bucket INTEGER NOT NULL,
    count INTEGER NOT NULL, PRIMARY KEY(scope,period,bucket));
'''


def connection():
    return _db.connection(DB, SCHEMA)


def name(owner, client):
    return 'guest-' + client[:6] if owner == 'guest' else owner


def handle(owner, body, track_presence=True):
    if not isinstance(body, dict):
        raise ValueError('잘못된 요청입니다.')
    client, action = body.get('client'), body.get('action', 'poll')
    if not isinstance(client, str) or not TOKEN.fullmatch(client):
        raise ValueError('브라우저 식별자가 올바르지 않습니다.')
    if action not in ('poll', 'send'):
        raise ValueError('지원하지 않는 동작입니다.')
    now = time.time()
    with connection() as db:
        db.execute('DELETE FROM presence WHERE seen<?', (now - 90,))
        db.execute('DELETE FROM limits WHERE bucket*period<?', (now - 120,))
        if action == 'send':
            text, request = body.get('text'), body.get('request')
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
                raise ValueError('메시지는 1~1000자로 입력하세요.')
            if any(ord(c) < 32 and c not in '\n\t' for c in text):
                raise ValueError('메시지에 허용되지 않는 문자가 있습니다.')
            if not isinstance(request, str) or not TOKEN.fullmatch(request):
                raise ValueError('요청 식별자가 올바르지 않습니다.')
            old = db.execute('SELECT text FROM messages WHERE owner=? AND client=? AND request=?',
                             (owner, client, request)).fetchone()
            if old and old['text'] != text.strip():
                raise ValueError('이미 사용한 요청 식별자입니다.')
            if not old:
                scope = owner + (':' + client if owner == 'guest' else '')
                for key, period, maximum in ((scope, 10, 5), (scope, 60, 30), ('*', 60, 120)):
                    bucket = int(now // period)
                    count = db.execute('SELECT count FROM limits WHERE scope=? AND period=? AND bucket=?',
                                       (key, period, bucket)).fetchone()
                    if count and count[0] >= maximum:
                        raise ValueError('메시지가 너무 많습니다. 잠시 후 다시 보내세요.')
                    db.execute('INSERT INTO limits VALUES (?,?,?,1) ON CONFLICT(scope,period,bucket) DO UPDATE SET count=count+1',
                               (key, period, bucket))
                db.execute('INSERT INTO messages(owner,client,request,text,created) VALUES (?,?,?,?,?)',
                           (owner, client, request, text.strip(), now))
                db.execute('DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT 100)')
        # Bound anonymous presence too; poll traffic never causes unbounded growth.
        if track_presence:
            db.execute('INSERT INTO presence VALUES (?,?,?) ON CONFLICT(owner,client) DO UPDATE SET seen=excluded.seen',
                       (owner, client, now))
        db.execute('DELETE FROM presence WHERE rowid NOT IN (SELECT rowid FROM presence ORDER BY seen DESC LIMIT 200)')
        rows = db.execute('SELECT * FROM messages ORDER BY id').fetchall()
        online = sorted({name(r['owner'], r['client']) for r in db.execute('SELECT owner,client FROM presence')})
        receipt = None
        if action == 'send':
            saved = db.execute('SELECT * FROM messages WHERE owner=? AND client=? AND request=?', (owner, client, request)).fetchone()
            if saved:
                receipt = dict(id=saved['id'], author=name(saved['owner'], saved['client']), text=saved['text'], created=saved['created'])
        return dict(ok=True, me=name(owner, client), online=online, receipt=receipt,
                    messages=[dict(id=r['id'], author=name(r['owner'], r['client']),
                                   text=r['text'], created=r['created']) for r in rows])
