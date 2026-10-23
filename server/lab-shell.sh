# 네트워크 실습 환경 공용 셸 설정
# ~/.bash_aliases 에서 source 되며, Ubuntu 기본 .bashrc 가 PS1 을 설정한 "뒤"에 실행된다.

# ── 명령 기록 (채점용) ───────────────────────────────────────
# 전체 화면은 asciinema 가 녹화하지만, 그건 사람이 보기용이라 잡음이 많다.
# 채점하는 쪽이 읽기 좋게 "무엇을 쳤고 성공했는지"만 따로 구조화해 남긴다.
# 실행 직전 명령을 DEBUG 트랩으로 붙잡는다.
#   history 를 쓰면 함정이 있다 — bash 는 .bashrc 를 처리한 뒤에 히스토리 파일을 읽으므로
#   첫 프롬프트 시점의 `history 1` 은 이전 세션의 명령을 가리키고, 히스토리가 비어 있으면
#   반대로 이번 세션의 첫 명령을 통째로 놓친다.
#   DEBUG 트랩은 실제로 실행될 명령을 직접 주므로 그런 문제가 없다.
#   (functrace 를 켜지 않으면 함수 내부 명령에는 걸리지 않아, 사용자가 친 것만 잡힌다.)
__lab_pending=""
__lab_ready=""     # 첫 프롬프트가 그려진 뒤부터 기록한다 (로그인 스크립트 내부 명령 제외)
__lab_pretrap() {
  [ -n "$__lab_ready" ] || return 0
  case "$BASH_COMMAND" in
    __lab_ps1|__lab_ps1\ *|__lab_logcmd*|__lab_pretrap*) return 0 ;;
  esac
  __lab_pending="$BASH_COMMAND"
}
trap '__lab_pretrap' DEBUG

__lab_logcmd() {
  local rc=$1 cmd
  [ -n "${LAB_CMDLOG-}" ] || return 0
  cmd="${__lab_pending}"
  __lab_pending=""
  [ -n "$cmd" ] || return 0
  jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
         --arg sid "${LAB_SESSION_ID-}" \
         --arg cwd "$PWD" \
         --arg cmd "$cmd" \
         --argjson rc "${rc:-0}" \
         '{ts:$ts,session:$sid,cwd:$cwd,rc:$rc,cmd:$cmd}' >> "$LAB_CMDLOG" 2>/dev/null
}

# 노드 안에서 한 일도 맥락이 필요하므로 진입/이탈을 남긴다.
__lab_logevent() {
  [ -n "${LAB_CMDLOG-}" ] || return 0
  jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
         --arg sid "${LAB_SESSION_ID-}" \
         --arg ev "$1" --arg detail "${2-}" \
         '{ts:$ts,session:$sid,event:$ev,detail:$detail}' >> "$LAB_CMDLOG" 2>/dev/null
}

# ── 프롬프트 ────────────────────────────────────────────────
# 실습 중 자기가 어디에 있는지(호스트/노드)가 헷갈리기 쉬워 색으로 구분한다.
if [ -n "${BASH_VERSION-}" ] && [ -t 1 ]; then
  __lab_ps1() {
    local rc=$?
    if [ -n "$__lab_ready" ]; then
      __lab_logcmd "$rc"
    else
      __lab_ready=1        # 지금부터가 사용자가 친 명령이다
      __lab_pending=""
    fi
    local c_user='\[\e[38;5;44m\]'      # 청록 — 실습 VM
    local c_path='\[\e[38;5;250m\]'
    local c_dim='\[\e[38;5;244m\]'
    local c_err='\[\e[38;5;203m\]'
    local c_rst='\[\e[0m\]'
    local mark='$'
    [ "$(id -u)" -eq 0 ] && { c_user='\[\e[38;5;203m\]'; mark='#'; }   # root 는 빨강
    if [ $rc -ne 0 ]; then
      PS1="${c_user}\u${c_dim}@lab ${c_path}\w ${c_err}[$rc]${c_rst}\n${c_user}${mark}${c_rst} "
    else
      PS1="${c_user}\u${c_dim}@lab ${c_path}\w${c_rst}\n${c_user}${mark}${c_rst} "
    fi
  }
  PROMPT_COMMAND=__lab_ps1
fi

# ── 색상 기본값 ──────────────────────────────────────────────
export LS_OPTIONS='--color=auto'
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias egrep='egrep --color=auto'
alias fgrep='fgrep --color=auto'
alias diff='diff --color=auto'
alias ip='ip -color=auto'
export LESS='-R'

# ── 파일·이동 ────────────────────────────────────────────────
alias ll='ls -alFh'
alias la='ls -A'
alias l='ls -CF'
alias ..='cd ..'
alias ...='cd ../..'
alias df='df -h'
alias free='free -h'

# ── 네트워크 확인 (이 랩에서 제일 자주 치는 것들) ──────────────
alias ipa='ip -br addr'          # 주소 요약
alias ipl='ip -br link'          # 링크 요약
alias ipr='ip route'             # 라우팅 테이블
alias ipn='ip neigh'             # ARP/이웃 테이블
alias ports='ss -tulnp'          # 열린 포트
alias fdb='bridge fdb show'      # 브리지 MAC 학습 테이블
alias vlans='bridge vlan show'   # VLAN 설정
alias tcpd='tcpdump -nni'        # tcpd eth1 처럼 사용
alias ct='conntrack -L'          # 연결 추적 테이블

# ── containerlab ────────────────────────────────────────────
alias cdep='containerlab deploy -t'
alias cdes='containerlab destroy -t'
alias cins='containerlab inspect -t'
alias nodes='docker ps --filter name=clab- --format "table {{.Names}}\t{{.Status}}"'

# 실습 목록 보기
# 학습 팩 v2 실행기가 설치된 VM 에서는 등록된 단계형 실습을 실행기로 다룬다. 웹 패널과 같은
# 잠금·세대를 쓰므로 터미널에서 바꾼 실습을 웹이 다시 배포하지 않고 이어 받는다. 실행기가 없는 VM 은
# 아래의 기존 동작 그대로다.
NETLAB_RUNNER=/opt/netlab/current/runner/netlab-runner
__lab_has_runner() { [ -x "$NETLAB_RUNNER" ]; }
__lab_runner() { sudo -n "$NETLAB_RUNNER" --cli "$@"; }

labs() {
  echo "사용 가능한 실습 (lab <번호> 로 배포):"
  local d
  for d in "$HOME"/network-lab/[0-9]*/; do
    [ -d "$d" ] || continue
    printf "  %s\n" "$(basename "$d")"
  done
  [ -d "$HOME/network-lab/checkpoints" ] && echo "  checkpoints/ (cp1, cp2)"
}

# lab 01        → 01장 토폴로지 배포
# lab 17 bgp    → 토폴로지가 여러 개인 장은 이름을 덧붙인다
lab() (
  if [ -z "${1-}" ]; then labs; return 0; fi
  if __lab_has_runner; then
    case "$1" in
      check) __lab_runner check; return ;;          # 참고용 확인. 학습 기록은 웹 확인 버튼으로 남는다
      reset) __lab_runner reset; return ;;
    esac
    if __lab_runner resolve "$1" "${2-}" >/dev/null 2>&1; then
      __lab_logevent deploy "v2:$1${2:+ $2}"
      __lab_runner start "$1" "${2-}"
      return
    fi
    # 등록되지 않은 실습은 기존 방식으로 띄운다. 실행기가 맡고 있던 실습은 먼저 실행기로 내린다.
    __lab_runner release || return 1
  fi
  local dir topo list
  dir=$(ls -d "$HOME"/network-lab/"$1"* 2>/dev/null | head -1)
  if [ -z "$dir" ]; then echo "그런 실습이 없습니다: $1"; labs; return 1; fi
  list=$(find "$dir" -maxdepth 1 -name '*.clab.yml' | sort)
  if [ -n "${2-}" ]; then
    topo=$(echo "$list" | grep -i -- "$2" | head -1)
  else
    topo=$(echo "$list" | head -1)
  fi
  if [ -z "$topo" ]; then echo "토폴로지 파일을 못 찾았습니다."; echo "$list"; return 1; fi
  topo=$(readlink -f -- "$topo") || return 1
  if [ "$(echo "$list" | wc -l)" -gt 1 ] && [ -z "${2-}" ]; then
    echo "이 장에는 토폴로지가 여러 개입니다. 첫 번째를 씁니다:"
    echo "$list" | sed 's|.*/|    |'
    echo "  (다른 것을 쓰려면: lab $1 <이름>)"
  fi
  # Serialize deployment/destruction across browser terminals for this learner.
  local switch_fd
  exec {switch_fd}>"$HOME/.netlab-switch.lock" || return 1
  flock -n "$switch_fd" || { echo "다른 터미널에서 실습을 전환 중입니다. 잠시 후 다시 시도하세요."; return 1; }
  __lab_destroy_others "$topo" || return 1
  local running
  running=$(docker ps --filter label=clab-topo-file --format '{{.Label "clab-topo-file"}}') || return 1
  if printf '%s\n' "$running" | grep -Fxq -- "$topo"; then
    echo "▶ 이미 실행 중: ${topo#$HOME/network-lab/} (기존 컨테이너와 변경 사항 유지)"
    return 0
  fi
  echo "▶ 배포: ${topo#$HOME/network-lab/}"
  __lab_logevent deploy "${topo#$HOME/network-lab/}"
  containerlab deploy -t "$topo"
)

# Only destroy explicitly resolved containerlab topologies in the learner tree.
# Do not prune Docker networks or touch unrelated containers.
__lab_destroy_others() {
  local keep="${1-}" listing old resolved root
  root=$(readlink -f -- "$HOME/network-lab") || return 1
  listing=$(docker ps -a --filter label=clab-topo-file --format '{{.Label "clab-topo-file"}}') || {
    echo "실행 중인 실습 조회에 실패했습니다. 전환을 중단합니다."; return 1;
  }
  local -a targets=()
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    resolved=$(readlink -f -- "$old") || return 1
    [ "$resolved" = "$keep" ] && continue
    case "$resolved" in
      "$root"/*.clab.yml) ;;
      *) echo "실습 폴더 밖의 토폴로지입니다. 자동 철거하지 않습니다: $old"; return 1 ;;
    esac
    [ -f "$resolved" ] || { echo "기존 토폴로지 파일이 없어 안전하게 전환할 수 없습니다: $old"; return 1; }
    targets+=("$resolved")
  done < <(printf '%s\n' "$listing" | sort -u)
  if [ "${#targets[@]}" -gt 0 ]; then
    echo "⚠ 기존 실습을 종료합니다. 컨테이너 내부 변경 사항은 사라집니다. ~/network-lab의 편집한 파일은 유지됩니다."
  fi
  for old in "${targets[@]}"; do
    echo "▶ 철거: ${old#$HOME/network-lab/}"
    containerlab destroy -t "$old" || { echo "철거에 실패해 새 실습을 배포하지 않습니다."; return 1; }
  done
}

# 실행 중인 토폴로지 철거
unlab() (
  if __lab_has_runner; then __lab_runner release || return 1; fi
  local switch_fd
  exec {switch_fd}>"$HOME/.netlab-switch.lock" || return 1
  flock -n "$switch_fd" || { echo "다른 터미널에서 실습을 전환 중입니다."; return 1; }
  __lab_destroy_others
)

# n h1              → h1 노드 안으로 들어가기
# n h1 ip -br addr  → h1 에서 명령만 실행
n() {
  if [ -z "${1-}" ]; then
    echo "사용법: n <노드> [명령...]"
    docker ps --filter name=clab- --format '  {{.Names}}' 2>/dev/null
    return 1
  fi
  local want=$1; shift
  local m
  m=$(docker ps --filter name=clab- --format '{{.Names}}' | grep -E -- "-${want}\$" | head -1)
  if [ -z "$m" ]; then
    m=$(docker ps --filter name=clab- --format '{{.Names}}' | grep -F -- "$want" | head -1)
  fi
  if [ -z "$m" ]; then
    echo "그런 노드가 없습니다: $want"
    echo "실행 중인 노드:"
    docker ps --filter name=clab- --format '  {{.Names}}' 2>/dev/null
    return 1
  fi
  if [ $# -eq 0 ]; then
    __lab_logevent enter-node "$m"
    docker exec -it "$m" bash
    __lab_logevent leave-node "$m"
  else
    __lab_logevent node-exec "$m: $*"
    docker exec "$m" "$@"
  fi
}

# 노드 간 연결 빠르게 확인:  ping h1 10.10.10.2
pingfrom() {
  [ $# -ge 2 ] || { echo "사용법: pingfrom <노드> <목적지>"; return 1; }
  n "$1" ping -c3 -W2 "$2"
}

# 실습 문서 열기:  doc 01
doc() {
  local dir
  dir=$(ls -d "$HOME"/network-lab/"${1-}"* 2>/dev/null | head -1)
  [ -n "$dir" ] || { echo "그런 실습이 없습니다: ${1-}"; return 1; }
  ${PAGER:-less} "$dir/README.md"
}
# 해설 열기:  walk 01
walk() {
  local dir
  dir=$(ls -d "$HOME"/network-lab/"${1-}"* 2>/dev/null | head -1)
  [ -n "$dir" ] || { echo "그런 실습이 없습니다: ${1-}"; return 1; }
  ${PAGER:-less} "$dir/WALKTHROUGH.md"
}

# 단축키 요약
help-lab() {
  cat <<'EOF'

  실습 단축 명령
  ─────────────────────────────────────────────────────────────
  labs                 실습 목록
  lab 01               단원 전환·배포 (기존 다른 단원은 자동 철거)
  lab 17 bgp           토폴로지 지정 (같은 토폴로지는 유지)
  unlab                실행 중인 실습 철거
  nodes                실행 중인 노드 목록
  n h1                 h1 노드 안으로 들어가기
  n h1 ip -br addr     h1 에서 명령만 실행
  pingfrom h1 10.10.10.2
  doc 01 / walk 01     실습 설명 / 해설 문서
  lab-reset            환경 전체 초기화
  lab check / lab reset  단계형 실습: 참고용 확인 / 처음 상태로 (실행기가 있는 환경)

  네트워크 확인
  ─────────────────────────────────────────────────────────────
  ipa  주소     ipl  링크     ipr  라우팅     ipn  ARP
  fdb  MAC학습  vlans VLAN    ports 열린포트  ct   conntrack
  tcpd eth1     패킷 캡처

EOF
}
