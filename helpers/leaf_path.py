#!/usr/bin/env python3
# filename: leaf_path.py
"""
Leaf-path walker for Logic App JSON structures.

Walks a JSON object and produces a flat table of every leaf value with
its full rooted path. Paths use dot notation for dict keys and bracket
notation for array indices, matching the convention used in vitals.yaml
and patcher config files.

Path format:
  ActionName.inputs.variables[0].value
  ActionName.runAfter.Previous_Step[0]

This module is intentionally non-opinionated about what to DO with the
paths. Cartographer prints them. Patcher uses them for find-and-replace.
Verifier uses them for drift detection. Recon uses them for inventory.

Designed to be upstream-able: depends only on checksum.py from the same
helpers package. No Logic-App-specific assumptions beyond the path format.

Public API:
  walk_leaves(node, prefix="")
      Walk a JSON node, yield (path, value) for every leaf.

  walk_step_fields(action_name, raw_action, chop=12)
      Walk a single step's raw JSON. Returns dict of:
        { relative_path: { "value", "fingerprint", "json_path" } }

  build_fingerprints_for_app(defn, chop=12)
      Walk entire Logic App definition (all scopes). Returns dict of:
        { scoped_key: { "action_name", "scope_path", "step_fingerprint",
                        "run_after_fingerprint", "fields": {...} } }
      Scoped keys prevent collisions for duplicate names at different levels.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Tuple

from helpers.checksum import fingerprint, fingerprint_canonical, fingerprint_run_after


# ─────────────────────────────────────────────────────────────────────────────
# Core leaf walker
# ─────────────────────────────────────────────────────────────────────────────

def walk_leaves(
    node: Any,
    prefix: str = "",
) -> Iterator[Tuple[str, Any]]:
    """
    Walk a JSON node and yield (path, value) for every leaf.

    Inputs:
      node: Any JSON-serializable object.
      prefix: Path prefix (used in recursion, start with "").

    Outputs:
      Iterator of (dotted_path, leaf_value) tuples.
      Leaf values are str, int, float, bool, or None.

    Path conventions:
      - Dict keys:     "parent.child"
      - Array indices:  "parent[0]"
      - Nested:        "parent.child[0].grandchild"
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{prefix}.{key}" if prefix else key
            yield from walk_leaves(value, child_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            item_path = f"{prefix}[{i}]"
            yield from walk_leaves(item, item_path)
    else:
        yield (prefix, node)


# ─────────────────────────────────────────────────────────────────────────────
# Step-level field fingerprinting
# ─────────────────────────────────────────────────────────────────────────────

def walk_step_fields(
    action_name: str,
    raw_action: Dict[str, Any],
    chop: int = 12,
) -> Dict[str, Dict[str, str]]:
    """
    Walk all leaf fields in a single step and fingerprint each one.

    Inputs:
      action_name: The step's code name (used as root of json_path).
      raw_action: The raw action dict from the Logic App definition.
      chop: Truncation length for fingerprints.

    Outputs:
      Dict mapping relative_field_path to:
        {
          "value": <string representation of the leaf value>,
          "fingerprint": <SHA-256 hex truncated to chop>,
          "json_path": <full rooted path: ActionName.field[index].leaf>
        }

    The json_path is what other tools (patcher, verifier, recon) use as
    a literal lookup address into the source JSON.
    """
    fields: Dict[str, Dict[str, str]] = {}

    if not isinstance(raw_action, dict):
        return fields

    for relative_path, leaf_value in walk_leaves(raw_action):
        value_str = str(leaf_value) if leaf_value is not None else ""
        fp = fingerprint(leaf_value, chop)
        full_json_path = f"{action_name}.{relative_path}" if relative_path else action_name

        fields[relative_path] = {
            "value": value_str,
            "fingerprint": fp,
            "json_path": full_json_path,
        }

    return fields


# ─────────────────────────────────────────────────────────────────────────────
# Full-app fingerprint builder
# ─────────────────────────────────────────────────────────────────────────────

def build_fingerprints_for_app(
    defn: Dict[str, Any],
    chop: int = 12,
) -> Dict[str, Any]:
    """
    Walk the entire Logic App definition and build a complete fingerprint map.

    Operates on the RAW definition (not the registry) to capture all steps
    at all scope levels, including duplicates that the registry deduplicates.

    Inputs:
      defn: Logic App definition (the object containing 'actions').
      chop: Truncation length for all fingerprints.

    Outputs:
      Dict mapping scoped_key to step fingerprint data:
        {
          "<scope_path>.<action_name>": {
            "action_name": str,
            "scope_path": str,
            "step_fingerprint": str,       (whole-step canonical hash)
            "run_after_fingerprint": str,   (runAfter-only hash)
            "fields": {                     (per-leaf field fingerprints)
              relative_path: {
                "value": str,
                "fingerprint": str,
                "json_path": str            (full rooted path for lookup)
              }
            }
          }
        }

    Scoped keys use dot-separated scope paths so duplicate action names
    at different nesting levels don't collide in the output dict.
    """
    result: Dict[str, Any] = {}

    def _walk_actions(actions: Any, scope_path: str) -> None:
        if not isinstance(actions, dict):
            return

        for action_name, action_obj in actions.items():
            if not isinstance(action_obj, dict):
                continue

            scoped_key = f"{scope_path}.{action_name}" if scope_path else action_name
            run_after = action_obj.get("runAfter", {})

            result[scoped_key] = {
                "action_name": action_name,
                "scope_path": scope_path or "root",
                "step_fingerprint": fingerprint_canonical(action_obj, chop),
                "run_after_fingerprint": fingerprint_run_after(run_after, chop),
                "fields": walk_step_fields(action_name, action_obj, chop),
            }

            # Recurse into containers.
            nested_actions = action_obj.get("actions")
            if isinstance(nested_actions, dict):
                _walk_actions(nested_actions, scoped_key)

            # If/Condition else branch.
            else_obj = action_obj.get("else")
            if isinstance(else_obj, dict):
                else_actions = else_obj.get("actions")
                if isinstance(else_actions, dict):
                    _walk_actions(else_actions, f"{scoped_key}.else")

            # Switch cases.
            cases = action_obj.get("cases")
            if isinstance(cases, dict):
                for case_name, case_obj in cases.items():
                    if isinstance(case_obj, dict):
                        case_actions = case_obj.get("actions")
                        if isinstance(case_actions, dict):
                            _walk_actions(case_actions, f"{scoped_key}.cases.{case_name}")

            # Switch default.
            default_obj = action_obj.get("default")
            if isinstance(default_obj, dict):
                default_actions = default_obj.get("actions")
                if isinstance(default_actions, dict):
                    _walk_actions(default_actions, f"{scoped_key}.default")

    top_actions = defn.get("actions")
    if isinstance(top_actions, dict):
        _walk_actions(top_actions, "")

    return result
