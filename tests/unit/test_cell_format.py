# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/cell_format.py — the shared favorability + number-format
helpers. Pure logic, no I/O (open-core unit)."""
from __future__ import annotations

from precis_mcp.cell_format import excel_number_format, favorability


class TestFavorability:
    def test_neutral_has_no_signal(self):
        assert favorability(100, "neutral") is None
        assert favorability(-100, "neutral") is None

    def test_zero_or_none_has_no_signal(self):
        assert favorability(0, "natural") is None
        assert favorability(None, "natural") is None
        assert favorability(0, "inverse") is None

    def test_natural_positive_is_favorable(self):
        # revenue up = good
        assert favorability(50, "natural") == "favorable"
        assert favorability(-50, "natural") == "unfavorable"

    def test_inverse_positive_is_unfavorable(self):
        # cost up = bad
        assert favorability(50, "inverse") == "unfavorable"
        assert favorability(-50, "inverse") == "favorable"


class TestExcelNumberFormat:
    def test_percent_is_one_place_regardless_of_decimals(self):
        # Literal "%" suffix, not Excel's percent operator (engine emits 15.3, not 0.153).
        assert excel_number_format("percent") == '0.0"%";–0.0"%"'
        assert excel_number_format("percent", 3) == '0.0"%";–0.0"%"'

    def test_currency_zero_decimals(self):
        assert excel_number_format("currency", 0) == "#,##0;–#,##0"

    def test_currency_two_decimals(self):
        assert excel_number_format("currency", 2) == "#,##0.00;–#,##0.00"

    def test_number_matches_currency(self):
        assert excel_number_format("number", 1) == "#,##0.0;–#,##0.0"

    def test_unknown_format_falls_back(self):
        assert excel_number_format("weird") == "#,##0;–#,##0"

    def test_negative_sign_is_en_dash_not_hyphen(self):
        nf = excel_number_format("currency")
        assert "–" in nf  # U+2013 en-dash
        assert "-" not in nf   # never an ASCII hyphen-minus
