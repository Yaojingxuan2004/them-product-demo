"""Durable local dialogue, explicit extraction, scoped inputs and late-result guards."""
import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from providers import MockProvider, ProviderFailure
from prompts import PROMPT_VERSION, system_prompt


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()




class ChatService:
    def __init__(self, store, problem, provider=None, max_calls=6, background=True):
        self.store, self.Problem = store, problem
        self.provider = provider or MockProvider()
        self.max_calls, self.background = max_calls, background
        self.wake, self.stop = threading.Event(), threading.Event()
        with store.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS chat_threads(id TEXT PRIMARY KEY,kind TEXT NOT NULL,
                status TEXT NOT NULL,scope TEXT NOT NULL,provider TEXT NOT NULL,created TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_messages(id TEXT PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES chat_threads(id),
                speaker TEXT NOT NULL,text TEXT NOT NULL,task_id TEXT,created TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_tasks(id TEXT PRIMARY KEY,thread_id TEXT NOT NULL REFERENCES chat_threads(id),
                kind TEXT NOT NULL,status TEXT NOT NULL,message_id TEXT,request TEXT NOT NULL,result TEXT,
                error TEXT,provider TEXT NOT NULL,attempt_of TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_operations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,response TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS extraction_uses(message_id TEXT PRIMARY KEY,task_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_calls(id TEXT PRIMARY KEY,task_id TEXT UNIQUE,provider TEXT NOT NULL,
                model TEXT NOT NULL,status TEXT NOT NULL,response_id TEXT,usage TEXT,raw_result TEXT,
                created TEXT NOT NULL,updated TEXT NOT NULL);
            ''')
            # Never restart unfinished remote work merely because the service restarted.
            db.execute("UPDATE chat_tasks SET status='uncertain',error='服务已重启，原任务未闭合；请核对记录后再决定是否重试。',updated=? WHERE status IN ('running','queued')", (stamp(),))
        self.worker = None
        if background:
            self.worker = threading.Thread(target=self.loop, daemon=True)
            self.worker.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=1)

    def check_kind(self, kind, principal):
        if kind not in ('ideal', 'preview') or principal != ('owner' if kind == 'ideal' else 'visitor'):
            raise self.Problem(403, '此会话不属于当前使用视角。')

    def memories(self, db, kind):
        where = "status='accepted'" + (" AND public=1 AND role='personal'" if kind == 'preview' else '')
        rows = db.execute('SELECT id,text,role,source_id FROM items WHERE '+where+' ORDER BY id').fetchall()
        # Do not include private source text or private timestamps/counts in preview input.
        return [{'id': r['id'], 'text': r['text'], 'role': r['role'],
                 'recorded_at': db.execute('SELECT created FROM sources WHERE id=?',(r['source_id'],)).fetchone()[0]}
                for r in rows]

    def scope(self, db, kind):
        return digest({'prompt_version': PROMPT_VERSION, 'memories': self.memories(db, kind)})

    def invalidate(self, db):
        for thread in db.execute("SELECT * FROM chat_threads WHERE status='active'").fetchall():
            if thread['scope'] != self.scope(db, thread['kind']) or thread['provider'] != self.provider.mode:
                db.execute("UPDATE chat_threads SET status='stale' WHERE id=?",(thread['id'],))

    def new_thread(self, db, kind):
        db.execute("UPDATE chat_threads SET status='closed' WHERE kind=? AND status='active'",(kind,))
        tid = secrets.token_hex(12)
        db.execute('INSERT INTO chat_threads VALUES(?,?,?,?,?,?)',
                   (tid,kind,'active',self.scope(db,kind),self.provider.mode,stamp()))
        return dict(db.execute('SELECT * FROM chat_threads WHERE id=?',(tid,)).fetchone())

    def operation(self, oid, kind, principal):
        self.check_kind(kind, principal)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM chat_operations WHERE id=? AND kind=?',(oid,kind)).fetchone()
            return {'state':'committed','result':json.loads(row['response'])} if row else {'state':'not_found'}

    def serialize_task(self, task):
        out={k:task[k] for k in ('id','kind','status','message_id','error','provider','created','updated')}
        if task['kind']=='extract' and task['status']=='succeeded':
            out['candidate_count']=json.loads(task['result'])['count']
        return out

    def snapshot(self, kind, principal):
        self.check_kind(kind,principal)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.invalidate(db)
            t=db.execute('SELECT * FROM chat_threads WHERE kind=? ORDER BY created DESC,rowid DESC LIMIT 1',(kind,)).fetchone()
            out={'thread':None,'messages':[],'tasks':[],'extractable':[],
                 'provider':self.provider.mode,'model':self.provider.model,
                 'visible_memories':self.memories(db,kind) if kind=='preview' else []}
            if not t:return out
            out['thread']={k:t[k] for k in ('id','kind','status','created','provider')}
            # Stale preview history is accessible only via the separate owner history API.
            if t['status']!='active' and kind=='preview':return out
            out['messages']=[dict(m) for m in db.execute('SELECT id,speaker,text,created FROM chat_messages WHERE thread_id=? ORDER BY rowid',(t['id'],))]
            out['tasks']=[self.serialize_task(x) for x in db.execute('SELECT * FROM chat_tasks WHERE thread_id=? ORDER BY rowid',(t['id'],))]
            if kind=='ideal' and t['status']=='active':
                out['extractable']=[dict(m) for m in db.execute("SELECT id,text,created FROM chat_messages WHERE thread_id=? AND speaker='user' AND id NOT IN (SELECT message_id FROM extraction_uses) ORDER BY rowid LIMIT 4",(t['id'],))]
            return out

    def history(self, principal):
        if principal!='owner':raise self.Problem(403,'历史仅供本机资料主人回看。')
        with self.store.connect() as db:
            rows=[]
            for t in db.execute('SELECT id,kind,status,provider,created FROM chat_threads ORDER BY rowid DESC LIMIT 10'):
                row=dict(t)
                row['messages']=[dict(m) for m in db.execute('SELECT speaker,text,created FROM chat_messages WHERE thread_id=? ORDER BY rowid',(t['id'],))]
                rows.append(row)
            return {'threads':rows,'note':'最近10个会话仅供本人回看，不自动进入新会话。'}

    def request_for(self, db, thread, message=None, sources=None):
        memories=self.memories(db,thread['kind'])
        if sources is not None:
            data={'task':'extract','prompt_version':PROMPT_VERSION,'sources':sources,'existing_memories':memories,
              'context':[dict(m) for m in db.execute('SELECT speaker,text FROM chat_messages WHERE thread_id=? ORDER BY rowid DESC LIMIT 12',(thread['id'],))][::-1]}
            return {'messages':[{'role':'system','content':system_prompt(thread['kind'], extracting=True)},{'role':'user','content':encode(data)}], 'max_tokens':1200}
        messages=[{'role':'system','content':system_prompt(thread['kind'])}]
        # Only completed pairs are reused. Failed input is retained in UI but not silently retried.
        pairs=db.execute("SELECT u.text AS question,a.text AS answer FROM chat_messages a JOIN chat_tasks t ON a.task_id=t.id JOIN chat_messages u ON u.id=t.message_id WHERE t.thread_id=? AND t.kind='reply' AND t.status='succeeded' AND a.speaker='assistant' ORDER BY a.rowid DESC LIMIT 5",(thread['id'],)).fetchall()
        for pair in reversed(pairs):messages.extend([{'role':'user','content':pair['question']},{'role':'assistant','content':pair['answer']}])
        data={'task':'reply','prompt_version':PROMPT_VERSION,'role':thread['kind'],'display_name':'演示人物','today':stamp()[:10],
              'personal_memories':[m for m in memories if m['role']=='personal'],
              'friend_expectations':[m for m in memories if m['role']=='ideal'], 'question':message}
        # Preview has no private section, including no private count or internal version counter.
        if thread['kind']=='preview':data.pop('friend_expectations')
        messages.append({'role':'user','content':encode(data)})
        return {'messages':messages,'max_tokens':600}

    def submit(self, body, principal):
        if not isinstance(body,dict):raise self.Problem(400,'请求格式错误。')
        kind=body.get('kind');self.check_kind(kind,principal)
        oid=body.get('operation_id')
        if not isinstance(oid,str) or not 16<=len(oid)<=80:raise self.Problem(400,'缺少操作标识。')
        fingerprint=digest(body)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.invalidate(db)
            old=db.execute('SELECT * FROM chat_operations WHERE id=?',(oid,)).fetchone()
            if old:
                if old['kind']!=kind or old['fingerprint']!=fingerprint:raise self.Problem(409,'同一操作标识内容不同。')
                return json.loads(old['response'])
            action=body.get('action')
            if action not in ('new','send','extract','retry'):raise self.Problem(400,'不支持的会话操作。')
            tid=body.get('thread_id')
            if tid is not None and not isinstance(tid,str):raise self.Problem(400,'会话标识无效。')
            if action=='new':
                thread=self.new_thread(db,kind)
                result={'thread_id':thread['id'],'message':'已开启新会话，不携带旧消息。'}
            else:
                thread=db.execute('SELECT * FROM chat_threads WHERE id=? AND kind=?',(tid,kind)).fetchone() if tid else None
                if not thread:
                    if action!='send' or tid:raise self.Problem(404,'会话不存在，请重新打开。')
                    existing=db.execute('SELECT id FROM chat_threads WHERE kind=? LIMIT 1',(kind,)).fetchone()
                    if existing:raise self.Problem(409,'已有会话，请刷新页面或明确开启新会话。')
                    thread=self.new_thread(db,kind)
                if thread['status']!='active':raise self.Problem(409,'资料或用途已变化，请开启新会话后重新表达。')
                task_kind='extract' if action=='extract' else 'reply'
                prior=None
                if action=='retry':
                    prior=db.execute('SELECT * FROM chat_tasks WHERE id=? AND thread_id=?',(body.get('task_id'),thread['id'])).fetchone()
                    if not prior or prior['status'] not in ('failed','uncertain'):raise self.Problem(409,'这次任务不能重试，请核对最新状态。')
                    if prior['status']=='uncertain' and body.get('uncertain_ack') is not True:raise self.Problem(409,'请先核对，并确认重新尝试可能产生新的调用。')
                    if db.execute('SELECT id FROM chat_tasks WHERE attempt_of=?',(prior['id'],)).fetchone():raise self.Problem(409,'已经为这次任务建立重试，请查看最新任务。')
                    task_kind=prior['kind']
                    if task_kind=='reply' and db.execute("SELECT id FROM chat_tasks WHERE thread_id=? AND kind='reply' AND created>? AND message_id!=? LIMIT 1",(thread['id'],prior['created'],prior['message_id'])).fetchone():
                        raise self.Problem(409,'之后已有新的对话消息。请在新会话重新提出旧问题，避免错接回答。')
                if task_kind=='extract' and kind!='ideal':raise self.Problem(403,'访客发言不能整理为主人记忆。')
                # A failed reply needs an explicit retry or a new session; no orphaned user turns.
                unresolved=db.execute("SELECT id FROM chat_tasks WHERE thread_id=? AND kind=? AND status IN ('queued','running','uncertain')",(thread['id'],task_kind)).fetchall()
                if any(not prior or x['id']!=prior['id'] for x in unresolved):raise self.Problem(409,'已有任务等待完成或核对，请勿重复发送。')
                task_id=secrets.token_hex(12);message_id=None
                if prior:
                    request=json.loads(prior['request']);message_id=prior['message_id']
                elif task_kind=='reply':
                    text=body.get('text')
                    if not isinstance(text,str) or not 1<=len(text.strip())<=800:raise self.Problem(400,'请输入1—800字符，草稿不会被截断。')
                    if kind=='preview' and not self.memories(db,kind):raise self.Problem(409,'目前暂无可用于介绍本人的资料。')
                    message_id=secrets.token_hex(12)
                    db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?)',(message_id,thread['id'],'user',text.strip(),None,stamp()))
                    request=self.request_for(db,thread,message=text.strip())
                else:
                    selected=body.get('message_ids')
                    rows=[dict(m) for m in db.execute("SELECT id,text,created FROM chat_messages WHERE thread_id=? AND speaker='user' AND id NOT IN (SELECT message_id FROM extraction_uses) ORDER BY rowid LIMIT 4",(thread['id'],))]
                    if not rows or selected!=[r['id'] for r in rows]:raise self.Problem(409,'待整理范围已变化或为空，请刷新后核对。')
                    request=self.request_for(db,thread,sources=rows)
                db.execute('INSERT INTO chat_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (task_id,thread['id'],task_kind,'queued',message_id,encode(request),None,None,self.provider.mode,prior['id'] if prior else None,stamp(),stamp()))
                if prior:
                    db.execute("UPDATE chat_tasks SET status='retried',updated=? WHERE id=?",(stamp(),prior['id']))
                if task_kind=='extract':
                    for src in json.loads(request['messages'][-1]['content'])['sources']:
                        db.execute('INSERT INTO extraction_uses VALUES(?,?) ON CONFLICT(message_id) DO UPDATE SET task_id=excluded.task_id',(src['id'],task_id))
                result={'thread_id':thread['id'],'task_id':task_id,'message':'输入与任务已保存，最新处理状态见下方。'}
            db.execute('INSERT INTO chat_operations VALUES(?,?,?,?)',(oid,fingerprint,kind,encode(result)))
        self.wake.set()
        return result

    def loop(self):
        while not self.stop.is_set():
            self.wake.wait(1);self.wake.clear()
            while not self.stop.is_set() and self.run_next():pass

    def run_next(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.invalidate(db)
            task=db.execute("SELECT * FROM chat_tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not task:return False
            task=dict(task);thread=db.execute('SELECT * FROM chat_threads WHERE id=?',(task['thread_id'],)).fetchone()
            if thread['status']!='active':
                db.execute("UPDATE chat_tasks SET status='stale',error='资料或会话已变化，本任务未发出。',updated=? WHERE id=?",(stamp(),task['id']));return True
            if self.provider.mode!='mock' and db.execute("SELECT count(*) FROM chat_calls WHERE provider!='mock'").fetchone()[0]>=self.max_calls:
                db.execute("UPDATE chat_tasks SET status='failed',error='已达到本演示库真实调用尝试上限，未发出新请求。',updated=? WHERE id=?",(stamp(),task['id']));return True
            call_id=secrets.token_hex(12)
            db.execute("UPDATE chat_tasks SET status='running',updated=? WHERE id=?",(stamp(),task['id']))
            db.execute('INSERT INTO chat_calls VALUES(?,?,?,?,?,?,?,?,?,?)',(call_id,task['id'],self.provider.mode,self.provider.model,'started',None,None,None,stamp(),stamp()))
        try:
            response=self.provider.invoke(json.loads(task['request']))
        except ProviderFailure as error:
            self.fail(task['id'],call_id,error.state,error.message);return True
        except Exception:
            self.fail(task['id'],call_id,'uncertain','处理未完成，原始任务保留，请核对；未自动重试。');return True
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');self.invalidate(db)
                db.execute("UPDATE chat_calls SET status='returned',response_id=?,usage=?,raw_result=?,updated=? WHERE id=?",
                           (response.get('response_id'),encode(response.get('usage')),encode(response.get('result')),stamp(),call_id))
                thread=db.execute('SELECT * FROM chat_threads WHERE id=?',(task['thread_id'],)).fetchone()
                if thread['status']!='active':
                    db.execute("UPDATE chat_tasks SET status='stale',result=?,error='资料或会话已变化，晚到结果未发布到当前会话。',updated=? WHERE id=?",(encode(response.get('result')),stamp(),task['id']));return True
                # Save raw returned evidence even when semantic structure fails validation.
                db.execute('SAVEPOINT output_validation')
                try:
                    result=response['result']
                    if isinstance(result,str):result=json.loads(result)
                    if response.get('finish_reason')!='stop':raise ValueError('输出未完整结束。')
                    checked=self.accept_result(db,task,result)
                except (ValueError,KeyError,TypeError) as error:
                    db.execute('ROLLBACK TO output_validation')
                    db.execute("UPDATE chat_tasks SET status='failed',error=?,updated=? WHERE id=?",('结果未通过结构或来源检查，原返回已保留供核对。',stamp(),task['id']))
                else:
                    db.execute("UPDATE chat_tasks SET status='succeeded',result=?,updated=? WHERE id=?",(encode(checked),stamp(),task['id']))
        except Exception:
            self.fail(task['id'],call_id,'uncertain','收到返回但保存未完成，请核对任务记录；未自动重试。')
        return True

    def fail(self, tid, cid, state, message):
        with self.store.connect() as db:
            db.execute('UPDATE chat_tasks SET status=?,error=?,updated=? WHERE id=?',(state,message,stamp(),tid))
            db.execute('UPDATE chat_calls SET status=?,updated=? WHERE id=?',(state,stamp(),cid))

    def accept_result(self, db, task, result):
        if not isinstance(result,dict):raise ValueError('对象错误')
        data=json.loads(json.loads(task['request'])['messages'][-1]['content'])
        if task['kind']=='reply':
            if set(result)!={'answer','memory_ids'}:raise ValueError('字段错误')
            if not isinstance(result['answer'],str) or not 1<=len(result['answer'].strip())<=4000:raise ValueError('回答长度错误')
            allowed={m['id'] for m in data['personal_memories']+data.get('friend_expectations',[])}
            refs=result['memory_ids']
            if not isinstance(refs,list) or any(not isinstance(x,str) or x not in allowed for x in refs):raise ValueError('来源越界')
            db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?)',(secrets.token_hex(12),task['thread_id'],'assistant',result['answer'].strip(),task['id'],stamp()))
            return result
        if set(result)!={'candidates'} or not isinstance(result['candidates'],list) or len(result['candidates'])>12:raise ValueError('候选格式错误')
        if db.execute("SELECT count(*) FROM items WHERE status='pending'").fetchone()[0]+len(result['candidates'])>40:raise ValueError('候选超额')
        source_map={s['id']:s for s in data['sources']};seen=set();ids=[];sources={}
        for row in result['candidates']:
            if not isinstance(row,dict) or set(row)!={'message_id','role','quote'}:raise ValueError('候选字段错误')
            mid,role,quote=row['message_id'],row['role'],row['quote']
            if not isinstance(mid,str) or mid not in source_map or role not in ('personal','ideal'):raise ValueError('归属错误')
            if not isinstance(quote,str) or not 1<=len(quote)<=300 or quote not in source_map[mid]['text']:raise ValueError('引文不匹配或过长')
            key=(mid,role,quote)
            if key in seen:raise ValueError('重复候选')
            seen.add(key)
            if mid not in sources:
                sid=secrets.token_hex(12);sources[mid]=sid
                source=source_map[mid]
                source_kind='mock_chat_self_report' if task['provider']=='mock' else 'chat_self_report'
                db.execute('INSERT INTO sources VALUES(?,?,?,?)',(sid,source['text'],source_kind,source['created']))
            iid=secrets.token_hex(12)
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',(iid,quote,role,'pending',0,1,sources[mid],stamp(),stamp()))
            self.store.audit(db,iid,'create');ids.append(iid)
        return {'item_ids':ids,'count':len(ids),'note':'候选仍需逐条确认与核对含义、否定及跨来源冲突。'}
