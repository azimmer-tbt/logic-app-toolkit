#!/usr/bin/env python3
# File: helpers/flow_model.py
"""
Flow-level model that owns StepNodes plus canonical ordering/pathways views.

This module is intentionally "thin but central":
- Step parsing stays in helpers/definition_model.py (StepInfo/StepNode/registry).
- Ordering/pathways logic stays in order_* modules.
- This FlowInfo object becomes the shared handoff:
    Analyzer builds FlowInfo (and serializes flow_model.json).
    Post-Processor/Renderer consume flow_model.json without re-deriving graphs.

Design goals
------------
- Keep writers "dumb": they render lists/rows already planned.
- Keep ordering logic centralized: compute once, store on FlowInfo/StepNode.
- Keep JSON serialization straightforward and stable over time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from helpers.definition_model import (
    StepNode,
    build_step_nodes_from_registry,
    collect_registry,
)


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------


@dataclass
class PathwayRow:
    """Render-ready pathways row with 3 lanes.

    Each lane is a "block": a list of human-readable step titles.
    Writers should join with '<br>' for markdown table cells.
    """

    fail: List[str] = field(default_factory=list)
    success: List[str] = field(default_factory=list)
    alt_success: List[str] = field(default_factory=list)

    # Optional debug metadata (safe to ignore in renderers)
    debug: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fail": list(self.fail),
            "success": list(self.success),
            "alt_success": list(self.alt_success),
            "debug": dict(self.debug) if self.debug else {},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PathwayRow":
        return PathwayRow(
            fail=list(d.get("fail", []) or []),
            success=list(d.get("success", []) or []),
            alt_success=list(d.get("alt_success", []) or []),
            debug=dict(d.get("debug", {}) or {}),
        )


@dataclass
class FlowInfo:
    """Flow-level container for StepNodes and canonical ordered views.

    Notes
    -----
    - `nodes` holds StepNodes keyed by code name.
    - Ordering modules should fill:
        - node.path (optional)
        - node.order_index
        - execution_order (flattened walk order)
        - pathways_rows (render-ready)
    """

    flow_name: str = ""
    nodes: Dict[str, StepNode] = field(default_factory=dict)

    # Root/top-level actions order (as seen in the definition's actions map)
    root_children: List[str] = field(default_factory=list)

    # Canonical flattened designer walk order (StepNode names)
    execution_order: List[str] = field(default_factory=list)

    # Steps-only flattened execution order (real StepNode names only; no branch markers like true/false/default/case:*).
    # Intended for StepDocs stitch order and “Steps by build sequence” outputs.
    execution_steps: List[str] = field(default_factory=list)

    # Render-ready pathways table plan
    pathways_rows: List[PathwayRow] = field(default_factory=list)

    # Optional synthetic nodes (e.g., scope_end markers) and extra metadata
    synthetic_nodes: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------------------
    # Convenience accessors
    # -------------------------------------------------------------------------

    def has_node(self, name: str) -> bool:
        return bool(name) and name in self.nodes

    def get_node(self, name: str) -> Optional[StepNode]:
        return self.nodes.get(name)

    def children_of(self, name: str) -> List[str]:
        node = self.get_node(name)
        return list(node.children) if node else []

    def parent_of(self, name: str) -> Optional[str]:
        node = self.get_node(name)
        return node.parent if node else None

    def next_in_execution(self, name: str) -> Optional[str]:
        """Return next step in flattened execution order, if present."""
        if not self.execution_order:
            return None
        try:
            idx = self.execution_order.index(name)
        except ValueError:
            return None
        nxt = idx + 1
        if nxt >= len(self.execution_order):
            return None
        return self.execution_order[nxt]

    def prev_in_execution(self, name: str) -> Optional[str]:
        """Return previous step in flattened execution order, if present."""
        if not self.execution_order:
            return None
        try:
            idx = self.execution_order.index(name)
        except ValueError:
            return None
        prv = idx - 1
        if prv < 0:
            return None
        return self.execution_order[prv]

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "flow_model_v1",
            "flow_name": self.flow_name,
            "root_children": list(self.root_children),
            "execution_order": list(self.execution_order),
            "execution_steps": list(self.execution_steps),
            "synthetic_nodes": list(self.synthetic_nodes),
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
            "pathways_rows": [r.to_dict() for r in self.pathways_rows],
            "meta": dict(self.meta) if self.meta else {},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FlowInfo":
        nodes_in = d.get("nodes", {}) or {}
        nodes: Dict[str, StepNode] = {}

        # Rehydrate StepNode objects from serialized dicts.
        # (We keep it strict/simple: StepNode stores links as names.)
        for name, nd in nodes_in.items():
            node = StepNode(
                name=nd.get("name", name),
                atype=nd.get("atype", ""),
                run_after=nd.get("run_after", {}) or {},
                parent=nd.get("parent", None),
                raw={},  # raw is intentionally not stored in flow_model.json
                path=nd.get("path", "") or "",
                order_index=int(nd.get("order_index", -1)),
                is_synthetic=bool(nd.get("is_synthetic", False)),
                synthetic_kind=str(nd.get("synthetic_kind", "") or ""),
            )
            node.atype_display = nd.get("atype_display", node.atype)
            node.is_container = bool(nd.get("is_container", False))
            node.pretty_category = nd.get("pretty_category", "") or ""
            node.pretty_type = nd.get("pretty_type", "") or ""
            node.pretty_name = nd.get("pretty_name", "") or ""

            node.children = list(nd.get("children", []) or [])
            node.predecessors = list(nd.get("predecessors", []) or [])
            node.successors = list(nd.get("successors", []) or [])

            nodes[name] = node

        return FlowInfo(
            flow_name=str(d.get("flow_name", "") or ""),
            nodes=nodes,
            root_children=list(d.get("root_children", []) or []),
            execution_order=list(d.get("execution_order", []) or []),
            execution_steps=list(d.get("execution_steps", []) or []),
            pathways_rows=[PathwayRow.from_dict(r) for r in (d.get("pathways_rows", []) or [])],
            synthetic_nodes=list(d.get("synthetic_nodes", []) or []),
            meta=dict(d.get("meta", {}) or {}),
        )

    def write_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False), encoding="utf-8")
        return p

    @staticmethod
    def read_json(path: str | Path) -> "FlowInfo":
        p = Path(path)
        return FlowInfo.from_dict(json.loads(p.read_text(encoding="utf-8")))


# -----------------------------------------------------------------------------
# Builders
# -----------------------------------------------------------------------------


def build_flow_info_from_definition(
    defn: Dict[str, Any],
    *,
    flow_name: str = "",
) -> FlowInfo:
    """Build FlowInfo from the Logic Apps `definition` object.

    This builds ONLY:
      - StepNodes (from StepInfo registry)
      - root_children (definition['actions'] keys order)
      - parent/child links
      - predecessors/successors (from runAfter)
    It does NOT compute:
      - flattened execution_order
      - pathways_rows

    Those are produced by ordering/pathways modules and then stored back onto
    the returned FlowInfo instance.
    """
    actions = defn.get("actions", {}) or {}
    root_children = list(actions.keys())

    registry = collect_registry(defn)
    nodes = build_step_nodes_from_registry(registry)

    return FlowInfo(
        flow_name=flow_name,
        nodes=nodes,
        root_children=root_children,
    )
