#!/usr/bin/env python3
# File: helpers/definition_model.py
# -*- coding: utf-8 -*-
"""
Shared helpers for parsing a Logic Apps definition into an in-memory registry.

This module owns:
  - Core definition extraction (definition / triggers / actions).
  - Container detection.
  - The StepInfo model.
  - Step registry construction from a Logic Apps definition.
  - The helper to build the per-step `data` payload from raw JSON.

Analyzer and other tools should consume these helpers instead of
re-implementing parsing/understanding logic.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Definition extraction
# ─────────────────────────────────────────────────────────────────────────────


def extract_core(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Extract the core Logic Apps definition and its trigger/action maps.

    Parameters
    ----------
    doc:
        The full Logic Apps export JSON (or the `definition` object itself).

    Returns
    -------
    defn, triggers, actions:
        The normalized definition object plus its `triggers` and `actions`
        sub-maps (each defaulting to an empty dict when absent).
    """
    definition = doc.get("definition", doc)
    return (
        definition,
        definition.get("triggers", {}) or {},
        definition.get("actions", {}) or {},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Container detection
# ─────────────────────────────────────────────────────────────────────────────


def is_container_type(atype: str) -> bool:
    """
    Return True if the engine type is a known container/scope type.

    Container types typically own nested actions/cases/branches, such as:
      - Scope
      - Foreach
      - If
      - Switch
    """
    return atype in {"Scope", "Foreach", "If", "Switch"}


def _compute_is_container_flag(raw: Dict[str, Any], atype: str) -> bool:
    """
    Decide whether a step is a container/scope.

    Criteria:
      - Engine/container type (via is_container_type), OR
      - Structural hints in the raw JSON:
          * actions: { ... }
          * cases: { ... }
          * branches: [ ... ]
          * else.actions: { ... }
    """
    raw = raw or {}
    atype = atype or ""

    # 1) Type-level container classification (e.g., Scope, ForEach, If, Switch).
    try:
        if is_container_type(atype):
            return True
    except NameError:
        # Defensive: if is_container_type is not visible for some reason,
        # treat the step as non-container rather than failing.
        return False

    # 2) Structural hints from the JSON payload.
    if isinstance(raw.get("actions"), dict):
        return True
    if isinstance(raw.get("cases"), dict):
        return True
    if isinstance(raw.get("branches"), list):
        return True
    if isinstance(raw.get("else"), dict) and isinstance(raw["else"].get("actions"), dict):
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Step model + registry
# ─────────────────────────────────────────────────────────────────────────────


class StepInfo:
    """
    Represents a single Logic Apps step, with lightweight metadata extracted
    during the analyzer pass.

    Uses __slots__ to reduce memory footprint, so all fields must be declared
    here explicitly.
    """

    __slots__ = (
        "name",            # step name (CodeName)
        "atype",           # raw type (e.g. "Compose", "Http")
        "atype_display",   # resolved/designer type for display
        "run_after",       # dict of predecessors and their states
        "parent",          # container (scope) name if any
        "raw",             # raw JSON for the step
        "pretty_category", # pretty category (e.g. "Data Operations")
        "pretty_type",     # designer-facing type label from catalog
        "pretty_name",     # per-instance designer name (from CodeName)
        "is_container",    # boolean: is this step a container
        "is_first_child_in_container",
        "placement_anchor_step",
        "placement_anchor_pretty_step",
    )

    def __init__(
        self,
        name: str,
        atype: str,
        run_after: Dict[str, List[str]],
        parent: Optional[str],
        raw: Dict[str, Any],
    ) -> None:
        self.name = name
        self.atype = atype
        self.atype_display = atype
        self.run_after = run_after or {}
        self.parent = parent
        self.raw = raw

        # Compute container flag once, based on raw JSON + type.
        self.is_container = _compute_is_container_flag(self.raw, self.atype)

        # Filled in later by catalog attachment:
        #   - pretty_category: catalog category bucket (e.g. "Data Operations")
        #   - pretty_type:     catalog type label (e.g. "Set variable")
        #   - pretty_name:     per-instance designer name (derived from CodeName)
        #   - is_first_child_in_container: whether this step is first in its container
        self.pretty_category = ""
        self.pretty_type = ""
        self.pretty_name = ""
        self.is_first_child_in_container = False

        # Filled later by ordering/placement enrichment:
        #   - placement_anchor_step: CodeName of the step to click “+” under (Case 4)
        #   - placement_anchor_pretty_step: Pretty name of that anchor step
        self.placement_anchor_step = ""
        self.placement_anchor_pretty_step = ""


# ─────────────────────────────────────────────────────────────────────────────
# StepNode: Navigable graph node model
# ─────────────────────────────────────────────────────────────────────────────

class StepNode(StepInfo):
    """A navigable, downstream-friendly step model.

    StepInfo is the lightweight, definition-parsed record.
    StepNode extends it with relationship fields and ordering slots so later
    stages (ordering, pathways, rendering) do not need to re-derive graphs.

    Notes
    -----
    - The Analyzer/ordering modules should populate ordering/pathway fields.
    - This class intentionally stores names (strings) for links (children,
      predecessors, successors) to keep JSON serialization simple.
    """

    __slots__ = StepInfo.__slots__ + (
        "path",          # optional fully-qualified path (Root::Scope::...)
        "children",      # ordered list of direct child step names
        "predecessors",  # direct runAfter predecessors (step names)
        "successors",    # inverse of predecessors (step names)
        "order_index",   # optional global ordering index (flattened walk)
        "is_synthetic",  # supports synthetic nodes (e.g., scope_end)
        "synthetic_kind",
    )

    def __init__(
        self,
        name: str,
        atype: str,
        run_after: Dict[str, List[str]],
        parent: Optional[str],
        raw: Dict[str, Any],
        *,
        path: str = "",
        order_index: int = -1,
        is_synthetic: bool = False,
        synthetic_kind: str = "",
    ) -> None:
        super().__init__(name=name, atype=atype, run_after=run_after, parent=parent, raw=raw)

        # Navigation
        self.path = path
        self.children: List[str] = []
        self.predecessors: List[str] = []
        self.successors: List[str] = []

        # Ordering/debug
        self.order_index = int(order_index)

        # Synthetic node support
        self.is_synthetic = bool(is_synthetic)
        self.synthetic_kind = str(synthetic_kind or "")

    def add_child(self, child_name: str) -> None:
        if child_name and child_name not in self.children:
            self.children.append(child_name)

    def add_predecessor(self, pred_name: str) -> None:
        if pred_name and pred_name not in self.predecessors:
            self.predecessors.append(pred_name)

    def add_successor(self, succ_name: str) -> None:
        if succ_name and succ_name not in self.successors:
            self.successors.append(succ_name)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the node to a JSON-safe dict (for flow_model.json)."""
        return {
            "name": self.name,
            "atype": self.atype,
            "atype_display": self.atype_display,
            "run_after": self.run_after or {},
            "parent": self.parent,
            "path": self.path,
            "is_container": bool(self.is_container),
            "pretty_category": self.pretty_category,
            "pretty_type": self.pretty_type,
            "pretty_name": self.pretty_name,
            "placement_anchor_step": getattr(self, "placement_anchor_step", ""),
            "placement_anchor_pretty_step": getattr(self, "placement_anchor_pretty_step", ""),
            "children": list(self.children),
            "predecessors": list(self.predecessors),
            "successors": list(self.successors),
            "order_index": int(self.order_index),
            "is_synthetic": bool(self.is_synthetic),
            "synthetic_kind": self.synthetic_kind,
        }


def build_step_nodes_from_registry(registry: Dict[str, StepInfo]) -> Dict[str, StepNode]:
    """Build StepNodes from a StepInfo registry and attach navigability links.

    This helper intentionally does NOT compute execution order.

    It attaches:
      - parent → children links (nesting)
      - runAfter → predecessors links (direct)
      - successors links (inverse)

    Ordering/pathways modules can later set `order_index`, `path`, and
    synthetic node metadata.
    """

    nodes: Dict[str, StepNode] = {}

    # 1) Clone base fields.
    for name, info in (registry or {}).items():
        nodes[name] = StepNode(
            name=info.name,
            atype=info.atype,
            run_after=info.run_after,
            parent=info.parent,
            raw=info.raw,
        )

        # Carry over any catalog-enriched fields (if already attached).
        nodes[name].atype_display = getattr(info, "atype_display", info.atype)
        nodes[name].pretty_category = getattr(info, "pretty_category", "")
        nodes[name].pretty_type = getattr(info, "pretty_type", "")
        nodes[name].pretty_name = getattr(info, "pretty_name", "")
        nodes[name].placement_anchor_step = getattr(info, "placement_anchor_step", "")
        nodes[name].placement_anchor_pretty_step = getattr(info, "placement_anchor_pretty_step", "")
        nodes[name].is_container = getattr(info, "is_container", False)

    # 2) Parent → children links.
    for name, node in nodes.items():
        if node.parent and node.parent in nodes:
            nodes[node.parent].add_child(name)

    # 3) Predecessors from runAfter.
    for name, node in nodes.items():
        ra = node.run_after or {}
        if isinstance(ra, dict):
            for pred in ra.keys():
                if pred in nodes:
                    node.add_predecessor(pred)

    # 4) Successors (inverse edges).
    for name, node in nodes.items():
        for pred in list(node.predecessors):
            nodes[pred].add_successor(name)

    return nodes


def _walk_actions(
    actions: Dict[str, Any],
    parent: Optional[str],
    registry: Dict[str, StepInfo],
) -> None:
    """
    Recursively walk nested actions and populate the StepInfo registry.

    This mirrors the Logic Apps nesting structure, recursing into:
      - actions
      - else.actions
      - cases[*].actions
      - default.actions
      - branches[*].actions
    """
    for name, obj in (actions or {}).items():
        atype = obj.get("type", "Action")
        registry[name] = StepInfo(
            name=name,
            atype=atype,
            run_after=obj.get("runAfter", {}),
            parent=parent,
            raw=obj,
        )

        # Descend containers: actions, else.actions, cases, default, branches.
        if isinstance(obj.get("actions"), dict):
            _walk_actions(obj["actions"], name, registry)

        if isinstance(obj.get("else"), dict) and isinstance(obj["else"].get("actions"), dict):
            _walk_actions(obj["else"]["actions"], name, registry)

        if isinstance(obj.get("cases"), dict):
            for case_value in obj["cases"].values():
                if isinstance(case_value, dict) and isinstance(case_value.get("actions"), dict):
                    _walk_actions(case_value["actions"], name, registry)

        if isinstance(obj.get("default"), dict) and isinstance(obj["default"].get("actions"), dict):
            _walk_actions(obj["default"]["actions"], name, registry)

        if isinstance(obj.get("branches"), list):
            for branch in obj["branches"]:
                if isinstance(branch, dict) and isinstance(branch.get("actions"), dict):
                    _walk_actions(branch["actions"], name, registry)


def collect_registry(defn: Dict[str, Any]) -> Dict[str, StepInfo]:
    """
    Build a name → StepInfo registry from the Logic Apps definition.

    Parameters
    ----------
    defn:
        The normalized `definition` object (output of extract_core()).

    Returns
    -------
    dict
        Mapping from step CodeName to StepInfo instance.
    """
    registry: Dict[str, StepInfo] = {}
    _walk_actions(defn.get("actions", {}) or {}, parent=None, registry=registry)
    
    # Set is_first_child_in_container for each step
    for name, step in registry.items():
        if step.parent:
            children = [n for n, s in registry.items() if s.parent == step.parent]
            step.is_first_child_in_container = (children and children[0] == name)
    
    return registry


# ─────────────────────────────────────────────────────────────────────────────
# Step data payload helper
# ─────────────────────────────────────────────────────────────────────────────


def _build_step_data_from_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the template-facing `data` payload for a step card from its raw JSON.

    Rules (see Stage 1 contract §6.1):
      - Start from a deep copy of the step's `raw` payload.
      - Strip structural keys that are handled separately, notably `runAfter`.
      - Leave other fields intact so templates and overrides can address them
        via `data.*` (e.g., data.inputs, data.body, data.parameters).
    """
    cleaned: Dict[str, Any] = copy.deepcopy(raw or {})

    # Structural keys that should not be part of the data.* tree.
    cleaned.pop("runAfter", None)

    return cleaned
