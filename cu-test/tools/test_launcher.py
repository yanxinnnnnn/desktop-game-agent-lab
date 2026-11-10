"""真实 PowerShell 进程调用；独立临时目录与 stub 写入事实断言。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from test_no_tools_preflight import fixture

ENGINE = None


def quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def encoded(script):
    return base64.b64encode(script.encode('utf-16le')).decode('ascii')


def command(script, engine=None):
    return [engine or ENGINE, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded(script)]


def resolve_engine(spec):
    for name in (spec,) if spec is not None else ('powershell', 'pwsh'):
        try:
            path = Path(name)
            if not path.is_absolute():
                # 裸命令名沿用系统可执行文件搜索规则，显式文件路径必须为绝对路径。
                found = shutil.which(name) if path.name == name else None
                if not found:
                    continue
                path = Path(found)
            if not path.is_absolute() or not path.is_file() or (os.name == 'nt' and path.suffix.lower() != '.exe'):
                continue
            script = "'OFFLINE_ENGINE=' + $PSVersionTable.PSVersion.Major + '.' + $PSVersionTable.PSVersion.Minor"
            probe = subprocess.run(command(script, str(path)), capture_output=True, text=True, timeout=8)
            version = re.fullmatch(r'OFFLINE_ENGINE=(\d+)\.(\d+)', probe.stdout.strip())
            if probe.returncode == 0 and version and tuple(map(int, version.groups())) >= (5, 1):
                return str(path)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return None


class SyntheticCase(unittest.TestCase):
    def setUp(self):
        if not ENGINE:
            self.skipTest('PowerShell runtime unavailable; no launcher/Guard execution')
        self.tmp = tempfile.TemporaryDirectory(prefix="offline launcher ' ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'exp'
        self.cfg = fixture(self.root)
        self.hash = hashlib.sha256(Path(self.cfg['base']).read_bytes()).hexdigest()
        self.manifest = self.root / 'manifest.json'
        self.marker = self.root / 'stub-marker.json'
        self.counter = self.root / 'stub-starts.txt'

    def invoke_script(self, script, timeout=40):
        return subprocess.run(command(script), capture_output=True, text=True, timeout=timeout)

    def phase_script(self, phase, mode='stub', tools=TOOLS, root=None, extra='', expected=None):
        result = "$ErrorActionPreference='Stop'; $sb=[scriptblock]::Create((Get-Content -LiteralPath " + quote(tools / 'joint_experiment.ps1') + " -Raw -Encoding UTF8)); & $sb -ToolsDir " + quote(tools) + ' -Phase ' + phase + ' -ExpRoot ' + quote(root or self.root) + ' -Mode ' + mode
        if phase == 'preflight':
            result += ' -ConfigDir ' + quote(self.root / 'config') + ' -ExpectedBaseSha256 ' + quote(self.hash if expected is None else expected)
        return result + ' ' + extra

    def phase(self, phase, **kwargs):
        return self.invoke_script(self.phase_script(phase, **kwargs))

    def ready(self):
        p = self.phase('preflight')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.manifest.is_file(), p.stdout + p.stderr)
        self.assertEqual(json.loads(self.manifest.read_text(encoding='utf-8-sig'))['preflight'], 'PASS')

    def count(self):
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def zero(self):
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.count(), 0)

    def blocked(self, p):
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.zero()

    def mutate_manifest(self, **values):
        m = json.loads(self.manifest.read_text(encoding='utf-8-sig'))
        m.update(values)
        self.manifest.write_text(json.dumps(m), encoding='utf-8')


class GuardTests(SyntheticCase):
    def guard(self, expr):
        p = self.invoke_script(". ([scriptblock]::Create((Get-Content -LiteralPath " + quote(TOOLS / 'launcher_guard.ps1') + " -Raw -Encoding UTF8))); " + expr + ' | ConvertTo-Json -Depth 10 -Compress')
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def home(self, path, root=None, real=None):
        return self.guard('Assert-ExperimentHome ' + quote(path) + ' ' + quote(root or self.root) + ' ' + quote(real or Path(self.tmp.name) / 'fake-user'))

    def test_existing_home(self):
        self.assertTrue(self.home(self.root / 'dsh-home')['Ok'])

    def test_empty_whitespace_and_relative(self):
        for value in ('', '   ', 'dsh-home'):
            self.assertFalse(self.home(value)['Ok'])

    def test_missing_home(self):
        self.assertFalse(self.home(self.root / 'missing')['Ok'])

    def test_prefix_sibling_and_traversal(self):
        for value in (Path(str(self.root) + '-other') / 'home', self.root / '..' / 'exp' / 'dsh-home'):
            self.assertFalse(self.home(value)['Ok'])

    def test_synthetic_private_subdirectory(self):
        fake_user = Path(self.tmp.name) / 'fake-user'
        private = fake_user / '.dsh' / 'profiles' / 'nested'
        private.mkdir(parents=True)
        self.assertFalse(self.home(private, real=fake_user)['Ok'])

    def test_null_manifest(self):
        self.assertFalse(self.guard('Assert-Manifest $null ' + quote(self.root) + " ''")['Ok'])

    def test_manifest_json_roundtrip(self):
        self.ready()
        expr = '$m=Get-Content -LiteralPath ' + quote(self.manifest) + ' -Raw -Encoding UTF8 | ConvertFrom-Json; Assert-Manifest $m ' + quote(self.root) + " ''"
        self.assertTrue(self.guard(expr)['Ok'])

    def test_env_empty_mismatch_and_secret_name(self):
        for assignments in ('', "$e['DSH_HOME']=''", "$e['DSH_HOME']='relative'", "$e['DSH_HOME']=" + quote(self.root), "$e['DSH_HOME']=" + quote(self.root / 'dsh-home') + "; $e['SYNTHETIC_API_KEY']='fake-only'"):
            expr = '$e=New-Object Collections.Specialized.StringDictionary; ' + assignments + '; Assert-ChildEnvironment $e ' + quote(self.root / 'dsh-home')
            self.assertFalse(self.guard(expr)['Ok'])

    def test_env_correct(self):
        expr = "$e=New-Object Collections.Specialized.StringDictionary; $e['DSH_HOME']=" + quote(self.root / 'dsh-home') + '; Assert-ChildEnvironment $e ' + quote(self.root / 'dsh-home')
        self.assertTrue(self.guard(expr)['Ok'])

    def test_windows_case_comparison(self):
        if os.name != 'nt':
            self.skipTest('Windows case-insensitive filesystem comparison unavailable')
        self.assertTrue(self.home(str(self.root / 'dsh-home').upper())['Ok'])


class RuntimeTests(SyntheticCase):
    def resolve_python(self, prefixes):
        # 候选使用真实子进程，模拟退出码、版本与挂起，不修改 PATH。
        candidates = '; '.join(
            '@{ Path = ' + quote(sys.executable) + '; Prefix = ' + quote(prefix) + ' }'
            for prefix in prefixes
        )
        script = (
            "$ErrorActionPreference='Stop'; "
            ". ([scriptblock]::Create((Get-Content -LiteralPath " + quote(TOOLS / 'python_runtime.ps1') + " -Raw -Encoding UTF8))); "
            "function Get-OfflinePythonCandidates { " + candidates + " }; "
            "Resolve-OfflinePython -ToolsDir " + quote(TOOLS) + " -WorkingDirectory " + quote(self.root) +
            " | ConvertTo-Json -Compress"
        )
        probe = self.invoke_script(script)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return json.loads(probe.stdout)

    def test_python_valid_executable(self):
        result = self.resolve_python([''])
        self.assertTrue(result['Ok'])
        self.assertTrue(Path(result['Data']).samefile(sys.executable))

    def test_python_windowsapps_stub_exit_falls_back(self):
        # WindowsApps 占位程序已观测返回 9009，模拟同样的退出行为。
        result = self.resolve_python(['-I -S -B -c "import sys; sys.exit(9009)" ', ''])
        self.assertTrue(result['Ok'])
        self.assertTrue(Path(result['Data']).samefile(sys.executable))

    def test_python_old_version_and_timeout_fall_back(self):
        old = "-I -S -B -c \"import json, sys; print(json.dumps(dict(major=3, minor=9, path=sys.executable)))\" "
        start = time.monotonic()
        result = self.resolve_python([old, '-I -S -B -c "import time; time.sleep(30)" ', ''])
        self.assertTrue(result['Ok'])
        self.assertGreaterEqual(time.monotonic() - start, 2.5)
        self.assertLess(time.monotonic() - start, 20)

    def test_python_all_candidates_fail(self):
        stub = '-I -S -B -c "import json, sys; print(json.dumps(dict(major=3, minor=13, path=sys.executable))); sys.exit(9009)" '
        result = self.resolve_python([stub])
        self.assertFalse(result['Ok'])
        self.assertEqual(result['Code'], 'E_PYTHON_UNAVAILABLE')

    def test_engine_absolute_and_command_name(self):
        self.assertEqual(resolve_engine(ENGINE), ENGINE)
        with mock.patch.object(shutil, 'which', return_value=ENGINE) as lookup:
            self.assertEqual(resolve_engine('pwsh'), ENGINE)
            lookup.assert_called_once_with('pwsh')

    def test_engine_rejects_wrong_program_and_old_version(self):
        self.assertIsNone(resolve_engine(sys.executable))
        old = subprocess.CompletedProcess([], 0, 'OFFLINE_ENGINE=5.0\n', '')
        with mock.patch.object(subprocess, 'run', return_value=old):
            self.assertIsNone(resolve_engine(ENGINE))

    def test_engine_unavailable_fails_once_before_tests(self):
        probe = subprocess.run(
            [sys.executable, '-I', __file__, '--powershell', str(self.root / 'missing-pwsh.exe')],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(probe.returncode, 2)
        self.assertEqual(probe.stdout, '')
        self.assertEqual(len(probe.stderr.splitlines()), 1)
        self.assertIn('E_ENGINE_NOT_FOUND', probe.stderr)


class IntegrationTests(SyntheticCase):
    def test_preflight_dry_zero_spawn(self):
        self.ready()
        self.assertEqual(self.phase('run', mode='dry').returncode, 0)
        self.zero()

    def test_stub_environment_count_and_exit(self):
        self.ready()
        os.environ['SYNTHETIC_CREDENTIAL'] = 'dummy-only'
        try:
            p = self.phase('run')
        finally:
            os.environ.pop('SYNTHETIC_CREDENTIAL', None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.count(), 1)
        record = json.loads(self.marker.read_text(encoding='utf-8-sig'))
        self.assertEqual(Path(record['home']), self.root / 'dsh-home')
        self.assertEqual(Path(record['user_home']), self.root / 'dsh-home')
        self.assertEqual(Path(record['cwd']), self.root)
        self.assertEqual(Path(record['temp']), self.root)
        allowed = {'dsh_home', 'home', 'userprofile', 'appdata', 'localappdata', 'temp', 'tmp', 'systemroot', 'windir', 'stub_scenario'}
        # PowerShell 自行增加 PSModulePath/PATH；启动前 Guard 不允许传入 PATH。
        self.assertLessEqual({n.lower() for n in record['environment_names']}, allowed | {'psmodulepath', 'powershell_distribution_channel', 'path'})
        self.assertEqual(record['scenario'], 'ok')

    def test_missing_manifest(self):
        self.blocked(self.phase('run'))

    def test_stale_success_missing_config(self):
        self.ready()
        Path(self.cfg['profile']).unlink()
        self.blocked(self.phase('preflight'))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_stale_success_missing_hash(self):
        self.ready()
        self.blocked(self.phase('preflight', expected=''))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_stale_success_bad_hash(self):
        self.ready()
        self.blocked(self.phase('preflight', expected='0' * 64))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_stale_success_missing_home(self):
        self.ready()
        (self.root / 'dsh-home').rmdir()
        self.blocked(self.phase('preflight'))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_home_tool_reenable_blocks_manifest(self):
        Path(self.cfg['home']).write_text('- id: tool-bash\n  disabled: false\n')
        self.blocked(self.phase('preflight'))
        self.assertFalse(self.manifest.exists())

    def test_all_config_changes_rejected_in_dry_and_stub(self):
        self.ready()
        originals = {k: Path(self.cfg[k]).read_bytes() for k in ('base', 'profile', 'overlay', 'config')}
        for key, original in originals.items():
            with self.subTest(key=key):
                Path(self.cfg[key]).write_bytes(original + b'\n')
                self.blocked(self.phase('run', mode='dry'))
                self.blocked(self.phase('run'))
                Path(self.cfg[key]).write_bytes(original)
        Path(self.cfg['home']).write_text('[]\n')
        self.blocked(self.phase('run', mode='dry'))
        self.blocked(self.phase('run'))

    def test_present_home_removal_rejected(self):
        Path(self.cfg['home']).write_text('[]\n')
        self.ready()
        Path(self.cfg['home']).unlink()
        self.blocked(self.phase('run'))

    def test_manifest_corruption_and_hash_tamper(self):
        self.ready()
        original = self.manifest.read_bytes()
        for values in ({'base_sha256': ''}, {'base_sha256': '0' * 64}, {'base_sha256': 'invalid'},
                       {'snapshot_sha256': '0' * 64}, {'preflight': 'FAIL'}, {'trust': 'installed'},
                       {'schema': 'dsh-experiment-manifest-v1'}, {'files': []},
                       {'home': str(self.root / 'other')}, {'exp_root': str(self.root.parent)},
                       {'config_dir': str(self.root.parent)}):
            with self.subTest(values=values):
                self.manifest.write_bytes(original)
                self.mutate_manifest(**values)
                self.blocked(self.phase('run'))
        self.manifest.write_text('{')
        self.blocked(self.phase('run'))

    def test_real_and_authorization_flag_rejected(self):
        self.ready()
        self.blocked(self.phase('run', mode='real'))
        self.blocked(self.phase('run', extra='-IAuthorizeRealRun'))
        self.blocked(self.phase('run', extra="-StubExecutable arbitrary"))

    def test_large_stdout(self):
        self.large('stdout', 2097152, 0)

    def test_large_stderr(self):
        self.large('stderr', 0, 2097152)

    def test_large_both_streams(self):
        self.large('both', 2097152, 2097152)

    def large(self, scenario, stdout, stderr):
        self.ready()
        p = self.phase('run', extra='-StubScenario ' + scenario + ' -TimeoutMs 15000')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.marker.exists())
        self.assertIn(f'stdout_chars={stdout} stderr_chars={stderr} retained_chars=0', p.stdout)
        self.assertLess(len(p.stdout) + len(p.stderr), 2048)

    def test_nonzero_exit_is_not_completed(self):
        self.ready()
        p = self.phase('run', extra='-StubScenario nonzero')
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.marker.exists())
        self.assertIn('exit=7', p.stdout)
        self.assertNotIn('COMPLETED', p.stdout)

    def test_wall_clock_timeout_kills_stub(self):
        self.ready()
        start = time.monotonic()
        p = self.phase('run', extra='-StubScenario timeout -TimeoutMs 3000')
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertLess(time.monotonic() - start, 12)
        self.assertEqual(self.count(), 1)
        record = json.loads(self.marker.read_text(encoding='utf-8-sig'))
        self.assertNotIn('COMPLETED', p.stdout)
        result = self.invoke_script('if (Get-Process -Id ' + str(record['pid']) + ' -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }')
        self.assertEqual(result.returncode, 0)

    def make_link(self, link, target, junction=False):
        try:
            if junction:
                if os.name != 'nt':
                    self.skipTest('Windows junction API unavailable; symlink tests are separate')
                p = self.invoke_script('New-Item -ItemType Junction -Path ' + quote(link) + ' -Target ' + quote(target) + ' -ErrorAction Stop | Out-Null')
                if p.returncode:
                    self.skipTest('Windows junction creation unavailable for current user')
            else:
                link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            self.skipTest('symlink privilege/platform unavailable')
        self.addCleanup(lambda: link.unlink() if link.is_symlink() else os.rmdir(link))

    def test_root_symlink(self):
        self.ready()
        link = Path(self.tmp.name) / 'linked-root'
        self.make_link(link, self.root)
        self.blocked(self.phase('run', root=link))
        self.assertTrue(self.manifest.exists())

    def test_home_symlink_invalidates_success(self):
        self.ready()
        (self.root / 'dsh-home').rmdir()
        target = Path(self.tmp.name) / 'other-home'
        target.mkdir()
        self.make_link(self.root / 'dsh-home', target)
        self.blocked(self.phase('preflight'))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_config_symlink(self):
        target = Path(self.tmp.name) / 'other-config'
        shutil.move(self.root / 'config', target)
        self.make_link(self.root / 'config', target)
        self.blocked(self.phase('preflight'))

    def test_root_junction(self):
        self.ready()
        link = Path(self.tmp.name) / 'junction-root'
        self.make_link(link, self.root, junction=True)
        self.blocked(self.phase('run', root=link))

    def test_parent_junction(self):
        self.ready()
        link = Path(self.tmp.name) / 'junction-parent'
        self.make_link(link, self.root.parent, junction=True)
        self.blocked(self.phase('run', root=link / 'exp'))

    def test_home_junction(self):
        (self.root / 'dsh-home').rmdir()
        target = Path(self.tmp.name) / 'other-home'
        target.mkdir()
        self.make_link(self.root / 'dsh-home', target, junction=True)
        self.blocked(self.phase('preflight'))

    def test_output_symlink_zero_spawn(self):
        self.ready()
        target = Path(self.tmp.name) / 'untouched'
        target.write_text('synthetic')
        self.make_link(self.marker, target)
        p = self.phase('run')
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.count(), 0)
        self.assertEqual(target.read_text(), 'synthetic')

    def test_no_partial_manifest_or_temporary_snapshot(self):
        self.ready()
        self.assertEqual(list(self.root.glob('.manifest-*.tmp')), [])
        self.assertEqual(list(self.root.glob('.preflight-*')), [])

    def copied_tools(self):
        target = Path(self.tmp.name) / 'test-tools'
        shutil.copytree(TOOLS, target, ignore=shutil.ignore_patterns('__pycache__'))
        return target

    def test_stale_success_missing_guard_exception(self):
        self.ready()
        tools = self.copied_tools()
        (tools / 'launcher_guard.ps1').unlink()
        self.blocked(self.phase('preflight', tools=tools))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))

    def test_stale_success_checker_exception(self):
        self.ready()
        tools = self.copied_tools()
        (tools / 'no_tools_preflight.py').write_text('raise RuntimeError("synthetic")\n')
        self.blocked(self.phase('preflight', tools=tools))
        self.assertFalse(self.manifest.exists())
        self.blocked(self.phase('run'))
        self.assertEqual(list(self.root.glob('.preflight-*')), [])

    def test_stale_success_python_unavailable(self):
        self.ready()
        tools = self.copied_tools()
        with (tools / 'python_runtime.ps1').open('a', encoding='utf-8') as script:
            script.write('\nfunction Get-OfflinePythonCandidates {}\n')
        result = self.phase('preflight', tools=tools)
        self.blocked(result)
        self.assertIn('BLOCKED E_PYTHON_UNAVAILABLE', result.stdout)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(list(self.root.glob('.preflight-*')), [])
        self.blocked(self.phase('run'))

    def test_interrupted_preflight_and_concurrent_run(self):
        self.ready()
        tools = self.copied_tools()
        pid_file = self.root / 'checker-pid.txt'
        checker = 'import os,time\nfrom pathlib import Path\nPath(' + repr(str(pid_file)) + ').write_text(str(os.getpid()))\ntime.sleep(60)\n'
        (tools / 'no_tools_preflight.py').write_text(checker)
        parent = subprocess.Popen(command(self.phase_script('preflight', tools=tools)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not pid_file.exists() and time.monotonic() < deadline and parent.poll() is None:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), 'controlled checker did not reach invalidated state')
            self.assertFalse(self.manifest.exists())
            self.blocked(self.phase('run'))
            parent.kill()
            parent.communicate(timeout=8)
            self.blocked(self.phase('run'))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.communicate(timeout=8)
            # 强制终止父进程不能运行 finally；只清理此测试明确记录的合成 checker。
            if pid_file.exists():
                self.invoke_script('Stop-Process -Id ' + pid_file.read_text().strip() + ' -ErrorAction SilentlyContinue')

    def test_successful_stub_repeated_start_count(self):
        self.ready()
        self.assertEqual(self.phase('run').returncode, 0)
        self.assertEqual(self.phase('run').returncode, 0)
        self.assertEqual(self.count(), 2)
        self.assertTrue(self.marker.exists())

    def test_config_file_reparse(self):
        self.ready()
        source = Path(self.cfg['overlay'])
        target = Path(self.tmp.name) / 'other-overlay.yml'
        source.rename(target)
        self.make_link(source, target)
        self.blocked(self.phase('run', mode='dry'))
        self.blocked(self.phase('run'))

    def test_lock_file_reparse_zero_spawn(self):
        self.ready()
        lock = self.root / '.launcher.lock'
        lock.unlink()
        target = Path(self.tmp.name) / 'outside-lock'
        self.make_link(lock, target)
        self.blocked(self.phase('run'))
        self.assertFalse(target.exists())

    def test_start_failure_is_blocked_without_started(self):
        self.ready()
        tools = self.copied_tools()
        launcher = tools / 'joint_experiment.ps1'
        text = launcher.read_text(encoding='utf-8')
        # 只修改测试副本，以真实 Process.Start 失败覆盖启动异常；生产无可执行路径参数。
        text = text.replace('$psi.FileName = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName', '$psi.FileName = ' + quote(self.root / 'missing-synthetic-executable'))
        launcher.write_text(text, encoding='utf-8')
        p = self.phase('run', tools=tools)
        self.blocked(p)
        self.assertNotIn('STARTED', p.stdout)

    def test_windows_powershell_51(self):
        p = self.invoke_script('$PSVersionTable.PSVersion.Major.ToString()+"."+$PSVersionTable.PSVersion.Minor.ToString()')
        if os.name != 'nt' or p.stdout.strip() != '5.1':
            self.skipTest('selected engine is not Windows PowerShell 5.1; compatibility not verified')
        self.assertEqual(p.returncode, 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--powershell')
    parser.add_argument('--suite', choices=('guard', 'integration', 'all'), default='all')
    args = parser.parse_args()
    spec = args.powershell if args.powershell is not None else os.environ.get('LAUNCHER_TEST_POWERSHELL')
    ENGINE = resolve_engine(spec)
    if ENGINE is None:
        print('E_ENGINE_NOT_FOUND: pass an absolute PowerShell 5.1+ executable path with --powershell or use a command on PATH.', file=sys.stderr)
        sys.exit(2)
    classes = {
        'guard': [RuntimeTests, GuardTests],
        'integration': [IntegrationTests],
        'all': [RuntimeTests, GuardTests, IntegrationTests],
    }[args.suite]
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in classes)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failures = len(result.failures) + len(result.errors)
    print(f'PASS={result.testsRun - failures - len(result.skipped)} FAIL={failures} SKIP={len(result.skipped)}')
    sys.exit(1 if failures else 0)
