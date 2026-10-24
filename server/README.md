# Netlab 서버 — 상세 설치·배포·백업 안내

처음 사용하는 학습자·설치자는 [Netlab 플랫폼 README](../README.netlab.md)부터 읽는다.
이 문서는 서버 설치·업데이트·백업의 세부 절차를 다룬다.
현재 개발 VM의 접속 주소, 소스 전달, 배포, 검증, 롤백은
개발기 접속 주소·배포 절차는 내부 문서 `server/local/DEVELOPMENT.md`에 둔다. `server/local/`은 Git에 넣지 않으므로 운영자에게 직접 받는다.

LXD VM을 발급하여 브라우저에서 네트워크 실습을 제공하는 서버 소스다.
기본은 인증 없는 공용 guest 환경이며, 선택적으로 인증 기반 개인별 환경을 제공한다.
루트의 `00-*`~`17-*`는 실습 콘텐츠이고 `server/`는 이를 서비스하는 관리 프로그램이다.
기존 서비스 주소는 `netlab.gnu.or.kr`, `netlab.modus.ai.kr`이다.

개발·운영 앱의 컨테이너화와 호스트 권한 분리는
[문제 분석과 리팩토링 요청서](REFACTORING_REQUEST.md)에 제안 범위와 완료 기준을 정리했다.

## 기능과 구성

개발·운영 앱의 컨테이너화와 호스트 권한 분리는
[문제 분석과 리팩토링 요청서](REFACTORING_REQUEST.md)에 제안 범위와 완료 기준을 정리했다.

- 전용 VM 발급·시작·일시 정지·초기화·영구 종료, 예비 풀과 자동 회수
- 기본 공개 웹 접속, 선택적 Cloudflare Access JWT 검증·이메일 계정 매핑·역할 관리
- 관리자 상태 감시, ttyd 브라우저 터미널
- 녹화·리플레이·명령 타임라인, LLM 채점용 자료 추출
- 일부 챕터의 상태 검사 및 LLM 도움말 (전체 자동 채점은 미완성)

```text
기본: 브라우저 → nginx 서버IP:8088 → 공용 guest 실습 환경

선택적 Access 구성:
브라우저 → Cloudflare Access → Tunnel → nginx 127.0.0.1:8088
                                          ├─ labd 127.0.0.1:9000 → LXD
                                          └─ 사용자 VM ttyd :7681
```

| 저장소 | 설치 위치 / 역할 |
|---|---|
| `labd.py`, `netlab/` | `/opt/netlab-admin/`, HTTP 진입점과 설정·상태·VM 수명·nginx·녹화·유지작업 모듈 |
| `templates/` | `/opt/netlab-admin/templates/`, 사용자 패널·관리자 콘솔 HTML |
| `netlab-user`, `netlab-record`, `netlab-backup` | `/usr/local/bin/`, 계정·녹화·암호화 백업 CLI |
| `netlab_tutor.py`, `tutor_chat.py`, `packs.py`, `runtimes.py`, `assets/` | `/opt/netlab-admin/`, 도움말·튜터·재생기 |
| `knowledge/packs.json`, `knowledge/corpus.json` | `/opt/netlab-admin/knowledge/`, 학습 팩 메타데이터와 교재 원문(빌드 산출물) |
| `nginx-netlab.conf` | 비밀값 없는 템플릿; 실제 사이트는 `/etc/nginx/sites-available/netlab` |
| `systemd/` | labd·VM 격리·Docker/LXD 방화벽 서비스 |
| `lab-shell.sh`, `lab-session`, `ttyd-lab`, `apply-shell.sh` | 골든 VM 내부에 설치하는 게스트 구성 |
| `config/*.example.json` | 설정 예제; 실제 설정은 호스트 `/etc/netlab/` |

사용자 DB, 상태, 녹화, 비밀번호 해시, Tunnel 토큰, 프록시 시크릿, LLM 키는 Git에 넣지 않는다.
실제 값은 호스트에서 `sudoedit`로 편집한다. Nginx 템플릿의
`__NETLAB_PROXY_SECRET__`를 실제 값으로 바꾸어 커밋하지 않는다.

## 기존 운영 서버 업데이트

로컬 검증:

```bash
python3 server/deploy.py --source-check
python3 -m venv /tmp/netlab-check-venv
/tmp/netlab-check-venv/bin/pip install -r server/requirements-community.txt
/tmp/netlab-check-venv/bin/python -m unittest discover -s server/tests -v
```

검토한 저장소를 운영 서버로 복사하거나 clone한 뒤 실행:

```bash
sudo python3 server/deploy.py --check
sudo python3 server/deploy.py --apply
```

배포할 소스는 커밋된 상태여야 한다. tar 로 옮길 때는 보내기 전에 저장소에서 `python3 server/deploy.py --stamp` 를
실행한다(커밋·태그와 조상 커밋 목록이 `.release-source.json` 에 남는다). `--check`·`--apply` 는 운영의
`release-manifest.json` 기록을 보고 다음이면 멈춘다. 운영 코드가 미커밋 변경으로 배포돼 있을 때, 운영 커밋이 새 소스에
포함되지 않을 때, 새 소스 자체에 미커밋 변경이 있을 때다. 운영에만 있는 변경을 덮어써 잃지 않게 하려는 것이다.
그 변경을 먼저 병합한 뒤 배포하고, 이전 커밋으로 되돌리는 경우처럼 의도한 때만 `--allow-source-overwrite` 를 붙인다.

`--apply` 는 서비스 상태 확인 뒤 라이브 사이트의 server 블록마다 사용자 경로(`/u/<계정>/ctl/status`)를 자격 증명
없이 불러 본다. 인증이 걸린 위치는 401, 공개 모드는 200 만 정상이다. 그 밖의 응답(5xx·404·nginx 자체의 403)이면
사용자별 nginx 위치나 인증 하위 요청이 깨진 것으로 보고 이전 코드로 되돌린다.

### Nginx 사이트의 auth_request 헤더 갱신 (2026-10 이후 필수)

labd는 요청 URI 대신 사용자별 내부 인증 location이 고정해서 보내는 대상 계정 헤더로 환경 소유자를 판정한다.
인증 URI는 `/_netlab_access_계정`이며, 쿼리나 클라이언트 헤더로 대상을 선택하지 않는다.
`auth_request /_access?target=...` 방식은 nginx가 쿼리를 경로로 취급해 404와 500을 일으키므로 사용하지 않는다.
운영 사이트(`/etc/nginx/sites-available/netlab`)가 옛 템플릿(`X-Original-URI`)이면 `--check`가
실패하고, labd도 Access 요청을 모두 거부한다(검사를 건너뛰어 열어 두지 않는다). 배포 전에 한 번 갱신한다.

```bash
sudo cp -p /etc/nginx/sites-available/netlab /etc/nginx/sites-available/netlab.bak
sudo sed -i 's|^\( *\)proxy_set_header X-Original-URI [$]request_uri;|&\n\1proxy_set_header X-Lab-Target-User "";\n\1proxy_set_header X-Lab-Auth-Scheme target;|' /etc/nginx/sites-available/netlab
sudo grep -c 'X-Lab-Auth-Scheme target' /etc/nginx/sites-available/netlab   # 2 가 정상
sudo nginx -t && sudo systemctl reload nginx
```

옛 `X-Original-URI` 줄은 새 labd 가 무시하므로 배포 중에 남아 있어도 되며, 배포가 끝난 뒤 지워도 된다.
이 순서(사이트 갱신 → labd 배포)면 옛 labd 와 새 labd 모두 자기 헤더를 받아 중단 구간이 없다.
사용자별 스니펫(`/etc/nginx/netlab.d/*-users.conf`)은 labd 가 기동할 때 다시 생성하므로 손대지 않는다.
실제 nginx의 서브요청·헤더 전달 회귀 검증은 격리된 임시 서버로 실행한다.
다음 명령에는 nginx 실행 권한이 필요하며 운영 사이트·VM·JWT를 변경하지 않는다.

```bash
sudo python3 server/tests/integration_nginx_auth.py
```

기본 동작은 읽기 전용 검사다. `--apply`는 의존성·JSON·LXD 이미지·Nginx를 검사하고,
labd와 채팅 서비스를 정지한 뒤 `/var/backups/netlab/release-*`에 코드·CLI·서비스 unit을 백업한다.
코드 교체 후 두 서비스를 시작하고 인증 없는 관리자 요청의 403, 채팅 헬스 및 서비스 상태를 검사한다.
실패하면 이전 실행 파일을 복구하고 재시작한다.

기존 `/etc/netlab`, 사용자 DB, VM, 녹화, 실제 Nginx 사이트와 방화벽 unit은 보존된다.
Docker와 VM은 재시작하지 않는다. labd 시작 시 기존 동작대로 라우팅 스니펫 재생성과
nginx reload가 일어난다. 관리 API는 잠깐 중단되고 평소의 자동 정지·회수 정책은 다시 동작한다.
이 백업은 코드 롤백용이며 VM/사용자 데이터 백업을 대체하지 않는다.

성공한 배포를 나중에 되돌리려면 labd와 채팅 서비스를 정지하고 해당 backup의 `code/`,
서비스 unit 및 CLI를 원래 위치에 복원한다. 이후 `systemctl daemon-reload`와
`systemctl start labd`를 실행하고, 백업에 채팅 unit이 있으면 채팅 서비스도 시작한다.
채팅 서비스 도입 이전으로 되돌릴 때는 새 채팅 서비스를 비활성화한다.
자동 롤백 시 새로 추가된 비실행 파일은 남을 수 있다.

## 새 Ubuntu 서버 자동 설치

Ubuntu 24.04 **x86_64**, `/dev/kvm`, 인터넷 연결이 필요하다. VM 위에 설치하면
중첩 가상화를 먼저 켠다. 기본 ZFS 풀 100GiB와 추가 여유 10GiB 이상을 준비한다.
호스트에 Docker를 설치하지 않는다. Docker는 학습자 VM 내부에 설치된다.

저장소 루트에서 실행한다:

```bash
# 변경 없이 요구사항과 실행 계획 확인
sudo python3 server/bootstrap.py

# 기본: 인증 없이 공용 guest 실습 환경 제공
sudo python3 server/bootstrap.py --apply
# 브라우저: http://서버IP:8088

# 선택: Cloudflare Access 인증 사용
sudo python3 server/bootstrap.py --apply \
  --access-team-domain YOUR_TEAM.cloudflareaccess.com \
  --access-aud YOUR_APPLICATION_AUD \
  --admin-email operator@example.com
```

자동으로 수행하는 작업:

1. 호스트 의존 패키지와 LXD 5.21 stable 설치
2. ZFS 파일 기반 `labpool` 생성 (물리 디바이스 포맷 없음)
3. `lxdbr0` NAT 네트워크 및 포트 격리된 전용 `netlab` 프로필 생성
4. 호스트·LAN 접근 차단용 Netlab 전용 방화벽 체인 및 재적용 타이머 설치
5. 임시 Ubuntu VM에 Docker, ttyd, asciinema, 고정 버전 containerlab 설치
6. 현재 체크아웃의 실습 콘텐츠·셸·녹화 기능 설치
7. 실제 01장 배포 → h1/h2 ping → 철거 → 골든 이미지 `netlab` 발행
8. 설정·시크릿 생성, Nginx·labd 시작과 관리자 API 보호 검사

기본 설치는 `0.0.0.0:8088`에서 로그인 없이 접속할 수 있다 (`auth_mode: open`).
접속자는 공용 `guest` 계정의 VM·터미널·파일을 공유한다. 개인별 격리나 사용자 신원
검증은 제공하지 않으며 관리자 웹/API는 차단한다. 실습 시작·정지·초기화도 공유되므로
동시 사용에 주의한다. 신뢰할 수 있는 테스트망에서 사용하고, 인터넷 공개 전에 설치자가
방화벽·TLS·리버스 프록시·인증 정책을 선택한다. 외부 인증을 앞에 붙이는 것만으로
공용 guest 환경이 개인별 환경으로 바뀌지는 않는다.

Access 옵션을 지정하면 기존 JWT 기반 개인별 계정 모드(`auth_mode: protected`)로
설치한다. 이미 설치된 서버의 업데이트는 기존 인증 모드와 Nginx 설정을 보존하며,
옵션을 생략해 재실행해도 기존 보호 설정을 공개 모드로 바꾸지 않는다.

containerlab 기본 버전은 0.74.2이며 공식 릴리스 체크섬을 확인한다.
`--containerlab-version`으로 변경할 수 있다. Ubuntu 이미지와 apt 패키지는 설치 시점의
업데이트를 사용하므로 전체 비트 단위 재현 빌드는 아니다. 실습 YAML의 이미지 태그도
각 랩 정의를 따른다. 빌드된 이미지와 백업으로 배포 버전을 보관한다.

`--storage-size 40GiB`처럼 새 풀 크기를 바꿀 수 있다. 기존 풀의 크기는 변경하지 않는다.
대역 충돌이 있으면 `--subnet 10.210.0.1/24`처럼 /24 게이트웨이를 지정한다.
기존 같은 이름의 네트워크·프로필·스토리지가 호환되지 않으면 덮어쓰지 않고 중단한다.
기존 `netlab` VM 이미지가 있으면 재사용하며 alias를 교체하지 않는다.
이미 설치된 서버에서 다시 실행하면 기존 설정을 보존하는 코드 업데이트로 전환한다.

방화벽은 `NETLAB-IN`, `NETLAB-FWD` 체인만 관리한다. 다른 Docker 규칙을 비우지 않는다.
VM 프로필에도 `security.port_isolation=true`를 적용한다. 기존 운영에서 가져온
`netlab-netguard.service`, `lxd-docker-fix.service`는 호환용 보관 파일이며
자동 신규 설치에서는 사용하지 않는다. 신규 방화벽은 SSH 설정을 변경하지 않는다.

Cloudflare의 도메인/Tunnel/Access 정책은 계정 권한이 필요한 외부 설정이다.
기존 Tunnel에서 `http://127.0.0.1:8088`로 라우팅하고 Access로 호스트명 전체를 보호한다.
Access는 선택 기능이다. 공개 모드에서 전환할 때는 config의 `auth_mode`를 `protected`로
바꾸고 팀 도메인·AUD를 설정하며, Nginx 사이트도 `nginx-netlab.conf` 템플릿으로
교체해야 한다 (프록시 시크릿 치환 필수). 설정 한쪽만 바꾸면 안 된다.
관리자 이메일을 생략했다면 최초 로그인 후 `netlab-user role <계정> admin`으로 승격한다.
비상 Basic Auth 리스너는 새 설치에서 `127.0.0.2:8088`에만 바인딩된다.

실패 시 `/var/lib/netlab-bootstrap/`의 상태와 `guest-build.log`를 확인한다.
실패한 빌더 VM은 진단을 위해 남긴다. 이름은 `builder.json`에 기록한다.
재시도는 기존 자원을 재사용하고 새 빌더를 만들 수 있으므로 실패한 빌더의 정리는
관리자가 해당 이름을 확인한 뒤 한다. 운영 VM을 이름 패턴으로 일괄 삭제하지 않는다.
성공 시 이번 실행에서 생성한 빌더만 삭제한다.

### AI 헬퍼 설정

현재 패널의 AI 실습 튜터는 자유 질문과 후속 대화를 지원한다. `현재 상태 확인`과
`해결 내용 정리`를 누르면 등록된 진단을 실행하고, 출력과 수집 시각을 답변 옆에 표시한다.
일반 질문에서도 "방금 고쳤어", "ping이 안 돼", "현재 상태" 등 상태 확인 의도가 있으면
고정 진단을 새로 실행한다. 개념 질문은 추가 네트워크 검사를 생략한다.
우측 하단 AI 튜터 아이콘으로 열고 닫는다. 1400px 이상에서는 오른쪽에 420~840px 폭의
플로팅 패널을 표시하고 본문에 공간을 확보해 터미널과 겹치지 않게 한다. 좁은 화면에서는
터미널 아래에 펼치며, 닫으면 열기 전 스크롤 위치로 돌아간다. 열림 상태는 이 브라우저에 저장한다.
일반 질문은 VM의 상태를 변경하는 명령을 실행하지 않는다. 모델이 작성한 명령도 자동 실행하지 않는다.
00~17장의 진단 정보를 제공하며 17장은 OSPF/BGP를 구분한다.
00·01·02는 관찰형이고, 08·09는 증상 탐구형이므로 단일 성공/실패로 자동 채점하지 않는다.
나머지 장의 자동 검증은 등록된 통신·설정 조건만 확인하며 전체 과제나 개념 이해를 인증하지 않는다.

대화는 `/var/lib/netlab/tutor/chat.sqlite3`에 저장한다. 최근 12회 대화를 모델에 전달한다
(`history_turns`, 2~24회).
그 이전 대화는 가설·관찰·미확인 사항을 구분한 요약으로 유지하며, 요약을 검증 사실로 취급하지 않는다.
화면에는 최근 60회까지 표시하고 이 브라우저의 최근 20개 대화를 선택할 수 있다.
기본 보관 기간은 30일(`retention_days`, 1~365일)이다.
기한이 지난 완료 메시지는 다음 API 요청에서 정리한다. 실습 VM 초기화 후에는 새 대화를 시작한다.
컨테이너 배포 식별자가 바뀌면 이전 실행의 대화는 현재 모델 맥락에서 제외한다.
개방형 설치는 브라우저별 대화 토큰으로 대화를 나누지만 VM은 공용이다.

`최근 명령·화면 함께 전달`은 기본 해제다. 선택하면 최근 바깥 셸의 명령 기록과
asciinema 화면 출력 일부를 제한된 크기로 전달한다. 컨테이너 내부 화면도 포함될 수 있지만
어느 명령의 출력인지 정확히 구분하지 않으므로 확인된 명령·결과 쌍으로 취급하지 않는다.
필요한 출력은 질문에 붙여 넣을 수도 있다.
`terminal_context_enabled: false`로 서버에서 기록 공유를 비활성화할 수 있다.
모델에 전달할 기록에 비밀이 포함됐는지 확인해야 하며 자동 가림은 보조 수단이다.

기존 백업은 `/var/lib/netlab` 전체를 포함하므로 대화 DB도 포함한다.
백업 시 labd를 중지한 뒤 복사하는 기존 절차를 유지해야 한다.
대화 작업 중 서버가 재시작되면 중단 상태로 표시하고 자동으로 모델을 재호출하지 않는다.


튜터는 선택 기능이다. `config/tutor.example.json`을 호스트의
`/etc/netlab/tutor.json`으로 복사하고 endpoint/model/key 설정 및 권한 600을 적용한다.

저장소 루트에서 `sudo install -m 600 server/config/tutor.example.json /etc/netlab/tutor.json`을
실행한 뒤 `sudoedit /etc/netlab/tutor.json`으로 공통 설정을 편집한다.
모델별 접속 주소·키·노출 여부는 관리자 콘솔의 **AI 튜터 모델 관리**에서 관리할 수 있다.
모델 추가·편집·삭제, 사용자 노출/숨김, 기본 모델 지정을 지원한다.
숨긴 모델은 사용자 목록에서 제외하고 서버에서도 선택을 거부한다. 노출 모델이 없으면 튜터 질문을 사용할 수 없다.
삭제는 모델 선택 목록에서의 삭제이며 모델 서빙 프로세스나 이전 대화는 삭제하지 않는다.
API 키는 입력만 가능하고 조회 응답에는 포함하지 않는다. 편집 시 키를 비워두면 유지하고,
`저장된 API 키 제거`를 선택하면 해당 모델의 키를 비운다.
설정은 재시작 없이 다음 모델 선택·질문에 반영되며, 저장 전 설정을 `/etc/netlab/tutor-backups/`에 권한 600으로 백업한다.
동시 편집으로 설정이 바뀌면 저장을 거부하므로 목록을 새로고침하고 다시 확인한다.

튜터 상단 **모델 선택** 버튼에서 관리자가 허용한 모델을 선택할 수 있다.
`models`는 모델 ID를 키로 하는 객체이며 각 값에 `label`, `model`, `endpoint`,
필요 시 `api_key`와 추론 파라미터를 설정한다. `default_model`은 기본 ID다.
각 모델의 `enabled`는 사용자 노출 여부이며 생략하면 노출한다. 기본 모델을 숨기거나 삭제하면
남은 노출 모델 중 하나를 기본값으로 정하고, 전부 숨기거나 삭제하면 사용자 목록을 비운다.
예제에는 Qwen/Muse/Gemma 로컬 포트가 들어 있으며 실제 서빙 환경에 맞춰 수정한다.
`models`가 없는 기존 단일 모델 설정도 지원한다. 브라우저에는 ID와 표시명만 제공한다.
선택은 브라우저에 저장되고 다음 질문부터 적용되며, 대화는 유지하고 각 답변에 모델을 표시한다.
진행 중인 답변의 모델은 바뀌지 않는다. 모델 접속 주소·키는 클라이언트가 지정할 수 없다.
`max_tokens`는 추론과 최종 답변을 합친 생성 상한이다(예제 8192).
`request_timeout_seconds`로 모델 응답 대기 시간을 지정한다(예제 600초,
허용 범위 10~1800초, 생략 시 기존 기본값 90초). 입력과 출력을 합친 모델의
문맥 한도는 별도로 지켜야 하며, 생성 상한이 긴 답변을 강제하지는 않는다.
설정은 도움말 요청마다 읽으므로 변경 후 서비스 재시작은 필요하지 않다.
`endpoint`는 관리 서버에서 접근 가능한 Chat Completions URL을 사용한다.
API 키가 없는 로컬 모델은 `api_key`를 빈 문자열로 두거나 생략한다.
선택 항목 `top_p`, `top_k`, `reasoning_effort`는 설정한 경우에만 모델로 전달한다.
모델 서버가 지원하는 항목만 지정한다. 예를 들어 Qwen 추론 모델에는 `max_tokens: 4096`,
`temperature: 1.0`, `top_p: 0.95`, `top_k: 20`, `reasoning_effort: "low"`를 설정할 수 있다.
추론 토큰 때문에 최종 답변이 비면 토큰 한도와 추론 옵션을 확인한다.
학습자 화면에는 최종 `content`만 표시하며 내부 `reasoning`은 표시하지 않는다.
VM에서 호스트/LAN으로의 접근은 기본 차단되므로 별도 관리 VM에서 모델에 연결하려면
관리 서버→모델 API 경로만 허용해야 한다. `.local` 이름 해석도 해당 서버에서 확인한다.
`max_concurrent_jobs`는 동시 모델 작업 수(기본 4, 1~8)다. 같은 대화의 요청은 직렬 처리하고
같은 요청 식별자의 재전송은 중복 모델 호출을 만들지 않는다.
먼저 `lab 01`처럼 실습을 배포하면 튜터가 챕터를 감지한다. 배포 전에도 개념 질문은 가능하다.

### 접속자 채팅

화면 오른쪽 상단의 테마 버튼으로 라이트·다크 테마를 전환한다. 최초에는 운영체제 설정을 따르고, 직접 선택하면 해당 브라우저에 저장해 새로고침 후에도 유지한다. 테마 설정은 실습 VM이나 다른 사용자의 화면에는 영향을 주지 않는다.

터미널 상단의 테마 메뉴는 별도로 `기본 · Slate`, `Solarized Dark`, `Solarized Light (White)`, `Ubuntu · Aubergine`을 제공한다. 선택은 브라우저에 저장되며, ttyd의 `window.term.options.theme`에 적용하므로 iframe 재로드나 셸 재연결 없이 바뀐다. 전체화면을 새로 열 때도 선택한 색상을 전달한다. 이미 열린 독립 전체화면 탭과 녹화 리플레이의 테마는 자동 변경하지 않는다.

오른쪽 아래 `채팅` 버튼에서 전체 채팅방을 이용한다. 별도 가입 없이 서버가 로그인 계정명을 작성자로 지정하며 이메일은 공개하지 않는다. 게스트는 브라우저별 `guest-xxxxxx` 임시 이름을 사용한다. 접속 목록은 현재 WebSocket이 연결된 실습 페이지 기준이며, 같은 계정의 여러 탭은 한 명으로 집계한다. 관리자 화면에도 같은 목록을 표시한다. 전체화면 터미널만 열어둔 사용자는 이 목록에 포함되지 않는다.

최근 메시지 100개를 `/var/lib/netlab/community/chat.sqlite3`에 보관한다. 최초 연결·재연결 시 내역을 동기화하고 이후 새 메시지와 접속자 변화만 전달한다. 채팅창이나 탭이 숨겨져도 연결을 유지한다. 서버 Ping은 25초마다, Pong 대기는 20초이며 정상 연결 종료는 즉시 반영한다. 절전·네트워크 단절 감지는 지연될 수 있다. 메시지는 1000자, 계정당 10초에 5개·분당 30개, 전체 분당 120개로 제한한다(게스트는 브라우저 단위). 메시지는 DB 저장 후 전달하고, 전송 확인이 끊겨 재시도해도 동일 요청 ID는 중복 저장하지 않는다. 실행 중인 실습 VM이 없어도 사용할 수 있다. AI 튜터와 채팅창은 한 번에 하나만 열린다.

`netlab-community.service`는 loopback `127.0.0.1:9001`에서 실행한다. 기존 nginx·Tunnel의 `/community/ws` 경로를 사용하므로 외부 포트나 터널 추가는 없다. 기존 로그인 경로에서 발급한 5분 접속권을 첫 프레임으로 전달하고 만료 1분 전에 갱신한다. 접속권은 계정·브라우저·Origin에 결합되며 URL이나 로그에 넣지 않는다. 로그아웃·계정 비활성화는 갱신 실패 후 최대 5분 안에 반영된다. 관리자 관찰 연결은 학습자 수에 포함하지 않는다.

설치·업데이트는 `python3-venv`가 필요하다. bootstrap은 이를 설치하고, deploy는 `/opt/netlab-community-venv`에 고정된 `websockets==15.0.1`을 설치한다. 기존 서버에서 처음 전환할 때는 `sudo apt-get install python3-venv` 후 일반 `deploy.py --apply`를 사용한다. 접속자 상태는 메모리로 관리하고 서버 재시작 시 연결 재수립으로 복원한다. 기존 HTTP 채팅 API는 롤백 호환용으로 남지만 새 화면은 폴링하지 않는다.

#### 가이드 검색과 개념 Wiki

현재 장 README와 해당 토폴로지를 기본 문맥으로 제공한 뒤 SQLite FTS5로 관련 절을
검색한다. 별도 임베딩 서버나 벡터 DB는 필요하지 않다. 장 제목·절 제목·행 번호를
보존하며, 답변의 `[G1]` 같은 출처와 문서 이름 링크를 패널에서 확인할 수 있다. 내부 파일 경로와 원문 발췌는 출처 목록에 나열하지 않으며, 링크를 누르면 제목·문단·표·코드를 갖춘 HTML 문서로 열린다. 저장된 과거 대화의 출처도 같은 방식으로 표시한다.
문서의 초기 고장·설계와 현재 진단은 별도 자료로 전달하며, 실제 상태 판단에는 진단을 우선한다.

`WALKTHROUGH.md`는 구체적인 해결 방법·수정 명령·정답을 요청할 때만 검색 대상으로 넣는다.
README와 토폴로지 자체에도 초기 고장이나 명령 예시가 있으므로 이것은 정답의 보안상 은닉이
아니라 학습 안내 정책이다. 문서를 읽은 모델도 틀릴 수 있으므로 실제 진단과 출처를 함께 확인한다.

`knowledge/wiki/`는 원문을 연결해 정리한 개념 문서다. L2/ARP/VLAN, 왕복 라우팅,
NAT/연결 상태, MTU/터널, DNS/서비스 진단을 다룬다. 학습자의 대화나 추정을 자동으로
공통 Wiki에 기록하지 않는다. 원문과 일치하는지 검토한 뒤 Git으로 문서를 관리한다.

실습 원문이나 Wiki를 수정했다면 배포 전에 저장소 루트에서 다음을 실행한다.

### 학습 팩

학습 콘텐츠는 서버 코드가 아니라 **학습 팩**으로 들어온다. 팩은 매니페스트와 레슨 디렉터리의 묶음이고,
서버는 `server/build_knowledge.py`가 컴파일한 `knowledge/packs.json`·`corpus.json`만 읽는다.
팩이 가진 스크립트(토폴로지, `fix.sh` 등)는 학습자 VM 안에서만 실행되며 호스트는 레슨이 선언한
고정 argv 만 `lxc exec`로 실행한다.

| 파일 | 내용 |
|---|---|
| `pack.toml` (저장소 루트 또는 `packs/<id>/`) | 팩 id·제목, 런타임(`containerlab`), 교재·해설·정의 파일 이름 규칙, 튜터용 정답 숨김 패턴과 동의어 |
| `<레슨 디렉터리>/lesson.toml` | 레슨 id·번호·제목, 핵심 개념, 목표, 심어진 고장, 평가 방식(`fault`/`observe`/`exploration`), 채점 `checks`, 진단 `probes`, 단계식 `hints`, 검색 범위를 넓히는 `topics` |
| `server/runtimes.py` | 런타임 어댑터. 레슨을 올리고 내린 뒤 "지금 어떤 레슨이 떠 있는가", "노드에서 argv 실행" 을 런타임별로 구현한다 |

`checks` 는 `node`, `argv`, `expect = { contains | regex, stream = "stdout", rc = [...] }` 로 쓰며 모두
만족해야 통과다. 첫 번째 check 가 주 성공조건이고, 선택적인 `fallback` 은 주 조건이 맞지 않을 때 한 번
더 확인한다(예: PMTUD 의 경로 캐시). 토폴로지가 여러 개인 디렉터리는 `[[lessons]]` 로 레슨을 나누고
공통 항목은 파일 맨 위에 둔다. 레슨 id 는 팩 전체에서 유일해야 하며 HTTP API 와 튜터 DB 의
`chapter` 값으로 쓰인다. 레슨을 추가하거나 고친 뒤에는 아래 빌드를 다시 실행한다.

```bash
python3 server/build_knowledge.py
python3 server/build_knowledge.py --check
python3 server/deploy.py --source-check
```

생성물 `knowledge/corpus.json`에는 원문·문서별 SHA256·전체 버전이, `knowledge/packs.json`에는
학습 팩 메타데이터가 들어간다. 배포는 두 파일을 함께 복사하고 원문과 생성물이 불일치하면 검사를
실패시킨다. 모델이 표시한 출처는 해당
배포 버전의 원문이다. 검색 인덱스는 이 자료에서 메모리에 구성하므로 별도 운영 DB가 없다.
`guide_context_chars`는 검색 자료 전체 길이(기본 14000자, 4000~30000), `rag_top_k`는
기본 장 자료 외 추가 검색 절 수(기본 4, 1~6)다. 한국어는 형태소 분석 없는 키워드 검색과
개념 별칭을 사용하므로 질문에 따라 검색 누락이 있을 수 있다.

#### 학습 팩 v2 시범(M1·M2·M3)

03·12·17(OSPF/BGP)장과 shell 예제 팩(`packs/example-shell`)을 단계형 실습으로 제공하는 시범 경로다. 계약은 [구현 계약](../docs/PACK_IMPLEMENTATION_CONTRACTS.md),
순서는 [구현 절차](../docs/PACK_IMPLEMENTATION_PROCEDURE.md)를 따른다. `build_knowledge.py`가 v1 데이터와 함께
`knowledge/v2/catalog.json`을 만들고, 학습자 VM 안의 `netlab-runner`가 레슨을 실행한다. runner가 없는 VM에서는
레슨 API가 410(`path: legacy`)과 갱신 안내를 돌려주고, 패널의 단계형 실습 영역은 보이지 않아 기존 화면과 같다.

**이미지(M3).** 운영 VM은 runner를 담은 이미지에서 만든다. 이미지는 지금 설정된 이미지(또는 `--base`)에서
빌드 VM을 띄워 runner와 카탈로그의 모든 팩 번들을 설치하고, 해설이 있는 모든 레슨을 시작→해설 적용→확인(통과)→종료로
돌려 본 뒤 **새 별칭**으로 게시한다. 기존 별칭과 이미지는 바꾸거나 지우지 않는다. 실패하면 빌드 VM을 진단용으로 남긴다.
지문·기반 이미지·릴리스·runner 버전과 protocol·번들 맵·검증 결과는 `/var/lib/netlab/images/<별칭>.json`에 남는다.

```bash
python3 server/deploy.py --stamp                                                  # 저장소에서: 소스 기준 기록
python3 server/packctl.py bundle --pack network --out network-bundle.tar
python3 server/packctl.py bundle --pack example-shell --out shell-bundle.tar
# (서버 코드를 deploy.py 로 먼저 배포한 뒤) 관리 서버에서:
sudo python3 /opt/netlab-admin/packctl.py image --alias netlab-v2-YYYYMMDD \
    --bundle network-bundle.tar --bundle shell-bundle.tar --source <git 커밋>
sudo python3 /opt/netlab-admin/packctl.py activate --alias netlab-v2-YYYYMMDD
sudo python3 /opt/netlab-admin/packctl.py images                                 # 기록과 기반 이미지별 VM 수
```

`activate`는 기록이 있고, 별칭이 기록한 지문을 가리키며, 번들이 지금 서버 카탈로그와 같은 이미지만 설정
`image`로 연결한다. 그 뒤 labd는 이전 이미지로 만든 **미할당 예비 VM**을 배정하지 않고 새 이미지로 교체한다.
사용자에게 배정된 VM은 이미지가 달라도 그대로 둔다. 기존 사용자가 단계형 실습을 쓰려면 환경을 반납하고 새로
받아야 하며, 이때 VM 안 작업 파일은 사라진다. 일반 초기화는 같은 VM의 clean 스냅샷 복원이라 이미지를 바꾸지 않는다.
runner가 이미지에 들어 있으므로 초기화 뒤에도 runner가 남고 환경 식별자(incarnation)만 새로 바뀐다.
팩 내용이 바뀌어 번들이 달라지면 같은 번들로 이미지를 새로 만들어 연결한다. 이전 이미지의 VM은 번들 불일치를
표시하고 환경 교체를 안내한다. v1 실행 경로로 자동 우회하지 않는다.

**긴급 중지와 되돌리기.** `/etc/netlab/config.json`에 `"lessons_paused": true`를 넣고 `systemctl kill -s HUP labd`를
보내면 새 레슨 작업을 503으로 거절한다(조회·터미널·튜터는 유지). 문제가 있으면 먼저 접수를 멈추고 진행 중 작업을
확인한 뒤 `packctl.py activate --alias netlab --legacy`로 새 VM 발급을 이전 이미지로 되돌리고, 필요하면 `deploy.py`
백업으로 코드를 되돌린다. 진행 DB(`/var/lib/netlab/learning/progress.sqlite3`)·사용자 작업·VM은 지우지 않는다.

**개발 전용 설치(M1).** 이미지 없이 확인할 때는 개발 서버의 전용 검증 VM에만 runner를 넣을 수 있다. 서버 설정에
`"environment": "development"`가 있어야 하고, VM은 labd가 관리하는 미할당 예비 VM이며 `user.netlab.verify=true`
라벨이 있어야 한다. 이 설치는 clean 스냅샷 뒤에 들어가므로 그 VM을 초기화하면 runner가 사라진다.

```bash
sudo python3 /opt/netlab-admin/packctl.py install --instance <VM> \
    --bundle network-bundle.tar --bundle shell-bundle.tar                        # 개발 서버에서
sudo python3 server/tests/integration_lesson_v2.py --account <테스트 계정> [--with-cli] [--with-reboot] [--m2] [--m3]
```

runner가 있는 VM의 터미널에서는 `lab 12`, `lab 17 ospf`가 runner로 시작되고 웹 패널이 그 실습을 이어 받는다.
`lab check`는 참고용 확인이며 학습 기록은 웹의 확인 버튼으로만 남는다.

튜터 변경 검증은 `python3 -m unittest discover -s server/tests -q`로 실행한다.
LXD 관리 서버에서 별도로 만든 골든 이미지 검증 VM이 있다면
`python3 server/tests/integration_tutor.py --instance netlab-tutor-verify-YYYYMMDD`로
각 장을 배포·진단·수정·철거하며 확인한다. 이 명령은 해당 VM의 실습을 변경하므로
전용 VM 이름만 허용하며, 이미 실습이 실행 중이면 시작하지 않는다.
`--chapter 04`처럼 특정 장만 선택할 수 있다.

## 암호화 백업과 빈 서버 복원

root 전용 `netlab-backup` CLI를 제공한다. 공개 guest 웹에서는 호출할 수 없다.
GnuPG 대칭 AES-256 암호화를 사용하며 비밀번호는 터미널에서 입력한다 (12자 이상).
자동화는 root 소유·권한 600의 `--passphrase-file`을 사용할 수 있다.
백업과 비밀번호 파일은 별도의 안전한 장소에 보관한다. 비밀번호를 잃으면 복원할 수 없다.

```bash
sudo netlab-backup create --mode config --output /안전한백업경로/config.gpg
sudo netlab-backup create --mode full --output /안전한백업경로/full.gpg
sudo netlab-backup inspect /안전한백업경로/full.gpg
```

- `config`: `/etc/netlab`, `/var/lib/netlab`, Netlab Nginx 사이트. 사용자·권한·인증
  시크릿·호스트에 이미 수집된 녹화를 포함한다. VM 내부 파일과 미수집 녹화는 제외한다.
  복원 시 VM 할당 상태는 비운다. 대상에는 골든 이미지가 미리 있어야 한다.
- `full`: 위 파일 + 상태 파일에 등록된 학습자/예비 VM과 스냅샷 + 골든 이미지.
  labd를 멈추고 실행 중인 VM을 정상 종료한 뒤 내보낸다. 원래 실행 중이던 VM과 labd는
  내보내기 성공/실패 후 재시작한다. 강제 종료는 하지 않는다. 백업 중 터미널이 끊기므로
  사용자에게 미리 공지한다. 대용량 VM은 중단 시간이 길 수 있다.
- Netlab 밖의 VM, 호스트 전체 OS, Cloudflare 계정·Tunnel 자격증명, 외부 LLM 서비스,
  VM에 별도 마운트한 외부 디스크는 이 도구의 백업 범위가 아니다.
  추가 프로필·외부 디스크·커스텀 LXD 장치가 붙은 VM은 전체 백업을 거부한다.

SQLite DB(튜터 대화·채팅·공지·학습 진행)는 SQLite backup API로 만든 일관된 사본만 담고 실행 중인
WAL/SHM 파일은 섞지 않는다. `create`·`inspect`·`restore`는 모든 DB의 무결성과 외래키를, 학습 진행 DB는 회차·이벤트·
작업·완료 기록의 관계까지 검사하고 문제가 있으면 멈춘다. 복원된 진행 기록에서 실행 중이던 작업은 labd가 시작할 때
중단(interrupted)으로 표시하며 다시 실행하지 않는다.

임시 평문은 root 전용 작업 디렉터리에 만들어지고 종료 시 삭제된다. 삭제는 보안 소거가
아니므로 저장 매체 암호화를 권장한다. `--work-dir`으로 여유 있는 디스크를 선택한다.
내보낸 자료·tar·암호문·최종 출력이 공존하므로 백업 크기의 약 4배 이상 여유를 준비한다.
출력 파일을 덮어쓰지 않는다. 백업 중 다른 관리자가 LXD/계정/설정을 변경하지 않아야 한다.

### 전체 백업을 새 서버에 복원

동일한 코드 리비전을 checkout하고, `inspect`에 표시된 대역으로 빈 인프라만 준비한다.
현재 버전은 동일한 네트워크·프로필·스토리지 드라이버로 복원하며 IP 재매핑은 하지 않는다.
원본이 기존 운영용 `default` 프로필인 경우 해당 프로필도 동일하게 준비해야 한다.

```bash
# 예시 대역. 실제로는 inspect에서 확인한 원본 값을 사용한다.
sudo python3 server/bootstrap.py --apply --prepare-restore --subnet 10.210.0.1/24
sudo netlab-backup restore /안전한백업경로/full.gpg --dry-run
sudo netlab-backup restore /안전한백업경로/full.gpg
```

`--dry-run`은 복호화·무결성·호환성·충돌만 검사하며 서비스를 변경하지 않는다.
기존 계정/VM 할당/Netlab VM이 있거나 이미지 별칭이 다른 이미지를 가리키면 중단한다.
현재 버전은 사용 중인 서버 덮어쓰기나 병합 복원을 지원하지 않는다.

실행 직전 대상 파일을 `/var/backups/netlab/pre-restore-*`에 보관한다. 실패하면 서비스는
정지 상태로 남기며 `progress.json`에 가져온 VM을 기록한다. 자동으로 VM을 삭제하거나
중간 상태에서 재시도하지 않는다. 기록을 확인해 별도 빈 대상에서 다시 복원하는 것이 안전하다.
성공 후 학습자 VM은 정지 상태이므로 포털에서 시작한다. 기존 예비 VM은 labd가 다시
시작할 수 있으며, 추가 예비 VM 생성은 `pool_size: 0`으로 억제한다.
인증 모드와 프록시 시크릿도 복원되므로 Cloudflare 및 외부 접속 경로는 설치자가 별도로
연결한다. 복원 후 웹 접속·실습 파일·터미널을 확인하기 전에는 원본을 폐기하지 않는다.

검증 기록 (2026-10-22 KST): 별도 Ubuntu 24.04 중첩 VM에서 설정 백업 생성·검사,
50GiB 학습자 VM 1개와 `clean` 스냅샷·골든 이미지를 포함한 약 3.4GiB 암호화 전체
백업을 시험했다. 다른 빈 Ubuntu VM에서 dry-run과 실제 복원을 완료하고 설정 파일·
게스트 샘플 파일 해시, 스냅샷 보존, 포털 HTTP 200, 터미널 WebSocket 101,
관리자 접근 거부 403을 확인했다. 사용 중인 대상의 복원 거부도 확인했다.
일반 LXD 백업 포맷은 큰 가상 디스크와 스냅샷의 압축 해제를 반복할 수 있어 복원에
오래 걸린다. 이 검증은 테스트 환경 대상이며 기존 운영 데이터에 대한 복원 시험은 아니다.
개별 파일만 준비해야 하는 경우에는 `deploy.py --prepare`를 사용하되, 자동 설치와
섞지 않는다. 세부 운영과 골든 이미지 갱신은 [운영 문서](OPERATIONS.md)를 참고한다.

## 팀원 사용 안내

1. 기본 모드는 `http://서버IP:8088`에 접속한다. Access 모드는 서비스 도메인에서
   로그인한다. **시작**으로 환경을 발급한다.
2. 터미널에서 `labs` → `lab 01` → `n h1` 순서로 실습한다.
3. `doc 01`은 문제 설명, `walk 01`은 해설, `help-lab`은 단축 명령이다.
4. `lab 04`처럼 다른 단원을 실행하면 기존 단원을 자동 철거하고 새 단원만 배포한다. 컨테이너 내부 변경 사항은 사라지며 `~/network-lab`에서 편집한 파일은 유지된다. 같은 토폴로지를 다시 실행하면 기존 컨테이너는 유지한다. `unlab`은 남아 있는 실습을 모두 철거한다.
5. 다 쓰면 **일시 정지**한다. **초기화/영구 종료는 작업을 지운다.**
6. 터미널은 복습·피드백 목적으로 녹화된다. 비밀번호나 개인 키를 입력하지 않는다.

`lab 01`에서 `clab_admins` 그룹 오류가 나면 이전 이미지의 계정 권한을 확인한다.
현재 버전은 신규 설치와 VM 발급·시작·초기화 후 준비 단계에서 `lab`의 그룹을 보정한다.
이미 열려 있던 터미널에는 새 그룹이 반영되지 않으므로 터미널을 닫고 다시 연결한다.
설치 검증도 root가 아닌 실제 `lab` 계정으로 배포·ping·철거를 수행한다.

서버 업데이트만으로 기존 VM 내부의 콘텐츠는 바뀌지 않는다. 새 사용자에게 변경된
랩을 제공하려면 골든 이미지를 갱신한다. 운영·백업 정책은 [운영 문서](OPERATIONS.md) 참고.

## 소스 출처

2026-10-22 운영 호스트의 배포본을 가져왔다. 주 개발은 2026-08-12~16에 진행됐고
원본은 Git으로 관리되지 않았다.
이번 통합부터 이 저장소를 배포 소스의 기준으로 사용한다. 실제 비밀값은 제외하고 Nginx
템플릿, LXD 바인딩 예제, 오래된 관리자 설명을 정리했다. [외부 구성 요소](THIRD_PARTY.md) 참고.
