#!/usr/bin/env python3
# helpers/classification.py
"""
Classification helpers for Stage 1.

Responsibilities:
- Load global rules.json(.jsonc) next to analyzer_config.
- Load per-flow __override_rules.json under the flow root.
- Merge rules and build the vitals "tree" structure with app-level categories.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from .filenames_config import get_override_rules_json_path

from .jsonc_config import strip_json_comments
from .definition_model import StepInfo
from .markdown_helpers_step import pretty_name, safe_slug


def load_global_rules(base_dir: Path, *, verbose: bool = False) -> Dict[str, Any]:
    """
    Load root-level rules.json / rules.jsonc located next to analyzer_config.

    Behaviour:
      - Looks for rules.jsonc first, then rules.json, under base_dir.
      - Supports JSONC (comments) using strip_json_comments.
      - Returns an empty dict when no rules file is present.
      - Performs minimal schema validation on categories/matchRules/stepTags.
    """
    candidates = ["rules.jsonc", "rules.json"]
    rules: Dict[str, Any] = {}

    chosen: Optional[Path] = None
    for name in candidates:
        candidate = base_dir / name
        if candidate.is_file():
            chosen = candidate
            break

    if not chosen:
        if verbose:
            print(
                f"INFO: No global rules.json found under {base_dir}; "
                "classification defaults will be empty for this run.",
                file=sys.stderr,
            )
        return rules

    try:
        raw_text = chosen.read_text(encoding="utf-8")
        cleaned = strip_json_comments(raw_text)
        loaded = json.loads(cleaned)
    except Exception as exc:
        print(
            f"WARNING: Failed to load rules from {chosen}: {exc}. "
            "Ignoring rules for this run.",
            file=sys.stderr,
        )
        return rules

    if not isinstance(loaded, dict):
        print(
            f"WARNING: Global rules file {chosen} did not contain a JSON object; "
            "ignoring rules for this run.",
            file=sys.stderr,
        )
        return rules

    categories = loaded.get("categories")
    if categories is not None and not isinstance(categories, dict):
        print(
            f"WARNING: rules.json categories must be an object; "
            f"got {type(categories).__name__}. Ignoring categories.",
            file=sys.stderr,
        )
        loaded["categories"] = {}

    match_rules = loaded.get("matchRules")
    if match_rules is not None and not isinstance(match_rules, dict):
        print(
            f"WARNING: rules.json matchRules must be an object; "
            f"got {type(match_rules).__name__}. Ignoring matchRules.",
            file=sys.stderr,
        )
        loaded["matchRules"] = {}

    step_tags = loaded.get("stepTags")
    if step_tags is not None and not isinstance(step_tags, dict):
        print(
            f"WARNING: rules.json stepTags must be an object; "
            f"got {type(step_tags).__name__}. Ignoring stepTags.",
            file=sys.stderr,
        )
        loaded["stepTags"] = {}

    return loaded


def load_override_rules(flow_root: Path, *, verbose: bool = False) -> Dict[str, Any]:
    """
    Load or initialize per-flow override rules file under the given flow root
    directory.

    The effective filename is controlled by the OVERRIDE_RULES_JSON label in
    filenames.json (default: `override_rules.json`).
    """
    # Resolve via filenames_config so OVERRIDE_RULES_JSON overrides are honored.
    override_path = get_override_rules_json_path(flow_root)
    rules: Dict[str, Any] = {}

    if override_path.is_file():
        try:
            raw_text = override_path.read_text(encoding="utf-8")
            cleaned = strip_json_comments(raw_text)
            loaded = json.loads(cleaned)
        except Exception as exc:
            print(
                f"WARNING: Failed to load override rules from {override_path}: {exc}. "
                "Ignoring overrides for this run.",
                file=sys.stderr,
            )
            return rules

        if not isinstance(loaded, dict):
            print(
                f"WARNING: Override rules file {override_path} did not contain a JSON object; "
                "ignoring overrides for this run.",
                file=sys.stderr,
            )
            return rules

        categories = loaded.get("categories")
        if categories is not None and not isinstance(categories, dict):
            print(
                f"WARNING: __override_rules.json categories must be an object; "
                f"got {type(categories).__name__}. Ignoring categories.",
                file=sys.stderr,
            )
            loaded["categories"] = {}

        match_rules = loaded.get("matchRules")
        if match_rules is not None and not isinstance(match_rules, dict):
            print(
                f"WARNING: __override_rules.json matchRules must be an object; "
                f"got {type(match_rules).__name__}. Ignoring matchRules.",
                file=sys.stderr,
            )
            loaded["matchRules"] = {}

        step_tags = loaded.get("stepTags")
        if step_tags is not None and not isinstance(step_tags, dict):
            print(
                f"WARNING: __override_rules.json stepTags must be an object; "
                f"got {type(step_tags).__name__}. Ignoring stepTags.",
                file=sys.stderr,
            )
            loaded["stepTags"] = {}

        return loaded

    skeleton: Dict[str, Any] = {
        "categories": {},
        "matchRules": {
            "stepNameStartsWith": {},
        },
        "stepTags": {},
    }

    try:
        override_path.write_text(
            json.dumps(skeleton, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if verbose:
            print(
                f"INFO: Created per-flow override rules skeleton at {override_path}",
                file=sys.stderr,
            )
        return skeleton
    except Exception as exc:
        print(
            f"WARNING: Failed to create __override_rules.json at {override_path}: {exc}. "
            "Override rules will be unavailable for this run.",
            file=sys.stderr,
        )
        return rules


def build_vitals_tree(
    registry: Dict[str, StepInfo],
    order: List[str],
    rules: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build the nested vitals tree structure for _vitals.json, including
    app-level categories derived from rules.
    """
    match_rules: Dict[str, Any] = {}
    step_name_startswith: Dict[str, Any] = {}
    step_tags: Dict[str, Any] = {}

    if isinstance(rules, dict):
        match_rules = rules.get("matchRules") or {}
        if isinstance(match_rules, dict):
            sns = match_rules.get("stepNameStartsWith") or {}
            if isinstance(sns, dict):
                step_name_startswith = sns
        st = rules.get("stepTags") or {}
        if isinstance(st, dict):
            step_tags = st

    def classify_step(step_name: str, step_info: StepInfo) -> Optional[str]:
        """
        Return the app_category for this step based on rules.

        Precedence:
          1) Explicit per-step category in stepTags[slug].category.
          2) Name-based rules from matchRules.stepNameStartsWith.
          3) Otherwise, no category.
        """
        slug = safe_slug(step_name)
        tag_entry = step_tags.get(slug)

        if isinstance(tag_entry, dict):
            category = tag_entry.get("category")
            if isinstance(category, str) and category.strip():
                return category.strip()

        pretty = getattr(step_info, "pretty_name", "") or pretty_name(step_name)
        if step_name_startswith:
            for category_name, prefixes in step_name_startswith.items():
                if not isinstance(prefixes, list):
                    continue
                for prefix in prefixes:
                    if not isinstance(prefix, str):
                        continue
                    if pretty.startswith(prefix):
                        return str(category_name)

        return None

    def build_node(step_name: str) -> Dict[str, Any]:
        step = registry[step_name]
        code_type = step.atype
        matched_type = getattr(step, "atype_display", code_type) or code_type
        app_category = classify_step(step_name, step) or ""

        node: Dict[str, Any] = {
            "step": {
                "code_name": step_name,
                "code_type": code_type,
                "code_category": code_type,  # legacy alias
                "matched_type": matched_type,
                "pretty_name": getattr(step, "pretty_name", "") or pretty_name(step_name),

                # Designer group from catalog (Control, Data Operations, …)
                "pretty_plugin_category": getattr(step, "pretty_category", "") or "",

                # Per-step type from catalog (Condition, Create CSV table, HTTP, …)
                "pretty_type": getattr(step, "pretty_type", "") or "",

                # App-level category from rules / overrides (logging, security, …)
                "pretty_app_category": app_category or "",

                "is_container": bool(getattr(step, "is_container", False)),
            },
            "children": [],
        }

        if getattr(step, "is_container", False):
            children = [name for name in order if registry[name].parent == step_name]
            for child in children:
                node["children"].append(build_node(child))

        return node

    # Top-level nodes: all steps whose parent is None
    roots = [name for name in order if registry[name].parent is None]

    # Build a node for every root; children will be recursively included
    nodes = [build_node(root_name) for root_name in roots]

    # Sanity check: ensure every step appears exactly once in the final tree
    # by verifying that all `order` entries were consumed by the recursive builder.
    seen: set[str] = set()

    def walk(node: Dict[str, Any]) -> None:
        step_block = node.get("step") or {}
        code_name = step_block.get("code_name")
        if code_name:
            seen.add(code_name)
        for c in node.get("children") or []:
            walk(c)

    for n in nodes:
        walk(n)

    missing = [s for s in order if s not in seen]
    if missing:
        raise SystemExit(
            f"ERROR: build_vitals_tree failed to include steps: {missing}. "
            "Check parent/child relationships in StepInfo."
        )

    return {
        "creation_order": order,
        "nodes": nodes,
    }
