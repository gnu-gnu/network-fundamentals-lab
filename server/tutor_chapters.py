"""Trusted curriculum metadata. Probes are fixed argv, never model-generated."""

CONCEPTS = {
 '00': '인터페이스의 주소·서브넷과 링크 상태를 구분하고 명령 출력의 근거를 읽는다.',
 '01': '같은 서브넷은 목적지 MAC을 ARP로 찾는다. 다른 서브넷은 경로와 다음 홉이 필요하며 다음 홉 MAC을 찾는다.',
 '02': '스위치는 출발지 MAC을 학습한다. 이더넷 프레임에는 IP TTL 같은 홉 제한이 없어 L2 루프가 브로드캐스트를 증폭시킨다. STP는 중복 경로를 차단한다.',
 '03': '같은 IP 서브넷 설정만으로 L2 연결이 생기지 않는다. VLAN별 브로드캐스트 도메인과 양쪽 트렁크의 VLAN 허용이 일치해야 한다.',
 '04': '라우팅은 목적지별 다음 홉 선택이다. 요청의 경로와 응답의 리턴 경로가 모두 있어야 통신한다.',
 '05': '라우팅은 가장 긴 접두사부터 선택한다. 더 구체적인 /32 블랙홀은 정상 /24 연결 경로보다 우선한다.',
 '06': 'IP TTL은 라우터마다 감소하며 0이 되면 폐기된다. TTL 소진은 루프를 멈추지만 잘못된 경로를 고치지는 않는다.',
 '07': 'VXLAN은 L3 언더레이 위에 L2 오버레이를 만든다. 언더레이 도달성만으로 충분하지 않고 VNI·VTEP 등의 일치가 필요하다.',
 '08': 'SYN 무응답과 RST는 다르다. 서버 리스너 부재와 중간 방화벽 REJECT는 둘 다 RST를 만들 수 있어 출처를 관찰해야 한다.',
 '09': 'conntrack은 연결 상태와 타이머를 유지한다. 유휴 타임아웃과 SNAT 포트 자원 고갈은 서로 다른 실패 원인이다.',
 '10': 'SNAT은 출발지 주소를 바꾸고 응답 시 연결 상태에 따라 역변환한다. 리턴 경로가 없는 사설 출발지로는 왕복이 성립하지 않을 수 있다.',
 '11': 'TCP는 양 끝 주소·포트의 일관성이 필요하다. 비대칭 경로 자체가 항상 장애는 아니지만 상태 장비나 일방적 주소 재작성을 만나면 실패할 수 있다.',
 '12': 'PMTUD는 DF 패킷의 ICMP fragmentation-needed 피드백으로 경로 MTU를 학습한다. 피드백 차단 시 큰 패킷만 조용히 실패할 수 있다.',
 '13': '터널 헤더가 MTU를 소비한다. 로컬 링크 MTU만으로 경로 중간의 더 작은 MTU를 알 수 없으므로 경로 최솟값에서 오버헤드를 빼야 한다.',
 '14': '권한 DNS 변경과 재귀 캐시 갱신은 별개다. TTL 동안 옛 답이 유지되며 DNS 응답 성공과 실제 서비스 성공도 별개다.',
 '15': 'ICMP는 진단뿐 아니라 경로 오류·MTU 통보를 전달한다. 전면 차단과 필수 유형 선별 허용을 구분한다.',
 '16': '헤어핀 DNAT 후 서버가 클라이언트로 직접 응답하면 역변환을 우회할 수 있다. 제한된 SNAT으로 왕복을 NAT 장비에 통과시킨다.',
 '17-ospf': 'OSPF 인접 관계에는 영역 등 설정 일치가 필요하다. 인접 형성·경로 교환·커널 포워딩을 각각 확인한다.',
 '17-bgp': 'BGP 세션 Established는 모든 목적지의 경로 광고를 보장하지 않는다. 광고할 접두사와 수신 경로, 왕복 경로를 따로 확인한다.',
}


def ping(node, address):
    return node, ['ping', '-c1', '-W2', address], '1 received'


def chapter(prefix, title, fault, goal, success, probes, observational=False):
    return dict(prefix='clab-' + prefix, title=title, fault=fault, goal=goal,
                success=success, probes=probes, observational=observational,
                hints=['관찰 결과를 근거로 원인을 좁혀 보세요.',
                       '양 끝의 설정과 왕복 경로를 비교해 보세요.',
                       '정상 조건과 실제 상태의 차이를 확인하세요.'])


# ── 챕터 메타데이터 (netlab_tutor.CHAPTERS 의 원천) ─────────────────────
# 각 챕터:
#   prefix   : clab 노드 이름 접두사 (배포 감지·명령에 씀)
#   title    : 사람용 이름
#   fault    : 심어진 고장 (LLM 맥락용, 학습자에겐 직접 안 보임)
#   goal     : 성공조건을 사람 말로
#   success  : (node, argv, needle) — node 안에서 argv 실행, 출력에 needle 있으면 통과
#   probes   : help 가 모을 비파괴 진단들 [(라벨, node, argv)]  node=None 이면 VM 셸에서
#   hints    : 단계식 힌트. 도움 요청이 늘수록 뒤 항목까지 LLM 에 준다(점점 구체적).
BASE = {
    "01": {
        "prefix": "clab-01-l2-arp",
        "title": "01장 L2 인접성 & ARP",
        "observational": True,   # 고칠 고장이 없는 관찰 실습
        "fault": "이 챕터엔 심어둔 고장이 없다. 같은 L2 세그먼트에서 ARP 로 서로를 찾고, "
                 "다른 서브넷은 라우터가 없어 의도적으로 unreachable 인 것을 관찰하는 실습이다.",
        "goal": "ARP 로 MAC 을 학습하는 과정과, 라우터 없이 다른 서브넷이 왜 안 되는지를 관찰한다.",
        "probes": [
            ("h1 ARP 테이블", "h1", ["ip", "neigh"]),
            ("h1 실습 주소(관리망 제외)", "h1", ["ip", "-br", "addr", "show", "dev", "eth1"]),
            ("h2 실습 주소와 MAC", "h2", ["ip", "addr", "show", "dev", "eth1"]),
            ("h3 실습 주소와 MAC", "h3", ["ip", "addr", "show", "dev", "eth1"]),
            ("sw 브리지 MAC", "sw", ["bash", "-c", "bridge fdb show 2>/dev/null | grep -v permanent | head"]),
        ],
        "hints": [
            "h1 에서 h2(10.10.10.2)로 ping 해보고, ip neigh 로 ARP 테이블에 MAC 이 학습되는지 보세요.",
            "sw(스위치)의 bridge fdb 에 각 호스트 MAC 이 어느 포트에서 학습됐는지 관찰하세요.",
            "실습 B에서 h2 주소를 10.10.20.2/24로 바꾼 뒤 다른 서브넷으로 ping 하면 왜 'Network unreachable'인지 생각해 보세요.",
        ],
    },
    "07": {
        "prefix": "clab-07-vxlan-overlay",
        "title": "07장 VXLAN 오버레이",
        "fault": "vtep2 의 VXLAN VNI 가 200 으로 설정돼 vtep1(100)과 불일치. "
                 "언더레이(L3)는 정상이라 outer 패킷은 도착하지만 VNI 가 달라 조용히 버려진다.",
        "goal": "vtep1 에서 오버레이 주소 192.168.100.2 로 ping 이 통해야 한다.",
        "success": ("vtep1", ["ping", "-c1", "-W2", "192.168.100.2"], "1 received"),
        "probes": [
            ("vtep1 오버레이 ping", "vtep1", ["ping", "-c1", "-W1", "192.168.100.2"]),
            ("vtep1 언더레이 ping", "vtep1", ["ping", "-c1", "-W1", "10.7.2.1"]),
            ("vtep1 vxlan 설정", "vtep1", ["ip", "-d", "link", "show", "vxlan100"]),
            ("vtep2 vxlan 설정", "vtep2", ["ip", "-d", "link", "show", "vxlan100"]),
            ("vtep1 fdb", "vtep1", ["bridge", "fdb", "show", "dev", "vxlan100"]),
        ],
        "hints": [
            "증상을 정확히 나눠보세요. 언더레이(10.7.x)는 되는데 오버레이(192.168.100.x)만 안 되나요?",
            "VXLAN 은 양쪽 끝이 합의해야 성립합니다. 한쪽 설정만 보지 말고 vtep1 과 vtep2 를 나란히 비교해 보세요.",
            "양쪽 vxlan 인터페이스의 VNI(vxlan id)를 비교하세요. 값이 다르면 패킷은 도착해도 조용히 버려집니다.",
        ],
    },
    "04": {
        "prefix": "clab-04-l3-routing",
        "title": "04장 L3 라우팅",
        "fault": "r2 에 10.4.1.0/24(반대편 서브넷)로 가는 경로가 없다. 왕복 중 돌아오는 "
                 "경로가 끊겨 통신이 실패한다(편도만 되는 전형적 라우팅 누락).",
        "goal": "h1 에서 h2(다른 서브넷)로 ping 이 통해야 한다.",
        "success": ("h1", ["ping", "-c1", "-W2", "10.4.2.10"], "1 received"),
        "probes": [
            ("h1→h2 ping", "h1", ["ping", "-c1", "-W1", "10.4.2.10"]),
            ("h1 라우팅", "h1", ["ip", "route"]),
            ("r1 라우팅", "r1", ["ip", "route"]),
            ("r2 라우팅", "r2", ["ip", "route"]),
        ],
        "hints": [
            "ping 이 아예 안 가나요, 아니면 가는데 응답이 안 오나요? 편도만 되는지 확인해 보세요.",
            "라우터의 양방향을 모두 보세요. 가는 경로가 있으면 돌아오는 경로도 있어야 합니다.",
            "r2 의 라우팅 테이블에 10.4.1.0/24 로 가는 경로가 있는지 확인하세요. 없으면 돌아오는 패킷이 길을 잃습니다.",
        ],
    },
    "10": {
        "prefix": "clab-10-nat-basics",
        "title": "10장 NAT 기초",
        "fault": "nat 노드에 SNAT(MASQUERADE) 규칙이 없다. 안쪽(cli)에서 나간 패킷의 출발지가 "
                 "사설 주소 그대로라, 바깥(srv)이 응답을 어디로 보낼지 몰라 왕복이 안 된다.",
        "goal": "cli 에서 srv 로 통신이 왕복해야 한다.",
        "success": ("cli", ["ping", "-c1", "-W2", "10.10.2.10"], "1 received"),
        "probes": [
            ("cli→srv ping", "cli", ["ping", "-c1", "-W1", "10.10.2.10"]),
            ("nat POSTROUTING", "nat", ["iptables", "-t", "nat", "-S", "POSTROUTING"]),
            ("nat conntrack", "nat", ["bash", "-c", "conntrack -L 2>/dev/null | head -5"]),
            ("cli 주소", "cli", ["ip", "-br", "addr"]),
        ],
        "hints": [
            "cli 가 srv 로 보낸 패킷은 도착하는데 응답이 안 오나요? 출발지 주소가 뭘로 보일지 생각해 보세요.",
            "srv 입장에서 cli 의 사설 주소로는 응답을 보낼 수 없습니다. 중간의 nat 노드가 주소를 바꿔줘야 합니다.",
            "nat 노드의 iptables NAT 테이블(POSTROUTING)에 MASQUERADE/SNAT 규칙이 있는지 확인하세요.",
        ],
    },
}

EXTRA = {
 '00': chapter('00-onramp', '00장 온램프', '고장 없음. 도구를 연습하는 관찰 실습.',
    '주소·인터페이스·ping 출력을 읽는다.', None,
    [('h1 주소', 'h1', ['ip', '-br', 'addr']), ('h1 이웃', 'h1', ['ip', 'neigh'])], True),
 '02': chapter('02-bcast-loop', '02장 브로드캐스트와 L2 루프',
    '배포 시 sw1 eth3은 down이므로 루프가 없다. 학습자가 링크를 올릴 때 루프가 생긴다.',
    'MAC 학습과 루프·STP를 관찰한다. 링크를 자동으로 올리지 않는다.', None,
    [('sw1 브리지 포트', 'sw1', ['bridge', '-d', 'link']),
     ('sw2 브리지 포트', 'sw2', ['bridge', '-d', 'link']),
     ('sw1 STP', 'sw1', ['ip', '-d', 'link', 'show', 'br0'])], True),
 '03': chapter('03-vlan-trunk', '03장 VLAN과 트렁크', 'sw2 트렁크 eth2에 VLAN 10이 누락돼 있다.',
    'VLAN 10 h1↔h2 통신을 회복하고 VLAN 20 h3 격리를 유지한다.', ping('h1', '10.3.10.2'),
    [('sw1 VLAN', 'sw1', ['bridge', 'vlan', 'show']), ('sw2 VLAN', 'sw2', ['bridge', 'vlan', 'show']),
     ('h1 이웃', 'h1', ['ip', 'neigh'])]),
 '05': chapter('05-lpm-blackhole', '05장 최장 접두사와 블랙홀', 'r1에 10.5.2.10/32 blackhole이 있다.',
    '10.5.2.10과 대조군 .11의 차이를 설명하고 통신을 회복한다.', ping('h1', '10.5.2.10'),
    [('r1 경로', 'r1', ['ip', 'route']), ('r1 목적지 선택', 'r1', ['ip', 'route', 'get', '10.5.2.10']),
     ('대조군 ping', 'h1', ['ping', '-c1', '-W1', '10.5.2.11'])]),
 '06': chapter('06-ttl-loop', '06장 TTL과 라우팅 루프', 'r1과 r2가 서로를 기본 경로로 가리킨다.',
    '모르는 목적지를 순환시키지 않고 명시적으로 거절한다. 외부 목적지 ping 성공이 목표가 아니다.',
    ('r2', ['ip', 'route', 'show', 'default'], 'unreachable default'),
    [('r1 경로', 'r1', ['ip', 'route']), ('r2 경로', 'r2', ['ip', 'route'])]),
 '08': chapter('08-tcp-3way', '08장 TCP 연결 증상 분류',
    '6060 정상, 8080 DROP, 9090 리스너 없음, 7070 방화벽 RST. 수정은 부록이다.',
    '타임아웃과 RST의 발신 주체를 구분한다.', None,
    [('서버 리스너', 'srv', ['ss', '-lnt']), ('방화벽', 'fw', ['nft', 'list', 'ruleset'])], True),
 '09': chapter('09-conntrack', '09장 연결 추적',
    'established timeout 30초, SNAT 포트 30000-30001 두 개로 제한.',
    '유휴 연결 소멸과 신규 연결 포트 고갈을 구분한다. 단일 ping은 완료 판정이 아니다.', None,
    [('연결 추적 제한', 'nat', ['sysctl', 'net.netfilter.nf_conntrack_tcp_timeout_established']),
     ('NAT 규칙', 'nat', ['iptables', '-t', 'nat', '-S']),
     ('연결 추적', 'nat', ['conntrack', '-L'])], True),
 '11': chapter('11-symmetry-nat', '11장 비대칭 경로와 주소 변환',
    'srtr의 클라이언트 리턴 경로가 nat를 지나며 SYN-ACK 출발지가 바뀐다.',
    'TCP 왕복 경로와 출발지 주소의 일관성을 회복한다.',
    ('cli', ['bash', '-c', 'timeout 4 nc -w 2 10.11.2.10 443'], 'pong'),
    [('srtr 경로', 'srtr', ['ip', 'route']), ('재작성 규칙', 'nat', ['nft', 'list', 'ruleset'])]),
 '12': chapter('12-mtu-pmtud', '12장 MTU와 PMTUD',
    'r1의 ICMP frag-needed 차단. 중간 링크 MTU 1400을 발신자가 알 수 없다.',
    '큰 DF 패킷에 MTU 통보를 받고 경로 MTU를 학습한다. 큰 ping 자체의 성공을 요구하지 않는다.',
    ('h1', ['ping', '-M', 'do', '-c1', '-W2', '-s', '1472', '10.12.2.10'], 'mtu = 1400'),
    [('r1 차단 규칙', 'r1', ['nft', 'list', 'ruleset']),
     ('h1 경로 MTU', 'h1', ['ip', 'route', 'get', '10.12.2.10']),
     ('r1 링크 MTU', 'r1', ['ip', '-br', 'link'])]),
 '13': chapter('13-tunnel-mtu', '13장 오버레이 MTU',
    '경로 중간 MTU 1400, vtep1 오버레이 MTU 1450, vtep2가 비초기 조각을 거부한다.',
    '경로 최솟값에서 오버헤드를 뺀 MTU로 큰 오버레이 패킷을 전달한다.',
    ('vtep1', ['ping', '-M', 'dont', '-c1', '-W2', '-s', '1400', '192.168.100.2'], '1 received'),
    [('vtep1 링크', 'vtep1', ['ip', '-d', 'link', 'show', 'vxlan100']),
     ('vtep2 링크', 'vtep2', ['ip', '-d', 'link', 'show', 'vxlan100']),
     ('중간 링크', 'r1', ['ip', '-br', 'link'])]),
 '14': chapter('14-dns', '14장 DNS와 캐시',
    'app.lab이 죽은 srv1을 가리킨다. 권한 서버 수정 후에도 캐시에 옛 답이 남을 수 있다.',
    '권한 답과 캐시 답을 구분하고 app.lab 서비스를 회복한다. multi.lab은 별도 과제다.',
    ('cli', ['curl', '--noproxy', '*', '-sS', '--max-time', '3', 'http://app.lab/'], 'pong-from-srv2'),
    [('캐시 응답', 'cli', ['dig', '+time=1', '+tries=1', '@10.14.0.53', 'app.lab']),
     ('권한 응답', 'cli', ['dig', '+time=1', '+tries=1', '@10.14.0.54', 'app.lab']),
     ('다중 A', 'cli', ['dig', '+time=1', '+tries=1', '@10.14.0.53', 'multi.lab'])]),
 '15': chapter('15-icmp-blocked', '15장 ICMP 차단', 'fw가 ICMP를 전면 차단한다.',
    '필수 ICMP를 선별 허용한다. ping 성공만으로 전체 정책이 올바르다고 판정하지 않는다.',
    ping('h1', '10.15.2.10'),
    [('방화벽 규칙', 'fw', ['nft', 'list', 'ruleset']), ('발신 경로', 'h1', ['ip', 'route'])]),
 '16': chapter('16-nat-hairpin', '16장 헤어핀 NAT',
    '내부 클라이언트가 공인 주소로 DNAT 접속할 때 헤어핀 SNAT가 없다.',
    '서버 응답도 NAT 경로를 지나도록 해 공인 주소 접속을 회복한다.',
    ('cli', ['curl', '--noproxy', '*', '-sS', '--max-time', '3', 'http://10.16.99.1:8080/'], 'pong-hairpin'),
    [('NAT 규칙', 'nat', ['iptables', '-t', 'nat', '-S']),
     ('서버 경로', 'srv', ['ip', 'route']), ('연결 추적', 'nat', ['conntrack', '-L'])]),
}

EXTRA['03']['additional_success'] = [
    ('h1', ['ping', '-c1', '-W2', '10.3.10.3'], '0 received')]

for key in ('08', '09'):
    EXTRA[key].update(observational=False, assessment_mode='exploration')

for key, protocol, fault, command in (
    ('17-ospf', 'OSPF', 'r2 area 1과 r1 area 0 불일치로 이웃이 형성되지 않는다.', 'show ip ospf neighbor'),
    ('17-bgp', 'BGP', '세션은 Established지만 r1의 10.17.1.0/24 network 광고가 없다.', 'show bgp ipv4 unicast summary')):
    EXTRA[key] = chapter(key, '17장 ' + protocol, fault,
        '인접 관계와 경로 광고를 구분하고 h1↔h2 왕복 통신을 회복한다.', ping('h1', '10.17.2.10'),
        [('r1 ' + protocol, 'r1', ['vtysh', '-c', command]),
         ('r2 ' + protocol, 'r2', ['vtysh', '-c', command]),
         ('r1 경로', 'r1', ['ip', 'route']), ('r2 경로', 'r2', ['ip', 'route'])])

# 기본 챕터(BASE)는 EXTRA 에 같은 키가 없을 때만 들어간다 — 예전 CHAPTERS.update(EXTRA) 와 같은 우선순위.
for _key, _meta in BASE.items():
    EXTRA.setdefault(_key, _meta)
