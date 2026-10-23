# 네트워크 트러블슈팅 시리즈 (기본기 중심)

TCP/IP **기본 원리 하나**를 잡고, 그 개념이 깨졌을 때 나는 장애를 **직접 재현→관찰→수정**하며 체득하는 실습 시리즈.
클라우드 특정 기능이 아니라 **어디서든 통하는 네트워크 기본기**를 다룬다. (AWS·K8s 등은 "실무 사례"로만 인용)

브라우저로 실습을 제공하는 **Netlab 플랫폼**을 설치하거나 사용하려면
[Netlab 플랫폼 README](README.netlab.md)를 참고하세요. 이 문서는 실습 콘텐츠 중심의 안내입니다.

## 철학 / 공통 포맷
각 편은 아래 순서를 따른다:
1. **개념 한 장** — 이 편이 가르치는 원리
2. **토폴로지** — 최소 구성
3. **재현(고장)** — 기본 상태가 "고장"
4. **관찰** — `tcpdump`/`traceroute`/`ip` 로 증상 읽기
5. **원인** — 왜 깨지는가
6. **한 줄 수정(고침)** — 토글 한 줄로 정상화
7. **교훈** — 일반화된 원리

## 공통 환경 / 도구 (벤더 중립)
- **containerlab** + `nicolaka/netshoot`(ip/iproute2, nftables/iptables, socat, nc, tcpdump, curl, mtr)
- 동적 라우팅 편만 **FRR**(`frrouting/frr`)
- 각 편 = 폴더 하나: `NN-topic/` 안에 `*.clab.yml` + `README.md`(재현·수정 절차) + `fix.sh` + `WALKTHROUGH.md`
- **`WALKTHROUGH.md` = 모범 답안**: 전체 명령을 실제 실행한 기록 — 단계마다 *의도 → 실측 결과 → 해석*.
  권장 사용법: **먼저 README만 보고 스스로 실습 → 막히거나 끝낸 뒤 WALKTHROUGH와 대조** (처음부터 답안을 펴면 진단 근육이 안 붙는다)
- **웹 뷰어**: `walkthrough.html` — 전 장 모범답안을 한 페이지에서 단계별로 넘겨보기(←/→), 명령 복사 버튼 포함.
  브라우저로 열면 되고, WALKTHROUGH.md 수정 후엔 `python3 tools/build-walkthrough.py`로 재생성.
- 원칙: **고장이 기본값**, 수정은 `docker exec ... ip route replace ...` 같은 **한 줄**로.

## 실행 방법

### 팀원용 웹 실습 서버

브라우저 접속 → 실습 VM 발급 → 웹 터미널 실습을 제공하는 서버 소스도
[`server/`](server/README.md)에 포함되어 있습니다. 기본은 공용 guest 환경이며,
선택적으로 Cloudflare Access 기반 개인별 환경을 제공합니다. LXD VM 관리,
관리자 콘솔, 세션 녹화·리플레이와 일부 챕터의 LLM 도움 기능을 제공합니다.

- 플랫폼 소개·빠른 시작: [Netlab README](README.netlab.md)
- 운영자: [설치·배포 안내](server/README.md), [상세 운영 문서](server/OPERATIONS.md)
- 백업·이전: [암호화 백업과 빈 서버 복원](server/README.md#암호화-백업과-빈-서버-복원) (`netlab-backup` CLI)
- 학습자: [웹 실습 사용 안내](server/README.md#팀원-사용-안내)
- 로컬/단일 EC2에서 직접 실습하려면 아래 절차를 사용합니다.

새 Ubuntu 24.04 x86_64 서버는 `sudo python3 server/bootstrap.py --apply`로
LXD·전용 네트워크·스토리지·골든 VM 이미지·관리 서비스를 자동 설치할 수 있습니다.
기본은 `http://서버IP:8088`에서 인증 없이 공용 guest 환경에 접속합니다.
외부 공개와 인증은 설치자가 선택합니다. Cloudflare Access 선택 설정과 공유 환경의
주의사항은 [서버 안내](server/README.md#새-ubuntu-서버-자동-설치)를 참고하세요.

### Linux (containerlab 설치된 경우)
```sh
cd 01-l2-arp && sudo containerlab deploy -t 01-l2-arp.clab.yml
```
### macOS / containerlab 미설치 (Docker만 있으면) — `clab.sh` 래퍼
macOS는 containerlab이 호스트 netns/veth를 직접 다루지 못해서, **containerlab을 컨테이너로** 띄워 Docker Desktop VM 위에서 돌린다(`clab.sh`가 감싼다: `--privileged --pid=host --network=host` + docker.sock).
```sh
# 저장소 루트(network-lab/)에서. 토폴로지 경로는 repo 기준 상대경로.
chmod +x clab.sh                                   # 최초 1회
./clab.sh deploy   01-l2-arp/01-l2-arp.clab.yml
./clab.sh inspect  01-l2-arp/01-l2-arp.clab.yml
docker exec -it clab-01-l2-arp-h1 bash             # 노드 접속(진단)
./clab.sh destroy  01-l2-arp/01-l2-arp.clab.yml
```
- 노드 이미지(netshoot 등)는 자동으로 받는다. (검증 환경: macOS/Apple Silicon + Docker Desktop)
- **관리망 겹침 주의**: 각 topo의 `mgmt.ipv4-subnet` 이 이미 쓰는 docker 네트워크와 겹치면 배포가 실패한다 → 겹치지 않는 좁은 /24로 변경(이 저장소는 `10.99.99.0/24` 사용, 회사 172.0.0.0/12 회피).

### EC2 (Amazon Linux 2023) — 신입 파일럿 / 공용 실습 호스트
로컬 Docker Desktop 없이 EC2에서 그대로 돌릴 수 있다 (실측 검증됨 —
`clab.sh` 경로 그대로, 문서 수정 없이 전 장 동일 재현).

**권장 구성**
| 항목 | 값 | 이유 |
|---|---|---|
| AMI / 타입 | AL2023, **t3.medium (x86)** | x86이면 17장 FRR이 네이티브 (arm64는 에뮬레이션 필요) |
| 디스크 | gp3 24GB | 이미지 캐시 포함 여유 |
| 네트워크 | **프라이빗 서브넷 + NAT** (퍼블릭 IP·SSH 키·인바운드 불필요) | 접속은 SSM Session Manager |
| IAM | `AmazonSSMManagedInstanceCore` 붙은 인스턴스 프로파일 | SSM 접속/원격 실행 |

**절차**
1. **대역 충돌 확인**(1회): VPC 라우팅에 `10.99.99.0/24`(clab-mgmt)와 `172.17.0.0/16`(docker0)이
   없는지 확인. 겹치면 topo의 mgmt 서브넷/도커 `bip`을 변경.
2. `tools/ec2-user-data.sh`를 user-data로 지정해 기동 — 파일 상단의 `REPO_URL`(사내 git) 또는
   `TARBALL_URL`(git 원격이 없으면 `git archive`로 만든 tar.gz의 URL, 예: S3 presigned) 중 하나를 채울 것.
3. 부팅 후 SSM 세션 접속 → 확인:
   ```sh
   sudo -i
   cat /opt/network-lab/SMOKE_TEST_RESULT          # SMOKE_OK ... 면 이 호스트에서 전 장 동작 보장
   cd /opt/network-lab && ./clab.sh deploy 01-l2-arp/01-l2-arp.clab.yml
   ```
   (문제 시 `/var/log/network-lab-bootstrap.log` 확인)

**주의**
- **호스트당 랩 1개** — 컨테이너 이름·mgmt망이 고정이라 동시 실습 불가. 신입 여러 명이면 1인 1인스턴스
  (실습 시간만 기동하면 시간당 수십 원).
- 아웃바운드 인터넷 필요: 이미지 pull + 14장의 `apk add dnsmasq`.
- 실습 트래픽(스톰 포함)은 전부 컨테이너 netns 안에 갇혀 VPC로 새지 않는다.

---

## 커리큘럼 (기본기 → 응용)

**트랙 구분**: **[코어]** = 신입 1차 온보딩 필수(10편) · **[확장]** = 심화. 블록(L2→L3→오버레이→Transport/상태→MTU→이름/진단면→심화) 순으로 아래 계층부터 쌓는다.

> 태그 범례: **[코어]** 필수 · **[확장]** 심화 · ✅ 구현완료

### 전체 목차 — 각 장에서 무엇을 알게 되는가

- **00. 온램프** — IP/서브넷 표기 읽는 법과 도구 3종(`ip`·`ping`·`tcpdump`) 손 익히기 *(선택)*
- **01. L2 인접성 & ARP** — "같은 네트워크"의 진짜 의미와, 모든 IP 통신에 선행하는 ARP [코어]
- **02. 브로드캐스트 도메인 & L2 루프** — 스위치가 프레임을 옮기는 방식(MAC 학습)과 루프가 스톰이 되는 이유 [코어]
- **03. VLAN / 트렁크** — 태그가 하나의 스위치를 여러 네트워크로 가르는 방식 [확장]
- **04. L3 라우팅 & 기본 게이트웨이** — 라우터는 테이블이 전부라는 것, 그리고 왕복은 별개의 두 결정이라는 것 [코어]
- **05. LPM & 블랙홀** — 겹치는 라우트의 승자 규칙과, /32 하나가 조용히 전체를 죽이는 방식 [코어]
- **06. TTL & 라우팅 루프** — 패킷이 영원히 돌지 않는 이유(TTL)와 traceroute의 정체 [확장]
- **07. VXLAN 기초** — 캡슐화로 라우터 너머에 "같은 L2"를 만들어내는 원리 (오버레이/CNI의 토대) [확장]
- **08. TCP 3-way 실패 유형** — 타임아웃/refused/중간자 RST의 지문 차이로 원인 계층을 좁히는 법 [코어]
- **09. conntrack / 포트 & idle timeout** — 방화벽·NAT의 "기억"이 만료되거나 고갈될 때 나는 장애 [코어]
- **10. NAT 기본** — 소스를 바꿔야 인터넷에 나갈 수 있는 이유와 SNAT/DNAT의 방향 감각 [코어]
- **11. 대칭성 & NAT 소스 재작성** — 리턴 경로가 NAT를 스치기만 해도 연결이 죽는 이유 (08~10의 합성) [코어]
- **12. MTU / MSS / PMTUD** — "작은 건 되는데 큰 것만 멎는" 장애의 구조와 ICMP의 역할 [코어]
- **13. 터널/오버레이 MTU** — 오버레이의 50바이트 세금과 숨은 단편화의 청구서 [확장]
- **14. DNS 기본** — TTL이 곧 페일오버 시간인 이유, "고쳤는데도 안 됨"의 정체(캐시) [코어]
- **15. ICMP 차단의 대가** — "ping 안 됨 ≠ 죽음", ICMP를 전부 막으면 부서지는 것들 [확장]
- **16. NAT 헤어핀** — "밖에서는 되는데 안에서 공인 IP로는 안 되는" 이유 [확장]
- **17. 동적 라우팅 (OSPF/BGP)** — 라우트가 스스로 걸어오는 방식과, 안 걸어올 때의 추적 사다리 [확장]
- **CP-1 · CP-2. 자가 진단 테스트** — README 없이 증상만으로 원인 좁히기 (04·05 / 08~11 범위)

---

### 00. 온램프 — "읽기 전에" *(권장·선택)* ✅
- **성격**: 고장편이 아님. 진입 장벽 제거용 준비물.
- **내용**: IP/서브넷 표기(CIDR·2진수 마스크 계산), **L2/L3 계층** 개념, 도구 첫걸음(`ip`/`ping`/`tcpdump`).
- **목적**: 01편이 곧장 `ip addr add 10.10.10.1/24` 로 시작해도 벽이 되지 않도록.

---

## 🅰 L2 — 같은 세그먼트

### 01. L2 인접성 & ARP  · [코어] ✅
- **개념**: 같은 브로드캐스트 도메인 안에서만 직접 통신. next-hop은 반드시 L2로 도달 가능해야.
- **고장**: 중복 IP(ARP 충돌) / ARP 미해결 / 잘못된 서브넷 마스크.
- **관찰**: `ip neigh`, `tcpdump arp` — 누가 응답하나, 간헐 실패.
- **교훈**: L3 이전에 L2가 성립해야 한다.
```
h1 --- (switch/bridge) --- h2      # 같은 세그먼트, 마스크/중복 IP 실험
```

### 02. 브로드캐스트 도메인 & L2 루프 (+MAC 학습/CAM)  · [코어] ✅
- **개념**: 스위치의 **MAC 학습(CAM 테이블)** 과 unknown-unicast 플러딩 → "스위치가 프레임을 어떻게 옮기나". 그리고 STP가 없으면 루프 = 브로드캐스트 스톰.
- **고장**: 브리지 2개를 링크 2개로 연결(루프) → 스톰.
- **관찰**: `bridge fdb`(MAC 학습 확인), CPU/트래픽 폭증, `tcpdump`로 무한 증폭.
- **교훈**: L2는 루프에 취약, 그래서 STP가 필요.
- ⚠️ **안전장치**: macOS Docker VM(`--network=host`)에서 실제 스톰은 호스트를 때릴 수 있음 → **바운드·짧게**(패킷 수/시간 제한) 재현.

### 03. VLAN / 트렁크  · [확장] ✅
- **개념**: VLAN = 논리적 브로드캐스트 도메인, 트렁크는 태그로 다중화.
- **고장**: 트렁크 태그 허용 누락(양단 비대칭). (native VLAN 불일치는 콜아웃)
- **교훈**: 태그가 맞아야 같은 VLAN. (24bit VNI로 확장되는 07편 VXLAN의 예고)

---

## 🅱 L3 — 세그먼트를 넘어

### 04. L3 라우팅 기본 & 기본 게이트웨이 (+ICMP·traceroute 기초)  · [코어] ✅
- **개념**: 라우팅 테이블, 기본 게이트웨이, 목적지 기반 포워딩. + **ICMP echo·traceroute의 기초** — 이 시점부터 진단 도구로 쓴다(ICMP는 한 편으로 묶지 않고 04→06→12→15로 나눠 짠다).
- **고장**: 라우트/디폴트 누락, `ip_forward=0`.
- **관찰**: `ip route get`, `traceroute` 로 어디서 멎나.
```
h1 --- r1 --- r2 --- h2      # r*에 라우트 누락/게이트웨이 오설정
```

### 05. Longest Prefix Match & 블랙홀  · [코어] ✅
- **개념**: **가장 구체적인 경로가 이긴다**. 잘못된 `/32`가 전체를 덮을 수 있다.
- **고장**: `h2/32` 를 엉뚱한 next-hop(또는 블랙홀)로 → 조용히 소실.
- **관찰**: `ip route get h2` 가 엉뚱한 곳을 가리킴.
- **교훈**: 구체 경로 하나가 광역 경로를 무력화한다. ("라우트 한 줄이 전체를 죽이는" 또 다른 사례: 11편)

### 06. TTL & 라우팅 루프  · [확장] ✅
- **개념**: TTL 감소로 루프를 끊는다. (여기서 **ICMP time-exceeded** 를 눈으로)
- **고장**: r1↔r2 상호 디폴트 → 루프.
- **관찰**: `traceroute` 에 같은 홉 반복, `ICMP time exceeded`.

---

## 🅲 오버레이 — L3 위의 L2

### 07. VXLAN 기초 — L2 over L3  · [확장·확장 1순위] ✅
- **개념**: 이더넷 프레임을 **UDP/IP에 캡슐화**(dstport 4789) → 라우팅된 underlay를 넘어 **"같은 L2 세그먼트"** 를 만든다. **VNI(24bit)** = 대형 VLAN 태그. — 순수 리눅스 커널 기본기(`ip link add type vxlan`), CNI 오버레이가 쓰는 원리를 **한 줄 콜아웃**으로만 언급.
- **고장(기본)**: **VNI 불일치** / **VTEP `remote`·FDB 누락** → 같은 오버레이 서브넷인데 ARP가 너머로 안 감 → **조용히 소실**.
- **관찰**: `tcpdump`로 outer(UDP/4789)·inner 프레임, `bridge fdb show`, `ip -d link show vxlan…`.
- **교훈**: **"같은 L2"조차 물리 케이블이 아니라 encapsulation이 정할 수 있다**(01편 핵심의 확장). MTU 함정은 13편에서 터뜨린다.
- **전제**: VTEP 둘 사이에 **진짜 L3 홉(라우터)** 이 있어야 "L2 over L3"가 증명됨(같은 브리지면 요점이 사라짐). macOS에선 멀티캐스트 BUM 대신 **P2P 유니캐스트/static FDB**.
```
host --- VTEP === (routed underlay) === VTEP --- host   # 캡슐화가 세그먼트를 잇는다
```

---

## 🅳 Transport & 상태 — 연결과 세션

### 08. TCP 3-way 실패 유형 구분  · [코어] ✅
- **개념**: 증상이 원인을 말해준다.
  - **타임아웃**(무응답) = SYN 유실 / 리턴 안 옴 / 소스 불일치로 drop
  - **Connection refused**(RST) = 포트 안 열림
  - **간헐/멎음** = 방화벽 상태/재전송
- **고장**: 각 상황을 하나씩 만들어 tcpdump로 비교.
- **교훈**: "타임아웃 vs refused vs reset" 만으로 계층을 좁힌다.

### 09. conntrack / 포트 & idle timeout  · [코어] ✅
- **개념**: 상태 테이블, ephemeral 포트.
- **고장**: idle timeout 짧게 → 장수 연결(커넥션 풀)이 조용히 죽음 / SNAT 포트 고갈로 **신규만** 실패.
- **교훈**: "기존 연결은 되는데 새 연결만 안 됨" → 포트/상태 고갈 신호.

### 10. NAT 기본 — SNAT/DNAT, 소스 재작성  · [코어] ✅
- **개념**: NAT는 헤더의 **IP(때로 포트)를 바꾼다**. **SNAT**(소스 치환)·**DNAT**(목적지 치환)의 방향과 대상.
- **고장(기본)**: DNAT만 하고 **리턴 라우트 없음** / 소스가 바뀐 걸 클라가 모름.
- **관찰**: `conntrack -L`, `tcpdump`로 소스 IP가 언제 바뀌나.
- **교훈**: **NAT 지점에서 IP가 바뀐다** — 11편(대칭성)의 토대.

### 11. 대칭성 & 상태(stateful) + NAT의 소스 재작성  · [코어] ✅
- **선행**: 08(3-way) · 09(conntrack) · 10(NAT 기본)을 **합성**하는 편 — 이 셋을 배운 뒤에 보는 것이 좋다.
- **개념**:
  1. **상태 저장 장치(방화벽/NAT)는 왕복이 같은 장비를 지나야** 한다.
  2. **NAT는 헤더(소스 IP)를 바꾼다** → 클라가 접속한 IP와 응답 소스가 달라지면 세션이 안 맺힌다.
- **고장(기본)**: 서버→클라 **리턴 경로가 NAT를 경유**하도록 라우팅 → 응답 소스가 NAT IP로 치환 → 클라는 "내가 붙은 IP가 아닌 곳"에서 SYN-ACK를 받아 drop → **타임아웃**.
- **수정**: 서버 서브넷의 "클라 대역" 경로를 NAT가 아니라 **대칭 경로(정상 게이트웨이)** 로 → 소스 보존 → 성공.
- **관찰**: 클라에서 `tcpdump` — 고장 시 SYN-ACK 소스가 **NAT IP**, 수정 후 **서버 IP**.
```
client --- edge ─────── srtr --- server(VIP:443)
              \           /
               \         /
                ── nat ──      # srtr의 "라우트 테이블"이 client 대역을 nat로 보내면 고장
                               # via edge(대칭)로 바꾸면 정상 (nat은 edge·srtr 양쪽에 연결)
```
- **교훈**: 포워드가 되는데 리턴이 안 되면 **경로 대칭성/헤더 재작성**을 의심하라.

---

## 🅴 경로 & MTU

### 12. MTU / MSS / PMTUD & ICMP frag-needed  · [코어] ✅
- **개념**: MSS 협상은 양 끝 MTU만 반영, 중간 저 MTU 구간은 **PMTUD(ICMP)** 로 발견.
- **고장**: 중간 링크 MTU 축소 + **ICMP frag-needed 차단** → 작은 패킷은 되고 큰 패킷(TLS 인증서 등)에서 **멎음**.
- **관찰**: `ping -M do -s`, `tracepath`, MSS clamp 후 재시도.
- **교훈**: "작은 건 되는데 큰 전송/TLS에서 멎음" = MTU 블랙홀.

### 13. 터널/오버레이 오버헤드로 MTU 축소 (VXLAN을 매개로)  · [확장] ✅
- **개념**: VXLAN/GRE/IPsec 헤더만큼 유효 MTU 감소. **VXLAN = 정확히 50바이트**(VXLAN 8 + UDP 8 + outer IP 20 + outer Eth 14).
- **고장**: 커널은 자기 링크의 −50은 자동 반영(초과 설정은 거부!)하지만 **경로 중간의 더 좁은 underlay는 모른다** → 숨은 단편화 → 조각을 버리는 경로에서 큰 패킷만 사망.
- **교훈**: 터널/오버레이 구간엔 **MTU 조정 / MSS clamp**. (07 개념 → 12 PMTUD → 여기서 구체화)

---

## 🅵 이름 & 진단면

### 14. DNS 기본  · [코어] ✅
- **개념**: 리졸버/해석 순서, 캐시·TTL.
- **고장**: 캐시·TTL로 페일오버 안 먹음("고쳤는데도 안 됨") / 다중 A에 죽은 주소가 섞여 간헐 지연. (split-horizon은 16편 콜아웃에서 개념만)
- **교훈**: "핑은 되는데 이름으로는 안 됨" 또는 그 반대 → 해석 계층 분리해서 보라.

### 15. ICMP 차단의 대가 (진단·PMTUD 붕괴)  · [확장·실패편] ✅
- **개념**: ping/traceroute/PMTUD가 ICMP에 의존.
- **고장**: ICMP 전면 차단 → traceroute 무용 + PMTUD 블랙홀.
- **교훈**: "보안"으로 ICMP 다 막으면 진단 불가 + MTU 문제 유발.

### 16. NAT 심화 — 헤어핀 & 프로토콜 파괴  · [확장] ✅
- **개념**: SNAT/DNAT, 헤어핀(내부에서 자기 공인 IP), embedded-IP 프로토콜.
- **고장**: 헤어핀 실패 — 내부에서 자기 공인 IP 접속만 타임아웃. (FTP·SIP 등 embedded-IP 파괴는 개념 콜아웃)
- **교훈**: NAT는 IP를 바꾸므로 "IP를 페이로드에 담는" 프로토콜과 상성이 나쁘다.

---

## 🅶 심화

### 17. 동적 라우팅 — BGP/OSPF (FRR)  · [확장] ✅
- **개념**: 경로 광고/수신/선택. (FRR 사용)
- **고장**: 인접 불성립(OSPF area 불일치) / 세션은 Established인데 prefix 미광고(BGP). (필터 드랍·비대칭 선택은 콜아웃)
- **교훈**: "라우트가 왜 없지/왜 이 경로지"를 광고·수신·정책으로 추적.

---

## 🧪 블라인드 체크포인트 (평가)
따라 하면 재현되는 실습만으론 "아는 것"에 그친다. 실무는 **README 없이 증상만으로 계층을 좁히는** 일. 코어 블록을 마칠 때마다 **"고장난 채로 뜨는" 랩**으로 스스로 진단하게 한다.
- **CP-1 (L3 종합)**: 04·05를 섞은 이중 고장 배포 → 어디서 멎나 스스로 찾기.
- **CP-2 (상태/NAT 종합)**: 08·09·10·11을 섞어 고장 배포 → 타임아웃/RST/소스불일치 구분.

---

## 부록 A. 진단 방법론 (모든 편 공통)
- **증상 → 계층 좁히기**: 타임아웃/RST/에러메시지가 각각 뜻하는 것.
- **`tcpdump`로 3-way 읽기**: SYN/SYN-ACK/RST, 소스 IP·크기 확인.
- **경로**: `ip route get`, `traceroute`, `mtr`.
- **MTU**: `ping -M do -s`, `tracepath`, MSS clamp 후 재시도.
- **툴의 한계**: 정적 분석 도구는 데이터플레인을 보지 못한다(설정만 본다). 일부 LB/게이트웨이는 ICMP 무응답.

## 부록 B. 리포지토리 구조
```
network-lab/
├── README.md                     # (이 문서) 시리즈 인덱스
├── clab.sh                       # containerlab-in-docker 래퍼 (macOS/미설치 환경)
├── 00-onramp/                    # (선택) 준비물 프라이머
├── 01-l2-arp/          ✅        # 각 편 = 폴더 하나
├── 02-bcast-loop/
├── 03-vlan-trunk/
├── 04-l3-routing/
├── 05-lpm-blackhole/
├── 06-ttl-loop/
├── 07-vxlan-overlay/             # L2 over L3
├── 08-tcp-3way/
├── 09-conntrack/
├── 10-nat-basics/
├── 11-symmetry-nat/
├── 12-mtu-pmtud/
├── 13-tunnel-mtu/                # VXLAN을 매개로
├── 14-dns/
├── 15-icmp-blocked/
├── 16-nat-hairpin/
├── 17-dynamic-routing/
│     ├── *.clab.yml
│     ├── README.md               # 재현→관찰→수정 절차
│     ├── WALKTHROUGH.md          # 모범 답안 (의도→실측→해석 실행 기록)
│     └── fix.sh
└── checkpoints/                  # 블라인드 진단 랩(CP-1, CP-2)
```

## 진행 순서(제안)
- **코어 트랙(신입 1차 온보딩, 10편)**: `01 → 02 → 04 → 05 → 08 → 09 → 10 → 11 → 12 → 14`
  - L2 성립 → L3 도달 → 연결/상태 → 대칭성&NAT → MTU → 이름 해석. 이 10편이면 "증상으로 계층 좁히기"가 몸에 붙는다.
- **확장 트랙(2차)**: `03`(VLAN) · `06`(TTL) · **`07`(VXLAN, 확장 1순위)** · `13`(터널 MTU) · `15`(ICMP 차단) · `16`(NAT 심화) · `17`(동적 라우팅).
- **블라인드 체크포인트**: 코어 블록을 마칠 때마다 CP-1(L3)·CP-2(상태/NAT)로 스스로 진단.

