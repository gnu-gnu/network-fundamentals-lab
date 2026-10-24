// Read-only fixture browser check: lifecycle calls are mocked; no VM/model operations.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.addEventListener('open',r,{once:true}));
let id=0;const pending=new Map(),errors=[];
ws.addEventListener('message',e=>{const d=JSON.parse(e.data);if(d.method==='Runtime.exceptionThrown')errors.push(d.params.exceptionDetails.exception?.description||d.params.exceptionDetails.text);if(pending.has(d.id)){pending.get(d.id)(d);pending.delete(d.id);}});
async function cmd(method,params={}){const n=++id;const d=await new Promise(r=>{pending.set(n,r);ws.send(JSON.stringify({id:n,method,params}));});assert(!d.error,JSON.stringify(d));return d.result;}
async function ev(expression){const d=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!d.exceptionDetails,JSON.stringify(d));return d.result.value;}
async function wait(expression){for(let i=0;i<100;i++){if(await ev(expression))return;await new Promise(r=>setTimeout(r,40));}throw Error(expression);}
await cmd('Runtime.enable');await cmd('Page.enable');await cmd('Network.enable');
await cmd('Network.setBlockedURLs',{urls:['*/term/','*/term/*']});
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`
localStorage.setItem('netlab-theme','light');window.fixtureState='running';window.testLifecycle=[];window.confirm=()=>false;
const nativeFetch=fetch;window.fetch=(url,options={})=>{
 const path=String(url).split('?')[0];let body={};try{body=JSON.parse(options.body||'{}');}catch{}
 let data;
 if(path.endsWith('status'))data={state:fixtureState,instance:'netlab-preview',ip:'10.200.0.20',created:'2026-10-24'};
 else if(path.endsWith('sessions'))data={sessions:[]};
 else if(path.endsWith('notice'))data={ok:true,notice:null,read:false};
 else if(path.endsWith('community'))return Promise.resolve(new Response(JSON.stringify({ok:false,message:'미리보기'}),{status:503}));
 else if(path.endsWith('tutor'))data={ok:true,session:'a'.repeat(64),models:[{id:'default',label:'기본 모델'}],default_model:'default',messages:[],chapter:'01'};
 else if(options.method==='POST'&&/\\/(start|stop|restore|destroy)$/.test(path)){testLifecycle.push({path,body});data={ok:true,message:'모의 요청'};}
 else return nativeFetch(url,options);
 return Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
};`});
await cmd('Page.navigate',{url:process.argv[2]||'http://127.0.0.1:18899/u/guest/ctl/'});
await wait('!!document.getElementById("environmentmore") && document.getElementById("state").textContent.includes("실행 중")');
assert(await ev('Array.from(document.querySelectorAll("svg circle,svg rect")).every(shape=>shape.parentElement.tagName.toLowerCase()!=="circle"&&Array.from(shape.attributes).every(attr=>!attr.value.endsWith("/")))'),'SVG geometry must parse correctly');
const sizes=[];
for(const width of [390,768,1024,1366,1920]){
 await cmd('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
 assert(await ev('document.documentElement.scrollWidth<=innerWidth'),'Horizontal overflow at '+width);
 assert(await ev('(()=>{const a=document.getElementById("noticelauncher").getBoundingClientRect(),b=document.getElementById("themetoggle").getBoundingClientRect();return Math.abs(a.height-b.height)<1&&Math.abs(a.top-b.top)<1})()'));
 assert(await ev('(()=>{const dock=document.querySelector(".assist-dock").getBoundingClientRect(),a=document.getElementById("tutorlauncher").getBoundingClientRect(),b=document.getElementById("communitylauncher").getBoundingClientRect();return dock.left>=0&&dock.right<=innerWidth&&a.right+7<=b.left&&Math.abs(a.height-b.height)<1})()'));
 await ev('document.getElementById("environmentmore").click()');
 assert(await ev('(()=>{const m=document.getElementById("environmentmenu"),r=m.getBoundingClientRect();return !m.hidden&&r.left>=0&&r.right<=innerWidth&&document.activeElement.id==="brestore"})()'));
 await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 assert(await ev('document.getElementById("environmentmenu").hidden && document.activeElement.id==="environmentmore"'));
 sizes.push(width);
}
await ev('document.getElementById("environmentmore").click();document.getElementById("brestore").click()');
assert.equal(await ev('testLifecycle.length'),0,'Cancel must prevent destructive request');
assert(await ev('document.getElementById("environmentmenu").hidden'));
await ev('fixtureState="stopped";refresh()');
await wait('document.getElementById("runctrl").classList.contains("hide")');
assert(await ev('document.getElementById("tutorlauncher").classList.contains("hide")'));
await ev('document.getElementById("environmentmore").click()');
assert(await ev('document.getElementById("brestore").classList.contains("hide")'));
await ev('environmentMenu(false);fixtureState="none";refresh()');
await wait('document.getElementById("give").classList.contains("hide")');
await ev('fixtureState="running";refresh();document.getElementById("themetoggle").click()');
assert(await ev('document.documentElement.dataset.theme==="dark" && !!document.querySelector("#themeicon svg") && document.getElementById("themeicon").dataset.mode==="dark"'));
await ev('document.getElementById("themetoggle").click()');
assert(await ev('document.documentElement.dataset.theme==="light" && !!document.querySelector("#themeicon svg")'));
await cmd('Emulation.setDeviceMetricsOverride',{width:1920,height:1200,deviceScaleFactor:1,mobile:false});
await ev('scrollTo(0,0)');
await writeFile('/tmp/netlab-panel-chrome-desktop.png',Buffer.from((await cmd('Page.captureScreenshot',{format:'png'})).data,'base64'));
await cmd('Emulation.setDeviceMetricsOverride',{width:390,height:1000,deviceScaleFactor:1,mobile:false});
await writeFile('/tmp/netlab-panel-chrome-mobile.png',Buffer.from((await cmd('Page.captureScreenshot',{format:'png'})).data,'base64'));
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,sizes,alignedControls:true,consistentIcons:true,dockNoOverlap:true,keyboardMenu:true,dangerousActionCancelled:true,stateTransitions:true,themeSvgPreserved:true,errors}));
await cmd('Browser.close');ws.close();
