"""Learning-pack contract: compiled data is complete, and the tutor reads lessons only through it."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import build_knowledge  # noqa: E402
import netlab_tutor as tutor  # noqa: E402
import packs  # noqa: E402
import runtimes  # noqa: E402

ROOT = Path(__file__).parents[2]


class PackDataTests(unittest.TestCase):
    def test_every_lesson_is_complete_and_unique(self):
        self.assertTrue(packs.LESSONS)
        self.assertEqual(len({l['id'] for l in packs.LESSONS.values()}), len(packs.LESSONS))
        for lesson in packs.LESSONS.values():
            pack = packs.pack_of(lesson)
            runtimes.get(lesson['runtime'])
            self.assertTrue((ROOT / lesson['dir'] / pack['files']['guide']).is_file(), lesson['id'])
            self.assertTrue(lesson['concept'] and lesson['goal'] and lesson['title'])
            if packs.graded(lesson):
                self.assertTrue(lesson['fault'] and lesson['checks'], lesson['id'])
            for check in lesson['checks']:
                self.assertTrue(check['node'] and check['argv'])
                self.assertEqual(sum(k in check['expect'] for k in ('contains', 'regex')), 1)
            for probe in lesson['probes']:
                self.assertTrue(probe['label'] and probe['argv'])

    def test_manifest_errors_are_reported_not_compiled(self):
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            build_knowledge.check_item({'node': 'h1', 'argv': ['true'], 'expect': {}}, 'fixture')
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            build_knowledge.check_item({'node': 'h1', 'argv': ['true'], 'expect': {'contains': 'a', 'regex': 'b'}}, 'fixture')
        with self.assertRaisesRegex(RuntimeError, 'cannot nest'):
            build_knowledge.check_item({'node': 'h1', 'argv': ['true'], 'expect': {'contains': 'a'},
                                        'fallback': {'node': 'h1', 'argv': ['true'], 'expect': {'contains': 'a'},
                                                     'fallback': {}}}, 'fixture')
        with self.assertRaisesRegex(ValueError, 'Unknown runtime'):
            runtimes.get('kubernetes')


class RuntimeDetectionTests(unittest.TestCase):
    def test_topology_label_identifies_the_lesson_before_names(self):
        listing = ('2026-10-22 08:00:00 +0900 KST\tclab-01-l2-arp-h1\t/home/lab/network-lab/01-l2-arp/01-l2-arp.clab.yml\n'
                   '2026-10-22 09:00:00 +0900 KST\tclab-17-bgp-r1\t/home/lab/network-lab/17-dynamic-routing/bgp.clab.yml')
        with patch.object(tutor, 'lxc_exec', return_value=(0, listing, '')):
            self.assertEqual(tutor.deployed_chapter('fixture'), '17-bgp')

    def test_unknown_lab_still_reports_its_number(self):
        with patch.object(tutor, 'lxc_exec', return_value=(0, '2026-10-22 08:00:00 +0900 KST\tclab-42-future-h1\t/tmp/x.clab.yml', '')):
            self.assertEqual(tutor.deployed_chapter('fixture'), '42')
        with patch.object(tutor, 'lxc_exec', return_value=(1, '', 'boom')):
            self.assertIsNone(tutor.deployed_chapter('fixture'))

    def test_context_is_scoped_to_the_running_lesson(self):
        listing = 'a clab-14-dns-h1\nb clab-04-l3-routing-r1\nc clab-14-dns-srv1'
        with patch.object(tutor, 'lxc_exec', return_value=(0, listing, '')):
            self.assertEqual(tutor.deployment_context('fixture', '14'), '["a clab-14-dns-h1", "c clab-14-dns-srv1"]')
            self.assertEqual(tutor.deployment_context('fixture', None), '["a clab-14-dns-h1", "b clab-04-l3-routing-r1", "c clab-14-dns-srv1"]')
        with patch.object(tutor, 'lxc_exec', return_value=(1, '', '')):
            self.assertEqual(tutor.deployment_context('fixture', '14'), 'unknown')

    def test_node_exec_goes_through_the_lesson_runtime(self):
        with patch.object(tutor, 'lxc_exec', return_value=(0, 'ok', '')) as lxc:
            tutor.node_exec('vm1', packs.lesson('04'), 'r2', ['ip', 'route'], 9)
            lxc.assert_called_once_with('vm1', ['docker', 'exec', 'clab-04-l3-routing-r2', 'ip', 'route'], 9)
            tutor.node_exec('vm1', packs.lesson('04'), None, ['uptime'], 5)
            self.assertEqual(lxc.call_args.args, ('vm1', ['uptime'], 5))


class CheckEvaluationTests(unittest.TestCase):
    def test_fallback_output_is_part_of_the_detail(self):
        with patch.object(tutor, 'node_exec', side_effect=[(1, '', 'ping: sendmsg: Message too large'),
                                                           (0, '10.12.2.10 via 10.12.1.1 cache expires 500sec mtu 1400', '')]):
            result = tutor.check('fixture', '12')
        self.assertTrue(result['ok'])
        self.assertIn('[PMTU route cache]', result['conditions'][0]['detail'])

    def test_fallback_is_not_consulted_when_the_node_is_missing(self):
        with patch.object(tutor, 'node_exec', return_value=(125, '', 'Error: No such container')) as execute:
            result = tutor.check('fixture', '12')
        self.assertEqual(result['status'], 'diagnostic_error')
        self.assertEqual(execute.call_count, 1)

    def test_unknown_lesson_is_honest(self):
        self.assertFalse(tutor.check('fixture', '42')['known'])
        self.assertIn('자동 도우미', tutor.help_('fixture', '42', 1)['advice'])


if __name__ == '__main__':
    unittest.main()
