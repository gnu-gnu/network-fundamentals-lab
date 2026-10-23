// Real textarea keyboard events with mocked tutor API: no model requests.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const corpus=JSON.parse(await readFile(new URL('../knowledge/corpus.json',import.meta.url),'utf8'));
const refs=['04-l3-routing/README.md','server/knowledge/wiki/routing-and-return-path.md'].map((path,index)=>{
  const doc=corpus.documents.find(doc=>doc.path===path);
  return {kind:'reference',label:'[G'+(index+1)+'] '+path+' · 과거 저장 형식',source:doc.id,sha256:doc.sha256,line:1,output:'숨길 원문 발췌',time:Date.now()/1000};
});
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let count=0;const pending=new Map(),errors=[];
ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.text);if(pending.has(data.id)){pending.get(data.id)(data);pending.delete(data.id);}});
async function cmd(method,params={}){const id=++count;const result=await new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,method,params}));});assert(!result.error,JSON.stringify(result));return result.result;}
async function ev(expression){const result=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!result.exceptionDetails,JSON.stringify(result));return result.result.value;}
async function wait(expression){for(let i=0;i<100;i++){if(await ev(expression))return;await new Promise(resolve=>setTimeout(resolve,30));}throw Error('Timeout: '+expression);}
async function enter(shift=false){await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13,modifiers:shift?8:0});await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,modifiers:shift?8:0});}
await cmd('Runtime.enable');await cmd('Page.enable');await cmd('Network.enable');
await cmd('Network.setBlockedURLs',{urls:['*/term/','*/term/*']});
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`
const originalFetch=window.fetch;window.testTutorRequests=[];const jobs=[];
const references=${JSON.stringify(refs)};
window.fetch=(url,options)=>{
 if(String(url).endsWith('tutor')){
  const body=JSON.parse(options.body);const response=()=>new Response(JSON.stringify({ok:true,session:'a'.repeat(64),chapter:'01',messages:jobs,terminal_sharing_allowed:false}),{headers:{'Content-Type':'application/json'}});
  if(body.action==='send'){
   window.testTutorRequests.push(body);
   return new Promise(resolve=>setTimeout(()=>{jobs.push({id:String(jobs.length),question:body.message,answer:'테스트 응답 [G1] [G2]',status:'done',evidence:references,created:Date.now()/1000});resolve(response());},300));
  }
  return Promise.resolve(response());
 }
 return originalFetch(url,options);
};
`});
await cmd('Page.navigate',{url:(process.argv[2]||process.env.NETLAB_URL||(()=>{console.error('usage: node browser_tutor_input.mjs <panel url>  (or NETLAB_URL)');process.exit(2)})())});
await wait('typeof tutorSend==="function"');
await ev('refresh().then(()=>document.getElementById("running").classList.remove("hide"))');
await ev('if(document.getElementById("tutorpanel").hidden)document.getElementById("tutorlauncher").click()');
await cmd('Page.bringToFront');
for(const width of [390,768,1366,1920]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  assert(await ev('(()=>{const a=document.getElementById("tutorinput").getBoundingClientRect(),b=document.getElementById("tutorsend").getBoundingClientRect();return b.left>=a.right+6&&Math.abs(b.top-a.top)<1&&Math.abs(b.height-a.height)<1&&document.documentElement.scrollWidth<=innerWidth})()'),'Send button must match input height and remain beside it');
}
await ev('document.getElementById("tutorinput").value="첫 줄";document.getElementById("tutorinput").focus()');
await enter(true);
assert.equal(await ev('testTutorRequests.length'),0);
assert(await ev('document.getElementById("tutorinput").value.includes("\\n")'));
await enter();
await wait('testTutorRequests.length===1');
await wait('!document.getElementById("tutorsend").disabled && document.getElementById("tutorinput").value===""');
assert.equal(await ev('testTutorRequests[0].message'),'첫 줄');
assert(await ev('document.querySelector(".tutor-sources").textContent.includes("04장 교재") && document.querySelector(".tutor-sources").textContent.includes("개념 해설")'));
assert(await ev('!document.getElementById("tutorlog").textContent.includes("README.md") && !document.getElementById("tutorlog").textContent.includes("server/knowledge") && !document.getElementById("tutorlog").textContent.includes("숨길 원문 발췌")'));
assert(await ev('fetch(document.querySelector(".tutor-sources a").href).then(async response=>{const html=await response.text();const doc=new DOMParser().parseFromString(html,"text/html");return response.status===200&&!!doc.querySelector("article h1")&&!!doc.querySelector("table")&&!doc.querySelector("header").textContent.includes("README.md")})'));
await ev('document.getElementById("tutorinput").value="한글 입력";document.getElementById("tutorinput").dispatchEvent(new CompositionEvent("compositionstart"));document.getElementById("tutorinput").dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));document.getElementById("tutorinput").dispatchEvent(new CompositionEvent("compositionend"));document.getElementById("tutorinput").dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",isComposing:true,bubbles:true}));document.getElementById("tutorinput").dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",keyCode:229,bubbles:true}))');
assert.equal(await ev('testTutorRequests.length'),1);
await ev('document.getElementById("tutorsend").click();document.getElementById("tutorinput").value="전송 중 새 초안"');
await enter();
await wait('document.querySelectorAll(".tutor-message").length===2');
assert.equal(await ev('testTutorRequests.length'),2,'No duplicate send while request is in flight');
assert.equal(await ev('document.getElementById("tutorinput").value'),'전송 중 새 초안');
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,enterSends:true,shiftEnterNewline:true,koreanCompositionProtected:true,sendButton:true,equalInputButtonHeight:true,friendlyLegacySourceLinks:true,renderedHtmlDocument:true,noDuplicateSend:true,newDraftPreserved:true,browserErrors:errors},null,2));
await cmd('Browser.close');ws.close();
