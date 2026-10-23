// Real ttyd HTML/xterm renderer + mocked socket: no real shell or VM is started.
// node browser_terminal_themes.mjs /tmp/netlab-ttyd-client.html [test-panel URL]
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const html=await readFile(process.argv[2],{encoding:'base64'});
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let count=0,documents=0,realSockets=0;const pending=new Map(),errors=[];
ws.addEventListener('message',async event=>{
  const data=JSON.parse(event.data);
  if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.text);
  if(data.method==='Network.webSocketCreated')realSockets++;
  if(data.method==='Fetch.requestPaused'){
    const {requestId,request,resourceType}=data.params;
    if(resourceType==='Document')documents++;
    const token=new URL(request.url).pathname.endsWith('/token');
    await cmd('Fetch.fulfillRequest',{requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:token?'application/json':'text/html; charset=utf-8'}],body:token?Buffer.from('{"token":"fixture"}').toString('base64'):html});
  }
  if(pending.has(data.id)){pending.get(data.id)(data);pending.delete(data.id);}
});
async function cmd(method,params={}){const id=++count;const result=await new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,method,params}));});assert(!result.error,JSON.stringify(result));return result.result;}
async function ev(expression){const result=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!result.exceptionDetails,JSON.stringify(result));return result.result.value;}
async function wait(expression){for(let i=0;i<150;i++){if(await ev(expression))return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('Timeout: '+expression);}
await cmd('Runtime.enable');await cmd('Page.enable');await cmd('Network.enable');
await cmd('Fetch.enable',{patterns:[{urlPattern:'*/term/*',requestStage:'Request'}]});
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`
if(window===top){
 const original=window.fetch;
 window.fetch=(input,...rest)=>String(input).endsWith('status')?Promise.resolve(new Response(JSON.stringify({state:'running',instance:'theme-fixture'}),{headers:{'Content-Type':'application/json'}})):original(input,...rest);
} else if(location.pathname.includes('/term/')) {
 window.netlabTestSockets=0;
 window.WebSocket=class extends EventTarget {
  static OPEN=1;static CONNECTING=0;static CLOSED=3;
  constructor(){super();window.netlabTestSockets++;this.readyState=0;setTimeout(()=>{this.readyState=1;this.dispatchEvent(new Event('open'));},30);}
  send(value){if(this.sent)return;this.sent=true;setTimeout(()=>{
   for(const text of ['2'+JSON.stringify({theme:{background:'#0d1621'},fontSize:13,disableLeaveAlert:true,rendererType:'canvas'}),'0lab@theme-preview $ terminal session preserved\\r\\n']){
    this.dispatchEvent(new MessageEvent('message',{data:new TextEncoder().encode(text).buffer}));
   }
  },20);}
  close(){this.readyState=3;}
 };
}
`});
await cmd('Page.navigate',{url:process.argv[3]||'http://192.168.219.178:18088/u/guest/ctl/'});
await wait('document.getElementById("termframe")?.contentWindow.term?.options && typeof netlabTerminal!=="undefined"');
await wait('document.getElementById("termframe").contentWindow.term.buffer.active.getLine(0).translateToString().includes("session preserved")');
await ev('window.originalThemeTerminal=document.getElementById("termframe").contentWindow.term;window.originalThemeFrame=document.getElementById("termframe").contentWindow;window.originalThemeSource=document.getElementById("termframe").src');
const firstDocuments=documents;
for(const [theme,background] of [['current','#0d1621'],['solarized-dark','#002b36'],['solarized-light','#fdf6e3'],['ubuntu','#300a24']]){
  await ev('document.getElementById("terminaltheme").value='+JSON.stringify(theme)+';document.getElementById("terminaltheme").dispatchEvent(new Event("change"))');
  assert.equal(await ev('document.getElementById("termframe").contentWindow.term.options.theme.background'),background);
  assert.equal(await ev('JSON.parse(new URL(document.getElementById("termfull").href).searchParams.get("theme")).background'),background);
  assert(await ev('window.originalThemeTerminal===document.getElementById("termframe").contentWindow.term && window.originalThemeFrame===document.getElementById("termframe").contentWindow && window.originalThemeSource===document.getElementById("termframe").src'));
  assert.equal(await ev('document.getElementById("termframe").contentWindow.netlabTestSockets'),1);
  assert.equal(documents,firstDocuments);
}
await ev('document.getElementById("themetoggle").click()');
assert.equal(await ev('document.getElementById("termframe").contentWindow.term.options.theme.background'),'#300a24');
await ev('document.getElementById("termframe").contentWindow.term.options.theme={background:"#123456"}');
await wait('document.getElementById("termframe").contentWindow.term.options.theme.background==="#300a24"');
for(const width of [390,768,1366,1920]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  assert(await ev('(()=>{const r=document.getElementById("terminaltheme").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&document.documentElement.scrollWidth<=innerWidth})()'));
}
await ev('window.terminalThemeReloadMarker=true');await cmd('Page.reload');
await wait('!window.terminalThemeReloadMarker && document.getElementById("termframe")?.contentWindow.term?.options?.theme?.background==="#300a24"');
assert.equal(await ev('document.getElementById("terminaltheme").value'),'ubuntu');
assert.equal(realSockets,0,'A real shell WebSocket must never be opened by this fixture');
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,themes:4,realTtydRenderer:true,mockedSocketOnly:true,noIframeReloadOnSelection:true,terminalIdentityPreserved:true,fullscreenTheme:true,persistsAcrossReload:true,independentOfWebTheme:true,reappliesAfterServerPreferences:true,responsive:true,browserErrors:errors},null,2));
await cmd('Browser.close');ws.close();
