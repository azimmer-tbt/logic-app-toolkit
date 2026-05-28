#!/usr/bin/env python3
# filename: confessor_tui.py
"""
Textual TUI for Logic App verification — Browse and Compare modes.

Launched from confessor.py when --report is not specified.
Reuses the compare engine from confessor.py and widgets from helpers/tui_framework.py.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Tree, TabbedContent, TabPane

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)

from helpers.tui_framework import (
    build_action_tree, action_summary,
    DetailPanel, StatusBar, HistoryPanel,
    export_desired_values_template,
)
from confessor import (
    run_compare, VerifyReport, CheckResult,
    _render_report, _record_run,
    _history_path, _load_history,
)


class VerifyApp(App):
    """Textual TUI — Browse and Compare modes for Logic App verification."""

    CSS = """
    Screen { layout: vertical; }
    Tree { height: 2fr; }
    DetailPanel { height: 1fr; }
    StatusBar { dock: bottom; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "report", "Report to terminal"),
        Binding("space", "toggle_mark", "Mark/Unmark"),
        Binding("x", "export", "Export template"),
        Binding("h", "history", "History"),
    ]

    def __init__(
        self,
        patched_path: str,
        desired_values_path: str,
        materials_dir: str,
        no_history: bool = False,
    ):
        super().__init__()
        self.patched_path = patched_path
        self.desired_values_path = desired_values_path
        self.materials_dir = materials_dir
        self.no_history = no_history

        with open(patched_path, "r", encoding="utf-8") as f:
            self.doc = json.load(f)
        self.actions = self.doc['definition']['actions']
        self.marked: set = set()
        self.report: Optional[VerifyReport] = None


    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Compare", id="compare-tab"):
                yield Tree("Compare — checks", id="compare-tree")
            with TabPane("Browse", id="browse-tab"):
                yield Tree("Browse — actions", id="browse-tree")
        yield DetailPanel(id="detail")
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        # Run compare
        self.report = run_compare(
            self.patched_path,
            self.desired_values_path,
            self.materials_dir,
        )

        if not self.no_history:
            _record_run(self.report, self.patched_path)

        # Populate compare tree
        self._build_compare_tree()

        # Populate browse tree
        self._build_browse_tree()

        # Update status
        self._update_status()

    def _build_compare_tree(self) -> None:
        tree = self.query_one("#compare-tree", Tree)
        tree.clear()
        tree.root.expand()

        if self.report is None:
            return

        # Group by category
        categories: dict = {}
        for check in self.report.checks:
            categories.setdefault(check.category, []).append(check)

        cat_labels = {
            'config': 'Configuration Values',
            'email': 'Email Content',
            'css': 'CSS Styling',
            'schema': 'Parse Schema',
            'structural': 'Structural Checks',
        }
        cat_order = ['config', 'email', 'css', 'schema', 'structural']

        for cat in cat_order:
            checks = categories.get(cat, [])
            if not checks:
                continue
            pass_count = sum(1 for c in checks if c.passed)
            total = len(checks)
            icon = "✅" if pass_count == total else "❌"
            label = cat_labels.get(cat, cat)
            cat_node = tree.root.add(
                f"{icon} {label} ({pass_count}/{total})",
                data={'type': 'category', 'category': cat},
            )
            for check in checks:
                check_icon = "✅" if check.passed else "❌"
                cat_node.add(
                    f"{check_icon} {check.field_name}",
                    data={'type': 'check', 'check': check},
                )


    def _build_browse_tree(self) -> None:
        tree = self.query_one("#browse-tree", Tree)
        tree.clear()
        tree.root.expand()
        build_action_tree(tree.root, self.actions, self.marked)

    def _update_status(self) -> None:
        status = self.query_one("#status", StatusBar)
        app_name = self.report.app_name if self.report else "?"
        check_summary = ""
        if self.report:
            icon = "✅" if self.report.all_pass else "❌"
            check_summary = f"{icon} {self.report.pass_count}/{len(self.report.checks)}"
        status.set_status("Verify", app_name, len(self.marked), check_summary)

    # ── Event handlers ────────────────────────────────────────────────

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        detail = self.query_one("#detail", DetailPanel)
        data = event.node.data
        if data is None:
            detail.clear_detail()
            return

        if data.get('type') == 'check':
            detail.set_check_detail(data['check'])
        elif 'action' in data:
            detail.set_action_detail(data['name'], data['action'])
        else:
            detail.clear_detail()

    def action_toggle_mark(self) -> None:
        """Toggle mark on the currently focused browse tree node."""
        tree = self.query_one("#browse-tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        name = node.data.get('name', '')
        if not name:
            return

        if name in self.marked:
            self.marked.discard(name)
        else:
            self.marked.add(name)

        # Rebuild browse tree to update checkmarks
        self._build_browse_tree()
        self._update_status()

    def action_export(self) -> None:
        """Export marked steps as a desired_values template."""
        if not self.marked:
            self.notify("Nothing marked — use Space to mark steps first", severity="warning")
            return

        app_name = self.report.app_name if self.report else "UNKNOWN"
        output_dir = os.path.dirname(os.path.abspath(self.patched_path))
        output_path = os.path.join(output_dir, f"template_{app_name}.v2.txt")

        export_desired_values_template(
            app_name, self.actions, self.marked, output_path)

        self.notify(f"Template exported: {output_path}", severity="information")


    def action_report(self) -> None:
        """Print Rich report to terminal and exit."""
        if self.report:
            self.exit()
            _render_report(self.report)

    def action_history(self) -> None:
        """Show verification history in the detail panel."""
        detail = self.query_one("#detail", DetailPanel)
        if self.report:
            hp = _history_path(self.patched_path, self.report.app_name)
            history = _load_history(hp)
            runs = history.get('runs', [])
            if not runs:
                detail.update("[dim]No verification history yet.[/]")
                return
            lines = [f"[bold]Recent runs ({len(runs)}):[/]"]
            for run in reversed(runs[-10:]):
                icon = "✅" if run.get('all_pass') else "❌"
                ts = run.get('timestamp', '?')
                passed = run.get('passed', 0)
                total = run.get('total_checks', 0)
                lines.append(f"  {icon} {ts}  {passed}/{total} checks")
            detail.update("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point (called from verify.py)
# ─────────────────────────────────────────────────────────────────────────────

def launch_tui(
    patched_path: str,
    desired_values_path: str,
    materials_dir: str,
    no_history: bool = False,
) -> int:
    """Launch the Textual TUI. Returns 0 if all pass, 1 if failures."""
    app = VerifyApp(
        patched_path=patched_path,
        desired_values_path=desired_values_path,
        materials_dir=materials_dir,
        no_history=no_history,
    )
    app.run()
    if app.report:
        return 0 if app.report.all_pass else 1
    return 2
