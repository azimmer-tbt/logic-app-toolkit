#!/usr/bin/env python3
# helpers/analyzer_unknowns.py
"""
Unknowns reporting helpers for the Logic App Analyzer.

This module centralizes tracking of:

- Catalog "misses" (uncataloged types/subtypes).
- Template "misses" (missing json/md template pairs).

It is responsible for:

- Keeping an in-memory list of unknowns during an analyzer run.
- Emitting a machine-readable Markdown (MRM) block summarizing unknowns.
- Writing a rich <output_stem>.todo.txt file with "what to do next" guidance.

Behaviour is Stage-1–compatible with the original inlined implementation
in 1_analyzer.py; only the location has changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

# ---------------------------------------------------------------------------
# Optional type checking imports
# ---------------------------------------------------------------------------

if TYPE_CHECKING:  # pragma: no cover
    # Import only for static type checkers to avoid runtime cycles.
    from helpers.definition_model import StepInfo  # noqa: F401


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AnalyzerUnknowns:
    """
    Collect unknown types/subtypes (catalog misses) and missing templates.

    Responsibilities
    ----------------
    - Emit a concise console warning (via the caller) when unknowns exist.
    - Write a rich <output_stem>.todo.txt file with "what to do next".
    - Optionally inject an MRM block for the post-processor.

    Notes
    -----
    This class intentionally stays ignorant of the concrete catalog structure;
    callers are expected to supply only the derived fields needed for human
    follow-up (step name, raw type, resolved key, etc.).
    """

    def __init__(self) -> None:
        # list[dict]: {step_name, raw_type, resolved_key, pretty_name}
        self.catalog_misses: List[Dict[str, str]] = []
        # list[dict]: {resolved_key, want_json, want_md}
        self.template_misses: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    # Catalog miss tracking
    # ------------------------------------------------------------------

    def add_catalog_miss(
        self,
        step_name: str,
        pretty_name: str,
        raw_type: str,
        resolved_key: str,
    ) -> None:
        """
        Record a catalog miss for a single step.

        Parameters
        ----------
        step_name:
            Code name (JSON key) of the step.
        pretty_name:
            Designer-facing label for the step (for humans).
        raw_type:
            Raw engine type (e.g. "ApiConnection").
        resolved_key:
            Effective catalog key (base or subtype) that failed to match.
        """
        self.catalog_misses.append(
            {
                "step_name": step_name,
                "pretty_name": pretty_name,
                "raw_type": raw_type,
                "resolved_key": resolved_key,
            }
        )

    # ------------------------------------------------------------------
    # Template miss tracking
    # ------------------------------------------------------------------

    def add_template_miss(self, resolved_key: str, templates_dir: str) -> None:
        """
        Record that a json/md template pair is missing for a key.

        Parameters
        ----------
        resolved_key:
            Effective type/template key (e.g. "ApiConnection.SharePointWrite").
        templates_dir:
            Root templates directory path (as a string) so that human-readable
            absolute/relative paths can be written into the TODO file.
        """
        self.template_misses.append(
            {
                "resolved_key": resolved_key,
                "want_json": f"{templates_dir}/{resolved_key}.json",
                "want_md": f"{templates_dir}/{resolved_key}.md",
            }
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def any(self) -> bool:
        """
        Return True if any catalog or template misses have been recorded.
        """
        return bool(self.catalog_misses or self.template_misses)

    # ------------------------------------------------------------------
    # MRM block emission
    # ------------------------------------------------------------------

    def render_mrm_block(self) -> str:
        """
        Render an MRM-wrapped Markdown block summarizing all unknowns.

        The block is intended for inclusion in the analyzer's .mrm.md output,
        for example:

            <!-- MRM:Unknowns:start -->
            ## Unknowns (Analyzer)
            ...
            <!-- MRM:Unknowns:end -->
        """
        lines: List[str] = []
        lines.append("<!-- MRM:Unknowns:start -->")
        lines.append("## Unknowns (Analyzer)")

        if self.catalog_misses:
            lines.append("### Uncataloged types/subtypes")
            for miss in self.catalog_misses:
                lines.append(
                    f"- `{miss['resolved_key']}` "
                    f"(from step `{miss['pretty_name']}` / raw `{miss['raw_type']}`)"
                )

        if self.template_misses:
            lines.append("### Missing templates (json/md pair not found)")
            for miss in self.template_misses:
                lines.append(
                    f"- `{miss['resolved_key']}` → "
                    f"expected: `{miss['want_json']}`, `{miss['want_md']}`"
                )

        lines.append("<!-- MRM:Unknowns:end -->")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # TODO file emission
    # ------------------------------------------------------------------

    def write_todo_file(
        self,
        out_md_path: Path,
        catalog_sample_hint: str = "",
        reg: Optional[Dict[str, "StepInfo"]] = None,
    ) -> Path:
        """
        Write <stem>.todo.txt with step-by-step guidance and examples.

        Parameters
        ----------
        out_md_path:
            Path to the main analyzer Markdown output file. The TODO file will
            use the same stem with a `.todo.txt` suffix.
        catalog_sample_hint:
            Optional block of example catalog JSON (pretty-printed) to help
            authors extend plugin_catalog.json.
        reg:
            Optional registry of steps by code name. When provided, the raw
            JSON for each uncataloged step is embedded to speed up human
            inspection.

        Returns
        -------
        Path
            The path to the written TODO file.
        """
        todo_path = out_md_path.with_suffix(".todo.txt")
        out_lines: List[str] = []

        out_lines.append("=== Logic App Analyzer: Items Requiring Attention ===\n")

        # ---- Catalog misses (per-step) -----------------------------------
        if self.catalog_misses:
            out_lines.append("UNCATALOGED TYPES/SUBTYPES\n")
            for miss in self.catalog_misses:
                step_name = miss["step_name"]      # code name (JSON key)
                pretty = miss["pretty_name"]       # Designer pretty name
                raw_type = miss["raw_type"]
                resolved = miss["resolved_key"]

                out_lines.append(f" - Step: {pretty}  (code: {step_name})")
                out_lines.append(f"   Raw type: {raw_type}")
                out_lines.append(f"   Resolved key: {resolved}")

                # If we have a registry, embed the raw JSON for this step.
                if (
                    reg is not None
                    and step_name in reg
                    and getattr(reg[step_name], "raw", None) is not None
                ):
                    try:
                        pretty_json = json.dumps(
                            reg[step_name].raw,
                            indent=4,
                            ensure_ascii=False,
                        )
                    except Exception:
                        pretty_json = "(error serializing raw step JSON)"

                    out_lines.append("   Code in question:")
                    out_lines.append("````")
                    out_lines.append(f"\"{step_name}\": {pretty_json}")
                    out_lines.append("````\n")
                else:
                    out_lines.append("   Code in question: (not available)\n")

            out_lines.append("Next steps:")
            out_lines.append("  1) Open your plugin_catalog.json")
            out_lines.append("  2) Decide how to address each miss:")
            out_lines.append("     a) If the raw type is a synonym of an existing canonical type, add an alias under \"type_aliases\" (many→one).")
            out_lines.append("        Example: { \"type_aliases\": { \"If\": \"Condition\" } }")
            out_lines.append("     b) If the step is a meta-type (e.g. ApiConnection) and needs a subtype, add/refine a \"hints\" entry (ways_to_match / conditions).")
            out_lines.append("     c) Otherwise, add a new entry under \"types\" or \"subtypes\" for the missing key.")
            out_lines.append("  3) Re-run analyzer; the .todo.txt should shrink to only truly new cases.")

            if catalog_sample_hint:
                out_lines.append("")
                out_lines.append("Example structure (types/subtypes/aliases/hints):")
                out_lines.append(catalog_sample_hint.strip())
            out_lines.append("")

        # ---- Template misses (json/md files not present) -----------------
        if self.template_misses:
            out_lines.append("MISSING TEMPLATES\n")
            for miss in self.template_misses:
                out_lines.append(f" - {miss['resolved_key']} → create:")
                out_lines.append(f"     {miss['want_json']}")
                out_lines.append(f"     {miss['want_md']}")
            out_lines.append("Next steps:")
            out_lines.append(
                "  1) Create the .json file listing fields your template consumes."
            )
            out_lines.append(
                "  2) Create the .md file with <placeholders> / {{#if}} blocks."
            )
            out_lines.append(
                "  3) Re-run the post-processor to render pretty instructions."
            )

        todo_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        return todo_path
