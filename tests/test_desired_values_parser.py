"""
Tier 1: Parser tests — desired_values v2 format.

Pure string parsing, no Logic App knowledge, no SHAs.
Tests the parser in isolation against fixture files and inline strings.

NOTE: Currently tested against FRESHMART-DEV fixture only.
TODO: Add edge case fixtures from additional desired_values files
      once multi-app torture test is complete.
"""

import os
import sys
import pytest

# Add repo root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utilities.generators.desired_values_parser import (
    parse_desired_values,
    extract_backtick,
    extract_backticked_names,
    DesiredValues,
    Section,
    Field,
)


class TestExtractBacktick:
    def test_simple(self):
        assert extract_backtick("`hello`") == "hello"

    def test_with_surrounding_text(self):
        assert extract_backtick("some text `value` more text") == "value"

    def test_no_backticks(self):
        assert extract_backtick("no backticks here") is None

    def test_empty_backticks(self):
        # empty backticks ``: the regex requires at least one char
        assert extract_backtick("``") is None

    def test_multiple_backtick_pairs(self):
        # takes the first pair
        assert extract_backtick("`first` and `second`") == "first"

    def test_backtick_with_special_chars(self):
        assert extract_backtick("`@{variables('api_server')}`") == \
            "@{variables('api_server')}"

    def test_file_reference(self):
        assert extract_backtick("`file:html_assets/en/message.html`") == \
            "file:html_assets/en/message.html"


class TestExtractBacktickedNames:
    def test_single_action(self):
        line = "LOGGING (`Initialize_Logging_Variables`)"
        assert extract_backticked_names(line) == ["Initialize_Logging_Variables"]

    def test_multiple_actions(self):
        line = "DATES (`Init_A`, `Init_B`, `Init_C`)"
        assert extract_backticked_names(line) == ["Init_A", "Init_B", "Init_C"]

    def test_no_parentheses(self):
        line = "STRUCTURAL (do not edit)"
        assert extract_backticked_names(line) == []

    def test_parentheses_no_backticks(self):
        line = "STRUCTURAL (do not edit — generator handles automatically)"
        assert extract_backticked_names(line) == []


class TestParseDesiredValues:
    def test_freshmart_fixture(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        assert result.app_name == "FRESHMART-DEV"
        assert len(result.sections) == 8

    def test_section_names(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        descriptions = [s.description for s in result.sections]
        assert any("DATES" in d for d in descriptions)
        assert any("LOGGING" in d for d in descriptions)
        assert any("EMAIL CONTENT" in d for d in descriptions)
        assert any("STRUCTURAL" in d for d in descriptions)

    def test_dates_section(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        dates = result.sections[0]
        assert len(dates.actions) == 3
        assert "Initialize_Effective_Date" in dates.actions
        assert len(dates.fields) == 3
        assert dates.fields[0].name == "effective_date"
        assert dates.fields[0].value == "February 1, 2026"
        assert dates.fields[0].is_file_ref is False

    def test_email_content_section(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        # Find the EMAIL CONTENT section
        email_section = None
        for s in result.sections:
            if "EMAIL CONTENT" in s.description:
                email_section = s
                break
        assert email_section is not None
        assert len(email_section.fields) == 4  # en, es, fr, pt
        for f in email_section.fields:
            assert f.is_file_ref is True
            assert f.file_path.startswith("html_assets/FRESHMART-DEV/")

    def test_structural_section_no_actions(self, freshmart_desired_values_path):
        """STRUCTURAL section has no backticked action names in parentheses."""
        result = parse_desired_values(freshmart_desired_values_path)
        structural = result.sections[-1]
        assert "STRUCTURAL" in structural.description
        assert structural.actions == []
        assert len(structural.fields) == 7

    def test_total_field_count(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        total = sum(len(s.fields) for s in result.sections)
        assert total == 25

    def test_file_ref_count(self, freshmart_desired_values_path):
        result = parse_desired_values(freshmart_desired_values_path)
        file_refs = sum(1 for s in result.sections
                        for f in s.fields if f.is_file_ref)
        assert file_refs == 11


class TestParserEdgeCases:
    def test_pipe_in_description(self, tmp_path):
        """Extra pipes in description column don't break parsing."""
        content = """Desired Values: TEST
============================================================

DATES (`Init_Date`)
------------------------------------------------------------
Force date | has | pipes | in | notes | `force_date` | `May 27`
"""
        p = tmp_path / "test.v2.txt"
        p.write_text(content)
        result = parse_desired_values(str(p))
        assert len(result.sections) == 1
        assert result.sections[0].fields[0].name == "force_date"
        assert result.sections[0].fields[0].value == "May 27"

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.v2.txt"
        p.write_text("")
        result = parse_desired_values(str(p))
        assert result.app_name == ""
        assert result.sections == []

    def test_section_with_no_pipe_rows(self, tmp_path):
        content = """Desired Values: TEST
============================================================

EMPTY SECTION (`Init_Nothing`)
------------------------------------------------------------
This section has no pipe rows at all.
Just comments.
"""
        p = tmp_path / "test.v2.txt"
        p.write_text(content)
        result = parse_desired_values(str(p))
        assert len(result.sections) == 1
        assert result.sections[0].fields == []

    def test_freeform_text_between_sections(self, tmp_path):
        content = """Desired Values: TEST
============================================================

FIRST (`Init_A`)
------------------------------------------------------------
Field A | notes | `field_a` | `value_a`

This is freeform text between sections.
It should be completely ignored by the parser.

SECOND (`Init_B`)
------------------------------------------------------------
Field B | notes | `field_b` | `value_b`
"""
        p = tmp_path / "test.v2.txt"
        p.write_text(content)
        result = parse_desired_values(str(p))
        assert len(result.sections) == 2
        assert result.sections[0].fields[0].value == "value_a"
        assert result.sections[1].fields[0].value == "value_b"

    def test_missing_backticks_in_pipe_row(self, tmp_path):
        """Pipe row without backticks is skipped gracefully."""
        content = """Desired Values: TEST
============================================================

DATES (`Init_Date`)
------------------------------------------------------------
Good row | notes | `field_a` | `value_a`
Bad row  | notes | no_backticks | also_none
Another  | notes | `field_b` | `value_b`
"""
        p = tmp_path / "test.v2.txt"
        p.write_text(content)
        result = parse_desired_values(str(p))
        assert len(result.sections[0].fields) == 2
        assert result.sections[0].fields[0].name == "field_a"
        assert result.sections[0].fields[1].name == "field_b"
