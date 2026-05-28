#!/usr/bin/env python3
# filename: checksum.py
"""
Checksum helpers for Logic App structural fingerprinting.

Provides a standardized SHA-256-based fingerprinting primitive used by
Cartographer's --detailed mode and consumable by any downstream tool
that needs deterministic value hashing.

All fingerprints are SHA-256 hex, truncated to a configurable length
(default 12 characters). This is the canonical fingerprint length for
the Cartographer ecosystem.

Convention note:
  - Cartographer/checksum.py:  default chop=12
  - la_lib._hash_value:        hardcoded 16
  - recon.py inline hash:      hardcoded 12
  - vitals.yaml (manual):      10
  This module does not unify those. It provides its own consistent
  convention for Cartographer output. The chop parameter lets callers
  match any convention they need.

Public API:
  fingerprint(value, chop=12)
      Core primitive. SHA-256 of a Python value, truncated.

  fingerprint_canonical(obj, chop=12)
      SHA-256 of a canonical JSON serialization of a dict/list.

  fingerprint_run_after(run_after_dict, chop=12)
      RunAfter-only fingerprint. Returns zero-hash for empty/missing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


# ─────────────────────────────────────────────────────────────────────────────
# Core primitive
# ─────────────────────────────────────────────────────────────────────────────

def fingerprint(
    value: Any,
    chop: int = 12,
) -> str:
    """
    SHA-256 fingerprint of a Python value, truncated to `chop` hex chars.

    Inputs:
      value: Any Python primitive (str, int, float, bool, None).
      chop: Number of hex characters to keep (default 12).

    Outputs:
      Hex string of length `chop`.

    Normalization:
      - Strings hash their UTF-8 bytes directly.
      - Other primitives hash via repr() to disambiguate
        (e.g., False vs "false", None vs "None", 0 vs "0").
    """
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = repr(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:chop]


# ─────────────────────────────────────────────────────────────────────────────
# Structural fingerprints
# ─────────────────────────────────────────────────────────────────────────────

def fingerprint_canonical(
    obj: Any,
    chop: int = 12,
) -> str:
    """
    SHA-256 fingerprint of a canonical JSON serialization.

    Inputs:
      obj: Any JSON-serializable object (dict, list, scalar).
      chop: Truncation length.

    Outputs:
      Hex fingerprint of the canonical form (sorted keys, no whitespace).

    Use for whole-step or whole-block fingerprinting where the exact
    JSON structure matters, not just individual leaf values.
    """
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:chop]


def fingerprint_run_after(
    run_after: Any,
    chop: int = 12,
) -> str:
    """
    Fingerprint of a step's runAfter dict only.

    Inputs:
      run_after: The step's runAfter value (dict, or None/missing).
      chop: Truncation length.

    Outputs:
      Hex fingerprint. Returns a zero-hash for empty/missing runAfter.
    """
    if not isinstance(run_after, dict) or not run_after:
        return "0" * chop
    return fingerprint_canonical(run_after, chop)
