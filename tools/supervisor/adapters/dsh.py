"""DSH Phase 1：有界、纯离线的保守 NDJSON fixture profile；不提供真实来源认证。"""

from dataclasses import dataclass
from enum import Enum
import json
import math

from ..events import Result, normalize_event

MAX_STRING_BYTES = 8 * 1024
MAX_LINE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_EVENTS = 1024
MAX_DEPTH = 16
MAX_NODES = 16384


class CodeOrigin(Enum):
    FIXTURE_HARNESS = 'fixture_harness'


@dataclass(frozen=True, repr=False)
class FixtureCodeEvidence:
    """独立于 JSON 的测试夹具证明，索引必须指向 error 事件；非原生字段路径。"""
    event_index: int
    code: str
    origin: CodeOrigin


@dataclass(frozen=True, repr=False)
class FixtureRunnerResult:
    """模拟可信 runner 的进程元数据；从来不由 NDJSON 文本反推。"""
    exit_code: int | None
    truncated: bool = False
    os_signal: int | None = None
    expected_session_id: str | None = None
    code_evidence: tuple[FixtureCodeEvidence, ...] = ()


@dataclass(frozen=True, repr=False)
class TaskContext:
    task_id: str
    event_id: str
    issue_url: str
    attempt_no: int


@dataclass(frozen=True, repr=False)
class OfflineCapture:
    """只接收显式 fixture/harness 输入；没有 source=adapter 或真实发送开关。"""
    ndjson: bytes | str
    context: TaskContext
    runner: FixtureRunnerResult


_CODE_SIGNALS = {
    'AUTH': 'auth_failed', 'MISSING_CREDENTIAL': 'auth_failed', 'INVALID_CREDENTIAL': 'auth_failed',
    'RATE_LIMIT': 'provider_rate_limit', 'QUOTA': 'quota_exhausted', 'ACCOUNT_QUOTA': 'quota_exhausted',
    'TIMEOUT': 'timeout',
    'NO_ADAPTER': 'sdk_fault', 'CONTEXT_WINDOW_EXCEEDED': 'sdk_fault',
    'EMPTY_RESPONSE': 'sdk_fault', 'SERVER': 'sdk_fault', 'TRANSPORT': 'sdk_fault',
}
_MESSAGES = {
    'E_DSH_CONTEXT': '只接受带有效任务与独立 runner 元数据的离线捕获。',
    'E_DSH_LIMIT': 'NDJSON 超出字节、事件、字符串、节点或嵌套限制。',
    'E_DSH_FRAMING': '必须为非空、逐行终止的 NDJSON；不接受空行或非标准分隔。',
    'E_DSH_JSON': 'NDJSON JSON/Unicode 无效；原始内容已抑制。',
    'E_DSH_SCHEMA': '事件不符合保守 fixture profile；未知原生字段不作为证据。',
    'E_DSH_ORDER': '事件顺序无效；必须单会话开始、单 final 结束。',
    'E_DSH_SESSION': '事件或 runner 的会话关联不一致；会话值已抑制。',
    'E_DSH_FINAL_MISSING': '缺少 final；不能将工具完成或进程退出视为任务完成。',
    'E_DSH_CONFLICT': 'error 事件与 completed final 冲突；不能确认成功。',
    'E_DSH_TRUNCATED': '捕获或事件标记为截断；不能确认结果。',
    'E_DSH_EVIDENCE': '错误码证明必须来自带类型的 fixture harness 且绑定实际 error 事件。',
    'E_DSH_INTERNAL': '离线解析失败；原始异常和输入已抑制。',
}


class _Rejected(Exception):
    pass


def _reject(code):
    raise _Rejected(code)


def _string(value):
    try:
        if len(value.encode('utf-8')) > MAX_STRING_BYTES:
            _reject('E_DSH_LIMIT')
    except UnicodeError:
        _reject('E_DSH_JSON')


def _walk(value, budget, depth=0):
    budget[0] += 1
    if budget[0] > MAX_NODES or depth > MAX_DEPTH:
        _reject('E_DSH_LIMIT')
    if isinstance(value, str):
        _string(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk(key, budget, depth + 1)
            _walk(item, budget, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _walk(item, budget, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        _reject('E_DSH_JSON')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _reject('E_DSH_JSON')
        result[key] = value
    return result


def _constant(value):
    _reject('E_DSH_JSON')


def _base(context, runner):
    return {'schema_version': '1.0', 'task_id': context.task_id, 'event_id': context.event_id,
            'attempt_no': context.attempt_no, 'agent': 'deepseek', 'issue_url': context.issue_url,
            'source': 'synthetic', 'kind': 'finished', 'exit_code': runner.exit_code,
            'http_status': None, 'signal': 'sdk_fault', 'retry_after_seconds': None, 'resume_token': None}


def _context(capture):
    if (not isinstance(capture, OfflineCapture) or not isinstance(capture.context, TaskContext) or
            not isinstance(capture.runner, FixtureRunnerResult)):
        _reject('E_DSH_CONTEXT')
    runner = capture.runner
    if (runner.exit_code is not None and (type(runner.exit_code) is not int or
            not -(2**31) <= runner.exit_code <= 2**32 - 1)) or type(runner.truncated) is not bool:
        _reject('E_DSH_CONTEXT')
    if runner.os_signal is not None and (type(runner.os_signal) is not int or not 1 <= runner.os_signal < 2**31):
        _reject('E_DSH_CONTEXT')
    if runner.expected_session_id is not None:
        if not isinstance(runner.expected_session_id, str) or not runner.expected_session_id:
            _reject('E_DSH_CONTEXT')
        _string(runner.expected_session_id)
    if type(runner.code_evidence) is not tuple or len(runner.code_evidence) > MAX_EVENTS:
        _reject('E_DSH_EVIDENCE')
    if not normalize_event(_base(capture.context, runner))['Ok']:
        _reject('E_DSH_CONTEXT')
    if runner.truncated:
        _reject('E_DSH_TRUNCATED')


def _lines(data):
    if type(data) is str:
        if len(data) > MAX_TOTAL_BYTES:
            _reject('E_DSH_LIMIT')
        try:
            data = data.encode('utf-8')
        except UnicodeError:
            _reject('E_DSH_JSON')
    if type(data) is not bytes:
        _reject('E_DSH_CONTEXT')
    if len(data) > MAX_TOTAL_BYTES:
        _reject('E_DSH_LIMIT')
    if not data or not data.endswith(b'\n'):
        _reject('E_DSH_FRAMING')
    lines = data.split(b'\n')[:-1]
    if len(lines) > MAX_EVENTS:
        _reject('E_DSH_LIMIT')
    for line in lines:
        if line.endswith(b'\r'):
            line = line[:-1]
        if len(line) > MAX_LINE_BYTES:
            _reject('E_DSH_LIMIT')
        if not line.strip() or b'\r' in line:
            _reject('E_DSH_FRAMING')
        yield line


def _shape(event):
    if not isinstance(event, dict) or event.get('type') not in (
            'session', 'text', 'thinking', 'tool_result', 'error', 'final'):
        _reject('E_DSH_SCHEMA')
    kind = event['type']
    if 'truncated' in event:
        if type(event['truncated']) is not bool:
            _reject('E_DSH_SCHEMA')
        if event['truncated']:
            _reject('E_DSH_TRUNCATED')
    if 'sessionId' in event and (not isinstance(event['sessionId'], str) or not event['sessionId']):
        _reject('E_DSH_SCHEMA')
    if kind in ('session', 'final'):
        allowed = {'type', 'sessionId', 'truncated'} | ({'reason'} if kind == 'final' else set())
        if 'sessionId' not in event or event.keys() - allowed:
            _reject('E_DSH_SCHEMA')
    if kind in ('text', 'thinking') and not isinstance(event.get('text'), str):
        _reject('E_DSH_SCHEMA')
    if kind == 'final':
        reason = event.get('reason')
        if not isinstance(reason, dict) or set(reason) != {'kind'}:
            _reject('E_DSH_SCHEMA')
        if not isinstance(reason['kind'], str) or not reason['kind']:
            _reject('E_DSH_SCHEMA')
    return kind


def _code_signal(evidence, error_indices):
    verified = {}
    for item in evidence:
        if (not isinstance(item, FixtureCodeEvidence) or item.origin is not CodeOrigin.FIXTURE_HARNESS or
                type(item.event_index) is not int or item.event_index not in error_indices or
                item.event_index in verified or not isinstance(item.code, str)):
            _reject('E_DSH_EVIDENCE')
        _string(item.code)
        if len(item.code) > 64:
            _reject('E_DSH_EVIDENCE')
        verified[item.event_index] = _CODE_SIGNALS.get(item.code, 'sdk_fault')
    if not error_indices or set(verified) != set(error_indices):
        return 'sdk_fault'
    signals = set(verified.values())
    return signals.pop() if len(signals) == 1 else 'sdk_fault'


class DshOfflineAdapter:
    """实现 EventAdapter.parse；只产生 synthetic 记录，未提供真实 runner 准入路径。"""

    def parse(self, record: object) -> Result:
        try:
            _context(record)
            session_id, final_kind = None, None
            errors, budget = [], [0]
            for index, line in enumerate(_lines(record.ndjson)):
                try:
                    event = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
                except (UnicodeError, ValueError):
                    _reject('E_DSH_JSON')
                _walk(event, budget)
                kind = _shape(event)
                if final_kind is not None or (index == 0 and kind != 'session'):
                    _reject('E_DSH_ORDER')
                if kind == 'session':
                    if session_id is not None:
                        _reject('E_DSH_ORDER')
                    session_id = event['sessionId']
                    if record.runner.expected_session_id is not None and session_id != record.runner.expected_session_id:
                        _reject('E_DSH_SESSION')
                elif session_id is None:
                    _reject('E_DSH_ORDER')
                if 'sessionId' in event and event['sessionId'] != session_id:
                    _reject('E_DSH_SESSION')
                if kind == 'error':
                    errors.append(index)
                if kind == 'final':
                    final_kind = event['reason']['kind']
            if final_kind is None:
                _reject('E_DSH_FINAL_MISSING')
            signal = _code_signal(record.runner.code_evidence, errors)
            if final_kind == 'completed' and errors:
                _reject('E_DSH_CONFLICT')
            if final_kind == 'completed' and record.runner.exit_code == 0 and record.runner.os_signal is None:
                signal = 'none'
            elif final_kind == 'completed' or record.runner.os_signal is not None:
                signal = 'sdk_fault'
            normalized = _base(record.context, record.runner) | {'signal': signal}
            return normalize_event(normalized)
        except _Rejected as error:
            code = error.args[0]
            return {'Ok': False, 'Code': code, 'Message': _MESSAGES[code], 'Data': None}
        except RecursionError:
            return {'Ok': False, 'Code': 'E_DSH_LIMIT', 'Message': _MESSAGES['E_DSH_LIMIT'], 'Data': None}
        except Exception:
            return {'Ok': False, 'Code': 'E_DSH_INTERNAL', 'Message': _MESSAGES['E_DSH_INTERNAL'], 'Data': None}
