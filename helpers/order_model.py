# helpers/order_model.py
"""
Designer-faithful ordering: node model + graph extraction (Phase 1).

This module builds a collapsed, container-aware representation of a Logic Apps
workflow definition. It does NOT compute ordering yet; it only extracts nodes
and parent/child relationships in a way that later phases can consume.

Key ideas:
- Containers are first-class nodes (Scope, Foreach, Condition, Switch, etc.)
- Branch/case groupings are represented as "branch groups" with child lists.
- Action nodes preserve raw runAfter and other metadata needed for ordering.

Later modules should:
- Resolve entry/exit nodes per scope (order_exits.py)
- Compute exit-first (reverse) ordering per scope (order_compute.py)
- Emit debug artifacts (order_debug.py)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------

JsonDict = Dict[str, Any]


# -----------------------------------------------------------------------------
# Node kinds
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeKind:
    """
    A lightweight "enum-ish" holder for node kind strings.

    We keep this simple (instead of Enum) to reduce import friction across
    helper modules while we're iterating.
    """

    STEP: str = "step"                    # A normal action (Compose, HTTP, etc.)
    CONTAINER: str = "container"          # Scope/Foreach container action
    CONDITION: str = "condition"          # If/Condition action
    SWITCH: str = "switch"                # Switch action
    BRANCH_GROUP: str = "branch_group"    # True/False/Default/Case grouping node


# -----------------------------------------------------------------------------
# Node model
# -----------------------------------------------------------------------------

@dataclass
class Node:
    """
    Represents either a real Logic Apps action OR a synthetic grouping node.

    For real actions:
      - action_name: the key under "actions" (e.g. "Get_Token")
      - display_name: usually derived from action_name (humanized elsewhere)
      - action_type: raw "type" from the action definition
      - run_after: raw runAfter dict (can be empty)

    For synthetic nodes (branch groups):
      - action_name is a generated id
      - action_type may be None
      - run_after typically empty; ordering is driven by parent container rules
    """

    node_id: str
    action_name: str
    display_name: str
    kind: str

    # Raw Logic Apps metadata
    action_type: Optional[str] = None
    run_after: Dict[str, Any] = field(default_factory=dict)
    raw_action: Optional[JsonDict] = None

    # Hierarchy
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)

    # Branch/case labeling (only meaningful for branch groups)
    branch_label: Optional[str] = None
    branch_order_hint: Optional[int] = None

    # Debug breadcrumbs
    source_path: Optional[str] = None   # e.g., "root.actions.X" or "...if.true.actions.Y"


# -----------------------------------------------------------------------------
# Model container
# -----------------------------------------------------------------------------

@dataclass
class Model:
    """
    Full extracted model from a Logic Apps definition.

    - nodes: all nodes (real + synthetic)
    - root_children: top-level nodes under the workflow root
    - errors/warnings: extraction-time notes
    """

    nodes: Dict[str, Node] = field(default_factory=dict)
    root_children: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def build_model(
    flow_def: JsonDict,
) -> Model:
    """
    Build a collapsed node model from a Logic Apps workflow definition.

    Args:
        flow_def:
            Parsed JSON for the workflow definition root (the object containing
            "actions", and typically "triggers" elsewhere).

    Returns:
        Model:
            Collapsed, container-aware model of nodes and parent-child edges.
    """
    model = Model()

    # In Logic Apps export shapes, "actions" may live at:
    # - flow_def["actions"] (common)
    # - flow_def["definition"]["actions"] (in ARM templates)
    actions = _find_actions_dict(flow_def)
    if actions is None:
        model.warnings.append("No 'actions' found in flow definition.")
        return model

    _extract_actions_into_parent(
        model=model,
        actions_dict=actions,
        parent_id=None,
        source_path="root.actions",
        root=True,
    )

    return model


def get_node(
    model: Model,
    node_id: str,
) -> Node:
    """
    Convenience accessor with a clearer failure mode during development.
    """
    try:
        return model.nodes[node_id]
    except KeyError as exc:
        raise KeyError(f"Node id not found: {node_id}") from exc


# -----------------------------------------------------------------------------
# Extraction helpers
# -----------------------------------------------------------------------------

def _find_actions_dict(
    flow_def: JsonDict,
) -> Optional[JsonDict]:
    """
    Locate the top-level actions dict regardless of wrapper shape.
    """
    if isinstance(flow_def.get("actions"), dict):
        return flow_def["actions"]

    definition = flow_def.get("definition")
    if isinstance(definition, dict) and isinstance(definition.get("actions"), dict):
        return definition["actions"]

    return None


def _extract_actions_into_parent(
    *,
    model: Model,
    actions_dict: JsonDict,
    parent_id: Optional[str],
    source_path: str,
    root: bool,
) -> None:
    """
    Extract a dict of Logic Apps actions into the model under a given parent node.
    """
    for action_name, action in actions_dict.items():
        node_id = _node_id_for_action(parent_id, action_name)
        display_name = _display_name_from_action_name(action_name)
        action_type = action.get("type")
        run_after = action.get("runAfter") or {}

        kind = _kind_from_action_type(action_type)

        node = Node(
            node_id=node_id,
            action_name=action_name,
            display_name=display_name,
            kind=kind,
            action_type=action_type,
            run_after=run_after,
            raw_action=action,
            parent_id=parent_id,
            source_path=f"{source_path}.{action_name}",
        )
        model.nodes[node_id] = node

        if root:
            model.root_children.append(node_id)
        elif parent_id is not None:
            model.nodes[parent_id].children_ids.append(node_id)

        # If this action contains nested actions, extract them as children.
        _extract_children_if_container(
            model=model,
            parent_node=node,
        )


def _extract_children_if_container(
    *,
    model: Model,
    parent_node: Node,
) -> None:
    """
    If parent_node is a container-like action, extract its internal structure.

    We create synthetic BRANCH_GROUP nodes for:
    - Condition: "true" and "false"
    - Switch: "cases.<caseKey>" and "default"
    - Foreach/Scope: direct "actions" children (no branch groups)
    """
    action = parent_node.raw_action or {}
    action_type = (parent_node.action_type or "").lower()

    # Scope / Foreach / Until / etc often have: {"actions": {...}}
    direct_actions = action.get("actions")
    if isinstance(direct_actions, dict) and action_type in {"scope", "foreach", "until"}:
        _extract_actions_into_parent(
            model=model,
            actions_dict=direct_actions,
            parent_id=parent_node.node_id,
            source_path=f"{parent_node.source_path}.actions",
            root=False,
        )
        return

    # Condition (If/Condition): typically "actions" for true and "else": {"actions": ...}
    if action_type in {"if", "condition"}:
        true_actions = action.get("actions")
        false_actions = None
        else_obj = action.get("else")
        if isinstance(else_obj, dict):
            false_actions = else_obj.get("actions")

        true_group_id = _add_branch_group(
            model=model,
            container_node=parent_node,
            label="true",
            order_hint=1,
            source_path=f"{parent_node.source_path}.actions",
        )
        if isinstance(true_actions, dict):
            _extract_actions_into_parent(
                model=model,
                actions_dict=true_actions,
                parent_id=true_group_id,
                source_path=f"{parent_node.source_path}.actions",
                root=False,
            )

        false_group_id = _add_branch_group(
            model=model,
            container_node=parent_node,
            label="false",
            order_hint=0,
            source_path=f"{parent_node.source_path}.else.actions",
        )
        if isinstance(false_actions, dict):
            _extract_actions_into_parent(
                model=model,
                actions_dict=false_actions,
                parent_id=false_group_id,
                source_path=f"{parent_node.source_path}.else.actions",
                root=False,
            )
        return

    # Switch: typically "cases": { "German": { "actions": {...}}, ... }, plus "default": {"actions": {...}}
    if action_type == "switch":
        cases_obj = action.get("cases")
        default_obj = action.get("default")

        # Default first (negative-first rule later can use order_hint)
        default_group_id = _add_branch_group(
            model=model,
            container_node=parent_node,
            label="default",
            order_hint=0,
            source_path=f"{parent_node.source_path}.default.actions",
        )
        if isinstance(default_obj, dict) and isinstance(default_obj.get("actions"), dict):
            _extract_actions_into_parent(
                model=model,
                actions_dict=default_obj["actions"],
                parent_id=default_group_id,
                source_path=f"{parent_node.source_path}.default.actions",
                root=False,
            )

        # Preserve case order as it appears in JSON for now (we’ll refine later).
        if isinstance(cases_obj, dict):
            for idx, (case_key, case_body) in enumerate(cases_obj.items(), start=1):
                case_group_id = _add_branch_group(
                    model=model,
                    container_node=parent_node,
                    label=f"case:{case_key}",
                    order_hint=idx,
                    source_path=f"{parent_node.source_path}.cases.{case_key}.actions",
                )
                if isinstance(case_body, dict) and isinstance(case_body.get("actions"), dict):
                    _extract_actions_into_parent(
                        model=model,
                        actions_dict=case_body["actions"],
                        parent_id=case_group_id,
                        source_path=f"{parent_node.source_path}.cases.{case_key}.actions",
                        root=False,
                    )
        return

    # Other container-like types can be added later.


def _add_branch_group(
    *,
    model: Model,
    container_node: Node,
    label: str,
    order_hint: int,
    source_path: str,
) -> str:
    """
    Create a synthetic BRANCH_GROUP node under a container node.
    """
    group_id = _node_id_for_branch_group(container_node.node_id, label)

    group_node = Node(
        node_id=group_id,
        action_name=group_id,
        display_name=_display_name_from_branch_label(label),
        kind=NodeKind().BRANCH_GROUP,
        action_type=None,
        run_after={},
        raw_action=None,
        parent_id=container_node.node_id,
        children_ids=[],
        branch_label=label,
        branch_order_hint=order_hint,
        source_path=source_path,
    )

    model.nodes[group_id] = group_node
    container_node.children_ids.append(group_id)
    return group_id


# -----------------------------------------------------------------------------
# Kind / naming utilities
# -----------------------------------------------------------------------------

def _kind_from_action_type(
    action_type: Optional[str],
) -> str:
    """
    Map Logic Apps action type to our coarse node kind.
    """
    if not action_type:
        return NodeKind().STEP

    at = action_type.lower()
    if at in {"scope", "foreach", "until"}:
        return NodeKind().CONTAINER
    if at in {"if", "condition"}:
        return NodeKind().CONDITION
    if at == "switch":
        return NodeKind().SWITCH

    return NodeKind().STEP


def _node_id_for_action(
    parent_id: Optional[str],
    action_name: str,
) -> str:
    """
    Stable node_id scheme for real actions.
    """
    if parent_id:
        return f"{parent_id}::{action_name}"
    return action_name


def _node_id_for_branch_group(
    container_node_id: str,
    label: str,
) -> str:
    """
    Stable node_id scheme for synthetic branch group nodes.
    """
    return f"{container_node_id}::branch::{label}"


def _display_name_from_action_name(
    action_name: str,
) -> str:
    """
    Default display name derivation for debug output.

    Note: your pipeline already has better "pretty titles"; this is just a safe
    fallback that works while we build the ordering engine.
    """
    return action_name.replace("_", " ")


def _display_name_from_branch_label(
    label: str,
) -> str:
    """
    Turn internal branch labels into something readable for debug.
    """
    if label.startswith("case:"):
        return label.replace("case:", "")
    return label.capitalize()
