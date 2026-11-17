"""#33 本机预检与已审查 DSH 启动边界；GitHub 内容不参与命令构造。"""

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .storage import _inside_git
from .windows_job import ProcessResult, capture_windows
from .worker_github import valid_number

KEY_NAME = 'DEEPSEEK_API_KEY'
PROMPT = 'Reply with exactly: READY. Do not call tools, open files, browse, modify data, or run commands.'
EFFECTIVE = {
    'version': '0.2.2-rc.1', 'profile': 'headless', 'provider': 'deepseek-official',
    'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/anthropic',
    'request_url': 'https://api.deepseek.com/anthropic/v1/messages',
    'max_tokens': 64, 'max_retries': 0, 'redirect': 'error',
}
CHECKS = frozenset({
    'effective_configuration', 'isolated_home_and_runtime_paths', 'tools_and_subagents_disabled',
    'other_providers_disabled', 'title_and_telemetry_disabled', 'session_log_and_plugin_packages_disabled',
    'other_request_data_and_network_reviewed', 'launcher_exit_and_one_call_verified',
})
_FIELDS = {'schema_version', 'issue', 'ledger', 'temp_parent', 'launcher', 'overlay', 'runtime_files',
           'effective', 'reviewed_checks'}
_LEGACY = {23, 30, 31, 33}
_SHA = re.compile(r'[a-f0-9]{64}')
_SAFE_CMD = re.compile(r'[%"!^&|<>()\r\n]')
_MAX_OVERLAY = 256 * 1024


class RealBlocked(Exception):
    """仅携带固定短码；绝不输出路径或私有配置。"""


def _unique(pairs):
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError('duplicate')
    return record


def _absolute(path):
    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate).startswith(('\\\\', '//')) or '..' in candidate.parts:
        raise RealBlocked('real_preflight_failed')
    for value in (candidate, *candidate.parents):
        try:
            info = value.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RealBlocked('real_preflight_failed')
    return candidate.resolve()


def _local(path):
    target = _absolute(path)
    if _inside_git(target) or target.is_relative_to((Path.home() / '.dsh').resolve()):
        raise RealBlocked('real_preflight_failed')
    return target


def _regular(path):
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RealBlocked('real_preflight_failed')


def _read(path, limit):
    _regular(path)
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise RealBlocked('real_preflight_failed')
    return data


@dataclass(frozen=True, repr=False)
class _Pin:
    path: Path
    digest: str

    @classmethod
    def load(cls, data):
        if (not isinstance(data, dict) or data.keys() != {'path', 'sha256'} or
                not isinstance(data['path'], str) or not isinstance(data['sha256'], str) or
                not _SHA.fullmatch(data['sha256'])):
            raise RealBlocked('real_preflight_failed')
        return cls(_local(data['path']), data['sha256'])

    def verify(self, limit=None):
        if _local(self.path) != self.path:
            raise RealBlocked('real_preflight_failed')
        if limit is None:
            _regular(self.path)
            with self.path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            data = None
        else:
            data = _read(self.path, limit)
            digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, self.digest):
            raise RealBlocked('real_preflight_failed')
        return data


def _policy(data):
    # True/False 与整数在 Python 中可相等；审查清单必须精确到值和类型。
    return (isinstance(data, dict) and data.keys() == EFFECTIVE.keys() and
            all(type(data[k]) is type(v) and data[k] == v for k, v in EFFECTIVE.items()))


def _on_windows():
    return os.name == 'nt'


@dataclass(frozen=True, repr=False)
class _Approved:
    issue: int
    ledger: Path
    temp_parent: Path
    launcher: _Pin
    overlay: _Pin
    runtime_files: tuple[_Pin, ...]

    def preflight(self):
        if not _on_windows():
            raise RealBlocked('real_windows_required')
        if not os.environ.get(KEY_NAME):
            raise RealBlocked('real_credentials_missing')
        try:
            self._verified_overlay()
            _windows_system(self.launcher.path, self.temp_parent)
        except (OSError, ValueError):
            raise RealBlocked('real_preflight_failed') from None

    def _verified_overlay(self):
        if _local(self.temp_parent) != self.temp_parent or not self.temp_parent.is_dir():
            raise RealBlocked('real_preflight_failed')
        self.launcher.verify()
        for item in self.runtime_files:
            item.verify()
        return self.overlay.verify(_MAX_OVERLAY)

    def run(self):
        # 不接收输入流、runner 回调或来源标记；只有这里取得实际 OS 退出码。
        try:
            if not _on_windows():
                raise RealBlocked('real_windows_required')
            overlay = self._verified_overlay()
            with tempfile.TemporaryDirectory(prefix='dsh-once-', dir=self.temp_parent) as name:
                root = Path(name)
                patch = root / 'overlay.yml'
                with patch.open('xb') as stream:
                    stream.write(overlay)
                # 使用审核的同一字节快照，启动前再次复核。
                if _Pin(patch, self.overlay.digest).verify(_MAX_OVERLAY) != overlay:
                    raise RealBlocked('real_preflight_failed')
                executable, command, env = _launch_inputs(self.launcher.path, root, patch)
                try:
                    return capture_windows(executable, command, root / 'work', env)
                finally:
                    env.pop(KEY_NAME, None)
        except RealBlocked as error:
            return ProcessResult(problem=error.args[0])
        except (OSError, ValueError):
            return ProcessResult(problem='runner_start')


def load_approved(issue, ledger, path, authorization):
    """授权是 Founder 在专用本机终端提供的独立参数；不读取 GitHub 评论或正文。"""
    if path is None and authorization is None:
        raise RealBlocked('real_dsh_disabled')
    if path is None or not isinstance(authorization, str) or not _SHA.fullmatch(authorization):
        raise RealBlocked('real_authorization_required')
    if not valid_number(issue) or issue in _LEGACY:
        raise RealBlocked('real_probe_not_allowed')
    try:
        raw = _read(_local(path), 32 * 1024)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), authorization):
            raise RealBlocked('real_authorization_mismatch')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if (not isinstance(data, dict) or data.keys() != _FIELDS or
                type(data['schema_version']) is not int or data['schema_version'] != 1):
            raise RealBlocked('real_preflight_failed')
        if not valid_number(data['issue']) or data['issue'] != issue:
            raise RealBlocked('real_probe_not_allowed')
        checks = data['reviewed_checks']
        if (not _policy(data['effective']) or not isinstance(checks, dict) or checks.keys() != CHECKS or
                not all(value is True for value in checks.values())):
            raise RealBlocked('real_preflight_failed')
        base, parent = _local(data['ledger']), _local(data['temp_parent'])
        if base != _local(ledger) or not parent.is_dir():
            raise RealBlocked('real_preflight_failed')
        launcher, overlay = _Pin.load(data['launcher']), _Pin.load(data['overlay'])
        files = data['runtime_files']
        if not isinstance(files, list) or not 1 <= len(files) <= 64:
            raise RealBlocked('real_preflight_failed')
        runtime = tuple(_Pin.load(item) for item in files)
        if (launcher.path.name.lower() not in ('dsh.cmd', 'dsh.exe') or
                len({pin.path for pin in (*runtime, launcher, overlay)}) != len(runtime) + 2):
            raise RealBlocked('real_preflight_failed')
        return _Approved(issue, base, parent, launcher, overlay, runtime)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise RealBlocked('real_preflight_failed') from None


def _windows_system(launcher, temp_parent):
    system_root = _absolute(os.environ.get('SystemRoot', ''))
    system = _absolute(system_root / 'System32')
    if not system.is_dir():
        raise RealBlocked('real_preflight_failed')
    if launcher.suffix.lower() == '.cmd':
        if any(_SAFE_CMD.search(str(value)) for value in (launcher, temp_parent, system)):
            raise RealBlocked('real_preflight_failed')
        if not _absolute(system / 'cmd.exe').is_file():
            raise RealBlocked('real_preflight_failed')
    return system_root, system


def _launch_inputs(launcher, root, patch):
    system_root, system = _windows_system(launcher, patch)
    home = root / 'home'
    for folder in (home, root / 'tmp', root / 'work', root / 'appdata', root / 'localappdata'):
        folder.mkdir()
    env = {
        'SystemRoot': str(system_root), 'windir': str(system_root),
        'PATH': os.pathsep.join((str(launcher.parent), str(system))),
        'DSH_HOME': str(home), 'HOME': str(home), 'USERPROFILE': str(home),
        'APPDATA': str(root / 'appdata'), 'LOCALAPPDATA': str(root / 'localappdata'),
        'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'DSH_TELEMETRY_DISABLED': '1',
    }
    args = ['--profile', EFFECTIVE['profile'], '--patch', str(patch), '--json', PROMPT]
    if launcher.suffix.lower() == '.cmd':
        # #23 使用 Windows 批处理入口；命令文本只拼接固定参数与预检的安全本地路径。
        executable = system / 'cmd.exe'
        env['ComSpec'] = str(executable)
        command = f'"{executable}" /d /v:off /s /c ""{launcher}" --profile headless --patch "{patch}" --json "{PROMPT}""'
    else:
        executable = launcher
        command = subprocess.list2cmdline([str(launcher), *args])
    # 只在最终启动边界读取一个值；不传入 gh、命令行、账本、文件或父进程报告。
    secret = os.environ.get(KEY_NAME)
    if not secret:
        raise RealBlocked('real_credentials_missing')
    env[KEY_NAME] = secret
    return executable, command, env
