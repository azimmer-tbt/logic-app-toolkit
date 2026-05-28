#!/usr/bin/env python3
# filenames_config.py
"""
Filename configuration helper for Logic App Documentation Toolkit.

This module centralizes the logic for reading `filenames.json` from the
project/script root (with the per-flow root as a fallback) and resolving
actual filenames for well-known artifacts such as:

    __architecture.md
    __logging.md
    __override_rules.json
    __prefix.md
    __purpose_and_business_reason.md
    __related_policy.md
    __suffix.md
    __summary.md
    __testing.md
    __variables.md
    __pathways.md
    _vitals.json
    rules.json
    summary.json
    variables.json

The intent is that all stages (1: Analyzer, 2: Post-Processor, 3: Renderer)
import this helper instead of hard-coding filenames in multiple places.

Usage pattern (Stage N):

    root = Path(args.input)
    vitals_path = get_vitals_path(root)
    summary_md_path = get_summary_markdown_path(root)
    variables_md_path = get_variables_markdown_path(root)

Internally, this module:

- Supports JSONC-style comments in filenames.json (// and /* */).
- Ignores entries with non-string keys/values and unknown labels.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

#: Default filename labels and their corresponding filenames.
LABEL_DEFAULTS = {
    # Core JSON metadata
    "VITALS_JSON": "_vitals.json",
    "SUMMARY_JSON": "summary.json",
    "VARIABLES_JSON": "variables.json",
    "RULES_JSON": "rules.json",
    "OVERRIDE_RULES_JSON": "override_rules.json",
    "PATHWAYS_JSON": "pathways.json",
    "FLOW_MODEL_JSON": "flow_model.json",
    "OVERRIDES_PATHWAYS_JSON": "override_pathways.json",

    # Markdown sections
    "SUMMARY_MD": "__summary.md",
    "VARIABLES_MD": "__variables.md",
    "PATHWAYS_MD": "__pathways.md",
    "COMPARES_MD": "__compares.md",
    "COMPARES_DIR": "_Compares",
    "VARIANTS_MD": "__variants.md",
    "PLATFORM_OBJECTS_MD": "Platform_Objects.md",
    "ARCHITECTURE_MD": "__architecture.md",
    "LOGGING_MD": "__logging.md",
    "TESTING_MD": "__testing.md",
    "PREFIX_MD": "__prefix.md",
    "PURPOSE_AND_BUSINESS_REASON_MD": "__purpose_and_business_reason.md",
    "RELATED_POLICY_MD": "__related_policy.md",
    "SUFFIX_MD": "__suffix.md",

    # Misc
    "TRIGGERS_JSON": "_Triggers.json",
    "HISTORY_CSV": "history.csv",
    "IDENTITY_TXT": "__identity.txt",
}

#: Inverted map from default filenames back to labels, for compatibility.
FILENAME_TO_LABEL: Dict[str, str] = {
    filename: label for label, filename in LABEL_DEFAULTS.items()
}

# -----------------------------------------------------------------------------
# Internal helpers (JSONC handling + overrides loading)
# -----------------------------------------------------------------------------

def _strip_jsonc_comments(text: str) -> str:
    """
    Remove simple JSONC-style comments from a string.

    Supports:
    - Line comments starting with //
    - Block comments delimited by /* ... */

    This is intentionally conservative and does not try to be a full parser;
    it assumes filenames.json is small and relatively simple.
    """
    # Remove /* ... */ blocks (greedy across newlines).
    # We avoid importing `re` if possible, but given the small size this is fine.
    import re

    block_comment_pattern = re.compile(r"/\*.*?\*/", re.DOTALL)
    no_block = re.sub(block_comment_pattern, "", text)

    # Strip // comments to end of line.
    stripped_lines = []
    for line in no_block.splitlines():
        idx = line.find("//")
        if idx != -1:
            line = line[:idx]
        stripped_lines.append(line)

    return "\n".join(stripped_lines)


def _format_json_decode_error(
    path: Path,
    raw_text: str,
    cleaned_text: str,
    exc: json.JSONDecodeError,
    *,
    context_lines: int = 2,
) -> str:
    """Return a human-friendly JSON/JSONC parse error message.

    This prints the file path, the line/column, a small snippet of surrounding
    lines, and a caret pointing at the error column.

    Notes:
    - `exc.lineno`/`exc.colno` refer to the string passed to `json.loads()`.
      Since we parse `cleaned_text` (JSONC comments stripped), the snippet is
      taken from that cleaned content.
    - We also include a hint that comments/trailing commas are common causes.
    """
    # Defensive: JSONDecodeError uses 1-based line numbers.
    lineno = max(int(getattr(exc, "lineno", 1)), 1)
    colno = max(int(getattr(exc, "colno", 1)), 1)

    lines = cleaned_text.splitlines()
    total = len(lines)

    start = max(lineno - context_lines - 1, 0)
    end = min(lineno + context_lines, total)

    # Build snippet with line numbers aligned.
    width = len(str(end))
    snippet_parts: list[str] = []
    for i in range(start, end):
        line_num = i + 1
        prefix = f"{line_num:>{width}} | "
        snippet_parts.append(prefix + lines[i])
        if line_num == lineno:
            caret_pad = " " * (len(prefix) + max(colno - 1, 0))
            snippet_parts.append(caret_pad + "^")

    snippet = "\n".join(snippet_parts) if snippet_parts else "(no content)"

    return (
        f"CONFIG_PARSE_ERROR: Failed to parse filenames config at '{path}'.\n"
        f"Reason: {exc.msg} (line {lineno}, column {colno})\n\n"
        f"--- Snippet (comments stripped before parsing) ---\n{snippet}\n"
        f"--- End snippet ---\n\n"
        "Hints:\n"
        "- Missing commas and trailing commas are common causes.\n"
        "- This file supports JSONC comments (// and /* */), which are stripped before parsing.\n"
    )


@lru_cache(maxsize=32)
def _load_label_overrides(root: Path) -> Dict[str, str]:
    """
    Load label-based filename overrides from filenames.json.

    Search order:
      1. Project/script root (directory above this helpers module).
      2. The given flow root.

    The JSON is expected to be a mapping of:
        "VITALS_JSON": "_vitals.json",
        "SUMMARY_MD": "__flow_summary.md",
        ...

    Keys must be known labels in LABEL_DEFAULTS. Unknown labels are ignored.
    """
    # Determine the project/script root: parent of the helpers/ directory.
    script_root = Path(__file__).resolve().parent.parent

    candidates = [
        script_root / "filenames.json",
        script_root / "filenames.jsonc",
        Path(root) / "filenames.json",
        Path(root) / "filenames.jsonc",
    ]

    config_path: Optional[Path] = None
    for candidate in candidates:
        if candidate.is_file():
            config_path = candidate
            break

    if config_path is None:
        return {}

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        cleaned = _strip_jsonc_comments(raw_text)
        data = json.loads(cleaned)
    except (OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            message = _format_json_decode_error(config_path, raw_text, cleaned, exc)
            raise RuntimeError(message) from exc

        # OSError: surface as a hard error so runs cannot silently ignore broken overrides.
        raise RuntimeError(
            f"CONFIG_READ_ERROR: Failed to read filenames config at '{config_path}': {exc}"
        ) from exc

    if not isinstance(data, Mapping):
        # We expect a simple object mapping of label -> filename (after JSONC comment stripping).
        raise RuntimeError(
            f"ERROR: filenames config at '{config_path}' must be a JSON object "
            "mapping label -> filename (e.g. {\"VITALS_JSON\": \"1vitals.json\"})."
        )

    overrides: Dict[str, str] = {}
    for key, val in data.items():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        label = key.strip()
        filename = val.strip()
        if not label or not filename:
            continue
        if label in LABEL_DEFAULTS:
            overrides[label] = filename

    return overrides


def _resolve_label(root: Path, label: str) -> str:
    """
    Resolve a filename for a given label (e.g. 'VITALS_JSON') at this root.

    If filenames.json overrides it, that value is used; otherwise
    LABEL_DEFAULTS provides the filename.
    """
    if label not in LABEL_DEFAULTS:
        raise KeyError(f"Unknown filename label: {label!r}")
    overrides = _load_label_overrides(root)
    return overrides.get(label, LABEL_DEFAULTS[label])


def resolve_filename(root: Path, name: str) -> str:
    """
    Resolve a filename for this pipeline root.

    This helper is label-aware but also supports legacy calls that pass a
    default filename instead of a label:

        - If `name` matches a known label (e.g. 'VITALS_JSON'), that label
          is resolved directly.
        - Else if `name` matches a known default filename, it is mapped back
          to its label and resolved.
        - Otherwise, `name` is returned unchanged.

    Callers that manage their own Path joining can use:

        vitals_name = resolve_filename(root, "VITALS_JSON")
        vitals_path = root / vitals_name
    """
    # Preferred: treat `name` as a label.
    if name in LABEL_DEFAULTS:
        return _resolve_label(root, name)

    # Backwards compatibility: treat `name` as a default filename and map
    # it to its known label, if any.
    if name in FILENAME_TO_LABEL:
        label = FILENAME_TO_LABEL[name]
        return _resolve_label(root, label)

    # Unknown name: leave unchanged.
    return name


# -----------------------------------------------------------------------------
# Public resolution helpers (names + paths)
# -----------------------------------------------------------------------------

def get_vitals_json_path(root: Path) -> Path:
    """
    Return the Path for the vitals file for this pipeline root.

    Label: VITALS_JSON (default filename: `_vitals.json`).
    """
    return Path(root) / resolve_filename(root, "VITALS_JSON")


# Backwards-compatible alias; older code may still call get_vitals_path.
def get_vitals_path(root: Path) -> Path:
    return get_vitals_json_path(root)


def get_architecture_markdown_path(root: Path) -> Path:
    """
    Return the Path for the architecture markdown section.

    Label: ARCHITECTURE_MD (default filename: `__architecture.md`).
    """
    return Path(root) / resolve_filename(root, "ARCHITECTURE_MD")


def get_logging_markdown_path(root: Path) -> Path:
    """
    Return the Path for the logging markdown section.

    Label: LOGGING_MD (default filename: `__logging.md`).
    """
    return Path(root) / resolve_filename(root, "LOGGING_MD")


def get_override_rules_json_path(root: Path) -> Path:
    """
    Return the Path for the override rules JSON file.

    Label: OVERRIDE_RULES_JSON (default filename: `override_rules.json`).
    """
    return Path(root) / resolve_filename(root, "OVERRIDE_RULES_JSON")


# Backwards-compatible alias for older name.
def get_override_rules_path(root: Path) -> Path:
    return get_override_rules_json_path(root)


def get_prefix_markdown_path(root: Path) -> Path:
    """
    Return the Path for the prefix markdown section.

    Label: PREFIX_MD (default filename: `__prefix.md`).
    """
    return Path(root) / resolve_filename(root, "PREFIX_MD")



def get_purpose_and_business_reason_markdown_path(root: Path) -> Path:
    """
    Return the Path for the Purpose and Business Reason markdown section.

    Label: PURPOSE_AND_BUSINESS_REASON_MD (default filename: `__purpose_and_business_reason.md`).
    """
    return Path(root) / resolve_filename(root, "PURPOSE_AND_BUSINESS_REASON_MD")


def get_related_policy_markdown_path(root: Path) -> Path:
    """
    Return the Path for the related policy markdown section.

    Label: RELATED_POLICY_MD (default filename: `__related_policy.md`).
    """
    return Path(root) / resolve_filename(root, "RELATED_POLICY_MD")


def get_suffix_markdown_path(root: Path) -> Path:
    """
    Return the Path for the suffix markdown section.

    Label: SUFFIX_MD (default filename: `__suffix.md`).
    """
    return Path(root) / resolve_filename(root, "SUFFIX_MD")


def get_summary_markdown_path(root: Path) -> Path:
    """
    Return the Path for the summary markdown section.

    Label: SUMMARY_MD (default filename: `__summary.md`).
    """
    return Path(root) / resolve_filename(root, "SUMMARY_MD")


def get_testing_markdown_path(root: Path) -> Path:
    """
    Return the Path for the testing markdown section.

    Label: TESTING_MD (default filename: `__testing.md`).
    """
    return Path(root) / resolve_filename(root, "TESTING_MD")


def get_variables_markdown_path(root: Path) -> Path:
    """
    Return the Path for the variables markdown section.

    Label: VARIABLES_MD (default filename: `__variables.md`).
    """
    return Path(root) / resolve_filename(root, "VARIABLES_MD")


def get_pathways_markdown_path(root: Path) -> Path:
    """
    Return the Path for the pathways markdown section.

    Label: PATHWAYS_MD (default filename: `__pathways.md`).
    """
    return Path(root) / resolve_filename(root, "PATHWAYS_MD")


def get_platform_objects_markdown_path(root: Path) -> Path:
    """Return the Path for the platform objects markdown section.

    Label: PLATFORM_OBJECTS_MD (default filename: `Platform_Objects.md`).
    """
    return Path(root) / resolve_filename(root, "PLATFORM_OBJECTS_MD")


def get_compares_markdown_path(flow_root: Path) -> Path:
    """
    Resolve the flow-level compares markdown path.

    Default: __compares.md
    Overrideable via filenames.json using key: COMPARES_MD
    """
    return Path(flow_root) / resolve_filename(flow_root, "COMPARES_MD")

def get_variants_markdown_path(flow_root: Path) -> Path:
    """
    Resolve the flow-level variants markdown path.

    Default: __variants.md
    Overrideable via filenames.json using key: VARIANTS_MD
    """
    return Path(flow_root) / resolve_filename(flow_root, "VARIANTS_MD")

def get_compares_dir(flow_root: Path, flow_prefix: str = "") -> Path:
    """
    Resolve the compares directory for this flow.

    Resolution order:
      1) filenames.json COMPARES_DIR override — used as-is if set
      2) <flow_prefix>_Compares  — preferred convention when flow_prefix supplied
      3) _Compares suffix default (filenames.json COMPARES_DIR default)

    The returned path is not guaranteed to exist; callers should check.
    """
    root = Path(flow_root)
    override = resolve_filename(root, "COMPARES_DIR")

    # If the user has overridden COMPARES_DIR in filenames.json, use it directly.
    if override != "_Compares":
        return root / override

    # Preferred: <flow_prefix>_Compares (e.g. FRESHMART-DEV-PRICES_Compares)
    if flow_prefix:
        candidate = root / f"{flow_prefix}_Compares"
        if candidate.exists() and candidate.is_dir():
            return candidate

    # Fallback: bare _Compares or the default suffix
    fallback = root / "_Compares"
    if fallback.exists() and fallback.is_dir():
        return fallback

    # Final fallback: return the prefixed path even if it doesn't exist yet
    if flow_prefix:
        return root / f"{flow_prefix}_Compares"
    return root / "_Compares"


def get_rules_json_path(root: Path) -> Path:
    """
    Return the Path for the rules JSON file.

    Label: RULES_JSON (default filename: `rules.json`).
    """
    return Path(root) / resolve_filename(root, "RULES_JSON")


def get_summary_json_path(root: Path) -> Path:
    """
    Return the Path for the summary JSON metadata file.

    Label: SUMMARY_JSON (default filename: `summary.json`).
    """
    return Path(root) / resolve_filename(root, "SUMMARY_JSON")


def get_variables_json_path(root: Path) -> Path:
    """
    Return the Path for the variables JSON metadata file.

    Label: VARIABLES_JSON (default filename: `variables.json`).
    """
    return Path(root) / resolve_filename(root, "VARIABLES_JSON")


def get_pathways_json_path(root: Path) -> Path:
    """
    Return the Path for the pathways JSON snapshot.

    Label: PATHWAYS_JSON (default filename: `pathways.json`).
    """
    return Path(root) / resolve_filename(root, "PATHWAYS_JSON")


def get_flow_model_json_path(root: Path) -> Path:
    """
    Return the Path for the flow model JSON artifact.

    Label: FLOW_MODEL_JSON (default filename: `flow_model.json`).
    """
    return Path(root) / resolve_filename(root, "FLOW_MODEL_JSON")


def get_pathways_overrides_json_path(root: Path) -> Path:
    """
    Return the Path for the pathways overrides JSON file.

    Label: OVERRIDES_PATHWAYS_JSON (default filename: `override_pathways.json`).
    """
    return Path(root) / resolve_filename(root, "OVERRIDES_PATHWAYS_JSON")


def get_triggers_json_name(root: Path) -> str:
    """
    Return the effective filename for the triggers JSON card.

    Label: TRIGGERS_JSON (default filename: `_Triggers.json`).
    """
    return resolve_filename(root, "TRIGGERS_JSON")


def get_triggers_json_path(root: Path) -> Path:
    """
    Return the Path for the triggers JSON card.

    Label: TRIGGERS_JSON (default filename: `_Triggers.json`).
    """
    return Path(root) / resolve_filename(root, "TRIGGERS_JSON")


def get_history_csv_name(root: Path) -> str:
    """
    Return the effective filename for the history CSV.

    Label: HISTORY_CSV (default filename: `history.csv`).
    """
    return resolve_filename(root, "HISTORY_CSV")


def get_history_csv_path(root: Path) -> Path:
    """
    Return the Path for the history CSV.

    Label: HISTORY_CSV (default filename: `history.csv`).
    """
    return Path(root) / resolve_filename(root, "HISTORY_CSV")

def get_identity_txt_path(root: Path) -> Path:
    """
    Return the Path for the pipeline-root identity metadata file.

    Label: IDENTITY_TXT (default filename: `__identity.txt`).
    """
    return Path(root) / resolve_filename(root, "IDENTITY_TXT")


# -----------------------------------------------------------------------------
# Debug / CLI helper
# -----------------------------------------------------------------------------

def _debug_print_overrides(root: Path) -> None:
    """
    Print effective filenames for this root, for debugging from the CLI.

    Example:

        python filenames_config.py /path/to/flow_root
    """
    overrides = _load_label_overrides(root)
    print(f"Root: {root}")
    print("Overrides from filenames.json (by label):")
    if not overrides:
        print("  (none)")
    else:
        for label in sorted(overrides):
            print(f"  {label!r} -> {overrides[label]!r}")

    print("\nEffective filenames (label -> filename):")
    for label in sorted(LABEL_DEFAULTS):
        effective = resolve_filename(root, label)
        print(f"  {label!r} -> {effective!r}")


def main(argv: Optional[Iterable[str]] = None) -> int:
    """
    Simple CLI entrypoint for debugging filenames.json resolution.

    Usage:
        filenames_config.py /path/to/flow_root
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Debug filenames.json resolution for a flow root."
    )
    parser.add_argument(
        "root",
        metavar="ROOT",
        help="Path to the flow root directory.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).expanduser().resolve()

    try:
        _debug_print_overrides(root)
    except RuntimeError as exc:
        # Surface configuration errors cleanly for CLI use.
        print(str(exc), file=sys.stderr)
        return 1

    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
    