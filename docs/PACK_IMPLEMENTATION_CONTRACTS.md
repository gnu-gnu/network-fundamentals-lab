# 학습 팩 v2 구현 계약

작성 기준: 2026-10-24. 계약 판: 1. 상태: 구현 전 합의된 규칙.

이 문서는 [구현 절차](PACK_IMPLEMENTATION_PROCEDURE.md)의 구현자가 공유해야 하는 실행·데이터·배포 계약입니다. 요청의 기계 검증 규칙은 [runner 요청 JSON Schema](schemas/netlab-runner-request.schema.json)에 있습니다. 현재 제품에 모든 기능이 이미 구현됐다는 의미는 아닙니다.

## 우선순위와 범위

이 계약과 Schema를 함께 관리합니다. 설명과 Schema가 충돌하면 임의로 한쪽을 골라 구현하지 말고 계약·검사를 함께 정정합니다. 후속 구현에 필요한 확장 동작도 Schema에 선언할 수 있지만, 실제 runner는 자신의 capability에 없는 동작을 반드시 거부합니다.

M1은 12장과 start/status/check/reset/stop, M2는 solve·probe 및 CLI 연결·추가 런타임을 다룹니다. 프로세스 내부 client에서 시작하고 별도 호스트 서비스는 후속 리팩토링으로 분리합니다. 그 전까지 앱 전체의 호스트 권한이 제거됐다고 주장하지 않습니다.

## 호출 경계와 소유권

```text
인증된 HTTP 요청
  → labd 레슨 API
  → 프로세스 내부 agent_client
      사용자·환경·인스턴스·레슨·동작 확인
      → 고정 lxc exec 대상 + netlab-runner + stdin JSON
          VM 실행기: 토큰 확인 → 등록 recipe 해석 → 실행
```

- account는 현재 인증 경로에서 얻으며 브라우저의 사용자 파라미터를 신뢰하지 않습니다.
- 인스턴스는 서버 소유 기록으로 해결합니다. 요청에 LXD 이름·프로젝트·파일 경로·argv·프록시 주소를 받지 않습니다.
- 기존 instances/state 관리 기록을 소유권의 단일 기준으로 사용합니다. VM 회수·교체 중에는 접수를 막고 이전 토큰을 무효화합니다. 별도 소유권 DB를 만들어 서로 다른 소유자를 기록하지 않습니다.
- attempt/job을 조회할 때도 소유권을 검사합니다. ID를 아는 것 자체는 권한이 아닙니다.
- guest는 공용 환경이며 개인 완료 실적으로 집계하지 않습니다.
- 기존 동일 origin JSON 검사와 인증 방식을 유지합니다. 학습자 VM에 호스트 관리 자격 정보·소켓을 제공하지 않습니다.
- runner 결과는 신뢰할 수 없는 입력으로 취급해 크기·형식·대상·토큰을 검사합니다. VM root가 가능한 환경의 판정은 외부 시험용 위변조 방지 증거가 아닙니다.

## 식별자와 요청 프로토콜

| 값 | 의미 |
| --- | --- |
| instance_id | 호스트가 관리하는 VM 식별자. runner 본문에 사용자 선택값으로 전달하지 않음 |
| incarnation | 생성·스냅샷 복원·교체 세션의 UUID. 호스트가 관리하고 게스트에 주입 |
| generation | 해당 incarnation 안의 환경 변경 세대. runner가 영속 관리 |
| request_id | Web·CLI·runner의 공통 작업 UUID. 별도 operation_id 없음 |
| job_id | 서버 내부 작업 조회 ID. VM 작업 식별자와 혼동하지 않음 |
| pack/bundle revision | 저작 콘텐츠와 실제 VM 배포 콘텐츠를 각각 식별하는 digest |

요청은 UTF-8 JSON 객체이며 중복 키·알 수 없는 필드를 거부합니다. stdin 본문은 최대 16KiB로 제한합니다. UUID와 digest는 소문자 정규형, 레슨은 `<pack>/<lesson>`, step/probe는 해당 레슨의 등록 ID입니다. 입력 별칭은 서버에서 정식 ID로 바꾼 뒤 실행기에 보냅니다.

| action | 요청 필드 | 제공 단계 |
| --- | --- | --- |
| status | protocol, action | M1 |
| start | protocol, request_id, action, expected, target_lesson, expected_bundle | M1 |
| reset | protocol, request_id, action, expected, lesson, expected_bundle | M1 |
| stop | protocol, request_id, action, expected, lesson | M1 |
| check | protocol, request_id, action, expected, lesson, expected_bundle, step | M1 |
| solve | protocol, request_id, action, expected, lesson, expected_bundle | M2 |
| probe | protocol, request_id, action, expected, lesson, expected_bundle, probe | M2 |

expected는 incarnation·generation만 포함합니다. expected.state는 없습니다. start의 target_lesson은 전환 대상이며 현재 레슨과 같아야 한다는 뜻이 아닙니다. 그 외 작업의 lesson은 현재 대상과 일치해야 합니다. 처음 start도 status로 얻은 토큰을 사용합니다. stop은 최신 bundle과의 일치를 요구하지 않지만 현재 토큰과 대상 레슨을 검증합니다.

```json
{
  "protocol": 1,
  "request_id": "b0d6148d-a065-49ec-a315-34021b402ee5",
  "action": "start",
  "expected": {
    "incarnation": "20d41a0c-b096-4f66-927e-22152ed8f9ba",
    "generation": 7
  },
  "target_lesson": "network/12",
  "expected_bundle": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

```json
{
  "protocol": 1,
  "request_id": "105db6bd-38ce-4b93-b9d5-16fc3893f31c",
  "action": "stop",
  "expected": {
    "incarnation": "20d41a0c-b096-4f66-927e-22152ed8f9ba",
    "generation": 8
  },
  "lesson": "network/12"
}
```

```json
{"protocol": 1, "action": "status"}
```

Schema는 구조만 검증합니다. 권한, 등록 ID, 실제 토큰, bundle, 런타임 capability는 실행 전에 별도로 검증합니다. 지원하지 않는 action은 실행하지 않고 unsupported_action을 반환합니다.

## 요청 접수와 결과

HTTP 인증·소유권 오류는 기존 정책대로 거부합니다. 구조 위반은 400, 토큰 불일치·busy·bundle 불일치는 409, runner 미설치 VM의 레슨 API는 410입니다. runner 고장을 미설치로 취급하지 않고 명시적인 오류로 표시합니다. 긴 작업은 202와 job_id를 반환하며 소유자만 결과를 조회합니다. 접수 뒤 발견한 경합도 job 결과에 busy로 기록하며 학습 실패로 처리하지 않습니다.

runner는 stdout에 JSON 객체 하나를 반환하고 훅 출력을 섞지 않습니다. 응답은 최대 64KiB로 제한하며 초과 상세 출력은 잘림을 표시합니다. 기본 필드는 protocol, request_id(status에서는 생략 가능), ok, state, lesson, observed, executed, bundle, result, error입니다.

- observed는 응답 시점에 확인한 토큰, executed는 실제 실행한 토큰입니다. 실행 전 거부했다면 executed=null입니다.
- 변경 작업은 증가한 세대를 executed로 반환하고 check/probe는 시작 시 검증한 세대를 반환합니다.
- 정상적으로 수행한 check의 fail은 실행 자체의 성공이므로 ok=true, result.status=fail입니다. 통신 장애와 학습상의 미통과를 구분합니다.
- 상태 파일이 손상됐다면 observed=null을 허용하고 recovery_required를 반환합니다. 세대를 만들어 내서 정상으로 처리하지 않습니다.
- error는 안정된 코드로 반환합니다. conflict, busy, bundle_mismatch, unsupported_action, interrupted, cancel_failed, recovery_required를 구분합니다.

## 중복 방지와 잠금

서버는 인증 주체와 request_id를 정규화한 요청 내용의 해시에 연결합니다. 같은 ID·같은 내용에는 같은 결과 또는 진행 중인 job을 반환하며 같은 ID·다른 내용은 거부합니다. 완료 후에도 최소 24시간 보존하고 실행 중이거나 결과가 불확실한 기록은 시간만으로 폐기하지 않습니다. M1부터 짧은 DB 트랜잭션과 유일성 제약으로 경합을 막습니다.

VM은 request_id·동작·단계·토큰을 최소 미완료 기록으로 영속화합니다. 상세 결과 저널과 회수는 후속 범위지만 중단 여부가 불확실한 작업을 자동 재실행해서는 안 됩니다. 재전송에 새 ID를 붙여 위험한 작업을 반복하지 않습니다. 호스트 VM 수명 작업에도 중복 실행 방지를 적용합니다.

변경 작업끼리는 기다리지 않고 busy를 반환합니다. check 도중 변경 요청은 다음 절의 취소 절차를 따릅니다. status는 실행 완료를 기다리지 않고 원자적으로 공개된 상태를 읽습니다. 파일럿에서는 state_seq 없이 status 조회를 직렬화하고 오래된 조회의 응답을 무시합니다. job 응답으로 현재 상태 전체를 덮어쓰지 않으며 회차 연결·종료는 새 status로 확인합니다.

동일 토큰·동작·대상의 진행 중 Web 요청은 기존 job으로 합칠 수 있습니다. 새 request_id의 동일 작업을 무조건 다시 실행하거나 완료된 작업을 영구히 같은 job으로 합치지는 않습니다.

자동 check는 앞 job이 끝난 뒤 간격을 두고 예약하며 pass·비실행 상태·단계 이동·숨겨진 탭에서는 멈춥니다. 여러 탭의 동일 attempt/step은 진행 중 job을 공유합니다. status 조회는 짧은 캐시로 합칠 수 있지만 실행 직전 토큰 검사는 캐시로 대신하지 않습니다.

## check 취소 후 변경

1. 변경 요청의 소유권·expected·대상을 검증합니다. 자동 검증 예약을 멈추고 짧은 제어 잠금으로 변경 예약을 확보합니다. 긴 check 실행 잠금을 먼저 기다리지 않습니다.
2. runner가 기록한 해당 check에만 취소를 요청합니다. HTTP에서 PID나 임의 시그널을 받지 않습니다. 변경 예약 중에는 새 check를 시작하지 않습니다.
3. TERM 후 유예를 두고 필요하면 KILL합니다. 컨테이너 내부 exec는 외부 프로세스뿐 아니라 어댑터가 실제 대상의 종료까지 확인합니다.
4. 종료 확인 후 환경 토큰을 다시 검증합니다. 바뀌었다면 conflict, 같다면 세대를 증가시키고 변경을 시작합니다.
5. 종료를 확인하지 못하면 cancel_failed 또는 recovery_required를 반환하며 변경을 실행하지 않습니다.

5초는 정상적으로 취소 가능한 검증에서 변경을 시작하는 목표이며, 미확인 상태에서도 시작하는 마감 시간이 아닙니다. cancelled는 fail로 집계하지 않고 취소된 check의 늦은 pass도 반영하지 않습니다. 취소와 자연 종료의 경합은 최종 결과의 유일성 제약으로 처리합니다. CLI에도 같은 제어를 적용합니다.

## 세대와 상태

| 동작 | 계약 |
| --- | --- |
| 같은 레슨 start | 토큰 검사 후 이미 running이면 no-op. 세대·컨테이너·작업 파일 유지 |
| 신규 또는 다른 레슨 start | 토큰·대상·bundle 확인 → generation 증가 영속화 → 이전 환경 정지 → up/setup/inject → 준비 완료 |
| reset | generation 증가 → reset 훅 또는 teardown/down/up/setup/inject |
| solve | generation 증가 → solve 훅. 회차는 유지하고 task 재검증 |
| stop | generation 증가 → teardown/down. teardown 실패 시에도 down 시도. 실패를 idle로 처리하지 않음 |
| check/probe | 세대 변경 없이 실행하고 결과에 검증한 토큰 첨부 |
| 재부팅 | boot_id 변경을 감지해 세대 증가. 준비 완료와 작업 상태를 검증한 뒤 재개 |

runner 상태는 `/var/lib/netlab/runner/`에 임시 파일 쓰기·fsync·rename·디렉터리 fsync 순서로 저장합니다. 작업 단계, 준비 완료 여부, boot_id, 토큰을 보존합니다. 기존 상태의 손상·소실을 신규 VM의 generation=0으로 처리하지 않습니다.

setup/inject/stop 실패 또는 미완료 표식이 있으면 컨테이너가 존재해도 정상 running으로 처리하지 않습니다. 상태가 불명확하면 interrupted/recovery_required입니다. shell 재개 역시 컨테이너 목록이 아니라 레슨의 완료 표식과 필요한 상태로 판단합니다.

재부팅 시 세대 증가는 잠금 아래에서 한 번만 수행합니다. 복구 확인 전 status는 recovering을 반환하며 이전 running을 그대로 보여 주지 않습니다. 작업 중에는 check를 허용하지 않습니다. status 조회 자체로 환경을 시작하거나 철거하지 않습니다.

일반 stop과 호스트 VM 정지·초기화·교체는 별개입니다. 일반 stop에서 generation은 생략할 수 없습니다. 상태 손상 시 모든 컨테이너를 자동 철거하지 않고 호스트 인증·소유권으로 제어하는 초기화, 명시적 교체, 운영자 조사 경로를 안내합니다. 초기화는 같은 VM의 clean 복원이며 이미지 업그레이드가 아닙니다. 환경 교체는 파기 후 새 풀에서 재발급합니다. 파괴 범위와 보존 대상을 먼저 확인하고 복원·교체 때 incarnation을 갱신합니다.

## 검증 결과와 훅

선언형 check는 node·argv·expect를 실행합니다. script형 check는 VM 내부의 승인된 파일을 실행합니다. all은 비어 있지 않은 선언형 항목 목록이며 선언 순서대로 모든 항목을 실행합니다.

| 결과 | 의미 |
| --- | --- |
| pass | 선언형 expect 충족 또는 script exit 0 |
| fail | 정상 검증했으나 expect 미충족 또는 script exit 1 |
| unverifiable | 노드 부재·전송 실패 등으로 판정 불가 또는 script exit 2 |
| error | timeout·signal·내부 오류·그 밖의 script 종료 코드 |
| cancelled | 취소. 학습 판정 집계에서 제외 |

집계 우선순위는 error > unverifiable > fail > pass입니다. fallback은 fail일 때만 한 단계 실행하며 환경 장애나 timeout을 숨기지 않습니다. 선언형의 0이 아닌 rc만으로 전송 장애라 판단하지 않고 expect.rc와 어댑터의 전송 정보를 함께 봅니다. v1의 정상 판정·조건 순서·기존 fallback을 유지하며 모든 항목의 장애 분류와 timeout 엄격화는 허용된 차이로 테스트합니다.

출력은 계속 읽되 마지막 8KiB를 보존하고 잘림을 표시합니다. 상한에 도달했다고 파이프 읽기를 중단하지 않습니다. 결과 파일은 일반 파일 여부·크기·JSON을 검사하며 message/detail은 각각 2KiB로 제한합니다. 종료 코드와 모순되는 status는 error, JSON이 아닌 메시지는 무시합니다. 실행, up/down, 결과 읽기에 제한 시간을 두고 종료가 확인되지 않으면 다음 변경을 허용하지 않습니다.

## 회차와 완료 기록

진행 DB는 `/var/lib/netlab/learning/progress.sqlite3`로 분리합니다. 튜터 jobs와 별개로 attempts, step_events, 최소 job/요청 기록, completion_events, summary를 관리합니다. 외부 명령 실행 중에는 DB 쓰기 트랜잭션을 유지하지 않습니다.

- attempt는 사용자·instance·incarnation·lesson·pack revision·bundle·현재 generation에 연결합니다. 열린 회차의 중복은 유일성 제약으로 막습니다.
- step_events는 추가 전용이며 실행 토큰과 요청 토큰을 보존합니다. 같은 job의 최종 결과를 중복 등록하지 않습니다.
- task의 현재 결과는 attempt·step·현재 generation으로 제한합니다. quiz/read는 같은 회차 안의 최신 응답·읽음 상태를 사용합니다.
- 토큰 불일치, 닫힌 회차, 취소된 job의 결과는 현재 판정에 반영하지 않습니다. 늦은 결과로 generation을 되돌리지 않습니다.
- complete는 회차를 닫지 않습니다. 회차마다 completed_at과 completion event를 한 번만 기록합니다. reset 후 현재 task 상태와 과거 완료 이력을 구분합니다.
- 도움 사용 여부는 완료 시점의 값을 완료 이벤트에 고정합니다. 완료 후 solve로 과거의 비지원 완료 기록을 바꾸지 않습니다.
- 미완료 회차에서 solve가 접수되어 실제 시도되면 도움 사용을 기록합니다. completion event와 summary는 같은 트랜잭션으로 갱신하며 summary는 이벤트로 재계산할 수 있어야 합니다.
- guest의 공용 환경은 다른 방문자가 변경할 수 있으므로 개인 완료 실적으로 집계하지 않습니다.

M2의 adopted_cli는 running 환경에 대응하는 회차가 없을 때 기존 회차를 정리하고 실제 환경에 새 회차를 연결합니다. start를 다시 실행하지 않습니다. CLI check는 참고 표시만 하며 공식 판정에 반영하지 않습니다. 동일 레슨의 CLI 재선택은 no-op, reset은 세대를 갱신합니다.

idle이면 기존 회차를 종료합니다. failed/recovery_required이면 자동 시작·완료하지 않습니다. 서버 자료 revision만 바뀌고 bundle이 같다면 기존 회차를 revision_changed로 닫고 같은 실제 환경에 새 회차를 연결할 수 있습니다. task는 재검증하고 이전 quiz/read를 새 교재에 자동 복사하지 않습니다. 진행 중 회차와 이력 조회에 필요한 과거 revision을 보존합니다.

## 팩과 공개 범위

network의 기존 레슨은 레슨 디렉터리의 일반 파일을 유지하는 호환 모드로 이전합니다. 모든 깊이의 lesson.toml, quiz/, hints/, 채점 파일, .git, clab-* 생성물, 편집기 임시·백업 파일을 제외합니다. 심볼릭 링크·특수 파일·경로 이탈은 거부합니다. fix.sh·공개 해설·공개 교재는 유지합니다. README·WALKTHROUGH의 로컬 참조와 topology binds·exec의 정적 참조를 검사합니다. 동적 참조는 미확인으로 표시하고 전용 VM에서 해설 절차를 실행해 확인합니다. environment.files 방식은 후속 팩에서 다룹니다.

recipe는 원본 TOML과 구분되는 실행용 생성물입니다. 힌트·fault 문자열·정답을 복사하지 않고 실행에 필요한 ID·runtime·node·check·probe·훅·시간 제한만 포함합니다. VM loader는 recipe 스키마를 사용하며 제외한 quiz/hints 파일을 요구하지 않습니다.

강한 비배포·기밀 보호 대상은 quiz/grading입니다. 이 자료는 server-only로 관리하고 학습자 API·검색·VM에 원본과 정답을 배포하지 않습니다. M4의 문항·피드백은 공개 정책에 따른 전용 응답으로 제공합니다. 힌트·fault는 학습 진행에 따른 공개 정책으로 다룹니다. VM에 해설·inject·check가 있으므로 답을 추측할 수 없다고 보장하지 않습니다. 일반 사용자 API는 내부 카탈로그 원본이 아닌 공개용 데이터를 반환합니다.

서버 카탈로그·서버 전용 데이터·VM 실행 번들을 분리합니다. 튜터 검색 전에 공개 등급과 단계별 help_level을 적용하며 모델 출력의 사후 삭제에만 의존하지 않습니다. 원본 pack.toml과 이미지 빌더 전용 스크립트도 학습자 번들에 포함하지 않습니다.

pack revision은 정규화한 저작 데이터와 파일 경로·내용 해시·실행 비트로, bundle revision은 생성 recipe와 실제 배포 내용으로 계산합니다. 시간·절대 경로·자기 자신의 해시는 넣지 않으며 같은 입력으로 재현 가능해야 합니다. 다른 팩의 변경으로 해당 팩 revision을 바꾸지 않습니다. 서버는 pack과 bundle의 대응을 관리하고 VM은 자신의 bundle을 비교합니다.

내용 해시는 SHA-256을 사용하고 상대 경로 manifest를 정렬합니다. catalog revision은 정렬한 pack ID·pack revision 쌍으로 계산합니다. recipe에는 저작 형식과 별도로 runner protocol 호환 범위를, 이미지에는 runner 버전·protocol·bundle map을 기록합니다. 서버 자료만 바뀌었다는 이유로 bundle이 같은 VM을 교체하지 않습니다.

소스와 VM의 배포 불일치와 root 권한 학습자의 파일 변경은 별개 문제입니다. revision 검사를 위변조 불가능한 시험 증명으로 취급하지 않습니다.

## 호환성과 종료 조건

VM capability로 실행 경로를 결정하고 계정별 실행 플래그는 두지 않습니다. 운영 긴급용 전역 접수 차단은 별도로 허용합니다. runner 미설치는 새 레슨 API에 410과 업그레이드 안내를 반환합니다. 설치된 runner의 고장·protocol 불일치는 명시적 오류이며 v1으로 자동 대체하지 않습니다. 기존 터미널·튜터·동결한 v1 check는 이행 기간에 한해 유지합니다.

마지막 기존 VM과 풀 후보를 정리하고, 작업 파일·녹화를 보존하고 v2 사용을 확인하며, 승인된 롤백 관찰 기간을 끝낸 뒤 v1 실행 경로를 제거합니다. 목표일은 운영 이행 승인 시 릴리스 기록에 정합니다. 이전 ID 별칭과 이력 조회는 유지하며 과거 이력의 일괄 재작성을 새 기능의 전제조건으로 삼지 않습니다.

## 개발 설치와 운영 복구

install은 개발 환경·전용 검증 라벨·미할당·관리 대상 여부를 모두 검증합니다. 실행 중 작업이 있으면 거부합니다. staging 검증 후 원자적으로 전환하고 실패하면 기존 설치를 유지합니다. 대상이 불명확하거나 신·구 파일이 혼합된 설치를 허용하지 않습니다. 운영은 승인된 이미지 경로로만 배포합니다.

진행 DB는 SQLite backup API로 만든 일관된 사본을 기존 백업에 포함합니다. 실시간 DB와 WAL/SHM을 섞지 않으며 복원 후 무결성·외래키·회차와 이벤트의 관계를 검사합니다. 복원된 running job은 자동 재실행하지 않습니다. 일반 코드 롤백에서 현재 사용자 데이터를 과거 상태로 강제 복원하지 않습니다.

## M1 구현에서 확정한 세부

M1 구현(12장 파일럿)에서 위 계약을 구체화한 값입니다. 계약의 규칙을 바꾸지 않으며, 바꿀 때는 이 절과 테스트를 함께 고칩니다.

| 항목 | 정한 값 |
| --- | --- |
| incarnation 위치 | LXD 설정 `user.netlab.incarnation`. VM 생성 때(clean 스냅샷 전) 넣고, 스냅샷 복원 뒤 기동 전에 새 값으로 바꿉니다. 게스트는 `/dev/lxd/sock`으로 읽습니다. 값이 없는 기존 VM은 서버가 처음 호출할 때 넣습니다 |
| runner 설치 위치 | `/opt/netlab/releases/<id>/{runner,packs/<pack>}`와 `current` 심볼릭 링크. 상태는 `/var/lib/netlab/runner/`. 서버는 `/opt/netlab/current/runner/netlab-runner`만 호출합니다 |
| CLI와의 관계 | 변경 작업은 셸 `lab` 명령과 같은 `~lab/.netlab-switch.lock`을 잡고, containerlab을 `lab` 사용자로 작업 사본 `~lab/network-lab`에서 실행합니다. 터미널에서 다른 실습으로 바꾸면 status의 `runtime.matches`가 false가 되고 check는 conflict입니다. 회차 연결(adopted_cli)은 M2입니다 |
| 요청 검증 | 서버와 runner가 같은 수기 검증기(`server/runner/protocol.py`)를 씁니다. Schema와 같은 사례 집합을 CI에서 함께 검사합니다. 중복 키, `7.0` 같은 비정수 세대, 16KiB 초과는 파싱 단계에서 거부합니다 |
| 추가 오류 코드 | `failed`(변경의 단계 실패, `stage` 동반), `invalid_request`(400), `no_environment`(409), `runner_missing`(410), `runner_error`(502) |
| 복구 사유 | `reboot`·`interrupted`는 레슨을 알면 reset·stop, 모르면 stop만 허용합니다. `state_damaged`·`exec_unconfirmed`는 runner 작업을 허용하지 않고 호스트의 초기화·교체로 복구합니다 |
| 공용 환경 | open 모드의 `guest` 계정만 공용으로 봅니다. 같은 서버에서 자기 VM을 가진 계정의 완료는 개인 기록에 남습니다 |
| 개발 설치 표식 | 서버 설정 `environment: development`, VM 설정 `user.netlab.verify=true`, labd가 관리하는 준비 완료 예비 VM이면서 미할당일 것 |
| 조회 주기 | M1은 자동 check 예약이 없고 확인 버튼만 있습니다. 패널은 15초마다 상태를 읽습니다. runner가 없는 VM은 410을 받은 뒤 패널이 5분, 서버가 60초 동안 다시 조회하지 않습니다 |

M1의 개발 설치는 clean 스냅샷을 만든 뒤에 runner를 넣습니다. 그래서 그 VM을 초기화하면 runner도 사라져 410이 됩니다. runner를 스냅샷 전에 담는 이미지 배포는 M3 범위입니다.

## M2 구현에서 확정한 세부

| 항목 | 정한 값 |
| --- | --- |
| 호환 레슨의 solve | 레슨 디렉터리의 `fix.sh`가 solve 훅입니다. 실행 비트가 없으면 빌드 오류입니다. 세대를 올리고, 실패해도 레슨은 running으로 두며 실패 단계를 보입니다 |
| 도움 사용 기록 | 미완료 회차에서 solve가 시도되면 성공 여부와 관계없이 `assisted`로 남습니다. 이미 기록된 완료 이벤트의 값은 바꾸지 않습니다 |
| 레슨 전환 순서 | 이전 레슨 teardown → containerlab 전체 철거(터미널에서 띄운 토폴로지 포함) → 새 레슨 up → setup → inject. 이전 레슨을 정리하지 못하면 이전 레슨의 `stop_failed`로 남고 새 레슨은 시작하지 않습니다 |
| `host` 노드 | 학습자 VM 자체입니다. v1의 노드 없는 진단은 `host`로 옮겼습니다 |
| shell 런타임 | 배포물이 없고 훅이 환경을 준비합니다. VM 재부팅 뒤에도 준비 완료 표식이 있으면 running으로 이어 갑니다 |
| 작업 사본 | network 팩은 `~lab/network-lab/<dir>`, 그 밖의 팩은 `~lab/labs/<pack>/<레슨 디렉터리>`입니다 |
| 스크립트 check | 번들의 스크립트를 root로, 작업 사본을 cwd로 실행합니다. `$NETLAB_RESULT`는 4KiB 이하 JSON만 읽고 message는 2KiB로 자릅니다 |
| CLI | runner가 있는 VM에서 `lab <장> [변형]`은 runner start, `lab check`는 참고용, `lab reset`은 runner reset입니다. 등록되지 않은 실습은 runner 실습을 먼저 내린 뒤 기존 방식으로 띄웁니다. runner가 없는 VM은 기존 동작입니다 |
| 이어 받기 | 상태 동기화에서 실행 중인 레슨에 열린 회차가 없으면 `adopted_cli` 회차를 만듭니다. runner start는 호출하지 않습니다 |
| 튜터 | runner가 있는 VM에서는 레슨·세대·단계를 runner와 회차에서 얻고, 등록된 probe ID만 runner로 실행합니다. 튜터의 check는 진단이며 진행 기록에 남기지 않습니다. 답하는 사이 환경이 바뀌면 이전 환경 기준이라는 안내를 붙입니다 |
| format 2 범위 | read·task 단계, argv·script·all 검증, 훅 5종, probe를 지원합니다. quiz·reflect·탭은 해당 단계 전까지 오류로 거부합니다 |
| 개발 설치 | 한 릴리스에 VM이 가질 모든 팩을 담습니다(`--bundle`을 여러 번) |

## M3 구현에서 확정한 세부

| 항목 | 정한 값 |
| --- | --- |
| 이미지 빌드 | `packctl image`가 설정 이미지(또는 지정한 기반)에서 빌드 VM을 만들고, labd와 같은 실습 계정 권한 정리와 `lab-shell.sh`를 반영한 뒤 runner와 **카탈로그의 모든 팩** 번들을 한 릴리스로 설치합니다. 해설 훅이 있는 모든 레슨을 start → solve → 모든 task check pass(수렴 대기 최대 180초) → stop으로 확인해야 게시합니다. 실패하면 reset → solve → check를 한 번만 다시 하고 시도 횟수를 기록에 남깁니다 |
| 작업 사본 정합 | runner는 기존 작업 사본을 덮어쓰지 않으므로, 빌드는 network 팩 번들의 레슨 파일을 이미지의 원본 사본(`/opt/network-lab-pristine`)과 `~lab/network-lab`에 덮어씁니다. 번들에 없는 파일은 v1 명령을 위해 남깁니다 |
| 게시 규칙 | 항상 새 별칭으로 게시하며 기존 별칭·이미지를 바꾸거나 지우지 않습니다. 실패하면 빌드 VM을 진단용으로 남깁니다. 게시 전 작업 사본·runner 상태·machine-id·SSH 호스트 키·세션 기록을 지웁니다 |
| 이미지 기록 | `/var/lib/netlab/images/<별칭>.json`에 지문, 기반 이미지와 지문, 소스 기준, 릴리스 ID, runner 버전(소스 해시)·protocol, catalog revision, 팩별 bundle revision, smoke 결과를 남깁니다. 이미지 속성에도 릴리스·protocol·번들 맵을 적습니다 |
| 연결 | `packctl activate`는 기록이 있고, 별칭이 기록한 지문을 가리키며, 번들 맵이 서버 카탈로그와 같은 이미지만 설정 `image`로 연결합니다. 실행기 없는 이전 이미지로 되돌릴 때만 `--legacy`를 씁니다 |
| 풀 이행 | 배정 시 설정 이미지와 기반 지문(`volatile.base_image`)이 다른 예비는 건너뛰고, 유지 작업이 준비 완료·미예약 예비만 지운 뒤 새로 채웁니다. 사용자에게 배정된 VM은 지우지 않습니다. 어느 쪽 지문이든 모르면 아무것도 지우지 않습니다 |
| runner 없는 VM | 레슨 API는 410, `path: legacy`와 환경 교체 안내를 돌려줍니다. 상태 API는 runner VM에서 `path: runner`, capability, 팩별 번들 일치 여부를 보입니다 |
| 긴급 중지 | 설정 `lessons_paused`가 참이면 새 레슨 작업(start·reset·stop·solve·check)을 503 `paused`로 거절합니다. 조회·힌트·읽음 표시·터미널·튜터는 유지합니다. 계정별 플래그는 두지 않습니다 |
| 백업 | SQLite DB는 backup API 사본(rollback journal 모드)으로 담고 `-wal`·`-shm`·`-journal`은 담지 않습니다. 백업·검사·복원 때 integrity_check, foreign_key_check, 열린 회차 중복, 완료와 회차의 계정·레슨 일치, 이벤트 작업의 계정 일치, 요약과 완료 수 일치를 확인합니다 |
| 복원 뒤 | labd 시작 시 queued·running 작업은 interrupted로 바꾸고 다시 실행하지 않습니다. 회차는 다음 상태 동기화에서 VM의 incarnation·generation과 맞춥니다 |

## 수용 기준 변경

계약과 테스트는 함께 변경합니다. 최소 수용 대상은 [구현 절차의 M1 수용표](PACK_IMPLEMENTATION_PROCEDURE.md#m1-수용-테스트)입니다. Schema의 정상 사례뿐 아니라 토큰 없는 구형 stop, 추가 argv, 음수 세대, 대상 누락, protocol 위반이 거부되는지 확인합니다. 문서 커밋은 구현 완료나 운영 배포 승인을 의미하지 않습니다.
