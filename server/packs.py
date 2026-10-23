"""Compiled learning packs: the only way lesson metadata enters the server at runtime.

`knowledge/packs.json` is produced by build_knowledge.py from pack.toml / lesson.toml.
Lesson ids are global; `chapter` in the HTTP API and the tutor DB carries a lesson id.
"""
import json
from pathlib import Path

PACKS_FILE = Path(__file__).with_name('knowledge') / 'packs.json'


def _load():
    if not PACKS_FILE.is_file():
        return dict(packs=[], lessons=[], revision='')
    return json.loads(PACKS_FILE.read_text())


_DATA = _load()
PACKS = {pack['id']: pack for pack in _DATA['packs']}
LESSONS = {lesson['id']: lesson for lesson in _DATA['lessons']}
REVISION = _DATA['revision']


def lesson(identifier):
    return LESSONS.get(identifier) if isinstance(identifier, str) else None


def pack_of(lesson):
    return PACKS[lesson['pack']]


def graded(lesson):
    """Only fault-repair lessons have a pass/fail condition."""
    return lesson['assessment'] == 'fault'


def by_definition(path):
    """Lesson whose definition file (e.g. a topology) is `path`; absolute VM paths match by suffix."""
    for item in LESSONS.values():
        definition = item.get('definition')
        if definition and (path == definition or path.endswith('/' + definition)):
            return item
    return None


def by_number(number):
    return [item for item in LESSONS.values() if item['number'] == number]


def redact_patterns(lesson=None):
    packs = [pack_of(lesson)] if lesson else PACKS.values()
    return [pattern for pack in packs for pattern in pack['tutor']['redact']]


def synonyms(lesson=None):
    packs = [pack_of(lesson)] if lesson else PACKS.values()
    return [(item['match'], item['terms']) for pack in packs for item in pack['tutor']['synonyms']]
