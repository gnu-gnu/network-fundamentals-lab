// Isolated CDP browser on :9228; mocked API, no model calls or lab mutations.
import assert from 'node:assert/strict';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.addEventListener('open',r,{once:true}));
let id=0;const pending=new Map(),errors=[];
ws.addEventListener('message',e=>{const d=JSON.parse(e.data);if(d.method==='Runtime.exceptionThrown')errors.push(d.params.exceptionDetails.text);if(pending.has(d.id)){pending.get(d.id)(d);pending.delete(d.id);}});
async function cmd(method,params={}){const n=++id;const d=await new Promise(r=>{pending.set(n,r);ws.send(JSON.stringify({id:n,method,params}));});assert(!d.error,JSON.stringify(d));return d.result;}
async function ev(expression){const d=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!d.exceptionDetails,JSON.stringify(d));return d.result.value;}
async function wait(expression){for(let i=0;i<100;i++){if(await ev(expression))return;await new Promise(r=>setTimeout(r,40));}throw Error(expression);}
await cmd('Runtime.enable');await cmd('Page.enable');
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`
if(!sessionStorage.modalTestInitialized){localStorage.clear();sessionStorage.modalTestInitialized='1';}const original=fetch;window.sent=[];
const models=[{id:'qwen',label:'Qwen 3.8 · 27B'},{id:'muse',label:'Muse Glimmer · 30B'},{id:'gemma',label:'Gemma 4 · 26B A4B'}];const messages=[];
window.fetch=(url,options)=>{
 if(String(url).endsWith('tutor')){const body=JSON.parse(options.body);
  if(body.action==='models')return Promise.resolve(new Response(JSON.stringify({ok:true,models,default_model:'qwen'})));
  if(body.action==='history'&&!messages.length)return Promise.resolve(new Response(JSON.stringify({ok:false,message:'실습 환경을 먼저 시작하세요.'}),{status:400}));
  if(body.action==='send'){sent.push(body);messages.push({id:String(sent.length),question:body.message,answer:'응답',status:'done',evidence:[],model_id:body.model});}
  return Promise.resolve(new Response(JSON.stringify({ok:true,session:'a'.repeat(64),models,default_model:'qwen',messages,terminal_sharing_allowed:false})));}
 return original(url,options);
};`});
await cmd('Page.navigate',{url:process.argv[2]||'http://127.0.0.1:18899/u/guest/ctl/'});
await wait('document.getElementById("tutormodel")?.textContent.includes("Qwen")');
await ev('document.getElementById("running").classList.remove("hide")');
await ev('document.getElementById("tutorlauncher").click()');
await new Promise(r=>setTimeout(r,350));
for(const width of [390,768,1366,1920]){
 await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
 await ev('document.getElementById("tutormodel").click()');
 await wait('document.getElementById("tutormodeldialog").open');
 assert(await ev('(()=>{const d=document.getElementById("tutormodeldialog"),r=d.getBoundingClientRect();return d.open&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&document.activeElement.closest("dialog")===d})()'));
 await ev('document.getElementById("tutormodeldialog").close()');
}
await ev('document.getElementById("tutormodel").click()');
await wait('document.getElementById("tutormodeldialog").open');
await ev('document.querySelectorAll(".tutor-model-option")[1].click();tutorSend("ARP 설명")');
await wait('sent.length===1 && !document.getElementById("tutormodel").disabled');
assert.equal(await ev('sent[0].model'),'muse');
assert(await ev('!document.getElementById("tutormodeldialog").open && document.getElementById("tutorlog").textContent.includes("Muse Glimmer")'));
await ev('document.getElementById("tutormodel").click()');
await wait('document.getElementById("tutormodeldialog").open');
await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
await wait('!document.getElementById("tutormodeldialog").open');
assert(await ev('!document.getElementById("tutorpanel").hidden'));
await cmd('Page.reload');
await wait('document.getElementById("tutormodel")?.textContent.includes("Muse")');
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,models:3,responsive:true,escapeKeepsTutorOpen:true,modelSent:true,answerLabel:true,persists:true,errors}));
await cmd('Browser.close');ws.close();
