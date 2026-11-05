"""M0.2a 离线状态机和事务式告警账本；从不调用外部 Agent。"""

import hashlib
import json
import math
import re
import sqlite3
import time
from uuid import uuid4

from tools.notifications import build_event
from .events import CATEGORIES, KINDS, STATES, StructuredAdapter, _identity, _result, _uuid, normalize_event
from .storage import _Store, _operation

_RETRY_BUDGET = 3
_RETRY_WINDOW = 3600
_HARD = {'quota_exhausted': 'BLOCKED_QUOTA', 'auth_failed': 'BLOCKED_AUTH',
         'unknown_failure': 'BLOCKED_EXECUTION'}
_PREFLIGHT = {'E_DISABLED', 'E_CREDENTIAL_MISSING', 'E_CREDENTIAL_INVALID', 'E_COOLDOWN', 'E_SEND_LIMIT'}
_DEFINITE_FAILURE = {'E_PROVIDER', 'E_HTTP', 'E_RATE_LIMIT'}


def _fingerprint(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _task(connection, task_id):
    row = connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    if (not _identity(task['task_id'], task['issue_url'], task['agent'], task['resume_token']) or
            task['state'] not in STATES or task['error_category'] not in (None, *CATEGORIES) or
            (task['event_id'] is not None and not _uuid(task['event_id']))):
        raise sqlite3.DatabaseError('Invalid checkpoint values')
    for key in ('attempt_no', 'retry_count', 'deadline'):
        if type(task[key]) is not int or task[key] < 0:
            raise sqlite3.DatabaseError('Invalid checkpoint counter')
    if task['state'] == 'RESUMABLE' and (
            type(task['retry_at']) is not int or task['retry_at'] > task['deadline'] or
            task['error_category'] not in ('timeout', 'provider_rate_limit')):
        raise sqlite3.DatabaseError('Invalid checkpoint retry')
    if task['event_id'] is not None:
        event = connection.execute('SELECT task_id FROM events WHERE event_id=?', (task['event_id'],)).fetchone()
        if event is None or event[0] != task_id:
            raise sqlite3.DatabaseError('Invalid checkpoint correlation')
    return task


def _journal(table, row):
    """损坏的本地元数据也不能经 check 变成任意原文输出。"""
    item = dict(row)
    valid = (_uuid(item['event_id']) and _uuid(item['task_id']) and
             type(item['attempt_no']) is int and item['attempt_no'] >= 0)
    if table == 'events':
        valid = (valid and item['source'] in ('synthetic', 'adapter', 'operator') and
                 item['kind'] in (*KINDS, 'pause', 'resume') and item['category'] in (None, *CATEGORIES) and
                 type(item['received_at']) is int and item['received_at'] >= 0 and
                 isinstance(item['fingerprint'], str) and bool(re.fullmatch('[0-9a-f]{64}', item['fingerprint'])))
        valid = valid and (item['exit_code'] is None or type(item['exit_code']) is int)
        valid = valid and (item['http_status'] is None or
                           (type(item['http_status']) is int and 100 <= item['http_status'] <= 599))
    else:
        valid = (valid and item['event_type'] in ('BLOCKED_QUOTA', 'BLOCKED_AUTH', 'BLOCKED_EXECUTION',
                 'FOUNDER_MERGE_REQUIRED', 'FOUNDER_GAME_ACTION_REQUIRED') and
                 item['status'] in ('PREPARED', 'RESERVED', 'ACCEPTED', 'UNKNOWN', 'FAILED') and
                 type(item['reservations']) is int and 0 <= item['reservations'] <= 3 and
                 type(item['next_try_at']) is int and item['next_try_at'] >= 0 and
                 item['api_accepted'] in (0, 1) and item['wechat_receipt_confirmed'] == 0)
    if not valid:
        raise sqlite3.DatabaseError('Invalid checkpoint journal')
    return item


def _update(connection, task):
    connection.execute('''UPDATE tasks SET state=?,error_category=?,event_id=?,attempt_no=?,
        retry_count=?,retry_at=?,deadline=?,resume_token=? WHERE task_id=?''',
        tuple(task[key] for key in ('state', 'error_category', 'event_id', 'attempt_no',
              'retry_count', 'retry_at', 'deadline', 'resume_token', 'task_id')))


def _event(connection, event, now):
    connection.execute('''INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)''', (
        event['event_id'], event['task_id'], event['attempt_no'], event['source'], event['kind'],
        event.get('category'), event.get('exit_code'), event.get('http_status'), now, _fingerprint(event)))


def _prepare(connection, task, event_type, now):
    connection.execute('''INSERT INTO deliveries(event_id,task_id,attempt_no,event_type,status,next_try_at)
        VALUES (?,?,?,?,'PREPARED',?) ON CONFLICT(task_id,attempt_no,event_type) DO NOTHING''',
        (task['event_id'], task['task_id'], task['attempt_no'], event_type, now))


def _view(task, now):
    return task | {'resume_ready': task['state'] == 'RESUMABLE' and
                   task['retry_at'] <= now <= task['deadline']}


class Supervisor:
    """调用方提供本地目录、可选可信适配器/发送器；构造时不读凭据或创建进程。"""

    def __init__(self, base_dir, *, clock=time.time, sender=None):
        self._store = _Store(base_dir)
        self._clock = clock
        self._sender = sender

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**40:
            raise ValueError('Invalid clock')
        return int(value)

    @_operation
    def register(self, task_id, issue_url, agent, *, resume_token=None):
        if not _identity(task_id, issue_url, agent, resume_token):
            return _result('E_TASK_INVALID', '任务仅接受 UUID4、本仓库 Issue 链接和受支持角色。')
        now = self._now()
        with self._store.transaction() as connection:
            if _task(connection, task_id):
                return _result('E_TASK_EXISTS', '任务已经存在；不覆盖历史。')
            connection.execute('''INSERT INTO tasks VALUES (?,?,?,'PENDING',NULL,NULL,0,0,NULL,?,?)''',
                               (task_id, issue_url, agent, now + _RETRY_WINDOW, resume_token))
            return _result(data=_view(_task(connection, task_id), now))

    @_operation
    def ingest(self, record, *, adapter=None):
        try:
            parsed = (adapter if adapter is not None else StructuredAdapter()).parse(record)
        except Exception:
            return _result('E_ADAPTER', '适配器失败；原始 SDK/客户端异常未保存。')
        if not isinstance(parsed, dict) or parsed.get('Ok') is not True:
            return _result('E_EVENT_INVALID', '适配器未提供有效结构化事件；原文未保存。')
        checked = normalize_event(parsed.get('Data'))
        if not checked['Ok']:
            return checked
        event, now = checked['Data'], self._now()
        with self._store.transaction() as connection:
            previous = connection.execute('SELECT fingerprint FROM events WHERE event_id=?',
                                          (event['event_id'],)).fetchone()
            if previous:
                code = 'E_DUPLICATE_EVENT' if previous[0] == _fingerprint(event) else 'E_EVENT_CONFLICT'
                return _result(code, '事件 ID 已存在；不重复迁移或通知。')
            task = _task(connection, event['task_id'])
            if task is None:
                return _result('E_TASK_NOT_FOUND', '任务未注册。')
            if (event['agent'], event['issue_url']) != (task['agent'], task['issue_url']):
                return _result('E_EVENT_CORRELATION', '事件与任务身份不一致。')
            if event['attempt_no'] != task['attempt_no']:
                return _result('E_STALE_EVENT', '事件不属于当前尝试；不接受迟到的旧结果。')
            kind, category, state = event['kind'], event['category'], task['state']
            repeat = kind == 'finished' and category is not None and category == task['error_category'] and (
                state in ('RESUMABLE', 'BLOCKED_QUOTA', 'BLOCKED_AUTH', 'BLOCKED_EXECUTION'))
            alert = None
            if kind == 'started' and state == 'PENDING':
                task['state'] = 'RUNNING'
            elif kind == 'finished' and state == 'RUNNING':
                task.update(error_category=category, retry_at=None)
                if category is None:
                    task['state'] = 'SUCCEEDED'
                elif category in _HARD:
                    task['state'] = alert = _HARD[category]
                else:
                    delay = max(30 * 2**task['retry_count'], event['retry_after_seconds'] or 0)
                    if task['retry_count'] >= _RETRY_BUDGET or now + delay > task['deadline']:
                        task['state'] = alert = 'BLOCKED_EXECUTION'
                    else:
                        task.update(state='RESUMABLE', retry_count=task['retry_count'] + 1, retry_at=now + delay)
            elif repeat:
                # 同一失败尝试的新日志事件不再次耗费重试预算，也不重新安排倒计时。
                pass
            elif kind in ('founder_merge_required', 'founder_game_action_required') and state in ('PENDING', 'RUNNING'):
                task.update(state='WAITING_FOUNDER', error_category=None, retry_at=None)
                alert = kind.upper()
            else:
                return _result('E_STATE_TRANSITION', '当前状态不接受该事件；检查事件顺序。')
            task['event_id'] = event['event_id']
            if event['resume_token'] is not None:
                task['resume_token'] = event['resume_token']
            _event(connection, event, now)
            _update(connection, task)
            if alert:
                _prepare(connection, task, alert, now)
            return _result(data=_view(task, now))

    @_operation
    def status(self, task_id=None):
        if task_id is not None and not _uuid(task_id):
            return _result('E_TASK_INVALID', '任务 ID 必须为 UUID4。')
        now = self._now()
        with self._store.transaction() as connection:
            ids = [row[0] for row in connection.execute('SELECT task_id FROM tasks ORDER BY task_id')]
            tasks = [_view(_task(connection, identifier), now) for identifier in ids
                     if task_id is None or identifier == task_id]
            if task_id is not None and not tasks:
                return _result('E_TASK_NOT_FOUND', '任务未注册。')
            return _result(data={'tasks': tasks})

    @_operation
    def check(self, task_id=None):
        if task_id is not None and not _uuid(task_id):
            return _result('E_TASK_INVALID', '任务 ID 必须为 UUID4。')
        now = self._now()
        with self._store.transaction() as connection:
            ids = [row[0] for row in connection.execute('SELECT task_id FROM tasks ORDER BY task_id')]
            tasks = [_view(_task(connection, identifier), now) for identifier in ids
                     if task_id is None or identifier == task_id]
            if task_id is not None and not tasks:
                return _result('E_TASK_NOT_FOUND', '任务未注册。')
            rows = {'tasks': tasks}
            for table in ('events', 'deliveries'):
                query = f'SELECT * FROM {table}'
                args = () if task_id is None else (task_id,)
                if task_id is not None:
                    query += ' WHERE task_id=?'
                rows[table] = [_journal(table, row) for row in connection.execute(query + ' ORDER BY event_id', args)]
            return _result(data=rows)

    @_operation
    def pause(self, task_id):
        return self._control(task_id, resume=False, acknowledge=False)

    @_operation
    def resume(self, task_id, *, acknowledge=False):
        return self._control(task_id, resume=True, acknowledge=acknowledge)

    def _control(self, task_id, *, resume, acknowledge):
        if not _uuid(task_id) or type(acknowledge) is not bool:
            return _result('E_TASK_INVALID', '任务 ID 或人工确认格式无效。')
        now = self._now()
        with self._store.transaction() as connection:
            task = _task(connection, task_id)
            if task is None:
                return _result('E_TASK_NOT_FOUND', '任务未注册。')
            state = task['state']
            deadline_exceeded = resume and state == 'RESUMABLE' and now > task['deadline']
            if resume and not deadline_exceeded and state == 'RESUMABLE':
                if now < task['retry_at']:
                    return _result('E_RETRY_NOT_DUE', '尚未到重试时刻；不等待、不启动客户端。')
                # 即使外部时钟已经到点，也只更改记账状态，不执行 Agent。
            elif resume and state in ('BLOCKED_QUOTA', 'BLOCKED_AUTH', 'BLOCKED_EXECUTION', 'WAITING_FOUNDER'):
                if acknowledge is not True:
                    return _result('E_FOUNDER_REQUIRED', '须人工确认阻塞原因已处理；不自动恢复。')
                task.update(retry_count=0, deadline=now + _RETRY_WINDOW)
            elif not resume and state in ('PENDING', 'RUNNING', 'RESUMABLE'):
                pass
            elif not deadline_exceeded:
                return _result('E_STATE_TRANSITION', '当前状态不接受暂停或恢复。')
            task['event_id'] = str(uuid4())
            if deadline_exceeded:
                task.update(state='BLOCKED_EXECUTION', retry_at=None)
            elif resume:
                task.update(state='PENDING', attempt_no=task['attempt_no'] + 1, error_category=None, retry_at=None)
                # 已经解决的旧尝试，其尚未提交的告警不应迟到发送。
                connection.execute("UPDATE deliveries SET status='FAILED' WHERE task_id=? AND status='PREPARED'", (task_id,))
            else:
                task.update(state='WAITING_FOUNDER', retry_at=None)
            control = {'event_id': task['event_id'], 'task_id': task_id, 'attempt_no': task['attempt_no'],
                       'source': 'operator', 'kind': 'resume' if resume and not deadline_exceeded else 'pause',
                       'category': task['error_category']}
            _event(connection, control, now)
            _update(connection, task)
            if deadline_exceeded:
                _prepare(connection, task, 'BLOCKED_EXECUTION', now)
                return _result('E_RETRY_DEADLINE', '重试窗口已过；任务阻塞并准备人工告警。', _view(task, now))
            return _result(data=_view(task, now))

    @_operation
    def notify(self, task_id, *, enabled=False):
        if enabled is not True:
            return _result('E_DISABLED', '通知默认关闭；不会读取 SendKey 或访问网络。')
        if not _uuid(task_id):
            return _result('E_TASK_INVALID', '任务 ID 必须为 UUID4。')
        if self._sender is None:
            return _result('E_NOTIFIER_UNAVAILABLE', '未提供通知发送器；保留待处理告警。')
        now = self._now()
        with self._store.transaction() as connection:
            task = _task(connection, task_id)
            if task is None:
                return _result('E_TASK_NOT_FOUND', '任务未注册。')
            row = connection.execute('''SELECT * FROM deliveries WHERE task_id=? AND attempt_no=?
                ORDER BY event_id LIMIT 1''', (task_id, task['attempt_no'])).fetchone()
            if row is None:
                return _result('E_NO_ALERT', '当前尝试没有准备好的人工告警。')
            delivery = _journal('deliveries', row)
            if delivery['status'] != 'PREPARED':
                return _result('E_DELIVERY_FINAL', '投递已提交或状态不确定；禁止自动重发。', delivery)
            if now < delivery['next_try_at']:
                return _result('E_NOTIFICATION_NOT_DUE', '通知预检冷却中。')
            alert = build_event(delivery['event_type'], delivery['event_id'], task['issue_url'], task['agent'])
            if not alert['Ok']:
                return _result('E_DB_INVALID', '持久化告警不符合通知契约；停止发送。')
            connection.execute("UPDATE deliveries SET status='RESERVED',reservations=reservations+1 WHERE event_id=?",
                               (delivery['event_id'],))
        # 先提交预留，再调用外部服务；崩溃留下 RESERVED，不会在重启时重新发送。
        try:
            sent = self._sender.send(alert['Data'], enabled=True)
        except Exception:
            sent = None
        status, result_code, sender_code = 'UNKNOWN', 'E_NOTIFICATION_UNCERTAIN', None
        if isinstance(sent, dict) and isinstance(sent.get('Code'), str):
            sender_code = sent.get('Code')
            if (sent.get('Ok') is True and sender_code == 'OK' and isinstance(sent.get('Data'), dict) and
                    sent['Data'].get('provider_accepted') is True):
                status, result_code = 'ACCEPTED', 'OK'
            elif sent.get('Ok') is False and sender_code in _PREFLIGHT:
                status = 'PREPARED' if delivery['reservations'] + 1 < 3 else 'FAILED'
                result_code = 'E_NOTIFICATION_DEFERRED' if status == 'PREPARED' else 'E_NOTIFICATION_FAILED'
            elif sent.get('Ok') is False and sender_code in _DEFINITE_FAILURE:
                status, result_code = 'FAILED', 'E_NOTIFICATION_FAILED'
        with self._store.transaction() as connection:
            connection.execute('''UPDATE deliveries SET status=?,next_try_at=?,api_accepted=?
                WHERE event_id=? AND status='RESERVED' ''',
                (status, self._now() + 300, int(status == 'ACCEPTED'), delivery['event_id']))
        # 仅输出自身固定状态和布尔结果；不转发 sender 的 Message/Data/异常。
        return _result(result_code, '' if result_code == 'OK' else '通知结果已记账；不自动重试。',
                       {'event_id': delivery['event_id'], 'status': status,
                        'api_accepted': status == 'ACCEPTED', 'wechat_receipt_confirmed': False})
