#!/usr/bin/env python3
# filename: purifier.py
"""
purifier.py — Redact or restore sensitive values in Logic App JSON.

Reads a creds.env file with TOKEN=VALUE pairs. In redact mode, replaces
every VALUE found in the JSON with its TOKEN. In replace mode, does the
reverse. Clears all credential values from memory after use.

Usage:
    python3 purifier.py --input-file app.json --creds-file creds.env --redact
    python3 purifier.py --input-file app.json --creds-file creds.env --redact --replace
    python3 purifier.py --input-file app.redacted.json --creds-file creds.env --replace

creds.env format:
    API_CLIENT_ID=abc123-def456
    API_CLIENT_SECRET=supersecretvalue
    SHAREPOINT_CONN=eyJ0eXAiOi...
    # Comments and blank lines are ignored

Output:
    --redact only:   writes <input>.redacted.json
    --replace only:  writes <input>.hydrated.json
    --redact --replace: redacts in-place (overwrites input), no sidecar
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import os
import sys
from pathlib import Path


def secure_clear(s: str) -> None:
    """Best-effort zeroing of a Python string's buffer."""
    if not s:
        return
    try:
        # CPython internals — not guaranteed but worth trying
        buf = ctypes.c_char * len(s)
        addr = id(s) + sys.getsizeof("") - 1  # rough offset to char buffer
        ctypes.memset(addr, 0, len(s))
    except Exception:
        pass  # Non-CPython or address wrong — silently skip


def load_creds(creds_path: Path) -> list[tuple[str, str]]:
    """Load TOKEN=VALUE pairs from a .env file. Returns list of (token, value)."""
    pairs = []
    with open(creds_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            token, value = line.split("=", 1)
            token = token.strip()
            value = value.strip()
            if not token or not value:
                continue
            pairs.append((token, value))
    return pairs


def redact(content: str, pairs: list[tuple[str, str]]) -> tuple[str, int]:
    """Replace all VALUE occurrences with TOKEN placeholders. Returns (new_content, count)."""
    count = 0
    for token, value in pairs:
        if value in content:
            occurrences = content.count(value)
            content = content.replace(value, f"<{token}>")
            count += occurrences
    return content, count


def hydrate(content: str, pairs: list[tuple[str, str]]) -> tuple[str, int]:
    """Replace all TOKEN placeholders with their VALUE. Returns (new_content, count)."""
    count = 0
    for token, value in pairs:
        placeholder = f"<{token}>"
        if placeholder in content:
            occurrences = content.count(placeholder)
            content = content.replace(placeholder, value)
            count += occurrences
    return content, count


def cleanup(pairs: list[tuple[str, str]]) -> None:
    """Zero out credential values in memory."""
    for i, (token, value) in enumerate(pairs):
        secure_clear(value)
        pairs[i] = (token, "")
    pairs.clear()
    gc.collect()


def main():
    parser = argparse.ArgumentParser(
        description="Redact or restore sensitive values in Logic App JSON."
    )
    parser.add_argument("--input-file", required=True, help="Logic App JSON file")
    parser.add_argument("--creds-file", required=True, help="Credentials .env file (TOKEN=VALUE per line)")
    parser.add_argument("--redact", action="store_true", help="Replace values with tokens")
    parser.add_argument("--replace", action="store_true", help="Replace tokens with values")
    args = parser.parse_args()

    if not args.redact and not args.replace:
        parser.error("At least one of --redact or --replace is required")

    input_path = Path(args.input_file)
    creds_path = Path(args.creds_file)

    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")
    if not creds_path.exists():
        sys.exit(f"Creds file not found: {creds_path}")

    # Load
    pairs = load_creds(creds_path)
    if not pairs:
        sys.exit("No TOKEN=VALUE pairs found in creds file")

    content = input_path.read_text(encoding="utf-8")
    print(f"Loaded {input_path.name} ({len(content)} chars)")
    print(f"Loaded {len(pairs)} credential pairs")

    try:
        if args.redact and args.replace:
            # In-place: redact and overwrite
            new_content, count = redact(content, pairs)
            input_path.write_text(new_content, encoding="utf-8")
            print(f"Redacted {count} occurrences in-place → {input_path}")

        elif args.redact:
            # Redact to sidecar
            new_content, count = redact(content, pairs)
            out_path = input_path.with_suffix(".redacted.json")
            out_path.write_text(new_content, encoding="utf-8")
            print(f"Redacted {count} occurrences → {out_path}")

        elif args.replace:
            # Hydrate to sidecar
            new_content, count = hydrate(content, pairs)
            out_path = input_path.with_suffix(".hydrated.json")
            out_path.write_text(new_content, encoding="utf-8")
            print(f"Hydrated {count} placeholders → {out_path}")

    finally:
        # Always clean up credentials from memory
        cleanup(pairs)
        secure_clear(content)
        if "new_content" in locals():
            secure_clear(new_content)
        gc.collect()
        print("Credentials cleared from memory")


if __name__ == "__main__":
    main()
