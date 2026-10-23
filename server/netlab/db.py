"""SQLite 연결 컨텍스트. 채팅·공지·튜터가 같은 규칙(0600, 즉시 잠금, 자동 커밋/롤백)을 쓴다."""
from contextlib import contextmanager
import os
import sqlite3


@contextmanager
def connection(path, schema, immediate=True, migrate=None):
    """
    path    : DB 파일. 상위 디렉터리는 0700 으로 만든다.
    schema  : CREATE ... IF NOT EXISTS 문들 (executescript 로 실행)
    immediate: True 면 BEGIN IMMEDIATE 로 쓰기 잠금을 먼저 잡는다
    migrate : 연결 직후 호출되는 함수(db) — 컬럼 추가 등 스키마 보정
    """
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript(schema)
    if migrate:
        migrate(db)
    try:
        if immediate:
            db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def add_column_if_missing(db, table, column, definition):
    if column not in [r[1] for r in db.execute("PRAGMA table_info(" + table + ")")]:
        db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
