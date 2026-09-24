"""Explicit mock or opt-in DeepSeek provider. No key at import, no auto retry."""
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class ProviderFailure(Exception):
    def __init__(self, state, message, diagnostics=None):
        self.state, self.message = state, message
        self.diagnostics = diagnostics or {}


class MockProvider:
    mode = 'mock'
    model = 'deterministic-fixture-v1'

    def invoke(self, request):
        data = json.loads(request['messages'][-1]['content'])
        if data['task'] == 'extract':
            candidates = []
            for source in data['sources']:
                # A deliberately tiny fixture, not an NLP model or an inferred persona.
                for quote, role in [('我不主动', 'personal'), ('希望朋友主动', 'ideal'),
                                    ('我最近参加过一次合唱演出', 'personal')]:
                    if quote in source['text']:
                        candidates.append({'message_id': source['id'], 'role': role, 'quote': quote})
            result = {'candidates': candidates}
        else:
            question = data['question']
            if data['role'] == 'ideal':
                answer = '【模拟流程】这条回复用于检查聊天保存和往返。可发送示例“我不主动，但希望朋友主动。”，再主动整理为待确认内容。'
            elif '那次活动' in question:
                answer = '【模拟流程】你指的是哪一次活动？'
            elif '合唱' in question and any('那次活动' in x['content'] for x in request['messages'][1:-1]):
                answer = '【模拟流程】承接上轮活动问题：这份合成示例没有提供演出的日期和场馆。'
            else:
                answer = '【模拟流程】当前请求已按访客范围准备。这里只验证会话和用途规则，不模拟真实人物判断。'
            result = {'answer': answer, 'memory_ids': []}
        return {'result': result, 'response_id': None, 'usage': None, 'finish_reason': 'stop'}


class DeepSeekProvider:
    mode = 'deepseek'

    def __init__(self, key, model):
        if not isinstance(key,str) or not key or any(ord(c)<33 or ord(c)>126 for c in key):
            raise ValueError('密钥含空白、非ASCII或控制字符；请在本地重新输入，未发送请求。')
        self._key, self.model = key, model

    def invoke(self, request):
        payload = dict(request, model=self.model, temperature=0, thinking={'type':'disabled'},
                       response_format={'type': 'json_object'})
        try:
            req = Request('https://api.deepseek.com/chat/completions',
                          data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+self._key})
        except (ValueError,TypeError):
            raise ProviderFailure('failed','本地请求构造失败，未发送请求。',{'phase':'request'}) from None
        diagnostics={'phase':'transport'}
        try:
            with urlopen(req, timeout=75) as response:
                diagnostics['phase']='read'
                body = response.read(1_000_001)
        except HTTPError as error:
            state = 'failed' if 400 <= error.code < 500 else 'uncertain'
            diagnostics['http_status']=error.code
            raise ProviderFailure(state, f'服务返回HTTP {error.code}；未取得可用回答，不自动重试。',diagnostics) from None
        except (URLError, TimeoutError, OSError):
            raise ProviderFailure('uncertain', '连接或读取中断，不能确认远程是否执行；不自动重试。',diagnostics) from None
        except (ValueError,TypeError):
            raise ProviderFailure('uncertain','请求传输或读取异常，执行情况需核对；不自动重试。',diagnostics) from None
        diagnostics.update(phase='decode',response_bytes=len(body))
        if len(body)>1_000_000:
            raise ProviderFailure('uncertain','返回过大，未能恢复完整结果；不自动重试。',diagnostics)
        try:
            raw=json.loads(body)
        except (ValueError,TypeError):
            raise ProviderFailure('uncertain','已读取响应，但外层JSON无法解析；不自动重试。',diagnostics) from None
        diagnostics.update(phase='schema',root_type=type(raw).__name__)
        # Only numeric structure facts are retained on a malformed envelope.
        # Never log raw bodies, credentials, arbitrary headers or error messages.
        if isinstance(raw,dict):
            choices=raw.get('choices')
            diagnostics['choices_is_list']=isinstance(choices,list)
            if isinstance(choices,list):diagnostics['choices_count']=len(choices)
        try:
            if not isinstance(raw,dict):raise ValueError()
            choices=raw['choices']
            if not isinstance(choices,list) or not choices:raise ValueError()
            choice=choices[0]
            if not isinstance(choice,dict) or not isinstance(choice.get('message'),dict):raise ValueError()
            if not isinstance(choice['message'].get('content'),str):raise ValueError()
            return {'result':choice['message']['content'],'response_id':raw.get('id'),
                    'usage':raw.get('usage'),'finish_reason':choice.get('finish_reason')}
        except (ValueError,KeyError,IndexError,TypeError):
            raise ProviderFailure('uncertain','外层JSON已解析，但回答结构不完整；不自动重试。',diagnostics) from None
