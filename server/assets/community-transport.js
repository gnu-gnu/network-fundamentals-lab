/* Shared, authenticated WebSocket transport for learner chat/admin presence. */
(() => {
  window.NetlabCommunityTransport=({ticketUrl,client,onEvent,onState})=>{
    let socket=null,ready=false,stopped=false,connecting=false,retry=null,renew=null,attempt=0;
    const pending=new Map();
    async function grant(){
      const response=await fetch(ticketUrl,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'ticket',client}),signal:AbortSignal.timeout(10000)});
      const data=await response.json();
      if(!response.ok||!data.ok)throw Error(data.message||'채팅 로그인 확인에 실패했습니다.');
      return data;
    }
    function scheduleRenew(expires){
      clearTimeout(renew);
      renew=setTimeout(async()=>{
        const current=socket;
        try {const data=await grant();if(current===socket&&current.readyState===WebSocket.OPEN)current.send(JSON.stringify({type:'authenticate',ticket:data.ticket}));}
        catch {current?.close(1000,'로그인 재확인');}
      },Math.max(1000,(expires*1000-Date.now())-60000));
    }
    function reconnect(){
      if(stopped)return;
      clearTimeout(retry);
      retry=setTimeout(connect,Math.min(30000,1000*2**Math.min(attempt++,5))*(.8+Math.random()*.4));
    }
    async function connect(){
      if(stopped||connecting)return;
      connecting=true;
      onState('connecting');
      try {
        const data=await grant();if(stopped)return;
        const url=new URL('/community/ws',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
        const current=new WebSocket(url);socket=current;
        const deadline=setTimeout(()=>{if(!ready)current.close();},10000);
        current.onopen=()=>current.send(JSON.stringify({type:'authenticate',ticket:data.ticket}));
        current.onmessage=event=>{
          if(current!==socket)return;
          let message;try{message=JSON.parse(event.data);}catch{return;}
          if(message.type==='snapshot'){
            clearTimeout(deadline);ready=true;attempt=0;scheduleRenew(message.expires);onState('connected');
          }
          if(message.type==='authenticated')scheduleRenew(message.expires);
          if(message.type==='ack'||message.type==='error'){
            const entry=pending.get(message.request);
            if(entry){clearTimeout(entry.timer);pending.delete(message.request);message.type==='ack'?entry.resolve():entry.reject(Error(message.message));}
          }
          onEvent(message);
        };
        current.onclose=()=>{
          clearTimeout(deadline);if(current!==socket)return;
          ready=false;clearTimeout(renew);onState('disconnected');
          for(const entry of pending.values()){clearTimeout(entry.timer);entry.reject(Error('연결이 끊겼습니다. 다시 보내면 중복 없이 재시도합니다.'));}pending.clear();
          reconnect();
        };
        current.onerror=()=>{}; // onclose owns retry/backoff.
      }catch(error){onState('disconnected',error.message);reconnect();}
      finally{connecting=false;}
    }
    connect();
    const resume=()=>{if(!ready&&!stopped){clearTimeout(retry);if(!socket||socket.readyState===WebSocket.CLOSED)connect();}};
    window.addEventListener('online',resume);
    // Keep sockets alive when the chat panel or browser tab is hidden.
    return {
      send(message){
        if(!ready||socket?.readyState!==WebSocket.OPEN)return Promise.reject(Error('채팅 재연결 후 다시 보내세요.'));
        return new Promise((resolve,reject)=>{
          const timer=setTimeout(()=>{pending.delete(message.request);reject(Error('전송 확인이 늦습니다. 다시 보내면 중복 없이 재시도합니다.'));},15000);
          pending.set(message.request,{resolve,reject,timer});socket.send(JSON.stringify({type:'send',...message}));
        });
      },
      close(){stopped=true;clearTimeout(retry);clearTimeout(renew);socket?.close();window.removeEventListener('online',resume);}
    };
  };
})();
