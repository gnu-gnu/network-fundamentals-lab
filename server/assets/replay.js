/*
 * 세션 리플레이 — 드롭다운에서 고르면 모달(좌 재생창 / 우 타임라인)로 크게 재생.
 * 관리자·학습자 공용. window.LAB_REPLAY = { admin: true|false }.
 *   admin  : 계정 드롭다운 + 세션 드롭다운 (URL 에 user 파라미터)
 *   student: 세션 드롭다운만 (본인, user 파라미터 없음)
 * 마운트: <div id="replay-root"></div>. AsciinemaPlayer 전역과 replay.css 선행 필요.
 */
(function () {
  var CFG = window.LAB_REPLAY || { admin: false };
  var root = document.getElementById("replay-root");
  if (!root) return;

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }
  function tsLabel(sid) {
    var m = sid.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/);
    return m ? (m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5]) : sid;
  }
  function mmss(t) {
    var m = Math.floor(t / 60), s = Math.floor(t % 60);
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }
  async function getJSON(url) {
    var r = await fetch(url, { cache: "no-store" });
    return r.json();
  }

  // ── 모달 ──
  var player = null;
  function closeModal() {
    if (player) { try { player.dispose(); } catch (e) {} player = null; }
    var b = document.querySelector(".rv-back");
    if (b) b.remove();
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape") closeModal(); }

  async function openModal(user, sid) {
    closeModal();
    var q = (CFG.admin ? "user=" + encodeURIComponent(user) + "&" : "") +
            "session=" + encodeURIComponent(sid);

    var back = el("div", "rv-back");
    back.addEventListener("click", function (e) { if (e.target === back) closeModal(); });
    var modal = el("div", "rv-modal");

    var head = el("div", "rv-head");
    head.appendChild(el("div", null,
      '<div class=ti>' + esc(tsLabel(sid)) + '</div>' +
      '<div class=sub>' + (CFG.admin ? esc(user) + " · " : "") + esc(sid) + '</div>'));
    var actions = el("div", "rv-actions");
    var tlBtn = el("button", "rv-btn", "타임라인 숨기기");
    var x = el("button", "rv-x", "&times;");
    x.addEventListener("click", closeModal);
    actions.appendChild(tlBtn);
    actions.appendChild(x);
    head.appendChild(actions);
    modal.appendChild(head);

    var body = el("div", "rv-body");
    var play = el("div", "rv-play");
    var tl = el("div", "rv-tl");
    tl.appendChild(el("div", "rv-tlhint", "명령을 누르면 그 순간으로 이동합니다"));
    body.appendChild(play);
    body.appendChild(tl);
    modal.appendChild(body);
    back.appendChild(modal);
    document.body.appendChild(back);
    document.addEventListener("keydown", onKey);

    // 타임라인 접기/펴기 — 재생창을 100% 폭까지 넓힌다.
    tlBtn.addEventListener("click", function () {
      var collapsed = body.classList.toggle("tl-collapsed");
      tlBtn.textContent = collapsed ? "타임라인 보기" : "타임라인 숨기기";
      // asciinema-player 는 컨테이너 크기 변화를 자동 반영하지 않으므로 다시 생성한다.
      recreate();
    });

    function recreate() {
      if (player) { try { player.dispose(); } catch (e) {} player = null; }
      play.innerHTML = "";
      requestAnimationFrame(function () {
        if (!document.body.contains(play)) return;
        player = AsciinemaPlayer.create("cast?" + q, play,
          { fit: "width", theme: "asciinema", idleTimeLimit: 2 });
      });
    }
    recreate();

    // 타임라인 내용
    try {
      var j = await getJSON("timeline?" + q);
      var it = j.items || [];
      if (!it.length) {
        tl.appendChild(el("div", "rv-empty", "명령 기록 없음"));
      } else {
        it.forEach(function (v) {
          var cls = v.kind === "event" ? "ev" : (v.rc ? "fail" : "");
          var txt = v.kind === "event"
            ? ("▸ " + v.label + (v.detail ? " " + v.detail : ""))
            : (v.cmd + (v.rc ? "  ✗" + v.rc : ""));
          var row = el("div", "rv-tlrow " + cls,
            '<span class=tt>' + mmss(v.t) + '</span>' +
            '<span class=tc>' + esc(txt) + '</span>');
          row.addEventListener("click", function () {
            if (player) { try { player.seek(v.t); player.play(); } catch (e) {} }
          });
          tl.appendChild(row);
        });
      }
    } catch (e) {
      tl.appendChild(el("div", "rv-empty", "타임라인 조회 실패"));
    }
  }

  // ── 세션 드롭다운 ──
  function fillSessions(sel, sessions) {
    if (!sessions.length) {
      sel.innerHTML = '<option value="">기록 없음</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = '<option value="">— 세션 선택 —</option>' +
      sessions.map(function (x) {
        return '<option value="' + x.session + '">' +
          tsLabel(x.session) + " · 명령 " + x.cmds + "개</option>";
      }).join("");
  }

  async function initStudent() {
    var bar = el("div", "rv-bar");
    var sel = el("select", "rv-sel");
    sel.innerHTML = '<option value="">불러오는 중…</option>';
    bar.appendChild(el("span", "rv-lbl", "세션"));
    bar.appendChild(sel);
    root.appendChild(bar);
    try {
      var j = await getJSON("sessions");
      fillSessions(sel, j.sessions || []);
    } catch (e) { sel.innerHTML = '<option>불러오기 실패</option>'; }
    sel.addEventListener("change", function () {
      if (sel.value) openModal(null, sel.value);
    });
  }

  async function initAdmin() {
    var bar = el("div", "rv-bar");
    var uSel = el("select", "rv-sel");
    uSel.innerHTML = '<option value="">— 계정 —</option>';
    var sSel = el("select", "rv-sel");
    sSel.innerHTML = '<option value="">세션</option>';
    sSel.disabled = true;
    bar.appendChild(el("span", "rv-lbl", "계정"));
    bar.appendChild(uSel);
    bar.appendChild(el("span", "rv-lbl", "세션"));
    bar.appendChild(sSel);
    root.appendChild(bar);

    try {
      var j = await getJSON("list");
      (j.accounts || []).forEach(function (u) {
        var o = document.createElement("option");
        o.value = u; o.textContent = u; uSel.appendChild(o);
      });
    } catch (e) {}

    uSel.addEventListener("change", async function () {
      var u = uSel.value;
      sSel.innerHTML = '<option value="">세션</option>';
      sSel.disabled = true;
      if (!u) return;
      sSel.innerHTML = '<option value="">불러오는 중…</option>';
      try {
        var r = await getJSON("sessions?user=" + encodeURIComponent(u));
        fillSessions(sSel, r.sessions || []);
      } catch (e) { sSel.innerHTML = '<option>실패</option>'; }
    });
    sSel.addEventListener("change", function () {
      if (uSel.value && sSel.value) openModal(uSel.value, sSel.value);
    });
  }

  if (CFG.admin) initAdmin(); else initStudent();
})();
