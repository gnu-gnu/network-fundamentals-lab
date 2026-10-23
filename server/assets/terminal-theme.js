/* ttyd exposes window.term; changing xterm options never reloads its WebSocket. */
(() => {
  const key='netlab-terminal-theme';
  const solarized={black:'#073642',red:'#dc322f',green:'#859900',yellow:'#b58900',blue:'#268bd2',magenta:'#d33682',cyan:'#2aa198',white:'#eee8d5',brightBlack:'#002b36',brightRed:'#cb4b16',brightGreen:'#586e75',brightYellow:'#657b83',brightBlue:'#839496',brightMagenta:'#6c71c4',brightCyan:'#93a1a1',brightWhite:'#fdf6e3'};
  const themes={
    current:{background:'#0d1621',foreground:'#dbe6f1',cursor:'#3fc4dd',cursorAccent:'#0d1621',selectionBackground:'#22465a',black:'#16222f',red:'#f87171',green:'#4ade80',yellow:'#fbbf24',blue:'#60a5fa',magenta:'#c084fc',cyan:'#3fc4dd',white:'#c8d6e5',brightBlack:'#4a5f74',brightRed:'#fca5a5',brightGreen:'#86efac',brightYellow:'#fcd34d',brightBlue:'#93c5fd',brightMagenta:'#d8b4fe',brightCyan:'#7ddef0',brightWhite:'#f1f6fb'},
    'solarized-dark':{...solarized,background:'#002b36',foreground:'#839496',cursor:'#93a1a1',cursorAccent:'#002b36',selectionBackground:'#586e7580'},
    'solarized-light':{...solarized,background:'#fdf6e3',foreground:'#657b83',cursor:'#586e75',cursorAccent:'#fdf6e3',selectionBackground:'#93a1a180'},
    ubuntu:{background:'#300a24',foreground:'#eeeeec',cursor:'#eeeeec',cursorAccent:'#300a24',selectionBackground:'#75507b',black:'#2e3436',red:'#cc0000',green:'#4e9a06',yellow:'#c4a000',blue:'#3465a4',magenta:'#75507b',cyan:'#06989a',white:'#d3d7cf',brightBlack:'#555753',brightRed:'#ef2929',brightGreen:'#8ae234',brightYellow:'#fce94f',brightBlue:'#729fcf',brightMagenta:'#ad7fa8',brightCyan:'#34e2e2',brightWhite:'#eeeeec'}
  };
  let selected='current';
  try {const saved=localStorage.getItem(key);if(Object.hasOwn(themes,saved))selected=saved;} catch {}
  function url(base) {
    const result=new URL(base,location.href);
    result.searchParams.set('theme',JSON.stringify(themes[selected]));
    return result.href;
  }
  window.netlabTerminal={url};
  document.addEventListener('DOMContentLoaded',()=>{
    const select=document.getElementById('terminaltheme'),frame=document.getElementById('termframe');
    if(!select||!frame)return;
    const notice=document.getElementById('terminalthemestatus'),full=document.getElementById('termfull');
    const notify=text=>{if(notice.textContent!==text)notice.textContent=text;};
    function apply() {
      select.value=selected;
      frame.style.backgroundColor=themes[selected].background;
      const source=frame.getAttribute('src');
      if(source&&full)full.href=url(source);
      if(!source){notify('');return;}
      try {
        const term=frame.contentWindow.term;
        if(!term?.options){notify('연결 후 적용');return;}
        const desired=themes[selected],current=term.options.theme||{};
        if(Object.keys(desired).some(name=>current[name]!==desired[name]))term.options.theme={...desired};
        notify('');
      } catch {notify('연결 후 적용');}
    }
    // Keep preferences after ttyd's initial preferences/reconnect frame arrives.
    // This is only a local property comparison; it sends no network requests.
    let interval=setInterval(apply,1000);
    window.addEventListener('pagehide',()=>clearInterval(interval));
    window.addEventListener('pageshow',event=>{if(event.persisted){apply();interval=setInterval(apply,1000);}});
    frame.addEventListener('load',apply);
    new MutationObserver(apply).observe(frame,{attributes:true,attributeFilter:['src']});
    select.addEventListener('change',()=>{
      if(!Object.hasOwn(themes,select.value))return;
      selected=select.value;
      try{localStorage.setItem(key,selected);}catch{}
      apply();
    });
    window.addEventListener('storage',event=>{
      if(event.key!==key&&event.key!==null)return;
      selected=Object.hasOwn(themes,event.newValue)?event.newValue:'current';apply();
    });
    apply();
  });
})();
