#!/bin/bash
# 터미널 테마 + 단축 명령을 실습 VM 에 적용한다. (골든 이미지와 기존 인스턴스 양쪽에 쓴다)
set -euo pipefail

# containerlab's non-root launcher requires this group as well as Docker access.
getent group clab_admins >/dev/null || groupadd --system clab_admins
usermod -aG clab_admins lab

install -m 644 /root/lab-shell.sh  /etc/lab-shell.sh
install -m 755 /root/ttyd-lab     /usr/local/bin/ttyd-lab
install -m 755 /root/lab-session  /usr/local/bin/lab-session

# 세션 녹화 도구 (이미지 정리 때 apt 목록을 지우므로 먼저 갱신한다)
if ! command -v asciinema >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq 2>&1 | tail -1
  apt-get install -y -qq asciinema 2>&1 | tail -2
fi
asciinema --version

# 녹화 보관 위치 — lab 사용자가 명령 로그를 쓸 수 있어야 한다.
mkdir -p /var/log/lab-sessions
chgrp lab /var/log/lab-sessions 2>/dev/null || true
chmod 775 /var/log/lab-sessions

# lab 사용자와 root 양쪽에서 쓰이도록 연결한다.
# Ubuntu 기본 .bashrc 는 PS1 설정 뒤에 ~/.bash_aliases 를 읽으므로 프롬프트도 여기서 덮을 수 있다.
for lab_home in /home/lab /root; do
  [ -d "$lab_home" ] || continue
  echo '[ -f /etc/lab-shell.sh ] && . /etc/lab-shell.sh' > "$lab_home/.bash_aliases"
done
chown lab:lab /home/lab/.bash_aliases 2>/dev/null || true

# 실습 파일을 쓰기 가능하게.
# 원본(/opt/network-lab-pristine)을 chmod a-w 로 잠가 두었는데 cp -a 가 그 권한까지
# 복사해서 사본이 읽기 전용이 된다. 실습 중 설정 파일을 고치고 containerlab 이 상태를
# 기록해야 하므로 사본은 반드시 쓸 수 있어야 한다.
if [ -d /home/lab/network-lab ]; then
  chown -R lab:lab /home/lab/network-lab
  chmod -R u+w /home/lab/network-lab
fi

# lab-reset 도 같은 실수를 반복하지 않도록 고친다.
cat > /usr/local/bin/lab-reset <<'RESET'
#!/bin/bash
# 실행 중인 모든 containerlab 토폴로지를 철거하고 실습 파일을 원본 상태로 되돌린다.
set -u
echo ">>> 실행 중인 랩 철거"
for c in $(sudo docker ps -aq --filter "name=clab-" 2>/dev/null); do
  sudo docker rm -f "$c" >/dev/null 2>&1
done
sudo docker network prune -f >/dev/null 2>&1
echo ">>> 실습 파일 원본 복원"
test -d /opt/network-lab-pristine || exit 1
test ! -L /home/lab/network-lab || exit 1
if [ -d /home/lab/network-lab ]; then
  lab_backup=$(mktemp -d /home/lab/network-lab-backup.XXXXXX)
  mv /home/lab/network-lab "$lab_backup/"
  echo "이전 파일 보관: $lab_backup/network-lab"
fi
cp -a /opt/network-lab-pristine /home/lab/network-lab
sudo chown -R lab:lab /home/lab/network-lab
chmod -R u+w /home/lab/network-lab
echo ">>> 완료. ~/network-lab 이 초기 상태입니다."
RESET
chmod +x /usr/local/bin/lab-reset

# 로그인 배너 — 우분투 기본 안내(광고·업데이트 알림)는 끄고 실습 안내만 남긴다.
[ -f /etc/default/motd-news ] && sed -i 's/^ENABLED=.*/ENABLED=0/' /etc/default/motd-news
for f in /etc/update-motd.d/*; do
  case "$(basename "$f")" in
    99-lab) ;;
    *) chmod -x "$f" 2>/dev/null || true ;;
  esac
done

cat > /etc/update-motd.d/99-lab <<'EOF'
#!/bin/sh
printf '\n'
printf '  \033[38;5;44m네트워크 기초 실습 환경\033[0m\n'
printf '  \033[38;5;244m─────────────────────────────────────────────\033[0m\n'
printf '  실습 목록   \033[38;5;44mlabs\033[0m          배포   \033[38;5;44mlab 01\033[0m\n'
printf '  노드 접속   \033[38;5;44mn h1\033[0m          철거   \033[38;5;44munlab\033[0m\n'
printf '  설명 문서   \033[38;5;44mdoc 01\033[0m        해설   \033[38;5;44mwalk 01\033[0m\n'
printf '  단축키 전체 \033[38;5;44mhelp-lab\033[0m      초기화 \033[38;5;44mlab-reset\033[0m\n'
printf '  \033[38;5;244m이 세션은 복습·피드백을 위해 기록됩니다.\033[0m\n'
printf '\n'
EOF
chmod +x /etc/update-motd.d/99-lab

# ttyd 를 래퍼로 실행하도록 변경
cat > /etc/systemd/system/ttyd.service <<'EOF'
[Unit]
Description=ttyd - 브라우저 터미널
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ttyd-lab
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart ttyd
sleep 2
systemctl is-active ttyd

# 검증 — 반드시 "대화형" 로그인 셸로 확인해야 한다.
# Ubuntu 기본 .bashrc 는 비대화형이면 즉시 return 하므로 bash -lc 로는 별칭이 안 잡힌다.
sudo -u lab bash -lic '
  for x in ll n lab help-lab ipa; do
    type "$x" >/dev/null 2>&1 && echo "  $x OK" || echo "  $x 없음"
  done' 2>/dev/null | grep -v 'job control' || true
echo "적용 완료"
