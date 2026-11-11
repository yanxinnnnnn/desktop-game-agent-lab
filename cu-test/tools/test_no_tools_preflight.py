"""只使用临时合成配置；CLI 实际退出状态及结构化结果回归。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from no_tools_preflight import REQUIRED_ROWS, check

TOOLS = Path(__file__).resolve().parent


def fixture(root):
    cfg = root / 'config'
    cfg.mkdir(parents=True)
    (root / 'dsh-home').mkdir()
    rows = sorted(REQUIRED_ROWS | {'hmr'})
    (cfg / 'base.yml').write_text(''.join(f"- id: {i}\n  name: '@deepseek-ai/{i}'\n" for i in rows), encoding='utf-8')
    (cfg / 'profile.yml').write_text('- id: hmr\n  disabled: true\n', encoding='utf-8')
    (cfg / 'overlay.yml').write_text(''.join(f'- id: {i}\n  disabled: true\n' for i in sorted(REQUIRED_ROWS)), encoding='utf-8')
    (cfg / 'capabilities.json').write_text(json.dumps({'providers': ['web'], 'tool_capable_rows': ['tool-bash']}), encoding='utf-8')
    pin = root / 'pin.json'
    pin.write_text(json.dumps({'base_sha256': hashlib.sha256((cfg / 'base.yml').read_bytes()).hexdigest()}), encoding='utf-8')
    return dict(base=str(cfg / 'base.yml'), profile=str(cfg / 'profile.yml'),
                overlay=str(cfg / 'overlay.yml'), home=str(root / 'dsh-home' / 'cordis.patch.yml'),
                config=str(cfg / 'capabilities.json'), pinned=str(pin))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='offline-preflight-')
        self.addCleanup(self.tmp.cleanup)
        self.cfg = fixture(Path(self.tmp.name))

    def cli(self):
        args = [sys.executable, '-I', str(TOOLS / 'no_tools_preflight.py')]
        for k, v in self.cfg.items():
            args += ['--' + k, v]
        p = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return p.returncode, json.loads(p.stdout)

    def write(self, layer, text):
        Path(self.cfg[layer]).write_text(text, encoding='utf-8')

    def assert_verdict(self, expected):
        code, result = self.cli()
        self.assertEqual(code, 0 if expected == 'PASS' else 2)
        self.assertEqual(result['verdict'], expected)

    def test_absent_home_pass(self):
        self.assert_verdict('PASS')
        self.assertIn({'check': 'home_patch_absent', 'ok': True}, check(self.cfg)['checks'])

    def test_home_disable_pass(self):
        self.write('home', '- id: tool-bash\n  disabled: true\n- id: web\n  disabled: true\n')
        self.assert_verdict('PASS')

    def test_home_tool_reenable_fails_even_if_overlay_disables(self):
        self.write('home', '- id: tool-bash\n  disabled: false\n')
        self.assert_verdict('FAIL')

    def test_home_provider_reenable_fails(self):
        self.write('home', '- id: web\n  disabled: false\n')
        self.assert_verdict('FAIL')

    def test_overlay_tool_reenable_fails(self):
        self.write('overlay', Path(self.cfg['overlay']).read_text().replace('- id: tool-bash\n  disabled: true', '- id: tool-bash\n  disabled: false'))
        self.assert_verdict('FAIL')

    def test_overlay_provider_reenable_fails(self):
        self.write('overlay', Path(self.cfg['overlay']).read_text().replace('- id: web\n  disabled: true', '- id: web\n  disabled: false'))
        self.assert_verdict('FAIL')

    def test_profile_reenable_fails(self):
        self.write('profile', '- id: hmr\n  disabled: true\n- id: web\n  disabled: false\n')
        self.assert_verdict('FAIL')

    def test_no_inventory_can_remove_required_rows(self):
        self.write('config', '{}')
        self.write('overlay', '[]\n')
        self.assert_verdict('FAIL')

    def test_capabilities_cannot_override_paths(self):
        for key in ('base', 'profile', 'home', 'overlay', 'pinned', 'config'):
            with self.subTest(key=key):
                self.write('config', json.dumps({key: 'synthetic-other'}))
                self.assert_verdict('FAIL')

    def test_unrecognized_yaml_fails(self):
        for text in ('', '# only comment\n', '- id: tool-bash\n  disabled: |\n    true\n',
                     '- id: tool-bash\n  disabled: !!bool true\n',
                     '- id: tool-bash\n  disabled: "true"\n',
                     '- id: tool-bash\n  disabled: true\n  disabled: false\n',
                     '- id: tool-bash\n  disabled: true\n    - id: web\n      disabled: false\n',
                     '- id: tool-bash\n  config:\n    disabled: false\n',
                     '- id: tool-bash\n  disabled: true\n---\n- id: web\n  disabled: false\n',
                     '- id: tool-bash\n  disabled: *alias\n',
                     '- id: tool-bash\n  disabled: true # ambiguous\n',
                     '- id: tool-bash\n  unknown: true\n',
                     '- insert:\n  - id: tool-bash\n    disabled: true\n'):
            with self.subTest(text=text):
                self.write('home', text)
                self.assert_verdict('FAIL')

    def test_unknown_ids_and_package_replacement_fail(self):
        for layer in ('profile', 'home', 'overlay'):
            original = Path(self.cfg[layer]).read_text() if Path(self.cfg[layer]).exists() else '[]\n'
            for text in ('- id: unknown\n  disabled: true\n', "- id: tool-bash\n  name: '@other/package'\n  disabled: true\n"):
                with self.subTest(layer=layer, text=text):
                    self.write(layer, text)
                    self.assert_verdict('FAIL')
            self.write(layer, original)

    def test_flat_and_insert_base_equivalent(self):
        text = Path(self.cfg['base']).read_text()
        self.write('base', '- insert:\n' + ''.join('  ' + line + '\n' for line in text.splitlines()))
        self.write('pinned', json.dumps({'base_sha256': hashlib.sha256(Path(self.cfg['base']).read_bytes()).hexdigest()}))
        self.assert_verdict('PASS')

    def test_home_empty_list_pass(self):
        self.write('home', '[]\n')
        self.assert_verdict('PASS')

    def test_missing_home_argument_fails(self):
        self.cfg.pop('home')
        self.assertEqual(check(self.cfg)['verdict'], 'FAIL')

    def test_invalid_pin_fails(self):
        for value in ('', 'bad', '0' * 64, 123, None):
            self.write('pinned', json.dumps({'base_sha256': value}))
            self.assert_verdict('FAIL')

    def test_invalid_json_and_exemptions_fail(self):
        for text in ('[]', '{', '{"providers":[],"providers":[]}', '{"providers":"web"}', '{"allowed_enabled_tools":["tool-bash"]}'):
            self.write('config', text)
            self.assert_verdict('FAIL')


if __name__ == '__main__':
    unittest.main(verbosity=2)
