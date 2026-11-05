"""厂商无关的结构化入口；不解析未经验证的客户端日志字符串。"""

import re
from typing import Protocol, TypedDict
from uuid import UUID


class Result(TypedDict):
    Ok: bool
    Code: str
    Message: str
    Data: dict | None


class AgentEvent(TypedDict):
    schema_version: str
    task_id: str
    event_id: str
    attempt_no: int
    agent: str
    issue_url: str
    source: str
    kind: str
    exit_code: int | None
    http_status: int | None
    signal: str
    retry_after_seconds: int | None
    resume_token: str | None
    category: str | None


class EventAdapter(Protocol):
    def parse(self, record: object) -> Result:
        """可信适配器返回中立结构化事件；Supervisor 仍会重做白名单校验。"""
        ...


STATES = ('PENDING', 'RUNNING', 'SUCCEEDED', 'BLOCKED_QUOTA', 'BLOCKED_AUTH',
          'BLOCKED_EXECUTION', 'WAITING_FOUNDER', 'RESUMABLE')
CATEGORIES = ('provider_rate_limit', 'quota_exhausted', 'auth_failed', 'timeout', 'unknown_failure')
KINDS = ('started', 'finished', 'founder_merge_required', 'founder_game_action_required')
SIGNALS = ('none', 'provider_rate_limit', 'quota_exhausted', 'auth_failed', 'timeout', 'sdk_fault')
FIELDS = {'schema_version', 'task_id', 'event_id', 'attempt_no', 'agent', 'issue_url', 'source',
          'kind', 'exit_code', 'http_status', 'signal', 'retry_after_seconds', 'resume_token'}


def _result(code='OK', message='', data=None):
    return {'Ok': code == 'OK', 'Code': code, 'Message': message, 'Data': data}


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        item = UUID(value)
        return item.version == 4 and str(item) == value
    except ValueError:
        return False


def _identity(task_id, issue_url, agent, resume_token=None):
    return (_uuid(task_id) and agent in ('codex', 'deepseek') and
            isinstance(issue_url, str) and bool(re.fullmatch(
                r'https://github\.com/yanxinnnnnn/desktop-game-agent-lab/issues/[1-9][0-9]{0,8}', issue_url)) and
            (resume_token is None or _uuid(resume_token)))


def normalize_event(record: object) -> Result:
    """只接收明确定义的退出状态/中立信号；未知字段或原始错误文本一律拒绝。"""
    bad = _result('E_EVENT_INVALID', '结构化事件不符合白名单契约；未保存原始内容。')
    if not isinstance(record, dict) or not FIELDS <= record.keys() or record.keys() - (FIELDS | {'category'}):
        return bad
    if not _identity(record['task_id'], record['issue_url'], record['agent'], record['resume_token']):
        return bad
    if record['schema_version'] != '1.0' or not _uuid(record['event_id']):
        return bad
    if type(record['attempt_no']) is not int or not 0 <= record['attempt_no'] <= 2**31 - 1:
        return bad
    if record['source'] not in ('synthetic', 'adapter') or record['kind'] not in KINDS or record['signal'] not in SIGNALS:
        return bad
    exit_code, status, retry = record['exit_code'], record['http_status'], record['retry_after_seconds']
    if exit_code is not None and (type(exit_code) is not int or not -(2**31) <= exit_code <= 2**32 - 1):
        return bad
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return bad
    if retry is not None and (type(retry) is not int or not 0 <= retry <= 86400):
        return bad
    signal = record['signal']
    if record['kind'] != 'finished':
        if any(value is not None for value in (exit_code, status, retry)) or signal != 'none':
            return bad
        category = None
    elif signal != 'none':
        # 这里的 signal 是适配器契约，不是臆造的 Codex/DeepSeek 原生错误码。
        if status in (401, 403) and signal != 'auth_failed':
            return bad
        category = 'unknown_failure' if signal == 'sdk_fault' else signal
    elif status in (401, 403):
        category = 'auth_failed'
    elif status == 429:
        category = 'provider_rate_limit'
    elif exit_code == 0 and (status is None or 200 <= status < 300):
        category = None
    else:
        category = 'unknown_failure'
    if retry is not None and category != 'provider_rate_limit':
        return bad
    if 'category' in record and record['category'] != category:
        return bad
    return _result(data={key: record[key] for key in FIELDS} | {'category': category})


class StructuredAdapter:
    """仅支持本模块中立格式；真实 CLI/SDK 格式仍待 Windows 只读验证。"""

    def parse(self, record: object) -> Result:
        return normalize_event(record)
