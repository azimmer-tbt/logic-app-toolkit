"""
Tier 3: Surgeon round-trip tests — generate → surgeon → verify.

Tests against FreshMart example PRIOR JSON fixture.
FreshMart exercises config + content patches (no structural catalog).

NOTE: Full structural round-trip tested manually against real app
      data (see TOOLING_SESSION_1.md acceptance test results).
TODO: Add round-trip for additional apps once multi-app torture test
      is complete.
"""

import json
import os
import sys
import subprocess
import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utilities.generators.desired_values_parser import parse_desired_values
from utilities.generators.generate_orthodox import (
    generate_patches,
    write_orthodox_yaml,
    _resolve_to_files,
)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


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


def _run_surgeon(prior_path, orthodox_path, output_path, log_path):
    """Run surgeon.py as a subprocess, return exit code."""
    surgeon_path = os.path.join(REPO_ROOT, "surgeon.py")
    result = subprocess.run(
        [sys.executable, surgeon_path,
         "--input", prior_path,
         "--patch-task", orthodox_path,
         "--output", output_path,
         "--log", log_path],
        capture_output=True, text=True,
    )
    return result


def _setup_test_tree(tmp_path, freshmart_materials_dir):
    """Create a target_state/ dir in tmp_path with a materials/ symlink
    next to it, so to_file: ../materials/... resolves correctly."""
    target_state = tmp_path / "target_state"
    target_state.mkdir()
    # Symlink materials as a sibling of target_state
    materials_link = tmp_path / "materials"
    materials_link.symlink_to(freshmart_materials_dir)
    return target_state


class TestSurgeonRoundTrip:
    def test_generate_and_apply_clean(self, freshmart_desired_values_path,
                                       freshmart_prior_path,
                                       freshmart_prior, freshmart_app_config,
                                       freshmart_materials_dir, tmp_path):
        """Full round trip: generate YAML → surgeon apply → all checks pass."""
        target_state = _setup_test_tree(tmp_path, freshmart_materials_dir)

        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        _resolve_to_files(patches, freshmart_materials_dir)

        orthodox_path = str(target_state / "test.orthodox.yaml")
        write_orthodox_yaml(patches, freshmart_app_config, orthodox_path)

        output_path = str(tmp_path / "patched.json")
        log_path = str(tmp_path / "surgeon.log")

        result = _run_surgeon(
            freshmart_prior_path, orthodox_path, output_path, log_path)

        log_content = open(log_path).read() if os.path.exists(log_path) else ""

        assert result.returncode == 0, \
            f"Surgeon failed:\nstdout: {result.stdout}\nstderr: {result.stderr}\nlog:\n{log_content}"
        assert "RESULT: CLEAN" in log_content, \
            f"Surgeon not clean:\n{log_content}"


    def test_idempotency(self, freshmart_desired_values_path,
                          freshmart_prior_path,
                          freshmart_prior, freshmart_app_config,
                          freshmart_materials_dir, tmp_path):
        """Apply once, then generate again against patched output → zero patches."""
        target_state = _setup_test_tree(tmp_path, freshmart_materials_dir)

        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        _resolve_to_files(patches, freshmart_materials_dir)

        orthodox_path = str(target_state / "first.orthodox.yaml")
        write_orthodox_yaml(patches, freshmart_app_config, orthodox_path)

        output_path = str(tmp_path / "patched.json")
        log_path = str(tmp_path / "surgeon.log")

        result = _run_surgeon(
            freshmart_prior_path, orthodox_path, output_path, log_path)
        assert result.returncode == 0

        # Now generate again against the patched output
        with open(output_path) as f:
            patched_doc = json.load(f)
        second_patches = generate_patches(
            desired, patched_doc, freshmart_app_config,
            freshmart_materials_dir)
        assert len(second_patches) == 0, \
            f"Expected zero patches on second pass, got {len(second_patches)}: " \
            f"{[p.get('section') + '/' + p.get('key','') for p in second_patches]}"

    def test_wrong_sha_causes_surgeon_failure(self, freshmart_desired_values_path,
                                                freshmart_prior_path,
                                                freshmart_prior, freshmart_app_config,
                                                freshmart_materials_dir, tmp_path):
        """A patch with a deliberately wrong from_sha should fail pre-op."""
        target_state = _setup_test_tree(tmp_path, freshmart_materials_dir)

        desired = parse_desired_values(freshmart_desired_values_path)
        patches = generate_patches(
            desired, freshmart_prior, freshmart_app_config,
            freshmart_materials_dir)
        _resolve_to_files(patches, freshmart_materials_dir)

        # Corrupt the first patch's from_sha
        patches[0]['from_sha'] = 'deadbeef0000'

        orthodox_path = str(target_state / "bad.orthodox.yaml")
        write_orthodox_yaml(patches, freshmart_app_config, orthodox_path)

        output_path = str(tmp_path / "patched.json")
        log_path = str(tmp_path / "surgeon.log")

        result = _run_surgeon(
            freshmart_prior_path, orthodox_path, output_path, log_path)
        # Surgeon should fail (exit 1) due to pre-op SHA mismatch
        assert result.returncode == 1
