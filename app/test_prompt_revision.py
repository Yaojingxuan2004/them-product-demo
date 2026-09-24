"""Offline input/version integration only; never evaluates model semantics."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from server import make_server, Problem
from chat_service import digest
from prompts import PROMPT_VERSION
from providers import MockProvider


class PromptRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.tmp.name)/'test.sqlite3', 0, background=False)
        self.chat, self.store = self.server.chat, self.server.store

    def tearDown(self):
        self.server.server_close()
        self.tmp.cleanup()

    def send(self, text):
        return self.chat.submit({'action':'send', 'kind':'ideal', 'text':text,
                                 'operation_id':str(uuid.uuid4())}, 'owner')

    def test_current_statement_is_sent_without_creating_memory(self):
        result = self.send('我刚参加完一次读书会。')
        self.chat.run_next()
        with self.store.connect() as db:
            task = db.execute('SELECT request FROM chat_tasks WHERE id=?', (result['task_id'],)).fetchone()
        request = json.loads(task['request'])
        data = json.loads(request['messages'][-1]['content'])
        self.assertEqual(data['question'], '我刚参加完一次读书会。')
        self.assertEqual(data['personal_memories'], [])
        self.assertEqual(data['prompt_version'], PROMPT_VERSION)
        self.assertEqual(self.store.snapshot()['items'], [])
        self.assertEqual(len(self.chat.snapshot('ideal', 'owner')['messages']), 2)

    def test_old_scope_rejects_send_but_keeps_history_and_calls(self):
        result = self.send('旧版本会话')
        self.chat.run_next()
        with self.store.connect() as db:
            db.execute('UPDATE chat_threads SET scope=?', (digest([]),))
        state = self.chat.snapshot('ideal', 'owner')
        self.assertEqual(state['thread']['status'], 'stale')
        self.assertEqual(len(state['messages']), 2)
        with self.assertRaises(Problem):
            self.chat.submit({'action':'send','kind':'ideal','thread_id':result['thread_id'],
                              'text':'不能接着发','operation_id':str(uuid.uuid4())}, 'owner')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chat_calls').fetchone()[0], 1)

    def test_old_scope_queued_request_never_reaches_provider(self):
        self.send('待发的旧规则请求')
        with self.store.connect() as db:
            db.execute('UPDATE chat_threads SET scope=?', (digest([]),))
        self.chat.run_next()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM chat_tasks').fetchone()[0], 'stale')
            self.assertEqual(db.execute('SELECT count(*) FROM chat_calls').fetchone()[0], 0)

    def test_adjacent_split_response_preserves_source_and_private_candidates(self):
        # This supplies a reviewed fixture; it does NOT prove a model can produce it.
        original = '我喜欢安静，但希望朋友愿意分享近况。'
        self.send(original)
        self.chat.run_next()
        state = self.chat.snapshot('ideal', 'owner')
        mid = state['extractable'][0]['id']
        self.chat.submit({'action':'extract','kind':'ideal','thread_id':state['thread']['id'],
                          'message_ids':[mid],'operation_id':str(uuid.uuid4())}, 'owner')
        captured = []
        class ReviewedFixture(MockProvider):
            def invoke(self, request):
                captured.append(request)
                return {'result':{'candidates':[
                    {'message_id':mid,'role':'personal','quote':'我喜欢安静'},
                    {'message_id':mid,'role':'ideal','quote':'希望朋友愿意分享近况'}]},
                    'finish_reason':'stop','response_id':None,'usage':None}
        self.chat.provider = ReviewedFixture()
        self.chat.run_next()
        payload = json.loads(captured[0]['messages'][-1]['content'])
        self.assertEqual(payload['prompt_version'], PROMPT_VERSION)
        self.assertEqual([s['text'] for s in payload['sources']], [original])
        self.assertTrue(any(m['speaker']=='assistant' for m in payload['context']))
        items = self.store.snapshot()['items']
        self.assertEqual({(i['text'],i['role']) for i in items},
                         {('我喜欢安静','personal'),('希望朋友愿意分享近况','ideal')})
        self.assertTrue(all(i['status']=='pending' and not i['public'] for i in items))
        self.assertTrue(all(i['source_text']==original for i in items))


if __name__ == '__main__':
    unittest.main()
