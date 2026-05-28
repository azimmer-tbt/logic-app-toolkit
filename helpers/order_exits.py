# helpers/order_exits.py
"""
Designer-faithful ordering: scope entry/exit resolution (Phase 2).

Phase 2 derives *semantics* from the Phase 1 extracted model:
- entry_nodes: nodes with no in-scope predecessors
- local_terminals: nodes with no in-scope successors
- exit_nodes: alias of local_terminals (kept for clarity)
- synthetic scope_end node (not added to Phase 1 model):
    scope_end depends on all exit_nodes

Why this matters:
- Branches (If/Switch) naturally create multiple local terminals.
- A "logging chain" on a fail branch can terminate locally and never rejoin the
  happy path. Designer still treats the parent container as completing at the
  end of the taken branch.
- Representing that as a synthetic scope_end join node makes later ordering
  (reverse-walk) stable and designer-faithful.

This module does NOT compute final ordering yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from helpers.order_model import Model, Node


JsonDict = Dict[str, Any]


# -----------------------------------------------------------------------------
# Scope model
# -----------------------------------------------------------------------------

@dataclass
class ScopeInfo:
    """
    Derived analysis for a single scope (root, container action, or branch group).
    """

    scope_id: str
    scope_name: str
    parent_scope_id: Optional[str]

    collapsed_nodes: List[str] = field(default_factory=list)

    # Edges computed within this scope between collapsed_nodes:
    # predecessors[n] = set(nodes that must complete before n)
    # successors[n]   = set(nodes that run after n)
    predecessors: Dict[str, Set[str]] = field(default_factory=dict)
    successors: Dict[str, Set[str]] = field(default_factory=dict)

    entry_nodes: List[str] = field(default_factory=list)
    local_terminals: List[str] = field(default_factory=list)
    exit_nodes: List[str] = field(default_factory=list)

    scope_end_node_id: str = ""
    scope_end_deps: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def compute_scopes(
    model: Model,
) -> List[ScopeInfo]:
    """
    Compute ScopeInfo for:
    - root scope
    - every container-ish node (anything with children_ids)
    - every branch_group node (also has children_ids)

    Returns:
        List[ScopeInfo] in no particular order (debug module can sort).
    """
    scopes: List[ScopeInfo] = []

    # Root scope
    root_scope = _compute_scope_for_children(
        model=model,
        scope_id="root",
        scope_name="Root",
        parent_scope_id=None,
        child_ids=list(model.root_children),
    )
    scopes.append(root_scope)

    # Any node with children becomes its own scope
    for node_id, node in model.nodes.items():
        if not node.children_ids:
            continue

        scope_name = node.display_name or node.action_name or node_id
        scopes.append(
            _compute_scope_for_children(
                model=model,
                scope_id=node_id,
                scope_name=scope_name,
                parent_scope_id=node.parent_id,
                child_ids=list(node.children_ids),
            )
        )

    return scopes


def scopes_to_debug_dicts(
    scopes: List[ScopeInfo],
) -> List[JsonDict]:
    """
    Convert ScopeInfo list into dicts suitable for order_debug.py `scopes=...`.

    Keeps the output stable and JSON-safe.
    """
    out: List[JsonDict] = []
    for s in scopes:
        edges_run_after: List[JsonDict] = []
        edges_run_before: List[JsonDict] = []

        for to_id, preds in s.predecessors.items():
            for from_id in sorted(preds):
                edges_run_after.append({"from": from_id, "to": to_id})

        for from_id, succs in s.successors.items():
            for to_id in sorted(succs):
                edges_run_before.append({"from": from_id, "to": to_id})

        out.append(
            {
                "scope_id": s.scope_id,
                "scope_name": s.scope_name,
                "parent_scope_id": s.parent_scope_id,
                "collapsed_nodes": list(s.collapsed_nodes),
                "entry_nodes": list(s.entry_nodes),
                "local_terminals": list(s.local_terminals),
                "exit_nodes": list(s.exit_nodes),
                "scope_end_node_id": s.scope_end_node_id,
                "scope_end_deps": list(s.scope_end_deps),
                "edges": {
                    "run_after": edges_run_after,
                    "run_before": edges_run_before,
                },
                "notes": list(s.notes),
            }
        )
    return out


# -----------------------------------------------------------------------------
# Core computation
# -----------------------------------------------------------------------------

def _compute_scope_for_children(
    *,
    model: Model,
    scope_id: str,
    scope_name: str,
    parent_scope_id: Optional[str],
    child_ids: List[str],
) -> ScopeInfo:
    """
    Compute in-scope dependency edges and derive entry/exit nodes for a scope.

    IMPORTANT:
    This scope is defined over its *collapsed nodes* (direct children only).
    If a child is a branch_group node, its internal actions will be analyzed
    in that branch_group's own ScopeInfo.
    """
    s = ScopeInfo(
        scope_id=scope_id,
        scope_name=scope_name,
        parent_scope_id=parent_scope_id,
        collapsed_nodes=[str(x) for x in child_ids],
    )

    # Initialize adjacency maps
    sib_set: Set[str] = set(s.collapsed_nodes)
    for nid in s.collapsed_nodes:
        s.predecessors[nid] = set()
        s.successors[nid] = set()

    # Build edges within the scope based on runAfter references that point to siblings.
    for nid in s.collapsed_nodes:
        node = model.nodes.get(nid)
        if node is None:
            s.notes.append(f"WARNING: node not found in model: {nid}")
            continue

        deps = _run_after_deps(node)
        for dep_action_name in deps:
            dep_id = _resolve_dep_to_sibling_id(
                model=model,
                scope_parent_id=scope_id if scope_id != "root" else None,
                sibling_ids=sib_set,
                dep_action_name=dep_action_name,
            )
            if dep_id is None:
                # Dependency exists but is not a sibling in this scope;
                # it is either outside this scope or refers to a nested child.
                continue

            s.predecessors[nid].add(dep_id)
            s.successors[dep_id].add(nid)

    # entry_nodes: no predecessors in this scope
    entry = [nid for nid in s.collapsed_nodes if not s.predecessors.get(nid)]
    # local_terminals: no successors in this scope
    terms = [nid for nid in s.collapsed_nodes if not s.successors.get(nid)]

    s.entry_nodes = sorted(entry, key=str.lower)
    s.local_terminals = sorted(terms, key=str.lower)
    s.exit_nodes = list(s.local_terminals)

    # Synthetic scope_end
    s.scope_end_node_id = _scope_end_id(scope_id)
    s.scope_end_deps = list(s.exit_nodes)

    # Notes for common edge cases
    if len(s.collapsed_nodes) > 0 and len(s.entry_nodes) == 0:
        s.notes.append("NOTE: scope has nodes but no entry_nodes (cycle or missing edge extraction).")

    if len(s.exit_nodes) == 0 and len(s.collapsed_nodes) > 0:
        s.notes.append("NOTE: scope has nodes but no exit_nodes (cycle or missing edge extraction).")

    if len(s.exit_nodes) > 1:
        s.notes.append(
            f"INFO: scope has multiple local terminals ({len(s.exit_nodes)}). "
            "This is expected for If/Switch branches and 'logging chain' fail paths."
        )

    return s


# -----------------------------------------------------------------------------
# Dependency resolution helpers
# -----------------------------------------------------------------------------

def _run_after_deps(node: Node) -> List[str]:
    """
    Extract dependency action names from node.run_after.
    """
    ra = node.run_after or {}
    if isinstance(ra, dict):
        return list(ra.keys())
    return []


def _resolve_dep_to_sibling_id(
    *,
    model: Model,
    scope_parent_id: Optional[str],
    sibling_ids: Set[str],
    dep_action_name: str,
) -> Optional[str]:
    """
    Given a runAfter dependency key (which is an action name), resolve it to the
    sibling node_id within this scope.

    In Logic Apps JSON, runAfter references are typically action keys (names),
    not node_ids. Our node_ids are either:
      - root: "ActionName"
      - nested: "<parent_node_id>::ActionName"

    So if we're in a nested scope, we try:
      - "<scope_parent_id>::<dep_action_name>"
      - "<dep_action_name>" (as a fallback)

    Returns:
        The sibling node_id if found; otherwise None.
    """
    candidates: List[str] = []

    if scope_parent_id:
        candidates.append(f"{scope_parent_id}::{dep_action_name}")

    candidates.append(dep_action_name)

    for cand in candidates:
        if cand in sibling_ids:
            return cand

    return None


def _scope_end_id(scope_id: str) -> str:
    """
    Stable synthetic scope_end node id.
    """
    return f"{scope_id}::end"
