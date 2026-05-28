#!/usr/bin/env python3
# filename: evangelist.py
"""
Evangelist v1.0.0 — Precise, dumb, fast Logic App value applicator.

Takes a Logic App JSON and a manifest of changes. Finds each named step,
writes each named field to the declared value. Aborts if anything named
in the manifest can't be found exactly where expected. Ignores everything
not in the manifest.

Two use cases, same operation:
  Clone:  pass the golden template as --app-json, get a new app out
  Ensure: pass an existing app as --app-json, get an updated app out

Usage:
    python3 evangelist.py --app-json golden.json --manifest changes.yaml --output out.json
    python3 evangelist.py --app-json existing.json --manifest changes.yaml --dry-run
    python3 evangelist.py --version

Exit codes:
    0  All changes applied (or dry-run completed)
    1  One or more manifest entries could not be resolved (step/field missing or wrong scope)
    3  Fatal error (bad input files, YAML parse error)
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).parent))
try:
    from helpers.checksum import fingerprint as _fp
except ImportError:
    def _fp(value: Any, chop: int = 12) -> str:
        if isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = repr(value).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:chop]


class ChangeRequest:
    __slots__ = ("step", "field", "value", "checksum", "note")
    def __init__(self, step, field, value, checksum=None, note=""):
        self.step = step; self.field = field; self.value = value
        self.checksum = checksum; self.note = note


class ChangeResult:
    __slots__ = ("req", "verdict", "old_value", "scope_path", "detail")
    def __init__(self, req):
        self.req = req; self.verdict = "PENDING"; self.old_value = None
        self.scope_path = None; self.detail = ""
    def to_dict(self):
        return {"step": self.req.step, "field": self.req.field, "note": self.req.note,
                "verdict": self.verdict, "old_value": self.old_value,
                "new_value": str(self.req.value) if self.req.value is not None else None,
                "scope_path": self.scope_path, "detail": self.detail}


def load_manifest(manifest_path: Path) -> Tuple[str, List[ChangeRequest]]:
    raw = manifest_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("EVG_E001: Manifest must be a YAML mapping.")
    app_label = str(data.get("app", manifest_path.stem))
    changes_raw = data.get("changes", [])
    if not isinstance(changes_raw, list):
        raise ValueError("EVG_E001: 'changes' must be a list.")
    requests = []
    for i, entry in enumerate(changes_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"EVG_E001: changes[{i}] must be a mapping.")
        step = entry.get("step"); field = entry.get("field")
        if not step or not field:
            raise ValueError(f"EVG_E001: changes[{i}] missing 'step' or 'field'.")
        if "value" not in entry:
            raise ValueError(f"EVG_E001: changes[{i}] missing 'value'.")
        checksum = str(entry["checksum"]) if "checksum" in entry and entry["checksum"] is not None else None
        requests.append(ChangeRequest(step=str(step), field=str(field),
                                      value=entry["value"], checksum=checksum,
                                      note=str(entry.get("note", ""))))
    return app_label, requests


_ARRAY_IDX_RE = re.compile(r'^(\w[\w\-]*)\[(\d+)\]$')

def _resolve_field_path(obj, path):
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        m = _ARRAY_IDX_RE.match(part)
        if m:
            current = current[m.group(1)][int(m.group(2))]
        else:
            current = current[part]
    final = parts[-1]
    m = _ARRAY_IDX_RE.match(final)
    if m:
        return current[m.group(1)], int(m.group(2))
    return current, final


def _find_steps_by_name(actions, target_name, scope_path="root"):
    results = []
    for name, action in actions.items():
        if not isinstance(action, dict):
            continue
        if name == target_name:
            results.append((action, scope_path))
        for container_key in ("actions", "else", "default"):
            sub = action.get(container_key)
            if isinstance(sub, dict):
                results.extend(_find_steps_by_name(
                    sub, target_name,
                    f"{scope_path}.{name}" if scope_path != "root" else name))
        cases = action.get("cases")
        if isinstance(cases, dict):
            for case_name, case_body in cases.items():
                sub = case_body.get("actions") if isinstance(case_body, dict) else None
                if isinstance(sub, dict):
                    results.extend(_find_steps_by_name(
                        sub, target_name,
                        f"{scope_path}.{name}.cases.{case_name}" if scope_path != "root"
                        else f"{name}.cases.{case_name}"))
    return results


def _coerce_value(manifest_value, existing_value):
    if isinstance(existing_value, bool):
        if str(manifest_value).lower() in ("true", "1"): return True
        if str(manifest_value).lower() in ("false", "0"): return False
    elif isinstance(existing_value, int) and not isinstance(existing_value, bool):
        try: return int(manifest_value)
        except (ValueError, TypeError): pass
    elif isinstance(existing_value, float):
        try: return float(manifest_value)
        except (ValueError, TypeError): pass
    return manifest_value


def apply_changes(app_json, requests):
    actions = app_json.get("definition", {}).get("actions", {})
    results = []; all_ok = True
    for req in requests:
        result = ChangeResult(req)
        matches = _find_steps_by_name(actions, req.step)
        if len(matches) == 0:
            result.verdict = "ABORT_NO_STEP"
            result.detail = f"Step '{req.step}' not found anywhere in app."
            results.append(result); all_ok = False; continue
        if len(matches) > 1:
            result.verdict = "ABORT_SCOPE"
            result.detail = f"Step '{req.step}' found in {len(matches)} scopes: {[s for _,s in matches]}."
            results.append(result); all_ok = False; continue
        action_dict, scope_path = matches[0]
        result.scope_path = scope_path
        try:
            parent, key = _resolve_field_path(action_dict, req.field)
        except (KeyError, IndexError, TypeError) as e:
            result.verdict = "ABORT_NO_FIELD"
            result.detail = f"Field path '{req.field}' not found in '{req.step}': {e}"
            results.append(result); all_ok = False; continue
        try:
            old_value = parent[key]
        except (KeyError, IndexError):
            result.verdict = "ABORT_NO_FIELD"
            result.detail = f"Final key '{key}' missing."
            results.append(result); all_ok = False; continue
        result.old_value = old_value
        if str(req.value) if req.value is not None else "" == str(old_value) if old_value is not None else "":
            result.verdict = "SKIPPED"; results.append(result); continue
        new_value = _coerce_value(req.value, old_value)
        parent[key] = new_value
        if req.checksum:
            actual_fp = _fp(str(new_value), chop=len(req.checksum))
            if actual_fp != req.checksum:
                result.verdict = "CHECKSUM_FAIL"
                result.detail = f"Written checksum {actual_fp} != declared {req.checksum}."
                results.append(result); all_ok = False; continue
        result.verdict = "WRITTEN"; results.append(result)
    return results, all_ok


def render_report(results, app_label, input_path, output_path, dry_run, all_ok):
    lines = [f"EVANGELIST v{__version__} — {app_label} [{'DRY RUN' if dry_run else 'APPLY'}]",
             f"Input:  {input_path}"]
    if output_path and not dry_run:
        lines.append(f"Output: {output_path}")
    lines.append("═" * 55); lines.append("")
    summary = {}
    for r in results:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
        label = r.req.note if r.req.note else f"{r.req.step} → {r.req.field}"
        dots = "." * max(1, 36 - len(label))
        detail = ""
        if r.verdict == "WRITTEN":
            detail = f'  "{r.old_value}" → "{r.req.value}"'
        elif r.verdict == "SKIPPED":
            detail = f'  already "{r.old_value}"'
        elif r.verdict in ("ABORT_NO_STEP", "ABORT_NO_FIELD", "ABORT_SCOPE", "CHECKSUM_FAIL"):
            detail = f"  {r.detail}"
        lines.append(f"  {label} {dots} {r.verdict}{detail}")
    lines.append("")
    parts = [f"{summary[v]} {v}" for v in
             ("WRITTEN", "SKIPPED", "CHECKSUM_FAIL", "ABORT_NO_STEP", "ABORT_NO_FIELD", "ABORT_SCOPE")
             if summary.get(v, 0) > 0]
    lines.append(f"SUMMARY: {', '.join(parts) if parts else 'nothing to do'}")
    lines.append(f"VERDICT: {'PASS' if all_ok else 'FAIL'}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Evangelist — precise Logic App value applicator.")
    parser.add_argument("--version", action="version", version=f"evangelist {__version__}")
    parser.add_argument("--app-json", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-report", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.output:
        print("EVG_E003: --output is required unless --dry-run.", file=sys.stderr); sys.exit(3)

    app_path = Path(args.app_json); manifest_path = Path(args.manifest)
    if not app_path.exists():
        print(f"EVG_E001: App JSON not found: {app_path}", file=sys.stderr); sys.exit(3)
    if not manifest_path.exists():
        print(f"EVG_E001: Manifest not found: {manifest_path}", file=sys.stderr); sys.exit(3)

    try:
        app_json = json.loads(app_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"EVG_E001: Failed to parse app JSON: {e}", file=sys.stderr); sys.exit(3)

    try:
        app_label, requests = load_manifest(manifest_path)
    except (ValueError, yaml.YAMLError) as e:
        print(f"EVG_E001: Failed to parse manifest: {e}", file=sys.stderr); sys.exit(3)

    working = copy.deepcopy(app_json)
    results, all_ok = apply_changes(working, requests)

    print(render_report(results, app_label, app_path.name,
                        args.output, args.dry_run, all_ok))

    if not args.dry_run and all_ok:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(working, indent=2, ensure_ascii=False), encoding="utf-8")
    elif not args.dry_run and not all_ok:
        print("\nEVG_E002: Output NOT written — one or more changes aborted.", file=sys.stderr)

    if args.json_report:
        report_data = {"tool": "evangelist", "version": __version__,
                       "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "app": app_label, "input": str(app_path), "output": args.output,
                       "dry_run": args.dry_run, "results": [r.to_dict() for r in results],
                       "verdict": "PASS" if all_ok else "FAIL"}
        Path(args.json_report).write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
