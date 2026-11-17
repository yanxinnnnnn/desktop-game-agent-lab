"""M0.3 单次 Worker。受控 GitHub 验收只运行固定的合成 READY 探针。"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from uuid import UUID, uuid4

from .adapters.dsh import DshOfflineAdapter, FixtureRunnerResult, OfflineCapture, TaskContext
from .events import _result
from .service import Supervisor
from .storage import _Store, _operation
from .worker_github import GhGitHub, GitHubError, ISSUE_URL, valid_number

_TASK = {'schema_version': 1, 'task_type': 'ready_probe', 'mode': 'synthetic'}
_FENCE = re.compile(r'^```json[ \t]*\r?\n([^`]+)^```[ \t]*\r?$', re.MULTILINE)
_PHASE = 'synthetic_wiring'
_SESSION = 'synthetic-ready-fixture'
_READY = (
    '{"type":"session","sessionId":"synthetic-ready-fixture"}\n'
    '{"type":"final","sessionId":"synthetic-ready-fixture","reason":{"kind":"completed"},"text":"READY"}\n'
).encode('ascii')
_RUN_TIMEOUT = 5
_GITHUB_PROBE_ISSUE = 31
_GH_ERRORS = {'invalid_issue', 'github_unavailable', 'github_auth', 'github_cli_missing', 'comment_scan_limit'}


def task_id_for(number):
    # Supervisor 要求 UUID4 形状。用固定仓库 Issue URL 的摘要构造持久关联键，
    # 不使用 Issue 正文、可变标题或用户提供的任务 ID。
    return str(UUID(bytes=hashlib.sha256((ISSUE_URL + str(number)).encode()).digest()[:16], version=4))


def _marker(task_id):
    return f'<!-- m03-worker:v1:{task_id} -->'


def _unique_pairs(pairs):
    result = dict(pairs)
    if len(pairs) != len(result):
        raise ValueError('Duplicate key')
    return result


def _supports(body):
    # CTO 的文字仅用于人类阅读。只认可唯一、完整的固定版本 JSON 声明。
    if not isinstance(body, str) or len(body) > 8192 or body.count('```') != 2:
        return False
    declarations = _FENCE.findall(body)
    if len(declarations) != 1 or len(declarations[0]) > 256:
        return False
    try:
        data = json.loads(declarations[0], object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        return False
    return data == _TASK and type(data['schema_version']) is int


@dataclass(frozen=True, repr=False)
class Capture:
    output: bytes = b''
    exit_code: int | None = None
    problem: str | None = None


def synthetic_ready():
    """固定、无后代、无网络的本地子进程；输出长度由内联夹具固定。"""
    try:
        # 仅继承 Windows 进程启动所需的系统根目录；不传递 gh、DSH 或模型环境。
        env = {k: v for k, v in os.environ.items() if k.upper() == 'SYSTEMROOT'}
        argv = [sys.executable, '-I', '-S', '-c', f'import sys; sys.stdout.buffer.write({_READY!r})']
        with tempfile.TemporaryDirectory(prefix='worker-ready-') as directory:
            proc = subprocess.run(argv, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=_RUN_TIMEOUT, check=False)
        return Capture(proc.stdout, proc.returncode)
    except subprocess.TimeoutExpired:
        # subprocess.run 等待本次固定子进程退出，不按进程名清理其他进程。
        return Capture(problem='runner_timeout')
    except OSError:
        return Capture(problem='runner_start')


@dataclass(frozen=True)
class _Outcome:
    classification: str
    exit_code: int | None
    terminal: str

    def valid(self):
        value = self.exit_code
        if value is not None and (type(value) is not int or not -(2**31) <= value < 2**32):
            return False
        return (
            (self.classification == 'PASS' and self.terminal == 'completed' and value == 0)
            or (self.classification == 'FAIL' and self.terminal in ('invalid_terminal', 'execution_failed'))
            or (self.classification == 'BLOCKED' and self.terminal in ('runner_start', 'runner_timeout') and value is None)
        )

    def body(self, task_id):
        return '\n'.join((_marker(task_id), f'task: {task_id}', f'phase: {_PHASE}',
                          f'classification: {self.classification}',
                          f'exit_code: {self.exit_code if self.exit_code is not None else "none"}',
                          f'terminal: {self.terminal}', 'synthetic: true'))


class _Publications:
    """同一 SQLite 的 Worker 专用账本，不改变 Supervisor 状态机或 schema 版本。"""

    def __init__(self, base_dir):
        self._store = _Store(base_dir)

    @_operation
    def lookup(self, task_id):
        with self._store.transaction() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_publications'").fetchone() is None:
                return _result()
            row = db.execute('SELECT classification,exit_code,terminal,state FROM worker_publications WHERE task_id=?',
                             (task_id,)).fetchone()
            if row is None:
                return _result()
            outcome = _Outcome(*tuple(row)[:3])
            if not outcome.valid() or row['state'] not in ('RESERVED', 'PUBLISHED'):
                raise sqlite3.DatabaseError('Invalid publication')
            return _result(data={'outcome': outcome, 'state': row['state']})

    @_operation
    def reserve(self, task_id, outcome):
        with self._store.transaction() as db:
            if not outcome.valid():
                raise sqlite3.DatabaseError('Invalid outcome')
            db.execute("""CREATE TABLE IF NOT EXISTS worker_publications (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                classification TEXT NOT NULL, exit_code INTEGER, terminal TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED','PUBLISHED')))""")
            db.execute("INSERT INTO worker_publications VALUES (?,?,?,?,'RESERVED')",
                       (task_id, outcome.classification, outcome.exit_code, outcome.terminal))
            return _result()

    @_operation
    def confirm(self, task_id):
        with self._store.transaction() as db:
            changed = db.execute("UPDATE worker_publications SET state='PUBLISHED' WHERE task_id=?", (task_id,))
            if changed.rowcount != 1:
                raise sqlite3.DatabaseError('Missing publication')
            return _result()


def _event(context, kind, *, exit_code=None, signal='none'):
    return dict(schema_version='1.0', task_id=context.task_id, event_id=context.event_id,
                issue_url=context.issue_url, agent='deepseek', attempt_no=context.attempt_no,
                kind=kind, source='synthetic', exit_code=exit_code, http_status=None, signal=signal,
                retry_after_seconds=None, resume_token=None)


def _finish(context, runner):
    try:
        capture = runner()
    except Exception:
        # 不持久化异常消息、子进程输出或 traceback。
        capture = Capture(problem='runner_start')
    if not isinstance(capture, Capture):
        capture = Capture(problem='runner_start')
    if capture.problem is not None:
        reason = 'runner_timeout' if capture.problem == 'runner_timeout' else 'runner_start'
        signal = 'timeout' if reason == 'runner_timeout' else 'sdk_fault'
        return _Outcome('BLOCKED', None, reason), _event(context, 'finished', signal=signal)
    code = capture.exit_code
    if type(code) is not int or not -(2**31) <= code < 2**32:
        code = None
    runner_metadata = FixtureRunnerResult(exit_code=code, os_signal=-code if code is not None and
                                          -(2**31) < code < 0 else None, expected_session_id=_SESSION)
    parsed = DshOfflineAdapter().parse(OfflineCapture(capture.output, context, runner_metadata))
    if not parsed['Ok']:
        return _Outcome('FAIL', code, 'invalid_terminal'), _event(context, 'finished', exit_code=code, signal='sdk_fault')
    event = parsed['Data']
    if event['category'] is None:
        return _Outcome('PASS', code, 'completed'), event
    return _Outcome('FAIL', code, 'execution_failed'), event


def _report(task_id, outcome, publication):
    return _result(data={'task': task_id, 'phase': _PHASE, 'classification': outcome.classification,
                         'exit_code': outcome.exit_code, 'terminal': outcome.terminal, 'synthetic': True,
                         'publication': publication})


def _blocked(code):
    return _result(code, '本次停止；不记录或显示原始输入及异常。')


def _reconcile(github, number, actor, task_id, outcome, publications):
    try:
        matches = github.marked_comments(number, _marker(task_id))
    except Exception:
        return _blocked('publication_unknown')
    if matches != [{'login': actor, 'body': outcome.body(task_id)}]:
        return _blocked('publication_unknown')
    if not publications.confirm(task_id)['Ok']:
        return _blocked('checkpoint_error')
    return _report(task_id, outcome, 'reconciled')


def run_once(number, base_dir, *, github, runner=synthetic_ready, mode='synthetic'):
    """只接受固定探针；执行及回写均为最多一次，未决回执只能只读对账。"""
    if mode != 'synthetic':
        return _blocked('real_dsh_disabled')
    if not valid_number(number):
        return _blocked('invalid_issue')
    task_id = task_id_for(number)
    issue_url = ISSUE_URL + str(number)
    supervisor = Supervisor(base_dir)
    publications = _Publications(base_dir)

    prior = publications.lookup(task_id)
    if not prior['Ok']:
        return _blocked('checkpoint_error')
    status = supervisor.status(task_id)
    if status['Code'] not in ('OK', 'E_TASK_NOT_FOUND'):
        return _blocked('checkpoint_error')
    if prior['Data'] is None and status['Ok']:
        # 包括领取后中断及完成后未预约回写的崩溃窗口。没有新执行权。
        return _blocked('already_claimed')
    try:
        actor = github.identity()
        if prior['Data'] is not None:
            return _reconcile(github, number, actor, task_id, prior['Data']['outcome'], publications)
        if not _supports(github.issue(number)):
            return _blocked('unsupported_task')
        if github.marked_comments(number, _marker(task_id)):
            return _blocked('existing_marker')
    except GitHubError as error:
        code = error.args[0] if error.args and isinstance(error.args[0], str) else None
        return _blocked(code if code in _GH_ERRORS else 'github_unavailable')
    except Exception:
        return _blocked('github_unavailable')

    # register 的事务和 task_id 主键共同决定唯一执行者；检查与领取竞态时只认事务结果。
    claim = supervisor.register(task_id, issue_url, 'deepseek')
    if not claim['Ok']:
        return _blocked('already_claimed' if claim['Code'] == 'E_TASK_EXISTS' else 'checkpoint_error')
    started = TaskContext(task_id, str(uuid4()), issue_url, claim['Data']['attempt_no'])
    if not supervisor.ingest(_event(started, 'started'))['Ok']:
        return _blocked('checkpoint_error')
    finished = TaskContext(task_id, str(uuid4()), issue_url, started.attempt_no)
    outcome, event = _finish(finished, runner)
    if not supervisor.ingest(event)['Ok']:
        return _blocked('checkpoint_error')
    if not publications.reserve(task_id, outcome)['Ok']:
        return _blocked('checkpoint_error')

    # 先预约再请求 POST；中断/超时均不可重发。只认可当前 gh 身份的精确回写。
    try:
        github.post(number, outcome.body(task_id), actor)
    except Exception:
        return _reconcile(github, number, actor, task_id, outcome, publications)
    if not publications.confirm(task_id)['Ok']:
        return _blocked('checkpoint_error')
    return _report(task_id, outcome, 'published')


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Worker 参数无效；请使用 --help。\n')


def main(argv=None):
    parser = _Parser(description='M0.3 单次 Worker：只允许指定 GitHub 探针上的合成 READY 回路。')
    parser.add_argument('--issue', required=True, type=int, help=f'仅接受 CTO 指定的探针 Issue #{_GITHUB_PROBE_ISSUE}。')
    parser.add_argument('--base-dir', required=True, type=Path, help='Git 仓库之外的稳定本机账本目录。')
    parser.add_argument('--mode', default='synthetic', choices=('synthetic', 'real'))
    parser.add_argument('--github-probe', action='store_true', help='显式启用该探针的一次 GitHub 回写。')
    args = parser.parse_args(argv)
    if args.mode != 'synthetic':
        result = _blocked('real_dsh_disabled')
    elif not args.github_probe:
        result = _blocked('github_probe_required')
    elif args.issue != _GITHUB_PROBE_ISSUE:
        result = _blocked('probe_issue_not_allowed')
    else:
        try:
            result = run_once(args.issue, args.base_dir, github=GhGitHub())
        except GitHubError:
            result = _blocked('github_cli_missing')
        except Exception:
            result = _blocked('worker_error')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['Ok'] and result['Data']['classification'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
