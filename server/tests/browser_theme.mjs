// Requires an isolated Chromium CDP page on :9228. Does not modify labs.
import assert from 'node:assert/strict';
const pages=await(await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let count=0;const pending=new Map(),errors=[];
ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.text);if(pending.has(data.id)){pending.get(data.id)(data);pending.delete(data.id);}});
async function cmd(method,params={}){const id=++count;const result=await new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,method,params}));});assert(!result.error,JSON.stringify(result));return result.result;}
async function ev(expression){const result=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!result.exceptionDetails,JSON.stringify(result));return result.result.value;}
async function wait(expression){for(let i=0;i<100;i++){if(await ev(expression))return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('Timeout: '+expression);}
async function reload(){const marker=String(Math.random());await ev('window.themeTestMarker='+JSON.stringify(marker));await cmd('Page.reload');await wait('window.themeTestMarker!=='+JSON.stringify(marker)+' && document.readyState==="complete" && !!document.getElementById("themetoggle")');}
await cmd('Runtime.enable');await cmd('Page.enable');
await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'}]});
const initial=await cmd('Page.addScriptToEvaluateOnNewDocument',{source:'localStorage.removeItem("netlab-theme")'});
await cmd('Page.navigate',{url:(process.argv[2]||process.env.NETLAB_URL||(()=>{console.error('usage: node browser_theme.mjs <panel url>  (or NETLAB_URL)');process.exit(2)})())});
await wait('document.readyState==="complete" && !!document.getElementById("themetoggle")');
await cmd('Page.removeScriptToEvaluateOnNewDocument',{identifier:initial.identifier});
assert.equal(await ev('document.documentElement.dataset.theme'),'dark');
assert.equal(await ev('getComputedStyle(document.body).backgroundColor'),'rgb(13, 22, 33)');
await ev('document.getElementById("themetoggle").click()');
assert.equal(await ev('document.documentElement.dataset.theme'),'light');
assert.equal(await ev('getComputedStyle(document.body).backgroundColor'),'rgb(246, 248, 250)');
assert.equal(await ev('getComputedStyle(document.documentElement).colorScheme'),'light');
await reload();
assert.equal(await ev('document.documentElement.dataset.theme'),'light');
assert.equal(await ev('localStorage.getItem("netlab-theme")'),'light');
await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'light'}]});
await ev('document.getElementById("themetoggle").focus()');
await cmd('Page.bringToFront');
await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13});
await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
assert.equal(await ev('document.documentElement.dataset.theme'),'dark');
assert.equal(await ev('document.getElementById("themetoggle").getAttribute("aria-pressed")'),'true');
await reload();
assert.equal(await ev('document.documentElement.dataset.theme'),'dark');
await ev('localStorage.removeItem("netlab-theme")');await reload();
assert.equal(await ev('document.documentElement.dataset.theme'),'light');
await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'}]});
await wait('document.documentElement.dataset.theme==="dark"');
for(const width of [390,768,1366,1920]){
  await cmd('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  assert(await ev('(()=>{const r=document.getElementById("themetoggle").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.top<150&&document.documentElement.scrollWidth<=innerWidth})()'));
}
assert.deepEqual(errors,[]);
console.log(JSON.stringify({ok:true,systemDefault:true,lightAndDark:true,persistsAcrossReload:true,followsSystemUntilExplicitChoice:true,keyboardToggle:true,mobileLayout:true,browserErrors:errors},null,2));
await cmd('Browser.close');ws.close();
