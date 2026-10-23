// Requires a disposable Chromium CDP page on :9228 and a test panel URL.
// No model requests or lab mutations. Usage: node browser_tutor_motion.mjs [test URL]
import assert from 'node:assert/strict';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.addEventListener('open',r,{once:true}));
let id=0;const calls=new Map(),errors=[];
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);if(calls.has(m.id)){calls.get(m.id)(m);calls.delete(m.id);}});
function cmd(method,params={}){return new Promise(r=>{const n=++id;calls.set(n,r);ws.send(JSON.stringify({id:n,method,params}));});}
async function ev(expression){const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!r.result.exceptionDetails,JSON.stringify(r));return r.result.result.value;}
async function wait(expression){for(let n=0;n<100;n++){if(await ev(expression))return;await new Promise(r=>setTimeout(r,30));}throw Error('Timed out: '+expression);}
await cmd('Runtime.enable');await cmd('Page.enable');
await cmd('Page.navigate',{url:process.argv[2]||'http://192.168.219.178:18088/u/guest/ctl/'});
await wait('typeof tutorSend==="function"');
// Exercise the UI even when the test VM is idle/stopped; never start a VM.
await ev('refresh().then(()=>document.getElementById("running").classList.remove("hide"))');
const results=[];
for(const width of [2048,1600,1400,1366,768,390]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  await ev('if(document.getElementById("tutorlauncher").getAttribute("aria-expanded")==="true") document.getElementById("tutorclose").click()');
  await wait('document.getElementById("tutorpanel").hidden');
  await ev('scrollTo(0,0)');
  const before=await ev('document.querySelector(".termbox").getBoundingClientRect().width');
  const entering=await ev('document.getElementById("tutorlauncher").click();document.getElementById("tutorpanel").getAnimations().length');
  assert(entering>0,'Missing entrance animation');
  await wait('document.getElementById("tutorpanel").getAnimations().length===0');
  assert(await ev('!document.getElementById("tutorpanel").hidden && !document.getElementById("tutorpanel").inert && document.activeElement.id==="tutorinput"'));
  assert(Math.abs(before-await ev('document.querySelector(".termbox").getBoundingClientRect().width'))<1);
  assert(await ev('document.documentElement.scrollWidth<=innerWidth'));
  const leaving=await ev('document.getElementById("tutorclose").click();(()=>{const p=document.getElementById("tutorpanel");return !p.hidden&&p.inert&&p.getAnimations().length>0})()');
  assert(leaving,'Closing must animate before hiding and block input');
  await wait('document.getElementById("tutorpanel").hidden');
  assert(await ev('Math.abs(scrollY)<1 && document.activeElement.id==="tutorlauncher"'));
  await ev('document.getElementById("tutorlauncher").click();document.getElementById("tutorclose").click();document.getElementById("tutorlauncher").click()');
  await wait('document.getElementById("tutorpanel").getAnimations().length===0');
  assert(await ev('!document.getElementById("tutorpanel").hidden && !document.getElementById("tutorpanel").inert'));
  await ev('document.getElementById("tutorclose").click()');
  await wait('document.getElementById("tutorpanel").hidden');
  assert(await ev('Math.abs(scrollY)<1'),'Interrupted closing lost original scroll position');
  results.push({width,animation:true,rapidToggle:true,terminalWidthPreserved:true});
}
await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
assert(await ev('document.getElementById("tutorlauncher").click();!document.getElementById("tutorpanel").hidden && document.getElementById("tutorpanel").getAnimations().length===0'));
assert(await ev('document.getElementById("tutorclose").click();document.getElementById("tutorpanel").hidden'));
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,results,reducedMotion:true,browserErrors:errors},null,2));
await cmd('Browser.close');ws.close();
