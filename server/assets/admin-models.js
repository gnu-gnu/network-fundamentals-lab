/* Admin-only configuration editor. Keys are write-only and never persisted in browser storage. */
(() => {
  const $=id=>document.getElementById(id),table=$('modelrows');if(!table)return;
  const dialog=$('modeleditor'),form=$('modelform'),status=$('modelstatus');
  let state={models:[],revision:''},editing=false,saving=false;
  const endpoint=new URL('models',location.href);
  async function request(body){
    const response=await fetch(endpoint,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,revision:state.revision})}:{cache:'no-store'});
    const data=await response.json();if(!response.ok||!data.ok)throw Error(data.message||'모델 설정 요청에 실패했습니다.');return data;
  }
  function button(label,handler){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=handler;return b;}
  function cell(text){const td=document.createElement('td');td.textContent=text;return td;}
  async function change(body){
    if(saving)return;saving=true;
    try{state=await request(body);status.textContent='저장했습니다. 사용자 모델 목록에 반영됐습니다.';render();return true;}
    catch(error){status.textContent=error.message;$('modeleditorstatus').textContent=error.message;return false;}
    finally{saving=false;}
  }
  function render(){
    table.replaceChildren();
    for(const model of state.models){
      const row=document.createElement('tr'),actions=document.createElement('td'),group=document.createElement('div');group.className='model-actions';
      row.append(cell(model.label),cell(model.id),cell(model.enabled?'노출':'숨김'),cell(model.id===state.default_model?'기본':'—'));
      group.append(button('편집',()=>edit(model)),button(model.enabled?'숨기기':'노출하기',()=>change({action:'visibility',id:model.id,enabled:!model.enabled})));
      if(model.enabled&&model.id!==state.default_model)group.append(button('기본으로',()=>change({action:'default',id:model.id})));
      const remove=button('삭제',()=>{if(confirm(model.label+'을(를) 사용자 모델 목록에서 삭제할까요? 기존 대화와 모델 서버는 유지됩니다.'))change({action:'delete',id:model.id});});remove.className='danger';group.append(remove);
      actions.append(group);row.append(actions);table.append(row);
    }
    if(!state.models.length){const row=document.createElement('tr'),td=cell('등록된 모델이 없습니다. 모델을 추가해 주세요.');td.colSpan=5;td.className='empty';row.append(td);table.append(row);}
    if(!state.models.some(model=>model.enabled))status.textContent='사용자에게 노출된 모델이 없습니다. AI 튜터 질문을 사용할 수 없습니다.';
  }
  async function load(){try{state=await request();status.textContent='';render();}catch(error){status.textContent=error.message;}}
  function edit(model){
    editing=Boolean(model);form.reset();$('modeleditorstatus').textContent='';
    $('modeleditortitle').textContent=editing?'모델 편집':'모델 추가';
    $('modelid').value=model?.id||'';$('modelid').readOnly=editing;
    $('modellabel').value=model?.label||'';$('modelname').value=model?.model||'';$('modelendpoint').value=model?.endpoint||'';
    $('modelkey').value='';$('modelkey').placeholder=model?.has_api_key?'키 설정됨 · 변경할 때만 입력':'키가 필요할 때 입력';
    $('modelenabled').checked=model?.enabled??true;$('modeldefault').checked=model?.id===state.default_model;
    $('modelparameters').value=JSON.stringify(model?.parameters||{},null,2);dialog.showModal();
  }
  function close(){dialog.close();form.reset();$('modelkey').value='';}
  $('modeladd').onclick=()=>edit();$('modelrefresh').onclick=load;$('modelclose').onclick=close;$('modelcancel').onclick=close;
  dialog.addEventListener('close',()=>{$('modelkey').value='';});
  form.onsubmit=async event=>{
    event.preventDefault();if(saving)return;
    let parameters;try{parameters=JSON.parse($('modelparameters').value.trim()||'{}');}catch{$('modeleditorstatus').textContent='추론 설정에 올바른 JSON을 입력하세요.';return;}
    const model={label:$('modellabel').value.trim(),model:$('modelname').value.trim(),endpoint:$('modelendpoint').value.trim(),enabled:$('modelenabled').checked,parameters};
    if($('modelclearkey').checked)model.api_key='';else if($('modelkey').value)model.api_key=$('modelkey').value;
    $('modelsave').disabled=true;
    try{if(await change({action:'save',id:$('modelid').value.trim(),create:!editing,model,set_default:$('modeldefault').checked}))close();}
    finally{$('modelsave').disabled=false;}
  };
  load();
})();
