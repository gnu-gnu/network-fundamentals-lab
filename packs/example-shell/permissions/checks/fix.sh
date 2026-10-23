#!/bin/bash
# exit 0 통과, 1 아직 아님, 2 판정 불가. 학습자에게 보일 메시지는 $NETLAB_RESULT 에 JSON 으로 쓴다.
f=/home/lab/shell-lab/secret.txt
say() { [ -n "${NETLAB_RESULT:-}" ] && printf '{"message": "%s"}\n' "$1" > "$NETLAB_RESULT"; }
if [ ! -f "$f" ]; then say "secret.txt 가 없습니다. 실습을 초기화하세요."; exit 2; fi
mode=$(stat -c %a "$f"); owner=$(stat -c %U "$f")
if [ "$(cat "$f")" != "netlab-example-secret" ]; then say "파일 내용이 바뀌었습니다. 내용은 그대로 두어야 합니다."; exit 1; fi
if [ "$owner" != lab ]; then say "소유자가 lab 이 아닙니다."; exit 1; fi
if [ "$mode" != 600 ]; then say "아직 소유자 외의 사용자에게 권한이 있습니다 (현재 $mode)."; exit 1; fi
say "소유자만 읽고 쓸 수 있습니다."
exit 0
