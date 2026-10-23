/* Learning pack v2 lesson panel (M1): steps, check button, hints, start/reset/stop.
 * The panel stays hidden on VMs without a runner (HTTP 410), so v1 environments look unchanged.
 * Conflicts are shown and the state refreshed; a destructive action is never retried automatically
 * with fresh tokens (contract: 충돌 후 최신 토큰 자동 대입 금지). */
(function(){
  const $=id=>document.getElementById(id);
  const panel=$('lessonpanel'); if(!panel) return;
  const base=()=>(typeof ctlBase==='function'?ctlBase():location.pathname.replace(/[^/]*$/,''));
  const STATUS={pass:'통과',fail:'아직 아님',unverifiable:'판정 불가 · 환경 확인 필요',error:'검증 오류',
    cancelled:'취소됨',untested:'아직 확인 안 함',done:'읽음',unread:'안 읽음'};
  const ENV={idle:'실습 없음',running:'실행 중',starting:'시작 중',resetting:'초기화 중',stopping:'종료 중',
    start_failed:'시작 실패',reset_failed:'초기화 실패',stop_failed:'종료 실패',recovering:'복구 확인 중',
    recovery_required:'복구 필요'};
  const RECOVERY={reboot:'VM 이 다시 시작되어 실습 연결이 끊겼습니다. 초기화하거나 종료하세요.',
    interrupted:'이전 작업이 중간에 끊겼습니다. 다시 실행하지 않았습니다. 초기화하거나 종료하세요.',
    state_damaged:'실습 상태 기록이 손상되었습니다. 자동으로 정리하지 않았습니다. 상단의 환경 초기화(되돌리기)나 환경 교체로 복구하세요.',
    exec_unconfirmed:'확인 명령이 멈췄는지 확인하지 못해 변경을 막았습니다. 상단의 환경 초기화(되돌리기)로 복구하세요.'};
  let st=null, lesson=null, selected=null, working=false, timer=null, loading=false, missingUntil=0;
  const MISSING_BACKOFF=300000;   // a VM without a runner gets one only by replacement; poll rarely

  function uuid(){
    const b=new Uint8Array(16); crypto.getRandomValues(b);
    b[6]=(b[6]&15)|64; b[8]=(b[8]&63)|128;
    const h=[...b].map(x=>x.toString(16).padStart(2,'0')).join('');
    return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20);
  }
  const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  function say(text){ $('lessonstatus').textContent=text||''; }
  async function api(path,body){
    const opt=body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    const r=await fetch(base()+path,opt);
    let j={}; try{ j=await r.json(); }catch(e){}
    return {status:r.status,body:j};
  }
  const running=()=>{ const r=$('running'); return r&&!r.classList.contains('hide'); };

  async function load(){
    if(loading||!running()) return; loading=true;
    try{
      const r=await api('lesson/state');
      if(r.status===410){ panel.hidden=true; st=null; missingUntil=Date.now()+MISSING_BACKOFF; return; }
      missingUntil=0;
      if(r.status!==200){ panel.hidden=false; say(r.body.message||'단계형 실습 상태를 읽지 못했습니다.'); return; }
      st=r.body; panel.hidden=false;
      const pick=$('lessonpick');
      if(!pick.options.length){
        for(const l of st.catalog){ const o=document.createElement('option'); o.value=l.id; o.textContent=l.title; pick.appendChild(o); }
      }
      const want=(st.environment.lesson&&st.catalog.some(l=>l.id===st.environment.lesson))?st.environment.lesson:(selected||pick.value);
      if(want&&want!==selected){ selected=want; pick.value=want; lesson=null; }
      if(!lesson&&selected){
        const lr=await api('lesson/'+selected);
        if(lr.status===200) lesson=lr.body.lesson;
      }
      render();
    }catch(e){ say('연결에 실패했습니다.'); }
    finally{ loading=false; }
  }

  function envText(env){
    if(!env.ok&&env.error) return env.message||'실습 환경 상태를 확인할 수 없습니다.';
    let t=(ENV[env.state]||env.state);
    if(env.lesson){ const l=st.catalog.find(x=>x.id===env.lesson); t+=' · '+(l?l.title:env.lesson); }
    if(env.observed) t+=' · 세대 '+env.observed.generation;
    return t;
  }

  function render(){
    if(!st) return;
    const env=st.environment, a=st.attempt, pack=selected?selected.split('/')[0]:null;
    const current=env.bundles_current&&pack?env.bundles_current[pack]!==false:true;
    const mine=env.lesson===selected, allowed=(env.recovery&&env.recovery.allowed)||[];
    const notes=[];
    $('lessonenv').textContent=envText(env);
    if(!current) notes.push('이 VM 의 교재 묶음이 서버와 다릅니다. 환경을 교체해야 새 실습을 쓸 수 있습니다.');
    if(env.state==='recovery_required') notes.push(RECOVERY[(env.recovery||{}).reason]||'복구가 필요합니다.');
    if(env.failure&&env.failure.stage) notes.push(env.failure.stage+' 단계에서 실패했습니다. 다시 시작하거나 종료하세요.');
    if(env.state==='running'&&env.runtime&&env.runtime.matches===false) notes.push('터미널에서 실습이 바뀌었습니다. 다시 시작하면 이 실습으로 맞춥니다.');
    if(env.paused) notes.push('운영자가 단계형 실습 접수를 잠시 멈췄습니다. 터미널은 그대로 쓸 수 있습니다.');
    if(st.shared) notes.push('공용 환경입니다. 다른 방문자가 실습을 바꿀 수 있고 개인 완료 기록에 남지 않습니다.');
    if(a&&a.origin==='adopted_cli') notes.push('터미널에서 시작한 실습을 다시 배포하지 않고 이어 받았습니다.');
    $('lessonnote').innerHTML=notes.map(n=>'<div>'+esc(n)+'</div>').join('');
    $('lessonnote').hidden=!notes.length;
    const busy=working||env.paused||(st.jobs&&st.jobs.length>0)||['starting','resetting','stopping','recovering'].includes(env.state);
    const can={
      start:current&&['idle','running','start_failed','reset_failed'].includes(env.state),
      reset:current&&mine&&(['running','reset_failed'].includes(env.state)||allowed.includes('reset')),
      stop:mine&&(['running','start_failed','reset_failed','stop_failed'].includes(env.state)||allowed.includes('stop')),
      solve:current&&mine&&env.state==='running'&&env.ready&&lesson&&lesson.solvable};
    $('lessonstart').disabled=busy||!can.start||!selected;
    $('lessonreset').disabled=busy||!can.reset;
    $('lessonstop').disabled=busy||!can.stop;
    $('lessonsolve').hidden=!(lesson&&lesson.solvable);
    $('lessonsolve').disabled=busy||!can.solve;
    const done=a&&a.completed_at;
    $('lessondone').hidden=!done;
    if(done) $('lessondone').textContent=(a.assisted?'해설 적용 후 완료했습니다':'완료했습니다')+' ('+a.completed_at.replace('T',' ').slice(0,16)+'). 초기화하거나 종료해도 완료 기록은 남습니다.';
    else if(a&&a.assisted){ $('lessondone').hidden=false; $('lessondone').textContent='해설을 적용했습니다. 확인 버튼으로 결과를 확인하세요. 완료 기록에는 해설 적용 후 완료로 남습니다.'; }
    renderSteps(busy,current,mine&&env.state==='running'&&env.ready&&a);
  }

  function renderSteps(busy,current,checkable){
    const list=$('lessonsteps');
    if(!lesson){ list.innerHTML=''; return; }
    const a=st.attempt, states=(a&&a.steps)||{};
    const openDetails=new Set([...list.querySelectorAll('details[open]')].map(d=>d.dataset.key));
    list.innerHTML='';
    lesson.steps.forEach((step,i)=>{
      const li=document.createElement('li'); li.className='lesson-step';
      const s=states[step.id]||(step.kind==='read'?'unread':'untested');
      let h='<div class=lesson-step-head><span class=lesson-step-no>'+(i+1)+'</span><span class=t>'+esc(step.title||step.id)+'</span>'+
        (step.required?'<span class=lesson-req>필수</span>':'')+'<span class="lesson-chip s-'+esc(s)+'">'+esc(STATUS[s]||s)+'</span></div>';
      if(step.kind==='read'){
        h+='<details data-key="read-'+esc(step.id)+'"><summary>본문 보기</summary><div class=lesson-body>'+step.html+'</div></details>';
        h+='<button type=button class=lesson-small data-view="'+esc(step.id)+'"'+((!a||s==='done')?' disabled':'')+'>읽음 표시</button>';
      }else if(step.kind==='task'){
        h+='<p class=d>'+esc(step.goal||'')+'</p>';
        if(step.html) h+='<details data-key="task-'+esc(step.id)+'"><summary>단계 설명</summary><div class=lesson-body>'+step.html+'</div></details>';
        h+='<div class=lesson-row><button type=button class=primary data-check="'+esc(step.id)+'"'+((busy||!checkable||!current)?' disabled':'')+'>확인</button>';
        const opened=((a&&a.hints_opened)||{})[step.id]||[];
        if(step.hints) h+='<button type=button data-hint="'+esc(step.id)+'"'+((!a||opened.length>=step.hints)?' disabled':'')+'>힌트 '+opened.length+'/'+step.hints+'</button>';
        h+='</div><ol class=lesson-hints id="hints-'+esc(step.id)+'"></ol>';
        const last=a&&a.last_check&&a.last_check[step.id];
        if(last&&last.items&&last.items.length){
          h+='<details data-key="items-'+esc(step.id)+'"><summary>마지막 확인 결과'+(last.stale?' · 반영되지 않음(환경이 바뀜)':'')+'</summary>'+
            last.items.map(it=>'<div class=lesson-item><b>'+esc(it.node)+'</b> · '+esc(STATUS[it.result]||it.result)+(it.fallback_used?' · 보조 확인':'')+
            (it.message?'<div>'+esc(it.message)+'</div>':'')+'<pre>'+esc(it.detail)+'</pre></div>').join('')+'</details>';
        }
      }
      li.innerHTML=h; list.appendChild(li);
    });
    list.querySelectorAll('details').forEach(d=>{ if(openDetails.has(d.dataset.key)) d.open=true; });
    list.querySelectorAll('.lesson-body pre').forEach(addCopy);
    list.querySelectorAll('.lesson-body a').forEach(x=>{ x.target='_blank'; x.rel='noopener noreferrer'; });
    restoreHints();
  }

  // Code blocks are preview + copy only: nothing is typed into or run in the terminal.
  function addCopy(pre){
    const b=document.createElement('button'); b.type='button'; b.className='lesson-copy'; b.textContent='복사';
    b.onclick=async()=>{
      const text=pre.innerText;
      try{ await navigator.clipboard.writeText(text); b.textContent='복사됨'; }
      catch(e){ const r=document.createRange(); r.selectNodeContents(pre); const s=getSelection(); s.removeAllRanges(); s.addRange(r); b.textContent='선택됨 · Ctrl+C'; }
      setTimeout(()=>{ b.textContent='복사'; },1500);
    };
    pre.classList.add('lesson-pre'); pre.appendChild(b);
  }

  const hintText={};
  function restoreHints(){
    for(const [step,texts] of Object.entries(hintText)){
      const ol=$('hints-'+step); if(!ol) continue;
      ol.innerHTML=texts.map(t=>'<li>'+esc(t)+'</li>').join('');
    }
  }

  async function job(path,body,label){
    working=true; render(); say(label+' 요청 중…');
    try{
      let r=await api(path,body);
      if(r.status===410){ panel.hidden=true; return; }
      if(r.status!==202&&r.status!==200){ say(r.body.message||('요청이 거절되었습니다 ('+r.status+').')); return; }
      await follow(r.body,label);
    }catch(e){ say('연결에 실패했습니다. 상태를 새로 확인하세요.'); }
    finally{ working=false; await load(); }
  }

  // Poll a job to its end. Also used after a page reload to pick up work that is still running.
  async function follow(j,label){
    while(j.state==='queued'||j.state==='running'){
      say(label+' 진행 중…');
      await new Promise(res=>setTimeout(res,1200));
      const p=await api('lesson/jobs/'+j.job_id);
      if(p.status!==200){ say('작업 상태를 확인하지 못했습니다.'); return; }
      j=p.body;
    }
    say(describe(label,j));
  }
  const LABEL={start:'시작',reset:'초기화',stop:'종료',solve:'해설 적용',check:'확인'};
  async function resume(){
    if(working||!st||!st.jobs||!st.jobs.length) return;
    const pending=st.jobs[0];
    working=true; render(); say('진행 중인 '+(LABEL[pending.action]||pending.action)+' 작업을 이어서 확인합니다…');
    try{ await follow({job_id:pending.job_id,state:pending.state},LABEL[pending.action]||pending.action); }
    catch(e){ say('작업 상태를 확인하지 못했습니다.'); }
    finally{ working=false; await load(); }
  }

  function describe(label,j){
    const res=j.result||{};
    if(!res.ok){
      if(res.error==='failed') return label+' 실패: '+(res.message||'');
      return res.message||(label+'에 실패했습니다.');
    }
    const run=res.runner||{}, pr=res.progress||{};
    if(run.noop) return '이미 실행 중인 실습입니다. 컨테이너와 변경 사항을 유지했습니다.';
    if(run.result){
      const s=run.result.status;
      if(pr.stale) return '확인하는 사이 환경이 바뀌어 이 결과는 반영하지 않았습니다.';
      if(pr.completed_now) return '통과! 실습을 완료했습니다.';
      const msg=(run.result.items||[]).map(it=>it.message).filter(Boolean).join(' ');
      return '확인 결과: '+(STATUS[s]||s)+(msg?' — '+msg:'');
    }
    if(label==='해설 적용') return '해설을 적용했습니다. 확인 버튼으로 다시 확인하세요.';
    return label+' 완료';
  }

  function act(action){
    if(!st||!st.environment.observed||!selected) return;
    const env=st.environment;
    const msg={start:(env.lesson&&env.lesson!==selected)?'지금 실습을 종료하고 새 실습을 시작합니다. 컨테이너 안의 변경 사항은 사라집니다.':null,
      reset:'실습 토폴로지를 처음 상태로 다시 만듭니다. 컨테이너 안의 변경 사항은 사라집니다.',
      stop:'실습 토폴로지를 철거합니다. 컨테이너 안의 변경 사항은 사라집니다.',
      solve:'해설을 실습 환경에 적용합니다. 이 회차의 완료 기록에는 해설 적용 후 완료로 남습니다.'}[action];
    if(msg&&!confirm(msg)) return;
    const label=LABEL[action];
    job('lesson/'+selected+'/'+action,{request_id:uuid(),expected:env.observed},label);
  }

  // The panel's own act() re-enables every button when it finishes, so recompute the allowed
  // state from the last status right before acting instead of trusting `disabled` alone.
  function allowed(selector){ render(); const b=panel.querySelector(selector); return b&&!b.disabled; }
  panel.addEventListener('click',async e=>{
    const t=e.target.closest('button'); if(!t||t.disabled||t.id) return;
    if(t.dataset.check){
      if(!allowed('[data-check="'+t.dataset.check+'"]')) return;
      job('lesson/'+selected+'/step/'+t.dataset.check+'/check',{request_id:uuid(),expected:st.environment.observed},'확인');
    }else if(t.dataset.view){
      const r=await api('lesson/'+selected+'/step/'+t.dataset.view+'/view',{});
      say(r.status===200?'읽음으로 표시했습니다.':(r.body.message||'표시하지 못했습니다.')); load();
    }else if(t.dataset.hint){
      const step=t.dataset.hint, opened=((st.attempt&&st.attempt.hints_opened)||{})[step]||[];
      const n=opened.length+1;
      const r=await api('lesson/'+selected+'/step/'+step+'/hint',{n});
      if(r.status===200){ (hintText[step]=hintText[step]||[])[n-1]=r.body.text; }
      else say(r.body.message||'힌트를 열지 못했습니다.');
      await load();
    }
  });
  $('lessonstart').onclick=()=>{ if(allowed('#lessonstart')) act('start'); };
  $('lessonreset').onclick=()=>{ if(allowed('#lessonreset')) act('reset'); };
  $('lessonstop').onclick=()=>{ if(allowed('#lessonstop')) act('stop'); };
  $('lessonsolve').onclick=()=>{ if(allowed('#lessonsolve')) act('solve'); };
  $('lessonpick').onchange=e=>{ selected=e.target.value; lesson=null; load(); };
  $('lessonrefresh').onclick=()=>load();

  // Reload already-opened hint texts after a page reload (server keeps which were opened).
  async function reloadHints(){
    if(!st||!st.attempt||!lesson) return;
    for(const [step,ns] of Object.entries(st.attempt.hints_opened||{})){
      for(const n of ns){ if(!(hintText[step]||[])[n-1]){
        const r=await api('lesson/'+selected+'/step/'+step+'/hint',{n});
        if(r.status===200) (hintText[step]=hintText[step]||[])[n-1]=r.body.text;
      }}
    }
    restoreHints();
  }
  function tick(){
    if(Date.now()<missingUntil) return;
    if(running()&&!document.hidden&&!working) load().then(reloadHints).then(resume);
    else if(!running()){ panel.hidden=true; }
  }
  setTimeout(tick,600);
  timer=setInterval(tick,15000);
  document.addEventListener('visibilitychange',()=>{ if(!document.hidden) tick(); });
})();
