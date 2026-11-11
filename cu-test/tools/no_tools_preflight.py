#!/usr/bin/env python3
"""离线、保守的四层配置检查；不是 DSH 运行时或安装来源证明。

只接受明确的 id/name/disabled 列表及 base 的单层 insert 容器。
不支持的 YAML 必须 FAIL，禁止跳过未知嵌套内容后判断安全。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

REQUIRED_ROWS = frozenset('''plugin-manager plan-mode subagent mcp-resources
    tool-working-directory tool-bash tool-pwsh tool-jobs tool-fs tool-fs-search
    tool-skill tool-subagent tool-subagent-control tool-subagent-fork tool-workflow
    tool-todo tool-goal tool-web web web-search-deepseek web-deepseek-api
    web-fetch-http subagent-spawn-in-process subagent-fork-in-process'''.split())
INVENTORY_KEYS = frozenset(('expected_tools', 'tool_capable_rows', 'providers',
                            'tool_capable_packages', 'allowed_enabled_tools'))
MAX_BYTES = 1024 * 1024


class ParseError(ValueError):
    pass


@dataclass
class Entry:
    id: str
    name: str | None = None
    disabled: str | None = None


def read_bytes(path: str) -> bytes:
    with open(path, 'rb') as fh:
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ParseError('input exceeds 1 MiB')
    return data


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ParseError('duplicate JSON key')
        obj[key] = value
    return obj


def load_json(path: str):
    return json.loads(read_bytes(path).decode('utf-8-sig'), object_pairs_hook=unique_object)


def parse_layer(path: str, *, base: bool = False) -> list[Entry]:
    raw = read_bytes(path).decode('utf-8-sig')
    lines = [line.rstrip() for line in raw.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if lines == ['[]']:
        return []
    if not lines:
        raise ParseError('empty patch')
    entries = []
    current = None
    row_indent = 0
    container = False
    keys = set()
    for line in lines:
        if '\t' in line:
            raise ParseError('tabs unsupported')
        if line == '- insert:' and base:
            container, current, row_indent = True, None, 2
            continue
        match = re.fullmatch(r'( *)- id: ([A-Za-z0-9_.-]+)', line)
        if match:
            if len(match[1]) != row_indent:
                raise ParseError('unsupported row indentation')
            current = Entry(match[2])
            entries.append(current)
            keys = {'id'}
            continue
        match = re.fullmatch(r'( *)(name|disabled): (.+)', line)
        if not match or current is None or len(match[1]) != row_indent + 2:
            raise ParseError('unsupported YAML structure')
        key, value = match[2], match[3]
        if key in keys:
            raise ParseError('duplicate YAML key')
        keys.add(key)
        if key == 'disabled':
            if value not in ('true', 'false'):
                raise ParseError('disabled must be an unquoted literal boolean')
            current.disabled = value
        else:
            if not re.fullmatch(r"(?:@[A-Za-z0-9_./-]+|'@[A-Za-z0-9_./-]+'|\"@[A-Za-z0-9_./-]+\")", value):
                raise ParseError('unsupported package name')
            current.name = value.strip('\'"')
    if not entries or (container and current is None):
        raise ParseError('empty insert')
    if len({e.id for e in entries}) != len(entries):
        raise ParseError('duplicate row id')
    return entries


def inventory(path: str) -> dict:
    obj = load_json(path)
    if not isinstance(obj, dict) or set(obj) - INVENTORY_KEYS:
        raise ParseError('capabilities accepts inventory fields only')
    for values in obj.values():
        if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                or len(values) != len(set(values))):
            raise ParseError('inventory must contain unique nonempty strings')
    if obj.get('allowed_enabled_tools'):
        raise ParseError('enabled-tool exemptions unsupported')
    return obj


def check(cfg: dict) -> dict:
    checks = []

    def rec(name, ok):
        checks.append({'check': name, 'ok': bool(ok)})

    try:
        caps = inventory(cfg['config'])
        pinned = load_json(cfg['pinned'])
        expected = pinned.get('base_sha256') if isinstance(pinned, dict) else None
        rec('base_hash_pinned', isinstance(expected, str)
            and bool(re.fullmatch('[0-9a-fA-F]{64}', expected))
            and expected.lower() == hashlib.sha256(read_bytes(cfg['base'])).hexdigest())
        # 缺失 Home patch 是明确的无操作层；调用方必须提供其预期路径。
        if not cfg.get('home'):
            raise ParseError('home path required, even when absent')
        home = Path(cfg['home'])
        absent = not home.exists() and not home.is_symlink()
        rec('home_patch_absent', absent) if absent else None
        layers = []
        for label in ('base', 'profile', 'home', 'overlay'):
            entries = [] if label == 'home' and absent else parse_layer(cfg[label], base=label == 'base')
            layers.append((label, entries))
            rec('parse_' + label, True)
        base_ids = {e.id for e in layers[0][1]}
        required = REQUIRED_ROWS | set(caps.get('expected_tools', [])) | set(caps.get('providers', [])) | set(caps.get('tool_capable_rows', []))
        rec('required_inventory_mounted', required <= base_ids)
        state = {}
        for label, entries in layers:
            for entry in entries:
                if label != 'base':
                    rec(label + '_known_id', entry.id in base_ids)
                    # 不接受包替换，也不允许先重启用后被 overlay 掩盖。
                    rec(label + '_no_package_replacement', entry.name is None)
                    rec(label + '_no_explicit_reenable', entry.disabled != 'false')
                if entry.disabled is not None:
                    state[entry.id] = entry.disabled
        # 保守子集：所有挂载行均禁用，避免漏分类插件/任意例外提升信任。
        rec('all_mounted_rows_disabled', all(state.get(i) == 'true' for i in base_ids))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        rec('input_supported_and_readable', False)
    errors = [c['check'] for c in checks if not c['ok']]
    return {'verdict': 'FAIL' if errors else 'PASS', 'checks': checks, 'errors': errors,
            'trust': 'offline-supplied-inputs-only'}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'profile', 'home', 'overlay', 'config', 'pinned'):
        ap.add_argument('--' + name, required=True)
    args = ap.parse_args(argv)
    result = check(vars(args))
    print(json.dumps(result))
    return 0 if result['verdict'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
