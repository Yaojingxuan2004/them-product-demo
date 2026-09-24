"""ThEM local demo: durable memory controls, default mock, opt-in model provider."""
import argparse
import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def now():
    return datetime.now(timezone.utc).isoformat()


def text_field(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise Problem(400, f"请输入1—{limit}个字符，不会自动截断你的内容。")
    return value.strip()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, text TEXT NOT NULL, role TEXT NOT NULL,
                    status TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL,
                    source_id TEXT NOT NULL REFERENCES sources(id), created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
                    event TEXT NOT NULL, snapshot TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES('public_revision',0);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, visitor=False):
        with self.connect() as db:
            db.execute('BEGIN')  # Keep rows and their public revision in one read snapshot.
            if visitor:
                rows = db.execute("SELECT text,version,id FROM items WHERE status='accepted' AND public=1 AND role='personal' ORDER BY created,id").fetchall()
                revision = db.execute("SELECT value FROM metadata WHERE key='public_revision'").fetchone()[0]
                return {'items': [dict(row) for row in rows], 'revision': revision, 'mode': 'authorized_records_only', 'ai_connected': False}
            rows = db.execute('SELECT i.*,s.text AS source_text,s.kind AS source_kind,s.created AS source_created FROM items i JOIN sources s ON s.id=i.source_id ORDER BY i.created DESC,i.id').fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item['history'] = [dict(h) for h in db.execute('SELECT event,snapshot,created FROM history WHERE item_id=? ORDER BY id', (row['id'],))]
                for event in item['history']:
                    event['snapshot'] = json.loads(event['snapshot'])
                items.append(item)
            return {'items': items, 'ai_connected': False, 'data_kind': 'synthetic_demo'}

    def operation(self, op_id):
        with self.connect() as db:
            row = db.execute('SELECT response FROM operations WHERE id=?', (op_id,)).fetchone()
            return {'state': 'committed', 'result': json.loads(row[0])} if row else {'state': 'not_found'}

    @staticmethod
    def source(db, text, kind):
        sid = secrets.token_hex(12)
        db.execute('INSERT INTO sources VALUES(?,?,?,?)', (sid, text, kind, now()))
        return sid

    @staticmethod
    def audit(db, item_id, event):
        row = dict(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())
        db.execute('INSERT INTO history(item_id,event,snapshot,created) VALUES(?,?,?,?)',
                   (item_id, event, json.dumps(row, ensure_ascii=False), now()))

    def mutate(self, payload):
        if not isinstance(payload, dict):
            raise Problem(400, '请求格式错误。')
        op_id = payload.get('operation_id')
        if not isinstance(op_id, str) or not 16 <= len(op_id) <= 80:
            raise Problem(400, '缺少有效操作标识，请刷新后重试。')
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Problem(409, '同一操作标识对应不同内容，请核对原操作。')
                return json.loads(previous['response'])
            action = payload.get('action')
            if action == 'create':
                entries = payload.get('entries')
                if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
                    raise Problem(400, '每次提交1—4条自述。')
                if db.execute("SELECT count(*) FROM items WHERE status='pending'").fetchone()[0] + len(entries) > 40:
                    raise Problem(409, '待确认内容已达40条，请先处理已有内容。')
                cleaned = []
                for e in entries:
                    if not isinstance(e, dict) or e.get('role') not in ('personal', 'ideal'):
                        raise Problem(400, '请选择关于我或对朋友的期待。')
                    cleaned.append((text_field(e.get('text'), 300), e['role']))
                # One submission is one immutable source; siblings are separate items.
                sid = self.source(db, '\n'.join(x[0] for x in cleaned), 'self_report')
                ids = []
                for content, role in cleaned:
                    iid, stamp = secrets.token_hex(12), now()
                    db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)', (iid, content, role, 'pending', 0, 1, sid, stamp, stamp))
                    self.audit(db, iid, 'create')
                    ids.append(iid)
                result = {'item_ids': ids, 'message': '已保存为待确认内容，尚未进入长期记忆。'}
            else:
                if action not in ('confirm', 'ignore', 'edit', 'allow', 'revoke', 'disable'):
                    raise Problem(400, '不支持的操作。')
                row = db.execute('SELECT * FROM items WHERE id=?', (payload.get('item_id'),)).fetchone()
                if row is None:
                    raise Problem(404, '找不到这条内容。')
                if type(payload.get('expected_version')) is not int or payload['expected_version'] != row['version']:
                    raise Problem(409, '内容已在其他页面更新。请刷新资料后再操作；你的编辑草稿会保留。')
                item = dict(row)
                old_public = bool(item['public'])
                if action in ('confirm', 'ignore') and item['status'] != 'pending':
                    raise Problem(409, '这条内容已经处理，请刷新资料。')
                if action in ('allow', 'revoke', 'disable') and item['status'] != 'accepted':
                    raise Problem(409, '只有已记住的内容可以进行这项操作。')
                if action in ('confirm', 'edit'):
                    if action == 'edit' and item['status'] not in ('pending', 'accepted'):
                        raise Problem(409, '历史记录不能直接恢复。请通过新的自述重新提交。')
                    if item['status'] != 'accepted' and db.execute("SELECT count(*) FROM items WHERE status='accepted'").fetchone()[0] >= 20:
                        raise Problem(409, '已达到20条有效记忆，请先停用不再使用的内容。')
                    if action == 'edit':
                        content = text_field(payload.get('text'), 300)
                        role = payload.get('role')
                        if role not in ('personal', 'ideal'):
                            raise Problem(400, '无效归属。')
                        if payload.get('meaning_confirmed') is not True:
                            raise Problem(400, '请确认修改后的内容及归属确实表达你的意思。')
                        if content == item['text'] and role == item['role']:
                            raise Problem(400, '内容没有变化，可取消编辑。')
                        item['source_id'] = self.source(db, content, 'explicit_correction')
                        item['text'], item['role'] = content, role
                    item['status'], item['public'] = 'accepted', 0
                elif action == 'ignore':
                    item['status'] = 'ignored'
                elif action == 'disable':
                    item['status'], item['public'] = 'disabled', 0
                elif action == 'allow':
                    if item['role'] != 'personal':
                        raise Problem(403, '对朋友的期待仅供本人使用，不能允许访客使用。')
                    if payload.get('confirmed_text') != item['text']:
                        raise Problem(409, '请针对当前这条准确内容确认访客用途。')
                    if item['public']:
                        raise Problem(409, '这条内容已经允许访客使用。')
                    item['public'] = 1
                elif action == 'revoke':
                    if not item['public']:
                        raise Problem(409, '这条内容当前仅供本人使用。')
                    item['public'] = 0
                item['version'] += 1
                item['updated'] = now()
                db.execute('UPDATE items SET text=?,role=?,status=?,public=?,version=?,source_id=?,updated=? WHERE id=?',
                           tuple(item[k] for k in ('text','role','status','public','version','source_id','updated','id')))
                self.audit(db, item['id'], action)
                if old_public or item['public']:
                    db.execute("UPDATE metadata SET value=value+1 WHERE key='public_revision'")
                messages = {'confirm': '已记住，默认仅供本人使用。', 'edit': '修改已保存，新内容仅供本人使用。',
                            'ignore': '已忽略，没有进入长期记忆。', 'allow': '已允许分身向访客使用这条内容。',
                            'revoke': '已停止访客使用；已展示过的信息不会因此消失。', 'disable': '已停用，不再作为记忆使用；原始记录保留。'}
                result = {'item_id': item['id'], 'version': item['version'], 'message': messages[action]}
            if hasattr(self, 'after_mutation'):
                self.after_mutation(db)
            db.execute('INSERT INTO operations VALUES(?,?,?,?)', (op_id, fingerprint, json.dumps(result, ensure_ascii=False), now()))
            return result


def make_server(db_path, port=8765, provider=None, max_calls=6, background=True):
    from chat_service import ChatService
    store = Store(db_path)
    chat = ChatService(store, Problem, provider, max_calls, background)
    store.after_mutation = chat.invalidate
    tokens = {secrets.token_urlsafe(32): 'owner', secrets.token_urlsafe(32): 'visitor'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Never log request bodies or local capability tokens.

        def send(self, status, value, content_type='application/json; charset=utf-8'):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict,list)) else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Transaction is already committed and queryable by operation ID.

        def guard(self, owner=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected:
                raise Problem(403, '仅允许本机回环地址访问。')
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{expected}':
                raise Problem(403, '不接受其他页面发起的请求。')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise Problem(403, '不接受跨站请求。')
            if owner and tokens.get(self.headers.get('X-Session')) != 'owner':
                raise Problem(403, '此操作仅供资料主人使用。')

        def do_GET(self):
            try:
                self.guard()
                path = urlparse(self.path).path
                if path == '/api/bootstrap':
                    return self.send(200, {'sessions': {v:k for k,v in tokens.items()}, 'stage': 'B', 'provider':chat.provider.mode, 'ai_connected':chat.provider.mode!='mock'})
                if path.startswith('/api/chat/'):
                    kind=parse_qs(urlparse(self.path).query).get('kind',[''])[0]
                    principal=tokens.get(self.headers.get('X-Session'))
                    if path=='/api/chat/state':return self.send(200,chat.snapshot(kind,principal))
                    if path=='/api/chat/history':return self.send(200,chat.history(principal))
                    if path.startswith('/api/chat/operations/'):
                        return self.send(200,chat.operation(path.rsplit('/',1)[-1],kind,principal))
                    raise Problem(404,'会话接口不存在。')
                if path == '/api/state':
                    self.guard(owner=True)
                    return self.send(200, store.snapshot())
                if path == '/api/visible':
                    if tokens.get(self.headers.get('X-Session')) != 'visitor':
                        raise Problem(403, '请使用访客预览会话。')
                    return self.send(200, store.snapshot(visitor=True))
                if path.startswith('/api/operations/'):
                    self.guard(owner=True)
                    return self.send(200, store.operation(path.rsplit('/',1)[-1]))
                files = {'/': ('index.html','text/html; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/chat.js':('chat.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8')}
                files['/them-display.woff2'] = ('them-display.woff2', 'font/woff2')
                if path not in files:
                    raise Problem(404, '页面不存在。')
                file, mime = files[path]
                self.send(200, (ROOT/'static'/file).read_bytes(), mime)
            except Problem as e:
                self.send(e.status, {'error': e.message})

        def do_POST(self):
            try:
                self.guard(owner=self.path!='/api/chat/actions')
                if self.path not in ('/api/actions','/api/chat/actions'):
                    raise Problem(404, '接口不存在。')
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise Problem(400, '请求应为JSON。')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 20000:
                    raise Problem(413, '请求内容过大或为空。')
                data = json.loads(self.rfile.read(size))
                result = chat.submit(data,tokens.get(self.headers.get('X-Session'))) if self.path=='/api/chat/actions' else store.mutate(data)
                self.send(200, result)
            except Problem as e:
                self.send(e.status, {'error': e.message})
            except (ValueError, UnicodeError):
                self.send(400, {'error': '请求格式无法识别，原保存状态未改变。'})
            except sqlite3.Error:
                self.send(503, {'error': '未能完成保存，请核对操作状态。', 'uncertain': True})

    class AppServer(ThreadingHTTPServer):
        def server_close(self):
            chat.close()
            super().server_close()
    server = AppServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.store = store
    server.chat = chat
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', type=Path, default=ROOT/'data'/'demo.sqlite3')
    parser.add_argument('--provider',choices=['mock','deepseek'],default='mock')
    parser.add_argument('--model',help='真实接入时必须显式指定已核对的模型名')
    parser.add_argument('--max-calls',type=int,default=6)
    args = parser.parse_args()
    provider=None
    if args.provider=='deepseek':
        import getpass
        import warnings
        from providers import DeepSeekProvider
        if not args.model or not 1<=args.max_calls<=6:
            parser.error('真实模式需指定--model，--max-calls为1—6；不会自动扩额。')
        print('真实API模式：页面发送和整理会产生付费请求；不自动重试。Key只在后端内存中。')
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            try:key=getpass.getpass('DeepSeek Key（隐藏输入）：').strip()
            except (getpass.GetPassWarning,EOFError,KeyboardInterrupt):raise SystemExit('已取消，未调用API。')
        if not key or any(c.isspace() for c in key):raise SystemExit('密钥为空或格式无效。')
        provider=DeepSeekProvider(key,args.model)
        key=None
    server = make_server(args.db, args.port,provider,args.max_calls)
    print(f'ThEM stage B: http://127.0.0.1:{server.server_port} ({server.chat.provider.mode})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
