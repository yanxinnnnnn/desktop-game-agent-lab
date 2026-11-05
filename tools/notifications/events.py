"""标准告警事件：白名单元数据生成固定模板，不接受外部自由文本。"""

import re
from uuid import UUID
from typing import TypedDict


class Result(TypedDict):
    Ok: bool
    Code: str
    Message: str
    Data: dict | None


class AlertEvent(TypedDict):
    schema_version: str
    event_id: str
    event_type: str
    github_url: str
    agent: str
    task: str
    state: str
    next_human_action: str
    title: str


# (标题、状态、下一项人工行动)，均由已审核代码定义，不复制异常或 PR 正文。
_TEMPLATES = {
    'FOUNDER_MERGE_REQUIRED': ('需要人工合并', 'review_pending', '请审阅关联 PR；仅由 Founder 决定是否手动合并。'),
    'BLOCKED_QUOTA': ('额度阻塞', 'blocked_quota', '请人工检查额度与预算；不自动充值或切换账号。'),
    'BLOCKED_AUTH': ('授权阻塞', 'blocked_auth', '请人工检查关联任务的授权配置；不要在消息中提供凭据。'),
    'BLOCKED_EXECUTION': ('执行阻塞', 'blocked_execution', '请查看关联任务并决定安全的下一步。'),
    'FOUNDER_GAME_ACTION_REQUIRED': ('需要人工游戏操作', 'human_action_required', '请在关联任务中确认授权范围与停止条件；本通知不构成实机操作授权。'),
    'NOTIFIER_TEST': ('通知通道测试', 'test', '请 Founder 人工确认微信是否收到；API 成功不等于已收到。'),
}
_AGENTS = {'codex', 'deepseek', 'cto', 'founder', 'system'}
_URL = re.compile(r'https://github\.com/yanxinnnnnn/desktop-game-agent-lab/(issues|pull)/([1-9][0-9]{0,8})\Z')
_MAX_TITLE = 32


def _result(code='OK', message='', data=None):
    return {'Ok': code == 'OK', 'Code': code, 'Message': message, 'Data': data}


def build_event(event_type: str, event_id: str, github_url: str, agent: str) -> Result:
    """只允许事件枚举、UUID4、本仓库 Issue/PR URL 与角色枚举。"""
    if not isinstance(event_type, str) or event_type not in _TEMPLATES:
        return _result('E_EVENT_INVALID', '不支持的通知事件类型。')
    if not isinstance(event_id, str):
        return _result('E_EVENT_INVALID', 'event_id 必须为规范 UUID4。')
    try:
        identifier = UUID(event_id)
    except ValueError:
        return _result('E_EVENT_INVALID', 'event_id 必须为规范 UUID4。')
    if identifier.version != 4 or str(identifier) != event_id:
        return _result('E_EVENT_INVALID', 'event_id 必须为规范 UUID4。')
    if not isinstance(agent, str) or agent not in _AGENTS:
        return _result('E_EVENT_INVALID', '通知角色不在白名单。')
    match = _URL.fullmatch(github_url) if isinstance(github_url, str) else None
    if match is None:
        return _result('E_EVENT_INVALID', '仅接受本仓库规范的 Issue 或 PR 链接。')
    subject = 'Issue' if match[1] == 'issues' else 'PR'
    task = f'{subject} #{match[2]}'
    heading, state, action = _TEMPLATES[event_type]
    event: AlertEvent = {
        'schema_version': '1.0', 'event_id': event_id, 'event_type': event_type,
        'github_url': github_url, 'agent': agent, 'task': task, 'state': state,
        'next_human_action': action, 'title': f'{heading} · {task}',
    }
    return _result(data=event)


def _validate_event(event):
    if not isinstance(event, dict):
        return _result('E_EVENT_INVALID', '通知必须为标准事件对象。')
    title = event.get('title')
    if (not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE or
            not title.isprintable()):
        return _result('E_TITLE_INVALID', '标题必须为 1–32 个可打印字符。')
    rebuilt = build_event(event.get('event_type'), event.get('event_id'),
                          event.get('github_url'), event.get('agent'))
    if not rebuilt['Ok'] or event != rebuilt['Data']:
        return _result('E_EVENT_INVALID', '拒绝非模板内容、额外字段或无效通知元数据。')
    return rebuilt


def _description(event):
    return '\n\n'.join([
        f"事件：{event['event_type']}", f"事件 ID：{event['event_id']}",
        f"Agent：{event['agent']}", f"任务：{event['task']}",
        f"状态：{event['state']}", f"下一项人工行动：{event['next_human_action']}",
        f"关联链接：{event['github_url']}",
    ])
