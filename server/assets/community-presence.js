(() => {
  const view=document.getElementById('adminpresence');if(!view)return;
  let client=localStorage.getItem('netlab-community-admin-client');
  if(!/^[a-f0-9]{32}$/.test(client||'')){client=Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');localStorage.setItem('netlab-community-admin-client',client);}
  window.NetlabCommunityTransport({ticketUrl:'community-ticket',client,
    onState(state){if(state!=='connected')view.textContent='접속자 현황 연결 중…';},
    onEvent(event){if(event.type==='snapshot'||event.type==='presence')view.textContent='페이지 접속 '+event.online.length+'명 · '+(event.online.join(', ')||'없음');}
  });
})();
