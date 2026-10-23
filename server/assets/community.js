/* Whole-room text chat. Never render message HTML or accept client identity. */
(() => {
  const panel=document.getElementById('communitypanel');
  if(!panel) return;
  const launcher=document.getElementById('communitylauncher'), close=document.getElementById('communityclose');
  const log=document.getElementById('communitymessages'), status=document.getElementById('communitystatus');
  const input=document.getElementById('communityinput'), send=document.getElementById('communitysend');
  const badge=document.getElementById('communitybadge'), online=document.getElementById('communityonline');
  const key='netlab-community:'+location.pathname;
  const token=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
  let client=localStorage.getItem(key+':client');
  if(!/^[a-f0-9]{32}$/.test(client||'')) {client=token();localStorage.setItem(key+':client',client);}
  let opened=false, closing=false, animation=null, sequence=0, returnScroll=null;
  const panelScroll=window.netlabPanelScroll||(window.netlabPanelScroll={value:null});
  let sending=false, initialized=false, latest=0, unread=0, connected=false;
  let room={me:'',messages:[],online:[]};
  function layout() {
    panel.hidden=!opened&&!closing;panel.inert=!opened;
    panel.setAttribute('aria-hidden',String(!opened));
    launcher.setAttribute('aria-expanded',String(opened));
    launcher.setAttribute('aria-label',opened?'접속자 채팅 닫기':'접속자 채팅 열기');
    document.body.classList.toggle('community-open',opened||closing);
  }
  function toggle(value) {
    if(value===opened) return;
    const serial=++sequence;
    const current=panel.hidden?null:{opacity:getComputedStyle(panel).opacity,transform:getComputedStyle(panel).transform};
    animation?.cancel();animation=null;
    if(value&&innerWidth<1400&&returnScroll===null) {
      returnScroll=panelScroll.value??scrollY;panelScroll.value=returnScroll;
    }
    opened=value;closing=!value;layout();
    if(opened) {
      window.dispatchEvent(new CustomEvent('netlab:panel-open',{detail:'community'}));
      unread=0;badge.hidden=true;
      if(innerWidth<1400) panel.scrollIntoView({block:'start',behavior:'instant'});
      input.focus({preventScroll:true});log.scrollTop=log.scrollHeight;
    } else launcher.focus({preventScroll:true});
    function finish() {
      if(serial!==sequence) return;
      animation?.cancel();animation=null;closing=false;layout();
      if(!opened) {
        const peerOpen=document.getElementById('tutorlauncher')?.getAttribute('aria-expanded')==='true';
        if(!peerOpen&&returnScroll!==null){scrollTo({top:returnScroll,behavior:'instant'});panelScroll.value=null;}
        returnScroll=null;
        window.dispatchEvent(new CustomEvent('netlab:panel-settled',{detail:'community'}));
      }
    }
    if(matchMedia('(prefers-reduced-motion:reduce)').matches||!panel.animate){finish();return;}
    const collapsed={opacity:0,transform:'translateY(14px) scale(.98)'},expanded={opacity:1,transform:'none'};
    animation=panel.animate([current||collapsed,opened?expanded:collapsed],{duration:opened?240:160,easing:'cubic-bezier(.16,1,.3,1)',fill:'both'});
    animation.onfinish=finish;
  }
  launcher.onclick=()=>toggle(!opened);close.onclick=()=>toggle(false);
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();toggle(false);}});
  window.addEventListener('netlab:panel-open',event=>{if(event.detail!=='community'&&opened)toggle(false);});
  window.addEventListener('netlab:panel-settled',event=>{
    if(event.detail!=='community'&&opened&&innerWidth<1400)panel.scrollIntoView({block:'start',behavior:'instant'});
  });
  layout();
  function render(data) {
    online.textContent='접속 '+data.online.length+'명 · '+data.online.join(', ');
    const newest=data.messages.at(-1)?.id||0;
    if(newest<latest) return; // An earlier poll must not undo a newer send result.
    const follow=log.scrollHeight-log.scrollTop-log.clientHeight<70;
    const newMessages=data.messages.filter(message=>message.id>latest);
    if(initialized&&!opened) unread+=newMessages.filter(message=>message.author!==data.me).length;
    latest=newest;initialized=true;
    badge.hidden=unread===0;badge.textContent=unread>99?'99+':String(unread);
    const retained=new Set(data.messages.map(message=>String(message.id)));
    for(const child of Array.from(log.children)) if(!retained.has(child.dataset.id))child.remove();
    for(const message of data.messages) {
      if(Array.from(log.children).some(child=>child.dataset.id===String(message.id)))continue;
      const item=document.createElement('article');item.className='community-message';item.dataset.id=String(message.id);
      const heading=document.createElement('header'),author=document.createElement('strong'),time=document.createElement('time');
      author.textContent=message.author+(message.author===data.me?' · 나':'');
      const date=new Date(message.created*1000);time.dateTime=date.toISOString();time.textContent=date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});time.title=date.toLocaleString();
      const text=document.createElement('p');text.textContent=message.text;
      heading.append(author,time);item.append(heading,text);log.append(item);
    }
    if(!data.messages.length) {status.textContent='첫 메시지를 남겨 보세요.';} else status.textContent='';
    if(follow||sending)log.scrollTop=log.scrollHeight;
  }
  const transport=window.NetlabCommunityTransport({ticketUrl:ctlBase()+'community',client,
    onState(state,message){connected=state==='connected';send.disabled=sending||!connected;
      status.textContent=connected?'':message||'채팅 연결 중…';
      if(!connected)online.textContent='접속자 현황 다시 연결 중…';},
    onEvent(event){
      if(event.type==='snapshot'){
        if((event.messages.at(-1)?.id||0)<latest){latest=0;initialized=false;log.replaceChildren();}
        room={me:event.me,messages:event.messages,online:event.online};render(room);
      }else if(event.type==='presence'){
        room.online=event.online;online.textContent='접속 '+event.online.length+'명 · '+event.online.join(', ');
      }else if(event.type==='message'&&event.message){
        if(!room.messages.some(message=>message.id===event.message.id))room.messages.push(event.message);
        room.messages.sort((a,b)=>a.id-b.id);room.messages=room.messages.slice(-100);render(room);
      }else if(event.type==='error')status.textContent=event.message;
    }
  });
  document.getElementById('communityform').addEventListener('submit',async event=>{
    event.preventDefault();const text=input.value.trim();if(!text||sending)return;
    sending=true;send.disabled=true;
    let pending;
    try{pending=JSON.parse(sessionStorage.getItem(key+':pending')||'null');}catch{}
    if(!pending||pending.text!==text)pending={text,request:token()};
    sessionStorage.setItem(key+':pending',JSON.stringify(pending));
    try {await transport.send(pending);if(input.value.trim()===pending.text)input.value='';sessionStorage.removeItem(key+':pending');}
    catch(error){status.textContent=error.message;}
    finally{sending=false;send.disabled=!connected;}
  });
  input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();document.getElementById('communityform').requestSubmit();}});
  window.addEventListener('pagehide',()=>transport.close(),{once:true});
  window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
})();
