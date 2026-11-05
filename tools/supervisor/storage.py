"""本机 SQLite 检查点。事务与唯一键控制状态/投递，不存外部原文。"""

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import sqlite3

from .events import _result

_ALERTS = "'BLOCKED_QUOTA','BLOCKED_AUTH','BLOCKED_EXECUTION','FOUNDER_MERGE_REQUIRED','FOUNDER_GAME_ACTION_REQUIRED'"
_SCHEMA = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, issue_url TEXT NOT NULL,
        agent TEXT NOT NULL CHECK(agent IN ('codex','deepseek')),
        state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','SUCCEEDED','BLOCKED_QUOTA',
            'BLOCKED_AUTH','BLOCKED_EXECUTION','WAITING_FOUNDER','RESUMABLE')),
        error_category TEXT CHECK(error_category IN ('provider_rate_limit','quota_exhausted',
            'auth_failed','timeout','unknown_failure')),
        event_id TEXT, attempt_no INTEGER NOT NULL CHECK(attempt_no>=0),
        retry_count INTEGER NOT NULL CHECK(retry_count BETWEEN 0 AND 3),
        retry_at INTEGER, deadline INTEGER NOT NULL, resume_token TEXT)""",
    """CREATE TABLE events (
        event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        attempt_no INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('synthetic','adapter','operator')),
        kind TEXT NOT NULL CHECK(kind IN ('started','finished','founder_merge_required',
            'founder_game_action_required','pause','resume')),
        category TEXT CHECK(category IN ('provider_rate_limit','quota_exhausted','auth_failed','timeout','unknown_failure')),
        exit_code INTEGER, http_status INTEGER, received_at INTEGER NOT NULL, fingerprint TEXT NOT NULL)""",
    f"""CREATE TABLE deliveries (
        event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id), attempt_no INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ({_ALERTS})),
        status TEXT NOT NULL CHECK(status IN ('PREPARED','RESERVED','ACCEPTED','UNKNOWN','FAILED')),
        reservations INTEGER NOT NULL DEFAULT 0 CHECK(reservations BETWEEN 0 AND 3),
        next_try_at INTEGER NOT NULL, api_accepted INTEGER NOT NULL DEFAULT 0 CHECK(api_accepted IN (0,1)),
        wechat_receipt_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(wechat_receipt_confirmed=0),
        UNIQUE(task_id,attempt_no,event_type))""",
)


class _LocationError(Exception):
    pass


def _inside_git(base):
    for parent in (base, *base.parents):
        marker = parent / '.git'
        # 平台可能挂载空 .git 占位目录；实际仓库（包括 unborn）都有 HEAD。
        if marker.is_file() or (marker / 'HEAD').is_file():
            return True
        if (parent / 'HEAD').is_file() and (parent / 'objects').is_dir():
            return True
    return False


def _operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _LocationError:
            return _result('E_STORAGE_LOCATION', '检查点必须位于 Git 仓库之外，数据库不得为符号链接。')
        except sqlite3.DatabaseError as error:
            if getattr(error, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return _result('E_DB_BUSY', '检查点被占用；本次未提交。')
            return _result('E_DB_INVALID', '检查点损坏、版本不兼容或结构无效；已停止，不自动重建。')
        except OSError:
            return _result('E_STORAGE_IO', '无法访问本地检查点；路径及系统详情已抑制。')
        except Exception:
            return _result('E_INTERNAL', 'Supervisor 操作失败；原始异常已抑制。')
    return wrapped


class _Store:
    def __init__(self, base_dir):
        self.base_dir = base_dir

    @contextmanager
    def transaction(self):
        if str(self.base_dir).startswith(('\\\\', '//')):
            raise _LocationError()
        base = Path(self.base_dir).expanduser().resolve()
        if _inside_git(base):
            raise _LocationError()
        base.mkdir(parents=True, exist_ok=True)
        path = base / 'supervisor.sqlite3'
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise _LocationError()
        connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise sqlite3.DatabaseError('Unsupported checkpoint')
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version=1')
            elif version != 1:
                raise sqlite3.DatabaseError('Unsupported checkpoint')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Invalid checkpoint')
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.DatabaseError('Invalid checkpoint references')
            # 版本存在但缺表时也失败，不能静默补表丢掉去重信息。
            for table in ('tasks', 'events', 'deliveries'):
                connection.execute(f'SELECT * FROM {table} LIMIT 0')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
