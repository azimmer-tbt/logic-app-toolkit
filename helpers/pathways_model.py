#!/usr/bin/env python3
# pathways_model.py
"""
Pathways & flow classification model for Logic App Documentation Toolkit.

This module is responsible for building a per-flow `pathways.json` snapshot
from the Analyzer's registry and vitals plus the merged rules configuration.

High-level responsibilities (mapped to the spec):

* Load any existing `pathways.json` snapshot and per-flow
  `pathways_overrides.json` if present.
* Use the merged rules (global + per-flow) to classify each step into:
    - category (e.g. preparation, core_operations, logging_succeed, logging_fail)
    - lane (e.g. prep, core, log_success, log_fail, trigger, other)
    - is_green_candidate / is_red_candidate based on the configured buckets.
* Respect per-flow step overrides and previously configured `override_lane`.
* Maintain `succeed_steps`, `fail_steps`, and `unmatched` collections.
* Compute simple path sequences (primary green path plus basic red branches).
* Write the updated snapshot back to `<flow_root>/pathways.json`.

The goal is to keep all "pathways brain" logic here so that
`1_analyzer.py` remains focused on reading the flow and building core
metadata.
"""

from helpers.filenames_config import (
    get_pathways_json_path,
    get_pathways_overrides_json_path,
)

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    # StepInfo is defined in the helpers package; we only need it for hints.
    from helpers.definition_model import StepInfo  # noqa: F401

# Type aliases
JSONDict = Dict[str, Any]
StepRegistry = Mapping[str, Any]  # effectively Mapping[str, StepInfo]


# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------


def build_and_write_pathways_snapshot(
    *,
    flow_root: Path,
    reg: StepRegistry,
    vitals: MutableMapping[str, Any],
    rules: Mapping[str, Any],
    source_md5: str,
    conservative_pathing: bool = False,
) -> Optional[Path]:
    """
    Build or update a per-flow pathways snapshot and write it to disk.

    Parameters
    ----------
    flow_root:
        Output directory for the current flow (Analyzer's `out_dir`).
    reg:
        Registry of steps built by Analyzer. Keys are step identifiers
        (usually the Logic App "code name") and values are StepInfo objects.
    vitals:
        The vitals root dictionary that Analyzer is preparing to write.
        Must include at least `source_md5` and `creation_order`.
    rules:
        Merged rules configuration (global + per-flow). This is expected
        to include the categories, match_rules, succeed_path, fail_path,
        and step_tags sections described in the Pathways spec, but this
        function is tolerant of missing keys.
    source_md5:
        MD5 hash of the raw flow definition JSON for the current run.

    Returns
    -------
    Optional[Path]
        The path to the written `pathways.json` file, or None if the
        snapshot could not be written for any reason.
    """
    try:
        flow_root_path = Path(flow_root)
    except TypeError:
        # If something bizarre is passed, fail soft and let Analyzer continue.
        return None

    # Use label-based filename resolution so filenames.json controls these.
    pathways_path = get_pathways_json_path(flow_root_path)
    overrides_path = get_pathways_overrides_json_path(flow_root_path)

    # Load any existing snapshot and per-flow overrides. Failures are treated
    # as "no previous data" rather than fatal errors.
    previous_snapshot = _load_json_if_exists(pathways_path)
    overrides = _load_json_if_exists(overrides_path) or {}

    previous_steps = (previous_snapshot or {}).get("steps", {})
    previous_unmatched = (previous_snapshot or {}).get("unmatched", {})
    previous_source_md5 = (previous_snapshot or {}).get("source_md5")

    # Determine whether we should prune stale steps from the previous snapshot.
    md5_changed = bool(previous_snapshot) and previous_source_md5 != source_md5

    # Build fresh step classification for all current steps.
    current_slugs = list(reg.keys())
    steps: JSONDict = {}

    # Precompute succeed/fail bucket configuration from rules.
    succeed_bucket = set(_as_string_list(rules.get("succeed_path", [])))
    fail_bucket = set(_as_string_list(rules.get("fail_path", [])))

    match_rules = rules.get("match_rules", {}) or {}
    step_tags = rules.get("step_tags", {}) or {}

    # Per-flow overrides, if any.
    step_overrides = (overrides.get("step_overrides") or {}) if isinstance(overrides, dict) else {}
    branch_overrides = (overrides.get("branch_overrides") or {}) if isinstance(overrides, dict) else {}
    path_overrides = (overrides.get("path_overrides") or {}) if isinstance(overrides, dict) else {}

    for slug in current_slugs:
        step_info = reg.get(slug)
        previous_record = previous_steps.get(slug, {})
        previous_unmatched_entry = previous_unmatched.get(slug, {})

        # Determine classification for this step.
        step_record = _classify_step(
            slug=slug,
            step_info=step_info,
            rules=rules,
            match_rules=match_rules,
            step_tags=step_tags,
            succeed_bucket=succeed_bucket,
            fail_bucket=fail_bucket,
            step_override=step_overrides.get(slug, {}),
            previous_record=previous_record,
            previous_unmatched_entry=previous_unmatched_entry,
        )
        steps[slug] = step_record

    # Apply container-based inheritance for clearly success/fail containers
    # (for example, "Success - ..." and "Fail - ..." scopes that have been
    # classified as logging_succeed / logging_fail). This allows descendants
    # to inherit an `inherit_role` of "success" or "fail" and adjusts their
    # candidacy and lane when they do not already have explicit overrides.
    _apply_container_inheritance(steps=steps, vitals=vitals)

    # Apply branch-level inheritance for conditional/switch steps using
    # per-flow branch_overrides (e.g. true/false branches or named switch
    # cases such as Mac/Mobile). This uses the same inherit_role/path_role
    # semantics as container inheritance but operates on branch subtrees.
    _apply_branch_inheritance(steps=steps, vitals=vitals, branch_overrides=branch_overrides)

    # Build flat succeed/fail sets from the final per-step records.
    succeed_steps, fail_steps = _build_succeed_fail_sets(steps)
    unmatched = _build_unmatched(
        steps=steps,
        succeed_steps=succeed_steps,
        fail_steps=fail_steps,
        previous_unmatched=(previous_unmatched if not md5_changed else {}),
    )

    # Compute paths (primary/alt green, red branches) using a simple heuristic
    # over creation_order and succeed/fail candidacy.
    paths: JSONDict = _build_paths(
        vitals=vitals,
        steps=steps,
        succeed_steps=succeed_steps,
        fail_steps=fail_steps,
        reg=reg,
        conservative_pathing=conservative_pathing,
        path_overrides=path_overrides,
    )

    # ------------------------------------------------------------------
    # Spec B — Three-lane designer-faithful pathways table (authoritative)
    #
    # Preferred source for execution ordering is the per-flow flow_model.json
    # (it contains execution_order + execution_steps + node run_after).
    # We fall back to vitals ordering keys if flow_model.json is missing.
    # ------------------------------------------------------------------

    three_lane_table: JSONDict = _build_three_lane_table_view(
        flow_root=flow_root_path,
        reg=reg,
        steps=steps,
        vitals=vitals,
    )
    # If we had a previous snapshot and the MD5 changed, prune any lingering
    # references to steps that no longer exist.
    if previous_snapshot and md5_changed:
        paths = _prune_paths(
            paths=(previous_snapshot.get("paths") or paths),
            valid_slugs=set(steps.keys()),
        )

    snapshot: JSONDict = {
        "version": "2025-11-30",
        "source_flow": str(vitals.get("flow_name", "")),
        "source_md5": source_md5,
        "steps": steps,
        "succeed_steps": sorted(succeed_steps),
        "fail_steps": sorted(fail_steps),
        "unmatched": unmatched,
        "paths": paths,
        "views": {
            "three_lane_table": three_lane_table,
        },
    }

    try:
        pathways_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Fail soft; Analyzer will continue without pathways metadata.
        return None

    # If no per-flow overrides file exists yet, create a skeleton to make it
    # easier for users to discover valid knobs without having to inspect
    # vitals/pathways by hand.
    if not overrides_path.exists():
        try:
            # Pre-populate branch_overrides with one entry per known conditional
            # in vitals["branches"], leaving the inner branches map empty so
            # users can fill in inherit_role/path_role for each branch_id.
            branches_info = vitals.get("branches") or {}
            branch_overrides_skeleton: JSONDict = {}
            if isinstance(branches_info, Mapping):
                for conditional_slug in branches_info.keys():
                    branch_overrides_skeleton[str(conditional_slug)] = {"branches": {}}

            # Pre-populate path_overrides with one entry per currently known
            # path, copying label/kind but leaving steps empty so the user can
            # explicitly spell out sequences if they choose.
            path_overrides_skeleton: JSONDict = {}
            if isinstance(paths, Mapping):
                for name, info in paths.items():
                    if not isinstance(info, Mapping):
                        continue
                    entry: JSONDict = {"steps": []}
                    if "label" in info:
                        entry["label"] = info.get("label")
                    if "kind" in info:
                        entry["kind"] = info.get("kind")
                    path_overrides_skeleton[str(name)] = entry

            overrides_skeleton: JSONDict = {
                "step_overrides": {},
                "branch_overrides": branch_overrides_skeleton,
                "path_overrides": path_overrides_skeleton,
            }

            overrides_path.write_text(
                json.dumps(overrides_skeleton, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Fail soft if we cannot create the overrides skeleton; the
            # absence of this file should never block Analyzer.
            pass

    return pathways_path


# ------------------------------------------------------------------------------
# Internal helpers – JSON loading and simple utilities
# ------------------------------------------------------------------------------


def _load_json_if_exists(path: Path) -> Optional[JSONDict]:
    """
    Load JSON data from a file if it exists.

    Returns the parsed dict, or None if the file does not exist or cannot
    be parsed.
    """
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _as_string_list(value: Any) -> List[str]:
    """
    Normalize a value into a list of strings.

    - None -> []
    - str  -> [str]
    - Iterable of scalars -> [str(x) for x in value]
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def _safe_lower(s: Optional[str]) -> str:
    """
    Return a lowercased string, tolerating None.
    """
    if s is None:
        return ""
    return str(s).lower()


def _get_step_metadata(step_info: Any) -> JSONDict:
    """
    Extract a minimal, generic metadata view from a StepInfo-like object.

    Because this helper lives in a shared library and we want to avoid tight
    coupling to the exact StepInfo implementation, we use a tolerant approach:
    - We attempt several common attribute names for engine type, subtype, and
      friendly name.
    - If attributes are missing, we fall back to empty strings.
    """
    if step_info is None:
        return {
            "engine_type": "",
            "subtype": "",
            "pretty_name": "",
        }

    # Engine type / internal step type
    engine_type = getattr(step_info, "atype", None)
    if engine_type is None:
        engine_type = getattr(step_info, "engine_type", None)
    if engine_type is None:
        engine_type = getattr(step_info, "type", "")

    # Catalog subtype / plugin identifier
    subtype = getattr(step_info, "subtype", None)
    if subtype is None:
        subtype = getattr(step_info, "atype_display", None)
    if subtype is None:
        subtype = getattr(step_info, "kind", "")

    # Friendly / pretty name for name-based matching
    pretty_name = getattr(step_info, "pretty_name", None)
    if pretty_name is None:
        pretty_name = getattr(step_info, "display_name", None)
    if pretty_name is None:
        pretty_name = getattr(step_info, "name", "")

    return {
        "engine_type": str(engine_type or ""),
        "subtype": str(subtype or ""),
        "pretty_name": str(pretty_name or ""),
    }


# ------------------------------------------------------------------------------
# Internal helpers – classification and buckets
# ------------------------------------------------------------------------------


def _classify_step(
    *,
    slug: str,
    step_info: Any,
    rules: Mapping[str, Any],
    match_rules: Mapping[str, Any],
    step_tags: Mapping[str, Any],
    succeed_bucket: Iterable[str],
    fail_bucket: Iterable[str],
    step_override: Mapping[str, Any],
    previous_record: Mapping[str, Any],
    previous_unmatched_entry: Mapping[str, Any],
) -> JSONDict:
    """
    Classify a single step into category, lane, and succeed/fail candidacy.

    This function implements the precedence rules from the Pathways spec:

    1. Per-step tags in `step_tags[slug].category` (global overrides).
    2. Type- and subtype-based rules in `match_rules.step_type_equals` and
       `match_rules.step_subtype_equals`.
    3. Name-based rules in `match_rules.step_name_starts_with` and
       `match_rules.step_name_contains_all`.
    4. Fallback to `"unknown"` if nothing matched.

    It also:
    - Maps category to a lane.
    - Marks `is_green_candidate` / `is_red_candidate` based on the succeed /
      fail buckets.
    - Applies per-flow step overrides from `step_override`.
    - Applies any previously configured `override_lane` from the unmatched
      entry, if the step still exists.
    """
    meta = _get_step_metadata(step_info)
    engine_type = meta["engine_type"]
    subtype = meta["subtype"]
    pretty_name = meta["pretty_name"]

    # 1. Start with previous record to carry forward any unknown fields.
    record: JSONDict = dict(previous_record) if previous_record else {}
    record["slug"] = slug
    record["engine_type"] = engine_type
    record["subtype"] = subtype
    record["pretty_name"] = pretty_name
    # Default inheritance metadata; may be updated by container/branch logic.
    if "inherit_role" not in record:
        record["inherit_role"] = None
    if "inherit_source" not in record:
        record["inherit_source"] = None

    # 2. Determine base category.
    category = _pick_category_for_step(
        slug=slug,
        engine_type=engine_type,
        subtype=subtype,
        pretty_name=pretty_name,
        match_rules=match_rules,
        step_tags=step_tags,
    )

    record["category"] = category or "unknown"

    # 3. Derive lane from category.
    lane = _map_category_to_lane(record["category"])
    record["lane"] = lane

    # 4. Determine candidacy based on succeed/fail buckets.
    category_lower = _safe_lower(record["category"])
    succeed_set = {c.lower() for c in succeed_bucket}
    fail_set = {c.lower() for c in fail_bucket}

    is_green_candidate = category_lower in succeed_set
    is_red_candidate = category_lower in fail_set

    record["is_green_candidate"] = bool(is_green_candidate)
    record["is_red_candidate"] = bool(is_red_candidate)

    # 5. Merge in tags from global step_tags, if present.
    tag_entry = step_tags.get(slug) or {}
    tags = tag_entry.get("tags") if isinstance(tag_entry, dict) else None
    if tags is None:
        tags = record.get("tags", [])
    record["tags"] = _as_string_list(tags)

    # 6. Apply per-flow step overrides.
    _apply_step_overrides(record, step_override)

    # 7. Apply any previous unmatched override_lane, if still present.
    override_lane = previous_unmatched_entry.get("override_lane") if isinstance(previous_unmatched_entry, dict) else None
    if override_lane:
        record["lane"] = str(override_lane)

    return record


def _pick_category_for_step(
    *,
    slug: str,
    engine_type: str,
    subtype: str,
    pretty_name: str,
    match_rules: Mapping[str, Any],
    step_tags: Mapping[str, Any],
) -> str:
    """
    Determine the category for a step using the configured precedence order.

    Precedence:
    1. step_tags[slug].category
    2. match_rules.step_type_equals
    3. match_rules.step_subtype_equals
    4. match_rules.step_name_starts_with
    5. match_rules.step_name_contains_all
    """
    # 1. Per-step tags override everything else.
    tag_entry = step_tags.get(slug)
    if isinstance(tag_entry, dict):
        tagged_category = tag_entry.get("category")
        if isinstance(tagged_category, str) and tagged_category.strip():
            return tagged_category.strip()

    # Normalize metadata for case-insensitive comparison.
    engine_type_lower = _safe_lower(engine_type)
    subtype_lower = _safe_lower(subtype)
    name_lower = _safe_lower(pretty_name)

    # 2. Type-based rules.
    type_rules = match_rules.get("step_type_equals") if isinstance(match_rules, dict) else None
    if isinstance(type_rules, Mapping):
        for category_name, type_values in type_rules.items():
            for val in _as_string_list(type_values):
                if engine_type_lower == _safe_lower(val):
                    return str(category_name)

    # 3. Subtype-based rules.
    subtype_rules = match_rules.get("step_subtype_equals") if isinstance(match_rules, dict) else None
    if isinstance(subtype_rules, Mapping):
        for category_name, subtype_values in subtype_rules.items():
            for val in _as_string_list(subtype_values):
                if subtype_lower == _safe_lower(val):
                    return str(category_name)

    # 4. Name prefix rules.
    name_prefix_rules = match_rules.get("step_name_starts_with") if isinstance(match_rules, dict) else None
    if isinstance(name_prefix_rules, Mapping):
        for category_name, prefixes in name_prefix_rules.items():
            for prefix in _as_string_list(prefixes):
                if name_lower.startswith(_safe_lower(prefix)):
                    return str(category_name)

    # 5. Name multi-token rules.
    name_contains_all_rules = match_rules.get("step_name_contains_all") if isinstance(match_rules, dict) else None
    if isinstance(name_contains_all_rules, Mapping):
        for category_name, token_groups in name_contains_all_rules.items():
            # token_groups is expected to be a list of lists; each inner list
            # is a set of tokens that must all appear in the name.
            try:
                for token_group in token_groups:
                    tokens = _as_string_list(token_group)
                    if all(_safe_lower(token) in name_lower for token in tokens):
                        return str(category_name)
            except TypeError:
                # If token_groups is not iterable, skip gracefully.
                continue

    # Nothing matched.
    return "unknown"


def _map_category_to_lane(category: str) -> str:
    """
    Map a category to a lane name.

    This is intentionally simple and self-contained. If we ever want to make
    lane mappings configurable, we can lift this into the rules config.
    """
    cat = _safe_lower(category)

    if cat.startswith("preparation"):
        return "prep"
    if cat == "core_operations":
        return "core"
    if cat == "logging_succeed":
        return "log_success"
    if cat == "logging_fail":
        return "log_fail"
    if cat == "logging":
        return "log_other"
    if cat == "trigger":
        return "trigger"

    return "other"


def _apply_step_overrides(record: JSONDict, step_override: Mapping[str, Any]) -> None:
    """
    Apply per-flow step overrides in-place on a step record.

    Supported override keys (per the updated spec):
    - category: semantic category override
    - lane: lane override (prep/core/log_* etc.)
    - path_role: explicit path membership ("primary" | "alt" | "fail" | "none")

    For backwards compatibility, we still honour:
    - is_green_candidate
    - is_red_candidate
    - tags

    In addition to updating the top-level fields, we also maintain a
    nested `override` block on the record so that `pathways.json` clearly
    reflects user intent separate from auto-detected values.
    """
    if not step_override or not isinstance(step_override, Mapping):
        return

    # Start from any existing override block on the record so that we
    # can merge new per-run overrides with previously persisted ones.
    override_block: JSONDict = {}
    existing_override = record.get("override")
    if isinstance(existing_override, Mapping):
        override_block.update(existing_override)

    # Category override.
    if "category" in step_override:
        category = step_override.get("category")
        if isinstance(category, str) and category.strip():
            category_str = category.strip()
            record["category"] = category_str
            override_block["category"] = category_str

    # Lane override.
    if "lane" in step_override:
        lane = step_override.get("lane")
        if isinstance(lane, str) and lane.strip():
            lane_str = lane.strip()
            record["lane"] = lane_str
            override_block["lane"] = lane_str

    # Explicit path_role override (primary/alt/fail/none).
    if "path_role" in step_override:
        path_role = step_override.get("path_role")
        if isinstance(path_role, str) and path_role.strip():
            override_block["path_role"] = path_role.strip()

    # Backwards-compatible flags: allow explicit candidacy and tags to be
    # carried through for older config formats, even though the modern
    # spec prefers `path_role` for path control.
    if "is_green_candidate" in step_override:
        record["is_green_candidate"] = bool(step_override.get("is_green_candidate"))
    if "is_red_candidate" in step_override:
        record["is_red_candidate"] = bool(step_override.get("is_red_candidate"))
    if "tags" in step_override:
        record["tags"] = _as_string_list(step_override.get("tags"))

    if override_block:
        record["override"] = override_block


def _build_succeed_fail_sets(steps: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Compute the flat succeed_steps and fail_steps arrays from per-step records.

    Modern precedence (per spec):

    1. If a step has override.path_role set:
       - "primary" or "alt" => treat as succeed (green-family) step.
       - "fail"             => treat as fail (red) step.
       - "none"             => force it out of both succeed and fail sets.
    2. Otherwise, fall back to is_green_candidate / is_red_candidate flags
       derived from category buckets.
    """
    succeed_steps: List[str] = []
    fail_steps: List[str] = []

    for slug, record in steps.items():
        if not isinstance(record, Mapping):
            continue

        override = record.get("override") if isinstance(record.get("override"), Mapping) else None
        path_role = None
        if isinstance(override, Mapping):
            raw_role = override.get("path_role")
            if isinstance(raw_role, str):
                path_role = raw_role.strip().lower() or None

        # 1. Honour explicit path_role overrides if present.
        if path_role is not None:
            if path_role == "none":
                # Explicitly excluded from all paths.
                continue
            if path_role in ("primary", "alt"):
                succeed_steps.append(slug)
                continue
            if path_role == "fail":
                fail_steps.append(slug)
                continue

        # 2. Fallback: use candidacy flags.
        if record.get("is_green_candidate"):
            succeed_steps.append(slug)
        if record.get("is_red_candidate"):
            fail_steps.append(slug)

    return succeed_steps, fail_steps


def _build_unmatched(
    *,
    steps: Mapping[str, Any],
    succeed_steps: Sequence[str],
    fail_steps: Sequence[str],
    previous_unmatched: Mapping[str, Any],
) -> JSONDict:
    """
    Compute the unmatched map for the snapshot.

    Unmatched steps are those that are neither green nor red candidates.
    We carry forward any existing `override_lane` values from the previous
    unmatched entries when the step still exists.
    """
    succeed_set = set(succeed_steps)
    fail_set = set(fail_steps)

    unmatched: JSONDict = {}

    for slug, record in steps.items():
        if not isinstance(record, Mapping):
            continue
        if slug in succeed_set or slug in fail_set:
            continue

        # Base entry with current lane and a suggested_lane that matches lane
        # for now (future heuristics may suggest different lanes).
        current_lane = str(record.get("lane", "other"))
        entry: JSONDict = {
            "current_lane": current_lane,
            "suggested_lane": current_lane,
            "override_lane": None,
        }

        # Carry forward any previous override_lane if it still exists.
        prev_entry = previous_unmatched.get(slug) if isinstance(previous_unmatched, Mapping) else None
        if isinstance(prev_entry, Mapping) and "override_lane" in prev_entry:
            entry["override_lane"] = prev_entry.get("override_lane")

        unmatched[slug] = entry

    return unmatched


 # ------------------------------------------------------------------------------
 # Container-based inheritance for containers classified as logging_succeed/fail
 # ------------------------------------------------------------------------------
def _apply_container_inheritance(*, steps: MutableMapping[str, Any], vitals: Mapping[str, Any]) -> None:
    """
    Apply simple container-based inheritance for clearly success/fail scopes.

    For now, we treat any container whose category has been classified as
    `logging_succeed` or `logging_fail` as a "success" or "fail" container
    respectively. For each such container, we:
    - Mark the container's own record with `inherit_role` and `inherit_source`.
    - Propagate `inherit_role` and `inherit_source` to neutral descendants
      listed in `vitals["containers"][container_slug]`, adjusting their
      lane and candidacy where safe.

    A descendant is considered neutral if:
    - It does not have an explicit override for category/lane/path_role.
    - Its category is not already a strong logging_succeed/logging_fail.
    """
    if not steps or not isinstance(steps, MutableMapping):
        return

    containers = vitals.get("containers") or {}
    if not isinstance(containers, Mapping):
        return

    for container_slug, children in containers.items():
        if container_slug not in steps:
            continue
        container_record = steps.get(container_slug)
        if not isinstance(container_record, Mapping):
            continue

        category = _safe_lower(container_record.get("category"))
        inherit_role: Optional[str] = None
        if category == "logging_succeed":
            inherit_role = "success"
        elif category == "logging_fail":
            inherit_role = "fail"

        if inherit_role is None:
            continue

        # Mark the container itself.
        container_record["inherit_role"] = inherit_role
        container_record["inherit_source"] = container_slug

        # Determine the lane we want descendants to tend towards.
        desired_lane = container_record.get("lane") or _map_category_to_lane(container_record.get("category", ""))

        # Walk descendants listed in vitals["containers"][container_slug].
        try:
            child_slugs = list(children)
        except TypeError:
            child_slugs = []

        for child_slug in child_slugs:
            child_record = steps.get(child_slug)
            if not isinstance(child_record, Mapping):
                continue

            # Always record the inheritance source/role so that downstream
            # tooling can see branch membership, even if we do not change
            # lane/candidacy due to explicit overrides.
            child_record["inherit_source"] = child_record.get("inherit_source") or container_slug
            # Do not override an explicit inherit_role that might have been
            # set by other logic (e.g. future branch overrides).
            if not child_record.get("inherit_role"):
                child_record["inherit_role"] = inherit_role

            # Check for explicit per-step overrides that should win.
            override_block = child_record.get("override")
            if isinstance(override_block, Mapping):
                has_explicit_lane = bool(override_block.get("lane"))
                has_explicit_category = bool(override_block.get("category"))
                has_explicit_path_role = bool(override_block.get("path_role"))
            else:
                has_explicit_lane = has_explicit_category = has_explicit_path_role = False

            if has_explicit_lane or has_explicit_category or has_explicit_path_role:
                # Respect user intent: do not change lane/candidacy.
                continue

            # Do not try to "paint over" explicitly strong logging categories.
            child_category = _safe_lower(child_record.get("category"))
            if child_category in ("logging_succeed", "logging_fail"):
                continue

            # Adjust candidacy flags based on inheritance.
            if inherit_role == "success":
                child_record["is_green_candidate"] = True
            elif inherit_role == "fail":
                child_record["is_red_candidate"] = True

            # Adjust lane if it is currently generic; avoid clobbering
            # obvious non-logging lanes like "core" or "trigger".
            current_lane = str(child_record.get("lane") or "")
            if current_lane in ("other", "log_other", "") and desired_lane:
                child_record["lane"] = desired_lane


# ------------------------------------------------------------------------------
# Branch-based inheritance for conditional/switch branches (branch_overrides)
# ------------------------------------------------------------------------------
def _apply_branch_inheritance(
    *,
    steps: MutableMapping[str, Any],
    vitals: Mapping[str, Any],
    branch_overrides: Mapping[str, Any],
) -> None:
    """
    Apply branch-level inheritance for conditional (If) and Switch steps
    based on the per-flow `branch_overrides` configuration.

    Expected shapes:

    - branch_overrides:
      {
        "<conditional_slug>": {
          "branches": {
            "true":  { "inherit_role": "fail",    "path_role": "fail"    },
            "false": { "inherit_role": "success", "path_role": "primary" }
          }
        },
        "Switch_Platform": {
          "branches": {
            "Mac":    { "inherit_role": "success_alt", "path_role": "alt"     },
            "Mobile": { "inherit_role": "success",     "path_role": "primary" }
          }
        }
      }

    - vitals["branches"]:
      {
        "<conditional_slug>": {
          "<branch_id>": ["ChildStep1", "ChildStep2", ...]
        }
      }

    For each (conditional_slug, branch_id) pair where both an override and
    a branch subtree are present, we:

    - Set `inherit_role` on each descendant step in that branch. This may
      override container-based inherit_role, as branch overrides are more
      specific.
    - Set `inherit_source` to the conditional slug so downstream tooling
      can see where the inheritance came from.
    - Adjust green/fail candidacy based on inherit_role:
        * "success" / "success_alt" -> is_green_candidate = True
        * "fail"                    -> is_red_candidate = True
    - If a branch-level path_role is provided ("primary"|"alt"|"fail"|
      "none") and the step does not already have an explicit override
      path_role, we write it into the record's `override.path_role`.

    Per-step overrides remain dominant: if a step already has an override
    path_role set, we do not replace it. Likewise, per-step overrides for
    lane/category are respected and we avoid changing those fields here.
    """
    if not steps or not isinstance(steps, MutableMapping):
        return
    if not isinstance(branch_overrides, Mapping) or not branch_overrides:
        return

    branches_info = vitals.get("branches") or {}
    if not isinstance(branches_info, Mapping):
        return

    for conditional_slug, conditional_cfg in branch_overrides.items():
        if not isinstance(conditional_cfg, Mapping):
            continue
        branch_cfg_map = conditional_cfg.get("branches")
        if not isinstance(branch_cfg_map, Mapping) or not branch_cfg_map:
            continue

        conditional_branches = branches_info.get(conditional_slug)
        if not isinstance(conditional_branches, Mapping):
            # No structural info for this conditional in vitals; skip.
            continue

        for branch_id, branch_cfg in branch_cfg_map.items():
            if not isinstance(branch_cfg, Mapping):
                continue

            inherit_role = branch_cfg.get("inherit_role")
            if isinstance(inherit_role, str):
                inherit_role = inherit_role.strip() or None
            else:
                inherit_role = None

            path_role = branch_cfg.get("path_role")
            if isinstance(path_role, str):
                path_role = path_role.strip() or None
            else:
                path_role = None

            branch_nodes = conditional_branches.get(branch_id)
            if not branch_nodes:
                continue

            try:
                branch_slugs = list(branch_nodes)
            except TypeError:
                branch_slugs = []

            for slug in branch_slugs:
                record = steps.get(slug)
                if not isinstance(record, Mapping):
                    continue

                # When applying branch-level inheritance, we treat it as
                # more specific than container-level inheritance. It is
                # therefore allowed to overwrite inherit_role/source that
                # may have been set by containers.
                if inherit_role:
                    record["inherit_role"] = inherit_role
                    record["inherit_source"] = conditional_slug

                    # Adjust candidacy flags based on inherit_role.
                    if inherit_role in ("success", "success_alt"):
                        record["is_green_candidate"] = True
                    elif inherit_role == "fail":
                        record["is_red_candidate"] = True

                # Apply branch-level path_role only when there is not
                # already an explicit per-step override path_role.
                if path_role:
                    existing_override = record.get("override")
                    if isinstance(existing_override, Mapping):
                        has_explicit_path_role = bool(existing_override.get("path_role"))
                        override_block = dict(existing_override)
                    else:
                        has_explicit_path_role = False
                        override_block = {}

                    if not has_explicit_path_role:
                        override_block["path_role"] = path_role
                        record["override"] = override_block



def _infer_success_vs_alt_success_by_branch(
    *,
    adjacency: Dict[str, List[str]],
    start_nodes: List[str],
    fail_nodes: set[str],
) -> Tuple[set[str], set[str]]:
    """Infer success vs alt_success tracks using a simple branching rule.

    Rule-of-thumb:
    - Any node not already classified as fail is considered part of success unless it
      is reached via the *secondary* branch from a non-fail branching point.
    - When a non-fail node has multiple outgoing edges to non-fail nodes, the
      first branch (deterministic order) remains `success` and all other branches
      are treated as `alt_success` (and propagate downstream).

    Notes:
    - Deterministic ordering is based on the outgoing node name sort (case-insensitive).
    - Nodes already in `fail_nodes` are excluded from this inference.
    """

    success: set[str] = set()
    alt_success: set[str] = set()

    stack: List[Tuple[str, bool]] = [(n, False) for n in start_nodes]
    visited: set[Tuple[str, bool]] = set()

    while stack:
        node, is_alt = stack.pop()
        state = (node, is_alt)
        if state in visited:
            continue
        visited.add(state)

        if node in fail_nodes:
            continue

        if is_alt:
            alt_success.add(node)
        else:
            success.add(node)

        outs = adjacency.get(node, [])
        outs_non_fail = [o for o in outs if o not in fail_nodes]

        if len(outs_non_fail) > 1:
            ordered = sorted(outs_non_fail, key=lambda s: s.lower())
            primary = ordered[0]
            secondaries = ordered[1:]

            # Primary continues current track.
            stack.append((primary, is_alt))

            # Secondary branches are alt_success.
            for o in secondaries:
                stack.append((o, True))

            continue

        for o in outs_non_fail:
            stack.append((o, is_alt))

    success -= alt_success
    return success, alt_success

# ------------------------------------------------------------------------------
# Internal helpers – paths
# ------------------------------------------------------------------------------


def _build_paths(
    *,
    vitals: Mapping[str, Any],
    steps: Mapping[str, Any],
    succeed_steps: Sequence[str],
    fail_steps: Sequence[str],
    reg: Mapping[str, Any],
    conservative_pathing: bool,
    path_overrides: Mapping[str, Any],
) -> JSONDict:
    """
    Build the `paths` section of the snapshot.

    This is a deliberately simple first pass that:
    - Defines a primary green path as all green-candidate steps (plus the
      trigger) in `creation_order` order.
    - Defines an alternate green path (`alt_green`) when `conservative_pathing` is False.
    - Defines one red path per fail-candidate step, each as a minimal
      single-node branch.

    This keeps the behavior deterministic and useful for charts without
    requiring deep run_after graph analysis yet.
    """
    if not steps:
        return {}

    creation_order = vitals.get("creation_order") or []
    if not isinstance(creation_order, Sequence):
        creation_order = list(steps.keys())

    step_records = steps

    # Determine trigger: prefer the first item in creation_order that
    # appears in steps; fall back to any step categorized as "trigger".
    trigger_slug: Optional[str] = None
    for slug in creation_order:
        if slug in step_records:
            trigger_slug = slug
            break
    if trigger_slug is None:
        for slug, record in step_records.items():
            if isinstance(record, Mapping) and _safe_lower(record.get("category")) == "trigger":
                trigger_slug = slug
                break

    succeed_set = set(succeed_steps)
    fail_set = set(fail_steps)
    # In non-conservative mode, we treat *all* non-fail steps as eligible for
    # success/alt_success lanes for charting. This matches the rule-of-thumb:
    # if it continues onward and is not a Fail-* node, it is success or alt_success.
    effective_green_set = succeed_set if conservative_pathing else (set(step_records.keys()) - fail_set)

    # Default heuristic: infer an alternate green track when non-fail branches split.
    # This is used for chart coloring (success vs alt_success) and does not affect
    # fail classification.
    inferred_success: Optional[set[str]] = None
    inferred_alt: Optional[set[str]] = None

    if not conservative_pathing and trigger_slug is not None:
        # Build parent->children adjacency from each step's run_after.
        adjacency: Dict[str, List[str]] = {}
        for child_slug, step_info in reg.items():
            ra = getattr(step_info, "run_after", None) or {}
            if not isinstance(ra, Mapping):
                continue
            for parent_slug in ra.keys():
                adjacency.setdefault(str(parent_slug), []).append(str(child_slug))

        inferred_success, inferred_alt = _infer_success_vs_alt_success_by_branch(
            adjacency=adjacency,
            start_nodes=[trigger_slug],
            fail_nodes=fail_set,
        )

        # In non-conservative mode, include all non-fail steps in the green-family
        # paths (success vs alt_success), not only pre-tagged green candidates.
        inferred_success &= effective_green_set
        inferred_alt &= effective_green_set

    # Build primary green path and optional alt green path in creation_order.
    primary_green_steps: List[str] = []
    alt_green_steps: List[str] = []

    for slug in creation_order:
        if slug not in step_records:
            continue

        # Always keep trigger at the start of primary_green.
        if slug == trigger_slug:
            if slug not in primary_green_steps:
                primary_green_steps.append(slug)
            continue

        if slug not in effective_green_set:
            continue

        if inferred_alt is not None and slug in inferred_alt:
            if slug not in alt_green_steps:
                alt_green_steps.append(slug)
            continue

        # Default: success lane.
        if slug not in primary_green_steps:
            primary_green_steps.append(slug)

    paths: JSONDict = {}

    if primary_green_steps:
        paths["primary_green"] = {
            "kind": "green",
            "label": "Primary golden path",
            "trigger": trigger_slug,
            "steps": primary_green_steps,
        }

    if alt_green_steps:
        paths["alt_green"] = {
            "kind": "green_alt",
            "label": "Alternate success path",
            "trigger": trigger_slug,
            "steps": [trigger_slug] + alt_green_steps if trigger_slug else alt_green_steps,
        }

    # Build minimal red paths: one branch per fail-candidate step.
    fail_set = set(fail_steps)
    for slug in fail_set:
        if slug not in step_records:
            continue
        paths[f"fail_{slug}"] = {
            "kind": "red",
            "label": f"Failure branch at {slug}",
            "steps": [slug],
        }

    # Apply explicit path overrides from per-flow configuration, if present.
    if isinstance(path_overrides, Mapping):
        valid_slugs = set(step_records.keys())
        succeed_set = set(succeed_steps)
        for name, override in path_overrides.items():
            if not isinstance(override, Mapping):
                continue
            raw_steps = override.get("steps")
            if not raw_steps:
                continue
            override_steps = [
                slug for slug in _as_string_list(raw_steps)
                if slug in valid_slugs
            ]
            if not override_steps:
                continue

            base_entry: JSONDict = dict(paths.get(name, {}))

            # Allow overrides to supply label/kind explicitly.
            if "label" in override:
                base_entry["label"] = str(override.get("label"))
            if "kind" in override:
                kind_val = override.get("kind")
                if kind_val is not None:
                    base_entry["kind"] = str(kind_val)

            base_entry["steps"] = override_steps

            # If kind is still missing, infer a reasonable default.
            if not base_entry.get("kind"):
                if name == "primary_green":
                    base_entry["kind"] = "green"
                elif any(slug in fail_set for slug in override_steps):
                    base_entry["kind"] = "red"
                elif all(slug in succeed_set for slug in override_steps):
                    base_entry["kind"] = "green_alt"
                else:
                    base_entry["kind"] = "other"

            paths[name] = base_entry

    return paths


def _prune_paths(*, paths: Mapping[str, Any], valid_slugs: Iterable[str]) -> JSONDict:
    """
    Prune any references to non-existent slugs from previously-recorded paths.

    This is primarily used when the source MD5 changes (flow structure
    changed) and we want to drop steps that no longer exist while keeping
    any path skeletons that still make sense.
    """
    valid = set(valid_slugs)
    if not paths or not isinstance(paths, Mapping):
        return {}

    pruned: JSONDict = {}

    for path_key, path_info in paths.items():
        if not isinstance(path_info, Mapping):
            continue
        steps = path_info.get("steps")
        if not steps:
            continue
        filtered_steps = [slug for slug in steps if slug in valid]
        if not filtered_steps:
            continue

        new_entry = dict(path_info)
        new_entry["steps"] = filtered_steps
        pruned[path_key] = new_entry

    return pruned


# ------------------------------------------------------------------------------
# Spec B — Three-lane pathways table view (Fail | Success | Alt-Success)
# ------------------------------------------------------------------------------


def _build_three_lane_table_view(
    *,
    flow_root: Path,
    reg: Mapping[str, Any],
    steps: Mapping[str, Any],
    vitals: Mapping[str, Any],
) -> JSONDict:
    """Build the Spec-B `views.three_lane_table` payload.

    Contract:
    - Prefer `flow_model.json` as the authoritative ordering source.
    - Do not infer order in PP/Renderer; all semantics happen here.

    Output shape:
      {
        "lanes": ["fail", "success", "alt_success"],
        "rows": [
          {
            "fail": {"lines": [...]},
            "success": {"lines": [...]},
            "alt_success": {"lines": [...]},
            "meta": {"kind": "step"}
          },
          ...
        ]
      }

    Notes:
    - This implementation is intentionally deterministic and conservative.
    - It focuses on Spec-B critical requirements: fail-indication + row-sharing.
    - It also performs basic compacting for Switch and simple If (when safe).
    """

    flow_model_path = Path(flow_root) / "flow_model.json"
    flow_model = _load_json_if_exists(flow_model_path) or {}

    execution_steps = []
    execution_order = []
    nodes = {}

    if isinstance(flow_model, Mapping):
        raw_steps = flow_model.get("execution_steps")
        raw_order = flow_model.get("execution_order")
        raw_nodes = flow_model.get("nodes")

        if isinstance(raw_steps, list):
            # Commented for emergency backup
            #execution_steps = [str(x) for x in raw_steps if x is not None]
            execution_steps = [
                str(x)
                for x in raw_steps
                if x is not None and "::branch::" not in str(x)
            ]
        if isinstance(raw_order, list):
            execution_order = [str(x) for x in raw_order if x is not None]
        if isinstance(raw_nodes, Mapping):
            nodes = dict(raw_nodes)

    # Fallbacks: tolerate legacy pipelines where ordering was stored in vitals.
    if not execution_steps:
        raw = vitals.get("execution_steps") or vitals.get("executionSteps")
        if isinstance(raw, list):
            # Commented for emergency backup
            #execution_steps = [str(x) for x in raw if x is not None]
            execution_steps = [
                str(x)
                for x in raw
                if x is not None and "::branch::" not in str(x)
            ]
    if not execution_order:
        raw = vitals.get("execution_order") or vitals.get("executionOrder")
        if isinstance(raw, list):
            execution_order = [str(x) for x in raw if x is not None]

    # Normalize execution_order to build a best-effort mapping from leaf step slug
    # -> active branch label (true/false/default/case:German/etc.).
    branch_for_step: Dict[str, str] = {}
    parent_for_step: Dict[str, str] = {}
    step_type_for_node: Dict[str, str] = {}
    run_after_for_node: Dict[str, List[str]] = {}

    for node_id, node_info in nodes.items():
        if not isinstance(node_info, Mapping):
            continue
        atype = node_info.get("atype_display") or node_info.get("atype") or ""
        step_type_for_node[str(node_id)] = str(atype)

        parent = node_info.get("parent")
        if parent is not None:
            parent_for_step[str(node_id)] = str(parent)

        ra = node_info.get("run_after")
        # In flow_model.json, run_after is { parent_slug: [states...] }.
        if isinstance(ra, Mapping):
            states: List[str] = []
            for _, st in ra.items():
                if isinstance(st, list):
                    states.extend([str(x) for x in st if x is not None])
            if states:
                run_after_for_node[str(node_id)] = states

    def _leaf_slug(x: str) -> str:
        # Convert "A::B::C" -> "C"
        parts = str(x).split("::")
        return parts[-1] if parts else str(x)

    def _is_branch_marker(x: str) -> bool:
        s = str(x)
        if "::branch::" in s:
            return True
        # Some FlowModel writers emit bare branch labels (e.g., "true", "false", "default").
        if s.lower() in ("true", "false", "default"):
            return True
        return False

    def _branch_label_from_marker(x: str) -> str:
        # "...::branch::case:German" -> "case:German"; "true" -> "true"
        s = str(x)
        if "::branch::" in s:
            return s.split("::branch::", 1)[1]
        return s.lower()

    active_branch = ""

    for item in execution_order:
        if _is_branch_marker(item):
            active_branch = _branch_label_from_marker(item)
            continue

        leaf = _leaf_slug(item)
        if active_branch and leaf not in branch_for_step:
            branch_for_step[leaf] = active_branch

    def _pretty_title(slug: str) -> str:
        # Prefer pathways step record pretty_name, then registry, then slug.
        rec = steps.get(slug)
        if isinstance(rec, Mapping):
            pn = rec.get("pretty_name")
            if isinstance(pn, str) and pn.strip():
                return pn.strip()
        step_info = reg.get(slug)
        if step_info is not None:
            pn = getattr(step_info, "pretty_name", None)
            if isinstance(pn, str) and pn.strip():
                return pn.strip()
            nm = getattr(step_info, "name", None)
            if isinstance(nm, str) and nm.strip():
                return nm.strip()
        return str(slug)

    def _is_fail_indicated(slug: str) -> bool:
        # 1) Prefer Designer-facing title, but tolerate underscore-normalized variants.
        title = _pretty_title(slug)
        title_norm = title.replace("_", " ") if isinstance(title, str) else ""
        if title_norm.startswith("Fail -") or title_norm.startswith("Fail-"):
            return True

        # 2) Also treat Fail-prefixed slugs as fail-indicated (defensive).
        slug_s = str(slug)
        if slug_s.startswith("Fail_") or slug_s.startswith("Fail-"):
            return True

        # 3) Respect explicit toolkit classification/overrides when available.
        rec = steps.get(slug)
        if isinstance(rec, Mapping):
            if str(rec.get("inherit_role") or "").lower() == "fail":
                return True
            if rec.get("is_red_candidate") is True:
                return True
            override = rec.get("override")
            if isinstance(override, Mapping):
                if str(override.get("path_role") or "").lower() == "fail":
                    return True

        # 4) Spec-B rule: first step gated by runAfter: Failed/TimedOut/Skipped.
        states = run_after_for_node.get(slug) or []
        bad = {"Failed", "TimedOut", "Skipped"}
        return any(s in bad for s in states)

    def _lane_for_step(slug: str) -> str:
        # Branch-level default mapping per Spec B; promotion to fail if fail-indicated.
        if _is_fail_indicated(slug):
            return "fail"
        b = (branch_for_step.get(slug) or "").lower()
        if b == "false":
            return "alt_success"
        return "success"

    def _is_switch(slug: str) -> bool:
        t = step_type_for_node.get(slug) or ""
        return str(t).lower() == "switch"

    def _is_if(slug: str) -> bool:
        t = step_type_for_node.get(slug) or ""
        return str(t).lower() in ("if", "condition")

    # Build quick lookup: which leaf steps belong to which container parent.
    children_by_parent: Dict[str, List[str]] = {}
    for child, parent in parent_for_step.items():
        children_by_parent.setdefault(parent, []).append(child)

    # Spec-B rows builder
    rows: List[JSONDict] = []

    def _new_row() -> JSONDict:
        return {
            "fail": {"lines": []},
            "success": {"lines": []},
            "alt_success": {"lines": []},
            "meta": {"kind": "step"},
        }

    # We will attach fail chains to the row whose success cell contains the
    # next non-fail success step in the execution_steps stream.
    pending_fail_lines: List[str] = []

    i = 0
    while i < len(execution_steps):
        # Commented as emergency backup
        slug = _leaf_slug(str(execution_steps[i]))

        # Compact Switch representation (Spec B §8)
        if _is_switch(slug):
            switch_title = _pretty_title(slug)

            # Consume consecutive steps that are children of this switch.
            j = i + 1
            branch_lines: Dict[str, List[str]] = {}

            while j < len(execution_steps):
                # Commented out as emergency backup
                #child = str(execution_steps[j])
                child = _leaf_slug(str(execution_steps[j]))
                parent = parent_for_step.get(child)
                if parent != slug:
                    break

                label = branch_for_step.get(child) or "default"
                branch_lines.setdefault(label, []).append(_pretty_title(child))
                j += 1

            row = _new_row()
            # Switch summary cell: list each case → chain.
            row["success"]["lines"].append(f"Switch ({switch_title})")

            # Deterministic ordering: default first, then case:* lexicographically.
            ordered_labels = sorted(
                branch_lines.keys(),
                key=lambda x: (0 if x == "default" else 1, str(x).lower()),
            )

            for label in ordered_labels:
                chain = branch_lines.get(label) or []
                if not chain:
                    continue
                if label == "default":
                    prefix = "- Default      → "
                elif label.startswith("case:"):
                    case_name = label.split("case:", 1)[1]
                    prefix = f"- If \"{case_name}\" → "
                else:
                    prefix = f"- {label} → "
                row["success"]["lines"].append(prefix + " → ".join(chain))

            # Attach any pending fail chain to this same row (row sharing).
            if pending_fail_lines:
                row["fail"]["lines"].extend(pending_fail_lines)
                pending_fail_lines = []

            rows.append(row)
            i = j
            continue

        # Compact If representation (Spec B §7.3), only when both branches are
        # single-step and non-fail-indicated.
        if _is_if(slug):
            if_title = _pretty_title(slug)

            # Identify immediate leaf children under this If container.
            # We consider at most the next 2 child leaves directly parented to the If.
            j = i + 1
            true_step: Optional[str] = None
            false_step: Optional[str] = None

            while j < len(execution_steps):
                child = _leaf_slug(str(execution_steps[j]))
                parent = parent_for_step.get(child)
                if parent != slug:
                    break

                b = (branch_for_step.get(child) or "").lower()
                if b == "true" and true_step is None:
                    true_step = child
                elif b == "false" and false_step is None:
                    false_step = child

                # If we already found both, stop early.
                if true_step and false_step:
                    break
                j += 1

            can_compact = (
                true_step is not None
                and false_step is not None
                and not _is_fail_indicated(true_step)
                and not _is_fail_indicated(false_step)
            )

            if can_compact:
                row = _new_row()
                row["success"]["lines"].append(f"{if_title} (If <condition>)")
                row["success"]["lines"].append(f"- True  → {_pretty_title(true_step)}")
                row["success"]["lines"].append(f"- False → {_pretty_title(false_step)}")

                if pending_fail_lines:
                    row["fail"]["lines"].extend(pending_fail_lines)
                    pending_fail_lines = []

                rows.append(row)

                # Skip the compacted child steps if they appear consecutively.
                # We only skip those that are directly parented to the If.
                k = i + 1
                while (
                    k < len(execution_steps)
                    and parent_for_step.get(_leaf_slug(str(execution_steps[k]))) == slug
                ):
                    k += 1
                i = k
                continue

        lane = _lane_for_step(slug)
        title = _pretty_title(slug)

        if lane == "fail":
            # Spec B: Fail lane is a chain; we buffer fail lines until we can
            # attach them to the next success-row (row sharing).
            pending_fail_lines.append(title)
            i += 1
            continue

        # Success or alt_success step => create a new row.
        row = _new_row()
        row[lane]["lines"].append(title)

        if pending_fail_lines:
            row["fail"]["lines"].extend(pending_fail_lines)
            pending_fail_lines = []

        rows.append(row)
        i += 1

    # If we ended with dangling fail steps and no subsequent success step,
    # emit a final row so the information is not lost.
    if pending_fail_lines:
        row = _new_row()
        row["fail"]["lines"].extend(pending_fail_lines)
        rows.append(row)

    return {
        "lanes": ["fail", "success", "alt_success"],
        "rows": rows,
    }
