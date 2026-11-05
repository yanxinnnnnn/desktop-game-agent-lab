"""合成事件/临时数据库/假时钟/Mock 网络；不运行 Agent 或读取真实凭据。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from tools.notifications import NotificationSender
from tools.supervisor import Supervisor, normalize_event
from tools.supervisor import __main__ as cli

_TASK = '00000000-0000-4000-8000-000000000001'
_URL = 'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/18'
_CANARY = 'SCTMOCKONLY000000000000000000'


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'checkpoint'
        self.clock = MagicMock(return_value=1000)
        self.sender = MagicMock()
        self.sender.send.return_value = {'Ok': True, 'Code': 'OK', 'Message': '',
                                        'Data': {'provider_accepted': True, 'wechat_receipt_confirmed': False}}
        self.supervisor = self.restart()
        environment = patch.object(os, 'environ', {})
        environment.start(); self.addCleanup(environment.stop)
        no_network = patch.object(socket, 'socket', side_effect=AssertionError('No networking'))
        no_network.start(); self.addCleanup(no_network.stop)
        no_process = patch('subprocess.Popen', side_effect=AssertionError('No child processes'))
        no_process.start(); self.addCleanup(no_process.stop)
        self.assertOk(self.supervisor.register(_TASK, _URL, 'codex'))

    def restart(self, sender=True):
        return Supervisor(self.base, clock=self.clock, sender=self.sender if sender else None)

    def event(self, kind='finished', **changes):
        event = dict(schema_version='1.0', task_id=_TASK, event_id=str(uuid4()), attempt_no=0,
                     agent='codex', issue_url=_URL, source='synthetic', kind=kind,
                     exit_code=1 if kind == 'finished' else None, http_status=None,
                     signal='none', retry_after_seconds=None, resume_token=None)
        return event | changes

    def start(self, attempt_no=0):
        return self.supervisor.ingest(self.event('started', attempt_no=attempt_no))

    def block(self, signal='quota_exhausted'):
        self.assertOk(self.start())
        event = self.event(signal=signal)
        self.assertOk(self.supervisor.ingest(event))
        return event

    def current(self):
        return self.supervisor.status(_TASK)['Data']['tasks'][0]

    def deliveries(self):
        return self.supervisor.check(_TASK)['Data']['deliveries']

    def assertOk(self, result):
        self.assertTrue(result['Ok'], result)
        self.assertEqual(result['Code'], 'OK')

    def assertCode(self, result, code):
        self.assertFalse(result['Ok'], result)
        self.assertEqual(result['Code'], code, result)
        self.assertNotIn(_CANARY, json.dumps(result))
        self.assertNotIn(str(self.base), json.dumps(result))

    def test_initial_running_and_success_are_offline(self):
        self.assertEqual(self.current()['state'], 'PENDING')
        self.assertOk(self.start())
        self.assertOk(self.supervisor.ingest(self.event(exit_code=0)))
        self.assertEqual(self.current()['state'], 'SUCCEEDED')
        self.assertEqual(self.deliveries(), [])
        self.sender.send.assert_not_called()

    def test_exit_zero_does_not_override_http_failure(self):
        for status, category in ((429, 'provider_rate_limit'), (401, 'auth_failed'), (500, 'unknown_failure')):
            self.assertEqual(normalize_event(self.event(exit_code=0, http_status=status))['Data']['category'], category)
        self.assertEqual(normalize_event(self.event(exit_code=None))['Data']['category'], 'unknown_failure')

    def test_generic_429_is_transient_not_quota_exhaustion(self):
        self.assertOk(self.start())
        self.assertOk(self.supervisor.ingest(self.event(http_status=429, retry_after_seconds=90)))
        task = self.current()
        self.assertEqual((task['state'], task['error_category'], task['retry_count'], task['retry_at']),
                         ('RESUMABLE', 'provider_rate_limit', 1, 1090))
        self.assertEqual(self.deliveries(), [])
        self.assertCode(self.supervisor.resume(_TASK), 'E_RETRY_NOT_DUE')

    def test_explicit_quota_blocks_without_retry(self):
        self.block()
        self.assertEqual(self.current()['state'], 'BLOCKED_QUOTA')
        self.assertIsNone(self.current()['retry_at'])
        self.assertEqual(self.deliveries()[0]['event_type'], 'BLOCKED_QUOTA')
        self.assertCode(self.supervisor.resume(_TASK), 'E_FOUNDER_REQUIRED')

    def test_auth_and_unknown_sdk_errors(self):
        for status in (401, 403):
            self.assertEqual(normalize_event(self.event(http_status=status))['Data']['category'], 'auth_failed')
        self.assertEqual(normalize_event(self.event(signal='sdk_fault'))['Data']['category'], 'unknown_failure')
        self.block('auth_failed')
        self.assertEqual(self.current()['state'], 'BLOCKED_AUTH')
        self.assertEqual(self.deliveries()[0]['event_type'], 'BLOCKED_AUTH')

    def test_unknown_failure_is_unrecoverable(self):
        self.block('sdk_fault')
        self.assertEqual(self.current()['state'], 'BLOCKED_EXECUTION')
        self.assertEqual(self.current()['error_category'], 'unknown_failure')

    def test_adapter_exception_is_sanitized_and_not_recorded(self):
        adapter = MagicMock()
        adapter.parse.side_effect = RuntimeError(_CANARY)
        self.assertCode(self.supervisor.ingest({}, adapter=adapter), 'E_ADAPTER')
        self.assertEqual(self.supervisor.check()['Data']['events'], [])

    def test_adapter_output_cannot_bypass_allowlist(self):
        adapter = MagicMock()
        adapter.parse.return_value = {'Ok': True, 'Data': self.event() | {'raw_log': _CANARY}}
        self.assertCode(self.supervisor.ingest({}, adapter=adapter), 'E_EVENT_INVALID')
        self.assertEqual(self.supervisor.check()['Data']['events'], [])

    def test_strict_metadata_and_raw_text_rejection(self):
        for change in ({'raw_log': _CANARY}, {'signal': _CANARY}, {'agent': 'personal-name'},
                       {'resume_token': _CANARY}, {'task_id': 'local-private-path'},
                       {'issue_url': _URL + '?secret=' + _CANARY}, {'retry_after_seconds': -1},
                       {'exit_code': True}, {'attempt_no': -1}, {'http_status': 999},
                       {'category': 'quota_exhausted'}):
            self.assertCode(self.supervisor.ingest(self.event() | change), 'E_EVENT_INVALID')
        self.assertNotIn(_CANARY.encode(), (self.base / 'supervisor.sqlite3').read_bytes())

    def test_event_task_agent_issue_and_attempt_correlation(self):
        self.assertOk(self.start())
        for change, code in [({'task_id': str(uuid4())}, 'E_TASK_NOT_FOUND'),
                             ({'agent': 'deepseek'}, 'E_EVENT_CORRELATION'),
                             ({'issue_url': _URL[:-2] + '19'}, 'E_EVENT_CORRELATION'),
                             ({'attempt_no': 1}, 'E_STALE_EVENT')]:
            self.assertCode(self.supervisor.ingest(self.event() | change), code)
        self.assertEqual(self.current()['state'], 'RUNNING')

    def test_event_duplicates_and_conflicts_survive_restart(self):
        event = self.block()
        restarted = self.restart()
        self.assertCode(restarted.ingest(event), 'E_DUPLICATE_EVENT')
        self.assertCode(restarted.ingest(event | {'signal': 'auth_failed'}), 'E_EVENT_CONFLICT')
        self.assertEqual(len(self.deliveries()), 1)

    def test_new_ids_for_same_blocked_episode_do_not_duplicate_alerts(self):
        event = self.block()
        self.assertOk(self.restart().ingest(event | {'event_id': str(uuid4())}))
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.deliveries()[0]['event_id'], event['event_id'])

    def test_repeated_transient_logs_do_not_consume_more_retries(self):
        self.assertOk(self.start())
        event = self.event(http_status=429)
        self.assertOk(self.supervisor.ingest(event))
        self.clock.return_value = 1010
        self.assertOk(self.restart().ingest(event | {'event_id': str(uuid4())}))
        self.assertEqual((self.current()['retry_count'], self.current()['retry_at']), (1, 1030))

    def test_backoff_budget_survives_restart_and_never_launches_client(self):
        self.assertOk(self.start())
        for attempt in range(4):
            result = self.supervisor.ingest(self.event(signal='timeout', attempt_no=attempt))
            self.assertOk(result)
            task = self.current()
            if attempt < 3:
                self.assertEqual(task['state'], 'RESUMABLE')
                self.assertEqual(task['retry_at'] - self.clock.return_value, 30 * 2**attempt)
                self.clock.return_value = task['retry_at']
                self.supervisor = self.restart()
                self.assertTrue(self.current()['resume_ready'])
                self.assertOk(self.supervisor.resume(_TASK))
                self.assertOk(self.start(attempt + 1))
            else:
                self.assertEqual(task['state'], 'BLOCKED_EXECUTION')
                self.assertEqual(task['retry_count'], 3)
                self.assertEqual(len(self.deliveries()), 1)

    def test_retry_after_beyond_deadline_does_not_retry_early(self):
        self.assertOk(self.start())
        self.assertOk(self.supervisor.ingest(self.event(http_status=429, retry_after_seconds=4000)))
        self.assertEqual(self.current()['state'], 'BLOCKED_EXECUTION')
        self.assertEqual(self.current()['error_category'], 'provider_rate_limit')

    def test_expired_resume_window_becomes_blocked_and_prepares_alert(self):
        self.assertOk(self.start())
        self.assertOk(self.supervisor.ingest(self.event(signal='timeout')))
        self.clock.return_value = 5000
        self.assertFalse(self.current()['resume_ready'])
        self.assertCode(self.supervisor.resume(_TASK), 'E_RETRY_DEADLINE')
        self.assertEqual(self.current()['state'], 'BLOCKED_EXECUTION')
        self.assertEqual(len(self.deliveries()), 1)

    def test_pause_acknowledged_resume_and_late_event_rejection(self):
        self.assertOk(self.start())
        self.assertOk(self.supervisor.pause(_TASK))
        self.assertEqual(self.current()['state'], 'WAITING_FOUNDER')
        self.assertCode(self.supervisor.resume(_TASK), 'E_FOUNDER_REQUIRED')
        self.assertOk(self.supervisor.resume(_TASK, acknowledge=True))
        self.assertEqual(self.current()['state'], 'PENDING')
        self.assertEqual(self.current()['attempt_no'], 1)
        self.assertCode(self.supervisor.ingest(self.event()), 'E_STALE_EVENT')

    def test_resume_keeps_opaque_token_and_cancels_old_unsent_alert(self):
        token = str(uuid4())
        self.assertOk(self.start())
        self.assertOk(self.supervisor.ingest(self.event(signal='quota_exhausted', resume_token=token)))
        self.assertOk(self.supervisor.resume(_TASK, acknowledge=True))
        self.assertEqual(self.current()['resume_token'], token)
        self.assertEqual(self.deliveries()[0]['status'], 'FAILED')
        self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_NO_ALERT')

    def test_founder_required_templates(self):
        for kind in ('founder_game_action_required', 'founder_merge_required'):
            other = str(uuid4())
            self.assertOk(self.supervisor.register(other, _URL, 'deepseek'))
            self.assertOk(self.supervisor.ingest(self.event(kind, task_id=other, agent='deepseek')))
            alert = self.supervisor.check(other)['Data']['deliveries'][0]
            self.assertEqual(alert['event_type'], kind.upper())
            self.assertEqual(self.supervisor.status(other)['Data']['tasks'][0]['state'], 'WAITING_FOUNDER')

    def test_notification_opt_in_and_sender_absence(self):
        self.block()
        self.assertCode(self.supervisor.notify(_TASK), 'E_DISABLED')
        self.assertCode(self.restart(sender=False).notify(_TASK, enabled=True), 'E_NOTIFIER_UNAVAILABLE')
        self.assertEqual(self.deliveries()[0]['status'], 'PREPARED')
        self.sender.send.assert_not_called()

    def test_notification_acceptance_and_durable_dedup(self):
        event = self.block()
        result = self.supervisor.notify(_TASK, enabled=True)
        self.assertOk(result)
        self.assertTrue(result['Data']['api_accepted'])
        self.assertFalse(result['Data']['wechat_receipt_confirmed'])
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.sender.send.assert_called_once()
        sent = self.sender.send.call_args.args[0]
        self.assertEqual(sent['event_id'], event['event_id'])
        self.assertEqual(sent['event_type'], 'BLOCKED_QUOTA')
        self.assertEqual(sent['github_url'], _URL)
        self.assertEqual(sent['agent'], 'codex')

    def test_notification_timeout_is_uncertain_never_retried(self):
        self.block()
        self.sender.send.return_value = {'Ok': False, 'Code': 'E_TIMEOUT', 'Message': _CANARY, 'Data': None}
        self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_NOTIFICATION_UNCERTAIN')
        self.assertEqual(self.deliveries()[0]['status'], 'UNKNOWN')
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.sender.send.assert_called_once()
        self.assertNotIn(_CANARY.encode(), (self.base / 'supervisor.sqlite3').read_bytes())

    def test_crash_after_reservation_does_not_resend_on_restart(self):
        self.block()
        self.sender.send.side_effect = SystemExit(99)
        with self.assertRaises(SystemExit):
            self.supervisor.notify(_TASK, enabled=True)
        self.assertEqual(self.deliveries()[0]['status'], 'RESERVED')
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.sender.send.assert_called_once()

    def test_concurrent_supervisors_claim_delivery_only_once(self):
        self.block()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.restart().notify(_TASK, enabled=True), range(2)))
        self.assertEqual(sum(r['Ok'] for r in results), 1)
        self.sender.send.assert_called_once()

    def test_missing_credentials_preflight_has_bounded_delayed_retries(self):
        self.block()
        self.sender.send.return_value = {'Ok': False, 'Code': 'E_CREDENTIAL_MISSING', 'Message': _CANARY, 'Data': None}
        for count in range(3):
            expected = 'E_NOTIFICATION_DEFERRED' if count < 2 else 'E_NOTIFICATION_FAILED'
            self.assertCode(self.restart().notify(_TASK, enabled=True), expected)
            if count < 2:
                self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_NOTIFICATION_NOT_DUE')
                self.clock.return_value += 300
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.assertEqual(self.sender.send.call_count, 3)

    def test_unexpected_sender_failure_is_sanitized(self):
        self.block()
        self.sender.send.side_effect = RuntimeError(_CANARY)
        self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_NOTIFICATION_UNCERTAIN')
        self.assertEqual(self.deliveries()[0]['status'], 'UNKNOWN')

    def test_real_m01_sender_is_consumed_with_mock_http_only(self):
        self.block()
        response = MagicMock(status=200)
        response.read.return_value = b'{"code":0}'
        with patch.object(os, 'environ', {'SERVERCHAN_SENDKEY': _CANARY}), \
                patch('tools.notifications.serverchan.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertOk(Supervisor(self.base, clock=self.clock, sender=NotificationSender()).notify(_TASK, enabled=True))
            opener.return_value.open.assert_called_once()

    def test_database_corruption_fails_closed_without_recreation(self):
        db = self.base / 'supervisor.sqlite3'
        db.write_bytes(b'synthetic corrupt checkpoint')
        self.assertCode(self.supervisor.status(), 'E_DB_INVALID')
        self.assertEqual(db.read_bytes(), b'synthetic corrupt checkpoint')
        self.sender.send.assert_not_called()

    def test_missing_delivery_table_is_not_silently_rebuilt(self):
        with sqlite3.connect(self.base / 'supervisor.sqlite3') as connection:
            connection.execute('DROP TABLE deliveries')
        self.assertCode(self.supervisor.status(), 'E_DB_INVALID')

    def test_atomic_rollback_if_alert_creation_fails(self):
        self.assertOk(self.start())
        with sqlite3.connect(self.base / 'supervisor.sqlite3') as connection:
            connection.execute("CREATE TRIGGER fail_delivery BEFORE INSERT ON deliveries BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        self.assertCode(self.supervisor.ingest(self.event(signal='quota_exhausted')), 'E_DB_INVALID')
        self.assertEqual(self.current()['state'], 'RUNNING')
        self.assertEqual(len(self.supervisor.check()['Data']['events']), 1)

    def test_reject_checkpoint_inside_any_git_root_and_database_symlink(self):
        fake_repo = Path(self.temp.name) / 'repo'
        fake_repo.mkdir(); (fake_repo / '.git').write_text('synthetic git marker')
        self.assertCode(Supervisor(fake_repo / 'state').status(), 'E_STORAGE_LOCATION')
        link_dir = Path(self.temp.name) / 'linked'; link_dir.mkdir()
        try:
            (link_dir / 'supervisor.sqlite3').symlink_to(self.base / 'supervisor.sqlite3')
        except (OSError, NotImplementedError):
            self.skipTest('该平台未授权创建 symlink；不更改 Windows 配置')
        self.assertCode(Supervisor(link_dir).status(), 'E_STORAGE_LOCATION')

    def test_check_reads_safe_events_without_action(self):
        self.block()
        before = self.supervisor.check()
        after = self.supervisor.check()
        self.assertEqual(before, after)
        self.sender.send.assert_not_called()
        self.assertNotIn(str(self.base), json.dumps(after))

    def test_cli_simulate_status_check_and_notify_default_are_offline(self):
        commands = [('simulate', '--case', 'quota'), ('status',), ('check',),
                    ('notify', '--task-id', _TASK)]
        for command in commands:
            output = io.StringIO()
            with patch('sys.argv', ['supervisor', '--base-dir', str(self.base), *command]), \
                    redirect_stdout(output), patch('tools.supervisor.__main__.NotificationSender') as constructor:
                code = cli._main()
            constructor.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(code, 1 if command[0] == 'notify' else 0)
            self.assertNotIn(str(self.base), output.getvalue())
            if command[0] == 'notify':
                self.assertEqual(result['Code'], 'E_DISABLED')

    def test_cli_invalid_arguments_do_not_echo_private_input(self):
        output = io.StringIO()
        with patch('sys.argv', ['supervisor', '--sendkey', _CANARY]), redirect_stderr(output):
            with self.assertRaises(SystemExit):
                cli._main()
        self.assertNotIn(_CANARY, output.getvalue())

    def test_provider_rejection_is_final_not_automatically_retried(self):
        self.block()
        self.sender.send.return_value = {'Ok': False, 'Code': 'E_PROVIDER', 'Message': _CANARY}
        self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_NOTIFICATION_FAILED')
        self.assertEqual(self.deliveries()[0]['status'], 'FAILED')
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.sender.send.assert_called_once()

    def test_sender_cannot_claim_wechat_receipt(self):
        self.block()
        self.sender.send.return_value['Data']['wechat_receipt_confirmed'] = True
        result = self.supervisor.notify(_TASK, enabled=True)
        self.assertOk(result)
        self.assertFalse(result['Data']['wechat_receipt_confirmed'])
        self.assertEqual(self.deliveries()[0]['wechat_receipt_confirmed'], 0)

    def test_database_write_failure_after_send_preserves_reservation(self):
        self.block()
        original = self.supervisor._store.transaction
        def fail_after_send(*args, **kwargs):
            if self.sender.send.called:
                raise sqlite3.DatabaseError('synthetic checkpoint fault')
            return original(*args, **kwargs)
        with patch.object(self.supervisor._store, 'transaction', side_effect=fail_after_send):
            self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_DB_INVALID')
        self.assertEqual(self.deliveries()[0]['status'], 'RESERVED')
        self.assertCode(self.restart().notify(_TASK, enabled=True), 'E_DELIVERY_FINAL')
        self.sender.send.assert_called_once()

    def test_concurrent_ingestion_commits_one_event_and_alert(self):
        self.assertOk(self.start())
        event = self.event(signal='quota_exhausted')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.restart().ingest(event), range(2)))
        self.assertEqual(sorted(r['Code'] for r in results), ['E_DUPLICATE_EVENT', 'OK'])
        self.assertEqual(len(self.deliveries()), 1)

    def test_unsupported_database_version_and_lock_fail_closed(self):
        db = self.base / 'supervisor.sqlite3'
        with sqlite3.connect(db) as connection:
            connection.execute('PRAGMA user_version=99')
        self.assertCode(self.supervisor.check(), 'E_DB_INVALID')
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)
            connection.execute('PRAGMA user_version=1')
        # 模拟 SQLITE_BUSY，避免测试依赖真实 5 秒阻塞时长。
        busy = sqlite3.OperationalError('synthetic busy')
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch('tools.supervisor.storage.sqlite3.connect', side_effect=busy):
            self.assertCode(self.supervisor.status(), 'E_DB_BUSY')

    def test_git_directory_marker_empty_placeholder_and_unc_location(self):
        repo = Path(self.temp.name) / 'git-repo'; repo.mkdir()
        (repo / '.git').mkdir(); (repo / '.git' / 'HEAD').write_text('ref: refs/heads/main')
        self.assertCode(Supervisor(repo / 'checkpoint').status(), 'E_STORAGE_LOCATION')
        empty = Path(self.temp.name) / 'empty-platform-marker'; empty.mkdir(); (empty / '.git').mkdir()
        self.assertOk(Supervisor(empty / 'checkpoint').status())
        self.assertCode(Supervisor('//synthetic-host/share').status(), 'E_STORAGE_LOCATION')

    def test_register_and_transition_errors_do_not_overwrite_state(self):
        self.assertCode(self.supervisor.register(_TASK, _URL, 'deepseek'), 'E_TASK_EXISTS')
        self.assertCode(self.supervisor.ingest(self.event(exit_code=0)), 'E_STATE_TRANSITION')
        self.assertEqual(self.current()['state'], 'PENDING')
        self.assertEqual(self.supervisor.check()['Data']['events'], [])

    def test_cli_explicit_notify_uses_injected_mock_sender(self):
        self.block()
        output = io.StringIO()
        args = ['supervisor', '--base-dir', str(self.base), 'notify', '--task-id', _TASK,
                '--enable-notifications']
        with patch('sys.argv', args), redirect_stdout(output), \
                patch('tools.supervisor.__main__.NotificationSender', return_value=self.sender):
            self.assertEqual(cli._main(), 0)
        self.assertEqual(json.loads(output.getvalue())['Data']['status'], 'ACCEPTED')
        self.sender.send.assert_called_once()

    def test_logically_corrupt_journal_does_not_echo_arbitrary_text(self):
        event = self.block()
        with sqlite3.connect(self.base / 'supervisor.sqlite3') as connection:
            connection.execute('UPDATE events SET fingerprint=? WHERE event_id=?', (_CANARY, event['event_id']))
        self.assertCode(self.supervisor.check(), 'E_DB_INVALID')

    def test_malformed_sender_result_is_uncertain(self):
        self.block()
        self.sender.send.return_value = {'Ok': False, 'Code': [_CANARY], 'Message': _CANARY}
        self.assertCode(self.supervisor.notify(_TASK, enabled=True), 'E_NOTIFICATION_UNCERTAIN')
        self.assertEqual(self.deliveries()[0]['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
