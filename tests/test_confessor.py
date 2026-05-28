"""
Tier 4: Verifier tests — verify.py compare engine and report.

Tests the index/query logic against FreshMart fixtures.
Report rendering is tested via the data model, not Rich output.

NOTE: TUI (Textual interactive mode) and Browse mode are not yet
built — those tests will be added when implemented.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from confessor import (
    run_compare,
    CheckResult,
    VerifyReport,
    _find_var_value,
    _find_foreach_handler,
    _find_switch,
    _find_parse_schema,
    _check_structural,
)


@pytest.fixture
def freshmart_app_config():
    config_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "freshmart_app_config.json")
    with open(config_path) as f:
        return json.load(f)


@pytest.fixture
def freshmart_patched(freshmart_prior_path, freshmart_desired_values_path,
                       freshmart_materials_dir, freshmart_app_config, tmp_path):
    """Generate and apply patches to create a patched FreshMart JSON."""
    from utilities.generators.desired_values_parser import parse_desired_values
    from utilities.generators.generate_orthodox import (
        generate_patches, write_orthodox_yaml, _resolve_to_files,
    )
    import subprocess

    with open(freshmart_prior_path) as f:
        prior = json.load(f)
    desired = parse_desired_values(freshmart_desired_values_path)
    patches = generate_patches(
        desired, prior, freshmart_app_config, freshmart_materials_dir)
    _resolve_to_files(patches, freshmart_materials_dir)

    # Write YAML in a target_state subdir with materials symlink
    target_state = tmp_path / "target_state"
    target_state.mkdir()
    (tmp_path / "materials").symlink_to(freshmart_materials_dir)

    orthodox_path = str(target_state / "test.orthodox.yaml")
    write_orthodox_yaml(patches, freshmart_app_config, orthodox_path)

    output_path = str(tmp_path / "patched.json")
    log_path = str(tmp_path / "surgeon.log")
    surgeon_path = os.path.join(os.path.dirname(__file__), "..", "surgeon.py")

    result = subprocess.run(
        [sys.executable, surgeon_path,
         "--input", freshmart_prior_path,
         "--patch-task", orthodox_path,
         "--output", output_path,
         "--log", log_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Surgeon failed: {result.stderr}"
    return output_path


class TestCompareEngine:
    def test_all_pass_on_freshly_patched(self, freshmart_patched,
                                          freshmart_desired_values_path,
                                          freshmart_materials_dir):
        """A freshly patched JSON should pass all compare checks."""
        report = run_compare(
            freshmart_patched,
            freshmart_desired_values_path,
            freshmart_materials_dir,
        )
        assert report.all_pass, \
            f"Expected all pass, got {report.fail_count} failures: " \
            f"{[(c.field_name, c.detail) for c in report.checks if not c.passed]}"

    def test_check_count(self, freshmart_patched,
                          freshmart_desired_values_path,
                          freshmart_materials_dir):
        report = run_compare(
            freshmart_patched,
            freshmart_desired_values_path,
            freshmart_materials_dir,
        )
        # Config + email + css + schema + structural
        assert len(report.checks) > 0
        categories = set(c.category for c in report.checks)
        assert 'config' in categories
        assert 'email' in categories

    def test_mismatch_detected(self, freshmart_patched,
                                freshmart_desired_values_path,
                                freshmart_materials_dir, tmp_path):
        """Verify detects when desired_values don't match patched JSON."""
        # Create a desired_values with a wrong date
        content = open(freshmart_desired_values_path).read()
        content = content.replace(
            "`February 1, 2026`", "`March 99, 2099`")
        wrong_path = str(tmp_path / "wrong.v2.txt")
        with open(wrong_path, "w") as f:
            f.write(content)

        report = run_compare(
            freshmart_patched, wrong_path, freshmart_materials_dir)
        assert not report.all_pass
        assert report.fail_count >= 1
        failed_fields = [c.field_name for c in report.checks if not c.passed]
        assert 'effective_date' in failed_fields


class TestNavigation:
    def test_find_var_value(self, freshmart_prior_path):
        with open(freshmart_prior_path) as f:
            doc = json.load(f)
        actions = doc['definition']['actions']
        val = _find_var_value(actions, 'Initialize_Effective_Date', 'effective_date')
        assert val == 'January 15, 2026'

    def test_find_var_missing(self, freshmart_prior_path):
        with open(freshmart_prior_path) as f:
            doc = json.load(f)
        actions = doc['definition']['actions']
        val = _find_var_value(actions, 'Initialize_Effective_Date', 'nonexistent')
        assert val is None

    def test_find_foreach_handler(self, freshmart_prior_path):
        with open(freshmart_prior_path) as f:
            doc = json.load(f)
        actions = doc['definition']['actions']
        handle, name = _find_foreach_handler(actions)
        assert handle is not None
        assert name == 'Handle_Stores_From_List'

    def test_find_switch(self, freshmart_prior_path):
        with open(freshmart_prior_path) as f:
            doc = json.load(f)
        actions = doc['definition']['actions']
        handle, _ = _find_foreach_handler(actions)
        switch, name = _find_switch(handle)
        assert switch is not None
        assert name == 'Send_Per-Region_Emails'

    def test_find_parse_schema(self, freshmart_prior_path):
        with open(freshmart_prior_path) as f:
            doc = json.load(f)
        actions = doc['definition']['actions']
        schema, scope, parse = _find_parse_schema(actions)
        assert schema is not None
        assert scope == 'Get_List_of_Stores'
        assert parse == 'Parse_Store_List'


class TestHistory:
    def test_history_recorded(self, freshmart_patched,
                               freshmart_desired_values_path,
                               freshmart_materials_dir):
        """Running verify creates a .verify/ history file."""
        report = run_compare(
            freshmart_patched,
            freshmart_desired_values_path,
            freshmart_materials_dir,
        )
        from confessor import _record_run, _history_path, _load_history
        _record_run(report, freshmart_patched)
        hp = _history_path(freshmart_patched, report.app_name)
        assert os.path.exists(hp)
        history = _load_history(hp)
        assert len(history['runs']) == 1
        assert history['runs'][0]['all_pass'] is True
