"""
Shared pytest fixtures for logic-app-toolkit tests.

NOTE: Currently using FRESHMART-DEV fixtures only.
TODO: Expand with additional app fixtures once multi-app torture test is complete.
"""

import os
import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def freshmart_prior_path():
    return os.path.join(FIXTURES_DIR, "PRIOR", "FRESHMART-DEV-PRICES.json")


@pytest.fixture
def freshmart_desired_values_path():
    return os.path.join(FIXTURES_DIR, "desired_values",
                        "desired_values_FRESHMART-DEV.v2.txt")


@pytest.fixture
def freshmart_materials_dir():
    return os.path.join(FIXTURES_DIR, "materials")


@pytest.fixture
def freshmart_app_config_path():
    return os.path.join(FIXTURES_DIR, "freshmart_app_config.json")
