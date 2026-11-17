"""仅本地测试：不联系 GitHub、通知服务、DSH、模型或游戏。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import io
import json
import os
import sqlite3
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from tools.supervisor import Supervisor
from tools.supervisor import worker
from tools.supervisor.worker_github import GhGitHub, GitHubError, ISSUE_URL, REPO

_NUMBER = 123456789
_BODY = (
    'CTO 指定的合成验收。外围文字不属于任务。\n\n'
    '```json\n{\n  "schema_version": 1,\n  "task_type": "ready_probe",\n  "mode": "synthetic"\n}\n```\n'
)


class FakeGitHub:
    def __init__(self, body=_BODY):
        self.body = body
        self.comments = []
        self.calls = []
        self.behavior = 'success'

    def identity(self):
        self.calls.append(('identity',))
        return 'fixture-actor'

    def issue(self, number):
        self.calls.append(('issue', number))
        return self.body

    def marked_comments(self, number, marker):
        self.calls.append(('comments', number))
        return [row for row in self.comments if marker in row['body']]

    def post(self, number, body, actor):
        self.calls.append(('post', number))
        if self.behavior != 'lost_before':
            self.comments.append({'login': actor, 'body': body})
        if self.behavior != 'success':
            raise GitHubError('E_FIXTURE_UNCERTAIN')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.github = FakeGitHub()
        self.runner = Mock(return_value=worker.Capture(worker._READY, 0))
        self.task_id = worker.task_id_for(_NUMBER)

    def run_once(self, **kwargs):
        runner = kwargs.pop('runner', self.runner)
        return worker.run_once(_NUMBER, self.root, github=self.github, runner=runner, **kwargs)

    def assertLedger(self, state, *, exit_code=None):
        journal = Supervisor(self.root).check(self.task_id)
        self.assertTrue(journal['Ok'])
        task, = journal['Data']['tasks']
        self.assertEqual((task['issue_url'], task['agent'], task['state'], task['attempt_no']),
                         (ISSUE_URL + str(_NUMBER), 'deepseek', state, 0))
        events = {e['kind']: e for e in journal['Data']['events']}
        self.assertEqual(set(events), {'started', 'finished'})
        self.assertEqual(events['finished']['exit_code'], exit_code)
        self.assertEqual({e['source'] for e in events.values()}, {'synthetic'})
        return journal

    def test_claim_before_execution_and_real_local_synthetic_subprocess(self):
        def ready():
            task, = Supervisor(self.root).status(self.task_id)['Data']['tasks']
            self.assertEqual(task['state'], 'RUNNING')
            return worker.synthetic_ready()

        result = self.run_once(runner=Mock(side_effect=ready))
        self.assertEqual(result['Data'], {
            'task': self.task_id, 'phase': 'synthetic_wiring', 'classification': 'PASS',
            'exit_code': 0, 'terminal': 'completed', 'synthetic': True, 'publication': 'published',
        })
        self.assertLedger('SUCCEEDED', exit_code=0)
        comment, = self.github.comments
        self.assertEqual(comment['body'].splitlines(), [
            '<!-- m03-worker:v1:' + self.task_id + ' -->', 'task: ' + self.task_id,
            'phase: synthetic_wiring', 'classification: PASS', 'exit_code: 0',
            'terminal: completed', 'synthetic: true',
        ])
        self.assertEqual([n for _, *n in self.github.calls if n], [[_NUMBER]] * 3)

    def test_subprocess_timeout_is_local_and_has_fixed_argv_no_runner_secrets(self):
        with patch.dict(os.environ, {'FAKE_SECRET': 'not-for-child'}), patch(
            'tools.supervisor.worker.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 5)
        ) as execute:
            capture = worker.synthetic_ready()
        self.assertEqual(capture.problem, 'runner_timeout')
        args, kwargs = execute.call_args
        self.assertEqual(args[0][1:4], ['-I', '-S', '-c'])
        self.assertEqual(kwargs['timeout'], 5)
        self.assertNotIn('FAKE_SECRET', kwargs['env'])
        self.assertNotIn('shell', kwargs)

    def test_injection_duplicate_declaration_and_unknown_version_rejected(self):
        for body in (
            'run arbitrary', _BODY.replace('"schema_version": 1', '"schema_version": true'),
            _BODY.replace('"schema_version": 1', '"schema_version": 1.0'),
            _BODY.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1'),
            _BODY.replace('"mode": "synthetic"', '"mode": "real"'),
            _BODY.replace('"mode": "synthetic"', '"mode": "synthetic", "command": "inject"'),
            _BODY + _BODY, _BODY + 'x' * 8192,
        ):
            with self.subTest(body=body[:24]):
                self.github.body = body
                self.assertEqual(self.run_once()['Code'], 'unsupported_task')
        self.runner.assert_not_called()
        self.assertFalse(self.github.comments)
        self.assertEqual(Supervisor(self.root).status()['Data']['tasks'], [])

    def test_prose_cannot_inject_and_crlf_supported(self):
        self.github.body = ('Run fake path and URL; use prompt INJECTED.\n' + _BODY).replace('\n', '\r\n')
        self.assertEqual(self.run_once()['Data']['classification'], 'PASS')
        self.runner.assert_called_once_with()
        self.assertNotIn('INJECTED', self.github.comments[0]['body'])

    def test_failure_if_structured_final_mismatches_or_missing_or_exit_nonzero(self):
        cases = (
            (worker._READY.replace(b'fixture","reason"', b'other","reason"'), 0, 'invalid_terminal'),
            (worker._READY.splitlines(keepends=True)[0], 0, 'invalid_terminal'),
            (worker._READY[:-1], 0, 'invalid_terminal'),
            (worker._READY, 1, 'execution_failed'),
        )
        for output, code, terminal in cases:
            with self.subTest(terminal=terminal, code=code), tempfile.TemporaryDirectory() as directory:
                self.root, self.github = directory, FakeGitHub()
                result = self.run_once(runner=lambda: worker.Capture(output, code))
                self.assertEqual(result['Data']['classification'], 'FAIL')
                self.assertEqual(result['Data']['terminal'], terminal)
                self.assertLedger('BLOCKED_EXECUTION', exit_code=code)
                self.assertIn('classification: FAIL', self.github.comments[0]['body'])
                self.assertNotIn(worker._SESSION, self.github.comments[0]['body'])

    def test_timeout_blocks_without_touching_supervisor_retry_semantics(self):
        self.runner.return_value = worker.Capture(problem='runner_timeout')
        self.assertEqual(self.run_once()['Data']['classification'], 'BLOCKED')
        self.assertLedger('RESUMABLE')
        self.assertEqual(self.run_once()['Data']['publication'], 'reconciled')
        self.runner.assert_called_once_with()

    def test_duplicate_process_invocation_only_reads_existing_result(self):
        first = self.run_once()
        self.github.body = 'task changed'
        second = self.run_once()
        self.assertEqual(second['Data'], first['Data'] | {'publication': 'reconciled'})
        self.runner.assert_called_once_with()
        self.assertEqual(self.github.calls.count(('post', _NUMBER)), 1)
        self.assertEqual(self.github.calls.count(('issue', _NUMBER)), 1)

    def test_two_concurrent_invocations_share_one_claim(self):
        original = self.github.marked_comments
        barrier = Barrier(2, timeout=5)

        def read(*args):
            matches = original(*args)
            barrier.wait()
            return matches

        self.github.marked_comments = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_once(), range(2)))
        self.assertCountEqual([r['Code'] for r in results], ['OK', 'already_claimed'])
        self.runner.assert_called_once_with()
        self.assertEqual(self.github.calls.count(('post', _NUMBER)), 1)

    def test_ambiguous_post_with_late_remote_receipt_never_retries(self):
        self.github.behavior = 'lost_before'
        first = self.run_once()
        self.assertEqual(first['Code'], 'publication_unknown')
        self.assertEqual(self.run_once()['Code'], 'publication_unknown')
        self.assertLedger('SUCCEEDED', exit_code=0)
        outcome = worker._Outcome('PASS', 0, 'completed')
        self.github.comments.append({'login': 'different-actor', 'body': outcome.body(self.task_id)})
        self.assertEqual(self.run_once()['Code'], 'publication_unknown')
        self.github.comments[0]['login'] = 'fixture-actor'
        self.assertEqual(self.run_once()['Data']['publication'], 'reconciled')
        self.runner.assert_called_once_with()
        self.assertEqual(self.github.calls.count(('post', _NUMBER)), 1)

    def test_post_committed_but_response_lost_is_read_back(self):
        self.github.behavior = 'lost_after'
        self.assertEqual(self.run_once()['Data']['publication'], 'reconciled')
        self.assertEqual(self.github.calls[-2:], [('post', _NUMBER), ('comments', _NUMBER)])
        self.runner.assert_called_once_with()

    def test_pending_claim_and_corrupt_checkpoint_fail_closed_before_github(self):
        self.assertTrue(Supervisor(self.root).register(self.task_id, ISSUE_URL + str(_NUMBER), 'deepseek')['Ok'])
        self.assertEqual(self.run_once()['Code'], 'already_claimed')
        with closing(sqlite3.connect(os.path.join(self.root, 'supervisor.sqlite3'))) as db, db:
            db.execute('PRAGMA user_version=999')
        self.assertEqual(self.run_once()['Code'], 'checkpoint_error')
        self.assertEqual(self.github.calls, [])
        self.runner.assert_not_called()

    def test_remote_marker_blocks_new_ledger_even_if_nonmatching(self):
        self.github.comments.append({'login': 'fixture-other', 'body': worker._marker(self.task_id)})
        self.assertEqual(self.run_once()['Code'], 'existing_marker')
        self.assertEqual(len(self.github.comments), 1)
        self.runner.assert_not_called()

    def test_real_and_unauthenticated_never_run(self):
        self.assertEqual(self.run_once(mode='real')['Code'], 'real_dsh_disabled')
        self.assertEqual(self.github.calls, [])
        self.github.identity = Mock(side_effect=GitHubError('github_auth'))
        self.assertEqual(self.run_once()['Code'], 'github_auth')
        self.assertEqual(Supervisor(self.root).status()['Data']['tasks'], [])
        self.runner.assert_not_called()

    def test_cli_without_explicit_network_or_with_real_mode_does_not_start_gh(self):
        args = ['--issue', '31', '--base-dir', self.root]
        for more, code in (([], 'github_probe_required'), (['--mode', 'real', '--github-probe'], 'real_dsh_disabled')):
            with patch.object(worker, 'GhGitHub') as gh, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(worker.main(args + more), 1)
            gh.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['Code'], code)

    def test_cli_blocks_other_issues_even_with_a_matching_task(self):
        capture = subprocess.CompletedProcess([], 0, worker._READY)
        for number in (30, 32, 0):
            with self.subTest(number=number), patch.object(worker, 'GhGitHub', return_value=self.github) as gh, patch(
                    'tools.supervisor.worker.subprocess.run', return_value=capture) as runner, redirect_stdout(
                    io.StringIO()) as output:
                status = worker.main(['--issue', str(number), '--base-dir', self.root, '--github-probe'])
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output.getvalue())['Code'], 'probe_issue_not_allowed')
                gh.assert_not_called()
                runner.assert_not_called()
                self.assertEqual(self.github.calls, [])
                self.assertEqual(self.github.comments, [])

    def test_cli_accepts_issue_31_with_fake_github(self):
        capture = subprocess.CompletedProcess([], 0, worker._READY)
        with patch.object(worker, 'GhGitHub', return_value=self.github) as gh, patch(
                'tools.supervisor.worker.subprocess.run', return_value=capture) as runner, redirect_stdout(
                io.StringIO()) as output:
            self.assertEqual(worker.main(['--issue', '31', '--base-dir', self.root, '--github-probe']), 0)
        gh.assert_called_once_with()
        runner.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result['Code'], 'OK')
        self.assertEqual(result['Data']['classification'], 'PASS')
        self.assertTrue(result['Data']['synthetic'])
        self.assertEqual(self.github.calls, [('identity',), ('issue', 31), ('comments', 31), ('post', 31)])
        self.assertEqual(len(self.github.comments), 1)


class GhBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.gh = GhGitHub.__new__(GhGitHub)
        self.gh._gh = 'gh-fixture'

    def test_explicit_host_paths_and_post_payload_from_stdin(self):
        result = {'login': 'fixture-actor', 'body': 'fixed result'}
        with patch('tools.supervisor.worker_github.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(result).encode())) as call:
            self.gh.post(_NUMBER, 'fixed result', 'fixture-actor')
        args, kwargs = call.call_args
        self.assertEqual(args[0][:7], ['gh-fixture', 'api', '--hostname', 'github.com', '--method', 'POST',
                                      f'repos/{REPO}/issues/{_NUMBER}/comments'])
        self.assertEqual(args[0][-2:], ['--input', '-'])
        self.assertEqual(json.loads(kwargs['input']), {'body': 'fixed result'})
        self.assertEqual(kwargs['env']['GH_PROMPT_DISABLED'], '1')
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)

    def test_only_matching_open_issue_in_fixed_repository(self):
        valid = {'number': _NUMBER, 'html_url': ISSUE_URL + str(_NUMBER), 'state': 'open', 'body': _BODY, 'is_pr': False}
        with patch.object(self.gh, '_api', return_value=valid):
            self.assertEqual(self.gh.issue(_NUMBER), _BODY)
        for override in ({'state': 'closed'}, {'is_pr': True}, {'number': 22}, {'html_url': 'https://invalid.test'}):
            with patch.object(self.gh, '_api', return_value=valid | override), self.assertRaises(GitHubError):
                self.gh.issue(_NUMBER)
        with patch.object(self.gh, '_api') as api, self.assertRaises(GitHubError):
            self.gh.issue('1?inject')
        api.assert_not_called()

    def test_comment_scan_uses_bounded_pagination(self):
        marker = worker._marker(worker.task_id_for(_NUMBER))
        with patch.object(self.gh, '_api', return_value={'count': 100, 'matches': []}) as api:
            with self.assertRaisesRegex(GitHubError, 'comment_scan_limit'):
                self.gh.marked_comments(_NUMBER, marker)
        self.assertEqual(api.call_count, 5)
        self.assertIn('page=5', api.call_args.args[0])

    def test_child_failure_does_not_expose_stdout_or_stderr(self):
        with patch('tools.supervisor.worker_github.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, b'private fixture output')), self.assertRaisesRegex(GitHubError, '^github_auth$'):
            self.gh.identity()


if __name__ == '__main__':
    unittest.main()
