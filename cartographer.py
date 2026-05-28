#!/usr/bin/env python3
# filename: cartographer.py
"""
Cartographer — Lightweight reference data generator for Logic Apps.

Reads a Logic App JSON export and produces structural reference data
for downstream tools (Liturgist, Reference Skill, Dev Skill) without
the full analyzer rendering overhead.

Performs the SAME depth of analysis as 1_analyzer.py:
  - Registry extraction (all steps, containers, parent/child)
  - Catalog resolution (hints, subtypes, pretty categories)
  - Variable collection (definitions + references)
  - Execution ordering (Phase 1-3 designer-faithful)
  - Branch membership computation
  - Effective runAfter inference
  - Flow model construction (nodes, predecessors, successors)
  - Pathways snapshot (success/fail/alt lane assignments)
  - Step validation (runAfter coherence)

Produces reference outputs:
  - flow_model.json     (nodes + execution order + pathways)
  - variables.json      (variable names, types, defined_by, referenced_by)
  - pathways.json       (success/fail/alt lane assignments)
  - vitals.json         (step counts, types, hierarchy tree, containers)
  - app_structure.yaml  (clean YAML: every step with metadata)
  - app_structure.md    (human-readable summary with TOC)
  - fingerprints.json   (--detailed only: per-step/per-field SHA-256 checksums with json_paths)

Exit codes:
  0 = clean map, all outputs nominal
  1 = damaged map, DAMAGE_REPORT.md explains what broke

Error codes (stdout):
  CARTO_E001  Input file not found or not readable
  CARTO_E002  Input is not valid JSON
  CARTO_E003  No Logic App definition found in input
  CARTO_E004  Registry extraction failed
  CARTO_E005  Catalog load failed
  CARTO_E006  Variable collection failed
  CARTO_E007  Ordering failed (both Phase 3 and legacy fallback)
  CARTO_E008  Flow model construction failed
  CARTO_E009  Vitals tree construction failed
  CARTO_E010  Pathways generation failed
  CARTO_E011  YAML structure write failed
  CARTO_E012  Markdown structure write failed
  CARTO_W001  Catalog miss (unknown connector type)
  CARTO_W002  Step has no effective runAfter after inference
  CARTO_W003  Phase 3 ordering failed, fell back to legacy
  CARTO_W004  Variable referenced but never initialized
  CARTO_W005  Variable initialized but never referenced
  CARTO_E013  Duplicate action names detected across scopes
  CARTO_E014  Circular runAfter dependency detected
  CARTO_E015  Fingerprints generation failed

Usage:
  python3 cartographer.py --input app.json --output /tmp/map
  python3 cartographer.py --input app.json --output /tmp/map --catalog plugin_catalog.json --verbose
  python3 cartographer.py --input app.json --output /tmp/map --detailed
  python3 cartographer.py --input app.json --output /tmp/map --detailed --chop-checksum 16
"""

CARTOGRAPHER_VERSION = "1.5.1"

import argparse
import datetime
import hashlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml


# ─────────────────────────────────────────────────────────────────────────────
# Helper imports (shared with 1_analyzer.py)
# ─────────────────────────────────────────────────────────────────────────────

from helpers.definition_model import (
    StepInfo,
    extract_core,
    collect_registry,
)
from helpers.catalog_tools import (
    load_catalog,
    _catalog_lookup_labels,
    _catalog_resolve_type,
    _attach_pretty_category,
    UnknownsGuard,
)
from helpers.hints import apply_hints_advanced
from helpers.analyzer_unknowns import AnalyzerUnknowns
from helpers.variables_model import (
    collect_variables,
    load_previous_variables,
    merge_variable_status,
    write_variables_json,
)
from helpers.creation_order import (
    compute_master_run_order,
    children_of,
    container_creation_markers,
)
from helpers.flow_model import (
    FlowInfo,
    build_flow_info_from_definition,
)
from helpers.classification import (
    load_global_rules,
    load_override_rules,
    build_vitals_tree,
)
from helpers.pathways_model import build_and_write_pathways_snapshot
from helpers.filenames_config import (
    get_vitals_path,
    get_pathways_json_path,
)
from helpers.jsonc_config import (
    deep_merge_dicts,
    load_jsonc_config,
)
from helpers.leaf_path import build_fingerprints_for_app
from helpers.order_model import build_model
from helpers.order_exits import compute_scopes
from helpers.order_walk import compute_scope_orders


# ─────────────────────────────────────────────────────────────────────────────
# Damage tracker
# ─────────────────────────────────────────────────────────────────────────────

class DamageTracker:
    """
    Collects errors and warnings during a Cartographer run.

    Errors (E codes) cause rc=1 and DAMAGE_REPORT.md.
    Warnings (W codes) are informational only (stderr).

    Inputs:
      None (constructed empty).

    Outputs:
      errors: list of (code, message) tuples.
      warnings: list of (code, message) tuples.
      clean_files: list of filenames written successfully.
      damaged_files: list of filenames written as .damaged.
    """

    def __init__(self) -> None:
        self.errors: List[Tuple[str, str]] = []
        self.warnings: List[Tuple[str, str]] = []
        self.clean_files: List[str] = []
        self.damaged_files: List[str] = []
        self._damaged_stage: Optional[str] = None

    def error(self, code: str, message: str) -> None:
        """Record an error. Marks all subsequent outputs as potentially damaged."""
        full = f"{code}: {message}"
        print(full, file=sys.stderr)
        self.errors.append((code, message))
        if self._damaged_stage is None:
            self._damaged_stage = code

    def warn(self, code: str, message: str) -> None:
        """Record a warning. Does not affect rc."""
        full = f"{code}: {message}"
        print(full, file=sys.stderr)
        self.warnings.append((code, message))

    def is_damaged(self) -> bool:
        """Return True if any errors have been recorded."""
        return len(self.errors) > 0

    def record_clean(self, filename: str) -> None:
        """Record a file that was written successfully."""
        self.clean_files.append(filename)

    def record_damaged(self, filename: str) -> None:
        """Record a file that was written as .damaged."""
        self.damaged_files.append(filename)

    def output_path(self, out_dir: Path, stem: str, ext: str, depends_on_ok: bool = True) -> Path:
        """
        Return the correct output path, adding .damaged if needed.

        Inputs:
          out_dir: Output directory.
          stem: Filename stem (e.g., "app_structure").
          ext: Extension with dot (e.g., ".yaml").
          depends_on_ok: If True and tracker has errors, use .damaged extension.

        Outputs:
          Path with or without .damaged inserted before extension.
        """
        if depends_on_ok and self.is_damaged():
            filename = f"{stem}.damaged{ext}"
            return out_dir / filename
        return out_dir / f"{stem}{ext}"

    def write_damage_report(self, out_dir: Path, input_path: str, run_id: str) -> None:
        """
        Write DAMAGE_REPORT.md to the output directory.

        Only called when is_damaged() is True.

        Inputs:
          out_dir: Output directory.
          input_path: Path to the input file (for report context).
          run_id: Run identifier.
        """
        lines: List[str] = []
        lines.append("# DAMAGE REPORT")
        lines.append("")
        lines.append(f"**Input:** `{input_path}`")
        lines.append(f"**Run:** `{run_id}`")
        lines.append(f"**Errors:** {len(self.errors)}")
        lines.append(f"**Warnings:** {len(self.warnings)}")
        lines.append("")

        lines.append("## Errors")
        lines.append("")
        if self.errors:
            for code, msg in self.errors:
                lines.append(f"- **{code}**: {msg}")
        else:
            lines.append("None.")
        lines.append("")

        lines.append("## Warnings")
        lines.append("")
        if self.warnings:
            for code, msg in self.warnings:
                lines.append(f"- **{code}**: {msg}")
        else:
            lines.append("None.")
        lines.append("")

        lines.append("## Files")
        lines.append("")
        if self.clean_files:
            lines.append("**Clean (usable):**")
            for f in self.clean_files:
                lines.append(f"- `{f}`")
            lines.append("")

        if self.damaged_files:
            lines.append("**Damaged (partial, for diagnostics only):**")
            for f in self.damaged_files:
                lines.append(f"- `{f}`")
            lines.append("")

        lines.append("## Error Code Reference")
        lines.append("")
        lines.append("| Code | Description |")
        lines.append("|------|-------------|")
        lines.append("| CARTO_E001 | Input file not found or not readable |")
        lines.append("| CARTO_E002 | Input is not valid JSON |")
        lines.append("| CARTO_E003 | No Logic App definition found in input |")
        lines.append("| CARTO_E004 | Registry extraction failed |")
        lines.append("| CARTO_E005 | Catalog load failed |")
        lines.append("| CARTO_E006 | Variable collection failed |")
        lines.append("| CARTO_E007 | Ordering failed (both Phase 3 and legacy) |")
        lines.append("| CARTO_E008 | Flow model construction failed |")
        lines.append("| CARTO_E009 | Vitals tree construction failed |")
        lines.append("| CARTO_E010 | Pathways generation failed |")
        lines.append("| CARTO_E011 | YAML structure write failed |")
        lines.append("| CARTO_E012 | Markdown structure write failed |")
        lines.append("| CARTO_W001 | Catalog miss (unknown connector type) |")
        lines.append("| CARTO_W002 | Step has no effective runAfter after inference |")
        lines.append("| CARTO_W003 | Phase 3 ordering failed, fell back to legacy |")
        lines.append("| CARTO_W004 | Variable referenced but never initialized |")
        lines.append("| CARTO_W005 | Variable initialized but never referenced |")
        lines.append("| CARTO_E013 | Duplicate action names across scopes |")
        lines.append("| CARTO_E014 | Circular runAfter dependency detected |")
        lines.append("| CARTO_E015 | Fingerprints generation failed |")

        report_path = out_dir / "DAMAGE_REPORT.md"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (ported from 1_analyzer.py analysis functions)
# ─────────────────────────────────────────────────────────────────────────────

def _read_json(p: str | Path) -> Dict[str, Any]:
    """
    Read a JSON file from disk with user-friendly errors.

    Inputs:
      p: Path to a JSON file.

    Outputs:
      Parsed JSON as a dict.

    Raises:
      SystemExit on file-not-found or parse error.
    """
    import os

    raw = str(p)
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)

    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _compute_file_md5(p: Path, chunk_size: int = 65536) -> str:
    """
    Return the hex MD5 digest of the given file.

    Inputs:
      p: Path to the file.

    Outputs:
      Hex MD5 string, or empty string on read failure.
    """
    h = hashlib.md5()
    try:
        with p.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                if not chunk:
                    break
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def _get_actions_root(defn: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the dict containing the top-level 'actions' map.

    Inputs:
      defn: Logic App definition object.

    Outputs:
      The dict containing 'actions' (may be defn itself or defn['definition']).
    """
    if isinstance(defn, dict):
        inner = defn.get("definition")
        if isinstance(inner, dict):
            return inner
    return defn


def _compute_branch_membership(defn: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """
    Compute branch/case membership for steps.

    Inputs:
      defn: Logic App definition.

    Outputs:
      Mapping of step_name -> {"branch_id": str, "branch_label": str}.
    """
    actions_root = _get_actions_root(defn)
    top_actions = actions_root.get("actions") if isinstance(actions_root, dict) else None
    if not isinstance(top_actions, dict):
        return {}

    membership: Dict[str, Dict[str, str]] = {}

    def _walk_actions(actions: Dict[str, Any], branch_stack: List[str]) -> None:
        if not isinstance(actions, dict):
            return

        for action_name, action_obj in actions.items():
            if not isinstance(action_obj, dict):
                continue

            if branch_stack:
                membership[action_name] = {
                    "branch_id": "|".join(branch_stack),
                    "branch_label": branch_stack[-1],
                }

            atype = str(action_obj.get("type") or "")

            if atype.lower() == "switch":
                cases = action_obj.get("cases")
                if isinstance(cases, dict):
                    for case_name, case_obj in cases.items():
                        if not isinstance(case_obj, dict):
                            continue
                        case_actions = case_obj.get("actions")
                        if isinstance(case_actions, dict):
                            case_label = str(case_name)
                            case_id = f"Switch:{action_name}:Case:{case_name}"
                            _walk_actions(case_actions, branch_stack + [case_id, case_label])

                default_obj = action_obj.get("default")
                if isinstance(default_obj, dict):
                    default_actions = default_obj.get("actions")
                    if isinstance(default_actions, dict):
                        default_id = f"Switch:{action_name}:Default"
                        _walk_actions(default_actions, branch_stack + [default_id, "Default"])

            if atype.lower() in ("if", "condition"):
                true_actions = action_obj.get("actions")
                if isinstance(true_actions, dict):
                    t_id = f"If:{action_name}:True"
                    _walk_actions(true_actions, branch_stack + [t_id, f"{action_name} → If true"])

                else_obj = action_obj.get("else")
                if isinstance(else_obj, dict):
                    else_actions = else_obj.get("actions")
                    if isinstance(else_actions, dict):
                        f_id = f"If:{action_name}:False"
                        _walk_actions(else_actions, branch_stack + [f_id, f"{action_name} → If false"])

            nested_actions = action_obj.get("actions")
            if isinstance(nested_actions, dict):
                _walk_actions(nested_actions, branch_stack)

    _walk_actions(top_actions, [])
    return membership


def _compute_first_in_branch(
    branch_membership: Dict[str, Dict[str, str]],
    order_index: Dict[str, int],
) -> Dict[str, str]:
    """
    Return branch_id -> step_name for the earliest step in each branch.

    Inputs:
      branch_membership: Mapping from _compute_branch_membership.
      order_index: Step name -> position in creation order.

    Outputs:
      Mapping of branch_id -> earliest step_name.
    """
    first_of: Dict[str, str] = {}
    best_idx: Dict[str, int] = {}

    for step_name, info in branch_membership.items():
        bid = str(info.get("branch_id") or "").strip()
        if not bid:
            continue
        idx = int(order_index.get(step_name, 10**9))
        if bid not in best_idx or idx < best_idx[bid]:
            best_idx[bid] = idx
            first_of[bid] = step_name

    return first_of


def _worst_run_after_priority(run_after: Dict[str, Any] | None) -> int:
    """
    Return a priority bucket for a step based on its runAfter states.

    Inputs:
      run_after: The step's runAfter dict.

    Outputs:
      0 if any dependency includes Failed/TimedOut/Skipped, else 1.
    """
    if not isinstance(run_after, dict) or not run_after:
        return 1

    negative = {"failed", "timedout", "skipped"}
    for _, states in run_after.items():
        if isinstance(states, list):
            st = {str(s).strip().lower() for s in states}
        elif states is None:
            st = set()
        else:
            st = {str(states).strip().lower()}
        if st & negative:
            return 0

    return 1


def _branch_priority_for_child(
    *,
    parent_name: str,
    parent_type: str,
    child_name: str,
    branch_membership: Dict[str, Dict[str, str]],
) -> int:
    """
    Return a per-child branch priority within a parent container.

    Inputs:
      parent_name: Name of the parent container step.
      parent_type: Raw type of the parent (e.g. "If", "Switch").
      child_name: Name of the child step.
      branch_membership: Branch membership mapping.

    Outputs:
      Priority int (lower = earlier). 0 for false/default, 1 for cases/true, 2 otherwise.
    """
    parent_type_l = (parent_type or "").strip().lower()
    info = branch_membership.get(child_name) or {}
    bid = str(info.get("branch_id") or "")

    if parent_type_l in ("if", "condition"):
        if f"If:{parent_name}:False" in bid:
            return 0
        if f"If:{parent_name}:True" in bid:
            return 1
        return 2

    if parent_type_l == "switch":
        if f"Switch:{parent_name}:Default" in bid:
            return 0
        if f"Switch:{parent_name}:Case:" in bid:
            return 1
        return 2

    return 1


def _stable_topo_sort_siblings(
    *,
    siblings: List[str],
    reg: Dict[str, StepInfo],
    ui_index: Dict[str, int],
    branch_membership: Dict[str, Dict[str, str]],
    parent_name: str,
) -> List[str]:
    """
    Topo-sort direct siblings by runAfter with stable designer-ish tie-breakers.

    Inputs:
      siblings: List of step names that share the same parent.
      reg: Step registry.
      ui_index: Step name -> position in registry insertion order.
      branch_membership: Branch membership mapping.
      parent_name: Name of the parent container (or "" for root).

    Outputs:
      Sorted list of step names.
    """
    sib_set = set(siblings)
    indeg: Dict[str, int] = {n: 0 for n in siblings}
    outs: Dict[str, List[str]] = {n: [] for n in siblings}

    for n in siblings:
        s = reg.get(n)
        if not s:
            continue
        ra = s.run_after or {}
        if not isinstance(ra, dict):
            continue
        for dep in ra.keys():
            if dep in sib_set:
                outs[dep].append(n)
                indeg[n] += 1

    ptype = ""
    if parent_name and parent_name in reg:
        ptype = str(getattr(reg[parent_name], "atype", "") or "")

    def _ready_sort_key(n: str) -> Tuple[int, int, int, str]:
        s = reg.get(n)
        ra_pri = _worst_run_after_priority(getattr(s, "run_after", None) if s else None)
        br_pri = _branch_priority_for_child(
            parent_name=parent_name,
            parent_type=ptype,
            child_name=n,
            branch_membership=branch_membership,
        )
        ui = int(ui_index.get(n, 10**9))
        return (ra_pri, br_pri, ui, n.lower())

    ready: List[str] = sorted(
        [n for n in siblings if indeg.get(n, 0) == 0],
        key=_ready_sort_key,
    )
    out: List[str] = []

    while ready:
        n = ready.pop(0)
        out.append(n)
        for m in outs.get(n, []):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort(key=_ready_sort_key)

    if len(out) != len(siblings):
        remaining = [n for n in siblings if n not in set(out)]
        remaining.sort(key=lambda x: (int(ui_index.get(x, 10**9)), x.lower()))
        out.extend(remaining)

    return out


def _designer_faithful_creation_order(
    defn: Dict[str, Any],
    reg: Dict[str, StepInfo],
    *,
    verbose: bool = False,
) -> Tuple[List[str], List[str]]:
    """
    Compute a flattened creation order that stays container-contiguous.

    Inputs:
      defn: Logic App definition.
      reg: Step registry.
      verbose: Emit diagnostic notes.

    Outputs:
      (order, notes) — flat step list and diagnostic messages.
    """
    notes: List[str] = []
    ui_order = list(reg.keys())
    ui_index = {n: i for i, n in enumerate(ui_order)}
    branch_membership = _compute_branch_membership(defn)

    def _emit_subtree(parent_name: Optional[str]) -> List[str]:
        if parent_name:
            siblings = children_of(parent_name, reg)
        else:
            siblings = [n for n, s in reg.items() if not getattr(s, "parent", None)]

        if not siblings:
            return []

        sorted_sibs = _stable_topo_sort_siblings(
            siblings=siblings,
            reg=reg,
            ui_index=ui_index,
            branch_membership=branch_membership,
            parent_name=parent_name or "",
        )

        emitted: List[str] = []
        for name in sorted_sibs:
            emitted.append(name)
            s = reg.get(name)
            if s and getattr(s, "is_container", False):
                emitted.extend(_emit_subtree(name))

        return emitted

    order = _emit_subtree(None)

    if verbose:
        notes.append(
            "Designer-faithful order: container-contiguous + per-scope topo sort."
        )

    return order, notes


# ─────────────────────────────────────────────────────────────────────────────
# YAML + Markdown structure writers
# ─────────────────────────────────────────────────────────────────────────────

def _write_app_structure_yaml(
    path: Path,
    title: str,
    reg: Dict[str, StepInfo],
    order: List[str],
    current_variables: Dict[str, Any],
    damage: DamageTracker,
) -> None:
    """
    Write app_structure.yaml — clean structural reference for every step.

    Inputs:
      path: Full output path (may include .damaged).
      title: Flow name.
      reg: Step registry.
      order: Flattened creation order.
      current_variables: Variable records from collect_variables.
      damage: Damage tracker for recording issues.
    """
    steps_list = []
    for idx, name in enumerate(order):
        s = reg.get(name)
        if not s:
            continue

        ra = s.run_after or {}
        run_after_list = []
        if isinstance(ra, dict):
            for dep, states in ra.items():
                if isinstance(states, list):
                    run_after_list.append({"step": dep, "states": states})
                else:
                    run_after_list.append({"step": dep, "states": [str(states)] if states else ["Succeeded"]})

        step_entry = {
            "number": idx + 1,
            "code_name": name,
            "type": s.atype,
            "matched_type": getattr(s, "atype_display", s.atype) or s.atype,
            "pretty_category": getattr(s, "pretty_category", "") or "",
            "parent": s.parent,
            "is_container": bool(getattr(s, "is_container", False)),
            "run_after": run_after_list if run_after_list else None,
        }

        if getattr(s, "is_container", False):
            step_entry["children_direct"] = getattr(s, "children_direct", [])

        steps_list.append(step_entry)

    variables_list = []
    for name, rec in sorted(current_variables.items()):
        variables_list.append({
            "name": rec.name,
            "type": rec.type_name,
            "defined_by": sorted(rec.defined_by),
            "referenced_by": sorted(rec.referenced_by),
        })

    structure = {
        "app_name": title,
        "total_steps": len(reg),
        "total_variables": len(current_variables),
        "steps": steps_list,
        "variables": variables_list,
    }

    if damage.is_damaged():
        structure["_damage"] = [f"{c}: {m}" for c, m in damage.errors]

    path.write_text(
        yaml.dump(structure, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_app_structure_md(
    path: Path,
    title: str,
    reg: Dict[str, StepInfo],
    order: List[str],
    current_variables: Dict[str, Any],
    types_used: Set[str],
    damage: DamageTracker,
) -> None:
    """
    Write app_structure.md — human-readable summary with TOC.

    Inputs:
      path: Full output path (may include .damaged).
      title: Flow name.
      reg: Step registry.
      order: Flattened creation order.
      current_variables: Variable records.
      types_used: Set of matched types seen.
      damage: Damage tracker.
    """
    lines: List[str] = []

    if damage.is_damaged():
        lines.append("> **WARNING: This map was generated from a damaged analysis.**")
        lines.append("> **See DAMAGE_REPORT.md for details.**")
        lines.append("")

    lines.append(f"# {title} — Structure Reference")
    lines.append("")
    lines.append(f"**Total steps:** {len(reg)}")
    lines.append(f"**Total variables:** {len(current_variables)}")
    lines.append(f"**Types used:** {', '.join(sorted(types_used))}")
    lines.append("")

    containers = {n: s for n, s in reg.items() if getattr(s, "is_container", False)}
    lines.append(f"**Containers:** {len(containers)}")
    for name, s in containers.items():
        kids = children_of(name, reg)
        lines.append(f"  - {name} ({s.atype}) — {len(kids)} children")
    lines.append("")

    lines.append("## Steps (Creation Order)")
    lines.append("")

    depth_map: Dict[str, int] = {}
    for name in order:
        s = reg.get(name)
        if not s:
            continue
        parent = s.parent
        depth = 0
        while parent:
            depth += 1
            ps = reg.get(parent)
            parent = ps.parent if ps else None
        depth_map[name] = depth

    for idx, name in enumerate(order):
        s = reg.get(name)
        if not s:
            continue
        depth = depth_map.get(name, 0)
        indent = "  " * depth
        matched = getattr(s, "atype_display", s.atype) or s.atype
        cat = getattr(s, "pretty_category", "") or ""
        container_flag = " 📦" if getattr(s, "is_container", False) else ""
        lines.append(f"{indent}{idx+1}. **{name}** [{matched}] {cat}{container_flag}")

    lines.append("")
    lines.append("## Variables")
    lines.append("")
    for name, rec in sorted(current_variables.items()):
        lines.append(f"- **{rec.name}** ({rec.type_name}) — defined by: {', '.join(sorted(rec.defined_by))}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# Scope-aware duplicate detection + circular dependency detection
# ─────────────────────────────────────────────────────────────────────────────

def _build_scope_aware_actions(
    defn: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Walk the raw definition and map every action name to its full scope path(s).

    Inputs:
      defn: Logic App definition (the object containing 'actions').

    Outputs:
      List of dicts, one per unique action name:
        {
          "name": "action_name",
          "occurrences": 2,
          "paths": ["root.actions.Scope_A.actions", "root.actions"],
          "scopes": ["Scope_A", "root"],
          "is_duplicate": true,
          "severity": "ERROR"  (ERROR if duplicate, OK if not)
        }
    """
    from collections import defaultdict

    names_at_paths: Dict[str, List[Dict[str, str]]] = defaultdict(list)

    def _walk(obj: Any, path: str, parent_scope: str) -> None:
        if not isinstance(obj, dict):
            return

        actions = obj.get("actions")
        if isinstance(actions, dict):
            for action_name, action_obj in actions.items():
                action_path = f"{path}.actions"
                names_at_paths[action_name].append({
                    "path": action_path,
                    "scope": parent_scope,
                })
                _walk(action_obj, f"{path}.actions.{action_name}", action_name)

        # If/Condition else branches.
        else_obj = obj.get("else")
        if isinstance(else_obj, dict):
            _walk(else_obj, f"{path}.else", parent_scope)

        # Switch cases.
        cases = obj.get("cases")
        if isinstance(cases, dict):
            for case_name, case_obj in cases.items():
                if isinstance(case_obj, dict):
                    _walk(case_obj, f"{path}.cases.{case_name}", parent_scope)

        # Switch default.
        default = obj.get("default")
        if isinstance(default, dict):
            _walk(default, f"{path}.default", parent_scope)

    _walk(defn, "root", "root")

    report: List[Dict[str, Any]] = []
    for name in sorted(names_at_paths.keys()):
        occurrences = names_at_paths[name]
        is_dupe = len(occurrences) > 1
        report.append({
            "name": name,
            "occurrences": len(occurrences),
            "paths": [o["path"] for o in occurrences],
            "scopes": [o["scope"] for o in occurrences],
            "is_duplicate": is_dupe,
            "severity": "ERROR" if is_dupe else "OK",
        })

    return report


def _detect_circular_dependencies(
    defn: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Walk the raw definition and detect runAfter circular dependencies.

    Checks within each scope (actions dict) independently, since
    runAfter references are scope-local in Logic Apps.

    Inputs:
      defn: Logic App definition.

    Outputs:
      List of cycle dicts:
        {
          "scope": "Handle_Devices_From_Search",
          "scope_path": "root.actions.Core.actions.Handle_Devices_From_Search",
          "cycle": ["Step_A", "Step_B", "Step_C", "Step_A"],
          "length": 3
        }
      Empty list if no cycles found.
    """
    from collections import defaultdict

    all_cycles: List[Dict[str, Any]] = []

    def _check_scope(actions: Dict[str, Any], scope_name: str, scope_path: str) -> None:
        """Check a single actions dict for internal runAfter cycles."""
        if not isinstance(actions, dict) or not actions:
            return

        # Build adjacency: step -> set of deps (within this scope only).
        edges: Dict[str, set] = defaultdict(set)
        action_names = set(actions.keys())

        for name, action_obj in actions.items():
            if not isinstance(action_obj, dict):
                continue
            ra = action_obj.get("runAfter", {})
            if isinstance(ra, dict):
                for dep in ra.keys():
                    if dep in action_names:
                        edges[name].add(dep)

        # DFS cycle detection.
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in action_names}

        def _dfs(node: str, path: List[str]) -> None:
            color[node] = GRAY
            for dep in edges.get(node, set()):
                if color.get(dep) == GRAY:
                    cycle_start = path.index(dep) if dep in path else -1
                    if cycle_start >= 0:
                        cycle = path[cycle_start:] + [dep]
                    else:
                        cycle = [node, dep, node]
                    all_cycles.append({
                        "scope": scope_name,
                        "scope_path": scope_path,
                        "cycle": cycle,
                        "length": len(cycle) - 1,
                    })
                elif color.get(dep, WHITE) == WHITE:
                    _dfs(dep, path + [dep])
            color[node] = BLACK

        for node in list(action_names):
            if color.get(node) == WHITE:
                _dfs(node, [node])

    def _walk(obj: Any, path: str, scope_name: str) -> None:
        if not isinstance(obj, dict):
            return
        actions = obj.get("actions")
        if isinstance(actions, dict):
            _check_scope(actions, scope_name, path)
            for action_name, action_obj in actions.items():
                _walk(action_obj, f"{path}.actions.{action_name}", action_name)
        else_obj = obj.get("else")
        if isinstance(else_obj, dict):
            _walk(else_obj, f"{path}.else", scope_name)
        cases = obj.get("cases")
        if isinstance(cases, dict):
            for case_name, case_obj in cases.items():
                if isinstance(case_obj, dict):
                    _walk(case_obj, f"{path}.cases.{case_name}", scope_name)
        default = obj.get("default")
        if isinstance(default, dict):
            _walk(default, f"{path}.default", scope_name)

    _walk(defn, "root", "root")
    return all_cycles


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Cartographer: generate lightweight reference data from a Logic App JSON export. "
            "Same analysis depth as 1_analyzer.py, different outputs."
        )
    )
    ap.add_argument("--version", action="version", version=f"Cartographer v{CARTOGRAPHER_VERSION}")
    ap.add_argument("--input", required=True, help="Path to the Logic Apps workflow JSON export.")
    ap.add_argument("--output", required=True, help="Output directory for reference data.")
    ap.add_argument("--catalog", required=False, default=None, help="Path to plugin_catalog.json.")
    ap.add_argument("--config", required=False, default=None, help="Path to analyzer config (JSON or JSONC).")
    ap.add_argument("--verbose", action="store_true", help="Emit diagnostic messages to stderr.")
    ap.add_argument(
        "--conservative-pathing",
        action="store_true",
        help="Disable pathway inference heuristics (success vs alt_success).",
    )
    ap.add_argument(
        "--detailed",
        action="store_true",
        help="Generate fingerprints.json with per-step/per-field SHA-256 checksums.",
    )
    ap.add_argument(
        "--chop-checksum",
        type=int,
        default=12,
        help="Hex character length for SHA-256 fingerprint truncation (default: 12).",
    )
    args = ap.parse_args()

    damage = DamageTracker()

    # ── Setup ────────────────────────────────────────────────────────────────

    _script_dir = Path(__file__).resolve().parent

    if not args.catalog:
        default_catalog = _script_dir / "plugin_catalog.json"
        if default_catalog.exists():
            args.catalog = str(default_catalog)
    if not args.catalog:
        damage.error("CARTO_E005", "No plugin_catalog.json found (--catalog not provided, none next to script)")

    input_path = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    run_id = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    source_md5 = _compute_file_md5(input_path)

    issues = AnalyzerUnknowns()
    catalog: Dict[str, Any] = {}

    if args.verbose:
        print(f"Cartographer: input={input_path}", file=sys.stderr)
        print(f"Cartographer: output={out_dir}", file=sys.stderr)
        print(f"Cartographer: run_id={run_id}", file=sys.stderr)

    # ── Load catalog ─────────────────────────────────────────────────────────

    if args.catalog:
        catalog_path = Path(args.catalog)
        if not catalog_path.exists():
            damage.error("CARTO_E005", f"Catalog not found: {catalog_path}")
        else:
            try:
                catalog = load_catalog(catalog_path)
            except Exception as exc:
                damage.error("CARTO_E005", f"Catalog load failed: {exc}")

    # Config: optional.
    default_config: Dict[str, Any] = {"redaction": {"enabled": False}}
    if args.config:
        config = load_jsonc_config(Path(args.config), default_config)
    else:
        config_candidate = _script_dir / "analyzer_config.jsonc"
        if config_candidate.is_file():
            config = load_jsonc_config(config_candidate, default_config)
        else:
            config = default_config

    # ── Stage 1: Extract + Analyze ───────────────────────────────────────────

    doc: Dict[str, Any] = {}
    defn: Dict[str, Any] = {}
    reg: Dict[str, StepInfo] = {}
    current_variables: Dict[str, Any] = {}

    # Load input.
    try:
        doc = _read_json(args.input)
    except FileNotFoundError:
        damage.error("CARTO_E001", f"Input file not found: {args.input}")
    except json.JSONDecodeError as exc:
        damage.error("CARTO_E002", f"Input is not valid JSON: {exc}")
    except Exception as exc:
        damage.error("CARTO_E001", f"Input file not readable: {exc}")

    # Extract definition.
    if doc:
        try:
            defn, _, _ = extract_core(doc)
            if not defn or not isinstance(defn.get("actions", None), dict):
                damage.error("CARTO_E003", "No Logic App definition found (missing 'actions' key)")
                defn = {}
        except Exception as exc:
            damage.error("CARTO_E003", f"extract_core failed: {exc}")

    # Build registry.
    if defn:
        try:
            reg = collect_registry(defn)
            if args.verbose:
                print(f"Cartographer: {len(reg)} steps in registry", file=sys.stderr)
        except Exception as exc:
            damage.error("CARTO_E004", f"Registry extraction failed: {exc}")

    # Collect variables.
    if reg:
        try:
            current_variables = collect_variables(defn, reg)
        except Exception as exc:
            damage.error("CARTO_E006", f"Variable collection failed: {exc}")

        # Check for variable anomalies.
        for var_name, rec in current_variables.items():
            if not rec.defined_by and rec.referenced_by:
                damage.warn("CARTO_W004", f"Variable '{var_name}' referenced but never initialized")
            if rec.defined_by and not rec.referenced_by:
                damage.warn("CARTO_W005", f"Variable '{var_name}' initialized but never referenced")

    # Apply hints + catalog resolution.
    if reg and catalog:
        hints_section = catalog.get("hints", [])
        apply_hints_advanced(reg, hints_section, verbose=args.verbose)

        templates_dir = _script_dir / "templates"
        try:
            _attach_pretty_category(reg, catalog, templates_dir, issues, verbose=args.verbose)
        except Exception as exc:
            damage.error("CARTO_E005", f"Catalog resolution failed: {exc}")

        for miss in issues.catalog_misses:
            damage.warn("CARTO_W001", f"Catalog miss — '{miss['pretty_name']}' resolved as {miss['resolved_key']}")

    # ── Stage 2: Ordering ────────────────────────────────────────────────────

    notes: List[str] = []
    order: List[str] = []
    ordering_result_obj = None

    if reg:
        try:
            order_model_obj = build_model(defn)
            scopes_obj = compute_scopes(order_model_obj)
            ordering_result_obj = compute_scope_orders(
                model=order_model_obj,
                scopes=scopes_obj,
            )
            if ordering_result_obj.execution_steps:
                order = list(ordering_result_obj.execution_steps)
            else:
                order = list(ordering_result_obj.flattened_order)

            if args.verbose:
                print(f"Cartographer: Phase 3 ordering succeeded ({len(order)} steps)", file=sys.stderr)

        except Exception as exc:
            damage.warn("CARTO_W003", f"Phase 3 ordering failed, falling back to legacy: {exc}")

            try:
                order, legacy_notes = _designer_faithful_creation_order(defn, reg, verbose=args.verbose)
                notes.extend(legacy_notes or [])
            except Exception as exc2:
                damage.error("CARTO_E007", f"Both Phase 3 and legacy ordering failed: {exc2}")
                order = list(reg.keys())

    sequence = container_creation_markers(order, reg) if order else []
    order_index = {n: i for i, n in enumerate(order)}

    # ── Stage 3: Branch membership + effective runAfter ──────────────────────

    branch_membership: Dict[str, Dict[str, str]] = {}
    children_in_order: Dict[str, List[str]] = {}
    roots_in_order: List[str] = []

    if reg and defn:
        branch_membership = _compute_branch_membership(defn)
        order_index_local = dict(order_index)
        first_in_branch = _compute_first_in_branch(branch_membership, order_index_local)

        for step_name in order:
            s = reg.get(step_name)
            if s is None:
                continue
            parent_name = getattr(s, "parent", None)
            if parent_name:
                children_in_order.setdefault(parent_name, []).append(step_name)
            else:
                roots_in_order.append(step_name)

        def _branch_parent_from_branch_id(bid: str) -> str:
            """Derive the controlling If/Switch step name from a branch_id token."""
            bid = str(bid or "")
            if not bid:
                return ""
            first_token = bid.split("|")[0]
            parts = first_token.split(":")
            if len(parts) >= 3 and parts[0] == "If":
                return parts[1]
            if len(parts) >= 3 and parts[0] == "Switch":
                return parts[1]
            return ""

        def _steps_in_same_branch(bid: str) -> List[str]:
            """Return steps in the same branch in global creation order."""
            if not bid:
                return []
            out: List[str] = []
            for n in order:
                info = branch_membership.get(n)
                if not isinstance(info, dict):
                    continue
                if str(info.get("branch_id") or "") == bid:
                    out.append(n)
            return out

        _effective_run_after_cache: Dict[str, Dict[str, Any]] = {}
        _era_depth: Set[str] = set()

        def _compute_effective_run_after(step_name: str) -> Dict[str, Any]:
            """
            Return the effective runAfter map (explicit or inferred).

            Includes cycle protection via _era_depth set.
            """
            if step_name in _effective_run_after_cache:
                return _effective_run_after_cache[step_name]

            if step_name in _era_depth:
                _effective_run_after_cache[step_name] = {}
                return {}
            _era_depth.add(step_name)

            try:
                s = reg.get(step_name)
                if s is None:
                    _effective_run_after_cache[step_name] = {}
                    return {}

                explicit = s.run_after or {}
                if isinstance(explicit, dict) and explicit:
                    _effective_run_after_cache[step_name] = explicit
                    return explicit

                info = branch_membership.get(step_name)
                bid = str(info.get("branch_id") or "") if isinstance(info, dict) else ""
                if bid:
                    branch_steps = _steps_in_same_branch(bid)
                    if step_name in branch_steps:
                        idx = branch_steps.index(step_name)
                        if idx > 0:
                            prev_branch = branch_steps[idx - 1]
                            inferred = {prev_branch: ["Succeeded"]}
                            _effective_run_after_cache[step_name] = inferred
                            return inferred
                        parent_ctrl = _branch_parent_from_branch_id(bid)
                        if parent_ctrl and parent_ctrl in reg:
                            inherited = _compute_effective_run_after(parent_ctrl)
                            _effective_run_after_cache[step_name] = inherited
                            return inherited

                parent_name = getattr(s, "parent", None)
                if parent_name and parent_name in reg and getattr(reg[parent_name], "is_container", False):
                    siblings = children_in_order.get(parent_name, [])
                    if siblings and siblings[0] == step_name:
                        inherited = _compute_effective_run_after(parent_name)
                        _effective_run_after_cache[step_name] = inherited
                        return inherited
                    if step_name in siblings:
                        idx = siblings.index(step_name)
                        if idx > 0:
                            prev_sib = siblings[idx - 1]
                            inferred = {prev_sib: ["Succeeded"]}
                            _effective_run_after_cache[step_name] = inferred
                            return inferred

                if step_name in roots_in_order:
                    idx = roots_in_order.index(step_name)
                    if idx > 0:
                        prev_root = roots_in_order[idx - 1]
                        inferred = {prev_root: ["Succeeded"]}
                        _effective_run_after_cache[step_name] = inferred
                        return inferred

                _effective_run_after_cache[step_name] = {}
                return {}

            finally:
                _era_depth.discard(step_name)

        # Validate effective runAfter.
        first_in_flow_name = order[0] if order else ""
        for step_name in order:
            if step_name not in reg:
                continue
            if step_name == first_in_flow_name:
                continue
            effective = _compute_effective_run_after(step_name)
            if not isinstance(effective, dict) or not effective:
                damage.warn(
                    "CARTO_W002",
                    f"No effective runAfter — step '{step_name}' (parent: {getattr(reg.get(step_name), 'parent', None)})",
                )

    # ── Stage 4: Collect types_used ──────────────────────────────────────────

    types_used: Set[str] = set()
    for name, s in reg.items():
        matched_type = getattr(s, "atype_display", s.atype) or s.atype
        types_used.add(matched_type)

    # ── Stage 5: Write outputs ───────────────────────────────────────────────

    # 5a. Flow model.
    try:
        flow_obj = build_flow_info_from_definition(defn, flow_name=str(input_path.stem))

        flattened_nodes = list(getattr(ordering_result_obj, "flattened_order", []) or []) if ordering_result_obj else []
        if flattened_nodes:
            flow_obj.execution_order = list(flattened_nodes)

        if getattr(flow_obj, "execution_order", None):
            execution_steps: List[str] = []
            seen: Set[str] = set()
            for node_id in (flow_obj.execution_order or []):
                leaf = str(node_id).split("::")[-1]
                if not flow_obj.has_node(leaf):
                    continue
                if leaf in seen:
                    continue
                execution_steps.append(leaf)
                seen.add(leaf)
            flow_obj.execution_steps = execution_steps

        node_order = getattr(flow_obj, "execution_steps", None) or flow_obj.execution_order
        if node_order:
            for idx, node_id in enumerate(node_order):
                if flow_obj.has_node(node_id):
                    n = flow_obj.get_node(node_id)
                    if n is not None:
                        n.order_index = idx

        flow_obj.meta = {
            "source_md5": source_md5,
            "run_id": run_id,
            "ordering_source": "phase3" if flattened_nodes else "legacy",
            "generator": "cartographer",
            "version": CARTOGRAPHER_VERSION,
        }

        fm_path = damage.output_path(out_dir, "flow_model", ".json", depends_on_ok=False)
        flow_obj.write_json(fm_path)
        damage.record_clean(fm_path.name)

        if args.verbose:
            print(f"Cartographer: wrote {fm_path}", file=sys.stderr)

    except Exception as exc:
        damage.error("CARTO_E008", f"Flow model construction failed: {exc}")
        fm_path = out_dir / "flow_model.damaged.json"
        fm_path.write_text(json.dumps({"_damage": str(exc)}, indent=2), encoding="utf-8")
        damage.record_damaged(fm_path.name)

    # 5b. Variables.
    try:
        prev_variables_root = load_previous_variables(out_dir)
        variables_list = merge_variable_status(
            current_map=current_variables,
            prev_root=prev_variables_root,
            run_id=run_id,
        )
        vj_path = damage.output_path(out_dir, "variables", ".json", depends_on_ok=False)
        write_variables_json(flow_root=out_dir, run_id=run_id, variables_list=variables_list)

        # Rename if needed (write_variables_json always writes to variables.json).
        actual_vj = out_dir / "variables.json"
        if damage.is_damaged() and actual_vj.exists() and actual_vj != vj_path:
            # Leave as variables.json — variables succeeded independently.
            pass

        damage.record_clean("variables.json")
        if args.verbose:
            print(f"Cartographer: wrote variables.json", file=sys.stderr)

    except Exception as exc:
        damage.error("CARTO_E006", f"Variable write failed: {exc}")
        vj_path = out_dir / "variables.damaged.json"
        vj_path.write_text(json.dumps({"_damage": str(exc)}, indent=2), encoding="utf-8")
        damage.record_damaged(vj_path.name)
        variables_list = []

    # 5c. Vitals.
    rules: Dict[str, Any] = {}
    try:
        rules = load_global_rules(_script_dir, verbose=args.verbose)
        override_rules = load_override_rules(out_dir, verbose=args.verbose)
        if override_rules:
            rules = deep_merge_dicts(rules, override_rules)
    except Exception:
        pass

    current_variables_count = sum(
        1 for v in variables_list if v.get("status") != "deprecated"
    ) if variables_list else 0

    vitals_root: Dict[str, Any] = {
        "flow_name": input_path.stem,
        "steps_count": len(reg),
        "types_used": sorted(types_used),
        "source_md5": source_md5,
        "variables": {
            "current": current_variables_count,
            "total_including_deprecated": len(variables_list),
        },
        "creation_order": [
            payload if kind == "STEP" else payload[0]
            for kind, payload in sequence
        ] if sequence else list(order),
        "containers": {
            name: children_of(name, reg)
            for name, step in reg.items()
            if getattr(step, "is_container", False)
        },
        "meta": {
            "run_id": run_id,
            "generator": "cartographer",
        },
    }

    # Vitals tree (can crash on parent/child mismatch — catch it).
    try:
        vitals_root["tree"] = build_vitals_tree(reg, order, rules)
    except Exception as exc:
        damage.error("CARTO_E009", f"Vitals tree failed: {exc}")
        vitals_root["tree"] = {"error": str(exc)}

    # Scope-aware duplicate detection (runs against raw definition, not registry).
    if defn:
        scope_actions = _build_scope_aware_actions(defn)
        vitals_root["actions_with_scope_paths"] = scope_actions
        dupes = [a for a in scope_actions if a["is_duplicate"]]
        if dupes:
            for d in dupes:
                damage.error(
                    "CARTO_E013",
                    f"Duplicate action name '{d['name']}' — {d['occurrences']} occurrences across scopes: {', '.join(d['scopes'])}",
                )

    # Circular dependency detection (runs against raw definition).
    if defn:
        cycles = _detect_circular_dependencies(defn)
        vitals_root["circular_dependencies"] = cycles
        if cycles:
            for cyc in cycles:
                damage.error(
                    "CARTO_E014",
                    f"Circular runAfter in scope '{cyc['scope']}' — {cyc['length']} steps: {' → '.join(cyc['cycle'])}",
                )


    vit_path = damage.output_path(out_dir, "vitals", ".json")
    try:
        vit_path.write_text(
            json.dumps(vitals_root, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if damage.is_damaged():
            damage.record_damaged(vit_path.name)
        else:
            damage.record_clean(vit_path.name)
        if args.verbose:
            print(f"Cartographer: wrote {vit_path}", file=sys.stderr)
    except Exception as exc:
        damage.error("CARTO_E009", f"Vitals write failed: {exc}")

    # 5d. Pathways.
    try:
        build_and_write_pathways_snapshot(
            flow_root=out_dir,
            reg=reg,
            vitals=vitals_root,
            rules=rules,
            source_md5=source_md5,
            conservative_pathing=bool(args.conservative_pathing),
        )
        damage.record_clean("pathways.json")
        if args.verbose:
            print(f"Cartographer: wrote pathways.json", file=sys.stderr)
    except Exception as exc:
        damage.error("CARTO_E010", f"Pathways generation failed: {exc}")
        pw_path = out_dir / "pathways.damaged.json"
        pw_path.write_text(json.dumps({"_damage": str(exc)}, indent=2), encoding="utf-8")
        damage.record_damaged(pw_path.name)

    # 5e. app_structure.yaml.
    yaml_path = damage.output_path(out_dir, "app_structure", ".yaml")
    try:
        _write_app_structure_yaml(yaml_path, input_path.stem, reg, order, current_variables, damage)
        if damage.is_damaged():
            damage.record_damaged(yaml_path.name)
        else:
            damage.record_clean(yaml_path.name)
        if args.verbose:
            print(f"Cartographer: wrote {yaml_path}", file=sys.stderr)
    except Exception as exc:
        damage.error("CARTO_E011", f"YAML structure write failed: {exc}")
        yaml_path = out_dir / "app_structure.damaged.yaml"
        yaml_path.write_text(f"_damage: {exc}\n", encoding="utf-8")
        damage.record_damaged(yaml_path.name)

    # 5f. app_structure.md.
    md_path = damage.output_path(out_dir, "app_structure", ".md")
    try:
        _write_app_structure_md(md_path, input_path.stem, reg, order, current_variables, types_used, damage)
        if damage.is_damaged():
            damage.record_damaged(md_path.name)
        else:
            damage.record_clean(md_path.name)
        if args.verbose:
            print(f"Cartographer: wrote {md_path}", file=sys.stderr)
    except Exception as exc:
        damage.error("CARTO_E012", f"Markdown structure write failed: {exc}")
        md_path = out_dir / "app_structure.damaged.md"
        md_path.write_text(f"> DAMAGE: {exc}\n", encoding="utf-8")
        damage.record_damaged(md_path.name)

    # 5g. fingerprints.json (--detailed mode only).
    if args.detailed and defn:
        fp_path = damage.output_path(out_dir, "fingerprints", ".json")
        try:
            chop = args.chop_checksum
            all_fingerprints = build_fingerprints_for_app(defn, chop=chop)

            fp_output = {
                "schema": "fingerprints_v1",
                "version": CARTOGRAPHER_VERSION,
                "chop": chop,
                "source_md5": source_md5,
                "run_id": run_id,
                "total_scoped_steps": len(all_fingerprints),
                "steps": all_fingerprints,
            }

            fp_path.write_text(
                json.dumps(fp_output, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if damage.is_damaged():
                damage.record_damaged(fp_path.name)
            else:
                damage.record_clean(fp_path.name)
            if args.verbose:
                print(f"Cartographer: wrote {fp_path} ({len(all_fingerprints)} steps, chop={chop})", file=sys.stderr)
        except Exception as exc:
            damage.error("CARTO_E015", f"Fingerprints generation failed: {exc}")
            fp_path = out_dir / "fingerprints.damaged.json"
            fp_path.write_text(json.dumps({"_damage": str(exc)}, indent=2), encoding="utf-8")
            damage.record_damaged(fp_path.name)

    # ── Finalize ─────────────────────────────────────────────────────────────

    if damage.is_damaged():
        damage.write_damage_report(out_dir, str(args.input), run_id)
        print(f"Cartographer v{CARTOGRAPHER_VERSION}: DAMAGED — {len(damage.errors)} error(s), see DAMAGE_REPORT.md", file=sys.stderr)
    else:
        print(f"Cartographer v{CARTOGRAPHER_VERSION}: OK — {len(reg)} steps, {len(current_variables)} variables", file=sys.stderr)

    print(f"Cartographer: output → {out_dir}", file=sys.stderr)

    if notes:
        for note in notes:
            print(f"  {note}", file=sys.stderr)

    return 1 if damage.is_damaged() else 0


# Standard main guard
if __name__ == "__main__":
    raise SystemExit(main())
