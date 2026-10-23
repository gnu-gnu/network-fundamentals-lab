// Dedicated fixture with temporary config on :18899. Never run against production.
import assert from 'node:assert/strict';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.addEventListener('open',r,{once:true}));
let id=0;const pending=new Map(),errors=[];
ws.addEventListener('message',e=>{const d=JSON.parse(e.data);if(d.method==='Runtime.exceptionThrown')errors.push(d.params.exceptionDetails.text);if(pending.has(d.id)){pending.get(d.id)(d);pending.delete(d.id);}});
async function cmd(method,params={}){const n=++id;const d=await new Promise(r=>{pending.set(n,r);ws.send(JSON.stringify({id:n,method,params}));});assert(!d.error,JSON.stringify(d));return d.result;}
async function ev(expression){const d=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!d.exceptionDetails,JSON.stringify(d));return d.result.value;}
async function wait(expression){for(let i=0;i<150;i++){if(await ev(expression))return;await new Promise(r=>setTimeout(r,40));}throw Error(expression);}
await cmd('Runtime.enable');await cmd('Page.enable');
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:'window.confirm=()=>true;'});
await cmd('Page.navigate',{url:'http://127.0.0.1:18899/admin/'});
await wait('document.querySelectorAll("#modelrows tr").length===2');
const row=id=>`Array.from(document.querySelectorAll('#modelrows tr')).find(row=>row.children[1].textContent===${JSON.stringify(id)})`;
const click=(id,label)=>ev(`Array.from(${row(id)}.querySelectorAll('button')).find(b=>b.textContent===${JSON.stringify(label)}).click()`);
await click('qwen','편집');
assert(await ev('document.getElementById("modelkey").value==="" && document.getElementById("modelkey").placeholder.includes("설정됨")'));
await ev('document.getElementById("modellabel").value="Qwen <img src=x>";document.getElementById("modelform").requestSubmit()');
await wait('!document.getElementById("modeleditor").open');
assert(await ev('document.getElementById("modelrows").textContent.includes("<img src=x>") && !document.querySelector("#modelrows img")'));
await click('qwen','숨기기');
await wait(`${row('qwen')}.children[2].textContent==='숨김'`);
assert(await ev('fetch("/u/guest/ctl/tutor",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"models"})}).then(r=>r.json()).then(d=>!d.models.some(m=>m.id==="qwen")&&d.default_model==="muse"&&d.models.every(m=>Object.keys(m).sort().join(",")==="id,label"))'));
await ev('document.getElementById("modeladd").click();document.getElementById("modelid").value="browser-model";document.getElementById("modellabel").value="테스트 모델";document.getElementById("modelname").value="test-api-model";document.getElementById("modelendpoint").value="http://localhost:9009/v1/chat/completions";document.getElementById("modelkey").value="browser-write-only-secret";document.getElementById("modeldefault").checked=true;document.getElementById("modelparameters").value=JSON.stringify({max_tokens:8192,chat_template_kwargs:{reasoning_strength:"low"}});document.getElementById("modelform").requestSubmit()');
await wait('!document.getElementById("modeleditor").open');
assert(await ev(`${row('browser-model')}.children[3].textContent==='기본'`));
assert(await ev('fetch("models").then(r=>r.json()).then(d=>!JSON.stringify(d).includes("browser-write-only-secret")&&!JSON.stringify(d).includes("fixture-private-secret")&&d.models.find(m=>m.id==="browser-model").has_api_key)'));
await click('browser-model','편집');
for(const width of [390,768,1366]){
 await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
 assert(await ev('(()=>{const r=document.getElementById("modeleditor").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()'));
}
await ev('document.getElementById("modelcancel").click()');
await click('browser-model','삭제');
await wait(`${row('browser-model')}===undefined`);
assert(await ev('fetch("/u/guest/ctl/tutor",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"models"})}).then(r=>r.json()).then(d=>d.models.length===1&&d.default_model==="muse")'));
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,create:true,edit:true,delete:true,visibility:true,defaultModel:true,keysWriteOnly:true,plainTextSafe:true,responsive:true,errors}));
await cmd('Browser.close');ws.close();
