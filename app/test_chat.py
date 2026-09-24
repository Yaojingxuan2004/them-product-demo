"""B-stage lifecycle and provider-contract tests; all outputs synthetic, zero paid calls."""
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from server import make_server, Problem
from providers import MockProvider, DeepSeekProvider, ProviderFailure
from chat_service import ChatService


class FixtureProvider(MockProvider):
    def __init__(self, reply=None, failure=None):
        self.reply,self.failure,self.calls=reply,failure,[]
    def invoke(self, request):
        self.calls.append(request)
        if self.failure:raise ProviderFailure(*self.failure)
        return {'result':self.reply,'response_id':'synthetic-response','usage':{'prompt_tokens':10},'finish_reason':'stop'} if self.reply is not None else super().invoke(request)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'chat.sqlite3'
        self.provider=FixtureProvider();self.server=make_server(self.path,0,self.provider,background=False)
        self.chat,self.store=self.server.chat,self.server.store
    def tearDown(self):
        self.server.server_close();self.temp.cleanup()
    def op(self,action,kind='ideal',**fields):
        return self.chat.submit(dict(action=action,kind=kind,operation_id=str(uuid.uuid4()),**fields),'owner' if kind=='ideal' else 'visitor')
    def state(self,kind='ideal'):return self.chat.snapshot(kind,'owner' if kind=='ideal' else 'visitor')
    def send(self,text='我不主动，但希望朋友主动。',kind='ideal'):
        t=self.state(kind)['thread']
        return self.op('send',kind,thread_id=t['id'] if t else None,text=text)
    def process(self):
        while self.chat.run_next():pass
    def memory(self,text='我参加过合唱演出。',role='personal',public=False):
        result=self.store.mutate(dict(action='create',entries=[{'text':text,'role':role}],operation_id=str(uuid.uuid4())))
        iid=result['item_ids'][0];self.change('confirm',iid)
        if public:self.change('allow',iid,confirmed_text=text)
        return iid
    def change(self,action,iid,**kw):
        item=next(i for i in self.store.snapshot()['items'] if i['id']==iid)
        return self.store.mutate(dict(action=action,item_id=iid,expected_version=item['version'],operation_id=str(uuid.uuid4()),**kw))
    def query(self,sql):
        with self.store.connect() as db:return [dict(r) for r in db.execute(sql)]
    def extract(self):
        state=self.state();return self.op('extract',thread_id=state['thread']['id'],message_ids=[m['id'] for m in state['extractable']])

    def test_duplicate_send_and_receipt_only_one_message_call(self):
        body=dict(action='send',kind='ideal',text='你好',operation_id=str(uuid.uuid4()))
        a=self.chat.submit(body,'owner');b=self.chat.submit(body,'owner');self.assertEqual(a,b)
        self.assertEqual(self.chat.operation(body['operation_id'],'ideal','owner')['state'],'committed')
        self.process();self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.state()['messages']),2)
        self.assertEqual(len(self.query('SELECT * FROM chat_calls')),1)

    def test_refresh_and_service_reopen_do_not_regenerate(self):
        self.send();self.process();self.state();self.state()
        reopened=ChatService(self.store,Problem,self.provider,background=False)
        self.assertEqual(len(reopened.snapshot('ideal','owner')['messages']),2)
        self.assertFalse(reopened.run_next());self.assertEqual(len(self.provider.calls),1)

    def test_restart_queued_is_uncertain_not_automatically_executed(self):
        self.send();reopened=ChatService(self.store,Problem,self.provider,background=False)
        task=reopened.snapshot('ideal','owner')['tasks'][0]
        self.assertEqual(task['status'],'uncertain');self.assertFalse(reopened.run_next());self.assertEqual(self.provider.calls,[])

    def test_preview_input_has_no_private_expectation_source(self):
        self.memory('PRIVATE_MARKER');self.memory('IDEAL_MARKER','ideal');self.memory('公开合唱',public=True)
        self.send('介绍一下',kind='preview');self.process()
        raw=json.dumps(self.provider.calls[-1],ensure_ascii=False)
        self.assertNotIn('PRIVATE_MARKER',raw);self.assertNotIn('IDEAL_MARKER',raw);self.assertNotIn('source_text',raw)
        self.assertNotIn('friend_expectations',raw);self.assertIn('公开合唱',raw)

    def test_ideal_composes_personal_expectation_and_current(self):
        self.memory('本人慢热');self.memory('期待主动','ideal');self.send('当前问题');self.process()
        data=json.loads(self.provider.calls[-1]['messages'][-1]['content'])
        self.assertEqual(data['personal_memories'][0]['text'],'本人慢热')
        self.assertEqual(data['friend_expectations'][0]['text'],'期待主动');self.assertEqual(data['question'],'当前问题')

    def test_empty_preview_rejected_ideal_starts(self):
        with self.assertRaises(Problem):self.send('你好','preview')
        self.assertEqual(self.query('SELECT * FROM chat_tasks'),[])
        self.send();self.process();self.assertEqual(self.state()['tasks'][0]['status'],'succeeded')

    def test_confirmation_invalidates_ideal_but_private_not_preview(self):
        self.memory(public=True);self.send('你好','preview');self.process();self.send();self.process()
        self.memory('新的私人记忆')
        self.assertEqual(self.state('preview')['thread']['status'],'active')
        self.assertEqual(self.state()['thread']['status'],'stale')

    def test_revoke_blocks_late_reply_and_old_preview_history(self):
        iid=self.memory(public=True);self.send('介绍','preview')
        original=self.provider.invoke
        def during(request):
            self.change('revoke',iid)
            return original(request)
        self.provider.invoke=during;self.process()
        self.assertEqual(self.query('SELECT status FROM chat_tasks')[0]['status'],'stale')
        self.assertEqual(self.state('preview')['messages'],[])
        self.assertEqual(self.query('SELECT status FROM chat_calls')[0]['status'],'returned')
        with self.assertRaises(Problem):self.chat.history('visitor')
        self.assertEqual(len(self.chat.history('owner')['threads'][0]['messages']),1)

    def test_new_thread_context_excludes_old_conversation(self):
        self.send('旧的秘密表达');self.process();self.op('new');self.send('新的问题');self.process()
        self.assertNotIn('旧的秘密表达',json.dumps(self.provider.calls[-1],ensure_ascii=False))

    def test_only_recent_five_complete_pairs(self):
        for i in range(7):self.send(f'第{i}句');self.process()
        request=self.provider.calls[-1]
        self.assertEqual(len(request['messages']),12)
        previous=[m['content'] for m in request['messages'][1:-1] if m['role']=='user']
        self.assertEqual(previous,[f'第{i}句' for i in range(1,6)])

    def test_clarification_has_prior_question_and_reply(self):
        self.memory(public=True);self.send('你那次活动什么时候？','preview');self.process();self.send('合唱演出那次','preview');self.process()
        request=self.provider.calls[-1]
        self.assertEqual(request['messages'][1]['content'],'你那次活动什么时候？')
        self.assertIn('哪一次活动',request['messages'][2]['content'])
        self.assertIn('模拟流程',self.state('preview')['messages'][-1]['text'])

    def test_extraction_fixed_range_and_independent_reply(self):
        self.send();self.process();task=self.extract();self.send('后来补充的消息')
        self.process();items=self.store.snapshot()['items']
        self.assertEqual(len(items),2);self.assertEqual({i['role'] for i in items},{'ideal','personal'})
        self.assertTrue(all(i['status']=='pending' and not i['public'] for i in items))
        self.assertEqual(len(self.state()['extractable']),1)
        self.assertEqual(self.state()['extractable'][0]['text'],'后来补充的消息')

    def test_old_extraction_range_cannot_recreate_ignored_items(self):
        self.send();self.process();state=self.state();body=dict(action='extract',kind='ideal',thread_id=state['thread']['id'],message_ids=[m['id'] for m in state['extractable']],operation_id=str(uuid.uuid4()))
        self.chat.submit(body,'owner');self.process();items=self.store.snapshot()['items'];self.change('ignore',items[0]['id'])
        self.chat.submit(body,'owner');self.process();self.assertEqual(len(self.store.snapshot()['items']),2)
        body['operation_id']=str(uuid.uuid4())
        with self.assertRaises(Problem):self.chat.submit(body,'owner')

    def test_failed_extraction_does_not_remove_reply_or_mark_success(self):
        self.send();self.process();self.extract();self.provider.failure=('failed','合成失败');self.process()
        self.assertEqual(len(self.state()['messages']),2);self.assertEqual(self.state()['tasks'][-1]['status'],'failed')
        self.assertEqual(self.store.snapshot()['items'],[])

    def test_bad_second_quote_rolls_back_all_candidates_keeps_raw(self):
        self.send();self.process();state=self.state();mid=state['extractable'][0]['id'];self.extract()
        self.provider.reply={'candidates':[{'message_id':mid,'role':'personal','quote':'我不主动'},{'message_id':mid,'role':'ideal','quote':'并非原文'}]}
        self.process();self.assertEqual(self.store.snapshot()['items'],[])
        self.assertEqual(self.state()['tasks'][-1]['status'],'failed')
        self.assertIn('并非原文',self.query('SELECT raw_result FROM chat_calls ORDER BY rowid DESC LIMIT 1')[0]['raw_result'])

    def test_empty_extraction_is_success_without_fabrication(self):
        self.send('天气话题');self.process();self.extract();self.process()
        self.assertEqual(self.store.snapshot()['items'],[]);self.assertEqual(self.state()['tasks'][-1]['status'],'succeeded')

    def test_uncertain_retry_requires_ack_and_no_duplicate_user(self):
        self.provider.failure=('uncertain','合成中断');self.send();self.process();state=self.state();task=state['tasks'][0]
        with self.assertRaises(Problem):self.op('retry',thread_id=state['thread']['id'],task_id=task['id'])
        self.provider.failure=None;self.op('retry',thread_id=state['thread']['id'],task_id=task['id'],uncertain_ack=True);self.process()
        self.assertEqual(len([m for m in self.state()['messages'] if m['speaker']=='user']),1)
        self.assertEqual(len(self.provider.calls),2)
        self.send('重试成功后继续');self.process()
        self.assertEqual(self.state()['tasks'][-1]['status'],'succeeded')

    def test_principal_and_visitor_extraction_denied(self):
        with self.assertRaises(Problem):self.chat.snapshot('ideal','visitor')
        with self.assertRaises(Problem):self.chat.submit(dict(kind='ideal',action='send',text='hi',operation_id=str(uuid.uuid4())),'visitor')
        self.memory(public=True);self.send('我想教给你本人经历','preview');self.process()
        with self.assertRaises(Problem):self.op('extract','preview',thread_id=self.state('preview')['thread']['id'],message_ids=[])

    def test_source_reference_outside_request_rejected(self):
        self.provider.reply={'answer':'无依据的回答','memory_ids':['private-made-up-id']}
        self.send();self.process();self.assertEqual(self.state()['tasks'][0]['status'],'failed')
        self.assertEqual(len(self.state()['messages']),1)

    def test_real_attempt_quota_counts_failures_and_survives_restart(self):
        self.provider.mode='fixture-live';self.chat.max_calls=1;self.provider.failure=('failed','合成服务失败')
        self.send();self.process()
        self.chat=ChatService(self.store,Problem,self.provider,max_calls=1,background=False)
        state=self.state();self.op('retry',thread_id=state['thread']['id'],task_id=state['tasks'][0]['id']);self.process()
        self.assertEqual(len(self.provider.calls),1);self.assertIn('上限',self.state()['tasks'][-1]['error'])

    def test_remote_adapter_contract_with_mocked_transport(self):
        response={'id':'fixture','usage':{'prompt_tokens':2},'choices':[{'message':{'content':'{"answer":"测试","memory_ids":[]}'},'finish_reason':'stop'}]}
        class Transport:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,size):return json.dumps(response).encode()
        with patch('providers.urlopen',return_value=Transport()) as transport:
            result=DeepSeekProvider('synthetic-placeholder','explicit-test-model').invoke({'messages':[{'role':'user','content':'JSON'}],'max_tokens':600})
        self.assertEqual(transport.call_count,1);self.assertEqual(result['response_id'],'fixture')
        request=transport.call_args.args[0];payload=json.loads(request.data)
        self.assertEqual(payload['response_format'],{'type':'json_object'});self.assertNotIn('synthetic-placeholder',json.dumps(payload))
        self.assertEqual(payload['thinking'],{'type':'disabled'})


if __name__=='__main__':unittest.main()
