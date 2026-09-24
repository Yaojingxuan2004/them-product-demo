"""State/HTTP checks only. No model calls, user testing or semantic evaluation."""
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from server import make_server, Store


class MemoryAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.sqlite3'
        self.server = make_server(self.path, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.tokens = self.request('/api/bootstrap')[1]['sessions']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, role='owner', extra=None):
        headers={'Content-Type':'application/json'}
        if hasattr(self,'tokens'): headers['X-Session']=self.tokens[role]
        headers.update(extra or {})
        req=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urlopen(req,timeout=5) as res: return res.status,json.load(res)
        except HTTPError as e: return e.code,json.load(e)

    def action(self, action, **kwargs):
        return self.request('/api/actions',{'action':action,'operation_id':str(uuid.uuid4()),**kwargs})

    def state(self): return self.request('/api/state')[1]['items']
    def visible(self): return self.request('/api/visible',role='visitor')[1]['items']
    def create(self, text='我喜欢合唱。', role='personal'):
        status,result=self.action('create',entries=[{'text':text,'role':role}]); self.assertEqual(status,200)
        return result['item_ids'][0]
    def current(self, iid): return next(x for x in self.state() if x['id']==iid)
    def change(self, action, iid, **kwargs):
        return self.action(action,item_id=iid,expected_version=self.current(iid)['version'],**kwargs)

    def test_pending_confirm_default_private_and_persistence(self):
        iid=self.create(); self.assertEqual(self.visible(),[])
        self.assertEqual(self.change('confirm',iid)[0],200)
        self.assertEqual(self.visible(),[])
        self.assertEqual(Store(self.path).snapshot()['items'][0]['status'],'accepted')

    def test_individual_confirmation_preserves_siblings(self):
        _,result=self.action('create',entries=[{'text':'我慢热。','role':'personal'},{'text':'希望朋友主动。','role':'ideal'}])
        a,b=result['item_ids']; self.change('confirm',a); self.change('confirm',b)
        self.assertEqual(len([i for i in self.state() if i['status']=='accepted']),2)
        self.assertEqual(self.current(a)['source_id'],self.current(b)['source_id'])

    def test_ideal_cannot_be_public_at_api(self):
        iid=self.create('我希望朋友尊重安静。','ideal'); self.change('confirm',iid)
        self.assertEqual(self.change('allow',iid,confirmed_text=self.current(iid)['text'])[0],403)
        self.assertEqual(self.visible(),[])

    def test_public_exact_text_and_no_private_source_leak(self):
        _,result=self.action('create',entries=[{'text':'可展示的合唱。','role':'personal'},{'text':'私人自述不会公开。','role':'personal'}])
        a,b=result['item_ids']; self.change('confirm',a); self.change('confirm',b)
        self.assertEqual(self.change('allow',a,confirmed_text='不匹配的正文')[0],409)
        self.change('allow',a,confirmed_text=self.current(a)['text'])
        output=self.request('/api/visible',role='visitor')[1]
        self.assertEqual(len(output['items']),1)
        self.assertNotIn('私人自述',json.dumps(output,ensure_ascii=False))
        self.assertNotIn('source_text',output['items'][0])

    def test_edit_creates_new_source_and_revokes_public(self):
        iid=self.create(); original=self.current(iid)['source_id']; self.change('confirm',iid)
        self.change('allow',iid,confirmed_text=self.current(iid)['text'])
        self.change('edit',iid,text='我暂时不参加合唱。',role='personal',meaning_confirmed=True)
        item=self.current(iid)
        self.assertNotEqual(item['source_id'],original); self.assertEqual(self.visible(),[])
        with self.server.store.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM sources WHERE id=?',(original,)).fetchone()[0],'我喜欢合唱。')
        self.assertTrue(any(h['snapshot']['text']=='我喜欢合唱。' for h in item['history']))

    def test_pending_edit_confirm_and_role_ack(self):
        iid=self.create('希望朋友主动。','ideal')
        self.assertEqual(self.change('edit',iid,text='我会主动联系朋友。',role='personal')[0],400)
        self.assertEqual(self.current(iid)['status'],'pending')
        self.assertEqual(self.change('edit',iid,text='我会主动联系朋友。',role='personal',meaning_confirmed=True)[0],200)
        self.assertEqual(self.current(iid)['status'],'accepted')

    def test_stale_write_rejected(self):
        iid=self.create(); self.change('confirm',iid)
        self.assertEqual(self.action('ignore',item_id=iid,expected_version=1)[0],409)
        self.assertEqual(self.current(iid)['status'],'accepted')

    def test_idempotent_create_and_reuse_conflict(self):
        op={'operation_id':str(uuid.uuid4()),'action':'create','entries':[{'text':'同一条提交。','role':'personal'}]}
        first=self.request('/api/actions',op); second=self.request('/api/actions',op)
        self.assertEqual(first,second); self.assertEqual(len(self.state()),1)
        op['entries'][0]['text']='另一个内容。'
        self.assertEqual(self.request('/api/actions',op)[0],409)

    def test_lost_ack_query_and_same_operation(self):
        iid=self.create(); op={'operation_id':str(uuid.uuid4()),'action':'confirm','item_id':iid,'expected_version':1}
        # Commit without consuming its HTTP response: emulate lost acknowledgement.
        self.server.store.mutate(op)
        state=self.request('/api/operations/'+op['operation_id'])[1]
        self.assertEqual(state['state'],'committed')
        self.assertEqual(self.request('/api/actions',op)[0],200)
        self.assertEqual(self.current(iid)['version'],2)
        self.assertEqual(len(self.current(iid)['history']),2)

    def test_ignore_disable_do_not_restore_on_old_operation(self):
        op={'operation_id':str(uuid.uuid4()),'action':'create','entries':[{'text':'旧来源。','role':'personal'}]}
        iid=self.request('/api/actions',op)[1]['item_ids'][0]; self.change('ignore',iid)
        self.request('/api/actions',op); self.assertEqual(self.current(iid)['status'],'ignored')
        j=self.create(); self.change('confirm',j); self.change('disable',j)
        self.assertEqual(self.change('confirm',j)[0],409)
        self.assertEqual(self.visible(),[])

    def test_revoke_changes_public_snapshot(self):
        iid=self.create(); self.change('confirm',iid); self.change('allow',iid,confirmed_text=self.current(iid)['text'])
        old=self.request('/api/visible',role='visitor')[1]
        self.change('revoke',iid); new=self.request('/api/visible',role='visitor')[1]
        self.assertGreater(new['revision'],old['revision']); self.assertEqual(new['items'],[])
        self.assertEqual(self.current(iid)['status'],'accepted')

    def test_visitor_denied_owner_data_mutation_and_operation_receipt(self):
        iid=self.create('不该进入访客接口的内容。')
        for path in ['/api/state','/api/operations/anything']:
            code,data=self.request(path,role='visitor');self.assertEqual(code,403);self.assertNotIn('items',data)
        code,_=self.request('/api/actions',{'action':'confirm','operation_id':str(uuid.uuid4()),'item_id':iid,'expected_version':1},role='visitor')
        self.assertEqual(code,403)

    def test_transaction_rollback_invalid_second_entry(self):
        code,_=self.action('create',entries=[{'text':'第一条','role':'personal'},{'text':'第二条','role':'invalid'}])
        self.assertEqual(code,400);self.assertEqual(self.state(),[])

    def test_capacity_rejects_without_evicting(self):
        for i in range(20):
            iid=self.create(f'合成自述{i}');self.change('confirm',iid)
        iid=self.create('第二十一条。');self.assertEqual(self.change('confirm',iid)[0],409)
        self.assertEqual(self.current(iid)['status'],'pending')
        self.assertEqual(sum(i['status']=='accepted' for i in self.state()),20)

    def test_cross_origin_request_and_host_denied(self):
        self.assertEqual(self.request('/api/bootstrap',extra={'Origin':'https://example.org'})[0],403)
        self.assertEqual(self.request('/api/bootstrap',extra={'Host':'other.invalid'})[0],403)

    def test_private_changes_do_not_increment_visitor_revision(self):
        old=self.request('/api/visible',role='visitor')[1]
        iid=self.create('本人私有。');self.change('confirm',iid)
        new=self.request('/api/visible',role='visitor')[1]
        self.assertEqual(old,new)


if __name__=='__main__': unittest.main(verbosity=2)
