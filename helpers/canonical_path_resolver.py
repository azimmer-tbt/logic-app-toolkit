#!/usr/bin/env python3
# filename: canonical_path_resolver.py
"""
Canonical path resolver for Logic App JSON structures.

Resolves orthodox.yaml-style dot-notation paths with bracket syntax
for name-based array lookups. Used by Surgeon (and future Evangelist)
to navigate to specific fields in a Logic App JSON tree.

Path format:
    definition.actions.CONTROL_PANEL.inputs.variables[mobile_or_mac].value

Bracket notation:
    variables[mobile_or_mac] means: in the 'variables' array, find the
    object where name == "mobile_or_mac". This is Portal-proof — array
    indices are not stable across Azure Portal saves.

Public API:
    resolve(doc, path)
        Navigate to a leaf value. Returns (parent, key) so the caller
        can read or write the value via parent[key].

    resolve_value(doc, path)
        Convenience wrapper: returns the value at path.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple, Union


# ─────────────────────────────────────────────────────────────────────────────
# Path tokenizer
# ─────────────────────────────────────────────────────────────────────────────

# Matches bracket segments: varname[lookup_key]
_BRACKET_RE = re.compile(r'^([^\[]+)\[([^\]]+)\]$')


def _tokenize(path: str) -> List[Union[str, Tuple[str, str]]]:
    """
    Split a dot-notation path into tokens.

    Inputs:
        path: Dot-notation path string, e.g.
              "definition.actions.STEP.inputs.variables[var_name].value"

    Outputs:
        List of tokens. Plain segments are strings. Bracket segments are
        (array_key, lookup_name) tuples.

        Example:
            ["definition", "actions", "STEP", "inputs",
             ("variables", "var_name"), "value"]
    """
    tokens: List[Union[str, Tuple[str, str]]] = []
    for segment in path.split("."):
        match = _BRACKET_RE.match(segment)
        if match:
            tokens.append((match.group(1), match.group(2)))
        else:
            tokens.append(segment)
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# Name-based array lookup
# ─────────────────────────────────────────────────────────────────────────────


def _find_by_name(arr: list, name: str) -> int:
    """
    Find an object in an array where obj["name"] == name.

    Inputs:
        arr:  List of dicts (e.g. Logic App variables array).
        name: The name value to search for.

    Outputs:
        Integer index of the matching object.

    Raises:
        ValueError: If no object with that name exists.
        TypeError:  If arr is not a list.
    """
    if not isinstance(arr, list):
        raise TypeError(f"Expected list for name-based lookup, got {type(arr).__name__}")
    for i, item in enumerate(arr):
        if isinstance(item, dict) and item.get("name") == name:
            return i
    raise ValueError(f"No object with name '{name}' found in array of {len(arr)} items")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def resolve(doc: Dict[str, Any], path: str) -> Tuple[Any, Union[str, int]]:
    """
    Navigate a JSON tree to a leaf. Returns (parent, key) for read/write.

    Inputs:
        doc:  Root JSON object (the full Logic App export).
        path: Orthodox-style dot-notation path with optional bracket lookups.

    Outputs:
        (parent, key) tuple where parent[key] is the target value.
        For bracket lookups, key is the integer index into the array.

    Raises:
        KeyError:   If a dict key in the path doesn't exist.
        ValueError: If a name-based lookup finds no match.
        TypeError:  If the path expects a dict/list but finds a scalar.
    """
    tokens = _tokenize(path)
    if not tokens:
        raise ValueError(f"Empty path: '{path}'")

    current = doc

    # Navigate all tokens except the last — we need to return (parent, key).
    for token in tokens[:-1]:
        if isinstance(token, tuple):
            array_key, lookup_name = token
            if not isinstance(current, dict) or array_key not in current:
                raise KeyError(f"Key '{array_key}' not found in {type(current).__name__}")
            arr = current[array_key]
            idx = _find_by_name(arr, lookup_name)
            current = arr[idx]
        else:
            if not isinstance(current, dict):
                raise TypeError(
                    f"Expected dict to navigate key '{token}', "
                    f"got {type(current).__name__}"
                )
            if token not in current:
                raise KeyError(f"Key '{token}' not found")
            current = current[token]

    # Handle the final token.
    last = tokens[-1]
    if isinstance(last, tuple):
        array_key, lookup_name = last
        if not isinstance(current, dict) or array_key not in current:
            raise KeyError(f"Key '{array_key}' not found in {type(current).__name__}")
        arr = current[array_key]
        idx = _find_by_name(arr, lookup_name)
        return arr, idx
    else:
        if not isinstance(current, dict):
            raise TypeError(
                f"Expected dict for final key '{last}', "
                f"got {type(current).__name__}"
            )
        return current, last


def resolve_value(doc: Dict[str, Any], path: str) -> Any:
    """
    Convenience: return the value at path.

    Inputs:
        doc:  Root JSON object.
        path: Orthodox-style dot-notation path.

    Outputs:
        The value at the resolved path.
    """
    parent, key = resolve(doc, path)
    return parent[key]
