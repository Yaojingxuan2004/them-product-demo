'use strict';
window.ThemChat=(()=>{
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const main=document.querySelector('#main');
 let api, notify, current=null, state=null, timer=null, loading=false, posting=false, generation=0, scopePanel=false;
 const drafts={ideal:'',preview:''},checked=new Set();
 const pending={};
 for(const kind of ['ideal','preview']){try{pending[kind]=JSON.parse(sessionStorage.getItem('them-chat-pending-'+kind)||'null');}catch{pending[kind]=null;}}
 const path=k=>'/api/chat/state?kind='+k;
 const active=()=>location.hash==='#'+current;
 const statusNames={queued:'已接收，等待处理',running:'正在处理',succeeded:'已保存结果',failed:'未取得可用结果',uncertain:'结果待核对',retried:'已建立新的尝试，原结果仍保留',stale:'资料或会话已变化，结果未发布'};
 function savePending(k,p){pending[k]=p;if(p)sessionStorage.setItem('them-chat-pending-'+k,JSON.stringify(p));else sessionStorage.removeItem('them-chat-pending-'+k);}
 function taskMarkup(task){return `<div class="task-status ${['failed','uncertain'].includes(task.status)?'task-error':''}"><strong>${task.kind==='extract'?'记忆整理':'对话回复'} · ${statusNames[task.status]}</strong>${task.error?`<p>${esc(task.error)}</p>`:''}${task.kind==='extract'&&task.status==='succeeded'?`<p>${task.candidate_count?`此次整理产生${task.candidate_count}条候选，请到记忆页核对含义及已有记录。`:'此次没有适合提取的内容，原对话保留。'}</p>`:''}${['failed','uncertain'].includes(task.status)&&state.thread?.status==='active'?`<div class="actions"><button class="secondary" data-chat="check" data-task="${task.id}">核对任务状态</button>${task.status==='failed'||checked.has(task.id)?`<button class="secondary" data-chat="retry" data-task="${task.id}">${task.status==='uncertain'?'确认未找回，重新尝试':'明确重试本条任务'}</button>`:''}</div>${task.status==='uncertain'&&checked.has(task.id)?'<p>本地仍无可用结果；无法据此保证远程没有执行。重新尝试可能产生新调用，并占用尝试额度。</p>':''}`:''}</div>`;}
 function draw(){
  if(!active()||!state)return;
  const ideal=current==='ideal', t=state.thread, stale=t&&t.status!=='active';
  const tasks=state.tasks, replyWaiting=tasks.some(t=>t.kind==='reply'&&['queued','running','uncertain'].includes(t.status));
  const extractWaiting=tasks.some(t=>t.kind==='extract'&&['queued','running','uncertain'].includes(t.status));
  const mock=state.provider==='mock', blocked=posting||!!pending[current];
  const title=ideal?'理想好友':'个人分身预览';
  main.innerHTML=`<div class="title-row"><div><h1>${title}</h1><p class="intro">${ideal?'在交流中表达自己，也说说你希望如何被回应。':'以访客视角提问，检查分身如何使用你允许的资料。'}</p></div><button class="secondary" data-chat="refresh">刷新进度</button></div>
  <div class="mode-banner" role="status"><strong>${mock?'模拟流程 · 不调用模型':'真实API模式 · 每次发送或整理消耗额度'}</strong><p>${mock?'这里使用固定的合成示例回复，供你体验操作流程，不代表真实模型效果。':'内容由AI生成，请核对重要信息。发送失败时不会自动重试。'}</p></div>
  ${posting?'<div class="task-status">正在提交，请稍候…</div>':pending[current]?'<div class="task-status task-error"><p>提交回执尚未确认。请先核对，不要另发相同消息。</p><button class="secondary" data-chat="recover">核对这次提交</button></div>':''}
  ${stale?'<div class="task-status"><strong>这段会话已不能继续</strong><p>资料、用途或会话已变化。开启新会话后重新表达，不携带旧消息或摘要。旧记录仅在本人历史中回看。</p></div>':''}
  ${!ideal?`<details class="available-records"><summary>当前访客可用资料</summary>${state.visible_memories.length?state.visible_memories.map(m=>`<p>${esc(m.text)}</p>`).join(''):'<p>目前暂无可用于介绍本人的资料。</p>'}</details>`:''}
  <section class="chat-transcript" aria-label="已保存的对话">${state.messages.length?state.messages.map(m=>`<article class="chat-message ${m.speaker==='user'?'from-user':'from-ai'}"><div class="chat-speaker">${m.speaker==='user'?(ideal?'主人发言':'访客提问'):(mock?'模拟回复':ideal?'理想好友AI':'AI个人分身')}<time>${new Date(m.created).toLocaleString('zh-CN',{hour12:false})}</time></div><p>${esc(m.text)}</p></article>`).join(''):`<div class="empty"><h2>${ideal?'从你想说的一句话开始':state.visible_memories.length?'提出一个你想了解的问题':'目前暂无可用于介绍本人的资料'}</h2><p>${ideal?'聊天不会自动成为长期记忆。需要时，再主动整理并逐条确认。':'回答不代表真人承诺。只有允许用于访客的资料才会进入本次请求。'}</p></div>`}</section>
  <section aria-label="任务进度" aria-live="polite">${tasks.filter(t=>t.status!=='succeeded'||t.kind==='extract').slice(-4).map(taskMarkup).join('')}</section>
  ${!stale?`<form id="chat-form" class="chat-composer"><label for="chat-text">${ideal?'发给理想好友':'向个人分身提问'}</label><textarea id="chat-text" maxlength="800" required placeholder="${ideal?'例如：我不主动，但希望朋友主动。':'例如：你那次活动什么时候？'}">${esc(drafts[current])}</textarea><div class="field-bottom"><span>当前回答使用最近5轮完整往返；更早历史只供回看。</span><span id="chat-count">${drafts[current].length}/800</span></div><div class="actions"><button class="primary" type="submit" ${replyWaiting||blocked||(!ideal&&!state.visible_memories.length)?'disabled':''}>${replyWaiting?'等待结果后发送':mock?'发送（模拟）':'发送'}</button>${mock?'<button type="button" class="quiet" data-chat="example">填入合成聊天示例</button>':''}</div></form>`:''}
  <div class="actions chat-tools"><button class="secondary" data-chat="new" ${blocked?'disabled':''}>${ideal?'开始新的对话':'开启新的预览'}</button>${ideal?`<button class="secondary" data-chat="scope" ${stale||blocked||extractWaiting||!state.extractable.length?'disabled':''}>整理这段对话</button><a class="quiet link-button" href="#memory" data-memory-tab="pending">查看待确认内容</a>`:'<a class="quiet link-button" href="#memory">返回主人设置</a>'}</div>
  ${scopePanel&&ideal&&!stale?`<section class="inline-panel"><h2>核对本次整理范围</h2><p>最多4条尚未整理的主人发言。提交后固定此范围，新消息留待下一次。AI回复和访客发言不会成为自述来源。</p>${state.extractable.map(m=>`<blockquote>${esc(m.text)}</blockquote>`).join('')}<button class="primary" data-chat="extract" ${blocked||extractWaiting?'disabled':''}>确认整理这${state.extractable.length}条${mock?'（模拟）':''}</button><button class="secondary" data-chat="cancel-scope">取消</button></section>`:''}
  <p class="hint">${ideal?'整理后的候选仍需核对正文、归属、时间和与已有资料的冲突。':'本工作区不显示私人内容及数量，不把访客发言写成主人记忆。'}</p>`;
 }
 async function refresh(force=false){
  if(loading||!current||!active())return;
  const kind=current, token=generation;loading=true;
  try{const next=await api(path(kind),{visitor:kind==='preview'});if(token!==generation||!active())return;
   const changed=JSON.stringify(next)!==JSON.stringify(state);state=next;
   if(changed||force){const input=document.activeElement?.id==='chat-text',start=input?document.activeElement.selectionStart:null;draw();if(input){const el=document.querySelector('#chat-text');el?.focus();if(el)el.setSelectionRange(start,start);}}
  }catch{if(active())notify('未能读取最新会话。请保持页面并检查本地服务，刷新不会重新生成。',true);}
  finally{loading=false;if(active()){clearTimeout(timer);timer=setTimeout(()=>refresh(),1500);}}
 }
 async function post(body,reuse=false){
  const kind=current;if(posting||(!reuse&&pending[kind]))return;
  posting=true;const operation=reuse?pending[kind]:{...body,kind,operation_id:crypto.randomUUID()};
  savePending(kind,operation);draw();
  try{const result=await api('/api/chat/actions',{method:'POST',body:operation,visitor:kind==='preview'});savePending(kind,null);if(operation.action==='send'&&drafts[kind]===operation.text)drafts[kind]='';scopePanel=false;notify(result.message);}
  catch(e){if(e.definite){savePending(kind,null);notify(e.message,true);}else notify('提交回执待核对。原操作标识已保留，不自动再发模型请求。',true);}
  finally{posting=false;await refresh(true);}
 }
 main.addEventListener('input',e=>{if(e.target.id==='chat-text'){drafts[current]=e.target.value;document.querySelector('#chat-count').textContent=drafts[current].length+'/800';}});
 main.addEventListener('submit',e=>{if(e.target.id!=='chat-form')return;e.preventDefault();e.stopImmediatePropagation();post({action:'send',thread_id:state.thread?.id||null,text:drafts[current]});});
 main.addEventListener('click',async e=>{
  const b=e.target.closest('[data-chat]');if(!b)return;e.preventDefault();e.stopImmediatePropagation();const a=b.dataset.chat;
  if(a==='refresh'){await refresh(true);return;}
  if(a==='example'){drafts[current]=current==='ideal'?'我不主动，但希望朋友主动。':'你那次活动什么时候？';draw();document.querySelector('#chat-text')?.focus();return;}
  if(a==='scope'||a==='cancel-scope'){scopePanel=a==='scope';draw();document.querySelector(a==='scope'?'[data-chat="extract"]':'[data-chat="scope"]')?.focus();return;}
  if(a==='new'){drafts[current]='';await post({action:'new'});return;}
  if(a==='extract'){await post({action:'extract',thread_id:state.thread.id,message_ids:state.extractable.map(m=>m.id)});return;}
  if(a==='check'){await refresh();checked.add(b.dataset.task);draw();return;}
  if(a==='retry'){const task=state.tasks.find(t=>t.id===b.dataset.task);await post({action:'retry',thread_id:state.thread.id,task_id:task.id,uncertain_ack:task.status==='uncertain'&&checked.has(task.id)});return;}
  if(a==='recover'){
   const op=pending[current];if(!op)return;
   try{const result=await api('/api/chat/operations/'+op.operation_id+'?kind='+current,{visitor:current==='preview'});
    if(result.state==='committed'){if(op.action==='send'&&drafts[current]===op.text)drafts[current]='';savePending(current,null);notify('已找到原提交，读取已有任务，不重新生成。');await refresh(true);}
    else{b.dataset.chat='resume';b.textContent='继续同一次提交';notify('未找到已提交记录。可继续同一操作，程序会防止重复建任务。');}
   }catch{notify('仍无法核对，请检查本地服务。',true);}return;
  }
  if(a==='resume')await post(null,true);
 });
 window.addEventListener('hashchange',()=>{generation++;clearTimeout(timer);state=null;scopePanel=false;});
 window.addEventListener('beforeunload',e=>{if(posting||drafts.ideal||drafts.preview){e.preventDefault();e.returnValue='';}});
 return {configure(config){api=config.api;notify=config.notify;},async render(kind){current=kind;state=null;main.innerHTML='<p class="loading">正在恢复已保存的会话…</p>';await refresh(true);},hasDraft(){return !!(drafts.ideal||drafts.preview);}};
})();
