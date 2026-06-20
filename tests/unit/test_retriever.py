# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/engine/retriever.py — SQL generation only (no ClickHouse)."""
from __future__ import annotations

import os

import pytest

from precis_mcp.engine.catalogue import BaseMetric, MetricPredicate, load_catalogue
from precis_mcp.engine.resolver import GrainSpec
from precis_mcp.engine.retriever import (
    GROUPING_COL,
    DataQuery,
    ExecutionPlan,
    _closing_totals_query,
    _full_grouping_expr,
    _group_clause_from_sets,
    _grouping_sets,
    _row_to_dimension_key,
    build_avg_metric_expression,
    build_metric_expression,
    compile_predicates_to_sql,
    generate_sql,
    retrieve,
)
from precis_mcp.engine.types import ROLLED_UP
from precis_mcp.engine.ibis_retriever import rollup_detail_rows

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
FEDERATED_CATALOGUE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "instance", "catalogue",
)

@pytest.fixture(scope="module")
def federated_catalogue():
    return load_catalogue(FEDERATED_CATALOGUE_DIR)


def _make_query(metric_keys: list[str], domain: str = "pnl") -> DataQuery:
    return DataQuery(
        scenario_key="actuals",
        scenario_id="ACTUALS",
        period_start="2025-01",
        period_end="2025-12",
        metric_keys=metric_keys,
        domain=domain,
    )


# ---------------------------------------------------------------------------
# 0. Grains: grouping-set construction, GROUPING SETS SQL, grain decoding
# ---------------------------------------------------------------------------

class TestGroupingSetHelpers:
    def test_grouping_sets_full_ladder(self):
        assert _grouping_sets(["cost_centre", "period"], GrainSpec(True, True, True)) == [
            ["cost_centre", "period"], ["cost_centre"], [],
        ]

    def test_grouping_sets_detail_only(self):
        assert _grouping_sets(["cost_centre"], GrainSpec()) == [["cost_centre"]]

    def test_grouping_sets_detail_plus_grand_total(self):
        assert _grouping_sets(["cost_centre", "period"], GrainSpec(detail=True, grand_total=True)) == [
            ["cost_centre", "period"], [],
        ]

    def test_plain_group_by_for_single_full_set(self):
        assert _group_clause_from_sets(["cost_centre"], [["cost_centre"]]) == ("GROUP BY cost_centre", False)

    def test_grouping_sets_clause_for_multi(self):
        clause, tag = _group_clause_from_sets(
            ["cost_centre", "period"], [["cost_centre", "period"], ["cost_centre"], []],
        )
        assert clause == "GROUP BY GROUPING SETS ((cost_centre, period), (cost_centre), ())"
        assert tag is True

    def test_no_dimensions_no_group_by(self):
        assert _group_clause_from_sets([], [[]]) == ("", False)


class TestFullGroupingExpr:
    """MSB-first bitmask reconstruction; time dims forced rolled up."""

    def test_time_dim_constant_non_time_uses_grouping(self):
        # dims=[cost_centre, quarter]: cost_centre is the MSB (weight 2); quarter the
        # LSB (weight 1) and a time dim, so always rolled up → constant 1.
        assert _full_grouping_expr(["cost_centre", "quarter"], ["quarter"], {"cost_centre"}) == (
            "GROUPING(cost_centre) * 2 + 1"
        )

    def test_all_dims_time_all_constant(self):
        assert _full_grouping_expr(["quarter"], ["quarter"], set()) == "1"


class TestRowToDimensionKey:
    def test_no_grouping_column_all_live(self):
        key = _row_to_dimension_key({"cost_centre": "A", "period": "2025-01"}, ["cost_centre", "period"])
        assert key == ("A", "2025-01")

    def test_grouping_bit_marks_rolled_up(self):
        # mask=1 (MSB-first, n=2) → the LSB (period) is rolled up
        key = _row_to_dimension_key(
            {"cost_centre": "A", "period": "", GROUPING_COL: 1}, ["cost_centre", "period"],
        )
        assert key == ("A", ROLLED_UP)

    def test_grand_total_all_rolled(self):
        key = _row_to_dimension_key(
            {"cost_centre": "", "period": "", GROUPING_COL: 3}, ["cost_centre", "period"],
        )
        assert key == (ROLLED_UP, ROLLED_UP)

    def test_empty_string_value_distinct_from_rolled_up(self):
        key = _row_to_dimension_key(
            {"cost_centre": "", "period": "2025-01", GROUPING_COL: 0}, ["cost_centre", "period"],
        )
        assert key == ("", "2025-01")
        assert key != (ROLLED_UP, "2025-01")


class TestGrainsSql:
    def test_detail_only_is_plain_group_by(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(dq, catalogue, ["cost_centre"], None, GrainSpec())[0]
        assert "GROUP BY t.cost_centre" in sql
        assert "GROUPING SETS" not in sql
        assert GROUPING_COL not in sql

    def test_subtotals_emit_grouping_sets_with_tag(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(
            dq, catalogue, ["cost_centre"], None, GrainSpec(detail=True, grand_total=True),
        )[0]
        assert "GROUP BY GROUPING SETS" in sql
        assert f"GROUPING(t.cost_centre) AS {GROUPING_COL}" in sql


class TestClosingTotalsQuery:
    def test_global_period_end_grouping_sets_and_tag(self):
        m = BaseMetric(
            key="hc", label="HC", source_column="hc",
            aggregation="sum", rollup_method="closing", sign="raw", format="number", fs_group="X",
        )
        dq = _make_query(["hc"])
        sql, _ = _closing_totals_query(
            dq, [m], "semantic.v_pnl", ["cost_centre", "quarter"],
            None, True, ["quarter"], [["cost_centre"], []],
            name_to_expr={"cost_centre": "t.cost_centre", "quarter": "t.quarter"},
            joins=[],
        )
        assert "t.period = {period_end:String}" in sql    # global period_end, not per-group
        assert "'' AS quarter" in sql                      # rolled-up time placeholder
        assert f"GROUPING(t.cost_centre) * 2 + 1 AS {GROUPING_COL}" in sql
        assert "GROUP BY GROUPING SETS ((t.cost_centre), ())" in sql


# ---------------------------------------------------------------------------
# 1. Monthly mode SQL
# ---------------------------------------------------------------------------

class TestPeriodDimension:
    """Period is now a regular dimension — no separate monthly mode."""

    def test_group_by_period(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, ["period"], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "GROUP BY t.period" in sql

    def test_no_rollup_method_in_where(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, ["period"], None)
        sql, _ = results[0]
        assert "rollup_method" not in sql

    def test_case_when_expressions_present(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, ["period"], None)
        sql, _ = results[0]
        assert "CASE WHEN" in sql
        assert "revenue" in sql
        assert "direct_cost" in sql

    def test_revenue_uses_abs(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["period"], None)
        sql, _ = results[0]
        assert "ABS(t.amount)" in sql

    def test_period_in_params(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["period"], None)
        _, params = results[0]
        assert params["period_start"] == "2025-01"
        assert params["period_end"] == "2025-12"

    def test_quarter_dimension(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["quarter"], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "GROUP BY t.quarter" in sql


# ---------------------------------------------------------------------------
# 2. Aggregate mode — sum group
# ---------------------------------------------------------------------------

class TestAggregateSumGroup:
    def test_single_query_for_sum_group(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1

    def test_no_group_by_period(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "GROUP BY t.period" not in sql

    def test_revenue_uses_abs_in_aggregate(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "ABS(t.amount)" in sql

    def test_period_range_in_where(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "period >= {period_start:String}" in sql
        assert "period <= {period_end:String}" in sql


# ---------------------------------------------------------------------------
# 3. Aggregate mode — avg group
# ---------------------------------------------------------------------------

class TestAggregateAvgGroup:
    def test_avg_division_expression(self, catalogue):
        dq = _make_query(["avg_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "/ NULLIF(COUNT(DISTINCT t.period), 0)" in sql

    def test_single_query_for_avg_group(self, catalogue):
        dq = _make_query(["avg_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 4. Aggregate mode — closing group
# ---------------------------------------------------------------------------

class TestAggregateClosingGroup:
    def test_closing_uses_period_end_only(self, catalogue):
        dq = _make_query(["closing_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        # Closing queries use period = {period_end:String}, not a range
        assert "period = {period_end:String}" in sql
        assert "period >= {period_start:String}" not in sql

    def test_single_query_for_closing_group(self, catalogue):
        dq = _make_query(["closing_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1

    def test_closing_with_quarter_dimension_uses_subquery(self, catalogue):
        """When grouping by quarter, closing metrics pick the last period per quarter."""
        dq = _make_query(["closing_fte_billable"])
        results = generate_sql(dq, catalogue, ["quarter"], None)
        sql, _ = results[0]
        # Should NOT use the flat period = period_end filter
        assert "period = {period_end:String}" not in sql
        # Should use a subquery to pick max(period) per quarter
        assert "max(period)" in sql
        assert "GROUP BY quarter)" in sql

    def test_closing_with_period_dimension_returns_all_periods(self, catalogue):
        """When period is a dimension, closing metrics return all periods (no closing filter)."""
        dq = _make_query(["closing_fte_billable"])
        results = generate_sql(dq, catalogue, ["period"], None)
        sql, _ = results[0]
        # Should use the full range, not the closing period = period_end filter
        assert "period >= {period_start:String}" in sql
        assert "period = {period_end:String}" not in sql

    def test_closing_with_cost_centre_uses_period_end(self, catalogue):
        """Non-time dimensions still use the flat period = period_end."""
        dq = _make_query(["closing_fte_billable"])
        results = generate_sql(dq, catalogue, ["cost_centre"], None)
        sql, _ = results[0]
        assert "period = {period_end:String}" in sql


# ---------------------------------------------------------------------------
# 5. Cost centre dimension
# ---------------------------------------------------------------------------

class TestCostCentreDimension:
    def test_cost_centre_in_select(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["cost_centre"], {"cost_centre": ["CC-SE", "CC-NO"]})
        sql, _ = results[0]
        assert "cost_centre" in sql.split("FROM")[0]  # in SELECT part

    def test_cost_centre_in_group_by(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["cost_centre"], {"cost_centre": ["CC-SE", "CC-NO"]})
        sql, _ = results[0]
        assert "GROUP BY t.cost_centre" in sql

    def test_cc_in_filter_when_dimension_filters_provided(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["cost_centre"], {"cost_centre": ["CC-SE", "CC-NO"]})
        sql, params = results[0]
        assert "toString(t.cost_centre) IN" in sql
        assert "{dimf_cost_centre:Array(String)}" in sql
        assert params["dimf_cost_centre"] == ["CC-SE", "CC-NO"]

    def test_cost_centre_with_single_filter(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["cost_centre"], {"cost_centre": ["CC-SE"]})
        sql, _ = results[0]
        assert "cost_centre" in sql.split("FROM")[0]

    def test_empty_filter_list_emits_match_nothing_predicate(self, catalogue):
        """A resolved deny-all scope ({col: []}) must still emit the IN
        predicate — dropping it would invert deny-all into allow-all."""
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["cost_centre"], {"cost_centre": []})
        sql, params = results[0]
        assert "toString(t.cost_centre) IN" in sql
        assert params["dimf_cost_centre"] == []


# ---------------------------------------------------------------------------
# 6. No dimension filters
# ---------------------------------------------------------------------------

class TestNoDimensionFilters:
    def test_no_cost_centre_in_filter(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "toString(t.cost_centre) IN" not in sql

    def test_period_no_filter(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, ["period"], None)
        sql, _ = results[0]
        assert "toString(t.cost_centre) IN" not in sql


# ---------------------------------------------------------------------------
# 6b. Derived breakdown axes — leaf join (no denormalised fact column needed)
# ---------------------------------------------------------------------------

class TestDerivedBreakdownJoin:
    def test_derived_axis_emits_leaf_join_and_alias_back(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(dq, catalogue, ["department"], None)[0]
        assert (
            "LEFT JOIN semantic.dim_cost_centre d0 "
            "ON t.cost_centre = d0.cost_centre"
        ) in sql
        assert "d0.department AS department" in sql
        assert "GROUP BY d0.department" in sql

    def test_two_derived_same_leaf_share_one_join(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(dq, catalogue, ["department", "division"], None)[0]
        assert sql.count("LEFT JOIN") == 1
        assert "GROUP BY d0.department, d0.division" in sql

    def test_derived_plus_leaf_mix_one_join(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(dq, catalogue, ["department", "cost_centre"], None)[0]
        assert sql.count("LEFT JOIN") == 1
        assert "d0.department AS department" in sql
        assert "t.cost_centre AS cost_centre" in sql

    def test_renamed_leaf_column_used_as_join_key(self, catalogue):
        # timesheets binds employee -> employee_id; grade (parent of employee)
        # joins dim_employee on the renamed column.
        dq = _make_query(["hours_worked"], domain="timesheets")
        sql, _ = generate_sql(dq, catalogue, ["grade"], None)[0]
        assert (
            "LEFT JOIN semantic.dim_employee d0 "
            "ON t.employee_id = d0.employee_id"
        ) in sql
        assert "GROUP BY d0.grade" in sql

    def test_time_parent_stays_fact_column_no_join(self, catalogue):
        dq = _make_query(["revenue"])
        sql, _ = generate_sql(dq, catalogue, ["quarter"], None)[0]
        assert "LEFT JOIN" not in sql
        assert "GROUP BY t.quarter" in sql

    def test_derived_axis_leaf_not_bound_raises(self, catalogue):
        # pipeline binds crm_account, not cost_centre; department is not groupable.
        metric = next(
            k for k, m in catalogue.metrics.items()
            if isinstance(m, BaseMetric) and m.domain == "pipeline"
        )
        dq = _make_query([metric], domain="pipeline")
        with pytest.raises(KeyError, match="not groupable"):
            generate_sql(dq, catalogue, ["department"], None)


class _FakeIbisCol:
    """Chainable no-op stand-in for an Ibis column expression."""
    def __eq__(self, other): return self
    def __ge__(self, other): return self
    def __le__(self, other): return self
    def __and__(self, other): return self
    def sum(self): return self
    def name(self, n): return self
    __hash__ = None


class _FakeIbisTable:
    def __init__(self, columns): self.columns = columns
    def __getitem__(self, key): return _FakeIbisCol()
    def filter(self, *a, **k): return self


class _FakeIbisConn:
    def __init__(self, columns): self._table = _FakeIbisTable(columns)
    def table(self, *a, **k): return self._table


class TestFederatedBreakdownGuard:
    def test_missing_foreign_column_raises_clear_error(self, federated_catalogue):
        from precis_mcp.engine.ibis_retriever import (
            IbisRetrieverError,
            build_ibis_query,
        )
        # The foreign view exposes cost_centre but not the derived 'department'.
        conn = _FakeIbisConn(["scenario", "period", "cost_centre", "amount"])
        dq = _make_query(["federated_net_amount"], domain="gl_federated")
        with pytest.raises(IbisRetrieverError, match="not groupable on federated"):
            build_ibis_query(dq, federated_catalogue, ["department"], None, conn)


# ---------------------------------------------------------------------------
# 7. build_metric_expression
# ---------------------------------------------------------------------------

class TestBuildMetricExpression:
    def test_abs_sign(self, catalogue):
        m = catalogue.metrics["revenue"]
        assert isinstance(m, BaseMetric)
        expr = build_metric_expression(m)
        assert "ABS(t.amount)" in expr
        assert "CASE WHEN" in expr

    def test_raw_sign(self, catalogue):
        m = catalogue.metrics["direct_cost"]
        assert isinstance(m, BaseMetric)
        expr = build_metric_expression(m)
        assert "ABS" not in expr
        assert expr.startswith("-") is False or "ELSE 0" in expr
        # Confirm it's a plain SUM CASE WHEN without any transformation
        assert "CASE WHEN" in expr
        assert "ABS" not in expr
        # Ensure no leading negation inside THEN clause
        assert "THEN t.amount" in expr

    def test_negate_sign(self):
        # Create a synthetic metric with sign=negate
        m = BaseMetric(
            key="test_negate",
            label="Test Negate",
            where=[MetricPredicate(column="fs_line", op="eq", value="Test")],
            source_column="amount",
            aggregation="sum",
            rollup_method="sum",
            sign="negate",
            format="currency",
            fs_group="Test",
        )
        expr = build_metric_expression(m)
        assert "THEN -t.amount" in expr
        assert "ABS" not in expr

    def test_build_avg_expression(self, catalogue):
        m = catalogue.metrics["avg_fte_billable"]
        assert isinstance(m, BaseMetric)
        expr = build_avg_metric_expression(m)
        assert "/ NULLIF(COUNT(DISTINCT t.period), 0)" in expr
        assert "CASE WHEN" in expr

    def test_count_distinct_uses_source_column(self):
        # Regression: count_distinct must distinct-count the metric's
        # source_column, not a hardcoded `employee_id` (which broke pipeline
        # count metrics — opportunity_id — with UNKNOWN_IDENTIFIER).
        m = BaseMetric(
            key="won_count",
            label="Won Deals",
            where=[MetricPredicate(column="stage_category", op="eq", value="Won")],
            source_column="opportunity_id",
            aggregation="count_distinct",
            rollup_method="sum",
            sign="raw",
            format="number",
            fs_group="Bookings",
        )
        expr = build_metric_expression(m)
        assert "COUNT(DISTINCT CASE WHEN" in expr
        assert "THEN t.opportunity_id END" in expr
        assert "employee_id" not in expr


class TestCompilePredicatesToSql:
    def test_empty_predicates_compile_to_true(self):
        assert compile_predicates_to_sql([]) == "1=1"

    def test_eq_and_in_predicates(self):
        where = [
            MetricPredicate(column="category", op="eq", value="AAA"),
            MetricPredicate(column="region", op="in", values=["UK", "US"]),
        ]
        assert compile_predicates_to_sql(where) == "t.category = 'AAA' AND t.region IN ('UK', 'US')"

    def test_string_value_is_quoted(self):
        where = [MetricPredicate(column="account", op="eq", value="9100")]
        assert compile_predicates_to_sql(where) == "t.account = '9100'"

    def test_unsafe_column_rejected(self):
        where = [MetricPredicate(column="a; DROP TABLE", op="eq", value="x")]
        with pytest.raises(ValueError, match="Invalid predicate column name"):
            compile_predicates_to_sql(where)


# ---------------------------------------------------------------------------
# 8. Multiple rollup groups in aggregate mode
# ---------------------------------------------------------------------------

class TestMultipleRollupGroups:
    def test_three_queries_for_all_groups(self, catalogue):
        # revenue=sum, avg_fte_billable=avg, closing_fte_billable=closing
        dq = _make_query(["revenue", "avg_fte_billable", "closing_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 3

    def test_three_queries_have_different_expressions(self, catalogue):
        dq = _make_query(["revenue", "avg_fte_billable", "closing_fte_billable"])
        results = generate_sql(dq, catalogue, [], None)
        # Each query should have different metric expressions
        sqls = [sql for sql, _ in results]
        assert any("revenue" in s for s in sqls)
        assert any("avg_fte_billable" in s for s in sqls)
        assert any("closing_fte_billable" in s for s in sqls)


# ---------------------------------------------------------------------------
# 9. Only sum metrics — single query returned
# ---------------------------------------------------------------------------

class TestOnlySumMetrics:
    def test_single_query(self, catalogue):
        dq = _make_query(["revenue", "direct_cost", "indirect_cost"])
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1

    def test_only_sum_metrics_in_query(self, catalogue):
        dq = _make_query(["revenue", "direct_cost"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "revenue" in sql
        assert "direct_cost" in sql


# ---------------------------------------------------------------------------
# 10. Period parameters
# ---------------------------------------------------------------------------

class TestPeriodParameters:
    def test_params_contain_correct_periods_monthly(self, catalogue):
        dq = DataQuery(
            scenario_key="budget",
            scenario_id="BUDGET",
            period_start="2026-01",
            period_end="2026-06",
            metric_keys=["revenue"],
        )
        results = generate_sql(dq, catalogue, [], None)
        _, params = results[0]
        assert params["period_start"] == "2026-01"
        assert params["period_end"] == "2026-06"

    def test_params_contain_correct_periods_aggregate(self, catalogue):
        dq = DataQuery(
            scenario_key="budget",
            scenario_id="BUDGET",
            period_start="2026-01",
            period_end="2026-06",
            metric_keys=["revenue"],
        )
        results = generate_sql(dq, catalogue, [], None)
        _, params = results[0]
        assert params["period_start"] == "2026-01"
        assert params["period_end"] == "2026-06"

    def test_scenario_id_in_params(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, [], None)
        _, params = results[0]
        assert params["scenario_id"] == "ACTUALS"

    def test_sql_references_scenario_placeholder(self, catalogue):
        dq = _make_query(["revenue"])
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "{scenario_id:String}" in sql


# ---------------------------------------------------------------------------
# 11. GL domain routing
# ---------------------------------------------------------------------------

class TestGLDomain:
    def test_gl_domain_uses_v_gl_view(self, catalogue):
        dq = _make_query(["gl_amount"], domain="gl")
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "semantic.v_gl" in sql

    def test_gl_domain_not_v_pnl(self, catalogue):
        dq = _make_query(["gl_amount"], domain="gl")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "v_pnl" not in sql

    def test_gl_amount_metric_expression(self, catalogue):
        dq = _make_query(["gl_amount"], domain="gl")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "gl_amount" in sql
        assert "CASE WHEN" in sql

    def test_gl_with_account_dimension_filter(self, catalogue):
        dq = _make_query(["gl_amount"], domain="gl")
        filters = {"account": ["4100", "4200"]}
        results = generate_sql(dq, catalogue, ["account"], filters)
        sql, _ = results[0]
        assert "toString(t.account) IN" in sql
        assert "GROUP BY t.account" in sql

    def test_pnl_domain_still_uses_v_pnl(self, catalogue):
        dq = _make_query(["revenue"], domain="pnl")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "semantic.v_pnl" in sql


# ---------------------------------------------------------------------------
# 12. Timesheets domain
# ---------------------------------------------------------------------------

class TestTimesheetsDomain:
    def test_timesheets_domain_uses_v_timesheets(self, catalogue):
        dq = _make_query(["hours_worked", "hours_billable"], domain="timesheets")
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "semantic.v_timesheets" in sql

    def test_timesheets_with_employee_dimension(self, catalogue):
        # timesheets binds key 'employee' to the physical column 'employee_id';
        # the engine groups by the column and aliases it back to the key.
        dq = _make_query(["hours_worked"], domain="timesheets")
        results = generate_sql(dq, catalogue, ["employee"], None)
        sql, _ = results[0]
        assert "employee_id AS employee" in sql
        assert "GROUP BY t.employee_id" in sql

    def test_timesheets_with_project_dimension(self, catalogue):
        dq = _make_query(["hours_billable"], domain="timesheets")
        results = generate_sql(dq, catalogue, ["project"], None)
        sql, _ = results[0]
        assert "project_id AS project" in sql
        assert "GROUP BY t.project_id" in sql

    def test_timesheets_by_employee_and_period(self, catalogue):
        dq = _make_query(["hours_worked", "hours_billable"], domain="timesheets")
        results = generate_sql(dq, catalogue, ["period", "employee"], None)
        sql, _ = results[0]
        assert "period" in sql
        assert "employee" in sql
        assert "GROUP BY" in sql

    def test_timesheets_with_employee_filter(self, catalogue):
        # Filtering by the 'employee' key resolves to the 'employee_id' column.
        dq = _make_query(["hours_worked"], domain="timesheets")
        filters = {"employee_id": ["42", "43"]}
        results = generate_sql(dq, catalogue, [], filters)
        sql, _ = results[0]
        assert "toString(t.employee_id) IN" in sql


# ---------------------------------------------------------------------------
# 13. Payroll domain
# ---------------------------------------------------------------------------

class TestPayrollDomain:
    def test_payroll_domain_uses_v_payroll(self, catalogue):
        dq = _make_query(["headcount"], domain="payroll")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "semantic.v_payroll" in sql

    def test_headcount_uses_avg_rollup(self, catalogue):
        """Headcount has rollup_method=avg, so aggregate mode should produce an avg query."""
        dq = _make_query(["headcount"], domain="payroll")
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "NULLIF(COUNT(DISTINCT t.period), 0)" in sql

    def test_payroll_cost_metrics(self, catalogue):
        dq = _make_query(["gross_salary", "total_payroll_cost"], domain="payroll")
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 1
        sql, _ = results[0]
        assert "gross_salary" in sql
        assert "total_payroll_cost" in sql

    def test_payroll_mixed_rollup(self, catalogue):
        """headcount (avg) + salary (sum) should produce 2 queries in aggregate."""
        dq = _make_query(["headcount", "gross_salary"], domain="payroll")
        results = generate_sql(dq, catalogue, [], None)
        assert len(results) == 2

    def test_payroll_with_cost_centre_dimension(self, catalogue):
        dq = _make_query(["total_payroll_cost"], domain="payroll")
        results = generate_sql(dq, catalogue, ["cost_centre"], None)
        sql, _ = results[0]
        assert "GROUP BY t.cost_centre" in sql


# ---------------------------------------------------------------------------
# 7. Versioned flag — unversioned domains omit commit_id from WHERE
# ---------------------------------------------------------------------------

class TestVersionedFlag:
    """Domains with versioned=false must not reference commit_id in SQL."""

    def test_unversioned_domain_no_commit_id(self, catalogue):
        """timesheets domain (versioned: false) should have no commit_id in SQL."""
        dq = _make_query(["billable_hours"], domain="timesheets")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "commit_id" not in sql

    def test_versioned_domain_has_commit_id(self, catalogue):
        """pnl domain (versioned: true, explicit in pnl.yml) should have commit_id in SQL."""
        dq = _make_query(["revenue"], domain="pnl")
        results = generate_sql(dq, catalogue, [], None)
        sql, _ = results[0]
        assert "commit_id" in sql

    def test_unversioned_aggregate_no_commit_id(self, catalogue):
        """Aggregate mode on unversioned domain should also omit commit_id."""
        dq = _make_query(["total_payroll_cost"], domain="payroll")
        results = generate_sql(dq, catalogue, [], None)
        for sql, _ in results:
            assert "commit_id" not in sql


# ---------------------------------------------------------------------------
# 14. Federated Ibis dispatch
# ---------------------------------------------------------------------------

class TestFederatedIbisDispatch:
    def test_retrieve_uses_ibis_backend_for_federated_domain(
        self, federated_catalogue, monkeypatch,
    ):
        calls = []

        def fake_execute_ibis_queries(dq, catalogue, dimensions, dimension_filters, conn):
            calls.append((dq, dimensions, dimension_filters, conn))
            return [[{
                "cost_centre": "CC-SE",
                "federated_revenue": 1000,
                "federated_direct_cost": 400,
            }]]

        monkeypatch.setattr(
            "precis_mcp.engine.ibis_retriever.execute_ibis_queries",
            fake_execute_ibis_queries,
        )

        dq = _make_query(
            ["federated_revenue", "federated_direct_cost"],
            domain="gl_federated",
        )
        plan = ExecutionPlan(data_queries=[dq], dimensions=["cost_centre"])
        raw = retrieve(
            plan,
            federated_catalogue,
            ch_client=None,
            dimension_filters={"cost_centre": ["CC-SE"]},
            ibis_backends={"customer_pg": object()},
        )

        assert calls
        assert calls[0][1] == ["cost_centre"]
        assert calls[0][2] == {"cost_centre": ["CC-SE"]}
        assert raw["actuals"][("CC-SE",)]["federated_revenue"] == 1000.0
        assert raw["actuals"][("CC-SE",)]["federated_direct_cost"] == 400.0

    def test_ibis_empty_filter_list_emits_match_nothing_predicate(self):
        """A resolved deny-all scope ({col: []}) must produce an isin([])
        expression (matches nothing) — dropping it would invert deny-all
        into allow-all."""
        import ibis

        from precis_mcp.engine.ibis_retriever import _base_filter_exprs

        table = ibis.table(
            {"scenario": "string", "period": "string", "cost_centre": "string"},
            name="t",
        )
        dq = _make_query(["federated_revenue"], domain="gl_federated")
        with_filter = _base_filter_exprs(table, dq, {"cost_centre": []})
        without_filter = _base_filter_exprs(table, dq, None)
        assert len(with_filter) == len(without_filter) + 1

    def test_ibis_detail_rows_roll_up_additively(self):
        result_sets = rollup_detail_rows(
            [
                {
                    "cost_centre": "CC-SE",
                    "period": "2025-01",
                    "federated_revenue": 1000,
                    "federated_direct_cost": 400,
                },
                {
                    "cost_centre": "CC-SE",
                    "period": "2025-02",
                    "federated_revenue": 2000,
                    "federated_direct_cost": 900,
                },
                {
                    "cost_centre": "CC-DA",
                    "period": "2025-01",
                    "federated_revenue": 500,
                    "federated_direct_cost": 200,
                },
            ],
            ["cost_centre", "period"],
            ["federated_revenue", "federated_direct_cost"],
            GrainSpec(detail=True, subtotals=True, grand_total=True),
        )

        rows = result_sets[0]
        by_key = {
            _row_to_dimension_key(row, ["cost_centre", "period"]): row
            for row in rows
        }
        assert by_key[("CC-SE", "2025-01")]["federated_revenue"] == 1000.0
        assert by_key[("CC-SE", ROLLED_UP)]["federated_revenue"] == 3000.0
        assert by_key[(ROLLED_UP, ROLLED_UP)]["federated_revenue"] == 3500.0
        assert by_key[("CC-SE", ROLLED_UP)]["federated_direct_cost"] == 1300.0
        assert by_key[(ROLLED_UP, ROLLED_UP)]["federated_direct_cost"] == 1500.0

    def test_retrieve_rolls_up_federated_domain_with_requested_grains(
        self, federated_catalogue, monkeypatch,
    ):
        def fake_execute_ibis_queries(dq, catalogue, dimensions, dimension_filters, conn):
            return [[
                {
                    "cost_centre": "CC-SE",
                    "period": "2025-01",
                    "federated_revenue": 1000,
                    "federated_direct_cost": 400,
                },
                {
                    "cost_centre": "CC-SE",
                    "period": "2025-02",
                    "federated_revenue": 2000,
                    "federated_direct_cost": 900,
                },
                {
                    "cost_centre": "CC-DA",
                    "period": "2025-01",
                    "federated_revenue": 500,
                    "federated_direct_cost": 200,
                },
            ]]

        monkeypatch.setattr(
            "precis_mcp.engine.ibis_retriever.execute_ibis_queries",
            fake_execute_ibis_queries,
        )

        dq = _make_query(
            ["federated_revenue", "federated_direct_cost"],
            domain="gl_federated",
        )
        plan = ExecutionPlan(
            data_queries=[dq],
            dimensions=["cost_centre", "period"],
            grains=GrainSpec(detail=True, subtotals=True, grand_total=True),
        )
        raw = retrieve(
            plan,
            federated_catalogue,
            ch_client=None,
            ibis_backends={"customer_pg": object()},
        )

        assert raw["actuals"][("CC-SE", ROLLED_UP)]["federated_revenue"] == 3000.0
        assert raw["actuals"][(ROLLED_UP, ROLLED_UP)]["federated_revenue"] == 3500.0
        assert raw["actuals"][(ROLLED_UP, ROLLED_UP)]["federated_direct_cost"] == 1500.0

    def test_retrieve_requires_configured_ibis_backend(self, federated_catalogue):
        dq = _make_query(["federated_revenue"], domain="gl_federated")
        plan = ExecutionPlan(data_queries=[dq], dimensions=[])

        with pytest.raises(RuntimeError, match="requires Ibis backend"):
            retrieve(plan, federated_catalogue, ch_client=None)
