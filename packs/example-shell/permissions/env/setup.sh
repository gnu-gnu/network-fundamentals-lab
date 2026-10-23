#!/bin/bash
# 실습 시작: 일부러 644 로 연 비밀 파일을 만든다. 두 번 실행해도 결과가 같다.
set -euo pipefail
dir=/home/lab/shell-lab
install -d -o lab -g lab -m 0755 "$dir"
printf 'netlab-example-secret\n' > "$dir/secret.txt"
chown lab:lab "$dir/secret.txt"
chmod 0644 "$dir/secret.txt"
