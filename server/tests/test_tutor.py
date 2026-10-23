import importlib.util
import io
import json
from pathlib import Path
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
spec = importlib.util.spec_from_file_location('tutor_test', Path(__file__).parents[1] / 'netlab-tutor.py')
tutor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tutor)


class TutorTests(unittest.TestCase):
    def test_latest_deployment_wins_over_metadata_order(self):
        listing = ('2026-10-22 08:00:00 +0900 KST\tclab-01-l2-arp-h1\n'
                   '2026-10-22 08:32:00 +0900 KST\tclab-04-l3-routing-r1\n'
                   '2026-10-22 08:10:00 +0900 KST\tclab-02-bcast-loop-h1')
        with patch.object(tutor, 'lxc_exec', return_value=(0, listing, '')):
            self.assertEqual(tutor.deployed_chapter('fixture'), '04')

    def test_latest_dynamic_routing_variant_is_preserved(self):
        with patch.object(tutor, 'lxc_exec', return_value=(0,
                '2026-10-22 09:00:00 +0900 KST\tclab-17-ospf-r1\n'
                '2026-10-22 08:00:00 +0900 KST\tclab-01-l2-arp-h1', '')):
            self.assertEqual(tutor.deployed_chapter('fixture'), '17-ospf')

    def call(self, config, message):
        with patch.object(tutor, 'load_cfg', return_value=config), \
                patch.object(tutor.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())) as opened:
            response = tutor.call_llm('fixture prompt')
        return response, opened.call_args.args[0]

    def test_local_reasoning_model_options_without_auth(self):
        cfg = dict(endpoint='http://model.invalid/v1/chat/completions', model='fixture',
                   max_tokens=4096, temperature=1.0, top_p=0.95, top_k=20, reasoning_effort='low')
        response, req = self.call(cfg, {'reasoning': 'private reasoning', 'content': '  답변  '})
        body = json.loads(req.data)
        for key in ('model', 'max_tokens', 'temperature', 'top_p', 'top_k', 'reasoning_effort'):
            self.assertEqual(body[key], cfg[key])
        self.assertFalse(req.has_header('Authorization'))
        self.assertEqual(response, '답변')

    def test_existing_bearer_key_still_supported(self):
        _, req = self.call(dict(endpoint='http://model.invalid/v1/chat/completions',
                                model='fixture', api_key='fixture-key'), {'content': 'ok'})
        self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-key')
        self.assertNotIn('top_k', json.loads(req.data))

    def test_muse_reasoning_strength_passes_to_chat_template(self):
        cfg=dict(endpoint='http://model.invalid/v1/chat/completions',model='muse',
                 chat_template_kwargs={'reasoning_strength':'low'})
        _, req=self.call(cfg, {'content':'ok'})
        self.assertEqual(json.loads(req.data)['chat_template_kwargs'],{'reasoning_strength':'low'})

    def test_long_response_budget_and_configurable_timeout(self):
        cfg=dict(endpoint='http://model.invalid/v1/chat/completions',model='fixture',
                 max_tokens=8192,request_timeout_seconds=600)
        with patch.object(tutor,'load_cfg',return_value=cfg), \
                patch.object(tutor.urllib.request,'urlopen',return_value=io.BytesIO(
                    json.dumps({'choices':[{'message':{'content':'ok'}}]}).encode())) as opened:
            self.assertEqual(tutor.call_llm('test'),'ok')
        self.assertEqual(json.loads(opened.call_args.args[0].data)['max_tokens'],8192)
        self.assertEqual(opened.call_args.kwargs['timeout'],600)

    def test_reasoning_only_response_is_not_an_answer(self):
        with self.assertRaisesRegex(RuntimeError, '최종 답변'):
            self.call(dict(endpoint='http://model.invalid/v1/chat/completions', model='fixture'),
                      {'reasoning': 'unfinished', 'content': None})

    def test_vlan_success_also_requires_other_vlan_isolation(self):
        with patch.object(tutor, 'node_exec', side_effect=[(0, '1 received', ''), (0, '1 received', '')]):
            self.assertFalse(tutor.check('fixture', '03')['ok'])
        with patch.object(tutor, 'node_exec', side_effect=[(0, '1 received', ''), (1, '0 received', '')]):
            self.assertTrue(tutor.check('fixture', '03')['ok'])

    def test_pmtud_success_is_mtu_feedback_not_ping_exit_code(self):
        with patch.object(tutor, 'node_exec', return_value=(1, 'Frag needed and DF set (mtu = 1400)', '')):
            self.assertTrue(tutor.check('fixture', '12')['ok'])
        for feedback in ('local error: message too long, mtu=1400', 'cache expires 500sec mtu 1400'):
            with patch.object(tutor, 'node_exec', return_value=(1, feedback, '')):
                self.assertTrue(tutor.check('fixture', '12')['ok'])
        with patch.object(tutor, 'node_exec', side_effect=[
                (1, '', 'ping: sendmsg: Message too large'),
                (0, '10.12.2.10 via 10.12.1.1 cache expires 500sec mtu 1400', '')]):
            self.assertTrue(tutor.check('fixture', '12')['ok'])

    def test_exploration_has_no_binary_completion(self):
        for chapter in ('08', '09'):
            result = tutor.check('fixture', chapter)
            self.assertEqual(result['assessment_mode'], 'exploration')

    def test_guest_failure_is_not_a_network_failure(self):
        with patch.object(tutor, 'node_exec', return_value=(1, '', 'Error: Instance is not running')):
            self.assertEqual(tutor.check('fixture', '04')['status'], 'diagnostic_error')
