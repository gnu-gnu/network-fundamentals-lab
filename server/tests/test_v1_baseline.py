"""v1 lesson semantics are frozen while learning pack v2 is introduced (procedure "착수와 기준선").

The fixture was taken from main e7aa862. A change here means a v1 check, fallback, probe or guide
mapping moved: update the fixture only together with a deliberate content change."""
import json
from pathlib import Path
import sys
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import packs  # noqa: E402

KEEP = ('id', 'pack', 'number', 'dir', 'title', 'assessment', 'checks', 'probes', 'definition', 'prefix', 'topics')


class V1BaselineTests(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads((SERVER / 'tests/fixtures/v1_lessons_baseline.json').read_text())

    def test_nineteen_lessons_keep_their_ids_checks_fallbacks_and_probes(self):
        current = [{k: l.get(k) for k in KEEP} for l in packs.LESSONS.values()]
        self.assertEqual(len(current), 19)
        self.assertEqual(current, self.baseline['lessons'])

    def test_guides_and_aliases(self):
        guide = packs.PACKS['network']['files']['guide']
        for lesson_id, path in self.baseline['guides'].items():
            self.assertEqual(packs.LESSONS[lesson_id]['dir'] + '/' + guide, path)
            self.assertEqual(packs.canonical(lesson_id), 'network/' + lesson_id)
            self.assertIs(packs.lesson('network/' + lesson_id), packs.LESSONS[lesson_id])
        self.assertIsNone(packs.canonical('other/12'))
        self.assertIsNone(packs.lesson('network/99'))


if __name__ == '__main__':
    unittest.main()
