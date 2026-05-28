#!/usr/bin/env python3
# filename: populate.py
"""
populate.py v2.0.0 — Unpack file bundles to disk.

Two input modes:

  JSON bundle (text files):
    Keys are relative paths, values are file contents.
    All files written as UTF-8 text.

    python3 populate.py bundle.json --base ./output

  Tarball (binary-safe, any file type):
    Unpacks the archive to --base, then optionally runs a postflight
    shell script. Postflight receives the resolved base directory as $1.
    Populate exits with the postflight's exit code.

    python3 populate.py kit.tar.gz --base ./output
    python3 populate.py kit.tar.gz --base ./output --postflight postflight.sh

Exit codes:
    0   Success (or postflight returned 0)
    1   Postflight returned non-zero
    3   Fatal error (file not found, bad JSON, unreadable archive)
"""
from __future__ import annotations

__version__ = "2.0.0"

import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


def unpack_json_bundle(bundle_path: Path, base: Path) -> int:
    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"POP_E001: Failed to parse JSON bundle: {e}", file=sys.stderr)
        sys.exit(3)
    if not isinstance(data, dict):
        print("POP_E001: JSON bundle must be a top-level object (path → content).", file=sys.stderr)
        sys.exit(3)
    count = 0
    for rel_path, content in data.items():
        full = base / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        count += 1
    return count


def unpack_tarball(tar_path: Path, base: Path) -> int:
    if not tarfile.is_tarfile(tar_path):
        print(f"POP_E002: Not a valid tar archive: {tar_path}", file=sys.stderr)
        sys.exit(3)
    base.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:*") as tf:
            members = tf.getmembers()
            safe = []
            for m in members:
                m.name = m.name.lstrip("/")
                if ".." in Path(m.name).parts:
                    print(f"POP_W001: Skipping unsafe path: {m.name}", file=sys.stderr)
                    continue
                safe.append(m)
            tf.extractall(path=base, members=safe)
            return len(safe)
    except tarfile.TarError as e:
        print(f"POP_E002: Failed to extract archive: {e}", file=sys.stderr)
        sys.exit(3)


def run_postflight(postflight: Path, base: Path) -> int:
    if not postflight.exists():
        print(f"POP_E003: Postflight script not found: {postflight}", file=sys.stderr)
        sys.exit(3)
    postflight.chmod(postflight.stat().st_mode | 0o111)
    result = subprocess.run(
        [str(postflight.resolve()), str(base.resolve())],
        cwd=str(base.resolve()),
    )
    return result.returncode


def main():
    parser = argparse.ArgumentParser(
        description="populate — unpack file bundles to disk."
    )
    parser.add_argument("--version", action="version", version=f"populate {__version__}")
    parser.add_argument("input", help="JSON bundle (.json) or tarball (.tar.gz / .tgz / etc.)")
    parser.add_argument("--base", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--postflight", default=None,
                        help="Shell script to run after tarball extraction. "
                             "Receives base directory as $1. Ignored for JSON bundle mode.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without writing anything.")
    args = parser.parse_args()

    input_path = Path(args.input)
    base = Path(args.base).resolve()

    if not input_path.exists():
        print(f"POP_E001: Input not found: {input_path}", file=sys.stderr)
        sys.exit(3)

    suffix = "".join(input_path.suffixes).lower()
    is_tarball = suffix in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz") \
                 or tarfile.is_tarfile(input_path)
    is_json = not is_tarball

    if args.dry_run:
        mode = "tarball" if is_tarball else "JSON bundle"
        print(f"populate v{__version__} [DRY RUN]")
        print(f"  Input:  {input_path}  ({mode})")
        print(f"  Base:   {base}")
        if args.postflight:
            print(f"  Postflight: {args.postflight}")
        sys.exit(0)

    if is_json:
        base.mkdir(parents=True, exist_ok=True)
        count = unpack_json_bundle(input_path, base)
        print(f"populate v{__version__}: wrote {count} file(s) to {base}")
        if args.postflight:
            print("POP_W002: --postflight is ignored in JSON bundle mode.", file=sys.stderr)
        sys.exit(0)

    count = unpack_tarball(input_path, base)
    print(f"populate v{__version__}: extracted {count} file(s) to {base}")

    if args.postflight:
        postflight = Path(args.postflight)
        print(f"  Running postflight: {postflight.name}")
        rc = run_postflight(postflight, base)
        if rc != 0:
            print(f"  Postflight exited {rc}.", file=sys.stderr)
            sys.exit(1)
        print(f"  Postflight: OK")

    sys.exit(0)


if __name__ == "__main__":
    main()
