#!/usr/bin/env python3
"""
helpers/catalog_tools.py
Unified library for catalog lookups, label resolution, and unknown type tracking.
Used by both analyzer (logicapp_doc_toolkit_v3c.py) and post-processor (logicapp_post_processor_md_v2i.py).
"""

import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

def strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC text for catalog loading."""
    # Remove /* ... */ blocks
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove // ... to end of line
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return text

# ──────────────────────────────────────────────────────────────────────────────
# Catalog access helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_catalog(path: Path) -> dict:
    """Load a JSON or JSONC catalog file."""
    try:
        text = path.read_text(encoding="utf-8")
        text = strip_json_comments(text)
        return json.loads(text)
    except Exception as e:
        print(f"ERROR: Failed to read catalog {path}: {e}", file=sys.stderr)
        return {}

def resolve_labels(atype_display: str, atype: str, catalog: dict) -> dict:
    """
    Look up pretty labels from a structured catalog with
    catalog['types'], catalog['subtypes'], and catalog['hints'].
    Returns {'pretty_category': str, 'pretty_name': str}.
    """
    if not catalog:
        return {"pretty_category": "", "pretty_name": ""}

    # unified view across both types and subtypes
    merged = {}
    if "types" in catalog and isinstance(catalog["types"], dict):
        merged.update(catalog["types"])
    if "subtypes" in catalog and isinstance(catalog["subtypes"], dict):
        merged.update(catalog["subtypes"])

    key_candidates = [atype_display, atype]
    for k in key_candidates:
        if k in merged:
            entry = merged[k]
            return {
                "pretty_category": entry.get("pretty_category", ""),
                "pretty_name": entry.get("pretty_name", "")
            }

    return {"pretty_category": "", "pretty_name": ""}

def _catalog_lookup_pretty_name(catalog: dict, key: str) -> str:
    """Return the pretty_name for a given key, if present."""
    if key in catalog:
        return catalog[key].get("pretty_name", "")
    return ""

def _catalog_lookup_labels(catalog: dict, key: str) -> dict:
    """
    Return the pretty_category / pretty_name block for a resolved type or subtype.
    Priority:
      1) catalog["subtypes"][key]
      2) catalog["types"][key]
      else {}
    """
    if not isinstance(catalog, dict):
        return {}

    sub = catalog.get("subtypes", {})
    if isinstance(sub, dict) and key in sub:
        return sub.get(key, {})

    typ = catalog.get("types", {})
    if isinstance(typ, dict) and key in typ:
        return typ.get(key, {})

    return {}

# ──────────────────────────────────────────────────────────────────────────────
# Catalog helpers
# ──────────────────────────────────────────────────────────────────────────────

def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict that is a deep merge of ``base`` and ``override``.

    Rules:
    - Works recursively on nested dictionaries.
    - When both sides have a dict at the same key, their contents are merged
      via the same rules.
    - For all other types (scalars, lists, None, etc.), the value from
      ``override`` replaces the one in ``base``.
    - Neither input dict is mutated.
    """
    if not isinstance(base, dict):
        raise TypeError(f"deep_merge_dicts: base must be dict, got {type(base)!r}")
    if not isinstance(override, dict):
        raise TypeError(f"deep_merge_dicts: override must be dict, got {type(override)!r}")

    result: Dict[str, Any] = deepcopy(base)

    for key, o_val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(o_val, dict):
            # Recurse for nested dicts
            result[key] = deep_merge_dicts(result[key], o_val)
        else:
            # For non-dicts, override wins (including None)
            result[key] = deepcopy(o_val)

    return result

def _catalog_lookup_pretty_category(catalog: Dict[str, Any], resolved_type: str) -> str:
    """
    Look up Designer-facing category for a resolved type
    (e.g., "Compose", "ApiConnection.SharePointWrite"). Returns "" if not found.
    """
    try:
        types = catalog.get("types", {})
        entry = types.get(resolved_type, {})
        return str(entry.get("pretty_category", "") or "")
    except Exception:
        return ""

def _attach_pretty_category(
    reg: Dict[str, "StepInfo"],
    catalog: Dict[str, Any],
    templates_dir: Path,
    issues: "AnalyzerUnknowns",
    verbose: bool = False,
) -> None:
    """
    Resolve each step's effective type key (base or subtype) via catalog/hints,
    attach pretty_category / pretty_name onto the StepInfo, and record any
    catalog misses in `issues`.

    Also emits a verbose line like:
      TYPE-MATCH: 'Fail - Prepare Log Data' is `Scope` from category 'Control'
    """
    from helpers.markdown_helpers_step import pretty_name  # local import to avoid circular dep
    meta_types = catalog.get("meta_types") or {}
    for name, s in reg.items():
        # 1) Figure out the resolved key: subtype if known, else base type.
        #    This function should also add catalog misses into `issues`.
        raw_key = s.atype_display or s.atype
        resolved = _catalog_resolve_type(catalog, raw_key, issues)

        # Keep the resolved key as the display type going forward
        s.atype_display = resolved

        # 2) Look up labels (pretty_category / pretty_name) from catalog
        labels = _catalog_lookup_labels(catalog, resolved)
        pretty_cat = labels.get("pretty_category", "") or ""
        pretty_step_name = labels.get("pretty_name", "") or ""

        # Persist them on the StepInfo (slots were extended earlier)
        s.pretty_category = pretty_cat
        # If catalog doesn't have a pretty_name override, fall back to existing pretty_name() logic
        s.pretty_name = pretty_step_name or pretty_name(name)

        # Determine whether this step is still effectively "unknown" from the
        # catalog's point of view:
        #
        # - If it resolves only to a meta_type that requires subtypes
        #   (e.g., "ApiConnection" in meta_types with requires_subtypes: true),
        #   we treat it as unclassified and record a catalog miss.
        #
        # - If no labels were found at all for the resolved key, we also treat
        #   it as a catalog miss so humans can update the catalog accordingly.
        meta_info = meta_types.get(resolved) if isinstance(meta_types, dict) else None
        requires_subtypes = bool(meta_info.get("requires_subtypes")) if isinstance(meta_info, dict) else False

        is_unknown = False
        if requires_subtypes:
            is_unknown = True
        elif not labels:
            is_unknown = True

        if is_unknown and issues is not None:
            # Use the catalog-provided pretty_name if available; otherwise fall
            # back to the designer pretty_name() logic so the TODO file has a
            # friendly label for this step.
            miss_pretty = s.pretty_name or pretty_name(name)
            issues.add_catalog_miss(
                step_name=name,
                pretty_name=miss_pretty,
                raw_type=s.atype,
                resolved_key=resolved,
            )

        # 3) Verbose logging for human inspection
        if verbose:
            cat = pretty_cat if pretty_cat else "(unknown)"
            step_pretty = pretty_name(name)
            # Example:
            #   TYPE-MATCH: 'Fail - Prepare Log Data' is `Scope` from category 'Control'
            print(
                f"TYPE-MATCH: '{step_pretty}' is `{resolved}` from category '{cat}'",
                file=sys.stderr,
            )


def _catalog_resolve_aliases(
    catalog: Dict[str, Any],
    key: str,
    *,
    alias_section: str,
    max_hops: int = 16,
) -> str:
    """Resolve a key through a many-to-one alias map in the catalog.

    The catalog may optionally define:
      - catalog["type_aliases"]: {"If": "Condition", ...}
      - catalog["subtype_aliases"]: {"ApiConnection.Foo": "ApiConnection.Bar", ...}

    Aliases are resolved transitively up to `max_hops` and cycle-protected.
    If the alias section is missing or invalid, the key is returned unchanged.
    """
    aliases = catalog.get(alias_section) if isinstance(catalog, dict) else None
    if not isinstance(aliases, dict):
        return key

    current = str(key)
    seen = set()
    for _ in range(max_hops):
        if current in seen:
            # Cycle detected; stop and return the last known key.
            return current
        seen.add(current)

        nxt = aliases.get(current)
        if not nxt:
            return current

        current = str(nxt)

    # Max hops reached; return the best-effort resolved key.
    return current

def _catalog_resolve_type(
    catalog: dict,
    raw_key: str,
    issues: "AnalyzerUnknowns | None" = None,
) -> str:
    """
    Resolve the effective catalog key for a raw type/subtype string.

    This helper is used by `_attach_pretty_category` to choose which key
    should be treated as the "resolved" type key (e.g., a subtype such as
    "ApiConnection.SharePointWrite" or a base type like "ApiConnection").

    It is intentionally *pure* with respect to unknown tracking: it does
    not call `AnalyzerUnknowns.add_catalog_miss` or any similar reporting
    API. Catalog-miss reporting remains the responsibility of higher-level
    pipeline steps (e.g., `collect_registry`), which have full step
    context such as step name and pretty name.

    Args:
        catalog:
            Catalog dictionary as returned by `load_catalog`, expected to
            contain "types" and "subtypes" mappings.
        raw_key:
            Raw type/subtype key to resolve, for example:
            "ApiConnection", "ApiConnection.SharePointWrite", etc.
        issues:
            Currently unused. Kept for signature compatibility with callers
            that already pass an `AnalyzerUnknowns` instance. Future
            refactors may reintroduce reporting here if full step context
            is provided.

    Returns:
        A string key to use as the resolved type:
            - First applies catalog-defined aliases (if present) via:
              * catalog["type_aliases"]
              * catalog["subtype_aliases"]
            - Then, if the (aliased) key matches a subtype or type key in the catalog,
              returns the key unchanged.
            - Else, if the portion before the first "." matches a subtype or type key,
              returns that base key.
            - Otherwise, returns the original (aliased) key unchanged.
    """
    # Always work with a string representation.
    key = str(raw_key)

    # 0) Apply catalog-defined many-to-one aliases (if present).
    #    - type_aliases should map raw *base* types (e.g. "If") to canonical types (e.g. "Condition").
    #    - subtype_aliases should map raw subtypes (e.g. "ApiConnection.Email.Send") to canonical subtypes.
    key = _catalog_resolve_aliases(catalog, key, alias_section="type_aliases")
    key = _catalog_resolve_aliases(catalog, key, alias_section="subtype_aliases")

    if not isinstance(catalog, dict):
        # Catalog is unusable; just return the original key.
        return key

    types = catalog.get("types") or {}
    subtypes = catalog.get("subtypes") or {}

    # 1) Exact subtype or type match.
    if key in subtypes or key in types:
        return key

    # 2) Try a base key before the first dot, e.g. "ApiConnection" from
    #    "ApiConnection.SharePointWrite".
    if "." in key:
        base = key.split(".", 1)[0]
        if base in subtypes or base in types:
            return base

    # 3) No match; return the original key without recording a miss here.
    return key


# ──────────────────────────────────────────────────────────────────────────────
# Unknown type tracker (merged UnknownsGuard)
# ──────────────────────────────────────────────────────────────────────────────

class UnknownsGuard:
    """
    Tracks unknown or missing catalog entries for reporting at the end of analyzer run.
    """

    def __init__(self) -> None:
        self._unknowns: List[str] = []

    def add(self, type_name: str) -> None:
        if type_name not in self._unknowns:
            self._unknowns.append(type_name)

    def has_unknowns(self) -> bool:
        return bool(self._unknowns)

    def summary(self) -> str:
        if not self._unknowns:
            return ""
        header = "\nUnrecognized types (not found in catalog):"
        items = "\n".join(f" - {t}" for t in sorted(self._unknowns))
        return f"{header}\n{items}\n"

    def write_todo_file(self, out_dir: Path) -> None:
        """Write a todo.txt file with checkbox list of unknowns."""
        if not self._unknowns:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        todo_path = out_dir / "catalog_todo.txt"
        with todo_path.open("w", encoding="utf-8") as fh:
            fh.write("# TODO: Add these missing entries to plugin_catalog.json\n\n")
            for t in sorted(self._unknowns):
                fh.write(f"[ ] {t}\n")
        print(f"Wrote TODO list: {todo_path}")


# ──────────────────────────────────────────────────────────────────────────────
# Optional quick-test entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick manual test
    dummy = Path("plugin_catalog.json")
    cat = load_catalog(dummy) if dummy.exists() else {}
    print(resolve_labels("ApiConnection.SharePointWrite", "ApiConnection", cat))
