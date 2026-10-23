// Real two-browser-context chat check. Test environment only: sends test messages.
import assert from 'node:assert/strict';
const version=await(await fetch('http://127.0.0.1:9228/json/version')).json();
const ws=new WebSocket(version.webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let counter=0;const pending=new Map(),errors=[];
ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.text);if(data.method==='Network.requestWillBeSent'&&data.params.request.postData){try{if(JSON.parse(data.params.request.postData).action==='poll')errors.push('Legacy chat polling is still active');}catch{}}if(pending.has(data.id)){pending.get(data.id)(data);pending.delete(data.id);}});
async function cmd(method,params={},sessionId){const id=++counter;const result=await new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});assert(!result.error,JSON.stringify(result));return result.result;}
async function ev(session,expression){const data=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);assert(!data.exceptionDetails,JSON.stringify(data));return data.result.value;}
async function wait(session,expression){for(let i=0;i<150;i++){if(await ev(session,expression))return;await new Promise(resolve=>setTimeout(resolve,100));}throw Error('Timed out: '+expression);}
const contexts=[];
async function page(){const {browserContextId}=await cmd('Target.createBrowserContext');contexts.push(browserContextId);const {targetId}=await cmd('Target.createTarget',{url:'about:blank',browserContextId});const {sessionId}=await cmd('Target.attachToTarget',{targetId,flatten:true});await cmd('Runtime.enable',{},sessionId);await cmd('Page.enable',{},sessionId);await cmd('Network.enable',{},sessionId);await cmd('Network.setBlockedURLs',{urls:['*/term/','*/term/*']},sessionId);
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`const NativeSocket=window.WebSocket;window.chatTestSockets=[];window.WebSocket=class extends NativeSocket{constructor(url,...args){const target=${JSON.stringify(process.argv[3]||'')};super(target&&String(url).endsWith('/community/ws')?target:url,...args);window.chatTestSockets.push(this);}};`},sessionId);
await cmd('Page.navigate',{url:(process.argv[2]||process.env.NETLAB_URL||(()=>{console.error('usage: node browser_community.mjs <panel url>  (or NETLAB_URL)');process.exit(2)})())},sessionId);await wait(sessionId,'!!document.getElementById("communitylauncher") && typeof tutorSend==="function"');await cmd('Page.bringToFront',{},sessionId);await wait(sessionId,'document.getElementById("communityonline").textContent.includes("guest-")');return sessionId;}
const a=await page();const b=await page();
await cmd('Page.bringToFront',{},a);
await ev(a,'document.getElementById("communitylauncher").click()');
const text='[채팅 검증 '+Date.now()+'] <img src=x onerror=alert(1)> 안녕하세요';
await ev(a,'document.getElementById("communityinput").value='+JSON.stringify(text)+';document.getElementById("communityform").requestSubmit()');
await wait(a,'document.getElementById("communitymessages").textContent.includes('+JSON.stringify(text)+')');
assert(await ev(a,'document.querySelectorAll("#communitymessages img").length===0'));
await ev(a,'document.getElementById("communityclose").click()');
await cmd('Page.bringToFront',{},b);
await ev(b,'document.getElementById("communitylauncher").click()');
await wait(b,'document.getElementById("communitymessages").textContent.includes('+JSON.stringify(text)+')');
await ev(b,'document.getElementById("communityinput").value="[채팅 검증] 잘 보입니다";document.getElementById("communityform").requestSubmit()');
await wait(b,'document.getElementById("communitymessages").textContent.includes("잘 보입니다")');
await cmd('Page.bringToFront',{},a);
await wait(a,'!document.getElementById("communitybadge").hidden');
await ev(a,'document.getElementById("communitylauncher").click()');
assert(await ev(a,'document.getElementById("communitybadge").hidden'));
await wait(a,'Number(document.getElementById("communityonline").textContent.match(/접속 ([0-9]+)명/)?.[1])>=2');
await ev(a,'chatTestSockets.at(-1).close()');
await wait(a,'chatTestSockets.length>=2 && !document.getElementById("communitysend").disabled');
assert(await ev(a,'document.getElementById("communitymessages").textContent.includes('+JSON.stringify(text)+')'));
await ev(a,'refresh().then(()=>document.getElementById("running").classList.remove("hide"))');
await ev(a,'document.getElementById("tutorlauncher").click()');
assert(await ev(a,'document.getElementById("communitylauncher").getAttribute("aria-expanded")==="false"'));
await wait(a,'document.getElementById("communitypanel").hidden');
await ev(a,'document.getElementById("communitylauncher").click()');
assert(await ev(a,'document.getElementById("tutorlauncher").getAttribute("aria-expanded")==="false"'));
await wait(a,'document.getElementById("tutorpanel").hidden');
await wait(a,'document.getElementById("communitypanel").getAnimations().length===0');
assert(await ev(a,'document.getElementById("communitypanel").getBoundingClientRect().top>=0 && document.getElementById("communitypanel").getBoundingClientRect().top<innerHeight'),'Panel switch scrolled the new panel out of view');
const sizes=[];
for(const width of [2048,1600,1400,1366,768,390]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false},a);
  await ev(a,'if(document.getElementById("communitylauncher").getAttribute("aria-expanded")==="true")document.getElementById("communityclose").click()');
  await wait(a,'document.getElementById("communitypanel").hidden');
  await ev(a,'scrollTo(0,0)');
  const before=await ev(a,'document.querySelector(".termbox").getBoundingClientRect().width');
  await ev(a,'document.getElementById("communitylauncher").click()');
  await wait(a,'document.getElementById("communitypanel").getAnimations().length===0');
  const metrics=await ev(a,'(()=>{const p=document.getElementById("communitypanel").getBoundingClientRect(),t=document.querySelector(".termbox").getBoundingClientRect();return {left:p.left,right:p.right,top:p.top,terminalRight:t.right,terminalBottom:t.bottom,terminalWidth:t.width,overflow:document.documentElement.scrollWidth>innerWidth}})()');
  assert(!metrics.overflow&&metrics.left>=0&&metrics.right<=width,JSON.stringify(metrics));
  assert(Math.abs(metrics.terminalWidth-before)<1,JSON.stringify(metrics));
  if(width>=1400)assert(metrics.left>=metrics.terminalRight+8,JSON.stringify(metrics));
  else assert(metrics.top>=metrics.terminalBottom,JSON.stringify(metrics));
  sizes.push(width);
}
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,twoContexts:true,messagesDelivered:true,unreadBadge:true,plainTextXssSafe:true,mutuallyExclusivePanels:true,sizes,browserErrors:errors},null,2));
for(const browserContextId of contexts)await cmd('Target.disposeBrowserContext',{browserContextId});
await cmd('Browser.close');ws.close();
