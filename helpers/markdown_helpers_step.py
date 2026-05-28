#!/usr/bin/env python3
# File: helpers/step_docs.py
# -*- coding: utf-8 -*-
"""
Helpers for rendering per-step documentation in Markdown.

This module owns:
  - Anchor / slug generation for step names.
  - Human-friendly pretty_name for CodeName.
  - Lightweight flattening of step config for display.
  - Markdown helpers for fenced blocks and option lines.
  - Branch tables for If / Switch containers.
  - The main `render_steps_md` function used by analyzer stage 1.

Analyzer should import these helpers instead of re-implementing them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from helpers.definition_model import StepInfo
from helpers.catalog_tools import resolve_labels


# ─────────────────────────────────────────────────────────────────────────────
# Anchor / display helpers
# ─────────────────────────────────────────────────────────────────────────────


def safe_slug(name: str) -> str:
    """
    Convert a step name into a safe, stable slug for anchors / filenames.

    Rules
    -----
      - Lowercase.
      - Replace any run of non-alphanumeric characters with a single underscore.
      - Strip leading / trailing underscores.
    """
    base = str(name or "").strip().lower()
    if not base:
        return "step"

    base = re.sub(r"[^a-z0-9]+", "_", base)
    base = base.strip("_") or "step"
    return base


def anchor(name: str) -> str:
    """
    Anchor ID used in markdown links. Keeps a small, readable prefix.
    """
    return safe_slug(name)


def pretty_name(name: str) -> str:
    """
    Human-friendly display name for a step when we only know the CodeName.

    Currently:
      - Replace underscores with spaces.
      - Collapse double spaces.
      - Strip leading/trailing whitespace.
    """
    text = str(name or "")
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip() or text


# ─────────────────────────────────────────────────────────────────────────────
# Generic markdown helpers
# ─────────────────────────────────────────────────────────────────────────────


def flatten(
    obj: Any,
    prefix: str = "",
    out: Optional[Dict[str, Any]] = None,
    depth: int = 2,
) -> Dict[str, Any]:
    """
    Flatten a nested JSON-like object into a dotted-key dict, up to `depth`.

    Example:
        {"inputs": {"body": {"foo": 1}}}
    becomes (with depth=2):
        {"inputs.body": {"foo": 1}}
    """
    if out is None:
        out = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if depth > 0 and isinstance(value, (dict, list)):
                flatten(value, dotted, out, depth - 1)
            else:
                out[dotted] = value
    elif isinstance(obj, list):
        out[prefix or "value"] = obj
    else:
        out[prefix or "value"] = obj

    return out


BACKTICKS = "```"


def md_fence(x: str) -> str:
    """
    Wrap a value in a fenced code block.
    """
    return f"{BACKTICKS}\n{x}\n{BACKTICKS}"


def option_line(key: str, value: Any) -> str:
    """
    Render a single option row for the step details section.

    Complex values (dict/list) are rendered as fenced JSON; HTTP-ish blobs
    and expression-like fields are rendered as fenced text; everything else
    is rendered as an inline code span.
    """
    key_lower = key.lower()

    if isinstance(value, (dict, list)):
        return f"- **{key}**:\n" + md_fence(
            json.dumps(value, ensure_ascii=False, indent=2)
        )

    text = "" if value is None else str(value)

    if (
        key_lower.startswith("inputs")
        or ".headers." in key_lower
        or key_lower.endswith(
            (".value", ".content", ".body", ".uri", ".expression")
        )
    ):
        return f"- **{key}**:\n" + md_fence(text)

    return f"- **{key}**: `{text}`"


# ─────────────────────────────────────────────────────────────────────────────
# Branch extraction / tables (sorted by run order)
# ─────────────────────────────────────────────────────────────────────────────


def md_branch_table(
    container_name: str,
    reg: Dict[str, StepInfo],
    order_index: Dict[str, int],
) -> Optional[str]:
    """
    Build a small markdown table for If / Switch containers, summarizing
    which steps live in each branch in creation-order order.
    """
    step_info = reg[container_name]
    raw = step_info.raw or {}

    def _linkify(name: str) -> str:
        return f"[{pretty_name(name)}](#{anchor(name)})"

    if step_info.atype == "If":
        then_steps = sorted(
            list((raw.get("actions") or {}).keys()),
            key=lambda x: (order_index.get(x, 10**9), x.lower()),
        )
        else_steps = sorted(
            list(((raw.get("else") or {}).get("actions") or {}).keys()),
            key=lambda x: (order_index.get(x, 10**9), x.lower()),
        )

        headers = ["Then", "Else", "Notes"]
        sep = ["---", "---", "---"]

        then_cell = "<br>".join(_linkify(x) for x in then_steps) if then_steps else ""
        else_cell = "<br>".join(_linkify(x) for x in else_steps) if else_steps else ""

        table: List[str] = []
        table.append("| " + " | ".join(headers) + " |")
        table.append("| " + " | ".join(sep) + " |")
        table.append("| " + " | ".join([then_cell, else_cell, ""]) + " |")
        return "\n".join(table)

    if step_info.atype == "Switch":
        cases = raw.get("cases") or {}
        case_pairs: List[Tuple[str, List[str]]] = []

        for label, body in cases.items():
            steps = sorted(
                list((body.get("actions") or {}).keys()),
                key=lambda x: (order_index.get(x, 10**9), x.lower()),
            )
            case_pairs.append((str(label), steps))

        case_pairs.sort(key=lambda t: t[0].lower())

        default_body = raw.get("default") or {}
        default_steps = (
            sorted(
                list((default_body.get("actions") or {}).keys()),
                key=lambda x: (order_index.get(x, 10**9), x.lower()),
            )
            if isinstance(default_body, dict)
            else []
        )

        headers: List[str] = [label for (label, _) in case_pairs] + [
            "Default",
            "Notes",
        ]
        sep = ["---"] * len(headers)

        cells: List[str] = []
        for _, steps in case_pairs:
            cells.append("<br>".join(_linkify(x) for x in steps) if steps else "")
        cells.append(
            "<br>".join(_linkify(x) for x in default_steps) if default_steps else ""
        )
        cells.append("")

        table = []
        table.append("| " + " | ".join(headers) + " |")
        table.append("| " + " | ".join(sep) + " |")
        table.append("| " + " | ".join(cells) + " |")
        return "\n".join(table)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering (PrettyName display, CodeName anchors)
# ─────────────────────────────────────────────────────────────────────────────

# NOTE: This constant is also used by analyzer when building history.csv.
HEAVY_PREFIXES: Tuple[str, ...] = (
    "actions",
    "cases",
    "default",
    "else",
    "branches",
)


def render_steps_md(
    title: str,
    reg: Dict[str, StepInfo],
    definition: Dict[str, Any],
    order_index: Dict[str, int],
    catalog: Dict[str, Any],
) -> str:
    """
    Render the full "Steps" section in Markdown, including:

      - Index of containers and non-container steps.
      - Detailed per-container sections with runAfter and branch tables.
      - Detailed per-step sections with flattened config and option lines.

    This function does not add any MRM markers; analyzer wraps the result
    with its own `mrm_wrap("Steps", ...)` and further MRM sub-markers.
    """
    container_names = sorted(
        [name for name, s in reg.items() if getattr(s, "is_container", False)],
        key=str.lower,
    )
    step_names = sorted(
        [name for name, s in reg.items() if not getattr(s, "is_container", False)],
        key=str.lower,
    )

    lines: List[str] = []

    title_line = definition.get("name") or title or "Logic App"
    lines.append(f"# {title_line}")
    lines.append("")
    lines.append('<a id="index"></a>')
    lines.append("## Index")

    # Index: containers
    if container_names:
        lines.append("### Containers")
        for name in container_names:
            lines.append(f"- [{pretty_name(name)}](#{anchor(name)})")
        lines.append("")

    # Index: steps
    if step_names:
        lines.append("### Steps")
        for name in step_names:
            lines.append(f"- [{pretty_name(name)}](#{anchor(name)})")
        lines.append("")

    # Container detail sections
    if container_names:
        lines.append("## Containers (alphabetical)")
        for name in container_names:
            step_info = reg[name]
            disp = pretty_name(name)

            lines.append(f"\n### {disp}\n<a id=\"{anchor(name)}\"></a>")
            lines.append(f"_Designer_PrettyName:_ **{disp}**")
            lines.append(f"_CodeName:_ **{name}**")
            lines.append(f"_StepTypeDesigner:_ **{step_info.atype_display}**")
            lines.append(f"_StepTypeCode:_ **{step_info.atype}**")
            lines.append(
                "_StepCategoryDesigner:_ **{}**".format(
                    resolve_labels(
                        step_info.atype_display,
                        step_info.atype,
                        catalog,
                    ).get("pretty_category", "")
                )
            )
            lines.append(
                f"<!-- analyzer: resolved_type={step_info.atype_display}; "
                f"raw_type={step_info.atype} -->"
            )

            if step_info.run_after:
                lines.append("- **runAfter**:")
                for pred, states in step_info.run_after.items():
                    lines.append(
                        f"  - **{pretty_name(pred)}** → states: "
                        f"`{', '.join(states)}`"
                    )

            children = [
                child_name
                for child_name, info in reg.items()
                if info.parent == name
            ]
            if children:
                lines.append("- **Contains**:")
                for child in sorted(
                    children,
                    key=lambda x: (order_index.get(x, 10**9), x.lower()),
                ):
                    lines.append(
                        f"  - [{pretty_name(child)}](#{anchor(child)})"
                    )

            table = md_branch_table(name, reg, order_index)
            if table:
                lines.append("")
                lines.append(table)
                lines.append("")

            lines.append(f"- **See figure**: **{disp}**")
            lines.append("[Back to Index](#index)\n")

    # Non-container step detail sections
    if step_names:
        lines.append("## Steps (alphabetical)")
        for name in step_names:
            step_info = reg[name]
            disp = pretty_name(name)

            # Strip heavy structural branches from the raw snapshot before
            # flattening so we keep the YAML-size under control.
            raw_light = {
                key: value
                for key, value in (step_info.raw or {}).items()
                if not any(key.startswith(prefix) for prefix in HEAVY_PREFIXES)
            }
            options = flatten(raw_light, depth=2)

            section_lines: List[str] = [
                f"\n### {disp}\n<a id=\"{anchor(name)}\"></a>",
                f"_Designer_PrettyName:_ **{disp}**",
                f"_CodeName:_ **{name}**",
                f"_StepTypeDesigner:_ **{step_info.atype_display}**",
                f"_StepTypeCode:_ **{step_info.atype}**",
                "_StepCategoryDesigner:_ **{}**".format(
                    resolve_labels(
                        step_info.atype_display,
                        step_info.atype,
                        catalog,
                    ).get("pretty_category", "")
                ),
            ]

            section_lines.append(
                f"<!-- analyzer: resolved_type={step_info.atype_display}; "
                f"raw_type={step_info.atype} -->"
            )

            if step_info.parent:
                section_lines.append(
                    f"_InsideContainer:_ **`{step_info.parent}`**"
                )

            if step_info.run_after:
                section_lines.append("- **runAfter**:")
                for pred, states in step_info.run_after.items():
                    section_lines.append(
                        f"  - **{pretty_name(pred)}** → states: "
                        f"`{', '.join(states)}`"
                    )

            # Special handling for inputs.variables
            if isinstance(options.get("inputs.variables"), list):
                section_lines.append("- **inputs.variables**:")
                for var in options["inputs.variables"]:
                    section_lines.append(
                        f"  - **name**: `{var.get('name', '')}`"
                    )
                    if var.get("type"):
                        section_lines.append(
                            f"    - **type**: `{var.get('type', '')}`"
                        )
                    section_lines.append(
                        "    - **initial value**:\n"
                        + md_fence(
                            ""
                            if var.get("value") is None
                            else str(var.get("value"))
                        )
                    )
                options.pop("inputs.variables", None)

            skip_keys = {"runAfter", "inputs.variables"}
            for key in sorted(
                k for k in options.keys() if k not in skip_keys
            ):
                if any(key.startswith(pref) for pref in HEAVY_PREFIXES):
                    continue
                section_lines.append(option_line(key, options[key]))

            section_lines.append("[Back to Index](#index)\n")
            lines.append("\n".join(section_lines))

    return "\n".join(lines) + "\n"
