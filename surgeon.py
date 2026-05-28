#!/usr/bin/env python3
# filename: surgeon.py
"""
Surgeon v2.2.0 — deterministic, checksummed patch applicator for Logic App JSON.

Applies patches from an orthodox.yaml file to a source Logic App JSON.
Pre-op: verifies every field matches expected checksums before cutting.
Post-op: verifies every patched field matches expected checksums after.
Atomic: all patches apply or none do. Never writes partial results.

Supported operations:
    replace_value    — swap a leaf value (default)
    rename_key       — rename a dict key
    add_action       — insert a new key into an actions dict (supports position_after)
    remove_action    — delete a key from an actions dict
    rename_action    — rename an action key + rewrite all sibling runAfter refs
    rewire_runafter  — change an action's runAfter predecessor
    add_variable     — append a variable to an InitializeVariable array
    remove_variable  — delete a variable from an InitializeVariable array
    edit_variable    — change the value/type of a variable in an InitializeVariable array
    rename_variable  — change the name of a variable in an InitializeVariable array

Inputs:
    --input:      Source Logic App JSON file
    --patch-task: Orthodox YAML file with patch instructions
    --output:     Where to write the patched result
    --log:        Where to write the audit log

Outputs:
    Patched JSON file (--output)
    Audit log (--log)
    Exit 0: all patches applied and verified
    Exit 1: any check failed or error occurred
"""

from __future__ import annotations

__version__ = "2.2.0"

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — imported from the toolkit
# ─────────────────────────────────────────────────────────────────────────────

from helpers.checksum import fingerprint
from helpers.canonical_path_resolver import resolve


# ─────────────────────────────────────────────────────────────────────────────
# Valid operations
# ─────────────────────────────────────────────────────────────────────────────

VALID_OPERATIONS = {
    "replace_value", "rename_key",
    "add_action", "remove_action", "rename_action",
    "add_variable", "remove_variable", "edit_variable", "rename_variable",
    "rewire_runafter",
}

# Operations that target InitializeVariable arrays exclusively.
VARIABLE_OPS = {"add_variable", "remove_variable", "edit_variable", "rename_variable"}


# ─────────────────────────────────────────────────────────────────────────────
# InitializeVariable helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_init_var_array(doc: Dict[str, Any], path: str) -> list:
    """
    Resolve a path to the variables array of an InitializeVariable action.
    Verifies the parent action's type is InitializeVariable.

    Inputs:
        doc:  Root JSON object.
        path: Orthodox path ending at the action level (not .inputs.variables).
              e.g. "definition.actions.CONTROL_PANEL"

    Outputs:
        The variables list (by reference — mutations affect the doc).

    Raises:
        ValueError: If the action is not InitializeVariable or has no variables array.
    """
    parent, key = resolve(doc, path)
    action = parent[key]
    if not isinstance(action, dict):
        raise TypeError(f"Expected dict at {path}, got {type(action).__name__}")
    action_type = action.get("type", "")
    if action_type != "InitializeVariable":
        raise ValueError(
            f"Variable operations only work on InitializeVariable actions. "
            f"Action at {path} has type '{action_type}'."
        )
    variables = action.get("inputs", {}).get("variables")
    if not isinstance(variables, list):
        raise ValueError(f"No variables array found at {path}.inputs.variables")
    return variables


def _find_var_index(variables: list, name: str) -> int:
    """
    Find a variable entry by name in a variables array.

    Inputs:
        variables: The variables list from an InitializeVariable action.
        name:      The variable name to search for.

    Outputs:
        Integer index of the matching entry.

    Raises:
        ValueError: If no entry with that name exists.
    """
    for i, v in enumerate(variables):
        if isinstance(v, dict) and v.get("name") == name:
            return i
    raise ValueError(f"Variable '{name}' not found in array of {len(variables)} entries")


def _var_value_sha(var_entry: Dict[str, Any]) -> str:
    """
    Compute the SHA of a variable entry's value.

    Inputs:
        var_entry: A dict with at least a "value" key.

    Outputs:
        SHA256[:12] fingerprint of the value.
    """
    return fingerprint(var_entry.get("value", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Action helpers (rewire, rename, ordered insert)
# ─────────────────────────────────────────────────────────────────────────────

def _rewrite_runafter_refs(
    actions_dict: Dict[str, Any],
    old_name: str,
    new_name: str,
) -> int:
    """
    Scan all actions in a dict and rewrite runAfter references from
    old_name to new_name. Returns the count of rewritten references.

    Inputs:
        actions_dict: The actions dict (sibling scope).
        old_name:     The action name being replaced.
        new_name:     The new action name.

    Outputs:
        Number of runAfter entries rewritten.
    """
    count = 0
    for action_name, action in actions_dict.items():
        if not isinstance(action, dict):
            continue
        run_after = action.get("runAfter")
        if isinstance(run_after, dict) and old_name in run_after:
            run_after[new_name] = run_after.pop(old_name)
            count += 1
    return count


def _insert_after_key(
    target_dict: Dict[str, Any],
    sibling_key: str,
    new_key: str,
    new_value: Any,
) -> None:
    """
    Insert a key-value pair into a dict immediately after sibling_key.
    Preserves dict ordering for Portal display purposes.

    Inputs:
        target_dict: The dict to modify (mutated in place).
        sibling_key: The existing key to insert after.
        new_key:     The new key to insert.
        new_value:   The value for the new key.

    Raises:
        KeyError: If sibling_key doesn't exist in target_dict.
    """
    if sibling_key not in target_dict:
        raise KeyError(f"Sibling key '{sibling_key}' not found for positional insert")

    # Rebuild the dict with the new entry after the sibling.
    items = list(target_dict.items())
    target_dict.clear()
    for k, v in items:
        target_dict[k] = v
        if k == sibling_key:
            target_dict[new_key] = new_value


def _count_runafter_refs(actions_dict: Dict[str, Any], name: str) -> int:
    """
    Count how many actions in the dict have a runAfter reference to name.

    Inputs:
        actions_dict: The actions dict to scan.
        name:         The action name to search for.

    Outputs:
        Count of actions referencing name in their runAfter.
    """
    count = 0
    for action in actions_dict.values():
        if isinstance(action, dict):
            run_after = action.get("runAfter")
            if isinstance(run_after, dict) and name in run_after:
                count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class PatchResult:
    """Tracks the outcome of a single patch operation."""

    __slots__ = ("index", "section", "path", "operation",
                 "from_val", "to_val", "from_sha", "to_sha",
                 "key", "pre_op_ok", "post_op_ok", "actual_pre_sha",
                 "actual_post_sha", "error")

    def __init__(self, index: int, patch: Dict[str, Any]) -> None:
        self.index = index
        self.section = patch.get("section", "unknown")
        self.path = patch.get("path", "")
        self.operation = patch.get("operation", "replace_value")
        self.from_val = patch.get("from")
        self.to_val = patch.get("to")
        self.from_sha = patch.get("from_sha", "")
        self.to_sha = patch.get("to_sha", "")
        self.key = patch.get("key", "")
        self.pre_op_ok = False
        self.post_op_ok = False
        self.actual_pre_sha = ""
        self.actual_post_sha = ""
        self.error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surgeon",
        description=f"Surgeon v{__version__} — checksummed patch applicator for Logic App JSON.",
    )
    parser.add_argument("--input", required=True,
                        help="Source Logic App JSON file")
    parser.add_argument("--patch-task", required=True,
                        help="Orthodox YAML file with patch instructions")
    parser.add_argument("--output", required=True,
                        help="Where to write the patched result")
    parser.add_argument("--log", required=True,
                        help="Where to write the audit log")
    parser.add_argument("--version", action="version", version=f"surgeon {__version__}")
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Load phase
# ─────────────────────────────────────────────────────────────────────────────

def _load_inputs(
    input_path: str,
    patch_path: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {input_path}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(patch_path, "r", encoding="utf-8") as f:
            orthodox = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"ERROR: Patch-task file not found: {patch_path}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: Invalid YAML in {patch_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(orthodox, dict):
        print(f"ERROR: Orthodox YAML root must be a dict, got {type(orthodox).__name__}",
              file=sys.stderr)
        sys.exit(1)

    return doc, orthodox


def _validate_source_match(orthodox: Dict[str, Any], input_path: str) -> None:
    source_file = orthodox.get("source", {}).get("file", "")
    input_basename = os.path.basename(input_path)
    if source_file and source_file != input_basename:
        print(f"WARNING: orthodox source.file is '{source_file}' "
              f"but --input is '{input_basename}'", file=sys.stderr)


def _resolve_to_files(
    patches: List[Dict[str, Any]],
    patch_path: str,
) -> None:
    orthodox_dir = os.path.dirname(os.path.abspath(patch_path))

    for i, patch in enumerate(patches):
        to_file = patch.get("to_file")
        if to_file is None:
            continue

        if "to" in patch:
            print(f"ERROR: Patch {i+1} has both 'to' and 'to_file' — pick one.",
                  file=sys.stderr)
            sys.exit(1)

        file_path = os.path.join(orthodox_dir, to_file)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except FileNotFoundError:
            print(f"ERROR: to_file not found: {file_path} (patch {i+1})",
                  file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError:
            # Fallback: read as raw text. This covers HTML, CSS, and other
            # string values used by replace_value and edit_variable.
            # Operations that need structured JSON (add_action, add_variable)
            # will fail at apply time with a type error — correct behavior.
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

        # Auto-unwrap for add_action: if the loaded JSON is a single-key dict
        # and that key matches patch.key, unwrap to the inner value.
        # This lets step files be self-documenting: {"ActionName": {body}}
        # while surgeon inserts just the body at the key.
        operation = patch.get("operation", "replace_value")
        key = patch.get("key", "")
        if (operation == "add_action"
                and isinstance(content, dict)
                and len(content) == 1
                and key in content):
            content = content[key]

        patch["to"] = content
        del patch["to_file"]


def _validate_operations(patches: List[Dict[str, Any]]) -> None:
    for i, patch in enumerate(patches):
        op = patch.get("operation", "replace_value")
        if op not in VALID_OPERATIONS:
            print(f"ERROR: Patch {i+1} has unknown operation '{op}'. "
                  f"Valid: {', '.join(sorted(VALID_OPERATIONS))}",
                  file=sys.stderr)
            sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-op verification
# ─────────────────────────────────────────────────────────────────────────────

def _pre_op_verify(
    doc: Dict[str, Any],
    patches: List[Dict[str, Any]],
    results: List[PatchResult],
) -> bool:
    all_ok = True

    for i, (patch, result) in enumerate(zip(patches, results)):
        path = patch.get("path", "")
        operation = patch.get("operation", "replace_value")
        expected_sha = patch.get("from_sha", "")
        expected_val = patch.get("from")

        try:
            # ── Variable array operations ─────────────────────────────
            if operation == "add_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                # Key must NOT already exist.
                try:
                    _find_var_index(variables, key)
                    result.error = (f"PRE-OP FAIL: variable '{key}' already exists "
                                    f"in {path} — add_variable requires it be absent")
                    all_ok = False
                    continue
                except ValueError:
                    pass  # Good — variable doesn't exist yet.
                result.actual_pre_sha = "(absent)"
                result.pre_op_ok = True

            elif operation == "remove_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                try:
                    idx = _find_var_index(variables, key)
                except ValueError:
                    result.error = (f"PRE-OP FAIL: variable '{key}' not found "
                                    f"in {path} — nothing to remove")
                    all_ok = False
                    continue
                actual_sha = _var_value_sha(variables[idx])
                result.actual_pre_sha = actual_sha
                if expected_sha and actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}[{key}].value "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            elif operation == "edit_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                try:
                    idx = _find_var_index(variables, key)
                except ValueError:
                    result.error = (f"PRE-OP FAIL: variable '{key}' not found "
                                    f"in {path} — nothing to edit")
                    all_ok = False
                    continue
                actual_sha = _var_value_sha(variables[idx])
                result.actual_pre_sha = actual_sha
                if expected_sha and actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}[{key}].value "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            elif operation == "rename_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                # Old name must exist.
                try:
                    _find_var_index(variables, key)
                except ValueError:
                    result.error = (f"PRE-OP FAIL: variable '{key}' not found "
                                    f"in {path} — nothing to rename")
                    all_ok = False
                    continue
                # New name must NOT exist.
                to_name = patch.get("to", "")
                try:
                    _find_var_index(variables, to_name)
                    result.error = (f"PRE-OP FAIL: target name '{to_name}' already "
                                    f"exists in {path} — rename would collide")
                    all_ok = False
                    continue
                except ValueError:
                    pass  # Good — target name doesn't exist yet.
                actual_sha = fingerprint(key)
                result.actual_pre_sha = actual_sha
                if expected_sha and actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}[{key}] name "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            # ── Action operations ─────────────────────────────────────
            elif operation == "add_action":
                key = patch.get("key", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if not isinstance(target_dict, dict):
                    result.error = (f"PRE-OP FAIL: {path} resolves to "
                                    f"{type(target_dict).__name__}, need dict")
                    all_ok = False
                    continue
                if key in target_dict:
                    result.error = (f"PRE-OP FAIL: key '{key}' already exists at "
                                    f"{path} — add_action requires the key be absent")
                    all_ok = False
                    continue
                result.actual_pre_sha = "(absent)"
                result.pre_op_ok = True

            elif operation == "remove_action":
                key = patch.get("key", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if not isinstance(target_dict, dict):
                    result.error = (f"PRE-OP FAIL: {path} resolves to "
                                    f"{type(target_dict).__name__}, need dict")
                    all_ok = False
                    continue
                if key not in target_dict:
                    result.error = (f"PRE-OP FAIL: key '{key}' not found at "
                                    f"{path} — nothing to remove")
                    all_ok = False
                    continue
                current_val = target_dict[key]
                actual_sha = fingerprint(json.dumps(current_val, sort_keys=True, ensure_ascii=False))
                result.actual_pre_sha = actual_sha
                if expected_sha and actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}.{key} "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            elif operation == "rename_key":
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key] if isinstance(parent, dict) else parent
                if not isinstance(target_dict, dict):
                    target_dict = parent[parent_key]
                key_name = str(expected_val)
                if key_name not in target_dict:
                    result.error = f"Key '{key_name}' not found in dict at {path}"
                    all_ok = False
                    continue
                actual_sha = fingerprint(key_name)
                result.actual_pre_sha = actual_sha
                if actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}[{key_name}] "
                                    f"expected {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            elif operation == "rewire_runafter":
                # Pre-op: action must exist, old predecessor must be in its runAfter.
                key = patch.get("key", "")  # The action whose runAfter we're editing.
                from_pred = patch.get("from", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if not isinstance(target_dict, dict):
                    result.error = (f"PRE-OP FAIL: {path} resolves to "
                                    f"{type(target_dict).__name__}, need dict")
                    all_ok = False
                    continue
                if key not in target_dict:
                    result.error = (f"PRE-OP FAIL: action '{key}' not found at {path}")
                    all_ok = False
                    continue
                action = target_dict[key]
                run_after = action.get("runAfter", {})
                if not isinstance(run_after, dict):
                    result.error = (f"PRE-OP FAIL: {path}.{key}.runAfter is "
                                    f"{type(run_after).__name__}, need dict")
                    all_ok = False
                    continue
                if from_pred and from_pred not in run_after:
                    result.error = (f"PRE-OP FAIL: '{from_pred}' not in "
                                    f"{path}.{key}.runAfter (has: {list(run_after.keys())})")
                    all_ok = False
                    continue
                result.actual_pre_sha = fingerprint(json.dumps(run_after, sort_keys=True))
                if expected_sha and result.actual_pre_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}.{key}.runAfter "
                                    f"expected sha {expected_sha} got {result.actual_pre_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            elif operation == "rename_action":
                # Pre-op: old name must exist, new name must not.
                key = patch.get("key", "")  # old name
                to_name = str(patch.get("to", ""))
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if not isinstance(target_dict, dict):
                    result.error = (f"PRE-OP FAIL: {path} resolves to "
                                    f"{type(target_dict).__name__}, need dict")
                    all_ok = False
                    continue
                if key not in target_dict:
                    result.error = (f"PRE-OP FAIL: action '{key}' not found at {path}")
                    all_ok = False
                    continue
                if to_name in target_dict:
                    result.error = (f"PRE-OP FAIL: target name '{to_name}' already "
                                    f"exists at {path} — rename would collide")
                    all_ok = False
                    continue
                actual_sha = fingerprint(key)
                result.actual_pre_sha = actual_sha
                if expected_sha and actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path}.{key} name "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

            else:  # replace_value
                parent, key = resolve(doc, path)
                current_val = parent[key]
                actual_sha = fingerprint(current_val)
                result.actual_pre_sha = actual_sha
                if actual_sha != expected_sha:
                    result.error = (f"PRE-OP FAIL: {path} "
                                    f"expected sha {expected_sha} got {actual_sha}")
                    all_ok = False
                    continue
                if expected_val is not None and str(current_val) != str(expected_val):
                    result.error = (f"PRE-OP FAIL: {path} "
                                    f"value mismatch: expected {expected_val!r} "
                                    f"got {current_val!r}")
                    all_ok = False
                    continue
                result.pre_op_ok = True

        except (KeyError, ValueError, TypeError) as e:
            result.error = f"PRE-OP FAIL: {path} — {e}"
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────────────────────────────────────
# Apply patches
# ─────────────────────────────────────────────────────────────────────────────

def _apply_patches(
    doc: Dict[str, Any],
    patches: List[Dict[str, Any]],
    results: List[PatchResult],
) -> bool:
    all_ok = True

    for patch, result in zip(patches, results):
        path = patch.get("path", "")
        operation = patch.get("operation", "replace_value")
        to_val = patch.get("to")
        to_sha = patch.get("to_sha", "")

        try:
            # ── Variable array operations ─────────────────────────────
            if operation == "add_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                # to is the full {name, type, value} dict.
                new_entry = to_val if isinstance(to_val, dict) else {"name": key, "type": "string", "value": to_val}
                variables.append(new_entry)
                actual_sha = _var_value_sha(new_entry)
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}[{key}].value "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "remove_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                idx = _find_var_index(variables, key)
                variables.pop(idx)
                result.actual_post_sha = "(removed)"
                result.post_op_ok = True

            elif operation == "edit_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                idx = _find_var_index(variables, key)
                # Replace value. If to is a dict with "value" key, use that.
                # Otherwise treat to as the raw value.
                if isinstance(to_val, dict) and "value" in to_val:
                    # Allow changing type too if provided.
                    if "type" in to_val:
                        variables[idx]["type"] = to_val["type"]
                    variables[idx]["value"] = to_val["value"]
                else:
                    variables[idx]["value"] = to_val
                actual_sha = _var_value_sha(variables[idx])
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}[{key}].value "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rename_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                idx = _find_var_index(variables, key)
                new_name = str(to_val)
                variables[idx]["name"] = new_name
                actual_sha = fingerprint(new_name)
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}[{key}→{new_name}] "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            # ── Action operations ─────────────────────────────────────
            elif operation == "add_action":
                key = patch.get("key", "")
                position_after = patch.get("position_after")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if position_after:
                    _insert_after_key(target_dict, position_after, key, to_val)
                else:
                    target_dict[key] = to_val
                actual_sha = fingerprint(json.dumps(to_val, sort_keys=True, ensure_ascii=False))
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}.{key} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "remove_action":
                key = patch.get("key", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                del target_dict[key]
                result.actual_post_sha = "(removed)"
                result.post_op_ok = True

            elif operation == "rename_key":
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key] if isinstance(parent, dict) else parent
                if not isinstance(target_dict, dict):
                    target_dict = parent[parent_key]
                from_key = str(patch.get("from"))
                to_key = str(to_val)
                value = target_dict[from_key]
                target_dict[to_key] = value
                del target_dict[from_key]
                actual_sha = fingerprint(to_key)
                result.actual_post_sha = actual_sha
                if actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}[{to_key}] "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rewire_runafter":
                key = patch.get("key", "")
                from_pred = str(patch.get("from", ""))
                to_pred = str(to_val)
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                action = target_dict[key]
                run_after = action.get("runAfter", {})
                if from_pred and from_pred in run_after:
                    # Preserve the status array (e.g. ["Succeeded"]).
                    statuses = run_after.pop(from_pred)
                    run_after[to_pred] = statuses
                elif not from_pred:
                    # Adding a new predecessor (from is empty = adding, not replacing).
                    statuses = patch.get("statuses", ["Succeeded"])
                    run_after[to_pred] = statuses
                action["runAfter"] = run_after
                actual_sha = fingerprint(json.dumps(run_after, sort_keys=True))
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}.{key}.runAfter "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rename_action":
                key = patch.get("key", "")  # old name
                to_name = str(to_val)
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                # Move the action to the new key.
                action_value = target_dict[key]
                target_dict[to_name] = action_value
                del target_dict[key]
                # Rewrite all runAfter references in sibling actions.
                rewritten = _rewrite_runafter_refs(target_dict, key, to_name)
                actual_sha = fingerprint(to_name)
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path}.{key}→{to_name} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            else:  # replace_value
                parent, key = resolve(doc, path)
                parent[key] = to_val
                actual_sha = fingerprint(to_val)
                result.actual_post_sha = actual_sha
                if actual_sha != to_sha:
                    result.error = (f"POST-APPLY WARN: {path} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

        except (KeyError, ValueError, TypeError) as e:
            result.error = f"APPLY FAIL: {path} — {e}"
            result.post_op_ok = False
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────────────────────────────────────
# Post-op verification
# ─────────────────────────────────────────────────────────────────────────────

def _post_op_verify(
    doc: Dict[str, Any],
    patches: List[Dict[str, Any]],
    results: List[PatchResult],
) -> bool:
    all_ok = True

    for patch, result in zip(patches, results):
        path = patch.get("path", "")
        operation = patch.get("operation", "replace_value")
        to_sha = patch.get("to_sha", "")
        to_val = patch.get("to")

        try:
            # ── Variable array operations ─────────────────────────────
            if operation == "add_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                try:
                    idx = _find_var_index(variables, key)
                except ValueError:
                    result.error = f"POST-OP FAIL: added variable '{key}' not found at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = _var_value_sha(variables[idx])
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}[{key}].value "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "remove_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                try:
                    _find_var_index(variables, key)
                    result.error = f"POST-OP FAIL: variable '{key}' still exists at {path}"
                    result.post_op_ok = False
                    all_ok = False
                except ValueError:
                    result.actual_post_sha = "(confirmed removed)"
                    result.post_op_ok = True

            elif operation == "edit_variable":
                key = patch.get("key", "")
                variables = _resolve_init_var_array(doc, path)
                try:
                    idx = _find_var_index(variables, key)
                except ValueError:
                    result.error = f"POST-OP FAIL: variable '{key}' not found at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = _var_value_sha(variables[idx])
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}[{key}].value "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rename_variable":
                key = patch.get("key", "")
                to_name = str(to_val)
                variables = _resolve_init_var_array(doc, path)
                # Old name should be gone.
                try:
                    _find_var_index(variables, key)
                    result.error = f"POST-OP FAIL: old variable name '{key}' still exists at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                except ValueError:
                    pass  # Good.
                # New name should exist.
                try:
                    _find_var_index(variables, to_name)
                except ValueError:
                    result.error = f"POST-OP FAIL: new variable name '{to_name}' not found at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = fingerprint(to_name)
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}[{to_name}] "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            # ── Action operations ─────────────────────────────────────
            elif operation == "add_action":
                key = patch.get("key", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if key not in target_dict:
                    result.error = f"POST-OP FAIL: added key '{key}' not found at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = fingerprint(json.dumps(target_dict[key], sort_keys=True, ensure_ascii=False))
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}.{key} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "remove_action":
                key = patch.get("key", "")
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                if key in target_dict:
                    result.error = f"POST-OP FAIL: key '{key}' still exists at {path}"
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.actual_post_sha = "(confirmed removed)"
                    result.post_op_ok = True

            elif operation == "rename_key":
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key] if isinstance(parent, dict) else parent
                if not isinstance(target_dict, dict):
                    target_dict = parent[parent_key]
                to_key = str(to_val)
                if to_key not in target_dict:
                    result.error = f"POST-OP FAIL: renamed key '{to_key}' not found at {path}"
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = fingerprint(to_key)
                result.actual_post_sha = actual_sha
                if actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}[{to_key}] "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rewire_runafter":
                key = patch.get("key", "")
                to_pred = str(to_val)
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                action = target_dict[key]
                run_after = action.get("runAfter", {})
                # Verify the new predecessor is present.
                if to_pred not in run_after:
                    result.error = (f"POST-OP FAIL: '{to_pred}' not in "
                                    f"{path}.{key}.runAfter after rewire")
                    result.post_op_ok = False
                    all_ok = False
                    continue
                # Verify old predecessor is gone (if one was specified).
                from_pred = str(patch.get("from", ""))
                if from_pred and from_pred in run_after:
                    result.error = (f"POST-OP FAIL: old predecessor '{from_pred}' "
                                    f"still in {path}.{key}.runAfter")
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = fingerprint(json.dumps(run_after, sort_keys=True))
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}.{key}.runAfter "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            elif operation == "rename_action":
                key = patch.get("key", "")  # old name
                to_name = str(to_val)
                parent, parent_key = resolve(doc, path)
                target_dict = parent[parent_key]
                # Old name should be gone.
                if key in target_dict:
                    result.error = (f"POST-OP FAIL: old action name '{key}' "
                                    f"still exists at {path}")
                    result.post_op_ok = False
                    all_ok = False
                    continue
                # New name should exist.
                if to_name not in target_dict:
                    result.error = (f"POST-OP FAIL: new action name '{to_name}' "
                                    f"not found at {path}")
                    result.post_op_ok = False
                    all_ok = False
                    continue
                # No runAfter should still reference the old name.
                stale_refs = _count_runafter_refs(target_dict, key)
                if stale_refs > 0:
                    result.error = (f"POST-OP FAIL: {stale_refs} action(s) still "
                                    f"reference '{key}' in runAfter at {path}")
                    result.post_op_ok = False
                    all_ok = False
                    continue
                actual_sha = fingerprint(to_name)
                result.actual_post_sha = actual_sha
                if to_sha and actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path}.{to_name} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

            else:  # replace_value
                parent, key = resolve(doc, path)
                current_val = parent[key]
                actual_sha = fingerprint(current_val)
                result.actual_post_sha = actual_sha
                if actual_sha != to_sha:
                    result.error = (f"POST-OP FAIL: {path} "
                                    f"expected {to_sha} got {actual_sha}")
                    result.post_op_ok = False
                    all_ok = False
                else:
                    result.post_op_ok = True

        except (KeyError, ValueError, TypeError) as e:
            result.error = f"POST-OP FAIL: {path} — {e}"
            result.post_op_ok = False
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

def _build_audit_log(
    args: argparse.Namespace,
    orthodox: Dict[str, Any],
    results: List[PatchResult],
    success: bool,
) -> str:
    target_name = orthodox.get("target", {}).get("name", "UNKNOWN")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(results)

    lines = [
        f"SURGEON v{__version__} AUDIT LOG",
        f"Date: {now}",
        f"Input: {os.path.basename(args.input)}",
        f"Patch-task: {os.path.basename(args.patch_task)}",
        f"Output: {os.path.basename(args.output)}",
        f"Target: {target_name}",
        "",
    ]

    pre_pass = sum(1 for r in results if r.pre_op_ok)
    lines.append(f"PRE-OP CHECKS: {pre_pass}/{total} PASS")
    for r in results:
        tag = "PASS" if r.pre_op_ok else "FAIL"
        sha_display = r.actual_pre_sha or "n/a"
        mark = "✓" if r.pre_op_ok else "✗"
        lines.append(f"  [{tag}] {r.section}.{_short_path(r.path)}: {sha_display} {mark}")
        if not r.pre_op_ok and r.error:
            lines.append(f"         {r.error}")
    lines.append("")

    applied = sum(1 for r in results if r.post_op_ok)
    lines.append(f"PATCHES APPLIED: {applied}/{total}")
    for i, r in enumerate(results, 1):
        op_label = f" [{r.operation}]" if r.operation != "replace_value" else ""
        if r.operation in ("add_action", "remove_action", "add_variable",
                           "remove_variable", "rename_variable", "rename_action"):
            lines.append(f"  [{i}]{op_label} {_short_path(r.path)}.{r.key}")
        elif r.operation == "rewire_runafter":
            lines.append(f"  [{i}]{op_label} {_short_path(r.path)}.{r.key}: "
                          f"{r.from_val!r} → {r.to_val!r}")
        elif r.operation == "edit_variable":
            lines.append(f"  [{i}]{op_label} {_short_path(r.path)}[{r.key}]: "
                          f"{r.from_val!r} → {r.to_val!r}")
        else:
            lines.append(f"  [{i}]{op_label} {_short_path(r.path)}: "
                          f"{r.from_val!r} → {r.to_val!r}")
    lines.append("")

    post_pass = sum(1 for r in results if r.post_op_ok)
    lines.append(f"POST-OP CHECKS: {post_pass}/{total} PASS")
    for r in results:
        tag = "PASS" if r.post_op_ok else "FAIL"
        sha_display = r.actual_post_sha or "n/a"
        mark = "✓" if r.post_op_ok else "✗"
        lines.append(f"  [{tag}] {r.section}.{_short_path(r.path)}: {sha_display} {mark}")
        if not r.post_op_ok and r.error:
            lines.append(f"         {r.error}")
    lines.append("")

    if success:
        lines.append(f"RESULT: CLEAN — {total} patches applied, {total} verified")
    else:
        failed = total - applied
        lines.append(f"RESULT: FAILED — {applied}/{total} applied, {failed} failed")

    return "\n".join(lines) + "\n"


def _short_path(path: str) -> str:
    parts = path.split(".")
    action = ""
    for i, p in enumerate(parts):
        if p == "actions" and i + 1 < len(parts):
            action = parts[i + 1]
    tail = parts[-1] if parts else path
    if action:
        return f"{action}.{tail}"
    return tail


# ─────────────────────────────────────────────────────────────────────────────
# Write phase
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write_json(doc: Dict[str, Any], output_path: str) -> None:
    output_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=4, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_log(log_path: str, content: str) -> None:
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    doc, orthodox = _load_inputs(args.input, args.patch_task)
    _validate_source_match(orthodox, args.input)

    working = copy.deepcopy(doc)

    patches = orthodox.get("patches", [])
    if not isinstance(patches, list):
        print("ERROR: 'patches' must be a list in orthodox YAML", file=sys.stderr)
        sys.exit(1)

    _validate_operations(patches)
    _resolve_to_files(patches, args.patch_task)

    if len(patches) == 0:
        print("WARNING: Zero patches in orthodox YAML — writing unchanged copy",
              file=sys.stderr)
        _atomic_write_json(working, args.output)
        log = _build_audit_log(args, orthodox, [], True)
        _write_log(args.log, log)
        print(f"Output: {args.output} (unchanged copy)")
        sys.exit(0)

    results = [PatchResult(i, p) for i, p in enumerate(patches)]

    pre_ok = _pre_op_verify(working, patches, results)

    if not pre_ok:
        print("PRE-OP VERIFICATION FAILED — aborting, no output written.",
              file=sys.stderr)
        for r in results:
            if r.error:
                print(f"  {r.error}", file=sys.stderr)
        log = _build_audit_log(args, orthodox, results, False)
        _write_log(args.log, log)
        print(f"Audit log: {args.log}")
        sys.exit(1)

    apply_ok = _apply_patches(working, patches, results)
    post_ok = _post_op_verify(working, patches, results)

    success = apply_ok and post_ok

    if success:
        _atomic_write_json(working, args.output)
        print(f"Output: {args.output}")
    else:
        print("POST-OP VERIFICATION FAILED — no output written.",
              file=sys.stderr)
        for r in results:
            if r.error:
                print(f"  {r.error}", file=sys.stderr)

    log = _build_audit_log(args, orthodox, results, success)
    _write_log(args.log, log)
    print(f"Audit log: {args.log}")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
