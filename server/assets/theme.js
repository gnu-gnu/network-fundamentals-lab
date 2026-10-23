/* Applied before CSS/paint; only the explicit preference is persisted. */
(() => {
  const key='netlab-theme', root=document.documentElement;
  const system=matchMedia('(prefers-color-scheme: dark)');
  let preference;
  try {preference=localStorage.getItem(key);} catch {}
  if(!['light','dark'].includes(preference))preference=null;
  function apply() {
    const dark=preference?preference==='dark':system.matches;
    root.dataset.theme=dark?'dark':'light';
    const button=document.getElementById('themetoggle');
    if(!button)return;
    button.setAttribute('aria-pressed',String(dark));
    button.title=dark?'라이트 테마로 전환':'다크 테마로 전환';
    document.getElementById('themeicon').textContent=dark?'☾':'☀';
    document.getElementById('themelabel').textContent=dark?'다크':'라이트';
  }
  apply();
  system.addEventListener('change',()=>{if(!preference)apply();});
  window.addEventListener('storage',event=>{
    if(event.key!==key&&event.key!==null)return;
    preference=['light','dark'].includes(event.newValue)?event.newValue:null;apply();
  });
  document.addEventListener('DOMContentLoaded',()=>{
    apply();
    document.getElementById('themetoggle')?.addEventListener('click',()=>{
      preference=root.dataset.theme==='dark'?'light':'dark';
      try {localStorage.setItem(key,preference);} catch {}
      apply();
    });
  });
})();
