#!/usr/bin/env python3
# filename: generate_orthodox.py
"""
Orthodox YAML generator for Logic App patching.

Reads a v2 desired_values file + a PRIOR Logic App JSON + the
materials tree on disk. Diffs everything. Emits an orthodox YAML
with correct SHAs computed via surgeon's own fingerprint().

This is the keystone of the patching pipeline. Its output is
consumed by surgeon.py to produce the patched Logic App JSON.

Usage:
    python3 generate_orthodox.py \\
        --input-desired-values target_state/desired_values_FRESHMART-DEV.v2.txt \\
        --input-prior PRIOR/FRESHMART-DEV-PRICES.json \\
        --input-materials materials/ \\
        --output-orthodox target_state/FRESHMART-DEV.orthodox.yaml \\
        [--dry-run] [--logfile generate_FRESHMART-DEV.log]

Exit codes:
    0 = YAML written with N patches (or dry-run found N diffs)
    1 = error (missing files, parse failure)
    2 = zero patches (current matches desired)
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Path setup — reach helpers/ two levels up from utilities/generators/
# ─────────────────────────────────────────────────────────────────────────────

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from helpers.checksum import fingerprint
from utilities.generators.desired_values_parser import (
    parse_desired_values, DesiredValues, Section, Field,
)


# ─────────────────────────────────────────────────────────────────────────────
# APP_REGISTRY — built-in example apps (FreshMart)
# For real deployments, use --app-config (single app JSON) or
# --app-registry (multi-app JSON with app names as keys).
# ─────────────────────────────────────────────────────────────────────────────

APP_REGISTRY: Dict[str, Dict[str, str]] = {
    'FRESHMART-DEV': {
        'prior_file': 'FRESHMART-DEV-PRICES.json',
        'desired_values': 'desired_values_FRESHMART-DEV.v2.txt',
        'mode': 'dev',
        'html_subdir': 'FRESHMART-DEV',
        'contractor_key': '',
        'schema_file': 'parse_schema_dev_stores.json',
        'search_type': 'stores',
        'wrapper_key': 'stores',
        'device_array_key': 'stores',
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Language mapping — Switch case paths and Compose action names
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
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_orthodox",
        description=f"Orthodox YAML generator v{__version__}",
    )
    p.add_argument('--input-desired-values', required=True,
                   help='Path to the v2 desired_values file')
    p.add_argument('--input-prior', required=True,
                   help='Path to the PRIOR Logic App JSON')
    p.add_argument('--input-materials', required=True,
                   help='Path to the materials/ directory')
    p.add_argument('--output-orthodox', default=None,
                   help='Path to write the orthodox YAML')
    p.add_argument('--dry-run', action='store_true',
                   help='Print diff summary only, write nothing')
    p.add_argument('--app-config', default=None,
                   help='Path to single-app config JSON (overrides registry)')
    p.add_argument('--app-registry', default=None,
                   help='Path to multi-app registry JSON (keys are app names)')
    p.add_argument('--logfile', default=None,
                   help='Path to write operational log')
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Load helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_prior(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_file_content(file_path: str):
    """
    Load a file, trying JSON first, falling back to raw text.
    Matches surgeon's _resolve_to_files behavior.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()


def _find_var_in_action(actions: dict, action_name: str, var_name: str):
    """
    Find a variable entry by name within an InitializeVariable action.
    Returns (var_entry, var_index) or (None, -1) if not found.
    """
    action = actions.get(action_name)
    if action is None or action.get("type") != "InitializeVariable":
        return None, -1
    variables = action.get("inputs", {}).get("variables", [])
    for i, v in enumerate(variables):
        if isinstance(v, dict) and v.get("name") == var_name:
            return v, i
    return None, -1


# ─────────────────────────────────────────────────────────────────────────────
# Diff functions — compare desired vs PRIOR, emit patch dicts
# ─────────────────────────────────────────────────────────────────────────────

def _diff_literal_field(
    field: Field,
    section: Section,
    actions: dict,
) -> List[Dict[str, Any]]:
    """Diff a literal string value against variables in the PRIOR."""
    patches = []
    for action_name in section.actions:
        var_entry, _ = _find_var_in_action(actions, action_name, field.name)
        if var_entry is None:
            continue
        current = var_entry.get("value", "")
        if str(current) != field.value:
            patches.append({
                'section': f'config_{action_name}',
                'operation': 'edit_variable',
                'path': f'definition.actions.{action_name}',
                'key': field.name,
                'from': current,
                'to': {
                    'name': field.name,
                    'type': var_entry.get('type', 'string'),
                    'value': field.value,
                },
                'from_sha': fingerprint(current),
                'to_sha': fingerprint(field.value),
            })
    return patches


def _diff_css_field(
    field: Field,
    section: Section,
    actions: dict,
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """Diff a CSS file reference against the shared_css_style variable."""
    patches = []
    file_path = os.path.join(materials_dir, field.file_path)
    content = _load_file_content(file_path)

    for action_name in section.actions:
        var_entry, _ = _find_var_in_action(actions, action_name, field.name)
        if var_entry is None:
            continue
        current_value = var_entry.get("value", "")
        # CSS is stored as a wrapper JSON: {"value": "...css..."}
        # For edit_variable, surgeon fingerprints the variable value string.
        # The to_file content is the wrapper; surgeon extracts wrapper['value'].
        current_sha = fingerprint(current_value)
        # to_sha: surgeon loads the wrapper JSON, then does
        # fingerprint(wrapper['value']) for edit_variable post-op.
        if isinstance(content, dict) and 'value' in content:
            to_sha = fingerprint(content['value'])
        else:
            to_sha = fingerprint(content)
        if current_sha != to_sha:
            patches.append({
                'section': 'css',
                'operation': 'edit_variable',
                'path': f'definition.actions.{action_name}',
                'key': field.name,
                'from': current_value,
                'from_sha': current_sha,
                'to_sha': to_sha,
                'to_file': _to_file_path(field.file_path),
            })
    return patches


def _to_file_path(materials_relative: str) -> str:
    """Convert a materials-relative path to a to_file path.
    Orthodox YAMLs live in target_state/, materials is a sibling dir.
    So to_file paths are ../materials/<path>."""
    return f"../materials/{materials_relative}"


def _diff_email_content(
    field: Field,
    actions: dict,
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """Compare on-disk HTML against the Compose step inputs in PRIOR."""
    patches = []
    lang = field.name
    if lang not in LANG_MAP:
        return patches

    case_path, compose_name = LANG_MAP[lang]
    file_path = os.path.join(materials_dir, field.file_path)
    html_content = open(file_path, "r", encoding="utf-8").read()

    # Navigate to the Compose step in the PRIOR
    # Support multiple naming conventions for the foreach handler and switch
    try:
        core_actions = actions['Core_-_Main_Workflow']['actions']
        # Find the foreach handler — try both naming conventions
        if 'Handle_Devices_From_Search' in core_actions:
            handle = core_actions['Handle_Devices_From_Search']['actions']
        elif 'Handle_Stores_From_List' in core_actions:
            handle = core_actions['Handle_Stores_From_List']['actions']
        else:
            return patches
        # Find the Switch — try both naming conventions
        if 'Send_Per-Language_Emails' in handle:
            sple = handle['Send_Per-Language_Emails']
            switch_name = 'Send_Per-Language_Emails'
        elif 'Send_Per-Region_Emails' in handle:
            sple = handle['Send_Per-Region_Emails']
            switch_name = 'Send_Per-Region_Emails'
        else:
            return patches
        node = sple
        for part in case_path.split('.'):
            node = node[part]
        compose_inputs = node['actions'][compose_name]['inputs']
    except KeyError:
        return patches

    # SHA comparison:
    # replace_value pre-op: fingerprint(current_val) — repr-based for dicts
    # replace_value post-op with to_file HTML: fingerprint(html_string) — string path
    current_sha = fingerprint(compose_inputs)
    to_sha = fingerprint(html_content)

    # Determine the foreach handler name for path construction
    if 'Handle_Devices_From_Search' in core_actions:
        handler_name = 'Handle_Devices_From_Search'
    else:
        handler_name = 'Handle_Stores_From_List'

    if current_sha != to_sha:
        if case_path == 'default':
            json_path = (f'definition.actions.Core_-_Main_Workflow.actions.'
                        f'{handler_name}.actions.'
                        f'{switch_name}.default.actions.'
                        f'{compose_name}.inputs')
        else:
            json_path = (f'definition.actions.Core_-_Main_Workflow.actions.'
                        f'{handler_name}.actions.'
                        f'{switch_name}.{case_path}.actions.'
                        f'{compose_name}.inputs')
        patches.append({
            'section': f'email_content_{lang}',
            'operation': 'replace_value',
            'path': json_path,
            'from_sha': current_sha,
            'to_file': _to_file_path(field.file_path),
            'to_sha': to_sha,
        })
    return patches


def _diff_schema(
    field: Field,
    actions: dict,
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """Compare on-disk parse schema against the PRIOR's Parse_Advanced_Search."""
    patches = []
    file_path = os.path.join(materials_dir, field.file_path)
    desired_schema = _load_file_content(file_path)

    try:
        core_actions = actions['Core_-_Main_Workflow']['actions']
        # Find the list-getter scope — try both naming conventions
        if 'Get_List_of_Devices' in core_actions:
            get_scope = core_actions['Get_List_of_Devices']['actions']
            parse_name = 'Parse_Advanced_Search'
        elif 'Get_List_of_Stores' in core_actions:
            get_scope = core_actions['Get_List_of_Stores']['actions']
            parse_name = 'Parse_Store_List'
        else:
            return patches
        parse_action = get_scope[parse_name]
        current_schema = parse_action['inputs']['schema']
    except KeyError:
        return patches

    # Determine scope name for path
    if 'Get_List_of_Devices' in core_actions:
        scope_name = 'Get_List_of_Devices'
    else:
        scope_name = 'Get_List_of_Stores'

    # replace_value: fingerprint uses repr() for dicts
    current_sha = fingerprint(current_schema)
    to_sha = fingerprint(desired_schema)

    if current_sha != to_sha:
        patches.append({
            'section': 'parse_schema',
            'operation': 'replace_value',
            'path': f'definition.actions.Core_-_Main_Workflow.actions.'
                    f'{scope_name}.actions.{parse_name}.inputs.schema',
            'from_sha': current_sha,
            'to_file': _to_file_path(field.file_path),
            'to_sha': to_sha,
        })
    return patches


def _diff_field(
    field: Field,
    section: Section,
    actions: dict,
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """Route a field to the right diff function."""
    if not field.is_file_ref:
        return _diff_literal_field(field, section, actions)

    # File reference — determine type by field name
    if field.name in LANGUAGE_CODES:
        return _diff_email_content(field, actions, materials_dir)
    elif field.name == 'shared_css_style':
        return _diff_css_field(field, section, actions, materials_dir)
    elif field.name == 'schema':
        return _diff_schema(field, actions, materials_dir)
    else:
        # Generic file field — skip silently for now.
        # Structural file refs are handled by check_structural_patches.
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Structural patch catalog — hardcoded checks, applied if missing
# ─────────────────────────────────────────────────────────────────────────────

def _check_structural_patches(
    actions: dict,
    app_config: Dict[str, str],
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """Check for structural patches. Apply if missing from PRIOR.
    Gracefully skips if expected paths don't exist (e.g. FreshMart fixtures)."""
    patches = []

    try:
        handle = actions['Core_-_Main_Workflow']['actions'] \
                        ['Handle_Devices_From_Search']['actions']
    except KeyError:
        # Non-standard app structure — skip structural patches
        return patches

    # 1. Test mode variables — add if missing
    if 'Initialize_Test_Mode_Switch' not in actions:
        patches.append({
            'section': 'structural_init',
            'operation': 'add_action',
            'path': 'definition.actions',
            'key': 'Initialize_Test_Mode_Switch',
            'to_file': _to_file_path('steps/Initialize_Test_Mode_Switch.json'),
        })

    if 'Initialize_Test_Mode_Email' not in actions:
        patches.append({
            'section': 'structural_init',
            'operation': 'add_action',
            'path': 'definition.actions',
            'key': 'Initialize_Test_Mode_Email',
            'to_file': _to_file_path('steps/Initialize_Test_Mode_Email.json'),
        })

    # 2. Rewire Initialize_Email_Variables.runAfter to point at
    #    Initialize_Test_Mode_Email (if not already)
    email_vars_ra = actions.get('Initialize_Email_Variables', {}).get('runAfter', {})
    if 'Initialize_Test_Mode_Email' not in email_vars_ra:
        patches.append({
            'section': 'structural_init',
            'operation': 'replace_value',
            'path': 'definition.actions.Initialize_Email_Variables.runAfter',
            'from_sha': fingerprint(email_vars_ra),
            'to': {'Initialize_Test_Mode_Email': ['Succeeded']},
            'to_sha': fingerprint({'Initialize_Test_Mode_Email': ['Succeeded']}),
        })

    # 3. device_country_code — add_variable if missing
    dev_user_vars = actions.get('Initialize_Device_and_User_Variables', {}) \
                          .get('inputs', {}).get('variables', [])
    has_country = any(v.get('name') == 'device_country_code' for v in dev_user_vars)
    if not has_country:
        patches.append({
            'section': 'structural_init',
            'operation': 'add_variable',
            'path': 'definition.actions.Initialize_Device_and_User_Variables',
            'key': 'device_country_code',
            'to': {'name': 'device_country_code', 'type': 'string', 'value': 'NULL'},
            'to_sha': fingerprint('NULL'),
        })


    # 4. Clear_Device_Country_Code — add if missing
    clear_actions = handle.get('Clear_Prior_Values', {}).get('actions', {})
    if 'Clear_Device_Country_Code' not in clear_actions:
        patches.append({
            'section': 'structural_foreach',
            'operation': 'add_action',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions.Clear_Prior_Values.actions',
            'key': 'Clear_Device_Country_Code',
            'to_file': _to_file_path('steps/Clear_Device_Country_Code.json'),
        })

    # 5. Set_Device_Country_Code — add if missing
    analyze_actions = handle.get('Analyze_Device_Info_and_Set_User_Data', {}) \
                           .get('actions', {})
    if 'Set_Device_Country_Code' not in analyze_actions:
        patches.append({
            'section': 'structural_foreach',
            'operation': 'add_action',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions.'
                    'Analyze_Device_Info_and_Set_User_Data.actions',
            'key': 'Set_Device_Country_Code',
            'to_file': _to_file_path('steps/Set_Device_Country_Code.json'),
        })


    # 6. Remove_Contractor_Tag → Overrides_Scope
    contractor_key = app_config['contractor_key']
    if contractor_key in handle:
        ct_val = handle[contractor_key]
        patches.append({
            'section': 'structural_foreach',
            'operation': 'remove_action',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions',
            'key': contractor_key,
            'from_sha': fingerprint(
                json.dumps(ct_val, sort_keys=True, ensure_ascii=False)
            ),
        })

    if 'Overrides_Scope' not in handle:
        patches.append({
            'section': 'structural_foreach',
            'operation': 'add_action',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions',
            'key': 'Overrides_Scope',
            'to_file': _to_file_path('steps/Overrides_Scope.json'),
        })

    # 7. Rewire Send_Per-Language_Emails.runAfter to Overrides_Scope
    sple = handle.get('Send_Per-Language_Emails', {})
    sple_ra = sple.get('runAfter', {})
    if 'Overrides_Scope' not in sple_ra:
        patches.append({
            'section': 'structural_foreach',
            'operation': 'replace_value',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions.'
                    'Send_Per-Language_Emails.runAfter',
            'from_sha': fingerprint(sple_ra),
            'to': {'Overrides_Scope': ['Succeeded']},
            'to_sha': fingerprint({'Overrides_Scope': ['Succeeded']}),
        })

    # 8. Switch expression fix
    switch_expr = sple.get('expression', '')
    if switch_expr != "@variables('device_language')":
        target = "@variables('device_language')"
        patches.append({
            'section': 'switch_fix',
            'operation': 'replace_value',
            'path': 'definition.actions.Core_-_Main_Workflow.actions.'
                    'Handle_Devices_From_Search.actions.'
                    'Send_Per-Language_Emails.expression',
            'from': switch_expr,
            'from_sha': fingerprint(switch_expr),
            'to': target,
            'to_sha': fingerprint(target),
        })

    return patches


# ─────────────────────────────────────────────────────────────────────────────
# Main generation logic
# ─────────────────────────────────────────────────────────────────────────────

def generate_patches(
    desired: DesiredValues,
    doc: Dict[str, Any],
    app_config: Dict[str, str],
    materials_dir: str,
) -> List[Dict[str, Any]]:
    """
    Generate the full patch list by diffing desired state against PRIOR.

    Phase 1: Config/content patches from desired_values pipe rows.
    Phase 2: Structural patches (always-check, apply-if-missing).
    """
    patches = []
    actions = doc['definition']['actions']

    # Phase 1: Desired values diff
    for section in desired.sections:
        # Skip STRUCTURAL section — handled by phase 2
        if section.description.upper().startswith('STRUCTURAL'):
            continue
        for field in section.fields:
            patches.extend(
                _diff_field(field, section, actions, materials_dir)
            )

    # Phase 2: Structural patches
    patches.extend(
        _check_structural_patches(actions, app_config, materials_dir)
    )

    return patches


def _resolve_to_files(patches: List[Dict[str, Any]], materials_dir: str) -> None:
    """
    For patches with to_file, resolve the file content and compute SHAs.
    This handles the same logic as surgeon's _resolve_to_files but
    we do it here so we can emit correct to_sha values in the YAML.

    Note: we do NOT replace to_file with to in the patches — the
    orthodox YAML keeps to_file references for auditability. We just
    need to compute to_sha from the file content.
    """
    for patch in patches:
        to_file = patch.get('to_file')
        if to_file is None:
            continue
        if 'to_sha' in patch:
            continue  # Already computed (e.g. structural patches)

        # Resolve relative to materials dir
        # to_file is ../materials/<path>, strip the ../materials/ prefix
        if to_file.startswith('../materials/'):
            rel_path = to_file[len('../materials/'):]
        else:
            rel_path = to_file
        file_path = os.path.join(materials_dir, rel_path)
        content = _load_file_content(file_path)

        # SHA depends on what surgeon will do with the content:
        operation = patch.get('operation', 'replace_value')
        key = patch.get('key', '')

        if operation == 'add_action':
            # Auto-unwrap: if single-key dict matching patch.key, use inner
            if isinstance(content, dict) and len(content) == 1 and key in content:
                content = content[key]
            # add_action post-op: fingerprint(json.dumps(val, sort_keys, ensure_ascii=False))
            patch['to_sha'] = fingerprint(
                json.dumps(content, sort_keys=True, ensure_ascii=False)
            )
        elif operation == 'edit_variable':
            # edit_variable with to_file: surgeon loads the wrapper,
            # extracts wrapper['value'], sets variable value.
            # post-op SHA: fingerprint(wrapper['value'])
            if isinstance(content, dict) and 'value' in content:
                patch['to_sha'] = fingerprint(content['value'])
            else:
                patch['to_sha'] = fingerprint(content)
        else:
            # replace_value: fingerprint(loaded_content)
            # For JSON files this is repr-based, for text it's string-based
            patch['to_sha'] = fingerprint(content)


def write_orthodox_yaml(
    patches: List[Dict[str, Any]],
    app_config: Dict[str, str],
    output_path: str,
) -> None:
    """Write the orthodox YAML file."""
    orthodox = {
        'source': {
            'file': app_config['prior_file'],
            'description': f"{app_config['mode']} app — prior state",
        },
        'target': {
            'name': app_config['prior_file'].replace('.json', ''),
            'description': f"{app_config['mode']} — patched to desired state",
        },
        'patches': patches,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(orthodox, f, default_flow_style=False,
                  sort_keys=False, allow_unicode=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _build_parser().parse_args()

    if not args.output_orthodox and not args.dry_run:
        print("ERROR: Must specify --output-orthodox or --dry-run",
              file=sys.stderr)
        return 1

    # Parse desired values
    try:
        desired = parse_desired_values(args.input_desired_values)
    except FileNotFoundError:
        print(f"ERROR: Desired values not found: {args.input_desired_values}",
              file=sys.stderr)
        return 1

    # Resolve app config — priority: --app-config > --app-registry > built-in
    app_name = desired.app_name
    if args.app_config:
        with open(args.app_config, "r", encoding="utf-8") as f:
            app_config = json.load(f)
    elif args.app_registry:
        with open(args.app_registry, "r", encoding="utf-8") as f:
            registry = json.load(f)
        if app_name not in registry:
            print(f"ERROR: App '{app_name}' not in registry {args.app_registry}. "
                  f"Available: {', '.join(sorted(registry.keys()))}",
                  file=sys.stderr)
            return 1
        app_config = registry[app_name]
    elif app_name in APP_REGISTRY:
        app_config = APP_REGISTRY[app_name]
    else:
        print(f"ERROR: Unknown app '{app_name}'. "
              f"Provide --app-config, --app-registry, or use a built-in app: "
              f"{', '.join(sorted(APP_REGISTRY.keys()))}",
              file=sys.stderr)
        return 1

    # Load PRIOR
    try:
        doc = _load_prior(args.input_prior)
    except FileNotFoundError:
        print(f"ERROR: PRIOR not found: {args.input_prior}",
              file=sys.stderr)
        return 1

    materials_dir = args.input_materials

    # Generate patches
    patches = generate_patches(desired, doc, app_config, materials_dir)

    # Resolve to_file SHAs
    _resolve_to_files(patches, materials_dir)

    if not patches:
        print(f"No patches needed — {app_name} matches desired state.")
        return 2

    if args.dry_run:
        print(f"Diffs found for {app_name}: {len(patches)} patches needed")
        for i, p in enumerate(patches, 1):
            op = p.get('operation', 'replace_value')
            section = p.get('section', '')
            key = p.get('key', '')
            if 'from' in p and 'to' in p:
                from_val = p['from']
                to_val = p['to']
                if isinstance(to_val, dict) and 'value' in to_val:
                    to_val = to_val['value']
                print(f"  [{i}] {section} ({op}): {key or p.get('path','').split('.')[-1]}"
                      f"  {from_val!r} → {to_val!r}")
            else:
                print(f"  [{i}] {section} ({op}): {key or p.get('path','').split('.')[-1]}")
        return 0

    # Write YAML
    write_orthodox_yaml(patches, app_config, args.output_orthodox)
    print(f"Wrote {args.output_orthodox} ({len(patches)} patches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
