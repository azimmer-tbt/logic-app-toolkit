#!/usr/bin/env python3
# filename: confessor.py
"""
Confessor — compare a patched Logic App JSON against desired state.

Two modes:
    Report (--report): Rich-formatted terminal output, pipe-friendly.
    TUI (default): Interactive Textual browser. (future)

Two workflows:
    Compare: patched JSON vs desired_values + materials → green/red per field
    Browse: explore a Logic App JSON, mark fields, export desired_values template (future)

Usage:
    python3 verify.py \
        --input-patched CURRENT/patched__FRESHMART-DEV-PRICES.json \
        --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \
        --input-materials materials/ \
        --report

    python3 verify.py \
        --input-patched CURRENT/patched__FRESHMART-DEV-PRICES.json \
        --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \
        --input-materials materials/

Exit codes:
    0 = all checks pass
    1 = one or more mismatches found
    2 = error (missing files, parse failure)
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Path setup
# ─────────────────────────────────────────────────────────────────────────────

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)

from helpers.checksum import fingerprint
from utilities.generators.desired_values_parser import (
    parse_desired_values, DesiredValues, Section, Field,
)


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary — internal names → business-facing terms
# ─────────────────────────────────────────────────────────────────────────────

VOCABULARY = {
    'orthodox': 'compliance spec',
    'surgeon': 'patch engine',
    'inquisitor': 'compliance checker',
    'evangelist': 'comparison tool',
    'fingerprint': 'checksum',
    'PRIOR': 'baseline',
    'cartographer': 'structure scanner',
}

def _biz(term: str) -> str:
    """Translate internal term to business-facing if in report mode."""
    return VOCABULARY.get(term, term)


# ─────────────────────────────────────────────────────────────────────────────
# Language mapping (shared with generator)
# ─────────────────────────────────────────────────────────────────────────────

LANG_MAP = {
    'en': ('default', 'Compose_English_Email'),
    'ja': ('cases.Japanese', 'Compose_Japanese_Email'),
    'de': ('cases.German', 'Compose_German_Email'),
    'fr': ('cases.French', 'Compose_French_Email'),
    'es': ('cases.Spanish', 'Compose_Spanish_Email'),
    'pt': ('cases.Portuguese', 'Compose_Portuguese_Email'),
    'it': ('cases.Italian', 'Compose_Italian_Email'),
    'ko': ('cases.Korean', 'Compose_Korean_Email'),
    'zh': ('cases.Chinese', 'Compose_Chinese_Email'),
}

LANGUAGE_CODES = set(LANG_MAP.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Check result data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of one field comparison."""
    category: str       # 'config', 'email', 'css', 'schema', 'structural'
    field_name: str     # human-readable field name
    path: str           # JSON path in the Logic App
    passed: bool
    expected: str       # expected value or SHA (truncated for display)
    actual: str         # actual value or SHA
    detail: str = ""    # extra context on mismatch


@dataclass
class VerifyReport:
    """Full verification result."""
    app_name: str
    checks: List[CheckResult]
    timestamp: str

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def all_pass(self) -> bool:
        return all(c.passed for c in self.checks)


# ─────────────────────────────────────────────────────────────────────────────
# Navigation helpers — find things in the Logic App JSON
# ─────────────────────────────────────────────────────────────────────────────

def _find_var_value(actions: dict, action_name: str, var_name: str) -> Optional[str]:
    """Find a variable's value in an InitializeVariable action."""
    action = actions.get(action_name)
    if action is None or action.get("type") != "InitializeVariable":
        return None
    for v in action.get("inputs", {}).get("variables", []):
        if isinstance(v, dict) and v.get("name") == var_name:
            return v.get("value", "")
    return None


def _find_foreach_handler(actions: dict):
    """Find the foreach handler scope, supporting both naming conventions."""
    core = actions.get('Core_-_Main_Workflow', {}).get('actions', {})
    for name in ('Handle_Devices_From_Search', 'Handle_Stores_From_List'):
        if name in core:
            return core[name].get('actions', {}), name
    return None, None


def _find_switch(handle_actions: dict):
    """Find the Switch action, supporting both naming conventions."""
    for name in ('Send_Per-Language_Emails', 'Send_Per-Region_Emails'):
        if name in handle_actions:
            return handle_actions[name], name
    return None, None


def _find_parse_schema(actions: dict):
    """Find the Parse action's schema, supporting both naming conventions."""
    core = actions.get('Core_-_Main_Workflow', {}).get('actions', {})
    for scope_name in ('Get_List_of_Devices', 'Get_List_of_Stores'):
        scope = core.get(scope_name, {}).get('actions', {})
        for parse_name in ('Parse_Advanced_Search', 'Parse_Store_List'):
            if parse_name in scope:
                return scope[parse_name].get('inputs', {}).get('schema'), scope_name, parse_name
    return None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# Compare engine — build checks from desired_values + materials + patched JSON
# ─────────────────────────────────────────────────────────────────────────────

def _load_file_content(file_path: str):
    """Load file — JSON first, fallback to raw text."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()


def _check_literal_field(
    field: Field,
    section: Section,
    actions: dict,
) -> List[CheckResult]:
    """Check a literal string value against the patched JSON."""
    results = []
    for action_name in section.actions:
        actual = _find_var_value(actions, action_name, field.name)
        if actual is None:
            continue
        passed = str(actual) == field.value
        results.append(CheckResult(
            category='config',
            field_name=field.name,
            path=f'{action_name}.{field.name}',
            passed=passed,
            expected=field.value,
            actual=str(actual),
            detail="" if passed else f"expected {field.value!r}, got {actual!r}",
        ))
    return results


def _check_email_content(
    field: Field,
    actions: dict,
    materials_dir: str,
) -> List[CheckResult]:
    """Check email HTML against patched Compose step inputs."""
    lang = field.name
    if lang not in LANG_MAP:
        return []
    case_path, compose_name = LANG_MAP[lang]
    file_path = os.path.join(materials_dir, field.file_path)

    if not os.path.exists(file_path):
        return [CheckResult(
            category='email', field_name=f'{lang} email',
            path=file_path, passed=False,
            expected='(file exists)', actual='MISSING',
            detail=f'Material file not found: {file_path}',
        )]

    html_content = open(file_path, "r", encoding="utf-8").read()
    expected_sha = fingerprint(html_content)

    handle_actions, handler_name = _find_foreach_handler(actions)
    if handle_actions is None:
        return []
    switch, switch_name = _find_switch(handle_actions)
    if switch is None:
        return []

    try:
        node = switch
        for part in case_path.split('.'):
            node = node[part]
        compose_inputs = node['actions'][compose_name]['inputs']
    except KeyError:
        return [CheckResult(
            category='email', field_name=f'{lang} email',
            path=f'{handler_name}.{switch_name}.{case_path}.{compose_name}',
            passed=False, expected=expected_sha[:12], actual='NOT FOUND',
            detail=f'Compose step {compose_name} not found in patched JSON',
        )]

    actual_sha = fingerprint(compose_inputs)
    passed = actual_sha == expected_sha
    return [CheckResult(
        category='email',
        field_name=f'{lang} email',
        path=f'{switch_name}.{case_path}.{compose_name}.inputs',
        passed=passed,
        expected=expected_sha[:12],
        actual=actual_sha[:12],
        detail="" if passed else "content mismatch — re-run generator",
    )]


def _check_css(
    field: Field,
    section: Section,
    actions: dict,
    materials_dir: str,
) -> List[CheckResult]:
    """Check CSS wrapper value against patched variable."""
    file_path = os.path.join(materials_dir, field.file_path)
    content = _load_file_content(file_path)
    if isinstance(content, dict) and 'value' in content:
        expected_sha = fingerprint(content['value'])
    else:
        expected_sha = fingerprint(content)

    for action_name in section.actions:
        actual = _find_var_value(actions, action_name, field.name)
        if actual is None:
            continue
        actual_sha = fingerprint(actual)
        passed = actual_sha == expected_sha
        return [CheckResult(
            category='css',
            field_name='shared_css_style',
            path=f'{action_name}.shared_css_style',
            passed=passed,
            expected=expected_sha[:12],
            actual=actual_sha[:12],
        )]
    return []


def _check_schema(
    field: Field,
    actions: dict,
    materials_dir: str,
) -> List[CheckResult]:
    """Check parse schema against patched Parse action."""
    file_path = os.path.join(materials_dir, field.file_path)
    desired_schema = _load_file_content(file_path)
    expected_sha = fingerprint(desired_schema)

    current_schema, scope_name, parse_name = _find_parse_schema(actions)
    if current_schema is None:
        return [CheckResult(
            category='schema', field_name='parse schema',
            path='(not found)', passed=False,
            expected=expected_sha[:12], actual='NOT FOUND',
        )]

    actual_sha = fingerprint(current_schema)
    passed = actual_sha == expected_sha
    return [CheckResult(
        category='schema',
        field_name='parse schema',
        path=f'{scope_name}.{parse_name}.inputs.schema',
        passed=passed,
        expected=expected_sha[:12],
        actual=actual_sha[:12],
    )]


def _check_structural(actions: dict) -> List[CheckResult]:
    """Check structural patches are present in the patched JSON.
    Only checks for elements that have a plausible parent in the app —
    skips checks when the expected container doesn't exist."""
    results = []

    # Test mode variables — only check if any InitializeVariable actions exist
    # (they should in any app, but be graceful)
    has_test_switch = 'Initialize_Test_Mode_Switch' in actions
    has_test_email = 'Initialize_Test_Mode_Email' in actions
    if has_test_switch or has_test_email:
        results.append(CheckResult(
            category='structural', field_name='test mode switch',
            path='definition.actions.Initialize_Test_Mode_Switch',
            passed=has_test_switch,
            expected='present', actual='present' if has_test_switch else 'MISSING',
        ))
        results.append(CheckResult(
            category='structural', field_name='test mode email',
            path='definition.actions.Initialize_Test_Mode_Email',
            passed=has_test_email,
            expected='present', actual='present' if has_test_email else 'MISSING',
        ))

    # Country code variable — only check if test mode was detected
    # (structural patches are all-or-nothing; if test mode is missing,
    # the app hasn't been structurally patched yet)
    if has_test_switch or has_test_email:
        dev_user_vars = actions.get('Initialize_Device_and_User_Variables', {}) \
                              .get('inputs', {}).get('variables', [])
        store_vars = actions.get('Initialize_Store_Variables', {}) \
                           .get('inputs', {}).get('variables', [])
        has_device_cc = any(v.get('name') == 'device_country_code' for v in dev_user_vars)
        has_store_cc = any(v.get('name') == 'store_country_code' for v in store_vars)
        cc_present = has_device_cc or has_store_cc
        results.append(CheckResult(
            category='structural', field_name='country code variable',
            path='(device/store variables)',
            passed=cc_present,
            expected='present', actual='present' if cc_present else 'MISSING',
        ))

    # Foreach-level checks — only if a handler exists
    handle_actions, handler_name = _find_foreach_handler(actions)
    if handle_actions is not None:
        has_overrides = 'Overrides_Scope' in handle_actions
        if has_overrides:
            results.append(CheckResult(
                category='structural', field_name='overrides scope',
                path=f'{handler_name}.Overrides_Scope',
                passed=True,
                expected='present', actual='present',
            ))

        switch, switch_name = _find_switch(handle_actions)
        if switch is not None:
            expr = switch.get('expression', '')
            correct = expr == "@variables('device_language')" or \
                      expr == "@variables('store_language')"
            if has_overrides or correct:
                # Only report switch if overrides scope is present
                # (the fix only matters when overrides exist to override)
                results.append(CheckResult(
                    category='structural', field_name='switch expression',
                    path=f'{switch_name}.expression',
                    passed=correct,
                    expected="@variables('..._language')",
                    actual=expr if len(expr) < 60 else expr[:57] + '...',
                    detail="" if correct else "reads raw item field — override bypassed",
                ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main compare orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_compare(
    patched_path: str,
    desired_values_path: str,
    materials_dir: str,
) -> VerifyReport:
    """Run all checks and return a VerifyReport."""
    with open(patched_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    actions = doc['definition']['actions']
    desired = parse_desired_values(desired_values_path)
    checks: List[CheckResult] = []

    for section in desired.sections:
        if section.description.upper().startswith('STRUCTURAL'):
            continue
        for field in section.fields:
            if not field.is_file_ref:
                checks.extend(
                    _check_literal_field(field, section, actions))
            elif field.name in LANGUAGE_CODES:
                checks.extend(
                    _check_email_content(field, actions, materials_dir))
            elif field.name == 'shared_css_style':
                checks.extend(
                    _check_css(field, section, actions, materials_dir))
            elif field.name == 'schema':
                checks.extend(
                    _check_schema(field, actions, materials_dir))

    # Structural checks (always run)
    checks.extend(_check_structural(actions))

    return VerifyReport(
        app_name=desired.app_name,
        checks=checks,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# History — .verify/<app_name>.history.json
# ─────────────────────────────────────────────────────────────────────────────

def _history_dir(patched_path: str) -> str:
    """Return the .verify/ directory alongside the patched file."""
    parent = os.path.dirname(os.path.abspath(patched_path))
    verify_dir = os.path.join(parent, '.verify')
    os.makedirs(verify_dir, exist_ok=True)
    return verify_dir


def _history_path(patched_path: str, app_name: str) -> str:
    return os.path.join(_history_dir(patched_path), f'{app_name}.history.json')


def _load_history(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"runs": [], "viewed_fields": [], "marked_for_report": []}


def _save_history(path: str, history: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def _record_run(report: VerifyReport, patched_path: str) -> None:
    """Append this verification run to history."""
    hp = _history_path(patched_path, report.app_name)
    history = _load_history(hp)
    history['runs'].append({
        'timestamp': report.timestamp,
        'patched_file': os.path.basename(patched_path),
        'total_checks': len(report.checks),
        'passed': report.pass_count,
        'failed': report.fail_count,
        'all_pass': report.all_pass,
    })
    # Keep last 20 runs
    history['runs'] = history['runs'][-20:]
    _save_history(hp, history)


# ─────────────────────────────────────────────────────────────────────────────
# Rich report output
# ─────────────────────────────────────────────────────────────────────────────

def _render_report(report: VerifyReport) -> None:
    """Render the verification report using Rich."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        # Fallback to plain text if Rich isn't available
        _render_plain(report)
        return

    console = Console()

    # Header
    status = "[bold green]ALL PASS[/]" if report.all_pass else "[bold red]FAILED[/]"
    console.print()
    console.print(Panel(
        f"[bold]{report.app_name}[/] — Verification Report\n"
        f"{report.timestamp}   {status}\n"
        f"{report.pass_count} passed, {report.fail_count} failed "
        f"of {len(report.checks)} checks",
        title="Verify",
        border_style="green" if report.all_pass else "red",
    ))

    # Group checks by category
    categories = {}
    for check in report.checks:
        categories.setdefault(check.category, []).append(check)

    category_order = ['config', 'email', 'css', 'schema', 'structural']
    category_labels = {
        'config': 'Configuration Values',
        'email': 'Email Content',
        'css': 'CSS Styling',
        'schema': 'Parse Schema',
        'structural': 'Structural Checks',
    }

    for cat in category_order:
        checks = categories.get(cat, [])
        if not checks:
            continue

        label = category_labels.get(cat, cat.upper())
        cat_pass = all(c.passed for c in checks)
        cat_icon = "✅" if cat_pass else "❌"

        table = Table(
            title=f"{cat_icon} {label}",
            show_header=True,
            header_style="bold",
            border_style="green" if cat_pass else "red",
            expand=True,
        )
        table.add_column("Status", width=6, justify="center")
        table.add_column("Field", min_width=20)
        table.add_column("Expected", min_width=15)
        table.add_column("Actual", min_width=15)
        table.add_column("Detail", min_width=20)

        for check in checks:
            icon = "✅" if check.passed else "❌"
            style = "" if check.passed else "red"
            table.add_row(
                icon,
                check.field_name,
                check.expected,
                check.actual,
                check.detail,
                style=style,
            )

        console.print()
        console.print(table)

    # Summary line
    console.print()
    if report.all_pass:
        console.print("[bold green]All checks passed.[/]")
    else:
        fails = [c for c in report.checks if not c.passed]
        console.print(f"[bold red]{len(fails)} check(s) failed:[/]")
        for f in fails:
            console.print(f"  [red]• {f.field_name}: {f.detail or 'mismatch'}[/]")
    console.print()


def _render_plain(report: VerifyReport) -> None:
    """Fallback plain-text renderer when Rich isn't available."""
    status = "ALL PASS" if report.all_pass else "FAILED"
    print(f"\n{report.app_name} — Verification Report")
    print(f"{'=' * 50}")
    print(f"{report.timestamp}   {status}")
    print(f"{report.pass_count} passed, {report.fail_count} failed "
          f"of {len(report.checks)} checks\n")

    for check in report.checks:
        icon = "PASS" if check.passed else "FAIL"
        print(f"  [{icon}] {check.category:12s} {check.field_name}")
        if not check.passed and check.detail:
            print(f"         {check.detail}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify",
        description=f"Verify v{__version__} — compare patched Logic App against desired state.",
    )
    p.add_argument('--input-patched', required=True,
                   help='Path to the patched Logic App JSON')
    p.add_argument('--input-desired-values', required=True,
                   help='Path to the v2 desired_values file')
    p.add_argument('--input-materials', required=True,
                   help='Path to the materials/ directory')
    p.add_argument('--report', action='store_true',
                   help='Print Rich report and exit (no TUI)')
    p.add_argument('--no-history', action='store_true',
                   help='Skip writing to .verify/ history')
    return p


def main() -> int:
    args = _build_parser().parse_args()

    # Validate inputs exist
    for path, label in [
        (args.input_patched, "Patched JSON"),
        (args.input_desired_values, "Desired values"),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}", file=sys.stderr)
            return 2

    if not os.path.isdir(args.input_materials):
        print(f"ERROR: Materials directory not found: {args.input_materials}",
              file=sys.stderr)
        return 2

    # Run compare
    report = run_compare(
        args.input_patched,
        args.input_desired_values,
        args.input_materials,
    )

    # Record history
    if not args.no_history:
        _record_run(report, args.input_patched)

    if args.report:
        _render_report(report)
    else:
        from confessor_tui import launch_tui
        return launch_tui(
            args.input_patched,
            args.input_desired_values,
            args.input_materials,
            args.no_history,
        )

    return 0 if report.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
