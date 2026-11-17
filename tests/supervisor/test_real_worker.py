"""使用假 GitHub、合成流和受控 stub；不运行 DSH 或访问网络。"""

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.supervisor import Supervisor, real_dsh as dsh, worker
from tools.supervisor.adapters.dsh import DshOfflineAdapter, FixtureRunnerResult, OfflineCapture, TaskContext
from tools.supervisor.windows_job import ProcessResult, capture_windows

from test_worker import FakeGitHub, _BODY

_ISSUE = 123456799
_CREDENTIAL = 'stub-credential-not-real'
_PRIVATE = 'private-fixture-session'
_BODY_REAL = _BODY.replace('"mode": "synthetic"', '"mode": "real"')
_URL = worker.ISSUE_URL + str(_ISSUE)


def _trace(other=None, middle=None, *, final=True):
    events = [{'type': 'session', 'sessionId': _PRIVATE, 'cwd': 'fixture-private-path'}]
    if middle:
        events.append(middle)
    if final:
        events.append({'type': 'final', 'sessionId': other or _PRIVATE,
                       'reason': {'kind': 'completed'}, 'text': 'private-fixture-output'})
    return ''.join(json.dumps(event) + '\n' for event in events).encode()


def _pin(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class RealWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = self.root / 'ledger'
        self.temporary = self.root / 'scratch'
        self.temporary.mkdir()
        self.launcher = self.root / 'dsh.cmd'
        self.overlay = self.root / 'reviewed-overlay.yml'
        self.runtime = self.root / 'base.yml'
        self.plan = self.root / 'review.json'
        # 文件只用于测试本地哈希/参数；用 stub 模拟进程执行结果。
        self.launcher.write_text('offline fixture, never executed')
        self.overlay.write_text('fixture overlay')
        self.runtime.write_text('fixture base')
        self.system = self.root / 'windows'
        (self.system / 'System32').mkdir(parents=True)
        (self.system / 'System32' / 'cmd.exe').touch()
        self.review = dict(schema_version=1, issue=_ISSUE, ledger=str(self.ledger), temp_parent=str(self.temporary),
                           launcher=_pin(self.launcher), overlay=_pin(self.overlay), runtime_files=[_pin(self.runtime)],
                           effective=dict(dsh.EFFECTIVE), reviewed_checks=dict.fromkeys(dsh.CHECKS, True))
        self.github = FakeGitHub(_BODY_REAL)
        self.github_constructor = self.enterContext(patch.object(worker, 'GhGitHub', return_value=self.github))
        self.native = self.enterContext(patch.object(dsh, 'capture_windows', return_value=ProcessResult(_trace(), 0)))
        self.enterContext(patch.object(dsh, '_on_windows', return_value=True))
        self.enterContext(patch.dict(os.environ, {
            dsh.KEY_NAME: _CREDENTIAL, 'SystemRoot': str(self.system), 'GH_TOKEN': 'fake-github-only',
            'USERPROFILE': str(self.root), 'HOME': str(self.root),
            'DEEPSEEK_BASE_URL': 'https://fixture.invalid', 'HTTPS_PROXY': 'fake-proxy',
        }, clear=True))

    def sign_fixture(self):
        self.plan.write_text(json.dumps(self.review), encoding='utf-8')
        return _pin(self.plan)['sha256']

    def main(self, number=_ISSUE, *, authority='valid', plan=True, ledger=None):
        if authority == 'valid':
            authority = self.sign_fixture()
        args = ['--issue', str(number), '--mode', 'real', '--github-probe', '--base-dir', str(ledger or self.ledger)]
        if plan:
            args += ['--real-plan', str(self.plan)]
        if authority is not None:
            args += ['--authorize-real', authority]
        with redirect_stdout(io.StringIO()) as output:
            status = worker.main(args)
        return status, json.loads(output.getvalue())

    def test_authorization_independent_and_wrong_probe_never_create_github(self):
        self.assertEqual(self.main(plan=False, authority=None)[1]['Code'], 'real_dsh_disabled')
        self.assertEqual(self.main(authority=None)[1]['Code'], 'real_authorization_required')
        self.sign_fixture()
        self.assertEqual(self.main(authority='0' * 64)[1]['Code'], 'real_authorization_mismatch')
        for number in (23, 30, 31, 33, _ISSUE + 1):
            with self.subTest(number=number):
                self.assertEqual(self.main(number)[1]['Code'], 'real_probe_not_allowed')
        self.assertEqual(self.main(ledger=self.root / 'other')[1]['Code'], 'real_preflight_failed')
        self.github_constructor.assert_not_called()
        self.native.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_rejects_unreviewed_attachments_and_overrides(self):
        original = self.review['effective']
        for key, value in (
            ('base_url', 'https://api.deepseek.com.example.invalid/anthropic'),
            ('base_url', 'https://api.deepseek.com@fixture.invalid/anthropic'),
            ('provider', 'other'), ('model', 'other'), ('max_tokens', 65),
            ('max_retries', 1), ('max_retries', False), ('redirect', 'follow'),
        ):
            with self.subTest(key=key, value=value):
                self.review['effective'] = original | {key: value}
                self.assertEqual(self.main()[1]['Code'], 'real_preflight_failed')
        self.review['effective'] = original
        self.review['reviewed_checks']['session_log_and_plugin_packages_disabled'] = False
        self.assertEqual(self.main()[1]['Code'], 'real_preflight_failed')
        self.github_constructor.assert_not_called()
        self.native.assert_not_called()

    def test_missing_credential_and_hash_drift_do_not_claim_or_publish(self):
        del os.environ[dsh.KEY_NAME]
        self.assertEqual(self.main()[1]['Code'], 'real_credentials_missing')
        os.environ[dsh.KEY_NAME] = _CREDENTIAL
        self.overlay.write_text('changed after review')
        self.assertEqual(self.main()[1]['Code'], 'real_preflight_failed')
        self.native.assert_not_called()
        self.assertFalse(self.github.comments)
        self.assertEqual(Supervisor(self.ledger).status()['Data']['tasks'], [])

    def test_synthetic_declaration_and_extra_prompt_cannot_trigger_real(self):
        for body in (_BODY, _BODY_REAL.replace('"mode": "real"', '"mode":"real","prompt":"inject"')):
            self.github.body = body
            self.assertEqual(self.main()[1]['Code'], 'unsupported_task')
        self.native.assert_not_called()
        self.assertFalse(self.github.comments)

    def journal(self):
        result = Supervisor(self.ledger).check(worker.task_id_for(_ISSUE))
        self.assertTrue(result['Ok'])
        return result['Data']

    def test_stub_success_uses_reviewed_cmd_and_environment_without_leaks(self):
        def check(exe, command, cwd, env):
            task, = self.journal()['tasks']
            self.assertEqual(task['state'], 'RUNNING')
            self.assertEqual(exe, self.system / 'System32' / 'cmd.exe')
            self.assertIn(' /d /v:off /s /c ', command)
            self.assertIn(f'"{self.launcher}" --profile headless --patch ', command)
            self.assertIn(f'--json "{dsh.PROMPT}"', command)
            self.assertNotIn(_CREDENTIAL, command)
            self.assertEqual((cwd.parent / 'overlay.yml').read_bytes(), self.overlay.read_bytes())
            self.assertEqual(set(env), {'SystemRoot', 'windir', 'PATH', 'DSH_HOME', 'HOME', 'USERPROFILE', 'APPDATA',
                                        'LOCALAPPDATA', 'TEMP', 'TMP', 'DSH_TELEMETRY_DISABLED', 'ComSpec', dsh.KEY_NAME})
            for name in ('DSH_HOME', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
                self.assertTrue(Path(env[name]).is_relative_to(cwd.parent))
            self.assertEqual(env[dsh.KEY_NAME], _CREDENTIAL)
            self.assertEqual(env['DSH_TELEMETRY_DISABLED'], '1')
            return ProcessResult(_trace(), 0)

        self.native.side_effect = check
        status, result = self.main()
        self.assertEqual(status, 0)
        self.assertEqual(result['Data'], {
            'task': worker.task_id_for(_ISSUE), 'phase': 'real_dsh', 'classification': 'PASS',
            'exit_code': 0, 'terminal': 'completed', 'synthetic': False, 'publication': 'published',
        })
        self.native.assert_called_once()
        self.assertEqual(list(self.temporary.iterdir()), [])
        journal = self.journal()
        self.assertEqual(journal['tasks'][0]['state'], 'SUCCEEDED')
        self.assertEqual({e['source'] for e in journal['events']}, {'adapter'})
        self.assertEqual(journal['deliveries'], [])
        remote, = self.github.comments
        self.assertIn('synthetic: false', remote['body'])
        for private in (_PRIVATE, dsh.PROMPT, _CREDENTIAL, 'fixture-private-path', 'private-fixture-output'):
            self.assertNotIn(private, json.dumps([result, journal, remote]))
            self.assertNotIn(private.encode(), (self.ledger / 'supervisor.sqlite3').read_bytes())

    def test_failures_have_distinct_sanitized_codes(self):
        cases = (
            (ProcessResult(problem='runner_start'), 'BLOCKED', 'runner_start', None),
            (ProcessResult(problem='runner_timeout'), 'BLOCKED', 'runner_timeout', None),
            (ProcessResult(_trace(), 7), 'FAIL', 'nonzero_exit', 7),
            (ProcessResult(_trace(final=False), 0), 'FAIL', 'terminal_missing', 0),
            (ProcessResult(_trace(other='other'), 0), 'FAIL', 'session_mismatch', 0),
            (ProcessResult(_trace()[:-1], 0), 'FAIL', 'invalid_terminal', 0),
            (ProcessResult(_trace(middle={'type': 'tool_result'}), 0), 'FAIL', 'unexpected_tool', 0),
            (ProcessResult(exit_code=0, problem='output_limit'), 'FAIL', 'output_limit', 0),
        )
        for index, (capture, classification, terminal, exit_code) in enumerate(cases):
            with self.subTest(terminal=terminal):
                self.ledger = self.root / f'ledger-{index}'
                self.review['ledger'] = str(self.ledger)
                self.github.comments.clear()
                self.native.return_value = capture
                status, result = self.main()
                self.assertEqual(status, 1)
                self.assertEqual((result['Data']['classification'], result['Data']['terminal'],
                                  result['Data']['exit_code']), (classification, terminal, exit_code))
                self.assertNotEqual(self.journal()['tasks'][0]['state'], 'SUCCEEDED')
                self.assertEqual(len(self.github.comments), 1)

    def test_lost_receipt_never_reruns_or_reposts_even_after_credential_removed(self):
        self.github.behavior = 'lost_before'
        self.assertEqual(self.main()[1]['Code'], 'publication_unknown')
        del os.environ[dsh.KEY_NAME]
        self.assertEqual(self.main()[1]['Code'], 'publication_unknown')
        self.github.comments.append({'login': 'fixture-actor', 'body': worker._Outcome(
            'PASS', 0, 'completed', False).body(worker.task_id_for(_ISSUE))})
        self.assertEqual(self.main()[1]['Data']['publication'], 'reconciled')
        self.native.assert_called_once()
        self.assertEqual(self.github.calls.count(('post', _ISSUE)), 1)

    def test_offline_adapter_and_runner_cannot_promote_fixture(self):
        context = TaskContext(worker.task_id_for(_ISSUE), str(worker.uuid4()), _URL, 0)
        fixture = OfflineCapture(_trace(), context, FixtureRunnerResult(0))
        normalized = DshOfflineAdapter._parse(fixture, forbid_tools=True)
        self.assertEqual(normalized['Data']['source'], 'synthetic')
        result = worker.run_once(_ISSUE, self.ledger, github=self.github,
                                 runner=lambda: worker.Capture(_trace(), 0), mode='real')
        self.assertEqual(result['Code'], 'real_dsh_disabled')
        self.assertEqual(self.github.calls, [])


@unittest.skipUnless(os.name == 'nt', '仅在 Windows 上测试 Job Object 的真实 stub 进程')
class WindowsJobStubTests(unittest.TestCase):
    def _capture(self, script, **options):
        with tempfile.TemporaryDirectory() as name:
            exe = Path(sys.executable)
            argv = subprocess.list2cmdline([str(exe), '-I', '-S', '-c', script])
            return capture_windows(exe, argv, Path(name), {'SystemRoot': os.environ['SystemRoot']}, **options)

    def test_exit_stdout_and_discarded_stderr(self):
        result = self._capture("import sys; print('stub'); print('stderr-fixture', file=sys.stderr); sys.exit(7)")
        self.assertEqual(result, ProcessResult(b'stub\r\n', 7))

    def test_timeout_and_stdout_limit(self):
        self.assertEqual(self._capture('import time; time.sleep(15)', timeout=.3).problem, 'runner_timeout')
        result = self._capture("import sys; sys.stdout.buffer.write(b'x' * 100000)", limit=1024)
        self.assertEqual(result.problem, 'output_limit')
        self.assertEqual(result.stdout, b'')

    def test_unexpected_tool_event_stops_the_stub(self):
        result = self._capture('import time; print(\'{"type":"tool_call"}\', flush=True); time.sleep(30)', timeout=1.5)
        self.assertEqual(result.problem, 'unexpected_tool')
        self.assertEqual(result.stdout, b'')

    def test_reviewed_batch_command_runs_only_a_fixed_local_stub(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {dsh.KEY_NAME: _CREDENTIAL}):
            root = Path(name)
            batch = root / 'dsh.cmd'
            overlay = root / 'overlay.yml'
            overlay.write_text('fixture only')
            # 这个测试脚本只检查 argv/config/环境后打印合成终态；不会导入 DSH。
            stub = root / 'stub.py'
            stub.write_text('import os, sys\n'
                            'from pathlib import Path\n'
                            'assert sys.argv[1:4] == ["--profile", "headless", "--patch"]\n'
                            'assert Path(sys.argv[4]).read_text() == "fixture only"\n'
                            f'assert sys.argv[5:] == ["--json", {dsh.PROMPT!r}]\n'
                            'assert os.environ["DSH_TELEMETRY_DISABLED"] == "1"\n'
                            'assert Path(os.environ["DSH_HOME"]).parent == Path.cwd().parent\n'
                            f'sys.stdout.buffer.write({_trace()!r})\n', encoding='utf-8')
            batch.write_text(f'@echo off\n"{sys.executable}" -I -S "{stub}" %*\nexit /b %errorlevel%\n',
                             encoding='utf-8')
            exe, command, env = dsh._launch_inputs(batch, root, overlay)
            self.assertEqual(capture_windows(exe, command, root / 'work', env), ProcessResult(_trace(), 0))

    def test_timeout_stops_the_stub_descendant(self):
        import _winapi as win
        with tempfile.TemporaryDirectory() as name:
            cwd = Path(name)
            exe = Path(sys.executable)
            script = ('from pathlib import Path; import subprocess,sys,time; '
                      'child=subprocess.Popen([sys.executable,"-I","-S","-c","import time; time.sleep(30)"]); '
                      'Path("pid").write_text(str(child.pid)); time.sleep(30)')
            argv = subprocess.list2cmdline([str(exe), '-I', '-S', '-c', script])
            result = capture_windows(exe, argv, cwd, {'SystemRoot': os.environ['SystemRoot']}, timeout=1.5)
            self.assertEqual(result.problem, 'runner_timeout')
            pid = int((cwd / 'pid').read_text())
            try:
                handle = win.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            except OSError as error:
                if error.winerror != 87:  # 已退出并且 PID 已释放。
                    raise
            else:
                try:
                    self.assertEqual(win.WaitForSingleObject(handle, 1000), win.WAIT_OBJECT_0)
                finally:
                    win.CloseHandle(handle)
