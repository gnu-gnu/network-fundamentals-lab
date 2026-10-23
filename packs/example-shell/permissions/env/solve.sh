#!/bin/bash
# 해설 적용: 소유자만 읽고 쓰게 한다.
set -euo pipefail
chmod 0600 /home/lab/shell-lab/secret.txt
