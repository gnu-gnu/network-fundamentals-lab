# 네트워크 실습 환경 — 운영 문서

2026-10-22 운영 호스트에서 가져온 문서다. 설치·업데이트는 [README](README.md)를 참고한다.
도메인·디스크·용량은 기존 운영 환경의 예시이며 새 서버에 그대로 적용하지 않는다.
플랫폼 소개는 [Netlab README](../README.netlab.md)를 참고한다. 현재 신규 설치의 기본은
로그인 없는 공용 guest 모드이며, 아래 인증 경로·물리 ZFS 구성·예비 VM 설정은 기존
운영 사례다. 신규 설치의 기본값은 [상세 설치 안내](README.md)를 기준으로 한다.

`gnu-gnu/network-fundamentals-lab` 을 **필요할 때 본인이 발급받는** 격리 VM 으로 제공한다.

## 구조

```
브라우저 ──▶ https://netlab.gnu.or.kr/  또는  https://netlab.modus.ai.kr/
                              (외부: Cloudflare Access → 계정 로그인)
        또는 http://<호스트>:8088/            (사내망 직결)
                 │                            로그인하면 본인 패널로 이동
                 │
                 ├─ /u/<계정>/ctl/       셀프서비스 패널 (시작·종료·초기화·반납)
                 ├─ /u/<계정>/term/      브라우저 터미널 (발급된 VM 으로 연결)
                 └─ /admin/              관리자 콘솔 (현황·강제 회수·정책)

호스트
   ├─ nginx :8088        127.0.0.1 → Access(JWT 검증) / 그 외 → basic auth
   │                     라우팅은 labd 가 상황에 맞춰 두 벌 생성한다.
   ├─ labd  :9000      발급/정지/회수, 예비 풀 유지, 자동 회수  (127.0.0.1 전용)
   └─ LXD 5.21 LTS
        ├─ 저장소 labpool = ZFS (zpool on /dev/ubuntu-vg/labzfs, 800GB)
        ├─ 네트워크 lxdbr0 = 10.200.0.0/24
        └─ VM  netlab-xxxxxxxx  (2 vCPU / 4GB / 50GB, IP 10.200.0.20~199)
              └─ Ubuntu 24.04 + Docker + containerlab + 랩 리포 + ttyd
```

## 동작 방식

- 환경은 **미리 만들어 두지 않는다.** 본인이 "시작"을 누를 때 발급된다.
- 체감 대기를 없애려고 **예비 VM 을 1대 미리 부팅**해 둔다(웜 풀). 요청이 오면 즉시 배정하고
  백그라운드로 다시 채운다. → 예비가 있으면 **약 1초**, 없으면 **약 30초**.
- **"종료"는 파기가 아니라 정지다.** 작업 내용이 남고, 다시 시작하면 이어서 쓴다.
- **정지된 채 7일**(기본, 변경 가능)이 지나면 자동 회수(파기)한다.
  **실행 중인 환경은 절대 자동 파기하지 않는다** — 쓰고 있는 사람의 작업을 지우지 않기 위해서다.
- 관리자는 언제든 강제 정지·회수할 수 있고, 회수 기간과 예비 대수를 콘솔에서 바꾼다.

## 인증 구조

접근 경로가 둘이고, 각각 인증 방식이 다르다.

| 경로 | 리스너 | 인증 | 용도 |
|---|---|---|---|
| `https://netlab.gnu.or.kr/`<br>`https://netlab.modus.ai.kr/` | `127.0.0.1:8088` | Cloudflare Access (JWT 검증) | 평소 사용 |
| `http://<호스트>:8088/` | `0.0.0.0:8088` | basic auth | 사내망 · **비상 경로** |

nginx 는 더 구체적인 listen 주소를 우선하므로, cloudflared 가 붙는 loopback 트래픽은
Access 블록으로, 그 외는 basic auth 블록으로 간다.

### 왜 이메일 헤더를 믿지 않는가

Access 는 통과한 요청에 `Cf-Access-Authenticated-User-Email` 을 실어 준다. 그런데 nginx 는
`0.0.0.0` 에서 듣고 있어 사내망에서 이렇게 위조할 수 있다.

```bash
curl -H 'Cf-Access-Authenticated-User-Email: admin@…' http://<호스트>:8088/admin/
```

그래서 이메일 헤더가 아니라 **Cloudflare 가 서명한 `Cf-Access-Jwt-Assertion` 을 검증**한다.
서명(RS256, Cloudflare 공개키) · 만료 · 발급자 · **AUD 태그**까지 본다. AUD 를 보므로 같은 팀의
다른 Access 앱 토큰도 통과하지 못한다. 검증은 `labd` 가 하고, 공개키는 1시간 캐시한다.

확인된 결과: 이메일 헤더 위조 → 401, 가짜 JWT → 401, 진짜 Access 토큰 → 통과.

### 권한 판정은 한 곳에서만

nginx 는 `auth_request` 로 labd 의 `/_access`(일반) · `/_access_admin`(관리자) 를 호출하고
**200/401/403 만 본다.** 토큰 검증, 계정 매핑, 타인 경로 차단, 차단 계정 거부가 전부 labd 에
모여 있다. 정책을 nginx 조건문에 흩뿌리면 경로가 늘어날 때 구멍이 생긴다.

### 계정은 처음 로그인할 때 만들어진다

누가 들어올 수 있는지는 **Cloudflare allow list** 가 정한다. 거기를 통과한 사람은 처음
접속하는 순간 계정이 생긴다. 계정명은 이메일 앞부분(`kim@gnu.or.kr` → `kim`)이고 충돌하면
숫자를 붙인다.

새 계정은 항상 일반 사용자다. 최초 관리자도 먼저 Access로 로그인한 뒤
`sudo netlab-user list`로 실제 계정명을 확인하고 `sudo netlab-user role <계정> admin`으로 승격한다.

### 사용자 테이블

`/var/lib/netlab/users.json` — 이메일, 역할, 생성·최종 로그인 시각, 차단 여부.
`/var/lib/netlab/visits.json` — 계정별 최근 접속 시각. labd 가 본 마지막 사용자 요청(패널·상태 폴링·API)으로,
Access·LAN·guest 모두 기록되며 계정당 60초에 한 번만 저장한다. 관리자 콘솔의 "최근 접속" 은 이 값과
`users.json` 의 최종 로그인 중 늦은 쪽이다.

```bash
sudo netlab-user list                  # 이메일·역할까지 표시
sudo netlab-user role   <계정> admin   # 관리자 지정 (해제는 user)
sudo netlab-user block  <계정>         # 차단 — Access 를 통과해도 거부
sudo netlab-user unblock <계정>
```

Access 계정은 이메일이 매핑된 뒤 역할을 지정한다. 이메일 없는 계정을 미리 만들면
최초 로그인 때 별도 계정이 생길 수 있다.

일반 사용자가 `/admin/` 을 찔러도 **403** 이다. 인증됐다는 것과 관리자라는 것은 따로 판정한다.

### 비상 경로

Cloudflare 나 터널에 장애가 나도 사내망에서 들어갈 수 있도록 basic auth 경로를 남겨 두었다.

```
http://<호스트>:8088/admin/     아이디 admin, 비밀번호는 netlab-user admin 으로 재설정
```

이 경로를 없애려면 `/etc/netlab/admin.htpasswd` 를 지우고 nginx 를 reload 하면 된다.
다만 그러면 Cloudflare 장애 시 콘솔에 못 들어간다(CLI 는 계속 쓸 수 있다).

Access 로 자동 생성된 계정은 비밀번호가 없으므로 **사내망 경로에는 노출되지 않는다.**
사내망에서도 쓰게 하려면 `netlab-user passwd <계정>` 으로 비밀번호를 주면 된다.

## 팀원 안내 문구

> 1. `https://netlab.gnu.or.kr/` 또는 `https://netlab.modus.ai.kr/` 접속 → **Cloudflare 인증 한 번**이면 본인 패널
>    (계정은 처음 로그인할 때 자동으로 만들어집니다)
> 2. **시작** 을 누르면 전용 실습 환경이 발급됩니다 (보통 몇 초)
> 3. **브라우저 터미널 열기** → 바로 실습
>    ```
>    lab 01        # 01장 배포
>    n h1          # h1 노드 안으로 들어가기
>    unlab         # 철거
>    ```
> 4. 단축 명령 전체는 터미널에서 `help-lab`
> 5. 각 챕터 설명은 `doc 01`, 해설은 `walk 01`
> 6. 다 쓰면 **일시 정지** — 작업 내용은 남습니다. 다음에 이어서 하면 됩니다
> 7. 환경이 꼬이면 터미널에서 `lab-reset`, 그래도 안 되면 패널의 **초기화**

## 터미널 단축 명령

터미널에서 `help-lab` 로 언제든 다시 볼 수 있다. 정의는 `/etc/lab-shell.sh`.

| 명령 | 하는 일 |
|---|---|
| `labs` | 실습 목록 |
| `lab 01` | 01장 토폴로지 배포 (`lab 17 bgp` 처럼 토폴로지 지정 가능) |
| `unlab` | 실행 중인 실습 철거 |
| `nodes` | 실행 중인 노드 목록 |
| `n h1` | h1 노드 안으로 들어가기 |
| `n h1 ip -br addr` | h1 에서 명령만 실행 |
| `pingfrom h1 10.10.10.2` | 노드에서 ping |
| `doc 01` / `walk 01` | 설명 / 해설 문서 |
| `ipa` `ipl` `ipr` `ipn` | 주소·링크·라우팅·ARP 요약 |
| `fdb` `vlans` `ports` `ct` | MAC 학습·VLAN·열린포트·conntrack |
| `tcpd eth1` | 패킷 캡처 |
| `ll` `la` `l` `..` `...` | 기본 파일·이동 |

터미널 색상은 패널·문서와 같은 계열(짙은 슬레이트 + 청록)로 맞춰져 있고,
프롬프트는 일반 사용자면 청록, **root 면 빨강**으로 바뀌어 권한을 눈으로 구분한다.
설정은 `/usr/local/bin/ttyd-lab` 에서 바꾼다.

## 세션 기록과 채점

실습 세션은 두 갈래로 기록되어 나중에 복습·피드백에 쓸 수 있다.
로그인 배너에 기록된다는 사실을 명시해 두었다.

| 파일 | 내용 |
|---|---|
| `*.cast` | 화면 전체 녹화(asciinema). **`docker exec` 로 노드에 들어가서 친 명령까지** 잡힌다 |
| `*.commands.jsonl` | 바깥 셸의 명령 — 시각·작업 디렉터리·**종료 코드**·노드 진입 이벤트 |

기록은 VM 안(`/var/log/lab-sessions`)에 쌓이고, labd 가 **주기적으로 그리고 정지·회수 직전에**
호스트(`/var/lib/netlab/recordings/<계정>/`)로 옮긴다. 환경이 파기돼도 기록은 남는다.

```bash
sudo netlab-record list [계정]              # 세션 목록
sudo netlab-record collect [계정]           # 지금 즉시 회수
sudo netlab-record transcript <계정> [세션]  # 읽을 수 있는 텍스트
sudo netlab-record grade <계정> [세션]       # LLM 채점용 묶음
sudo netlab-record purge <계정> --older-days 90
```

`grade` 는 채점 지시문 + 명령 타임라인 + 화면 기록을 하나로 묶어 출력한다. 그대로 LLM 에 넣으면 된다.

```bash
sudo netlab-record grade kim > kim.md      # 파일로 받아 LLM 에 첨부
```

**한계**: 화면 기록은 ANSI 제어문자를 걷어낸 텍스트다. 화면을 갱신하며 그리는 프로그램
(전체 화면 TUI 등)은 출력이 어긋날 수 있다. 이 실습은 대부분 줄 단위 출력이라 실용적으로 충분하다.

**용량**: 세션당 보통 수 KB~수십 KB지만 `tcpdump` 를 오래 돌리면 커진다. 주기적으로
`netlab-record purge` 로 정리한다.

## 운영 작업

### 정책 변경
관리자 콘솔(`/admin/`)에서 자동 회수 기간(일)과 예비 대수를 바꾼다.
파일로도 가능: `/etc/netlab/config.json` 수정 후 `systemctl restart labd`.

### 골든 이미지 갱신 (랩 리포가 업데이트됐을 때)
```bash
lxc launch netlab golden --vm -c limits.cpu=8 -c limits.memory=8GiB -d root,size=50GiB
lxc exec golden -- bash                      # 원하는 변경 수행
lxc exec golden -- bash -c 'truncate -s 0 /etc/machine-id; rm -f /etc/ssh/ssh_host_*; apt-get clean'
lxc stop golden
lxc publish golden --alias netlab --reuse
lxc delete golden
```
이미 발급된 환경에는 반영되지 않는다. 새로 발급되는 환경부터 적용된다.

학습 팩 v2 실행기를 담은 이미지는 위 수동 절차 대신 `packctl.py image`로 **새 별칭**에 만들고
`packctl.py activate`로 연결한다. 기존 별칭을 `--reuse`로 덮어쓰지 않는다. 절차·긴급 중지·되돌리기는
[서버 안내](README.md#학습-팩-v2-시범m1m2m3)에 있다.

### 포트 변경
포털은 **8088** 을 쓴다 (80 은 다른 용도로 비워 둔다). 바꾸려면 한 곳만 고치면 된다.

```bash
sudo sed -i 's/listen 8088/listen 9080/' /etc/nginx/sites-available/netlab
sudo nginx -t && sudo systemctl reload nginx
```

`netlab-user` 가 안내하는 접속 주소는 이 설정에서 포트를 읽어 만들므로 자동으로 따라온다.
labd 가 만드는 라우팅은 경로만 다루고 포트를 쓰지 않아 손댈 필요가 없다.

### 상태 점검
```bash
sudo netlab-user list                 # 계정·환경 현황
sudo lxc list                         # 실제 인스턴스
sudo zpool list labpool               # 저장소 사용률
sudo journalctl -u labd -f            # 발급/회수 로그
```

### 백업된 골든 이미지
`/var/tmp/netlab-backup/netlab.tar.gz` — LXD 를 다시 깔아야 할 때 여기서 복원한다.
```bash
lxc image import /var/tmp/netlab-backup/netlab.tar.gz --alias netlab
```

## 보안 조치 (2026-08-12 감사 대응)

독립 감사에서 나온 항목을 조치했다. VM 간 격리·KVM 경계는 원래 튼튼했고, 문제는 전부
"VM 바깥"이었다.

**격리 강제는 `netlab-netguard.service` 한 곳에 모여 있고 부팅·서비스 재시작·방화벽
플러시에도 자립적으로 복구된다** (규칙 전부 삭제 후 서비스 재실행만으로 복구 검증 완료).

| 조치 | 내용 |
|---|---|
| VM↔VM 차단 | 브리지 포트 격리(`isolated on`). 같은 브리지 통신은 netfilter 를 안 타므로 방화벽·ACL 로는 못 막는다. labd 와 netguard 이중으로 건다 |
| VM→호스트 차단 | `INPUT -i lxdbr0` REJECT (DNS·DHCP·ICMP·ESTABLISHED 만 허용) |
| VM→물리 LAN 차단 | `FORWARD -i lxdbr0 -d <사설망>` REJECT. 인터넷(공인 IP)은 명시 허용. 공유기·NAS·다른 PC 피벗 차단 |
| IPv6 인터넷 노출 차단 | LXD API 를 loopback(`127.0.0.1:8443`)로. IPv6 신규 SSH(22)·LXD(8443) 인바운드 REJECT. 관리자는 IPv4 로 접속 |
| SSH 하드닝 | `PasswordAuthentication no` (키 전용). cloud-init 이 되돌리지 않게 `50-cloud-init.conf` 도 수정 |
| fail-open 권한상승 차단 | `users.json` 손상 시 fail-closed(로그인 거부). "최초 사용자 admin" 자동부여 제거. common_name fallback 제거 |
| 녹화 tar DoS 차단 | 게스트 tar 를 root 가 파싱하던 것을 이름 화이트리스트 + 크기 상한 `lxc file pull` 로 교체. labd·CLI 양쪽 |

**주의: FORWARD 를 함부로 플러시하지 말 것.** docker 가 건 FORWARD 점프가 사라져 VM
인터넷이 끊긴다. netguard 가 `lxdbr0` forward 를 명시 허용해 이 경우에도 복구하지만,
docker 재시작이 필요한 상황이면 GPU 컨테이너 영향을 고려할 것.

**남은 위험 (사용자 확인 필요):**
- `netlab.gnu.or.kr` 외에 공유기에서 8088 이 외부로 포워딩돼 있으면 Access 우회 가능 →
  공유기 설정 확인
- IPv6 SSH 를 방화벽으로 막았지만 상단 공유기가 inbound IPv6 를 포워딩하는지는 별개 →
  LTE(WiFi 끄고) 로 `ssh -6 ...` 시도해 확인 권장

## 남은 일

8월 개발 당시의 목록이다. 현재 튜터에는 일부 챕터의 상태 검사·도움 기능이 있지만
전체 커리큘럼의 자동 채점이나 진도 추적이 완성된 것은 아니다.

세션이 끝나도 잊지 않도록 여기에 적어 둔다.

### 1. 사용자 작업 백업 — NAS 연동 (우선순위 높음)

지금 있는 ZFS 스냅샷은 **초기화용(`clean`)이지 백업이 아니다.** NVMe 하나가 죽거나 zpool 이
깨지면 팀원 전원의 진행 중 작업이 동시에 사라진다. 골든 이미지 백업
(`/var/tmp/netlab-backup/netlab.tar.gz`)조차 같은 디스크에 있어 함께 날아간다.

"종료해도 작업이 남는다"고 약속해 놓고 그 약속을 지킬 장치가 없는 상태다.

- 대상: 실습 VM 디스크 + 골든 이미지 + `users.json`·`state.json`·녹화
- 방법 후보: `zfs send/recv` 로 NAS 전송, 또는 `lxc export` 주기 실행
- 주기: 실행 중 환경은 하루 1회, 정지 환경은 회수 직전 1회면 충분

### 2. 상태 감시 — 관리자 콘솔에 추가됨 ✅ (2026-08-12)

관리자 콘솔(`/admin/`) 상단에 시스템 상태 패널을 넣었다. 20초마다 갱신하고, 경고·위험이
있으면 상단에 배너로 요약한다. labd 의 `/admin/health` 엔드포인트가 수집한다.

감시 항목 9가지: 서비스(7개 systemctl), 저장소(ZFS health·사용률), 루트 디스크, 메모리,
부하, Cloudflare 터널 연결 수, 외부 라우팅(netlab 호스트명), 웜 풀 준비 대수, 녹화 보관 용량.
임계치를 넘으면 ok→warn→crit 로 색이 바뀐다(예: ZFS 80%↑ warn, 90%↑ crit).

검증: 서비스를 멈추면 crit 로 어느 서비스인지까지 표시, 복구하면 ok 전환. 응답 0.09초.
외부 알림(메일·슬랙)은 다음 단계로 남겨둔다.


### 그 밖에 (평가에서 나온 것)

- **10명 동시 부하 시험** — 실제 검증은 3명까지만 했다. 교육 세션은 다 같이 시작하므로
  가장 있을 법한 시나리오가 가장 검증이 덜 됐다. 첫 세션 전에 확인할 것.
- **채점 자동화** — 녹화는 되지만 채점이 수동 복사·붙여넣기다. 자동화가 없으면
  한두 번 하고 안 쓰게 된다.
- **커리큘럼·진도 추적** — 환경은 있지만 "누가 어디까지 했는지"를 아무도 모른다.
  체크포인트(CP-1, CP-2) 통과 여부도 추적되지 않는다.
- **녹화의 프레이밍** — 감시가 아니라 피드백이라는 합의를 먼저 할 것. 팀원이 감시로
  받아들이면 안전하게 실패하는 걸 피하게 되고, 그러면 이 랩의 학습 효과가 무너진다.

## 주의사항

**저장소를 ZFS 로 쓰는 이유.** 처음에는 LVM thin pool 로 구축했는데, VM 을 반복 생성·삭제하던
중 **thin pool 내부 메타데이터가 LVM 볼륨 그룹 메타데이터와 어긋나** 새 볼륨 생성이 전부
실패했다(`already contain thin device with device_id N`). 겉으로는 `No space left on device`
로 보이지만 디스크 공간과 무관한 문제다. 이 시스템은 VM 을 계속 만들고 지우는 것이 본업이라
같은 사고가 재발하면 서비스 전체가 멎는다. ZFS 는 복제·스냅샷이 원자적이라 이런 desync 가
구조적으로 생기지 않고, 순차 8회 / 동시 6개 / 스냅샷 복원 반복 시험을 실패 0건으로 통과했다.

**ZFS 캐시 상한.** 이 호스트는 RTX 3090 ×2 로 vLLM 을 상시 운영한다. ZFS 는 기본적으로
RAM 절반을 캐시로 잡으므로 `/etc/modprobe.d/zfs.conf` 에서 `zfs_arc_max=8G` 로 제한해 두었다.
이 값을 지우면 ZFS 가 60GB 넘게 가져가 GPU 워크로드와 경쟁한다.

**Docker 와 LXD 방화벽 충돌.** Docker 가 `FORWARD` 정책을 `DROP` 으로 바꿔 lxdbr0 트래픽을
죽인다. `lxd-docker-fix.service` 가 `DOCKER-USER` 체인에 예외를 넣고 Docker 재시작 시 자동
재적용한다. VM 에서 인터넷이 안 되면 여기부터 본다:
```bash
sudo iptables -L DOCKER-USER -n -v | grep lxdbr0    # -v 없으면 인터페이스가 안 보인다
```
**LXD 를 재설치하면 이 서비스를 반드시 다시 실행**해야 한다 (`systemctl restart lxd-docker-fix`).

**IP 는 10.200.0.20~199 에서 배정된다.** VM 내부의 containerlab 관리망(`10.99.99.0/24`)이나
호스트 Docker 대역(`172.17/16`, `172.18/16`)과 겹치지 않는다. 커널이 분리되어 있어 여러 명이
같은 `10.99.99.0/24` 를 동시에 써도 충돌하지 않는다.

**labd 는 root 로 돈다.** `lxc` 실행과 nginx 설정 작성에 필요하다. `127.0.0.1:9000` 에만
바인딩되어 nginx 인증을 통과하지 않으면 도달할 수 없고, 사용자 신원은 nginx 가
`proxy_set_header` 로 덮어쓰므로 위조할 수 없다.

**외부 노출 경로.** `netlab.gnu.or.kr` 은 Cloudflare Tunnel 로 공개돼 있고 앞단에
**Cloudflare Access** 가 걸려 있다. Access 를 통과하지 못하면 오리진에 도달조차 못 한다.
TLS 는 Cloudflare 엣지에서 종단되고 터널 구간도 암호화되므로, 평문 구간은 이 장비 안의
loopback(`localhost:8088`) 뿐이다. 인증 상세는 위 「인증 구조」 참고.

터널 설정은 **원격 관리형**이라 로컬 파일이 아니라 Cloudflare 대시보드에 있다
(Zero Trust → Networks → Tunnels → Public Hostname). 현재 규칙:
`netlab.gnu.or.kr` · `netlab.modus.ai.kr` 둘 다 `→ localhost:8088` (같은 Access 앱, AUD 동일).

터미널은 WebSocket 인데 ttyd 가 5초마다 핑을 보내(`--ping-interval` 기본값)
Cloudflare 가 유휴로 끊지 않는다. nginx 쪽 `proxy_read_timeout` 도 86400초다.

**LXD 웹 UI** 는 `https://<호스트>:8443/ui/` 에서 쓸 수 있다. 브라우저 등록 토큰은
`sudo lxc config trust add --name admin-browser` 로 발급한다.
