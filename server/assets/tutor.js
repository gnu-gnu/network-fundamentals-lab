/* Render untrusted model/terminal text only through textContent. */
(() => {
  const log = document.getElementById('tutorlog');
  if (!log) return;
  const key = 'netlab-tutor:' + location.pathname;
  const panel=document.getElementById('tutorpanel'), launcher=document.getElementById('tutorlauncher');
  let opened=localStorage.getItem(key+':open')==='true';
  let returnScroll=null, closing=false, motion=null, motionSequence=0;
  const panelScroll=window.netlabPanelScroll||(window.netlabPanelScroll={value:null});
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  function layout() {
    panel.hidden=!opened&&!closing;
    panel.inert=!opened;
    panel.setAttribute('aria-hidden',String(!opened));
    launcher.setAttribute('aria-expanded',String(opened));
    launcher.setAttribute('aria-label',opened?'AI 튜터 닫기':'AI 튜터 열기');
    document.body.classList.toggle('tutor-open',(opened||closing)&&!document.getElementById('running').classList.contains('hide'));
  }
  function toggle(open) {
    if(open===opened) return;
    const sequence=++motionSequence;
    const current=panel.hidden?null:{opacity:getComputedStyle(panel).opacity,transform:getComputedStyle(panel).transform};
    if(motion) {motion.cancel();motion=null;}
    if(open&&!opened&&innerWidth<1400&&returnScroll===null) {
      returnScroll=panelScroll.value??scrollY;panelScroll.value=returnScroll;
    }
    opened=open;closing=!open;localStorage.setItem(key+':open',String(opened));layout();
    if(opened) {
      window.dispatchEvent(new CustomEvent('netlab:panel-open',{detail:'tutor'}));
      if(innerWidth<1400) panel.scrollIntoView({block:'start',behavior:'instant'});
      document.getElementById('tutorinput').focus({preventScroll:true});
    } else {
      launcher.focus({preventScroll:true});
    }
    function finish() {
      if(sequence!==motionSequence) return;
      if(motion) {motion.cancel();motion=null;}
      closing=false;layout();
      if(!opened) {
        const peerOpen=document.getElementById('communitylauncher')?.getAttribute('aria-expanded')==='true';
        if(!peerOpen&&returnScroll!==null) {
          window.scrollTo({top:returnScroll,behavior:'instant'});panelScroll.value=null;
        }
        returnScroll=null;
        window.dispatchEvent(new CustomEvent('netlab:panel-settled',{detail:'tutor'}));
      }
    }
    if(reducedMotion.matches||!panel.animate) {finish();return;}
    const collapsed={opacity:0,transform:innerWidth>=1400?'translateY(18px) scale(.965)':'translateY(14px) scale(.99)'};
    const expanded={opacity:1,transform:'translateY(0) scale(1)'};
    motion=panel.animate([current||collapsed,opened?expanded:collapsed],{
      duration:opened?300:190,easing:opened?'cubic-bezier(.16,1,.3,1)':'cubic-bezier(.4,0,1,1)',fill:'both'
    });
    motion.onfinish=finish;
  }
  launcher.onclick=()=>toggle(!opened);
  window.addEventListener('netlab:panel-open',event=>{if(event.detail!=='tutor'&&opened)toggle(false);});
  window.addEventListener('netlab:panel-settled',event=>{
    if(event.detail!=='tutor'&&opened&&innerWidth<1400)panel.scrollIntoView({block:'start',behavior:'instant'});
  });
  document.getElementById('tutorclose').onclick=()=>toggle(false);
  panel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();toggle(false);}});
  new MutationObserver(layout).observe(document.getElementById('running'),{attributes:true,attributeFilter:['class']});
  layout();
  let sid = localStorage.getItem(key) || '', sending = false, busy = false, timer;
  const status = document.getElementById('tutorstatus');
  const modelButton=document.getElementById('tutormodel'),modelDialog=document.getElementById('tutormodeldialog');
  let models=[],selectedModel=localStorage.getItem(key+':model')||'';
  modelButton.disabled=true;
  modelButton.onclick=()=>modelDialog.showModal();
  modelDialog.addEventListener('keydown',event=>event.stopPropagation());
  modelDialog.addEventListener('click',event=>{if(event.target===modelDialog)modelDialog.close();});
  function renderModels(data) {
    if(!data.models)return;
    models=data.models;
    if(!models.some(model=>model.id===selectedModel))selectedModel=data.default_model||models[0]?.id||'';
    const selected=models.find(model=>model.id===selectedModel);
    modelButton.onclick=()=>modelDialog.showModal();
    modelButton.textContent=selected?'모델: '+selected.label+' ▾':'모델 선택';
    modelButton.disabled=busy||sending||!models.length;
    const options=document.getElementById('tutormodeloptions');options.replaceChildren();
    for(const model of models){
      const button=document.createElement('button');button.type='button';
      button.className='tutor-model-option';button.textContent=model.label;
      button.setAttribute('aria-pressed',String(model.id===selectedModel));
      button.disabled=busy||sending;
      button.onclick=()=>{
        selectedModel=model.id;localStorage.setItem(key+':model',selectedModel);
        sessionStorage.removeItem(key+':request');renderModels(data);modelDialog.close();modelButton.focus();
      };
      options.append(button);
    }
  }
  let modelLoad=null;
  async function loadModels() {
    if(modelLoad)return modelLoad;
    modelLoad=(async()=>{
      try {
        const response=await fetch(ctlBase()+'tutor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'models'})});
        const data=await response.json();
        if(!response.ok||!data.ok)throw new Error(data.message||'모델 목록을 불러오지 못했습니다.');
        renderModels(data);
      } catch(e) {modelButton.textContent='모델 목록 다시 불러오기';modelButton.disabled=false;
        modelButton.onclick=async()=>{await loadModels();if(models.length)modelDialog.showModal();};
      } finally {modelLoad=null;}
    })();
    return modelLoad;
  }
  let sessions;
  try { sessions = JSON.parse(localStorage.getItem(key + ':sessions') || '[]'); } catch { sessions = []; }
  if (!Array.isArray(sessions)) sessions = [];
  function remember(data) {
    const known = sessions.find(s => s.id === sid);
    const label = data.messages[0]?.question.slice(0, 30) || '새 대화 · ' + new Date().toLocaleString();
    if (!known) sessions.unshift({id: sid, label});
    else if (data.messages.length) known.label = label;
    sessions = sessions.slice(0, 20);
    localStorage.setItem(key + ':sessions', JSON.stringify(sessions));
    const select = document.getElementById('tutorconversations');
    select.replaceChildren();
    for (const s of sessions) {
      const option = document.createElement('option'); option.value = s.id; option.textContent = s.label;
      option.selected = s.id === sid; select.append(option);
    }
  }
  function sourceUrl(e) { return ctlBase() + 'guide/' + e.source + '?sha=' + encodeURIComponent(e.sha256 || '') + '#L' + (e.line || 1); }
  function referenceTitle(e) {
    if(e.title)return e.title;
    // Older saved conversations used raw repository paths as labels.
    const chapter=e.label.match(/\b([0-9]{2})-/)?.[1];
    const prefix=chapter?chapter+'장 ':'';
    if(e.label.includes('/wiki/'))return '개념 해설';
    if(e.label.includes('.clab.yml'))return prefix+'참고 토폴로지';
    if(e.label.includes('WALKTHROUGH'))return prefix+'실습 해설';
    return prefix+'교재';
  }
  function answer(parent, text, evidence) {
    const sources = new Map(evidence.filter(e=>e.kind==='reference').map(e=>[e.label.match(/^\[(G\d+)\]/)?.[1],e]));
    function inline(text) {
      for (const piece of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)) {
        if (piece.startsWith('**') && piece.endsWith('**')) {
          const strong = document.createElement('strong'); strong.textContent = piece.slice(2,-2); parent.append(strong);
        } else if (piece.startsWith('`') && piece.endsWith('`')) {
          const code = document.createElement('code'); code.textContent = piece.slice(1,-1); parent.append(code);
        } else parent.append(document.createTextNode(piece));
      }
    }
    function prose(chunk) {
      let start=0;
      for (const match of chunk.matchAll(/\[(G\d+)\]/g)) {
        inline(chunk.slice(start,match.index));
        const ref=sources.get(match[1]);
        if (ref && /^[a-f0-9]{24}$/.test(ref.source)) {
          const link=document.createElement('a'); link.href=sourceUrl(ref);
          link.target='_blank';link.rel='noopener';link.textContent=match[0];link.title=referenceTitle(ref);parent.append(link);
        } else parent.append(document.createTextNode(match[0]));
        start=match.index+match[0].length;
      }
      inline(chunk.slice(start));
    }
    const chunks = text.split(/```[^\n]*\n([\s\S]*?)```/g);
    chunks.forEach((chunk, index) => {
      if (index % 2 === 0) { prose(chunk); return; }
      const pre = document.createElement('pre'); pre.className = 'tutor-code'; pre.textContent = chunk;
      const copy = document.createElement('button'); copy.textContent = '명령 복사';
      copy.onclick = async () => {
        try {
          if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(chunk);
          else {
            const field = document.createElement('textarea'); field.value = chunk;
            document.body.append(field); field.select(); document.execCommand('copy'); field.remove();
          }
          copy.textContent = '복사됨';
        } catch { copy.textContent = '직접 선택해서 복사하세요'; }
      };
      parent.append(pre, copy);
    });
  }
  async function api(body) {
    const response = await fetch(ctlBase() + 'tutor', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...body, session: sid})});
    const data = await response.json();
    if (!data.ok) throw new Error(data.message || '요청 실패');
    sid = data.session; localStorage.setItem(key, sid);
    remember(data);
    return data;
  }
  function render(data) {
    const share = document.getElementById('tutorshare');
    share.disabled = data.terminal_sharing_allowed === false;
    if (share.disabled) { share.checked = false; share.parentElement.title = '공용 VM: 방문자별 기록 분리 전까지 전달하지 않습니다.'; }
    document.getElementById('tutorcontext').textContent = '최근 대화에서 확인한 실습: ' + (data.chapter || '미배포 또는 확인 전');
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 70;
    const sessionChanged = log.dataset.session !== data.session;
    if (sessionChanged) log.replaceChildren();
    log.dataset.session = data.session;
    let pending = false;
    let previousContext = '';
    for (const msg of data.messages) {
      const id = msg.id || String(msg.created);
      const existing = Array.from(log.children).find(el => el.dataset.job === id);
      pending ||= msg.status === 'pending';
      const boundary = previousContext && msg.context && previousContext !== msg.context;
      previousContext = msg.context || previousContext;
      if (existing && existing.dataset.status === msg.status) continue;
      const item = document.createElement('div'); item.className = 'tutor-message';
      item.dataset.job = id; item.dataset.status = msg.status;
      if (boundary) {
        const marker = document.createElement('p'); marker.textContent = '— 실습 환경 변경: 이전 관찰과 분리 —'; item.append(marker);
      }
      const question = document.createElement('p'); question.className = 'tutor-question';
      question.textContent = '나: ' + msg.question;
      const response = document.createElement('div'); response.className = 'tutor-answer';
      const modelLabel=(data.models||[]).find(model=>model.id===msg.model_id)?.label;
      if(modelLabel){const badge=document.createElement('div');badge.className='d';badge.textContent=modelLabel;item.append(badge);}
      answer(response, msg.answer || '응답을 준비하고 있습니다…',msg.evidence);
      item.append(question, response);
      if (existing) existing.replaceWith(item); else log.append(item);
      const diagnostics=msg.evidence.filter(e=>e.kind!=='reference');
      const references=msg.evidence.filter(e=>e.kind==='reference'&&/^[a-f0-9]{24}$/.test(e.source));
      const group = document.createElement('details'), heading = document.createElement('summary');
      heading.textContent = '진단 근거 (' + diagnostics.length + ')';
      group.append(heading);
      if (diagnostics.length) item.append(group);
      for (const e of diagnostics) {
        const details = document.createElement('details'), summary = document.createElement('summary');
        summary.textContent = e.label + ' · ' + new Date(e.time * 1000).toLocaleTimeString();
        if (e.active) summary.textContent += ' · 튜터 능동 진단(캐시 변경 가능)';
        const output = document.createElement('pre');
        output.style.whiteSpace = 'pre-wrap';
        output.textContent = e.error || e.output;
        details.append(summary, output);
        group.append(details);
      }
      if(references.length){
        const sources=document.createElement('nav');sources.className='tutor-sources';sources.setAttribute('aria-label','답변 참고 문서');
        const label=document.createElement('span');label.textContent='참고 문서';sources.append(label);
        for(const ref of references){
          const link=document.createElement('a');link.href=sourceUrl(ref);link.target='_blank';link.rel='noopener';
          link.textContent=(ref.label.match(/^\[(G\d+)\]/)?.[0]||'')+' '+referenceTitle(ref);sources.append(link);
        }
        item.append(sources);
      }
      pending ||= msg.status === 'pending';
    }
    busy = pending;
    renderModels(data);
    for (const button of panel.querySelectorAll('.tutor-actions button, #tutorsend')) {
      if ((button.getAttribute('onclick') || '').includes('tutorSend')) button.disabled = pending;
    }
    const level = data.messages.at(-1)?.help_level || 0;
    status.textContent = data.stale ? '환경이 바뀌었습니다. 새 대화를 시작하세요.' : pending ? '응답을 준비 중입니다…' :
      '도움 단계: ' + ['관찰 방향', '증거 비교', '원인 설명', '수정 명령'][level] +
      (share.disabled ? ' · 공용 VM의 명령 기록 전달은 비활성화됩니다.' : '');
    clearTimeout(timer);
    if (pending) timer = setTimeout(load, 2000);
    if (follow || sessionChanged) log.scrollTop = log.scrollHeight;
  }
  async function load() {
    try { render(await api({action: 'history'})); }
    catch (e) { status.textContent = e.message; }
  }
  window.tutorSend = async (message, inspect = false, mode = 'chat') => {
    if (sending || busy) return;
    const input = document.getElementById('tutorinput');
    message = message || input.value.trim();
    if (!message && mode === 'reflection') message = '원인·관찰 근거·네트워크 원리를 내가 먼저 설명할 수 있게 질문해 줘.';
    if (!message) return;
    sending = true;
    modelButton.disabled=true;modelDialog.close();
    // Persist the request ID so an uncertain network failure can retry idempotently.
    let request = sessionStorage.getItem(key + ':request');
    let old = sessionStorage.getItem(key + ':message');
    if (!request || old !== message) request = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
    sessionStorage.setItem(key + ':request', request);
    sessionStorage.setItem(key + ':message', message);
    try {
      render(await api({action: 'send', message, inspect, mode, request, model:selectedModel||undefined,
        share_terminal: document.getElementById('tutorshare').checked}));
      if(input.value.trim()===message)input.value='';
      sessionStorage.removeItem(key + ':request');
    } catch (e) { status.textContent = e.message + ' 다시 보내면 재시도합니다.'; }
    finally {
      sending = false;modelButton.disabled=busy||!models.length;
      for(const option of document.querySelectorAll('.tutor-model-option'))option.disabled=busy;
    }
  };
  const input=document.getElementById('tutorinput');
  let composing=false;
  input.addEventListener('compositionstart',()=>{composing=true;});
  input.addEventListener('compositionend',()=>{composing=false;});
  input.addEventListener('keydown',event=>{
    if(event.key!=='Enter'||event.shiftKey||event.isComposing||composing||event.keyCode===229)return;
    event.preventDefault();
    if(!event.repeat)window.tutorSend();
  });
  window.tutorNew = async () => {
    try { render(await api({action: 'new'})); } catch (e) { status.textContent = e.message; }
  };
  window.tutorSelect = selected => {
    if (!sessions.some(s => s.id === selected)) return;
    sid = selected; localStorage.setItem(key, sid); clearTimeout(timer); load();
  };
  if(!sid)log.textContent = '실습 목표를 살펴보거나, 궁금한 내용을 자유롭게 물어보세요.';
  loadModels();
  load();
})();
