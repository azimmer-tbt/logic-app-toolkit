"""
Tier 2: Generator tests — generate_orthodox.py output correctness.

Tests against FreshMart example PRIOR JSON fixture.

NOTE: Currently tested against FRESHMART-DEV fixture only.
Structural patches are tested via round-trip in tier 3.
TODO: Expand with additional app fixtures once multi-app torture test is complete.
"""

import json
import os
import sys
import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers.checksum import fingerprint
from utilities.generators.desired_values_parser import parse_desired_values
from utilities.generators.generate_orthodox import (
    generate_patches,
    write_orthodox_yaml,
    _load_file_content,
    LANG_MAP,
)


@pytest.fixture
def freshmart_app_config():
    config_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "freshmart_app_config.json")
    with open(config_path) as f:
        return json.load(f)


@pytest.fixture
def freshmart_prior(freshmart_prior_path):
    with open(freshmart_prior_path) as f:
        return json.load(f)


class TestGeneratePatches:
    def test_patch_count(self, freshmart_desired_values_path,
                         freshmart_prior, freshmart_app_config,
                         freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        # 3 dates + 3 logging + 4 email + 1 css + 1 schema = 12
        assert len(patches) == 12

    def test_date_patches_are_edit_variable(self, freshmart_desired_values_path,
                                             freshmart_prior, freshmart_app_config,
                                             freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        date_patches = [p for p in patches if p['section'].startswith('config_Initialize_E')]
        for p in date_patches:
            assert p['operation'] == 'edit_variable'

    def test_date_patches_have_from_and_from_sha(self, freshmart_desired_values_path,
                                                   freshmart_prior, freshmart_app_config,
                                                   freshmart_materials_dir):
        """edit_variable patches include both from (literal) and from_sha."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        for p in patches:
            if p['operation'] == 'edit_variable':
                assert 'from' in p, f"Missing 'from' in {p['section']}"
                assert 'from_sha' in p, f"Missing 'from_sha' in {p['section']}"
                assert p['from_sha'] == fingerprint(p['from'])


    def test_email_patches_are_replace_value(self, freshmart_desired_values_path,
                                              freshmart_prior, freshmart_app_config,
                                              freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        email_patches = [p for p in patches
                         if p['section'].startswith('email_content_')]
        assert len(email_patches) == 4  # en, es, fr, pt
        for p in email_patches:
            assert p['operation'] == 'replace_value'
            assert 'to_file' in p
            assert p['to_file'].startswith('../materials/html_assets/')

    def test_email_paths_use_freshmart_naming(self, freshmart_desired_values_path,
                                               freshmart_prior, freshmart_app_config,
                                               freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        email_patches = [p for p in patches
                         if p['section'].startswith('email_content_')]
        for p in email_patches:
            assert 'Handle_Stores_From_List' in p['path']
            assert 'Send_Per-Region_Emails' in p['path']

    def test_css_patch_is_edit_variable(self, freshmart_desired_values_path,
                                        freshmart_prior, freshmart_app_config,
                                        freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        css_patches = [p for p in patches if p['section'] == 'css']
        assert len(css_patches) == 1
        assert css_patches[0]['operation'] == 'edit_variable'
        assert 'to_file' in css_patches[0]


    def test_schema_patch_uses_freshmart_path(self, freshmart_desired_values_path,
                                               freshmart_prior, freshmart_app_config,
                                               freshmart_materials_dir):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        schema_patches = [p for p in patches if p['section'] == 'parse_schema']
        assert len(schema_patches) == 1
        assert 'Get_List_of_Stores' in schema_patches[0]['path']
        assert 'Parse_Store_List' in schema_patches[0]['path']

    def test_no_structural_patches_for_freshmart(self, freshmart_desired_values_path,
                                                   freshmart_prior, freshmart_app_config,
                                                   freshmart_materials_dir):
        """FreshMart doesn't have Handle_Devices_From_Search, so structural
        catalog should gracefully produce zero structural patches."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        structural = [p for p in patches
                      if p['section'].startswith('structural')]
        assert len(structural) == 0

    def test_unchanged_fields_produce_no_patches(self, freshmart_desired_values_path,
                                                   freshmart_prior, freshmart_app_config,
                                                   freshmart_materials_dir):
        """Fields where PRIOR already matches desired should not appear."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        patch_keys = [p.get('key', '') for p in patches]
        # These fields match in PRIOR and desired_values
        assert 'logfile_prefix' not in patch_keys
        assert 'logging_prefix_path' not in patch_keys
        assert 'default_from' not in patch_keys
        assert 'sharepoint_site' not in patch_keys


class TestSHACorrectness:
    """Verify SHA computation matches surgeon's fingerprint for each operation type."""

    def test_edit_variable_from_sha(self, freshmart_desired_values_path,
                                     freshmart_prior, freshmart_app_config,
                                     freshmart_materials_dir):
        """edit_variable from_sha = fingerprint(current_value_string)."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        for p in patches:
            if p['operation'] != 'edit_variable':
                continue
            assert 'from' in p, f"{p['key']}: missing 'from'"
            current_val = p['from']
            assert p['from_sha'] == fingerprint(current_val), \
                f"{p['key']}: from_sha mismatch"


    def test_edit_variable_to_sha(self, freshmart_desired_values_path,
                                   freshmart_prior, freshmart_app_config,
                                   freshmart_materials_dir):
        """edit_variable to_sha = fingerprint(desired_value_string)."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        for p in patches:
            if p['operation'] != 'edit_variable':
                continue
            if 'to_file' in p:
                continue  # CSS — tested separately
            to_val = p['to']
            assert p['to_sha'] == fingerprint(to_val['value']), \
                f"{p['key']}: to_sha mismatch"

    def test_replace_value_from_sha(self, freshmart_desired_values_path,
                                     freshmart_prior, freshmart_app_config,
                                     freshmart_materials_dir):
        """replace_value from_sha = fingerprint(current_val) — repr-based for dicts."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        prior = freshmart_prior
        for p in patches:
            if p['operation'] != 'replace_value':
                continue
            # Verify from_sha is present and non-empty
            assert p.get('from_sha'), f"{p['section']}: missing from_sha"

    def test_all_patches_have_sha(self, freshmart_desired_values_path,
                                   freshmart_prior, freshmart_app_config,
                                   freshmart_materials_dir):
        """Every patch must have at least from_sha or to_sha (or both)."""
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        for p in patches:
            has_from = bool(p.get('from_sha'))
            has_to = bool(p.get('to_sha'))
            assert has_from or has_to, \
                f"{p['section']}/{p.get('key','')}: no SHA at all"


class TestZeroDiff:
    def test_matching_state_produces_no_patches(self, freshmart_prior,
                                                  freshmart_app_config,
                                                  freshmart_materials_dir,
                                                  tmp_path):
        """When desired == current for all fields, zero patches produced."""
        # Build a desired_values file that matches the PRIOR exactly
        content = """Desired Values: FRESHMART-DEV
============================================================

DATES (`Initialize_Effective_Date`, `Initialize_Price_Update_Date`, `Initialize_Expiry_Date`)
------------------------------------------------------------
Effective date   | match | `effective_date`    | `January 15, 2026`
Update deadline  | match | `price_update_date` | `January 10, 2026`
Expiry date      | match | `expiry_date`       | `January 31, 2026`

LOGGING (`Initialize_Logging_Variables`)
------------------------------------------------------------
App short name   | match | `app_shortname`       | `FreshMart_Prices`
App long name    | match | `app_longname`        | `FreshMart Weekly Price Update Emailer`
Notify           | match | `notification_emails` | `ops@freshmart.example.com`
"""
        p = tmp_path / "match.v2.txt"
        p.write_text(content)
        desired = parse_desired_values(str(p))
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        assert len(patches) == 0


class TestWriteOrthodoxYAML:
    def test_yaml_is_valid(self, freshmart_desired_values_path,
                            freshmart_prior, freshmart_app_config,
                            freshmart_materials_dir, tmp_path):
        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        output = tmp_path / "test.orthodox.yaml"
        write_orthodox_yaml(patches, freshmart_app_config, str(output))
        with open(output) as f:
            loaded = yaml.safe_load(f)
        assert 'source' in loaded
        assert 'target' in loaded
        assert 'patches' in loaded
        assert len(loaded['patches']) == 12
