#!/usr/bin/env python3
# filename: manifest_from_csv.py
"""
manifest_from_csv.py v1.0.0 — CSV to Evangelist manifest converter.

Reads a multi-app CSV and writes one Evangelist manifest YAML per app.

CSV columns (RFC 4180, QUOTE_ALL recommended):
    app, json_path, value, checksum, note

    app       — target app label (e.g. FRESHMART-DEV)
    json_path — full root-to-leaf path: StepName.inputs.variables[N].value
                First dot segment is the step name. Everything after is field.
    value     — target value
    checksum  — optional sha256[:N]; leave blank to skip verification
    note      — optional human label shown in Evangelist report

Optional column:
    step      — if present, overrides first-dot splitting of json_path

Output: one {app}.yaml per unique app value in --output-dir.

Usage:
    python3 manifest_from_csv.py --csv changes.csv --output-dir manifests/

Consideration:
    Step names containing literal dots break first-dot splitting of json_path.
    Azure Logic App Portal does not use dots in step names, but if yours do,
    add an explicit 'step' column and the converter will use it directly.

Exit codes:
    0  All manifests written
    1  Some rows had validation errors (manifests still written for valid rows)
    3  Fatal error (file not found, bad CSV structure)
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import yaml

REQUIRED_COLUMNS = {"app", "json_path", "value"}


class CsvRow:
    __slots__ = ("app", "step", "field", "value", "checksum", "note", "line_num")
    def __init__(self, app, step, field, value, checksum, note, line_num):
        self.app = app; self.step = step; self.field = field
        self.value = value; self.checksum = checksum
        self.note = note; self.line_num = line_num

    def to_change_dict(self):
        entry = {"step": self.step, "field": self.field, "value": _coerce(self.value)}
        if self.checksum: entry["checksum"] = self.checksum
        if self.note: entry["note"] = self.note
        return entry


def _coerce(raw: str):
    if raw.lower() == "true": return True
    if raw.lower() == "false": return False
    try: return int(raw)
    except ValueError: pass
    return raw


def parse_csv(csv_path: Path):
    rows = []; errors = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("MFC_E001: CSV appears to be empty.")
        headers = {h.strip().lower() for h in reader.fieldnames}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(f"MFC_E001: CSV missing required columns: {sorted(missing)}.")
        has_step = "step" in headers
        for line_num, raw_row in enumerate(reader, start=2):
            row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items()}
            app = row.get("app", ""); json_path = row.get("json_path", "")
            value = row.get("value", "")
            checksum = row.get("checksum", "") or None
            note = row.get("note", "")
            if not app:
                errors.append(f"  Line {line_num}: 'app' empty — skipping."); continue
            if not json_path:
                errors.append(f"  Line {line_num}: 'json_path' empty — skipping."); continue
            if has_step and row.get("step"):
                step = row["step"]; field = json_path
            else:
                if "." not in json_path:
                    errors.append(
                        f"  Line {line_num}: json_path '{json_path}' has no dot — skipping.")
                    continue
                step, field = json_path.split(".", 1)
            if not step or not field:
                errors.append(f"  Line {line_num}: empty step or field — skipping."); continue
            rows.append(CsvRow(app=app, step=step, field=field, value=value,
                               checksum=checksum, note=note, line_num=line_num))
    if errors:
        print("MFC_W001: Validation warnings:", file=sys.stderr)
        for e in errors: print(e, file=sys.stderr)
    return rows, bool(errors)


def write_manifests(rows, output_dir: Path, verbose=False):
    by_app = defaultdict(list)
    for row in rows: by_app[row.app].append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for app_label, app_rows in sorted(by_app.items()):
        manifest = {"app": app_label, "changes": [r.to_change_dict() for r in app_rows]}
        out_path = output_dir / f"{app_label}.yaml"
        out_path.write_text(
            yaml.dump(manifest, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8")
        written.append(out_path)
        if verbose:
            print(f"  Wrote {len(app_rows)} change(s) → {out_path}", file=sys.stderr)
    return written


def main():
    parser = argparse.ArgumentParser(
        description="manifest_from_csv — convert multi-app CSV to Evangelist manifests.")
    parser.add_argument("--version", action="version", version=f"manifest_from_csv {__version__}")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--output-dir", default="manifests")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"MFC_E001: CSV not found: {csv_path}", file=sys.stderr); sys.exit(3)

    try:
        rows, had_errors = parse_csv(csv_path)
    except ValueError as e:
        print(str(e), file=sys.stderr); sys.exit(3)

    if not rows:
        print("MFC_E002: No valid rows found.", file=sys.stderr); sys.exit(3)

    written = write_manifests(rows, Path(args.output_dir), verbose=args.verbose)
    apps = sorted({r.app for r in rows})
    print(f"manifest_from_csv v{__version__}: {len(rows)} row(s), "
          f"{len(apps)} app(s) → {len(written)} manifest(s) in {args.output_dir}/")
    for p in written:
        count = sum(1 for r in rows if r.app == p.stem)
        print(f"  {p.name}  ({count} change(s))")
    sys.exit(1 if had_errors else 0)


if __name__ == "__main__":
    main()
