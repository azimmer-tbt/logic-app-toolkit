# helpers/order_walk.py
"""
Designer-faithful ordering: ordering walk (Phase 3).

Phase 3 takes:
- Phase 1 extracted model (helpers/order_model.py)
- Phase 2 scope graphs (helpers/order_exits.py ScopeInfo)

…and computes:
1) A deterministic linear order of each scope's collapsed nodes
   - dependency-respecting (topological)
   - stable (reproducible)
   - tie-broken using "designer-ish" heuristics:
       * negative/fail/default/false-first
       * then cases
       * then true/success
       * then stable original appearance in that scope
2) A container-contiguous flattened order for the whole workflow
   - emit root scope in computed order
   - when encountering a node that has children, immediately emit its scope order

Notes / assumptions:
- This does NOT attempt to reproduce exact pixel-level Designer layout.
- It does aim to reproduce the *reading order* a human sees: top-to-bottom,
  keeping containers together, and showing fail/default branches first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from helpers.order_model import Model, Node, NodeKind
from helpers.order_exits import ScopeInfo


JsonDict = Dict[str, Any]


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------

@dataclass
class OrderingResult:
    """
    Output of Phase 3 ordering.

    - scope_orders: scope_id -> ordered list of node_ids for that scope
    - flattened_order: container-contiguous expansion starting from root
    - execution_steps: flattened order reduced to *real* nodes (actions/containers) only,
      suitable for StepDocs ordering (excludes synthetic branch markers)
    - tie_breaks_used: per-scope notes when we had to pick among multiple ready nodes
    """
    scope_orders: Dict[str, List[str]] = field(default_factory=dict)
    flattened_order: List[str] = field(default_factory=list)
    execution_steps: List[str] = field(default_factory=list)
    tie_breaks_used: Dict[str, List[str]] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def compute_scope_orders(
    *,
    model: Model,
    scopes: List[ScopeInfo],
) -> OrderingResult:
    """
    Compute Phase 3 ordering for every scope and also a flattened order.

    Args:
        model:
            Phase 1 model.
        scopes:
            Phase 2 derived scopes.

    Returns:
        OrderingResult containing per-scope orders and a flattened order.
    """
    scope_map = {s.scope_id: s for s in scopes}
    result = OrderingResult()

    # Compute per-scope orders
    for scope_id, scope in scope_map.items():
        order, tie_notes = _order_one_scope(model=model, scope=scope)
        result.scope_orders[scope_id] = order
        if tie_notes:
            result.tie_breaks_used[scope_id] = tie_notes

    # Compute flattened order (container-contiguous) starting from root
    result.flattened_order = flatten_container_contiguous(
        model=model,
        scope_orders=result.scope_orders,
        root_scope_id="root",
        include_branch_groups=True,
    )
    result.execution_steps = derive_execution_steps(
        model=model,
        flattened_order=result.flattened_order,
    )

    return result


# -----------------------------------------------------------------------------
# Core ordering logic
# -----------------------------------------------------------------------------

def derive_execution_steps(
    *,
    model: Model,
    flattened_order: List[str],
) -> List[str]:
    """Derive a "real steps only" execution list from a flattened walk.

    Why:
    - `flattened_order` is allowed to include synthetic BRANCH_GROUP nodes such as:
      `...::branch::true`, `...::branch::false`, `...::branch::default`, `...::branch::case:*`.
    - Downstream artifacts like StepDocs ordering must only include real nodes that
      exist in `model.nodes` (actions + containers).

    Strategy:
    - For each entry in `flattened_order`, take its leaf token (split on "::").
    - Keep it only if it exists in `model.nodes`.
    - Preserve first-seen order and drop duplicates.

    Returns:
        List of node ids (leaf action/container ids) in execution order.
    """
    out: List[str] = []
    seen: Set[str] = set()

    for entry in flattened_order:
        leaf = str(entry).split("::")[-1]
        if leaf in seen:
            continue
        node = model.nodes.get(leaf)
        if node is None:
            # Synthetic marker (true/false/default/case:*) or unknown id.
            continue
        if node.kind == NodeKind().BRANCH_GROUP:
            # Defensive: branch groups should not appear as real steps.
            continue

        seen.add(leaf)
        out.append(leaf)

    return out


def flatten_container_contiguous(
    *,
    model: Model,
    scope_orders: Dict[str, List[str]],
    root_scope_id: str = "root",
    include_branch_groups: bool = True,
) -> List[str]:
    """
    Expand scope orders into a single list, keeping containers contiguous.

    Strategy:
    - Emit scope_orders[root_scope_id] in order
    - For each emitted node that itself has children, recurse immediately

    Args:
        include_branch_groups:
            If True, include synthetic BRANCH_GROUP nodes in the flattened list.
            If False, we still recurse into them but do not emit the group node
            itself (useful if you don't want "German", "Default" headings as nodes).

    Returns:
        Flattened node_id list.
    """
    out: List[str] = []
    visited_scopes: Set[str] = set()

    def _emit_scope(scope_id: str) -> None:
        if scope_id in visited_scopes:
            # Prevent infinite recursion in malformed graphs.
            return
        visited_scopes.add(scope_id)

        ordered = scope_orders.get(scope_id, [])
        for nid in ordered:
            node = model.nodes.get(nid)
            if node is None:
                continue

            if node.kind == NodeKind().BRANCH_GROUP and not include_branch_groups:
                # Skip emitting the node itself but still emit its children scope.
                if node.children_ids and nid in scope_orders:
                    _emit_scope(nid)
                continue

            out.append(nid)

            # If node has children, recurse into its scope
            if node.children_ids and nid in scope_orders:
                _emit_scope(nid)

    _emit_scope(root_scope_id)
    return out


def inject_computed_orders_into_scopes_for_debug(
    *,
    scopes: List[ScopeInfo],
    scope_orders: Dict[str, List[str]],
    tie_breaks_used: Dict[str, List[str]],
) -> List[JsonDict]:
    """
    Convenience helper: convert ScopeInfo into debug dicts and add:
      - computed_order
      - tie_breaks_used

    This keeps analyzer wiring clean.
    """
    from helpers.order_exits import scopes_to_debug_dicts  # local import to avoid cycles

    scope_dicts = scopes_to_debug_dicts(scopes)
    for s in scope_dicts:
        sid = str(s.get("scope_id") or "")
        if sid in scope_orders:
            s["computed_order"] = list(scope_orders[sid])
        if sid in tie_breaks_used:
            s["tie_breaks_used"] = list(tie_breaks_used[sid])
    return scope_dicts



def _order_one_scope(
    *,
    model: Model,
    scope: ScopeInfo,
) -> Tuple[List[str], List[str]]:
    """
    Order one scope using a stable topological sort with designer-ish tie-breaks.

    Returns:
        (ordered_nodes, tie_break_notes)
    """
    # Start from Phase 2 adjacency
    nodes = list(scope.collapsed_nodes)
    node_set = set(nodes)

    # Local copies we can mutate
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    succ: Dict[str, Set[str]] = {n: set(scope.successors.get(n, set())) for n in nodes}

    # Predecessors for indeg
    for n in nodes:
        preds = scope.predecessors.get(n, set())
        preds_in_scope = {p for p in preds if p in node_set}
        indeg[n] = len(preds_in_scope)

    # Stable “UI index” fallback: original sibling appearance in this scope
    ui_index: Dict[str, int] = {n: i for i, n in enumerate(nodes)}

    ready: List[str] = [n for n in nodes if indeg[n] == 0]
    ordered: List[str] = []
    tie_notes: List[str] = []

    # Use a loop akin to Kahn’s algorithm
    while ready:
        # Choose “best” candidate among all ready nodes
        if len(ready) > 1:
            tie_notes.append(
                f"Scope '{scope.scope_name}': tie among {len(ready)} ready nodes; applying tie-break rules."
            )

        best = min(
            ready,
            key=lambda n: _tie_break_key(model=model, node_id=n, ui_index=ui_index),
        )
        ready.remove(best)
        ordered.append(best)

        # Reduce indeg of successors
        for nxt in sorted(succ.get(best, set()), key=str.lower):
            if nxt not in indeg:
                continue
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)

    # If we didn't place everything, we likely had a cycle or missing edge extraction.
    if len(ordered) != len(nodes):
        missing = [n for n in nodes if n not in ordered]
        tie_notes.append(
            f"WARNING: Scope '{scope.scope_name}': ordering incomplete; missing {len(missing)} nodes "
            f"(cycle or unresolved deps). Appending remaining nodes in stable UI order."
        )
        missing.sort(key=lambda n: ui_index.get(n, 10**9))
        ordered.extend(missing)

    return ordered, tie_notes


def _tie_break_key(
    *,
    model: Model,
    node_id: str,
    ui_index: Dict[str, int],
) -> Tuple[int, int, int, str]:
    """
    Tie-break priority tuple.

    Lower values sort earlier.

    Priority order (designer-ish):
      1) Branch group ordering:
         - false/default first (0)
         - cases next (1)
         - true last (2)
      2) Fail-ish actions before success-ish actions
      3) Stable UI index within this scope
      4) Lexical node_id as final stabilizer

    Returns:
        Tuple usable as a sorting key.
    """
    node = model.nodes.get(node_id)
    if node is None:
        return (9, 9, ui_index.get(node_id, 10**9), node_id.lower())

    branch_pri = _branch_group_priority(node)
    fail_pri = _failish_priority(node)
    ui_pri = ui_index.get(node_id, 10**9)

    return (branch_pri, fail_pri, ui_pri, node_id.lower())


def _branch_group_priority(node: Node) -> int:
    """
    Negative/default-first branch ordering.
    """
    if node.kind != NodeKind().BRANCH_GROUP:
        return 5

    label = (node.branch_label or "").lower()

    # Condition branches
    if label == "false":
        return 0
    if label == "true":
        return 2

    # Switch branches
    if label == "default":
        return 0
    if label.startswith("case:"):
        return 1

    return 3


def _failish_priority(node: Node) -> int:
    """
    Heuristic: actions/scopes named with 'fail' should appear earlier when ties exist.

    This is intentionally simple for Phase 3. We can refine later using:
    - runAfter state lists (Failed/TimedOut) once we plumb them into ScopeInfo edges.
    """
    name = (node.display_name or node.action_name or "").lower()
    if "fail" in name or "failed" in name:
        return 0
    return 1
