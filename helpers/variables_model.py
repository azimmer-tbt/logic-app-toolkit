#!/usr/bin/env python3
# filename: variables_model.py
"""
Helpers for variable and summary metadata (SPEC-ANL-VARS-1).

This module is responsible for:

- Collecting variable definitions and references from the analyzer registry.
- Loading any existing variables.json from a previous run.
- Merging current run data with previous runs to compute status and metadata.
- Writing variables.json in the Stage 1 contract format.

Summary metadata is handled in a separate helper (summary_model.py).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Set

# ------------------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------------------


@dataclass
class VariableRecord:
    """
    Internal representation of a single variable for the current run.

    This structure is not written directly to JSON. It is converted into a plain
    dict when merged with previous runs in merge_variable_status().
    """

    name: str
    type_name: str = "Unknown"
    defined_by: Set[str] = field(default_factory=set)
    referenced_by: Set[str] = field(default_factory=set)


# ------------------------------------------------------------------------------
# Public API (used by 1_analyzer.py)
# ------------------------------------------------------------------------------


def collect_variables(
    defn: Mapping[str, Any],
    reg: Mapping[str, Any],
) -> Dict[str, VariableRecord]:
    """
    Collect variables defined and referenced in the current flow.

    Args:
        defn: The core Logic App definition (not used directly today, but kept for
            future enhancements where variables might be inferred from top-level
            metadata).
        reg: The StepInfo registry produced by collect_registry(). StepInfo
            instances are expected to expose a .raw dict and a .name string.

    Returns:
        A mapping of variable name to VariableRecord, where each record contains:
        - type_name: Normalised variable type (String, Int, Bool, etc.).
        - defined_by: Set of step code_names that initialise/create this variable.
        - referenced_by: Set of step code_names that call variables('name').
    """

    _ = defn  # Reserved for potential future use.

    variables: Dict[str, VariableRecord] = {}

    # First pass: collect variable definitions from InitializeVariable steps.
    for step_name, step in reg.items():
        raw = _get_step_raw(step)
        if not isinstance(raw, Mapping):
            continue

        step_type = _get_step_type(step, raw)
        if step_type != "InitializeVariable":
            continue

        inputs = _safe_get_mapping(raw, "inputs")
        variables_list = inputs.get("variables", [])
        if not isinstance(variables_list, Iterable):
            continue

        for var in variables_list:
            if not isinstance(var, Mapping):
                continue

            var_name = str(var.get("name") or "").strip()
            if not var_name:
                continue

            raw_type = var.get("type")
            inferred_type = _normalise_type(raw_type)

            record = variables.get(var_name)
            if record is None:
                record = VariableRecord(name=var_name, type_name=inferred_type)
                variables[var_name] = record
            else:
                # If a previous record had Unknown type, and we now have a better
                # type, upgrade it.
                if record.type_name == "Unknown" and inferred_type != "Unknown":
                    record.type_name = inferred_type

            record.defined_by.add(str(step_name))

    # Second pass: collect references via variables('name') across all steps.
    for step_name, step in reg.items():
        raw = _get_step_raw(step)
        if not isinstance(raw, Mapping):
            continue

        for var_name in _scan_for_variable_references(raw):
            record = variables.get(var_name)
            if record is None:
                # This variable is referenced but not defined in this flow.
                record = VariableRecord(name=var_name, type_name="Unknown")
                variables[var_name] = record

            record.referenced_by.add(str(step_name))

    return variables


def load_previous_variables(flow_root: Path) -> Dict[str, Any]:
    """
    Load variables.json from a previous analyzer run, if it exists.

    Args:
        flow_root: Output directory for the current flow (same as out_dir in
            1_analyzer.py).

    Returns:
        A dict representing the previous variables.json root. If the file is
        missing or invalid, an empty structure with a variables key is returned:

            {
                "schema_version": 1,
                "variables": []
            }
    """

    path = flow_root / "variables.json"
    if not path.is_file():
        return {"schema_version": 1, "variables": []}

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("Failed to load %s: %s", path, exc)
        return {"schema_version": 1, "variables": []}

    if not isinstance(data, Mapping):
        return {"schema_version": 1, "variables": []}

    # Normalise minimal shape.
    variables = data.get("variables") or []
    if not isinstance(variables, list):
        variables = []

    schema_version = data.get("schema_version", 1)
    if not isinstance(schema_version, int):
        schema_version = 1

    return {
        "schema_version": schema_version,
        "variables": variables,
    }


def merge_variable_status(
    current_map: Mapping[str, VariableRecord],
    prev_root: Mapping[str, Any],
    run_id: str,
) -> List[Dict[str, Any]]:
    """
    Merge current run variable data with previous runs to compute status and
    metadata fields.

    Args:
        current_map: Mapping of variable name to VariableRecord for this run.
        prev_root: Parsed contents of the previous variables.json (or an empty
            structure if none existed).
        run_id: A string uniquely identifying this analyzer run, typically an
            ISO timestamp (UTC, seconds precision).

    Returns:
        A list of JSON-ready dicts, one per variable, sorted by variable name.
        Each entry has the shape:

            {
                "name": "...",
                "type": "String",
                "defined_by": [...],
                "referenced_by": [...],
                "status": "current" | "new" | "deprecated",
                "metadata": {
                    "created_at": "...",
                    "last_seen_run_id": "..."
                }
            }
    """

    previous_vars = prev_root.get("variables") or []
    previous_by_name: Dict[str, Mapping[str, Any]] = {}

    if isinstance(previous_vars, list):
        for item in previous_vars:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            previous_by_name[name] = item

    merged: List[Dict[str, Any]] = []

    # First, handle variables that exist in the current run.
    for name in sorted(current_map.keys()):
        record = current_map[name]
        prev = previous_by_name.get(name)

        # Base metadata.
        prev_metadata = {}
        if isinstance(prev, Mapping):
            prev_metadata = prev.get("metadata") or {}
            if not isinstance(prev_metadata, Mapping):
                prev_metadata = {}

        created_at = str(prev_metadata.get("created_at") or "").strip()
        if not created_at:
            created_at = run_id

        entry: Dict[str, Any] = {
            "name": record.name,
            "type": record.type_name,
            "defined_by": sorted(record.defined_by),
            "referenced_by": sorted(record.referenced_by),
            "status": "current" if prev is not None else "new",
            "metadata": {
                "created_at": created_at,
                "last_seen_run_id": run_id,
            },
        }

        merged.append(entry)

    # Next, handle variables that used to exist but are now absent.
    for name, prev in previous_by_name.items():
        if name in current_map:
            continue

        if not isinstance(prev, Mapping):
            continue

        prev_metadata = prev.get("metadata") or {}
        if not isinstance(prev_metadata, Mapping):
            prev_metadata = {}

        created_at = str(prev_metadata.get("created_at") or "").strip()
        if not created_at:
            created_at = run_id

        type_name = str(prev.get("type") or "Unknown")
        defined_by_prev = prev.get("defined_by") or []
        referenced_by_prev = prev.get("referenced_by") or []

        if not isinstance(defined_by_prev, list):
            defined_by_prev = []
        if not isinstance(referenced_by_prev, list):
            referenced_by_prev = []

        entry = {
            "name": name,
            "type": type_name,
            "defined_by": sorted(str(x) for x in defined_by_prev),
            "referenced_by": sorted(str(x) for x in referenced_by_prev),
            "status": "deprecated",
            "metadata": {
                "created_at": created_at,
                # We treat last_seen_run_id from previous runs as authoritative for
                # deprecated variables, so we do not overwrite it here.
                "last_seen_run_id": prev_metadata.get("last_seen_run_id", run_id),
            },
        }

        merged.append(entry)

    # Ensure deterministic ordering for easier diffing.
    merged.sort(key=lambda item: item.get("name", ""))
    return merged


def write_variables_json(
    flow_root: Path,
    run_id: str,
    variables_list: List[Dict[str, Any]],
) -> Path:
    """
    Write the final variables.json artifact for Stage 1.

    Args:
        flow_root: Output directory for the current flow (same as out_dir).
        run_id: Run identifier used in merge_variable_status().
        variables_list: List of JSON-ready variable dicts, as returned by
            merge_variable_status().

    Returns:
        The full Path to the written variables.json file.
    """

    path = flow_root / "variables.json"
    root: Dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "variables": variables_list,
    }

    with path.open("w", encoding="utf-8") as handle:
        json.dump(root, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return path


# ------------------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------------------


def _get_step_raw(step: Any) -> MutableMapping[str, Any] | Mapping[str, Any] | Any:
    """
    Extract the raw step payload from a StepInfo-like object.
    """

    if hasattr(step, "raw"):
        return getattr(step, "raw")
    return step


def _get_step_type(step: Any, raw: Mapping[str, Any]) -> str:
    """
    Determine the Logic Apps step type from either the StepInfo or raw payload.
    """

    if hasattr(step, "atype"):
        atype = getattr(step, "atype", None)
        if isinstance(atype, str) and atype:
            return atype

    step_type = raw.get("type")
    if isinstance(step_type, str) and step_type:
        return step_type

    return ""


def _safe_get_mapping(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """
    Safely return a nested mapping value, or an empty dict if not present.
    """

    value = obj.get(key, {})
    if not isinstance(value, Mapping):
        return {}
    return value


_VARIABLES_PATTERN = re.compile(r"variables\('([^']+)'\)")


def _scan_for_variable_references(node: Any) -> Set[str]:
    """
    Recursively scan a JSON-like object for occurrences of variables('name').

    Args:
        node: Any JSON-serialisable structure (dict, list, str, etc.).

    Returns:
        A set of variable names referenced via variables('name').
    """

    found: Set[str] = set()

    if isinstance(node, str):
        for match in _VARIABLES_PATTERN.finditer(node):
            name = match.group(1).strip()
            if name:
                found.add(name)
        return found

    if isinstance(node, Mapping):
        for value in node.values():
            found.update(_scan_for_variable_references(value))
        return found

    if isinstance(node, (list, tuple)):
        for item in node:
            found.update(_scan_for_variable_references(item))
        return found

    # Other scalar types cannot contain variable references.
    return found


def _normalise_type(raw_type: Optional[Any]) -> str:
    """
    Map Logic Apps variable types into user-facing type names.

    Args:
        raw_type: The raw type value from the Logic App definition.

    Returns:
        A normalised type string such as "String", "Int", "Bool", "Array",
        "Object", "Float", or "Unknown".
    """

    if raw_type is None:
        return "Unknown"

    text = str(raw_type).strip().lower()
    if not text:
        return "Unknown"

    if text in {"string"}:
        return "String"
    if text in {"boolean", "bool"}:
        return "Bool"
    if text in {"integer", "int"}:
        return "Int"
    if text in {"float", "double"}:
        return "Float"
    if text in {"array", "list"}:
        return "Array"
    if text in {"object", "map", "dictionary", "dict"}:
        return "Object"

    return "Unknown"
