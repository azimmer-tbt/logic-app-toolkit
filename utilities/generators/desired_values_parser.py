#!/usr/bin/env python3
# filename: desired_values_parser.py
"""
Parser for the v2 desired_values file format.

Pure parsing — no Logic App knowledge, no SHAs, no I/O beyond
reading the file. See SYNTAX.md for the format specification.

Public API:
    parse_desired_values(filepath) -> DesiredValues
    extract_backtick(text) -> Optional[str]
    extract_backticked_names(header_line) -> List[str]

CLI usage:
    python3 desired_values_parser.py --input-desired-values path/to/file.v2.txt

Exit codes:
    0 = parsed OK
    1 = file not found or parse error
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Field:
    """One pipe row from the desired_values file."""
    name: str               # Column C: backticked code field name
    value: str              # Column D: backticked value (literal or file: ref)
    is_file_ref: bool       # True if value starts with 'file:'
    file_path: Optional[str]  # Resolved path if is_file_ref, else None
    line_number: int        # For error reporting


@dataclass
class Section:
    """One section header + its pipe rows."""
    description: str        # Full header line text
    actions: List[str]      # Backticked action names from parentheses
    fields: List[Field]
    line_number: int


@dataclass
class DesiredValues:
    """Parsed result of a desired_values file."""
    app_name: str           # From "Desired Values: APP_NAME" line
    sections: List[Section]
    raw_lines: List[str]    # For error context


# ─────────────────────────────────────────────────────────────────────────────
# Extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_backtick(text: str) -> Optional[str]:
    """Pull content between first backtick pair. None if no backticks."""
    match = re.search(r'`([^`]+)`', text)
    return match.group(1) if match else None


def extract_backticked_names(header_line: str) -> List[str]:
    """Extract all backticked names from parenthetical in header."""
    paren_match = re.search(r'\(([^)]+)\)', header_line)
    if not paren_match:
        return []
    inner = paren_match.group(1)
    return re.findall(r'`([^`]+)`', inner)


# ─────────────────────────────────────────────────────────────────────────────
# Core parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_desired_values(filepath: str) -> DesiredValues:
    """
    Parse a v2 desired_values file. See SYNTAX.md.

    Inputs:
        filepath: Path to the desired_values .v2.txt file.

    Outputs:
        DesiredValues with app_name, sections, and raw_lines.

    Only two things matter: section headers (line above a dash row)
    and pipe rows (any line with | that has backticked content in
    the last two columns). Everything else is ignored.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    sections: List[Section] = []
    current_section: Optional[Section] = None

    # Extract app name from first line matching "Desired Values: X"
    app_name = ""
    for line in lines:
        m = re.match(r'Desired Values:\s*(.+)', line.strip())
        if m:
            app_name = m.group(1).strip()
            break

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Section header: line immediately above a dash row.
        if (i + 1 < len(lines)
                and lines[i + 1].strip().startswith('----')
                and stripped):
            actions = extract_backticked_names(stripped)
            current_section = Section(
                description=stripped,
                actions=actions,
                fields=[],
                line_number=i + 1,  # 1-indexed for humans
            )
            sections.append(current_section)

        # Pipe row: any line with | that has backticked content.
        elif '|' in stripped and current_section is not None:
            cols = stripped.split('|')
            if len(cols) >= 2:
                # Machine columns are the LAST two.
                field_name = extract_backtick(cols[-2])
                field_value = extract_backtick(cols[-1])
                if field_name and field_value:
                    is_file = field_value.startswith('file:')
                    current_section.fields.append(Field(
                        name=field_name,
                        value=field_value,
                        is_file_ref=is_file,
                        file_path=field_value[5:] if is_file else None,
                        line_number=i + 1,
                    ))

    return DesiredValues(
        app_name=app_name,
        sections=sections,
        raw_lines=lines,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Parse a v2 desired_values file.')
    p.add_argument('--input-desired-values', required=True,
                   help='Path to the desired_values .v2.txt file')
    return p


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = parse_desired_values(args.input_desired_values)
    except FileNotFoundError:
        print(f"ERROR: File not found: {args.input_desired_values}",
              file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: Parse failed: {e}", file=sys.stderr)
        return 1

    print(f"App: {result.app_name}")
    print(f"Sections: {len(result.sections)}")
    for section in result.sections:
        print(f"\n  [{section.line_number}] {section.description}")
        print(f"       Actions: {section.actions}")
        print(f"       Fields:  {len(section.fields)}")
        for field in section.fields:
            tag = " (file)" if field.is_file_ref else ""
            print(f"         {field.name} = {field.value}{tag}")

    total_fields = sum(len(s.fields) for s in result.sections)
    file_refs = sum(1 for s in result.sections
                    for f in s.fields if f.is_file_ref)
    print(f"\nTotal: {total_fields} fields, {file_refs} file references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
