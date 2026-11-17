"""Worker 的固定 GitHub REST 边界；不透传 Issue 数据或 gh 错误。"""

import json
import os
from pathlib import Path
import re
import subprocess

REPO = 'yanxinnnnnn/desktop-game-agent-lab'
ISSUES = f'repos/{REPO}/issues'
ISSUE_URL = f'https://github.com/{REPO}/issues/'
_TIMEOUT = 15
_PAGE_LIMIT = 5
_LOGIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?')


class GitHubError(Exception):
    """只返回 Worker 内部定义的短码。"""


def valid_number(number):
    return type(number) is int and 1 <= number <= 999_999_999


def _issue_path(number):
    if not valid_number(number):
        raise GitHubError('invalid_issue')
    return f'{ISSUES}/{number}'


class GhGitHub:
    """使用指定的本机 GitHub CLI 身份；网络仅用于指定探针验收。"""

    def __init__(self):
        # 只从可信的本机 PATH 选用绝对路径；不执行批处理或当前目录的程序。
        paths = [p for p in os.get_exec_path() if Path(p).is_absolute()
                 and Path(p).resolve() != Path.cwd().resolve()]
        name = 'gh.exe' if os.name == 'nt' else 'gh'
        candidate = next((p for p in (Path(directory) / name for directory in paths)
                          if p.is_file() and os.access(p, os.X_OK)), None)
        if candidate is None:
            raise GitHubError('github_cli_missing')
        self._gh = str(candidate.resolve())

    def _api(self, endpoint, *, method='GET', projection=None, body=None):
        argv = [self._gh, 'api', '--hostname', 'github.com', '--method', method, endpoint]
        if projection is not None:
            argv += ['--jq', projection]
        if body is not None:
            # 只把固定结果通过 stdin 传递；GH 凭据由 gh 自己处理。
            argv += ['--input', '-']
        # 使用 gh 本机的已登录身份，不读取环境中的凭据或继承调试/代理开关。
        allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE',
                   'HOME', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR', 'TMP', 'TEMP', 'TMPDIR'}
        env = {k: os.environ[k] for k in os.environ if k.upper() in allowed}
        env.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', GH_NO_EXTENSION_UPDATE_NOTIFIER='1')
        try:
            proc = subprocess.run(argv, input=json.dumps({'body': body}).encode() if body is not None else b'',
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=_TIMEOUT, check=False, env=env)
            if proc.returncode or len(proc.stdout) > 196608:
                raise GitHubError('github_unavailable')
            return json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise GitHubError('github_unavailable') from None

    def identity(self):
        try:
            user = self._api('user', projection='{login}')
            login = user['login']
            if not isinstance(login, str) or not _LOGIN.fullmatch(login):
                raise GitHubError('github_auth')
            return login
        except (GitHubError, KeyError, TypeError):
            raise GitHubError('github_auth') from None

    def issue(self, number):
        path = _issue_path(number)
        result = self._api(path, projection='{number,html_url,state,body:(.body // "")[:8193],is_pr:has("pull_request")}')
        if (not isinstance(result, dict) or result.get('number') != number or
                result.get('html_url') != ISSUE_URL + str(number) or result.get('state') != 'open' or
                result.get('is_pr') is not False or not isinstance(result.get('body'), str)):
            raise GitHubError('invalid_issue')
        return result['body']

    def marked_comments(self, number, marker):
        path = _issue_path(number)
        # marker 由本地固定格式产生；限制分页、每页投影及进程时长。
        if not re.fullmatch(r'<!-- m03-worker:v1:[0-9a-f-]{36} -->', marker):
            raise GitHubError('github_unavailable')
        query = ('{count:length, matches:[.[] | select((.body // "") | contains(' + json.dumps(marker) +
                 ')) | {login:.user.login,body:.body[:512]}]}')
        matches = []
        for page in range(1, _PAGE_LIMIT + 1):
            result = self._api(f'{path}/comments?per_page=100&page={page}', projection=query)
            if (not isinstance(result, dict) or type(result.get('count')) is not int or
                    not 0 <= result['count'] <= 100 or not isinstance(result.get('matches'), list) or
                    len(result['matches']) > result['count'] or any(not isinstance(row, dict) for row in result['matches'])):
                raise GitHubError('github_unavailable')
            matches.extend(result['matches'])
            if result['count'] < 100:
                return matches
        raise GitHubError('comment_scan_limit')

    def post(self, number, body, actor):
        path = _issue_path(number)
        result = self._api(f'{path}/comments', method='POST', body=body,
                           projection='{login:.user.login,body:.body[:512]}')
        if result != {'login': actor, 'body': body}:
            raise GitHubError('publication_unknown')
