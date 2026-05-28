#!/usr/bin/env python3
# jsonc_config.py
"""
Shared helpers for loading JSON/JSONC configuration files.

This module provides:

- strip_json_comments(text): best-effort removal of // and /* */ comments.
- deep_merge_dicts(base, override): recursive dict merge with override winning.
- load_jsonc_config(path, default_config): load a JSON/JSONC file, merge it
  into a default configuration, and return the merged result.

Each script owns its own DEFAULT_CONFIG and a thin load_config(...) wrapper,
and delegates the parsing/merging mechanics to this module.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict


# ─────────────────────────────────────────────────────────────────────────────
# JSONC stripping
# ─────────────────────────────────────────────────────────────────────────────


def strip_json_comments(text: str) -> str:
    """
    Best-effort removal of // and /* ... */ comments from JSONC text.

    This is not a full JSON parser but is sufficient for typical config files
    where comments do not appear inside string literals.
    """
    # Remove /* ... */ blocks
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove // ... to end of line
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Deep merge helpers
# ─────────────────────────────────────────────────────────────────────────────


def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merge two dicts, with values from ``override`` winning.

    Nested dictionaries are merged depth-first; other values are overwritten.
    ``base`` is not mutated; a new dict is returned.
    """
    result: Dict[str, Any] = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(val, dict)
        ):
            result[key] = deep_merge_dicts(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────


def load_jsonc_config(config_path: Path, default_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Load a JSON/JSONC config file and merge it into a default configuration.

    Behaviour
    ---------
    - If ``config_path`` exists and is a file:
        * Comments are stripped using strip_json_comments(...).
        * The cleaned string is parsed as JSON.
        * If the result is a dict, it is deep-merged into ``default_config``.
    - If the file is missing or invalid:
        * ``default_config`` (deep-copied) is returned unchanged.

    Parameters
    ----------
    config_path:
        Path to a JSON or JSONC configuration file.
    default_config:
        Default configuration dict to use as the base.

    Returns
    -------
    Dict[str, Any]
        Final configuration with user overrides applied where valid.
    """
    cfg = copy.deepcopy(default_config)

    if not config_path.is_file():
        return cfg

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except Exception:
        # On any IO problem, fall back to defaults.
        return cfg

    cleaned = strip_json_comments(raw_text)
    try:
        user_cfg = json.loads(cleaned)
    except Exception:
        # On parse failure, fall back to defaults.
        return cfg

    if isinstance(user_cfg, dict):
        cfg = deep_merge_dicts(cfg, user_cfg)

    return cfg
