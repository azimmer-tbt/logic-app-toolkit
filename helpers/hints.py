

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List

# ─────────────────────────────────────────────────────────────────────────────
# Hints (rules) engine for subtype detection
# ─────────────────────────────────────────────────────────────────────────────

def _hint_get_dotted(raw: Dict[str, Any], dotted: str) -> Any:
    """
    Safe dotted lookup into a nested dict. Example:
      _hint_get_dotted(raw, "inputs.host.connection.name")
    """
    cur: Any = raw
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _hint_value_matches(val: Any, op: str, pattern: Any) -> bool:
    """
    Core comparison operator for hint conditions.

    Supported ops:
      - equals / not_equals
      - contains / not_contains
      - exists / not_exists   (pattern is ignored)
    """
    op = (op or "").lower()

    # Presence checks first
    if op == "exists":
        # "truthy" existence: non-empty string / list / dict etc.
        if val is None:
            return False
        if isinstance(val, str):
            return len(val.strip()) > 0
        if isinstance(val, (list, dict, tuple, set)):
            return len(val) > 0
        return True

    if op == "not_exists":
        if val is None:
            return True
        if isinstance(val, str):
            return len(val.strip()) == 0
        if isinstance(val, (list, dict, tuple, set)):
            return len(val) == 0
        return False

    # From here on we treat both sides as strings for simplicity.
    sval = "" if val is None else str(val)
    spat = "" if pattern is None else str(pattern)

    if op == "equals":
        return sval == spat
    if op == "not_equals":
        return sval != spat
    if op == "contains":
        return spat in sval
    if op == "not_contains":
        return spat not in sval

    # Unknown op: be conservative and return False.
    return False


def _hint_condition_matches(raw: Dict[str, Any], cond: Dict[str, Any]) -> bool:
    """
    Evaluate a single condition dict of the form:
      {
        "field": "inputs.path",
        "op": "contains",
        "value": "https://intranet.example.com"
      }

    For exists/not_exists, 'value' is ignored.
    """
    field = cond.get("field")
    op    = cond.get("op", "equals")
    value = cond.get("value")

    if not field:
        # Malformed condition: treat as non-matching
        return False

    v = _hint_get_dotted(raw, field)
    return _hint_value_matches(v, op, value)


def _hint_group_matches_advanced(raw: Dict[str, Any], group: Dict[str, Any]) -> bool:
    """
    Evaluate a 'group' entry:

      {
        "name": "by_url",
        "needs_all": true,
        "conditions": [ {...}, {...} ]
      }

    rules:
      - If needs_all is True  → ALL conditions must match.
      - If needs_all is False → ANY condition may match.
      - Empty conditions      → always False (group is useless).
    """
    conditions = group.get("conditions") or []
    if not conditions:
        return False

    needs_all = bool(group.get("needs_all", True))

    if needs_all:
        for cond in conditions:
            if not _hint_condition_matches(raw, cond):
                return False
        return True
    else:
        for cond in conditions:
            if _hint_condition_matches(raw, cond):
                return True
        return False

def _hint_entry_matches_advanced(
    raw: Dict[str, Any],
    hint_entry: Dict[str, Any],
) -> bool:
    """
    Summary:
        Determine whether a single advanced hint entry matches the given
        step JSON payload.

        A hint entry may define multiple "ways to match" (OR semantics)
        under the `ways_to_match` key. Each way_to_match is itself a group
        of conditions, which are evaluated using `_hint_group_matches_advanced`.

    Args:
        raw:
            The raw step JSON object as emitted in the Logic App definition.
        hint_entry:
            The advanced hint definition dictionary. Expected keys:
                - "enabled": optional bool; when False, the hint is skipped.
                - "future_use": optional bool; when True, the hint is skipped.
                - "ways_to_match": list of condition groups (preferred).
                  Each group is a dict with "needs_all" and "conditions".
                - "groups" / "paths": optional legacy aliases for
                  "ways_to_match" (still honored for backward compatibility).

    Returns:
        True if at least one way_to_match group evaluates to True for the
        given step JSON. False otherwise.

    Notes:
        - This function does *not* check the step type; that filtering is
          handled at the caller (e.g. in `apply_hints_advanced`).
        - Both `enabled` and `future_use` gates are enforced:
            * If future_use is True, the hint is reserved and not applied.
            * If enabled is explicitly False, the hint is not applied.
    """
    # Gate by future_use and enabled flags.
    if hint_entry.get("future_use"):
        return False

    enabled = hint_entry.get("enabled", True)
    if not enabled:
        return False

    # Prefer the new catalog key, but accept legacy names for safety.
    groups = (
        hint_entry.get("ways_to_match")
        or hint_entry.get("groups")
        or hint_entry.get("paths")
        or []
    )

    if not groups:
        # No groups defined; cannot match.
        return False

    # OR semantics across groups: any matching group is enough.
    for group in groups:
        if _hint_group_matches_advanced(raw, group):
            return True

    return False

def apply_hints_advanced(reg: Dict[str, "StepInfo"], hints_obj: Any, verbose: bool=False) -> None:
    """
    Apply advanced hints with groups/conditions to the registry.

    Expected hints_obj shapes:
      - {"hints": [ ... ]}   (catalog-style)
      - [ ... ]              (plain list)
    """
    # Normalize hints list
    if isinstance(hints_obj, dict):
        hints_list = hints_obj.get("hints", [])
    elif isinstance(hints_obj, list):
        hints_list = hints_obj
    else:
        hints_list = []

    if not hints_list:
        if verbose:
            print("HINTS: no advanced hints supplied; skipping subtype inference.", file=sys.stderr)
        return

    for name, s in reg.items():
        raw = s.raw or {}
        # Only bother if this step's raw 'type' matches hint_entry['type']
        step_type = raw.get("type") or s.atype

        for h in hints_list:
            h_type = h.get("type")
            if h_type and h_type != step_type:
                continue

            if _hint_entry_matches_advanced(raw, h):
                subtype = h.get("subtype")
                if subtype:
                    old = s.atype_display
                    s.atype_display = subtype
                    if verbose:
                        print(f"SUBTYPE-MATCH: step '{name}' of meta-type '{old}' matches subtype '{subtype}'", file=sys.stderr)
                break  # stop at first matching hint for this step
