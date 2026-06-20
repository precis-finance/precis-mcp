# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/filter_resolver.py"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from precis_mcp.engine.filter_resolver import (
    FilterResolutionError,
    _find_filter_target,
    _map_to_view_column,
    resolve_filters,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _mock_ch_client(rows: list[tuple], column_names: list[str] | None = None):
    """Create a mock ClickHouse client that returns the given rows."""
    client = MagicMock()
    result = MagicMock()
    result.result_rows = rows
    result.column_names = column_names or ["id"]
    client.query.return_value = result
    return client


# ---------------------------------------------------------------------------
# _find_filter_target
# ---------------------------------------------------------------------------

class TestFindFilterTarget:
    def test_ragged_dimension_key(self, catalogue):
        target = _find_filter_target("org_structure", catalogue)
        assert target.resolution_type == "ragged"
        assert target.dimension.key == "org_structure"
        assert target.dimension.is_ragged
        assert target.dimension.leaf_dimension == "cost_centre"

    def test_derived_dimension_division(self, catalogue):
        target = _find_filter_target("division", catalogue)
        assert target.resolution_type == "derived"
        assert target.dimension.key == "division"
        assert target.dimension.is_derived

    def test_derived_dimension_department(self, catalogue):
        target = _find_filter_target("department", catalogue)
        assert target.resolution_type == "derived"
        assert target.dimension.key == "department"
        assert target.dimension.is_derived

    def test_leaf_dimension_cost_centre(self, catalogue):
        target = _find_filter_target("cost_centre", catalogue)
        assert target.resolution_type == "leaf"
        assert target.dimension.key == "cost_centre"
        assert target.dimension.is_leaf

    def test_leaf_column_id_not_matched(self, catalogue):
        """cost_centre_id (the DB column) is not a valid filter key."""
        with pytest.raises(FilterResolutionError, match="Unknown filter key"):
            _find_filter_target("cost_centre_id", catalogue)

    def test_unknown_key_raises(self, catalogue):
        with pytest.raises(FilterResolutionError, match="Unknown filter key"):
            _find_filter_target("nonexistent", catalogue)


# ---------------------------------------------------------------------------
# _map_to_view_column
# ---------------------------------------------------------------------------

class TestMapToViewColumn:
    def test_pnl_domain_maps_to_cost_centre(self, catalogue):
        dim = catalogue.dimensions["cost_centre"]
        col = _map_to_view_column(dim, catalogue, "pnl")
        # CubeDimension in pnl.yml has key="cost_centre"
        assert col == "cost_centre"

    def test_timesheets_employee_maps_to_employee_id_column(self, catalogue):
        # timesheets binds key 'employee' to the physical column 'employee_id'.
        dim = catalogue.dimensions["employee"]
        col = _map_to_view_column(dim, catalogue, "timesheets")
        assert col == "employee_id"

    def test_unknown_domain_falls_back_to_leaf_column(self, catalogue):
        dim = catalogue.dimensions["cost_centre"]
        col = _map_to_view_column(dim, catalogue, "nonexistent_domain")
        assert col == "cost_centre"


# ---------------------------------------------------------------------------
# resolve_filters — rollup hierarchy
# ---------------------------------------------------------------------------

class TestResolveFiltersRollup:
    def test_single_rollup_filter(self, catalogue):
        ch = _mock_ch_client([("CC-01",), ("CC-02",)])
        result = resolve_filters(
            {"org_structure": "dept:Cloud & Infrastructure"},
            catalogue, ch,
        )
        assert "cost_centre" in result
        assert sorted(result["cost_centre"]) == ["CC-01", "CC-02"]

        # Verify the SQL query used the rollup view
        sql_arg = ch.query.call_args[0][0]
        assert "dim_cost_centre_org_structure_rollup" in sql_arg
        assert "node_id" in sql_arg

    def test_rollup_filter_passes_node_id_as_param(self, catalogue):
        ch = _mock_ch_client([("CC-01",)])
        resolve_filters(
            {"org_structure": "div:Advisory"},
            catalogue, ch,
        )
        params = ch.query.call_args[1].get("parameters", {})
        assert params.get("node_id") == "div:Advisory"


# ---------------------------------------------------------------------------
# resolve_filters — attribute column
# ---------------------------------------------------------------------------

class TestResolveFiltersAttribute:
    def test_single_attribute_filter(self, catalogue):
        ch = _mock_ch_client([("CC-SENG-01",), ("CC-DATA-01",)])
        result = resolve_filters(
            {"division": "Technology Services"},
            catalogue, ch,
        )
        assert "cost_centre" in result
        assert sorted(result["cost_centre"]) == ["CC-DATA-01", "CC-SENG-01"]

        # Verify the SQL query used the source table
        sql_arg = ch.query.call_args[0][0]
        assert "semantic.dim_cost_centre" in sql_arg
        assert "division" in sql_arg

    def test_derived_filter_passes_value_as_param(self, catalogue):
        ch = _mock_ch_client([("CC-01",)])
        resolve_filters(
            {"department": "Cloud & Infrastructure"},
            catalogue, ch,
        )
        params = ch.query.call_args[1].get("parameters", {})
        assert params.get("val") == "Cloud & Infrastructure"


# ---------------------------------------------------------------------------
# resolve_filters — intersection
# ---------------------------------------------------------------------------

class TestResolveFiltersIntersection:
    def test_two_filters_same_dimension_intersected(self, catalogue):
        """Two filters on the same dimension → leaf sets are intersected."""
        ch = MagicMock()
        # First call (org_structure) returns 3 CCs
        result1 = MagicMock()
        result1.result_rows = [("CC-01",), ("CC-02",), ("CC-03",)]
        # Second call (division) returns 2 CCs, overlapping
        result2 = MagicMock()
        result2.result_rows = [("CC-02",), ("CC-03",), ("CC-04",)]
        ch.query.side_effect = [result1, result2]

        result = resolve_filters(
            {"org_structure": "dept:Cloud", "division": "Technology"},
            catalogue, ch,
        )
        assert "cost_centre" in result
        # Intersection: CC-02, CC-03
        assert sorted(result["cost_centre"]) == ["CC-02", "CC-03"]

    def test_intersection_can_be_empty(self, catalogue):
        """If intersection is empty, return empty list."""
        ch = MagicMock()
        result1 = MagicMock()
        result1.result_rows = [("CC-01",)]
        result2 = MagicMock()
        result2.result_rows = [("CC-99",)]
        ch.query.side_effect = [result1, result2]

        result = resolve_filters(
            {"org_structure": "dept:A", "division": "B"},
            catalogue, ch,
        )
        assert result["cost_centre"] == []


# ---------------------------------------------------------------------------
# resolve_filters — edge cases
# ---------------------------------------------------------------------------

class TestResolveFiltersEdgeCases:
    def test_empty_filters_returns_empty(self, catalogue):
        result = resolve_filters({}, catalogue, MagicMock())
        assert result == {}

    def test_unknown_key_raises(self, catalogue):
        with pytest.raises(FilterResolutionError, match="Unknown filter key"):
            resolve_filters(
                {"nonexistent": "value"},
                catalogue, MagicMock(),
            )

    def test_domain_mapping_affects_column_key(self, catalogue):
        """Result keys use the CubeDimension.key from the specified domain."""
        ch = _mock_ch_client([("CC-01",)])
        result = resolve_filters(
            {"org_structure": "dept:Cloud"},
            catalogue, ch, domain="pnl",
        )
        # pnl domain: CubeDimension key is "cost_centre"
        assert "cost_centre" in result

    def test_fallback_domain_uses_leaf_column(self, catalogue):
        """Unknown domain falls back to leaf_column as the result key."""
        ch = _mock_ch_client([("CC-01",)])
        result = resolve_filters(
            {"org_structure": "dept:Cloud"},
            catalogue, ch, domain="unknown",
        )
        # Falls back to dim.leaf_column = "cost_centre"
        assert "cost_centre" in result


# ---------------------------------------------------------------------------
# Period dimension hierarchy filters
# ---------------------------------------------------------------------------

class TestResolveFiltersPeriodHierarchy:
    def test_calendar_rollup_filter(self, catalogue):
        """Period calendar hierarchy filter resolves via rollup view."""
        ch = _mock_ch_client([("2025-01",), ("2025-02",), ("2025-03",)])
        result = resolve_filters(
            {"calendar": "q:2025-Q1"},
            catalogue, ch, domain="pnl",
        )
        assert "period" in result
        assert sorted(result["period"]) == ["2025-01", "2025-02", "2025-03"]

        sql_arg = ch.query.call_args[0][0]
        assert "dim_period_calendar_rollup" in sql_arg
        assert "node_id" in sql_arg

    def test_fiscal_year_attribute_filter(self, catalogue):
        """fiscal_year attribute filter resolves to period leaf values."""
        ch = _mock_ch_client([
            ("2025-01",), ("2025-02",), ("2025-03",),
            ("2025-04",), ("2025-05",), ("2025-06",),
            ("2025-07",), ("2025-08",), ("2025-09",),
            ("2025-10",), ("2025-11",), ("2025-12",),
        ])
        result = resolve_filters(
            {"fiscal_year": "2025"},
            catalogue, ch, domain="pnl",
        )
        assert "period" in result
        assert len(result["period"]) == 12

        sql_arg = ch.query.call_args[0][0]
        assert "dim_period" in sql_arg
        assert "fiscal_year" in sql_arg

    def test_quarter_attribute_filter(self, catalogue):
        """quarter attribute filter resolves to period leaf values."""
        ch = _mock_ch_client([("2025-04",), ("2025-05",), ("2025-06",)])
        result = resolve_filters(
            {"quarter": "2025-Q2"},
            catalogue, ch, domain="pnl",
        )
        assert "period" in result
        assert sorted(result["period"]) == ["2025-04", "2025-05", "2025-06"]