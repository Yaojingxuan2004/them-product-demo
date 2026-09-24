'use strict';
const $ = s => document.querySelector(s);
const escape = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const roleLabel = r => r === 'personal' ? '关于我' : '对朋友的期待';
const date = s => new Date(s).toLocaleString('zh-CN',{hour12:false});
const eventNames = {create:'提交自述',confirm:'确认并记住',edit:'编辑后确认',allow:'允许访客使用',revoke:'停止访客使用',ignore:'忽略',disable:'停用'};
let sessions={}, items=[], route='home', tab='pending', busy=false, panel=null, pending=null;
let draft={text:'',role:'personal'}, editDraft=null;
const main=$('#main');
// Authored, consistent SVG navigation icons; no external assets or dependencies.
const icons=['<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>','<path d="M20 11a8 8 0 0 1-8 8H5l-3 3V11a9 9 0 0 1 18 0Z"/><path d="M7 10h9M7 14h5"/>','<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h3"/>','<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>'];
document.querySelectorAll('.nav-icon').forEach((el,i)=>el.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true">${icons[i]}</svg>`);
function notify(message,error=false){let el=$('#notice');el.textContent=message;el.className='notice'+(error?' error':'');el.hidden=false;}
async function api(path,{method='GET',body,visitor=false}={}){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
  try{
    const res=await fetch(path,{method,headers:{'Content-Type':'application/json','X-Session':sessions[visitor?'visitor':'owner']||''},body:body?JSON.stringify(body):undefined,signal:controller.signal,cache:'no-store'});
    const data=await res.json();
    if(!res.ok){const error=new Error(data.error||'操作未完成');error.definite=res.status<500&&!data.uncertain;throw error;}
    return data;
  }finally{clearTimeout(timer);}
}
async function load(){items=(await api('/api/state')).items;}
function syncRecovery(){ $('#recovery').hidden=!pending; $('#retry-save').hidden=true; }
function lock(){main.querySelectorAll('button,input,textarea,select').forEach(el=>el.disabled=true);}
async function apply(body,reuse=false){
  if(busy||(!reuse&&pending))return;
  busy=true;lock();
  const operation=reuse?pending:{...body,operation_id:crypto.randomUUID()};
  notify('正在保存，请稍候…');
  try{
    const result=await api('/api/actions',{method:'POST',body:operation});
    pending=null;syncRecovery();panel=null;editDraft=null;
    if(operation.action==='create')draft.text='';
    notify(result.message);
    try{await load();}catch(e){notify('保存已完成，但最新列表暂未加载。请刷新资料。',true);}
  }catch(e){
    if(e.definite){notify(e.message,true);}
    else{pending=operation;syncRecovery();notify('尚未确认这次保存的结果，正在等待核对。草稿已保留。',true);}
  }finally{busy=false;await render();}
}
$('#check-save').onclick=async()=>{
  if(!pending)return;
  try{const state=await api('/api/operations/'+pending.operation_id);
    if(state.state==='committed'){
      if(pending.action==='create')draft.text='';
      pending=null;panel=null;editDraft=null;syncRecovery();await load();await render();notify(state.result.message);
    }else{$('#retry-save').hidden=false;notify('暂未找到已提交结果。可以继续同一保存，系统会防止重复记录。');}
  }catch(e){notify('仍无法核对，请保持页面，待服务恢复后再次核对。',true);}
};
$('#retry-save').onclick=()=>apply(null,true);
function title(name,copy,refresh=false){return `<div class="title-row"><div><h1>${name}</h1><p class="intro">${copy}</p></div>${refresh?'<button class="secondary" data-action="refresh">刷新资料</button>':''}</div>`;}
function home(){return `<section class="role-stage" aria-label="两个角色，各有所属">
  <div class="friend-field"><p class="role-name">理想好友 AI</p><h1>聊得来，<br>从一句你好开始。</h1><p class="hero-description">说说今天的你，慢慢找到喜欢的相处方式。</p><a class="primary link-button chat-entry" href="#ideal">开始聊天<svg viewBox="0 0 48 40" aria-hidden="true"><path d="M3 20h39M28 6l14 14-14 14"/></svg></a><p class="hero-signature"><span>ThEM</span>用对话，遇见更多可能的你</p></div>
  <div class="avatar-field"><p class="role-name">PERSONAL AVATAR</p><h2>个人分身</h2><svg class="conversation-cards" viewBox="0 0 540 305" aria-hidden="true"><g transform="rotate(-17 147 145)"><path class="card-violet" d="M58 61h168q27 0 27 27v99q0 27-27 27H106l-35 23 3-23H58q-27 0-27-27V88q0-27 27-27Z"/><path class="card-paper-stroke" d="M72 116h85m-85 33h80"/></g><g transform="rotate(15 400 145)"><path class="card-yellow" d="M330 53h155q27 0 27 27v97q0 27-27 27h-1l-2 24-30-24H330q-27 0-27-27V80q0-27 27-27Z"/></g><g transform="rotate(8 280 190)"><path class="card-paper" d="M210 115h167q28 0 28 28v97q0 27-28 27h-2l1 26-31-26H210q-28 0-28-27v-97q0-28 28-28Z"/><path class="card-violet-stroke" d="M225 177h130m-130 32h98"/></g><path class="card-violet-stroke" d="m442 43 12-34m18 56 31-29"/></svg><p class="avatar-promise">由你确认，替你表达</p><a class="secondary link-button memory-entry" href="#memory">管理我的资料</a></div>
  </section><ol class="journey" aria-label="从交流到分身表达的三个步骤">
  <li><div><span class="step-number" aria-hidden="true">01</span><h2>聊聊自己</h2><p>从今天想说的话开始</p></div><svg viewBox="0 0 120 110" aria-hidden="true"><path d="M12 35h71a9 9 0 0 1 9 9v38a9 9 0 0 1-9 9H37l-16 12V91h-9a9 9 0 0 1-9-9V44a9 9 0 0 1 9-9Z"/><path d="M24 57h47M24 72h33m54-57 5-13m9 26 10-8"/></svg></li>
  <li><div><span class="step-number" aria-hidden="true">02</span><h2>核对记忆</h2><p>确认之后，再决定展示给谁</p></div><svg viewBox="0 0 120 110" aria-hidden="true"><rect x="13" y="8" width="64" height="81" rx="9"/><path d="M30 30h29M30 47h29M30 64h14"/><circle class="step-check" cx="78" cy="80" r="23"/><path d="m68 80 8 8 12-15"/></svg></li>
  <li><div><span class="step-number" aria-hidden="true">03</span><h2>预览分身</h2><p>看看它会怎样介绍你</p></div><svg viewBox="0 0 120 110" aria-hidden="true"><rect class="step-pink" x="8" y="29" width="83" height="58" rx="8" transform="rotate(-10 8 29)"/><rect class="step-purple" x="19" y="34" width="96" height="60" rx="9"/><path class="step-eye" d="M43 64q24-27 48 0-24 27-48 0Z"/><circle class="step-purple" cx="67" cy="64" r="9"/><path d="m99 16 5-13m8 24 7-8"/></svg></li></ol>`;}
function form(){return `<form id="self-form" class="form-panel"><fieldset><legend>写一条你愿意记住的内容</legend><div class="role-options"><label class="role-choice"><input type="radio" name="role" value="personal" ${draft.role==='personal'?'checked':''}>关于我</label><label class="role-choice"><input type="radio" name="role" value="ideal" ${draft.role==='ideal'?'checked':''}>对朋友的期待</label></div></fieldset><label class="field-label" for="self-text">${draft.role==='personal'?'关于我，你可以记住……':'我希望朋友这样和我交流……'}</label><textarea id="self-text" maxlength="300" required placeholder="例如：我比较慢热，更喜欢先聊熟悉的话题。">${escape(draft.text)}</textarea><div class="field-bottom"><span>请一次表达一件事；这里不会自动提取或改写。</span><span id="count">${draft.text.length}/300</span></div><div class="actions"><button class="primary" type="submit">保存为待确认</button><button class="quiet" type="button" data-action="example">填入合成示例</button></div><p class="hint">提交后尚未记住，仍需你核对确认。请使用合成内容体验。</p></form>`;}
function editor(item){return `<form class="inline-panel reveal" data-edit="${item.id}"><label class="field-label" for="edit-text">修改后的完整内容</label><textarea id="edit-text" maxlength="300" required>${escape(editDraft.text)}</textarea><label class="field-label" for="edit-role">归属</label><select id="edit-role"><option value="personal" ${editDraft.role==='personal'?'selected':''}>关于我</option><option value="ideal" ${editDraft.role==='ideal'?'selected':''}>对朋友的期待</option></select><p>修改作为新的明确自述保存，原始依据不改写。新内容默认仅本人使用。</p><label class="check"><input id="edit-confirm" type="checkbox" required ${editDraft.confirmed?'checked':''}>我确认新的正文和归属表达了我的意思，期待没有被当成自身事实。</label><div class="actions"><button class="primary" type="submit">保存修改并记住</button><button type="button" class="secondary" data-action="cancel">取消编辑</button></div></form>`;}
function confirmPanel(item,action){
 const info={allow:['允许分身向访客使用这条内容？','仅允许下面这条准确内容，不会公开整段原始记录。','确认允许访客使用'],disable:['停用这条记忆？','之后本人和访客的记忆读取都不再使用它。原始记录与历史不会删除。','确认停用']}[action];
 return `<div class="inline-panel reveal"><h3>${info[0]}</h3><p>${info[1]}</p><blockquote>${escape(item.text)}</blockquote><div class="actions"><button class="${action==='disable'?'danger':'primary'}" data-action="commit-${action}" data-id="${item.id}">${info[2]}</button><button class="secondary" data-action="cancel">取消</button></div></div>`;
}
function row(item){const active=item.status==='accepted',isPending=item.status==='pending';return `<article class="memory-row"><div class="memory-head"><span>${roleLabel(item.role)}</span><span class="badge ${item.public?'':'private'}">${item.public?'访客可用':active?'仅本人使用':isPending?'待确认':item.status==='ignored'?'已忽略':'已停用'}</span><span>${date(item.updated)}</span></div><p class="memory-text">${escape(item.text)}</p><details><summary>查看原始依据与处理记录</summary><p>${item.source_kind==='explicit_correction'?'明确修正':item.source_kind==='mock_chat_self_report'?'聊天原文（模拟整理）':item.source_kind==='chat_self_report'?'主人聊天原文':'自述输入'} · ${date(item.source_created)}</p><blockquote>${escape(item.source_text)}</blockquote>${item.history.map(h=>`<div class="history-event"><strong>${eventNames[h.event]}</strong> · ${date(h.created)}<p>${escape(h.snapshot.text)} <span class="subtle">（${roleLabel(h.snapshot.role)}）</span></p></div>`).join('')}</details>
 ${isPending?`<div class="actions"><button class="primary" data-action="confirm" data-id="${item.id}">确认并记住</button><button class="secondary" data-action="edit" data-id="${item.id}">编辑后确认</button><button class="quiet" data-action="ignore" data-id="${item.id}">忽略</button></div>`:''}
 ${active?`<div class="actions">${item.role==='personal'?`<button class="${item.public?'secondary':'primary'}" data-action="${item.public?'revoke':'allow'}" data-id="${item.id}">${item.public?'停止访客使用':'允许访客使用'}</button>`:''}<button class="secondary" data-action="edit" data-id="${item.id}">修改内容</button><button class="quiet" data-action="disable" data-id="${item.id}">停用</button></div>`:''}
 ${panel?.id===item.id?(panel.action==='edit'?editor(item):confirmPanel(item,panel.action)):''}</article>`;}
function memory(){const rows=items.filter(i=>tab==='history'?['ignored','disabled'].includes(i.status):i.status===tab);return title('记忆与展示','记住什么，由你确认。向访客表达什么，由你另行决定。',true)+form()+`<div class="tabs" role="group" aria-label="记忆状态">${[['pending','待确认'],['accepted','已记住'],['history','处理记录']].map(([id,name])=>`<button aria-pressed="${tab===id}" data-tab="${id}">${name}</button>`).join('')}</div><section class="memory-list" aria-label="${tab==='pending'?'待确认内容':tab==='accepted'?'已记住内容':'处理记录'}">${rows.length?rows.map(row).join(''):`<div class="empty"><h3>${tab==='pending'?'还没有待确认的内容':tab==='accepted'?'从确认一条内容开始':'这里会保留忽略与停用记录'}</h3><p>${tab==='pending'?'先写下一条自述，再核对它是否准确。':tab==='accepted'?'在「待确认」中选择准确的内容，确认后默认仅供本人使用。':'停用不会删除原始依据，也不会自动恢复为有效记忆。'}</p></div>`}</section>`;}
async function render(){
 route=location.hash.slice(1)||'home'; if(!['home','memory','ideal','preview'].includes(route))route='home';
 document.body.dataset.route=route;
 const labels={home:'我的双角色',memory:'记忆与展示',ideal:'理想好友',preview:'分身预览'};
 $('#crumb').textContent=labels[route];document.title=`ThEM · ${labels[route]}`;
 document.querySelectorAll('nav a').forEach(a=>a.dataset.route===route?a.setAttribute('aria-current','page'):a.removeAttribute('aria-current'));
 if(route==='home')main.innerHTML=home();
 if(route==='memory')main.innerHTML=memory()+'<section class="plain-section"><button class="secondary" data-action="history">查看本人对话历史</button><div id="owner-history" hidden></div></section>';
 if(route==='ideal'||route==='preview')await ThemChat.render(route);
 if(busy||pending)lock();
}
main.addEventListener('input',async e=>{
 if(e.target.id==='self-text'){draft.text=e.target.value;$('#count').textContent=draft.text.length+'/300';}
 if(e.target.name==='role'){draft.role=e.target.value;await render();main.querySelector(`input[name="role"][value="${draft.role}"]`)?.focus();}
 if(e.target.id==='edit-text')editDraft.text=e.target.value;
 if(e.target.id==='edit-role')editDraft.role=e.target.value;
 if(e.target.id==='edit-confirm')editDraft.confirmed=e.target.checked;
});
main.addEventListener('submit',e=>{
 e.preventDefault();if(pending||busy)return;
 if(e.target.id==='self-form'){tab='pending';apply({action:'create',entries:[{text:draft.text,role:draft.role}]});}
 if(e.target.dataset.edit)apply({action:'edit',item_id:editDraft.id,expected_version:editDraft.version,text:editDraft.text,role:editDraft.role,meaning_confirmed:editDraft.confirmed});
});
main.addEventListener('click',async e=>{
 const memoryLink=e.target.closest('a[data-memory-tab]');
 if(memoryLink?.dataset.memoryTab==='pending')tab='pending';
 const button=e.target.closest('button');if(!button||busy||pending)return;
 if(button.dataset.tab){tab=button.dataset.tab;panel=null;editDraft=null;await render();main.querySelector(`[data-tab="${tab}"]`)?.focus();return;}
 const action=button.dataset.action, item=items.find(i=>i.id===button.dataset.id);
 if(action==='history'){try{const data=await api('/api/chat/history');const box=document.querySelector('#owner-history');box.innerHTML='<p>'+escape(data.note)+'</p>'+data.threads.map(t=>`<details><summary>${t.kind==='ideal'?'理想好友':'分身预览'} · ${date(t.created)} · ${t.provider==='mock'?'模拟':'真实API'} · ${escape(t.status)}</summary>${t.messages.map(m=>`<p><strong>${m.speaker==='user'?'已发送消息':'回复'}</strong>：${escape(m.text)}</p>`).join('')}</details>`).join('');box.hidden=false;}catch(e){notify(e.message,true);}return;}
 if(action==='refresh'){try{await load();await render();notify('已读取最新保存状态。');}catch(e){notify('读取失败，请确认本地服务仍在运行。',true);}return;}
 if(action==='example'){draft.text=draft.role==='personal'?'我比较慢热，更喜欢先聊熟悉的话题。':'我希望朋友尊重安静，不必一直主动找话题。';render();notify('已填入合成示例，尚未提交。');return;}
 if(action==='cancel'){const previous=panel;panel=null;editDraft=null;await render();if(previous)main.querySelector(`[data-action="${previous.action}"][data-id="${previous.id}"]`)?.focus();return;}
 if(!item)return;
 if(['edit','allow','disable'].includes(action)){
  panel={id:item.id,action};if(action==='edit')editDraft={id:item.id,version:item.version,text:item.text,role:item.role,confirmed:false};
  await render();document.querySelector('.inline-panel textarea, .inline-panel button')?.focus();return;
 }
 const actual=action.replace('commit-','');
 apply({action:actual,item_id:item.id,expected_version:item.version,...(actual==='allow'?{confirmed_text:item.text}:{})});
});
window.addEventListener('hashchange',async()=>{if(!busy&&!pending)$('#notice').hidden=true;if(location.hash==='#memory'){try{await load();}catch{notify('未能更新记忆列表，请刷新资料核对整理结果。',true);}}render();});
document.querySelector('.skip').addEventListener('click',e=>{e.preventDefault();main.focus();});
window.addEventListener('beforeunload',e=>{if(pending||busy||editDraft||draft.text){e.preventDefault();e.returnValue='';}});
ThemChat.configure({api,notify});
async function start(){
 main.innerHTML='<p class="loading">正在打开本地资料…</p>';
 try{const boot=await api('/api/bootstrap');sessions=boot.sessions;document.querySelector('.sidebar-foot p').innerHTML='资料保存在这台电脑上。<br>'+ (boot.provider==='mock'?'当前为模拟流程，无模型调用。':'真实API模式，发送会消耗额度。');await load();await render();}
 catch(e){main.innerHTML='<h1>暂时无法连接本地服务</h1><p>请先启动 server.py，再刷新此页面。已有保存内容不会因此清空。</p>';notify(e.message,true);}
}
start();
