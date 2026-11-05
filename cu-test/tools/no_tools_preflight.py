#!/usr/bin/env python3
"""Offline fail-closed preflight for DSH no-tools composition.

Design-only prototype. It reads the *actual* composition layers (base bundle,
profile patch, overlay patch), computes the effective entry state using the
documented layer order, and refuses to pass unless every model-facing tool
provider is disabled and nothing unknown was introduced.

It does NOT start DSH, contact any model, read credentials or modify files.

Documented basis (dsh-app-boot README, cordis-plugin-loader README):
  * layer order: bundle layers -> profile cordis.patch.yml -> home patch
    -> --patch overlays -> telemetry switch
  * `disabled` stops the entry and prevents it from starting
  * a patch naming an unknown entry only prints a stderr warning (NOT fatal)

Because unknown patch ids are only warned about, this preflight must not trust
the overlay: it recomputes the effective set and fails closed on any gap.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import sys
from dataclasses import dataclass, field

TOP_ITEM = re.compile(r"^- (?=\S)")
KEY_LINE = re.compile(r"^  ([A-Za-z0-9_.-]+):(?:\s*(.*))?$")
NESTED_ID = re.compile(r"^\s{4}- id:\s*(\S+)\s*$")

TOOL_PREFIX = "tool-"


class ParseError(Exception):
    pass


@dataclass
class Entry:
    id: str
    name: str | None = None
    disabled: str | None = None  # "true" | "false" | "expr" | None
    inserted: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)


def _strip_comment(line: str) -> str:
    # Only full-line comments are honoured; inline '#' may belong to a value.
    if line.lstrip().startswith("#"):
        return ""
    return line.rstrip("\r\n")


def parse_layer(path: str) -> list[Entry]:
    """Parse the constrained YAML subset used by cordis.patch.yml files.

    Handles both shapes shipped by DSH 0.2.2-rc.1:
      * a flat list of ``- id:`` rows (profile patches), and
      * one or more ``- insert:`` containers whose rows sit at deeper indent
        (the base bundle).

    Every ``- id:`` line starts a row regardless of nesting; keys belong to the
    row while their indent stays deeper than the row's marker indent. Anything
    that fits neither shape raises ParseError, which the caller treats as
    fail-closed.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    if not raw.strip():
        raise ParseError("empty file (documented: empty patch fails boot)")

    entries: list[Entry] = []
    current: Entry | None = None
    key_indent: int | None = None
    in_insert = False
    in_scalar = False
    scalar_base = 0

    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = _strip_comment(line)
        if not line.strip():
            continue
        if line.strip() in ("---", "..."):
            continue

        indent = len(line) - len(line.lstrip(" "))

        # Reject YAML features this subset deliberately does not support.
        if re.search(r"(^|\s)[&*][A-Za-z0-9_]", line):
            raise ParseError(f"line {lineno}: anchors/aliases are not supported")
        if re.match(r"^\s*(?:- )?<<:", line):
            raise ParseError(f"line {lineno}: merge keys are not supported")
        if re.search(r":\s+![^!]", line) or re.match(r"^\s*- !(?!!)", line):
            raise ParseError(f"line {lineno}: unknown YAML tag")

        m_id = re.match(r"^\s*- id:\s*(\S+)\s*$", line)
        if m_id:
            current = Entry(id=m_id.group(1))
            entries.append(current)
            key_indent = indent + 2
            continue

        if re.match(r"^\s*- insert:\s*$", line):
            in_insert = True
            current = None
            key_indent = None
            continue

        if re.match(r"^\s*- ", line) and not current:
            raise ParseError(f"line {lineno}: unexpected list item before an entry")

        if current is None:
            raise ParseError(f"line {lineno}: content before first entry")

        if in_scalar:
            if indent > scalar_base:
                continue
            in_scalar = False

        m_key = re.match(r"^\s{2,}([A-Za-z0-9_.-]+):(?:\s*(.*))?$", line)
        if m_key and indent == key_indent:
            key, value = m_key.group(1), (m_key.group(2) or "").strip()
            if value.startswith((">", "|")):
                in_scalar = True
                scalar_base = key_indent
                continue
            if key in current.keys:
                raise ParseError(f"line {lineno}: duplicate mapping key {key!r}")
            current.keys.add(key)
            if key == "name":
                current.name = value.strip("'\"")
            elif key == "disabled":
                if value.startswith("!!"):
                    current.disabled = "expr"
                elif value in ("true", "false"):
                    current.disabled = value
                elif value == "":
                    current.disabled = "expr"
                else:
                    raise ParseError(f"line {lineno}: unsupported disabled value {value!r}")
            continue

        if indent > (key_indent or 0):
            continue  # nested payload: config bodies, provider tables, block scalars

        if indent < (key_indent or 0):
            current = None
            if re.match(r"^\s*- insert:\s*$", line):
                continue
            raise ParseError(f"line {lineno}: dedented content outside a container")

        raise ParseError(f"line {lineno}: unrecognised structure: {line[:60]!r}")

    if not entries:
        raise ParseError("no entries parsed")
    return entries

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_pinned_hash(path: str | None) -> str | None:
    if not path:
        return None
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data.get("base_sha256")


def check(cfg: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    def rec(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}")

    # --- 1. baseline identity -------------------------------------------
    try:
        with open(cfg["base"], "r", encoding="utf-8") as fh:
            base_text = fh.read()
    except OSError as exc:
        rec("base_readable", False, str(exc))
        return {"verdict": "FAIL", "checks": checks, "errors": errors, "warnings": warnings}

    pinned = load_pinned_hash(cfg.get("pinned"))
    actual = sha256_text(base_text)
    if pinned:
        rec("base_hash_pinned", pinned == actual, f"expected {pinned[:16]}… got {actual[:16]}…")
    else:
        # Fail closed: without a pinned baseline the checker cannot detect a
        # version change, so it must not report a pass.
        rec("baseline_pin_present", False, "no pinned base_sha256 supplied")

    # --- 2. parse every layer -------------------------------------------
    layers: list[tuple[str, list[Entry]]] = []
    home_path = cfg.get("home")
    if home_path and not os.path.exists(home_path):
        rec("home_patch_absent", True, "home patch absent (no home layer)")
        home_path = None
    for label, path in (
        ("base", cfg["base"]),
        ("profile", cfg["profile"]),
        ("home", home_path),
        ("overlay", cfg["overlay"]),
    ):
        if path is None:
            continue
        try:
            layers.append((label, parse_layer(path)))
            rec(f"parse_{label}", True, f"{len(layers[-1][1])} entries")
        except (ParseError, OSError) as exc:
            rec(f"parse_{label}", False, str(exc))
            return {"verdict": "FAIL", "checks": checks, "errors": errors, "warnings": warnings}

    # --- 3. duplicates inside a layer -----------------------------------
    for label, entries in layers:
        seen: set[str] = set()
        dup = [e.id for e in entries if e.id in seen or seen.add(e.id)]
        rec(f"no_duplicate_ids_{label}", not dup, f"duplicates={dup}" if dup else "unique")

    # --- 4. effective composition ---------------------------------------
    state: dict[str, dict] = {}
    for label, entries in layers:
        for e in entries:
            cur = state.setdefault(e.id, {"disabled": None, "sources": []})
            if e.disabled is not None:
                cur["disabled"] = e.disabled
            cur["sources"].append(label)
            for ins in e.inserted:
                if ins not in state:
                    state[ins] = {"disabled": None, "sources": [f"{label}:insert"]}
                else:
                    state[ins]["sources"].append(f"{label}:insert")

    # --- 5. overlay must not name unknown ids ---------------------------
    base_ids = {e.id for e in dict(layers)["base"]}
    known = set(base_ids)
    overlay_ids = {e.id for e in dict(layers)["overlay"]}
    unknown_overlay = sorted(overlay_ids - known)
    rec(
        "overlay_ids_known",
        not unknown_overlay,
        f"unknown={unknown_overlay}" if unknown_overlay else "all overlay ids exist in base",
    )

    # --- 6. profile must not introduce new tool rows --------------------
    profile_ids = {e.id for e in dict(layers)["profile"]}
    capable_pkgs = set(cfg.get("tool_capable_packages", []))
    profile_added_capable = sorted(
        e.id for e in dict(layers)["profile"]
        if e.id not in base_ids and e.name in capable_pkgs
    )
    rec(
        "profile_adds_no_capable_plugin",
        not profile_added_capable,
        f"profile mounts tool-capable packages: {profile_added_capable}" if profile_added_capable else "none",
    )

    added_tools = sorted(
        i for i in profile_ids - base_ids if i.startswith(TOOL_PREFIX) or i in cfg.get("providers", [])
    )
    rec(
        "profile_adds_no_tools",
        not added_tools,
        f"profile adds {added_tools}" if added_tools else "none",
    )

    # --- 6b. no layer may explicitly re-enable a tool/provider -----------
    explicit_enable = sorted(
        e.id
        for _, entries in layers
        for e in entries
        if e.disabled == "false" and (e.id.startswith(TOOL_PREFIX) or e.id in cfg.get("providers", []))
    )
    rec(
        "no_explicit_reenable",
        not explicit_enable,
        f"explicit disabled:false on {explicit_enable}" if explicit_enable else "none",
    )

    # --- 7. every tool row disabled in the effective state --------------
    tool_ids = sorted(i for i in known if i.startswith(TOOL_PREFIX))
    expected_tools = set(cfg.get("expected_tools", []))
    missing_expected = sorted(expected_tools - set(tool_ids))
    rec(
        "expected_tool_inventory_complete",
        not missing_expected,
        f"missing from base={missing_expected}" if missing_expected else f"{len(tool_ids)} tool rows",
    )

    # 6c. capability-based inventory: rows whose *package* can register tools
    # are authoritative; the tool-* prefix alone is not sufficient.
    capable = [i for i in cfg.get("tool_capable_rows", []) if i in state]
    unknown_capable = [i for i in cfg.get("tool_capable_rows", []) if i not in state]
    rec(
        "tool_capable_rows_mounted",
        not unknown_capable,
        f"not mounted={unknown_capable}" if unknown_capable else f"{len(capable)} rows mounted",
    )
    enabled_capable = [i for i in capable if state.get(i, {}).get("disabled") != "true"]
    rec(
        "all_tool_capable_disabled",
        not enabled_capable,
        f"still enabled={enabled_capable}" if enabled_capable else "all disabled",
    )

    allowed = set(cfg.get("allowed_enabled_tools", []))
    enabled_tools = [i for i in tool_ids if state.get(i, {}).get("disabled") != "true" and i not in allowed]
    rec(
        "all_tools_disabled",
        not enabled_tools,
        f"still enabled={enabled_tools}" if enabled_tools else "all disabled",
    )

    # --- 8. providers must be disabled too ------------------------------
    providers = cfg.get("providers", [])
    enabled_providers = [p for p in providers if state.get(p, {}).get("disabled") != "true"]
    rec(
        "all_providers_disabled",
        not enabled_providers,
        f"still enabled={enabled_providers}" if enabled_providers else "all disabled",
    )

    # --- 9. no expression-based disabled (cannot be resolved offline) ----
    watched = set(tool_ids) | set(providers) | set(cfg.get("tool_capable_rows", []))
    expr = sorted(i for i, v in state.items() if v.get("disabled") == "expr" and i in watched)
    rec(
        "no_expression_disabled",
        not expr,
        f"unresolvable disabled expression on {expr}" if expr else "none",
    )

    # --- 10. any id touched by a later layer re-enabling? ---------------
    reenabled = sorted(
        i
        for i, v in state.items()
        if i in watched and len(v["sources"]) > 1 and v.get("disabled") != "true"
    )
    if reenabled:
        rec("no_later_reenable", False, f"later layer leaves enabled={reenabled}")

    verdict = "PASS" if not errors else "FAIL"
    return {"verdict": verdict, "checks": checks, "errors": errors, "warnings": warnings}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSH no-tools offline preflight (fail-closed)")
    ap.add_argument("--base", required=True, help="dsh-base/cordis.patch.yml")
    ap.add_argument("--profile", required=True, help="headless profile cordis.patch.yml")
    ap.add_argument("--overlay", required=True, help="no-tools overlay patch")
    ap.add_argument("--pinned", help="JSON file with base_sha256")
    ap.add_argument("--home", help="optional home-level cordis.patch.yml")
    ap.add_argument("--config", help="JSON file with expected_tools/providers")
    ap.add_argument("--json-out", help="write result JSON here")
    args = ap.parse_args(argv)

    cfg = {
        "base": args.base,
        "profile": args.profile,
        "overlay": args.overlay,
        "pinned": args.pinned,
    }
    if args.config:
        with open(args.config, "r", encoding="utf-8-sig") as fh:
            cfg.update(json.load(fh))

    result = check(cfg)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
