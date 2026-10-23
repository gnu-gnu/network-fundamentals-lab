"""Compare knowledge.retrieve between two server trees over a question battery.

usage: python3 tests/equivalence_retrieval.py <old_server_dir> <new_server_dir>
The old tree may predate learning packs (document kind "topology" is mapped to "definition").
Exit status is the number of differing comparisons (capped at 1).
"""
import importlib.util, itertools, sys
from pathlib import Path
OLD, NEW = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(NEW))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

old = load('old_knowledge', OLD / 'knowledge.py')
new = load('new_knowledge', NEW / 'knowledge.py')
import packs
chapters = [None, '', '00', '01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12', '13', '14', '15', '16', '17-ospf', '17-bgp', '99', '18']
questions = [
    'ARP 테이블이 비어 있는 이유는?', '리턴 라우트 힌트만', '리턴 라우트 r2 수정 명령', '단계 수정 r2 route 10.4.1.0',
    '인접 관계 area OSPF', '큰 DF 패킷 MTU 통보', '이 실습에서 뭘 관찰하면 될까? 현재 상태를 근거로 네 문장으로 설명해 줘.',
    'NAT 헤어핀이 뭐야', 'VLAN 트렁크 설정 확인', 'DNS 캐시 TTL 왜 남아있어', 'VXLAN VNI 가 뭔가요', '04장이랑 비교하면?',
    'BGP 세션 Established 인데 왜 안 돼', 'h1 에서 ping 이 안 가요', 'conntrack 테이블 어떻게 봐', '정답 명령을 구체적으로 알려 줘.',
    '터널 MTU 계산', 'ICMP 차단 규칙', '스위치 MAC 학습', '', 'r1 ip route', 'iptables -t nat -S 결과 해석',
]
options = [dict(), dict(solution=True, limit=6, budget=30000), dict(help_level=1), dict(help_level=2), dict(help_level=3), dict(solution=True, help_level=1)]
kind = {'topology': 'definition'}
total = diff = 0
for chapter, question, opts in itertools.product(chapters, questions, options):
    a = old.retrieve(chapter, question, **opts)
    b = new.retrieve(chapter, question, **opts)
    na = [(r['citation'], r['path'], r['section'], r['line'], kind.get(r['kind'], r['kind']), r['text'], r['truncated']) for r in a]
    nb = [(r['citation'], r['path'], r['section'], r['line'], r['kind'], r['text'], r['truncated']) for r in b]
    total += 1
    if na != nb:
        diff += 1
        if diff <= 5:
            print('DIFF', repr(chapter), question[:30], opts, [x[1] for x in na], '->', [x[1] for x in nb])
print(f'retrieve comparisons: {total}, differences: {diff}')
sys.exit(1 if diff else 0)
