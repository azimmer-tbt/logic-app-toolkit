#!/usr/bin/env python3
# filename: inquisitor.py
"""
Inquisitor v2.0.0 — Mode-driven field-level comparison engine for Logic Apps.

Reads Cartographer's fingerprints.json as ground truth, compares against
expectation documents (vitals.yaml, patch_task.yaml), reports what matches,
what's wrong, and what's missing.

Lives alongside cartographer.py, shares helpers/ package.

v2.0.0 changes (Phases A + D):
  Phase A — Fix multi-variable InitializeVariable lookup (Check 1a/1b):
    find_variable_in_fingerprints now walks ALL Initialize_* steps and matches
    by variables[N].name field value, ignoring the step_prefix hint from
    json_path. Fixes notification_emails MISSING when hint pointed to wrong step.
  Phase D — body() reference scope validation (Check 5, always-on):
    run_scope_validation() walks every SetVariable step, extracts body('X')
    expressions from inputs.value, verifies X is in same scope or parent scope.
    Catches Portal-invisible scope breaks before deploy. No vitals config needed.

Usage:
    python3 inquisitor.py --fingerprints fp.json --mode vitals --vitals vitals.yaml --app FRESHMART-DEV
    python3 inquisitor.py --fingerprints fp.json --mode vitals --vitals vitals.yaml --app FRESHMART-DEV --manifest MANIFEST.json --output-dir /tmp
    python3 inquisitor.py --version

Exit codes:
    0  All fields MATCH or PRESENT (including SCOPE_VALID)
    1  At least one WRONG_VALUE, MISSING, SCOPE_VIOLATION, or SCOPE_DANGLING
    2  All values correct but at least one HASH_MISMATCH
    3  Fatal error (bad input, missing app, chop mismatch)
"""
from __future__ import annotations

__version__ = "2.0.0"

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (co-located with cartographer)
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from helpers.checksum import fingerprint as helpers_fingerprint
except ImportError:
    def helpers_fingerprint(value: Any, chop: int = 12) -> str:
        if isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = repr(value).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:chop]


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class ComparisonRequest:
    """One field to compare."""
    __slots__ = (
        "field_id", "category", "lookup_strategy", "lookup_args",
        "expected_value", "expected_checksum", "mode", "notes",
    )

    def __init__(
        self,
        field_id: str,
        category: str,
        lookup_strategy: str,
        lookup_args: dict,
        expected_value: Optional[str],
        expected_checksum: Optional[str],
        mode: str,
        notes: str = "",
    ):
        self.field_id = field_id
        self.category = category
        self.lookup_strategy = lookup_strategy
        self.lookup_args = lookup_args
        self.expected_value = expected_value
        self.expected_checksum = expected_checksum
        self.mode = mode
        self.notes = notes


class ComparisonResult:
    """One field comparison outcome."""
    __slots__ = (
        "field_id", "category", "verdict", "expected_value", "actual_value",
        "expected_checksum", "actual_checksum", "fingerprints_key", "mode", "notes",
    )

    def __init__(self, req: ComparisonRequest):
        self.field_id = req.field_id
        self.category = req.category
        self.verdict = "PENDING"
        self.expected_value = req.expected_value
        self.actual_value = None
        self.expected_checksum = req.expected_checksum
        self.actual_checksum = None
        self.fingerprints_key = None
        self.mode = req.mode
        self.notes = req.notes

    def to_dict(self) -> dict:
        return {
            "field_id": self.field_id,
            "category": self.category,
            "verdict": self.verdict,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "expected_checksum": self.expected_checksum,
            "actual_checksum": self.actual_checksum,
            "fingerprints_key": self.fingerprints_key,
            "mode": self.mode,
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# YAML preprocessing
# ─────────────────────────────────────────────────────────────────────────────

def fix_yaml_brackets(raw: str) -> str:
    """
    Quote json_path values containing [N] or [LANG] so YAML parser
    doesn't interpret them as sequence literals.
    Only touches json_path values. Idempotent.
    """
    fixed = re.sub(
        r'json_path:\s+([^"\n{},]+\[[^\]]+\][^"\n{},]*)',
        lambda m: f'json_path: "{m.group(1).strip()}"',
        raw,
    )
    fixed = re.sub(
        r'json_path:\s+([^"\n]+\[LANG\][^"\n]*)',
        lambda m: f'json_path: "{m.group(1).strip()}"',
        fixed,
    )
    return fixed


PREPROCESSORS = {"fix_yaml_brackets": fix_yaml_brackets}

SENTINEL_VALUES = {"NEW_VAR", "SEE_MANIFEST"}


def detect_chop(checksums: List[str]) -> int:
    real = [c for c in checksums if c not in SENTINEL_VALUES and c]
    if not real:
        return 0
    lengths = set(len(c) for c in real)
    if len(lengths) > 1:
        raise ValueError(
            f"INQ_E005: Inconsistent checksum lengths in input: {lengths}. "
            f"Sample checksums: {real[:5]}"
        )
    return lengths.pop()


# ─────────────────────────────────────────────────────────────────────────────
# Manifest resolution
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest(manifest_path: Optional[Path]) -> Dict[str, str]:
    if manifest_path is None or not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _resolve_manifest(file_path: str, manifest: Dict[str, str]) -> Optional[str]:
    if not manifest:
        return None
    return manifest.get(file_path)


def _detect_manifest_prefix_mismatch(
    file_path: str, manifest: Dict[str, str],
) -> Optional[str]:
    if not manifest:
        return None
    parts = file_path.split("/")
    for i in range(1, len(parts)):
        shorter = "/".join(parts[i:])
        if shorter in manifest:
            return shorter
    return None


def _find_asset_file(file_path: str) -> Optional[str]:
    candidates = [
        file_path,
        f"html_assets/html_assets/{file_path.split('html_assets/')[-1]}" if "html_assets/" in file_path else None,
        f"html_assets/{file_path}",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def fix_manifest_paths(
    manifest_path: Path,
    manifest: Dict[str, str],
    vitals_paths: List[str],
) -> Dict[str, str]:
    import shutil
    prefix_to_add = None
    for vp in vitals_paths:
        parts = vp.split("/")
        for i in range(1, len(parts)):
            shorter = "/".join(parts[i:])
            if shorter in manifest:
                prefix_to_add = "/".join(parts[:i])
                break
        if prefix_to_add is not None:
            break
    if prefix_to_add is None:
        return manifest
    new_manifest = {f"{prefix_to_add}/{k}": v for k, v in manifest.items()}
    bak_path = manifest_path.with_suffix(".json.bak")
    shutil.copy2(manifest_path, bak_path)
    manifest_path.write_text(
        json.dumps(new_manifest, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return new_manifest


# ─────────────────────────────────────────────────────────────────────────────
# Vitals parser
# ─────────────────────────────────────────────────────────────────────────────

def vitals_parser(
    vitals_path: Path,
    app_name: str,
    manifest: Dict[str, str],
    modes_def: dict,
) -> List[ComparisonRequest]:
    """Parse vitals.yaml for a single app, return comparison requests."""
    raw = vitals_path.read_text(encoding="utf-8")
    for pp_name in modes_def.get("preprocess", []):
        pp_func = PREPROCESSORS.get(pp_name)
        if pp_func:
            raw = pp_func(raw)
    data = yaml.safe_load(raw)
    apps = data.get("apps", {})
    if app_name not in apps:
        raise KeyError(f"INQ_E004: App '{app_name}' not found in vitals.yaml. "
                       f"Available: {list(apps.keys())}")
    app = apps[app_name]
    requests: List[ComparisonRequest] = []

    # ── Variables ──────────────────────────────────────────────────────────
    for var_name, var_spec in app.get("variables", {}).items():
        if not isinstance(var_spec, dict):
            continue
        value = str(var_spec.get("value", ""))
        checksum = str(var_spec.get("checksum", ""))
        json_path_hint = var_spec.get("json_path", "")
        # Phase A fix: step_prefix is tiebreaker only — find_variable_in_fingerprints
        # walks ALL Initialize_* steps regardless of hint.
        step_prefix = json_path_hint.split(".")[0] if json_path_hint else ""
        requests.append(ComparisonRequest(
            field_id=f"variables.{var_name}",
            category="variables",
            lookup_strategy="by_variable_name",
            lookup_args={"step_prefix": step_prefix, "var_name": var_name},
            expected_value=value,
            expected_checksum=checksum,
            mode="vitals",
            notes=var_spec.get("note", ""),
        ))

    # ── HTML files ─────────────────────────────────────────────────────────
    html_sections = {
        "compose_body": {
            "action_template": "Compose_{Lang}_Email",
            "field_path": "inputs",
            "hash_stripped_file": False,
        },
        "subject": {
            "action_template": "Send_{Lang}_Email",
            "field_path": "inputs.body.Subject",
            "hash_stripped_file": False,
        },
        "button": {
            "action_template": "Store_Return_Button_-_Dev_Mobile_-_{Lang}",
            "field_path": "inputs",
            "hash_stripped_file": True,
        },
        "additional_info_links": {
            "action_template": "Store_Additional_Info_Links_-_Mobile_-_{Lang}",
            "field_path": "inputs",
            "hash_stripped_file": True,
        },
    }

    for section_name, section_conf in html_sections.items():
        section_data = app.get("html_files", {}).get(section_name, {})
        if not isinstance(section_data, dict):
            continue
        for lang, lang_spec in section_data.items():
            if lang == "json_path" or not isinstance(lang_spec, dict):
                continue
            file_path = lang_spec.get("file", "")
            checksum = str(lang_spec.get("checksum", ""))
            resolved_checksum = checksum
            resolve_note = ""
            if checksum == "SEE_MANIFEST":
                if section_conf.get("hash_stripped_file") and file_path:
                    try:
                        abs_path = _find_asset_file(file_path)
                        if abs_path:
                            with open(abs_path, "r", encoding="utf-8") as fh:
                                stripped = fh.read().strip()
                            resolved_checksum = helpers_fingerprint(stripped, chop=10)
                            resolve_note = f"hashed stripped file: {resolved_checksum}"
                        else:
                            resolved_checksum = "UNRESOLVED"
                            resolve_note = f"file not found: {file_path}"
                    except Exception as e:
                        resolved_checksum = "UNRESOLVED"
                        resolve_note = f"hash error: {e}"
                else:
                    resolved = _resolve_manifest(file_path, manifest)
                    if resolved:
                        resolved_checksum = resolved
                        resolve_note = f"resolved from MANIFEST: {resolved_checksum}"
                    else:
                        resolved_checksum = "UNRESOLVED"
                        prefix_hit = _detect_manifest_prefix_mismatch(file_path, manifest)
                        if prefix_hit:
                            resolve_note = (
                                f"PATH_MISMATCH: vitals says '{file_path}' "
                                f"but manifest has '{prefix_hit}'. "
                                f"Run with --fix-manifest-paths to correct."
                            )
                        else:
                            resolve_note = f"SEE_MANIFEST but '{file_path}' not in manifest"
            lang_title = lang.capitalize()
            action_name = section_conf["action_template"].replace("{Lang}", lang_title)
            requests.append(ComparisonRequest(
                field_id=f"html_files.{section_name}.{lang}",
                category=f"html_{section_name}",
                lookup_strategy="by_action_field",
                lookup_args={
                    "action_name": action_name,
                    "field_path": section_conf["field_path"],
                    "file_path": file_path,
                },
                expected_value=None,
                expected_checksum=resolved_checksum,
                mode="vitals",
                notes=resolve_note,
            ))

    return requests


# ─────────────────────────────────────────────────────────────────────────────
# Comparison engine
# ─────────────────────────────────────────────────────────────────────────────

def find_variable_in_fingerprints(
    fps_steps: Dict[str, Any],
    step_prefix: str,
    var_name: str,
) -> Optional[tuple]:
    """
    Phase A fix (v2.0.0): Walk ALL Initialize_* steps regardless of step_prefix.
    step_prefix is tiebreaker only — a wrong hint no longer causes MISSING.
    Returns (scoped_key, value_str, fingerprint_str) or None.
    """
    candidates: List[tuple] = []
    for scoped_key, step in fps_steps.items():
        action_name = step.get("action_name", "")
        if not action_name.startswith("Initialize_"):
            continue
        fields = step.get("fields", {})
        if fields.get("type", {}).get("value") != "InitializeVariable":
            continue
        name_field = None
        for rel_path, info in fields.items():
            if rel_path.endswith(".name") and info.get("value") == var_name:
                name_field = rel_path
                break
        if name_field is None:
            continue
        value_path = name_field.rsplit(".name", 1)[0] + ".value"
        if value_path in fields:
            vinfo = fields[value_path]
            candidates.append((scoped_key, vinfo["value"], vinfo["fingerprint"],
                                action_name == step_prefix))
    if not candidates:
        return None
    for c in candidates:
        if c[3]:
            return (c[0], c[1], c[2])
    return (candidates[0][0], candidates[0][1], candidates[0][2])


def find_action_field_in_fingerprints(
    fps_steps: Dict[str, Any],
    action_name: str,
    field_path: str,
) -> Optional[tuple]:
    """Find an action field in fingerprints. Returns (scoped_key, value, fp) or None."""
    for scoped_key, step in fps_steps.items():
        if step["action_name"] != action_name:
            continue
        fields = step.get("fields", {})
        if field_path in fields:
            info = fields[field_path]
            return (scoped_key, info["value"], info["fingerprint"])
        if field_path == "inputs" and "inputs" in fields:
            info = fields["inputs"]
            return (scoped_key, info["value"], info["fingerprint"])
    return None


def run_comparison(
    requests: List[ComparisonRequest],
    fps: dict,
    detected_chop: int,
) -> List[ComparisonResult]:
    """Run all comparison requests against fingerprints."""
    fps_steps = fps.get("steps", {})
    results: List[ComparisonResult] = []

    for req in requests:
        result = ComparisonResult(req)
        found = None
        if req.lookup_strategy == "by_variable_name":
            found = find_variable_in_fingerprints(
                fps_steps, req.lookup_args["step_prefix"], req.lookup_args["var_name"],
            )
        elif req.lookup_strategy == "by_action_field":
            found = find_action_field_in_fingerprints(
                fps_steps, req.lookup_args["action_name"], req.lookup_args["field_path"],
            )

        if found is None:
            result.verdict = "MISSING"
            results.append(result)
            continue

        scoped_key, actual_value, actual_fp = found
        result.fingerprints_key = scoped_key
        result.actual_value = actual_value
        if detected_chop > 0 and len(actual_fp) > detected_chop:
            actual_fp = actual_fp[:detected_chop]
        result.actual_checksum = actual_fp

        if req.expected_checksum == "NEW_VAR":
            result.verdict = "WRONG_VALUE" if (
                req.expected_value is not None and actual_value != req.expected_value
            ) else "PRESENT"
            if result.verdict == "PRESENT":
                result.notes = (result.notes + " [NEW_VAR]").strip()
            results.append(result)
            continue

        if req.expected_checksum == "UNRESOLVED":
            result.verdict = "UNRESOLVED"
            results.append(result)
            continue

        if req.expected_value is not None and actual_value != req.expected_value:
            result.verdict = "WRONG_VALUE"
            results.append(result)
            continue

        if req.expected_checksum:
            expected_cs = req.expected_checksum
            if detected_chop > 0 and len(expected_cs) > detected_chop:
                expected_cs = expected_cs[:detected_chop]
            result.expected_checksum = expected_cs
            if actual_fp == expected_cs:
                result.verdict = "MATCH"
            elif req.expected_value is not None and actual_value == req.expected_value:
                result.verdict = "HASH_MISMATCH"
            else:
                result.verdict = "WRONG_VALUE"
        else:
            result.verdict = "MATCH"

        results.append(result)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Phase D — body() scope validation (always-on, no vitals config needed)
# ─────────────────────────────────────────────────────────────────────────────

_BODY_REF_RE = re.compile(r"body\('([^']+)'\)", re.IGNORECASE)


def _scope_is_accessible(referencing_scope: str, target_scope: str) -> bool:
    """
    Conservative check: root targets always accessible; same-container accessible;
    different non-root scopes treated as inaccessible (zero false negatives).
    """
    if target_scope == "root":
        return True
    if referencing_scope == target_scope:
        return True
    return False


class ScopeResult:
    """One body() reference validation outcome."""
    __slots__ = ("referencing_step", "referencing_scope", "target_step",
                 "target_scope", "verdict", "notes")

    def __init__(self, referencing_step, referencing_scope, target_step,
                 target_scope, verdict, notes=""):
        self.referencing_step = referencing_step
        self.referencing_scope = referencing_scope
        self.target_step = target_step
        self.target_scope = target_scope
        self.verdict = verdict
        self.notes = notes

    def to_dict(self) -> dict:
        return {
            "referencing_step": self.referencing_step,
            "referencing_scope": self.referencing_scope,
            "target_step": self.target_step,
            "target_scope": self.target_scope,
            "verdict": self.verdict,
            "notes": self.notes,
        }


def run_scope_validation(fps: dict) -> List[ScopeResult]:
    """
    Phase D — Check 5: body() reference scope validation. Always-on.

    Walk every SetVariable step, extract body('X') refs from inputs.value,
    verify X is reachable from the referencing step's scope.

    Verdicts: SCOPE_VALID | SCOPE_VIOLATION | SCOPE_DANGLING
    """
    fps_steps = fps.get("steps", {})
    scope_list_by_action: Dict[str, List[str]] = {}
    for scoped_key, step in fps_steps.items():
        aname = step.get("action_name", "")
        spath = step.get("scope_path", "root")
        if aname:
            scope_list_by_action.setdefault(aname, []).append(spath)

    results: List[ScopeResult] = []
    for scoped_key, step in fps_steps.items():
        fields = step.get("fields", {})
        if fields.get("type", {}).get("value") != "SetVariable":
            continue
        referencing_step = step.get("action_name", scoped_key)
        referencing_scope = step.get("scope_path", "root")
        value_str = fields.get("inputs.value", {}).get("value", "")
        if not value_str:
            continue
        refs = _BODY_REF_RE.findall(value_str)
        if not refs:
            continue
        for target_step in refs:
            if target_step not in scope_list_by_action:
                results.append(ScopeResult(
                    referencing_step, referencing_scope, target_step, None,
                    "SCOPE_DANGLING",
                    f"body('{target_step}') — step not found in fingerprints",
                ))
                continue
            target_scopes = scope_list_by_action[target_step]
            any_accessible = any(
                _scope_is_accessible(referencing_scope, ts) for ts in target_scopes
            )
            display_scope = (target_scopes[0] if len(target_scopes) == 1
                             else target_scopes[0] + f" (+{len(target_scopes)-1} more)")
            if any_accessible:
                results.append(ScopeResult(
                    referencing_step, referencing_scope, target_step,
                    display_scope, "SCOPE_VALID",
                ))
            else:
                results.append(ScopeResult(
                    referencing_step, referencing_scope, target_step,
                    display_scope, "SCOPE_VIOLATION",
                    f"body('{target_step}') in scope '{display_scope}' "
                    f"not accessible from '{referencing_scope}'",
                ))
    return results



# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_MODES_FILE = _SCRIPT_DIR / "inqu__modes_definition.yaml"

# Parser dispatch table — maps parser name from modes YAML to function.
_PARSER_DISPATCH = {
    "vitals_parser": vitals_parser,
}


def _load_modes_definition() -> dict:
    """
    Load the modes definition YAML from the script directory.

    Inputs:
      None (reads from _MODES_FILE).

    Outputs:
      Dict of mode_name -> mode_config.

    Raises:
      SystemExit with INQ_E006 if file missing or unparseable.
    """
    if not _MODES_FILE.is_file():
        print(
            f"INQ_E006: Modes definition not found: {_MODES_FILE}",
            file=sys.stderr,
        )
        sys.exit(3)
    try:
        raw = _MODES_FILE.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        return data.get("modes", {})
    except Exception as exc:
        print(
            f"INQ_E006: Failed to parse modes definition: {exc}",
            file=sys.stderr,
        )
        sys.exit(3)
