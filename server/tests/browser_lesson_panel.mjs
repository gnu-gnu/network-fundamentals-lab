// Lesson panel (M1-11): layout at five widths, request shape, no automatic retry, 410 hides the panel.
// Lesson API is mocked in the page; nothing reaches a runner. Usage:
//   python3 server/tests/serve_panel.py 18990 &   (chrome --remote-debugging-port=9228)
//   node server/tests/browser_lesson_panel.mjs http://127.0.0.1:18990/u/guest/ctl/
import assert from 'node:assert/strict';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let count=0;const pending=new Map(),errors=[];
ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.exception?.description||data.params.exceptionDetails.text);if(pending.has(data.id)){pending.get(data.id)(data);pending.delete(data.id);}});
async function cmd(method,params={}){const id=++count;const result=await new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,method,params}));});assert(!result.error,JSON.stringify(result));return result.result;}
async function ev(expression){const result=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!result.exceptionDetails,JSON.stringify(result));return result.result.value;}
async function wait(expression,what){for(let i=0;i<200;i++){if(await ev(expression))return;await new Promise(resolve=>setTimeout(resolve,30));}throw Error('Timeout: '+(what||expression));}
await cmd('Runtime.enable');await cmd('Page.enable');await cmd('Network.enable');
await cmd('Network.setBlockedURLs',{urls:['*/term/','*/term/*']});
await cmd('Page.addScriptToEvaluateOnNewDocument',{source:`
const originalFetch=window.fetch;window.lessonPosts=[];window.lessonMode='normal';
const env={ok:true,state:'running',lesson:'network/12',observed:{incarnation:'20d41a0c-b096-4f66-927e-22152ed8f9ba',generation:3},
 bundle:'b'.repeat(64),ready:true,recovery:null,runtime:{topologies:['12-mtu-pmtud/12-mtu-pmtud.clab.yml'],matches:true},
 check_running:false,capabilities:['status','start','reset','stop','check'],failure:null,bundles_current:{network:true}};
window.lessonJobs=[{job_id:'d'.repeat(32),action:'reset',lesson_id:'network/12',step_id:null,state:'running'}];
const lesson={id:'network/12',title:'12장 MTU와 PMTUD',number:'12',completion:'required-steps',revision:'r',bundle:'b'.repeat(64),solvable:true,
 steps:[{id:'guide',kind:'read',title:'실습 안내',required:false,html:'<h1 id="L1">12장</h1><p>본문 '+'아주 긴 설명 '.repeat(60)+'</p><pre id="L5"><code><span id="L6">ping -M do -s 1472 -c1 10.12.2.10 '+'x'.repeat(200)+'</span></code></pre><div class="table-scroll"><table><tr><td>'+'긴셀'.repeat(80)+'</td></tr></table></div>'},
        {id:'verify',kind:'task',title:'목표 달성 확인',required:true,goal:'큰 DF 패킷에 MTU 통보를 받는다.',hints:3}]};
const attempt={attempt_id:'a',lesson_id:'network/12',generation:3,incarnation:env.observed.incarnation,started_at:'2026-10-24T00:00:00+00:00',
 completed_at:null,shared:false,steps:{guide:'unread',verify:'untested'},hints_opened:{},last_check:{},completions:0};
const json=(status,body)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
window.fetch=(url,options={})=>{
 const u=String(url);
 if(u.includes('/lesson/')||u.startsWith('lesson/')){
  if(window.lessonMode==='missing') return json(410,{ok:false,error:'runner_missing',message:'실행기 없음'});
  if(options.method==='POST'){
   const body=JSON.parse(options.body);window.lessonPosts.push({url:u,body});
   if(u.endsWith('/check')) return json(202,{ok:true,job_id:'c'.repeat(32),state:'queued'});
   return json(200,{ok:true});
  }
  if(u.endsWith('lesson/state')) return json(200,{ok:true,environment:env,catalog:[{id:'network/12',title:lesson.title,number:'12'}],attempt,shared:false,jobs:window.lessonJobs});
  if(u.endsWith('lesson/jobs/'+'d'.repeat(32))){ window.lessonJobs=[]; return json(200,{ok:true,job_id:'d'.repeat(32),state:'done',http_status:200,
    result:{ok:true,runner:{state:'running'},progress:{}}}); }
  if(u.endsWith('lesson/network/12')) return json(200,{ok:true,lesson});
  if(u.includes('lesson/jobs/')) return json(200,{ok:true,job_id:'c'.repeat(32),state:'failed',http_status:409,
    result:{ok:false,error:'conflict',message:'실습 환경이 그사이 바뀌었습니다. 상태를 새로 확인한 뒤 다시 시도하세요.'}});
 }
 if(u.endsWith('status')) return json(200,{state:'running',instance:'netlab-test',ip:'10.0.0.2',policy:''});
 return originalFetch(url,options);
};
`});
const url=process.argv[2]||process.env.NETLAB_URL;
if(!url){console.error('usage: node browser_lesson_panel.mjs <panel url>');process.exit(2);}
await cmd('Page.navigate',{url});
await wait('typeof refresh==="function"&&document.getElementById("lessonpanel")');
await wait('!document.getElementById("running").classList.contains("hide")','running view');
await wait('!document.getElementById("lessonpanel").hidden&&document.querySelectorAll(".lesson-step").length===2','lesson panel rendered');
// A job still running from before the reload is picked up and followed to its end.
await wait('document.getElementById("lessonstatus").textContent.includes("초기화 완료")','resumed job');
assert(await ev('!document.getElementById("lessonsolve").hidden'),'solvable lessons show the solve button');
await ev('document.querySelector(".lesson-step details").open=true');
for(const width of [390,768,1024,1366,1920]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<600});
  await new Promise(r=>setTimeout(r,150));
  const m=await ev(`(()=>{const t=document.getElementById('termframe').getBoundingClientRect(),p=document.getElementById('lessonpanel').getBoundingClientRect();
    const overlap=!(p.right<=t.left||p.left>=t.right||p.bottom<=t.top||p.top>=t.bottom);
    return {scroll:document.documentElement.scrollWidth,inner:innerWidth,overlap,beside:p.left>=t.right,below:p.top>=t.bottom,termW:t.width}})()`);
  assert(m.scroll<=m.inner,`no horizontal overflow at ${width}px (${m.scroll}>${m.inner})`);
  assert(!m.overlap,`lesson panel must not cover the terminal at ${width}px`);
  assert(m.beside||m.below,`lesson panel sits beside or below the terminal at ${width}px`);
  if(width>=1366) assert(m.beside,`wide screens use the space next to the terminal (${width}px)`);
}
// Code blocks: copy button only; nothing is sent anywhere.
assert(await ev('document.querySelectorAll(".lesson-body pre .lesson-copy").length===1'));
await ev('document.querySelector(".lesson-copy").click()');
assert.equal(await ev('lessonPosts.length'),0);
// Check sends request_id + the observed tokens, then shows the conflict without retrying.
await ev('document.querySelector("[data-check=verify]").click()');
await wait('lessonPosts.length===1','check POST');
const post=await ev('lessonPosts[0]');
assert(post.url.endsWith('lesson/network/12/step/verify/check'));
assert.match(post.body.request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
assert.deepEqual(post.body.expected,{incarnation:'20d41a0c-b096-4f66-927e-22152ed8f9ba',generation:3});
await wait('document.getElementById("lessonstatus").textContent.includes("그사이 바뀌었습니다")','conflict message');
await new Promise(r=>setTimeout(r,1500));
assert.equal(await ev('lessonPosts.length'),1,'a conflict is never retried automatically');
// Runner missing (v1 VM): the panel disappears and the page keeps working.
await ev('lessonMode="missing";document.getElementById("lessonrefresh").click()');
await wait('document.getElementById("lessonpanel").hidden','panel hidden on 410');
assert(await ev('!document.getElementById("termframe").closest(".hide")'),'terminal stays visible');
assert.deepEqual(errors,[],'no page errors: '+errors.join('\n'));
console.log('browser_lesson_panel: ok');
ws.close();
