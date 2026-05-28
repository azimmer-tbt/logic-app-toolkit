#!/usr/bin/env python3
# helpers/creation_order.py
"""
Creation-order helpers for Logic App steps.

This module centralises:
- top-level trigger run order (master run order)
- per-step creation order (designer-order traversal)
- container/child relationships for "creation order" narratives
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .definition_model import StepInfo


# ------------------------------------------------------------------------------
# Top-level trigger run order
# ------------------------------------------------------------------------------


def _collect_top_level_actions(defn: Dict[str, Any]) -> Dict[str, Any]:
    acts = defn.get("actions", {}) or {}
    return {k: v for k, v in acts.items() if isinstance(v, dict)}


def _top_level_edges(actions: Dict[str, Any]) -> Tuple[Dict[str, int], Dict[str, Set[str]]]:
    names = set(actions.keys())
    incoming: Dict[str, int] = {n: 0 for n in names}
    outgoing: Dict[str, Set[str]] = {n: set() for n in names}

    for name, action in actions.items():
        run_after = action.get("runAfter", {}) or {}
        for pred in run_after.keys():
            if pred in names:
                outgoing[pred].add(name)
                incoming[name] += 1

    return incoming, outgoing


def _step_type_priority(step_type: str) -> int:
    """
    Give InitializeVariable steps priority zero so they are suggested first when
    multiple starts are available. All other types default to priority one.
    """
    return 0 if (step_type or "").strip().lower() == "initializevariable" else 1


def _kahn_with_tiebreak(
    actions: Dict[str, Any],
    incoming: Dict[str, int],
    outgoing: Dict[str, Set[str]],
) -> List[str]:
    """
    Use Kahn's algorithm for a stable topological sort, breaking ties by:

    1. Type priority (InitializeVariable first),
    2. Designer name (case-insensitive).
    """
    import heapq

    ready: List[Tuple[int, str, str]] = []
    for name, indegree in incoming.items():
        if indegree == 0:
            step_type = (actions.get(name, {}).get("type") or "")
            heapq.heappush(ready, (_step_type_priority(step_type), name.lower(), name))

    order: List[str] = []
    while ready:
        _, _, name = heapq.heappop(ready)
        order.append(name)

        for succ in sorted(outgoing.get(name, []), key=str.lower):
            incoming[succ] -= 1
            if incoming[succ] == 0:
                step_type = (actions.get(succ, {}).get("type") or "")
                heapq.heappush(ready, (_step_type_priority(step_type), succ.lower(), succ))

    if len(order) < len(actions):
        remaining = sorted([n for n in actions if n not in order], key=str.lower)
        order.extend(remaining)

    return order


def compute_master_run_order(defn: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Compute a recommended top-level run order for actions plus a list of
    "parallel start" actions that have no prerequisites.
    """
    actions = _collect_top_level_actions(defn)
    incoming, outgoing = _top_level_edges(actions)
    parallel_starts = sorted([n for n, deg in incoming.items() if deg == 0], key=str.lower)
    order = _kahn_with_tiebreak(actions, incoming, outgoing)
    return order, parallel_starts


# ------------------------------------------------------------------------------
# Per-step creation order + container markers
# ------------------------------------------------------------------------------


TYPE_RANK: Dict[str, int] = {
    "InitializeVariable": 0,
    "ParseJson": 1,
    "Select": 1,
    "Response": 1,
    "Http": 2,
    "ApiConnection": 2,
    "Foreach": 3,
    "If": 3,
    "Switch": 3,
    "Scope": 4,
}


def type_rank(step_type: str) -> int:
    return TYPE_RANK.get(step_type, 5)


def topo_creation_order(
    registry: Dict[str, StepInfo],
    *,
    verbose: bool = False,
) -> Tuple[List[str], List[str]]:
    """
    STRICT DESIGNER-ORDER CREATION SEQUENCE (Option A1)

    Logic Apps define step order by the JSON order in which steps appear inside
    their parent containers. This order is preserved by definition_model's walk
    because Python dicts preserve insertion order.

    Therefore:
      • We do NOT perform a topological sort.
      • We do NOT reorder based on runAfter.
      • We do NOT apply alphabetical fallback.
      • We TRUST registry iteration order as authoritative designer order.
    """
    order = list(registry.keys())
    return order, []


def children_of(container: str, registry: Dict[str, StepInfo]) -> List[str]:
    """Return the names of steps whose parent is the given container name."""
    return [name for name, step in registry.items() if step.parent == container]


def container_creation_markers(
    order: List[str],
    registry: Dict[str, StepInfo],
) -> List[Tuple[str, Optional[Tuple[str, str]]]]:
    """
    Return an interleaved sequence of:
      ('STEP', step_name)
      ('CONTAINER', (container_name, container_type))
    where each container marker appears just after the first child inside it.
    """
    first_index: Dict[str, int] = {}
    for container_name, step in registry.items():
        if not getattr(step, "is_container", False):
            continue
        kids = children_of(container_name, registry)
        indices = [order.index(k) for k in kids if k in order]
        if indices:
            first_index[container_name] = min(indices)

    result: List[Tuple[str, Optional[Tuple[str, str]]]] = []
    inserted: Set[str] = set()
    for i, step_name in enumerate(order):
        result.append(("STEP", step_name))
        for container_name, idx in first_index.items():
            if container_name in inserted:
                continue
            if idx == i:
                result.append(("CONTAINER", (container_name, registry[container_name].atype)))
                inserted.add(container_name)

    return result
