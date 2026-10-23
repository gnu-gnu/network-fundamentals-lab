import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
import knowledge


class KnowledgeTests(unittest.TestCase):
    def test_negative_solution_request_and_hint_context(self):
        self.assertFalse(knowledge.allow_solution('정답은 말하지 말고 힌트만 주세요'))
        refs = knowledge.retrieve('04', '리턴 라우트 힌트만', help_level=1)
        self.assertFalse(any(r['kind'] == 'solution' for r in refs))
        self.assertNotIn('ip route replace', '\n'.join(r['text'] for r in refs))
        self.assertNotIn('ip route add', '\n'.join(r['text'] for r in refs))

    def test_current_chapter_guide_and_topology_are_included(self):
        refs = knowledge.retrieve('01', 'ARP 테이블이 비어 있는 이유는?')
        self.assertEqual(refs[0]['path'], '01-l2-arp/README.md')
        self.assertTrue(any(r['kind'] == 'topology' for r in refs))
        self.assertTrue(any(r['kind'] == 'wiki' for r in refs))
        self.assertLessEqual(sum(len(r['text']) for r in refs), 14000)

    def test_solution_requires_explicit_request(self):
        self.assertFalse(knowledge.allow_solution('ARP가 뭐야?'))
        self.assertTrue(knowledge.allow_solution('정답 명령을 구체적으로 알려 줘.'))
        ordinary = knowledge.retrieve('04', '리턴 라우트 r2 수정 명령', solution=False, budget=30000)
        self.assertFalse(any(r['kind'] == 'solution' for r in ordinary))
        # Broader query ensures the gated corpus is actually searchable.
        detailed = knowledge.retrieve('04', '단계 수정 r2 route 10.4.1.0', solution=True, limit=6, budget=30000)
        self.assertTrue(any(r['kind'] == 'solution' for r in detailed))

    def test_ospf_does_not_receive_bgp_topology(self):
        refs = knowledge.retrieve('17-ospf', '인접 관계 area OSPF')
        topology = [r['path'] for r in refs if r['kind'] == 'topology']
        self.assertEqual(topology, ['17-dynamic-routing/ospf.clab.yml'])

    def test_sources_have_real_ids_hashes_and_line_numbers(self):
        for ref in knowledge.retrieve('12', '큰 DF 패킷 MTU 통보'):
            source = knowledge.source(ref['document'])
            self.assertEqual(hashlib.sha256(source['text'].encode()).hexdigest(), ref['sha256'])
            self.assertGreaterEqual(ref['line'], 1)
            self.assertIn(ref['text'], source['text'])
        self.assertIsNone(knowledge.source('../../etc/passwd'))

    def test_generated_corpus_matches_repository_sources(self):
        import build_knowledge
        self.assertEqual(json.loads(knowledge.CORPUS.read_text()), build_knowledge.build())

    def test_generic_observation_request_does_not_mix_unrelated_labs(self):
        refs = knowledge.retrieve('01', '이 실습에서 뭘 관찰하면 될까? 현재 상태를 근거로 네 문장으로 설명해 줘.')
        self.assertTrue(all(r['path'].startswith(('01-', 'server/knowledge/wiki/')) for r in refs))
