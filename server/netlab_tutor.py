#!/usr/bin/env python3
"""
netlab_tutor — 실습 진도 검증(check) + 실시간 도움(help).

호스트에서만 돈다. VM 안 상태는 `lxc exec`(LXD 제어 채널)로 조회한다.
VM→호스트는 방화벽으로 막혀 있으므로 학습자는 이 로직·LLM 키에 접근할 수 없다.

세 가지가 한 뼈대(챕터 메타데이터 + 상태 수집)를 공유한다:
  check(inst, ch)  → 성공조건 판정 (진도)
  gather(inst, ch) → 지금 상태 스냅샷 (help 의 재료, 비파괴)
  help(inst, ch, n)→ 상태 + 단계식 톤으로 LLM 조언 (n = 도움 요청 횟수)

CLI:  python3 netlab_tutor.py check <instance> <chapter>
      python3 netlab_tutor.py gather <instance> <chapter>
      python3 netlab_tutor.py help  <instance> <chapter> [--attempt N]
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
from netlab.paths import TUTOR_CONFIG_PATH as TUTOR_CFG
from tutor_chapters import EXTRA, CONCEPTS

# 챕터 메타데이터(CHAPTERS)는 tutor_chapters.py 한 곳에서만 정의한다.
CHAPTERS = dict(EXTRA)

for chapter_id, chapter_meta in CHAPTERS.items():
    chapter_meta['core_concept'] = CONCEPTS[chapter_id]

# ── lxc / 진단 ────────────────────────────────────────────────────

def lxc_exec(instance, argv, timeout=25):
    p = subprocess.run(["lxc", "exec", instance, "--", *argv],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def node_exec(instance, prefix, node, argv, timeout=25):
    """clab 노드 안에서 명령 실행 (docker exec 경유)."""
    if node is None:
        return lxc_exec(instance, argv, timeout)
    full = f"{prefix}-{node}"
    return lxc_exec(instance, ["docker", "exec", full, *argv], timeout)


_CLAB_RE = re.compile(r"^clab-(\d{2})-")


def deployed_chapter(instance):
    """
    지금 배포된 챕터 번호를 알아낸다. 없으면 None.

    여러 실습이 남아 있으면 가장 최근 생성된 컨테이너의 실습을 선택한다.
    docker ps 기본 생성 역순에만 의존하지 않고 CreatedAt으로 정렬한다.
    """
    rc, out, _ = lxc_exec(instance, ["docker", "ps", "--format", "{{.CreatedAt}}\t{{.Names}}"], 20)
    if rc != 0:
        return None
    rows = [line.split('\t', 1) if '\t' in line else ['', line.strip()] for line in out.splitlines()]
    for _, n in sorted(rows, key=lambda row: row[0], reverse=True):
        for ch, meta in CHAPTERS.items():
            if n.startswith(meta['prefix'] + '-'):
                return ch
        m = _CLAB_RE.match(n)
        if m:
            return m.group(1)     # 메타데이터 없는 챕터라도 번호는 반환
    return None


# ── check ────────────────────────────────────────────────────────

def check(instance, chapter):
    meta = CHAPTERS.get(chapter)
    if not meta:
        return {"ok": False, "known": False, "chapter": chapter,
                "reason": f"{chapter}장은 아직 자동 채점 도우미가 없어요"}
    if meta.get("observational") or meta.get('assessment_mode') == 'exploration':
        # 고칠 고장이 없는 관찰 실습 — 통과/실패 개념이 없다.
        return {"ok": True, "known": True, "observational": True,
                "chapter": chapter, "title": meta["title"], "goal": meta["goal"],
                "assessment_mode": meta.get('assessment_mode', 'observation'),
                "detail": "관찰·탐구 실습입니다. 단일 자동 성공조건을 사용하지 않습니다."}
    node, argv, needle = meta["success"]
    rc, out, err = node_exec(instance, meta["prefix"], node, argv, 30)
    missing = rc not in (0, 1, 28, 124) or any(s in err for s in (
        'No such container', 'not found', 'Instance is not running', "agent isn't currently running"))
    passed = needle in (out + err) and not missing
    if chapter == '12' and not missing:
        passed = bool(re.search(r'mtu\s*[= ]\s*1400', out + err, re.I))
        if not passed:
            route_rc, route_out, route_err = node_exec(instance, meta['prefix'], node,
                                                      ['ip', 'route', 'get', '10.12.2.10'], 12)
            out += '\n[PMTU route cache]\n' + route_out + route_err
            passed = route_rc == 0 and bool(re.search(r'\bmtu\s+1400\b', route_out))
    conditions = [{'node': node, 'passed': passed, 'rc': rc, 'detail': (out + err)[-600:]}]
    for extra_node, extra_argv, extra_needle in meta.get('additional_success', []):
        extra_rc, extra_out, extra_err = node_exec(instance, meta['prefix'], extra_node, extra_argv, 15)
        extra_ok = extra_rc in (0, 1) and extra_needle in extra_out
        passed = passed and extra_ok
        conditions.append({'node': extra_node, 'passed': extra_ok, 'rc': extra_rc,
                           'detail': (extra_out + extra_err)[-600:]})
    return {"ok": passed, "known": True, "chapter": chapter,
            "title": meta["title"], "goal": meta["goal"],
            "status": 'diagnostic_error' if missing else 'passed' if passed else 'failed',
            "rc": rc, "conditions": conditions, "detail": (out + err).strip()[-300:]}


# ── gather (비파괴 상태 스냅샷) ───────────────────────────────────

def gather(instance, chapter):
    meta = CHAPTERS[chapter]
    snap = []
    for label, node, argv in meta["probes"]:
        rc, out, err = node_exec(instance, meta["prefix"], node, argv, 20)
        txt = (out + err).strip()
        # 길이 제한 — LLM 맥락 폭주 방지
        if len(txt) > 600:
            txt = txt[:600] + " …(생략)"
        snap.append({"label": label, "out": txt})
    return snap


# ── help (단계식 LLM 조언) ────────────────────────────────────────

def load_cfg():
    with open(TUTOR_CFG) as f:
        return json.load(f)


def build_prompt(chapter, snap, passed, attempt):
    meta = CHAPTERS[chapter]
    # 단계식: 도움 요청 횟수(attempt)만큼 힌트를 더 푼다.
    n = max(1, attempt)
    revealed = meta["hints"][:min(n, len(meta["hints"]))]
    level = ("방향만 짚어 주세요(소크라테스식). 절대 답이나 명령을 바로 주지 마세요."
             if n == 1 else
             "조금 더 구체적으로 짚어 주세요. 하지만 최종 명령은 아직 주지 마세요."
             if n == 2 else
             "이제 무엇이 문제인지 명확히 알려주고, 어디를 어떻게 고칠지 구체적으로 안내하세요. "
             "다만 완성된 정답 명령을 통째로 붙여주지는 말고 학습자가 마지막 한 걸음은 직접 하게 하세요.")

    probes = "\n".join(f"[{s['label']}]\n{s['out']}" for s in snap)
    hints = "\n".join(f"- {h}" for h in revealed)
    status = "이미 성공조건을 만족합니다(고쳐졌음)." if passed else "아직 성공조건을 만족하지 못했습니다."

    return f"""너는 네트워크 실습 조교다. 학습자가 지금 막혀서 도움을 요청했다.
이 실습은 일부러 심어둔 고장을 학습자가 관찰→진단→수정하는 형식이다.
너의 목표는 정답을 떠먹여 주는 게 아니라, 학습자가 스스로 원인에 도달하도록 이끄는 것이다.

## 실습
{meta['title']}
목표(성공조건): {meta['goal']}
현재 상태: {status}

## 심어진 고장(학습자에게 직접 말하지 말 것, 너의 참고용)
{meta['fault']}

## 지금 시스템에서 읽어온 실제 상태
{probes}

## 이번 조언에서 허용된 깊이
이번이 {n}번째 도움 요청이다. {level}
참고할 힌트(이 깊이까지만):
{hints}

## 답변 지침
- 한국어로, 3~5문장 이내로 간결하게.
- 위 '실제 상태'의 구체적 수치를 근거로 짚어라(예: 어떤 값이 무엇과 다른지).
- 허용된 깊이를 넘지 마라. 특히 1~2번째 요청에서는 정답 명령을 주지 마라.
- 격려하는 톤으로."""


def build_observe_prompt(chapter, snap, attempt):
    meta = CHAPTERS[chapter]
    n = max(1, attempt)
    revealed = meta["hints"][:min(n, len(meta["hints"]))]
    probes = "\n".join(f"[{s['label']}]\n{s['out']}" for s in snap)
    hints = "\n".join(f"- {h}" for h in revealed)
    return f"""너는 네트워크 실습 조교다. 지금 학습자는 관찰 실습 중이고, 이 챕터에는 고칠 고장이 없다.
정답을 찾는 게 아니라 무엇을 어떻게 관찰하면 개념이 손에 잡히는지 이끌어 주는 게 목표다.

## 실습
{meta['title']}
관찰 목표: {meta['goal']}

## 지금 시스템 상태
{probes}

## 이끌 방향(이번 {n}번째 요청까지)
{hints}

## 답변 지침
- 한국어로 3~4문장. 위 실제 상태를 근거로 "이걸 이렇게 관찰해 보세요" 식으로.
- 이건 고장수리가 아니라 관찰 실습임을 자연스럽게 알려라(고치라고 하지 말 것).
- 격려하는 톤."""


def model_options(cfg):
    """Return public labels only; endpoints/credentials never leave the server."""
    models = cfg.get('models') or {}
    if not models:
        return [{'id': 'default', 'label': '기본 모델'}], 'default'
    if not isinstance(models, dict) or any(not isinstance(v, dict) for v in models.values()):
        raise ValueError('모델 설정이 올바르지 않습니다.')
    default = cfg.get('default_model', next(iter(models)))
    if default not in models:
        raise ValueError('기본 모델 설정이 올바르지 않습니다.')
    return [{'id': key, 'label': str(value.get('label', key))[:80]} for key, value in models.items()], default


def model_config(cfg, model_id=None):
    options, default = model_options(cfg)
    model_id = model_id or default
    if model_id not in {item['id'] for item in options}:
        raise ValueError('선택할 수 없는 모델입니다.')
    selected = dict(cfg)
    selected.update((cfg.get('models') or {}).get(model_id, {}))
    return selected


def call_llm(prompt, model_id=None):
    cfg = load_cfg()
    cfg = model_config(cfg, model_id)
    payload = {
        "model": cfg["model"],
        "messages": prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}],
        "max_tokens": cfg.get("max_tokens", 700),
        "temperature": cfg.get("temperature", 0.3),
    }
    for name in ('top_p', 'top_k', 'reasoning_effort', 'chat_template_kwargs'):
        if name in cfg:
            payload[name] = cfg[name]
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.get('api_key'):
        headers['Authorization'] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(
        cfg["endpoint"], data=body,
        headers=headers)
    timeout = max(10, min(1800, int(cfg.get('request_timeout_seconds', 90))))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    content = d["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError('모델의 최종 답변이 비어 있습니다. max_tokens와 reasoning_effort 설정을 확인하세요.')
    return content.strip()


def help_(instance, chapter, attempt):
    meta = CHAPTERS.get(chapter)
    if not meta:
        # 도우미 메타데이터가 없는 챕터 — 정직하게 안내한다.
        return {"ok": True, "passed": False, "attempt": attempt, "chapter": chapter,
                "title": f"{chapter}장",
                "advice": (f"{chapter}장은 아직 자동 도우미가 준비되지 않았어요. "
                           "각 챕터의 README.md(터미널에서 `doc " + chapter + "`)를 먼저 읽어보고, "
                           "막히면 WALKTHROUGH.md(`walk " + chapter + "`)의 진단 순서를 따라가 보세요.")}
    res = check(instance, chapter)
    snap = gather(instance, chapter)
    if meta.get("observational"):
        # 관찰 실습 — 정답을 찾는 게 아니라 무엇을 관찰할지 이끈다.
        prompt = build_observe_prompt(chapter, snap, attempt)
    else:
        prompt = build_prompt(chapter, snap, res["ok"] and meta.get('assessment_mode') != 'exploration', attempt)
    try:
        advice = call_llm(prompt)
    except Exception as e:
        return {"ok": False, "message": f"LLM 호출 실패: {e}", "passed": res["ok"]}
    return {"ok": True, "passed": res["ok"] and not meta.get("observational", False) and meta.get('assessment_mode') != 'exploration', "attempt": attempt,
            "chapter": chapter, "title": meta["title"], "advice": advice}


# ── CLI ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    # detect: 배포된 챕터 번호만 stdout 으로 (labd 가 씀)
    pd = sub.add_parser("detect")
    pd.add_argument("instance")
    for name in ("check", "gather", "help"):
        p = sub.add_parser(name)
        p.add_argument("instance")
        p.add_argument("chapter", nargs="?")
        if name == "help":
            p.add_argument("--attempt", type=int, default=1)
    a = ap.parse_args()

    if a.cmd == "detect":
        ch = deployed_chapter(a.instance)
        if ch:
            print(ch)
            sys.exit(0)
        sys.exit(1)

    ch = a.chapter or deployed_chapter(a.instance)
    if not ch:
        print(json.dumps({"ok": False, "message": "배포된 챕터를 찾지 못함"},
                         ensure_ascii=False))
        sys.exit(1)

    if a.cmd == "check":
        print(json.dumps(check(a.instance, ch), ensure_ascii=False, indent=2))
    elif a.cmd == "gather":
        print(json.dumps(gather(a.instance, ch), ensure_ascii=False, indent=2))
    elif a.cmd == "help":
        print(json.dumps(help_(a.instance, ch, a.attempt), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
