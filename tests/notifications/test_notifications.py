"""全部为合成事件、假凭据和 Mock HTTP；不读取真实 SendKey，不发送真实请求。"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from tools.notifications import NotificationSender, build_event
from tools.notifications import __main__ as cli
from tools.notifications.serverchan import _NoRedirect

_FAKE_KEY = 'SCTMOCKONLY000000000000000000'
_URL = 'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/16'
_ID = '00000000-0000-4000-8000-000000000001'


class Notifications(unittest.TestCase):
    def setUp(self):
        # 替换环境映射，不复制/读取当前进程可能存在的真实凭据值。
        self.environment = patch.object(os, 'environ', {'SERVERCHAN_SENDKEY': _FAKE_KEY})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.socket_guard = patch.object(socket, 'socket', side_effect=AssertionError('No networking in tests'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.open_patch = patch('tools.notifications.serverchan.build_opener')
        self.builder = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.response = MagicMock()
        self.response.status = 200
        self.response.read.return_value = b'{"code": 0}'
        self.builder.return_value.open.return_value.__enter__.return_value = self.response
        self.clock = patch('tools.notifications.serverchan.time.monotonic', return_value=1000)
        self.time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sender = NotificationSender()
        self.event = build_event('NOTIFIER_TEST', _ID, _URL, 'codex')['Data']

    def fresh(self, number=2, event_type='NOTIFIER_TEST'):
        identifier = f'00000000-0000-4000-8000-{number:012d}'
        return build_event(event_type, identifier, _URL, 'codex')['Data']

    def send(self, event=None):
        return self.sender.send(self.event if event is None else event, enabled=True)

    def assertFailure(self, result, code):
        self.assertFalse(result['Ok'], result)
        self.assertEqual(result['Code'], code)
        self.assertNotIn(_FAKE_KEY, json.dumps(result, ensure_ascii=False))

    def test_all_event_templates_are_standard_and_safe(self):
        for kind in ('FOUNDER_MERGE_REQUIRED', 'BLOCKED_QUOTA', 'BLOCKED_AUTH',
                     'BLOCKED_EXECUTION', 'FOUNDER_GAME_ACTION_REQUIRED', 'NOTIFIER_TEST'):
            for agent in ('codex', 'deepseek', 'cto', 'founder', 'system'):
                result = build_event(kind, _ID, _URL, agent)
                self.assertTrue(result['Ok'], result)
                event = result['Data']
                for field in ('event_id', 'event_type', 'title', 'agent', 'task', 'state',
                              'next_human_action', 'github_url'):
                    self.assertTrue(event[field])
                self.assertLessEqual(len(event['title']), 32)
        event = build_event('FOUNDER_MERGE_REQUIRED', _ID, _URL.replace('issues/16', 'pull/17'), 'codex')['Data']
        self.assertEqual(event['task'], 'PR #17')
        self.builder.assert_not_called()

    def test_success_is_provider_acceptance_not_wechat_receipt(self):
        result = self.send()
        self.assertEqual(result, {'Ok': True, 'Code': 'OK', 'Message': '',
            'Data': {'provider_accepted': True, 'wechat_receipt_confirmed': False}})
        request = self.builder.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://sctapi.ftqq.com/' + _FAKE_KEY + '.send')
        self.assertEqual(request.get_method(), 'POST')
        payload = parse_qs(request.data.decode('utf-8'))
        self.assertEqual(set(payload), {'title', 'desp'})
        self.assertEqual(payload['title'], [self.event['title']])
        for value in (self.event['github_url'], self.event['agent'], self.event['task'],
                      self.event['state'], self.event['next_human_action']):
            self.assertIn(value, payload['desp'][0])
        self.assertNotIn(_FAKE_KEY, request.data.decode())
        self.assertEqual(self.builder.return_value.open.call_args.kwargs['timeout'], 10)
        self.assertIsInstance(self.builder.call_args.args[0], _NoRedirect)
        self.response.read.assert_called_once_with(8193)

    def test_disabled_does_not_even_read_credentials(self):
        with patch.object(os, 'environ', MagicMock(get=MagicMock(side_effect=AssertionError('Must not read')))):
            self.assertFailure(self.sender.send(self.event), 'E_DISABLED')
            self.assertFailure(self.sender.send(self.event, enabled='true'), 'E_DISABLED')
        self.builder.assert_not_called()

    def test_absent_and_invalid_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFailure(self.send(), 'E_CREDENTIAL_MISSING')
        for bad in ('', 'not-a-sendkey', 'SCTbad/key', 'SCTbad?query', 'SCTbad\nheader', 'SCT' + 'x'*201):
            with patch.dict(os.environ, {'SERVERCHAN_SENDKEY': bad}, clear=True):
                code = 'E_CREDENTIAL_MISSING' if not bad else 'E_CREDENTIAL_INVALID'
                self.assertFailure(self.send(), code)
        self.builder.assert_not_called()

    def test_nonzero_code_does_not_leak_provider_message(self):
        self.response.read.return_value = json.dumps({'code': 40001, 'message': _FAKE_KEY}).encode()
        result = self.send()
        self.assertFailure(result, 'E_PROVIDER')
        self.assertNotIn('40001', json.dumps(result))
        self.builder.return_value.open.assert_called_once()

    def test_malformed_missing_boolean_string_code_and_large_response(self):
        for number, raw in enumerate((b'not-json', b'[]', b'{}', b'{"code":false}',
                                      b'{"code":"0"}', b'\xff', b'x'*8193), 2):
            self.response.read.return_value = raw
            sender = NotificationSender()
            self.assertFailure(sender.send(self.fresh(number), enabled=True), 'E_RESPONSE')

    def test_http_status_without_exception_is_failure(self):
        self.response.status = 503
        self.assertFailure(self.send(), 'E_HTTP')
        self.response.read.assert_not_called()

    def test_transport_timeout_and_tls_failures_no_traceback_or_retry(self):
        for error, code in [(TimeoutError(_FAKE_KEY), 'E_TIMEOUT'),
                            (URLError(TimeoutError(_FAKE_KEY)), 'E_TIMEOUT'),
                            (URLError(_FAKE_KEY), 'E_TRANSPORT'),
                            (URLError(ssl.SSLCertVerificationError(_FAKE_KEY)), 'E_TRANSPORT'),
                            (RuntimeError(_FAKE_KEY), 'E_INTERNAL')]:
            with self.subTest(code=code):
                opener = self.builder.return_value.open
                opener.reset_mock()
                opener.side_effect = error
                result = NotificationSender().send(self.event, enabled=True)
                self.assertFailure(result, code)
                opener.assert_called_once()

    def test_http_error_suppresses_full_url_headers_and_body(self):
        error = HTTPError('https://sctapi.ftqq.com/' + _FAKE_KEY + '.send', 403,
                          _FAKE_KEY, {'X-secret': _FAKE_KEY}, io.BytesIO(_FAKE_KEY.encode()))
        self.builder.return_value.open.side_effect = error
        self.assertFailure(self.send(), 'E_HTTP')

    def test_redirect_handler_never_follows_even_same_origin(self):
        handler = _NoRedirect()
        for target in ('https://untrusted.invalid/', 'https://sctapi.ftqq.com/another'):
            self.assertIsNone(handler.redirect_request(None, None, 302, 'redirect', {}, target))
        self.builder.return_value.open.side_effect = HTTPError('mock-url', 302, _FAKE_KEY, {}, None)
        self.assertFailure(self.send(), 'E_HTTP')
        self.builder.return_value.open.assert_called_once()

    def test_dedup_by_event_id_including_failed_attempts(self):
        self.builder.return_value.open.side_effect = TimeoutError(_FAKE_KEY)
        self.assertFailure(self.send(), 'E_TIMEOUT')
        changed_type = self.fresh(1, 'BLOCKED_AUTH')
        self.assertEqual(changed_type['event_id'], self.event['event_id'])
        self.assertFailure(self.send(changed_type), 'E_DUPLICATE_EVENT')
        self.builder.return_value.open.assert_called_once()

    def test_quota_alert_cooldown_at_exact_boundary(self):
        self.assertTrue(self.send(self.fresh(2, 'BLOCKED_QUOTA'))['Ok'])
        self.time.return_value = 1299
        self.assertFailure(self.send(self.fresh(3, 'BLOCKED_QUOTA')), 'E_COOLDOWN')
        self.time.return_value = 1300
        self.assertTrue(self.send(self.fresh(3, 'BLOCKED_QUOTA'))['Ok'])
        self.assertEqual(self.builder.return_value.open.call_count, 2)

    def test_quota_cooldown_does_not_block_other_event_types(self):
        self.assertTrue(self.send(self.fresh(2, 'BLOCKED_QUOTA'))['Ok'])
        self.assertTrue(self.send(self.fresh(3, 'BLOCKED_AUTH'))['Ok'])

    def test_provider_429_cools_down_all_event_types(self):
        self.builder.return_value.open.side_effect = HTTPError('mock-url', 429, _FAKE_KEY, {}, None)
        self.assertFailure(self.send(), 'E_RATE_LIMIT')
        self.assertFailure(self.send(self.fresh(2, 'BLOCKED_AUTH')), 'E_COOLDOWN')
        self.time.return_value = 1300
        self.builder.return_value.open.side_effect = None
        self.assertTrue(self.send(self.fresh(2, 'BLOCKED_AUTH'))['Ok'])
        self.assertEqual(self.builder.return_value.open.call_count, 2)

    def test_maximum_five_attempts_per_sender_and_zero_retries(self):
        self.builder.return_value.open.side_effect = URLError(_FAKE_KEY)
        for number in range(1, 6):
            self.assertFailure(self.send(self.fresh(number)), 'E_TRANSPORT')
        self.assertFailure(self.send(self.fresh(6)), 'E_SEND_LIMIT')
        self.assertEqual(self.builder.return_value.open.call_count, 5)

    def test_rate_state_is_instance_memory_not_a_daily_guarantee(self):
        self.assertTrue(self.send()['Ok'])
        self.assertTrue(NotificationSender().send(self.event, enabled=True)['Ok'])
        self.assertEqual(self.builder.return_value.open.call_count, 2)

    def test_title_empty_control_oversized_and_untrusted_rejected(self):
        for title, code in [('', 'E_TITLE_INVALID'), ('x'*33, 'E_TITLE_INVALID'),
                            ('unsafe\n' + _FAKE_KEY, 'E_TITLE_INVALID'),
                            ('arbitrary text', 'E_EVENT_INVALID')]:
            event = deepcopy(self.event); event['title'] = title
            self.assertFailure(self.send(event), code)
        self.builder.assert_not_called()

    def test_privacy_whitelist_rejects_paths_names_tokens_and_free_payloads(self):
        for field in ('next_human_action', 'task', 'state', 'message', 'desp', 'username', 'screenshot'):
            event = deepcopy(self.event)
            event[field] = 'synthetic-private-path-and-account-canary ' + _FAKE_KEY
            result = self.send(event)
            self.assertFailure(result, 'E_EVENT_INVALID')
            self.assertNotIn('synthetic-private', json.dumps(result))
        self.builder.assert_not_called()

    def test_external_urls_credentials_queries_and_fragments_rejected(self):
        for url in (_URL + '?secret=' + _FAKE_KEY, _URL + '#comment', _URL.replace('https:', 'http:'),
                    _URL.replace('github.com/', 'github.com.evil.invalid/'),
                    _URL.replace('github.com/', 'user@github.com/'),
                    _URL.replace('issues/16', 'issues/../16'),
                    _URL.replace('yanxinnnnnn', 'other'), _URL + '\n', None):
            self.assertFailure(build_event('NOTIFIER_TEST', _ID, url, 'codex'), 'E_EVENT_INVALID')
        self.builder.assert_not_called()

    def test_event_id_and_agent_cannot_carry_arbitrary_metadata(self):
        for identifier in (_FAKE_KEY, 'synthetic-user-name', None, 42):
            self.assertFailure(build_event('NOTIFIER_TEST', identifier, _URL, 'codex'), 'E_EVENT_INVALID')
        self.assertFailure(build_event('NOTIFIER_TEST', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA', _URL, 'codex'), 'E_EVENT_INVALID')
        self.assertFailure(build_event('NOTIFIER_TEST', _ID, _URL, 'synthetic-real-name'), 'E_EVENT_INVALID')
        self.assertFailure(build_event('UNKNOWN_EVENT', _ID, _URL, 'codex'), 'E_EVENT_INVALID')

    def test_caller_event_is_not_mutated(self):
        before = deepcopy(self.event)
        self.assertTrue(self.send()['Ok'])
        self.assertEqual(self.event, before)

    def test_cli_default_dry_run_reads_no_key_or_network(self):
        output = io.StringIO()
        def no_key_read(name, default=None):
            if name == 'SERVERCHAN_SENDKEY':
                raise AssertionError('No key reads')
            return default
        with patch('sys.argv', ['notifications']), redirect_stdout(output), \
                patch.object(os, 'environ', MagicMock(get=MagicMock(side_effect=no_key_read))):
            self.assertEqual(cli._main(), 0)
        self.assertEqual(json.loads(output.getvalue())['Code'], 'DRY_RUN')
        self.builder.assert_not_called()

    def test_cli_failure_prints_only_safe_result(self):
        self.builder.return_value.open.side_effect = RuntimeError(_FAKE_KEY)
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['notifications', '--send']), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli._main(), 1)
        self.assertNotIn(_FAKE_KEY, output.getvalue() + errors.getvalue())
        self.assertNotIn('Traceback', output.getvalue() + errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())['Code'], 'E_INTERNAL')

    def test_cli_api_acceptance_exits_zero(self):
        output = io.StringIO()
        with patch('sys.argv', ['notifications', '--send']), redirect_stdout(output):
            self.assertEqual(cli._main(), 0)
        self.assertFalse(json.loads(output.getvalue())['Data']['wechat_receipt_confirmed'])

    def test_cli_mistaken_secret_argument_is_not_echoed(self):
        output = io.StringIO()
        with patch('sys.argv', ['notifications', '--sendkey', _FAKE_KEY]), redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                cli._main()
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn(_FAKE_KEY, output.getvalue())
        self.builder.assert_not_called()

    def test_workflow_static_security_contract(self):
        workflow = Path('.github/workflows/notifier-manual-test.yml').read_text()
        self.assertIn('workflow_dispatch:', workflow)
        for automatic in ('pull_request:', 'pull_request_target:', 'push:', 'schedule:', 'workflow_run:'):
            self.assertNotIn(automatic, workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('github.ref_protected', workflow)
        self.assertIn("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", workflow)
        self.assertIn('github.actor == github.repository_owner', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        self.assertEqual(workflow.count('${{ secrets.'), 1)
        secret_position = workflow.index('SERVERCHAN_SENDKEY: ${{ secrets.SERVERCHAN_SENDKEY }}')
        self.assertGreater(secret_position, workflow.index('name: Send one fixed NOTIFIER_TEST'))
        self.assertNotIn('${{ inputs.', workflow)


if __name__ == '__main__':
    unittest.main()
