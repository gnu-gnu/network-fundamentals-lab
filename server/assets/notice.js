/* Latest-only notice. Render text only; the server owns identity and read receipts. */
(() => {
  const panel=document.getElementById('noticepanel');
  if(!panel) return;
  const launcher=document.getElementById('noticelauncher'), close=document.getElementById('noticeclose');
  const badge=document.getElementById('noticebadge'), title=document.getElementById('noticetitle');
  const meta=document.getElementById('noticemeta'), body=document.getElementById('noticebody');
  const state=document.getElementById('noticestate');
  // Share the community chat browser token so a guest keeps one name across both features.
  const key='netlab-community:'+location.pathname+':client';
  const token=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
  let client=null;
  try{client=localStorage.getItem(key);}catch(e){}
  if(!/^[a-f0-9]{32}$/.test(client||'')){client=token();try{localStorage.setItem(key,client);}catch(e){}}
  let current={notice:null,read:false,read_at:null}, opened=false, error='';
  const stamp=t=>{const d=new Date(t*1000),p=x=>String(x).padStart(2,'0');return (d.getMonth()+1)+'/'+d.getDate()+' '+p(d.getHours())+':'+p(d.getMinutes());};
  async function api(payload) {
    const response=await fetch(ctlBase()+'notice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,client})});
    const data=await response.json();
    if(!response.ok||!data.ok) throw Error(data.message||'공지를 불러올 수 없습니다.');
    return data;
  }
  function render() {
    const n=current.notice, unread=Boolean(n)&&!current.read;
    badge.hidden=!unread;
    launcher.classList.toggle('unread',unread);
    launcher.setAttribute('aria-expanded',String(opened));
    launcher.setAttribute('aria-label',(opened?'공지 닫기':'공지 열기')+(unread?' (읽지 않은 공지 있음)':''));
    title.textContent=n?n.title:'등록된 공지가 없습니다.';
    meta.textContent=n?stamp(n.created)+' 게시':'';
    body.textContent=n?n.body:'';
    state.className='notice-state'+(n?(current.read?' read':' unread'):'');
    state.textContent=error||(n?(current.read?'읽음 · '+stamp(current.read_at):'안 읽음'):'');
  }
  async function refresh() {
    try{current=await api({action:'get'});error='';}
    catch(e){error=e.message;}
    render();
  }
  async function markRead() {
    if(!current.notice||current.read) return;
    try{current=await api({action:'read',id:current.notice.id});error='';}
    catch(e){error=e.message;await refresh();return;}
    render();
  }
  function toggle(value) {
    if(value===opened) return;
    opened=value;panel.hidden=!opened;render();
    if(opened){
      if(!matchMedia('(prefers-reduced-motion:reduce)').matches&&panel.animate)
        panel.animate([{opacity:0,transform:'translateY(-6px)'},{opacity:1,transform:'none'}],{duration:180,easing:'cubic-bezier(.16,1,.3,1)'});
      close.focus({preventScroll:true});
      refresh().then(markRead);
    } else launcher.focus({preventScroll:true});
  }
  launcher.onclick=()=>toggle(!opened);
  close.onclick=()=>toggle(false);
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();toggle(false);}});
  document.addEventListener('click',event=>{
    if(opened&&!panel.contains(event.target)&&!launcher.contains(event.target)) toggle(false);
  });
  render();refresh();
  setInterval(()=>{if(!document.hidden&&!opened)refresh();},60000);
})();
