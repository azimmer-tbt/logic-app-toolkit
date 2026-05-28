#!/usr/bin/env python3
# filename: tui_framework.py
"""
Reusable Textual TUI components for the Logic App toolkit.

Provides base widgets, tree builders, detail panels, and export
dialogs that any toolkit tool can use for interactive exploration.

Public API:
    ActionTreeBuilder  — walks Logic App JSON, builds Textual Tree nodes
    DetailPanel        — displays field details with expected/actual/SHA
    ExportDialog       — confirmation + path input for template export
    StatusBar          — bottom bar with mode, counts, key hints
    HistoryPanel       — displays .verify/ run history
    action_summary     — one-line summary string for an action
    build_action_tree  — recursive tree builder
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static, Tree, Label, RichLog, Input, Button
from textual.widgets.tree import TreeNode

from rich.text import Text
from rich.table import Table


# ─────────────────────────────────────────────────────────────────────────────
# Action summary — one-liner for tree display
# ─────────────────────────────────────────────────────────────────────────────

def action_summary(action: dict) -> str:
    """Human-readable one-line summary of a Logic App action."""
    atype = action.get('type', '?')
    if atype == 'InitializeVariable':
        var_list = action.get('inputs', {}).get('variables', [])
        names = [v.get('name', '?') for v in var_list if isinstance(v, dict)]
        preview = ', '.join(names[:3])
        suffix = '...' if len(names) > 3 else ''
        return f"InitVar [{len(names)}v: {preview}{suffix}]"
    elif atype == 'Scope':
        return f"Scope [{len(action.get('actions', {}))} steps]"
    elif atype == 'Foreach':
        return f"Foreach [{len(action.get('actions', {}))} steps]"
    elif atype == 'Switch':
        return f"Switch [{len(action.get('cases', {}))} cases]"
    elif atype == 'If':
        return "If"
    elif atype == 'SetVariable':
        return f"Set → {action.get('inputs', {}).get('name', '?')}"
    elif atype == 'Compose':
        return "Compose"
    elif atype in ('Http', 'ApiConnection'):
        return atype
    elif atype == 'ParseJson':
        return "ParseJson"
    else:
        return atype


# ─────────────────────────────────────────────────────────────────────────────
# Tree builder — recursive, works with any actions dict
# ─────────────────────────────────────────────────────────────────────────────

def build_action_tree(
    parent_node: TreeNode,
    actions: dict,
    marked: Optional[Set[str]] = None,
) -> None:
    """Recursively build a Textual Tree from a Logic App actions dict.

    Each node's data dict contains:
        'name': action key name
        'action': the action dict
        'path': dot-notation path from definition root
    """
    if marked is None:
        marked = set()

    for name, action in actions.items():
        if not isinstance(action, dict):
            continue
        atype = action.get('type', '?')
        summary = action_summary(action)
        prefix = "☑ " if name in marked else ""
        label = f"{prefix}{name}  ({summary})"
        node = parent_node.add(label, data={
            'name': name,
            'action': action,
        })

        # Recurse into children
        child_actions = action.get('actions', {})
        if child_actions:
            build_action_tree(node, child_actions, marked)

        # Switch: default + cases
        if atype == 'Switch':
            default_actions = action.get('default', {}).get('actions', {})
            if default_actions:
                dnode = node.add("default", data={
                    'name': 'default',
                    'action': action.get('default', {}),
                })
                build_action_tree(dnode, default_actions, marked)
            for case_name, case_data in action.get('cases', {}).items():
                case_actions = case_data.get('actions', {})
                if case_actions:
                    cnode = node.add(f"case: {case_name}", data={
                        'name': case_name,
                        'action': case_data,
                    })
                    build_action_tree(cnode, case_actions, marked)


# ─────────────────────────────────────────────────────────────────────────────
# Detail panel — shows field info when a tree node is selected
# ─────────────────────────────────────────────────────────────────────────────

class DetailPanel(Static):
    """Displays detailed information about the selected tree node."""

    DEFAULT_CSS = """
    DetailPanel {
        height: auto;
        min-height: 5;
        max-height: 20;
        border: solid $accent;
        padding: 1;
        margin-top: 1;
    }
    """

    def set_action_detail(self, name: str, action: dict) -> None:
        """Show detail for a Logic App action."""
        atype = action.get('type', '?')
        lines = [f"[bold]{name}[/]  ({atype})"]

        if atype == 'InitializeVariable':
            var_list = action.get('inputs', {}).get('variables', [])
            for v in var_list:
                if not isinstance(v, dict):
                    continue
                vname = v.get('name', '')
                vvalue = str(v.get('value', ''))
                vtype = v.get('type', '?')
                if len(vvalue) > 60:
                    vvalue = vvalue[:57] + '...'
                lines.append(f"  {vname} ({vtype}) = {vvalue}")

        elif atype == 'SetVariable':
            inputs = action.get('inputs', {})
            lines.append(f"  Variable: {inputs.get('name', '?')}")
            val = str(inputs.get('value', ''))
            if len(val) > 60:
                val = val[:57] + '...'
            lines.append(f"  Value: {val}")

        elif atype == 'Compose':
            inputs = action.get('inputs', '')
            val = str(inputs)
            if len(val) > 100:
                val = val[:97] + '...'
            lines.append(f"  Inputs: {val}")

        elif atype == 'Switch':
            expr = action.get('expression', '?')
            cases = list(action.get('cases', {}).keys())
            lines.append(f"  Expression: {expr}")
            lines.append(f"  Cases: {', '.join(cases)}")
            lines.append(f"  Has default: {'default' in action}")

        elif atype in ('Scope', 'Foreach'):
            children = list(action.get('actions', {}).keys())
            lines.append(f"  Children: {len(children)}")
            for c in children[:8]:
                lines.append(f"    • {c}")
            if len(children) > 8:
                lines.append(f"    ... and {len(children) - 8} more")

        ra = action.get('runAfter', {})
        if ra:
            preds = list(ra.keys())
            lines.append(f"  runAfter: {', '.join(preds)}")

        self.update("\n".join(lines))

    def set_check_detail(self, check) -> None:
        """Show detail for a CheckResult."""
        icon = "✅" if check.passed else "❌"
        lines = [
            f"{icon} [bold]{check.field_name}[/]  ({check.category})",
            f"  Path: {check.path}",
            f"  Expected: {check.expected}",
            f"  Actual:   {check.actual}",
        ]
        if check.detail:
            lines.append(f"  Detail: {check.detail}")
        self.update("\n".join(lines))

    def clear_detail(self) -> None:
        self.update("[dim]Select an item to see details[/]")


# ─────────────────────────────────────────────────────────────────────────────
# History panel — shows recent verification runs
# ─────────────────────────────────────────────────────────────────────────────

class HistoryPanel(Static):
    """Displays .verify/ run history."""

    DEFAULT_CSS = """
    HistoryPanel {
        height: auto;
        max-height: 15;
        border: solid $accent;
        padding: 1;
    }
    """

    def set_history(self, history: dict) -> None:
        runs = history.get('runs', [])
        if not runs:
            self.update("[dim]No verification history yet.[/]")
            return

        lines = [f"[bold]Recent runs ({len(runs)}):[/]"]
        for run in reversed(runs[-10:]):
            icon = "✅" if run.get('all_pass') else "❌"
            ts = run.get('timestamp', '?')
            passed = run.get('passed', 0)
            total = run.get('total_checks', 0)
            lines.append(f"  {icon} {ts}  {passed}/{total} checks")
        self.update("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Status bar
# ─────────────────────────────────────────────────────────────────────────────

class StatusBar(Static):
    """Bottom status bar showing mode and counts."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    def set_status(self, mode: str, app_name: str, marked_count: int = 0,
                   check_summary: str = "") -> None:
        parts = [f"[bold]{mode}[/]", app_name]
        if marked_count > 0:
            parts.append(f"[yellow]{marked_count} marked[/]")
        if check_summary:
            parts.append(check_summary)
        self.update("  │  ".join(parts))


# ─────────────────────────────────────────────────────────────────────────────
# Template exporter
# ─────────────────────────────────────────────────────────────────────────────

def export_desired_values_template(
    app_name: str,
    actions: dict,
    marked_names: Set[str],
    output_path: str,
) -> str:
    """Export marked steps as a desired_values v2 template file.

    Dumps the whole step by default — user prunes what they don't need.
    Each section gets a comment telling them to strike out unwanted rows.
    Returns the output path written.
    """
    lines = [
        f"Desired Values: {app_name}",
        "=" * 60,
        "",
        "# AUTO-GENERATED TEMPLATE from Browse mode",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "#",
        "# This template dumps every field from each marked step.",
        "# PRUNE what you don't need: delete entire rows for fields",
        "# you don't want the generator to track or patch.",
        "# The generator only acts on rows with backticked values.",
        "#",
        "# Fields with very long values should use file: references",
        "# instead of inline content. See SYNTAX.md.",
        "",
    ]

    for action_name in sorted(marked_names):
        action = actions.get(action_name)
        if action is None:
            # Might be nested — search recursively
            action = _find_action_recursive(actions, action_name)
            if action is None:
                lines.append(f"# {action_name}: not found at top level")
                lines.append("")
                continue

        atype = action.get('type', '')
        _export_action(lines, action_name, action, atype)

    content = "\n".join(lines) + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return output_path


def _find_action_recursive(actions: dict, name: str) -> Optional[dict]:
    """Search for an action by name anywhere in the tree."""
    if name in actions:
        return actions[name]
    for action in actions.values():
        if not isinstance(action, dict):
            continue
        # Check children
        child_actions = action.get('actions', {})
        if child_actions:
            found = _find_action_recursive(child_actions, name)
            if found:
                return found
        # Check switch cases
        if action.get('type') == 'Switch':
            for case_data in action.get('cases', {}).values():
                found = _find_action_recursive(case_data.get('actions', {}), name)
                if found:
                    return found
            found = _find_action_recursive(
                action.get('default', {}).get('actions', {}), name)
            if found:
                return found
    return None


def _export_action(lines: list, action_name: str, action: dict, atype: str) -> None:
    """Append a desired_values section for one action."""
    if atype == 'InitializeVariable':
        var_list = action.get('inputs', {}).get('variables', [])
        lines.append(f"# Step: {action_name} — {len(var_list)} variable(s)")
        lines.append(f"# Delete rows for variables you don't want to track.")
        lines.append(f"VARIABLES (`{action_name}`)")
        lines.append("-" * 60)
        for v in var_list:
            if not isinstance(v, dict):
                continue
            vname = v.get('name', '')
            vvalue = str(v.get('value', ''))
            vtype = v.get('type', 'string')
            if len(vvalue) > 80:
                lines.append(f"# NOTE: value for {vname} is {len(vvalue)} chars — consider file: reference")
                vvalue = vvalue[:77] + "..."
            lines.append(f"{vname} | {vtype} | `{vname}` | `{vvalue}`")
        lines.append("")

    elif atype == 'Scope':
        child_actions = action.get('actions', {})
        children = list(child_actions.keys())
        lines.append(f"# Step: {action_name} (Scope, {len(children)} children)")
        lines.append(f"# Contains: {', '.join(children[:6])}")
        if len(children) > 6:
            lines.append(f"#   ... and {len(children) - 6} more")
        lines.append(f"# Scope ordering matters for structural patches.")
        lines.append(f"# The generator's structural catalog handles scopes —")
        lines.append(f"# only include here if you need to override specific fields.")
        lines.append("")

    elif atype == 'Compose':
        inputs = action.get('inputs', '')
        val = str(inputs)
        lines.append(f"# Step: {action_name} (Compose)")
        if len(val) > 80:
            lines.append(f"# Value: {len(val)} chars — use file: reference for this content")
            lines.append(f"# Create a file and reference it as: `file:path/to/content.html`")
        else:
            lines.append(f"# Current value: {val}")
        lines.append("")

    elif atype == 'SetVariable':
        inputs = action.get('inputs', {})
        vname = inputs.get('name', '?')
        vvalue = str(inputs.get('value', ''))
        lines.append(f"# Step: {action_name} (SetVariable → {vname})")
        lines.append(f"# Current: {vvalue[:80]}")
        lines.append("")

    elif atype == 'ParseJson':
        lines.append(f"# Step: {action_name} (ParseJson)")
        lines.append(f"# Schema should be managed via file: reference")
        lines.append(f"# in the PARSE SCHEMA section.")
        lines.append("")

    else:
        lines.append(f"# Step: {action_name} ({atype})")
        lines.append(f"# No template pattern for this action type.")
        lines.append("")
