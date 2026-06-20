# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for MCP read tools — validates tool registration and request construction."""

import os
import pytest
from unittest.mock import patch
from tests.fakes.fake_clickhouse import FakeQueryResult
from tests.fakes.mock_mcp import MockMCP

class MockCatalogueRef:
    """Mock CatalogueRef that wraps a Catalogue instance."""
    def __init__(self, catalogue):
        self.current = catalogue


@pytest.fixture
def fake_ch(ch_client):
    """Per-test fake seeded with scenario rows. Wraps the canonical
    `ch_client` fixture (tests/conftest.py) with the canned semantic.scenarios
    response read tools expect at registration time."""
    ch_client.set_response(
        "FROM semantic.scenarios",
        FakeQueryResult(
            column_names=[
                "scenario_id", "alias", "name", "base_scenario", "status",
                "description", "created_by", "created_at", "locked_at",
                "horizon_start", "horizon_end", "actuals_cutoff",
                "granularity", "owner_user_id", "updated_at", "variant_of",
                "locks", "kind",
            ],
            result_rows=[
                (
                    "ACTUALS", "actuals", "Actuals", None, "LOCKED",
                    "Actual data", "system", None, None, "", "", None,
                    "monthly", "", None, None, "[]", "ACTUAL",
                ),
                (
                    "BUD-2026", "budget", "Budget 2026", "ACTUALS",
                    "APPROVED", "Budget", "system", None, None, "", "",
                    None, "monthly", "", None, None, "[]", "BUDGET",
                ),
            ],
        ),
    )
    return ch_client


@pytest.fixture
def tools(catalogue, fake_ch):
    """Register tools on a mock MCP and yield the tools dict with
    get_clickhouse_client patched to return the shared `fake_ch` for the
    duration of the test."""
    mock_mcp = MockMCP()
    from precis_mcp.tools.read_tools import register_read_tools
    register_read_tools(mock_mcp, MockCatalogueRef(catalogue))
    with patch('precis_mcp.tools.read_tools.get_clickhouse_client', return_value=fake_ch):
        yield mock_mcp.tools


# ---------------------------------------------------------------------------
# Test: All expected tools are registered
# ---------------------------------------------------------------------------

def test_all_expected_tools_registered(tools):
    expected_tools = {
        "list_scenarios",
        "list_kpis",
        "list_inspection_sources",
        "get_inspection_schema",
        "inspect_rows",
        "run_statement",
        "run_metric",
        "search_hierarchy",
        "resolve_to_cc_list",
        "reload_catalogue",
        "list_variants",
    }
    # list_commits / get_pending_changes / diff_commits are plan-workflow read
    # tools — they are not part of the open read set.
    assert expected_tools == set(tools.keys())


def _set_auth(user_id: str, allowed: list[str] | None, is_admin: bool = False):
    """Install an AuthContext with the given visible scenarios.

    Mirrors what ``load_permissions`` produces in production. The
    ``list_scenarios`` tool reads
    ``permissions.scenarios`` and filters its output accordingly.
    """
    from precis_mcp.auth import (
        ScenarioPermissions, UserPermissions,
    )
    from precis_mcp.auth import (
        AuthContext, set_auth_context,
    )
    scenarios = {}
    for sid in (allowed or []):
        scenarios[sid] = ScenarioPermissions(
            effective_role="analyst",
            tool_scopes={"read": None},
        )
    perms = UserPermissions(
        user_id=user_id,
        is_admin=is_admin, scenarios=scenarios,
    )
    set_auth_context(AuthContext(user_id=user_id, permissions=perms))


def _clear_auth():
    from precis_mcp.auth import clear_auth_context
    clear_auth_context()


def test_list_scenarios_includes_semantic_registry(tools, fake_ch):
    fake_ch.set_response(
        "FROM semantic.scenarios",
        FakeQueryResult(
            column_names=[
                "scenario_id", "alias", "name", "base_scenario", "status",
                "description", "created_by", "created_at", "locked_at",
                "horizon_start", "horizon_end", "actuals_cutoff",
                "granularity", "owner_user_id", "updated_at", "variant_of",
                "locks", "kind",
            ],
            result_rows=[
                (
                    "ACTUALS", "actuals", "Actuals", None, "LOCKED",
                    "Actual data", "system", None, None, "", "", None,
                    "monthly", "", None, None, "[]", "ACTUAL",
                ),
                (
                    "BUD-2026", "budget", "Budget 2026", "ACTUALS",
                    "APPROVED", "Budget", "system", None, None, "", "",
                    None, "monthly", "", None, None, "[]", "BUDGET",
                ),
            ],
        ),
    )

    # Admin context → no filtering, original behaviour.
    _set_auth("root", allowed=None, is_admin=True)
    try:
        result = tools["list_scenarios"]()
    finally:
        _clear_auth()

    assert result["registry"]["real"][0]["scenario"] == "actuals"
    assert {s["scenario"] for s in result["registry"]["shifted"]} >= {
        "actuals_py",
        "budget_pp",
    }
    comparison_keys = {s["scenario"] for s in result["registry"]["comparisons"]}
    assert "actuals_vs_budget" in comparison_keys
    assert "actuals_vs_budget_pct" in comparison_keys
    assert result["registry"]["real"][0]["scenario_id"] == "ACTUALS"
    assert "planning" not in result
    assert "catalogue" not in result


# ---------------------------------------------------------------------------
# list_scenarios — per-user filtering
# ---------------------------------------------------------------------------


class TestListScenariosFilter:
    """``list_scenarios`` must filter ``real``, ``shifted``,
    ``comparisons``, and ``compatibility_aliases`` by the caller's
    profile-derived scope (admins see all).

    Pre-fix, the tool returned the full ``to_reporting_vocabulary``
    payload regardless of who called it.
    """

    def test_non_admin_with_one_scenario_sees_only_that_one(self, tools, fake_ch):
        _set_auth("alice", allowed=["BUD-2026"])
        try:
            result = tools["list_scenarios"]()
        finally:
            _clear_auth()

        real_ids = {s["scenario_id"] for s in result["registry"]["real"]}
        assert real_ids == {"BUD-2026"}

        # Shifted entries follow the underlying real scenario.
        shifted_bases = {s["base_scenario_id"]
                         for s in result["registry"]["shifted"]}
        assert shifted_bases == {"BUD-2026"}

        # Comparisons reference scenario_ids — any cross-scenario
        # comparison involving ACTUALS must be filtered out, leaving
        # only self-comparisons against the visible base.
        for cmp in result["registry"]["comparisons"]:
            for sid in cmp["scenario_ids"]:
                assert sid in {"BUD-2026"}

    def test_non_admin_with_other_scenario_sees_disjoint_set(
        self, tools, fake_ch,
    ):
        _set_auth("bob", allowed=["ACTUALS"])
        try:
            result = tools["list_scenarios"]()
        finally:
            _clear_auth()

        real_ids = {s["scenario_id"] for s in result["registry"]["real"]}
        assert real_ids == {"ACTUALS"}

        # BUD-2026 must not leak via any payload section.
        for cmp in result["registry"]["comparisons"]:
            assert "BUD-2026" not in cmp["scenario_ids"]
        assert all(s["base_scenario_id"] != "BUD-2026"
                   for s in result["registry"]["shifted"])

    def test_admin_sees_all_scenarios(self, tools, fake_ch):
        _set_auth("root", allowed=None, is_admin=True)
        try:
            result = tools["list_scenarios"]()
        finally:
            _clear_auth()

        real_ids = {s["scenario_id"] for s in result["registry"]["real"]}
        assert real_ids == {"BUD-2026", "ACTUALS"}

    def test_user_with_no_matching_profile_sees_empty_real_list(
        self, tools, fake_ch,
    ):
        _set_auth("nobody", allowed=[])
        try:
            result = tools["list_scenarios"]()
        finally:
            _clear_auth()

        assert result["registry"]["real"] == []
        assert result["registry"]["shifted"] == []
        assert result["registry"]["comparisons"] == []


# ---------------------------------------------------------------------------
# Test: list_kpis returns all metrics with domain info
# ---------------------------------------------------------------------------

def test_list_kpis_returns_all_metrics(tools, catalogue):
    result = tools["list_kpis"]()
    assert isinstance(result, list)
    assert len(result) == len(catalogue.metrics)
    for entry in result:
        assert "name" in entry
        assert "label" in entry
        assert "format" in entry
        assert "type" in entry
        assert "domain" in entry
        assert entry["type"] in ("base", "derived")
    names = {e["name"] for e in result}
    assert names == set(catalogue.metrics.keys())


def test_list_kpis_base_metric_has_dimensions(tools):
    result = tools["list_kpis"]()
    # Find a known base metric
    revenue = next(e for e in result if e["name"] == "revenue")
    assert revenue["domain"] == "pnl"
    assert "dimension_keys" in revenue
    assert "cost_centre" in revenue["dimension_keys"]


def test_list_kpis_timesheets_metric_has_project_dimension(tools):
    result = tools["list_kpis"]()
    hours = next(e for e in result if e["name"] == "hours_billable")
    assert hours["domain"] == "timesheets"
    assert "project" in hours["dimension_keys"]


def test_list_kpis_federated_metric_marks_axis_only_dimensions(tools):
    result = tools["list_kpis"]()
    metric = next(e for e in result if e["name"] == "federated_hours_worked")
    assert metric["domain"] == "worklog_federated"
    # Inline axes are breakdown-only — present in axis_only, absent from the
    # unified dimension_keys (which are valid in both filters and dimensions).
    assert "work_date" in metric["axis_only_dimensions"]
    assert "work_date" not in metric["dimension_keys"]
    assert "cost_centre" in metric["dimension_keys"]


def test_list_kpis_derived_metric_resolves_domain(tools):
    result = tools["list_kpis"]()
    gm = next(e for e in result if e["name"] == "gross_margin")
    assert gm["type"] == "derived"
    # gross_margin is derived from revenue (pnl domain)
    assert gm["domain"] == "pnl"
    assert "dimension_keys" in gm


def test_list_inspection_sources_returns_enabled_domains(tools):
    result = tools["list_inspection_sources"]()
    keys = {entry["source_key"] for entry in result}
    assert "gl_federated" in keys
    assert "worklog_federated" in keys


def test_get_inspection_schema_returns_columns(tools):
    result = tools["get_inspection_schema"]("gl_federated")
    assert result["source_key"] == "gl_federated"
    assert "transaction_id" in result["inspect_columns"]
    filters = {entry["key"]: entry for entry in result["filter_dimensions"]}
    assert "cost_centre" in filters
    assert "cost_centre_id" not in filters
    assert filters["cost_centre"]["source_column"] == "cost_centre"
    assert filters["division"]["source_column"] == "cost_centre"
    assert filters["org_structure"]["source_column"] == "cost_centre"


def test_inspect_rows_calls_engine_with_resolved_scenario(tools, fake_ch):
    with patch("precis_mcp.tools.read_tools.engine_inspect_rows") as mock_inspect:
        mock_inspect.return_value = {
            "source_key": "gl",
            "columns": ["period", "amount"],
            "rows": [{"period": "2026-01", "amount": 100}],
            "row_count": 1,
            "limit": 200,
            "truncated": False,
            "query": {"backend_kind": "clickhouse", "sql": "SELECT ..."},
        }
        with patch("precis_mcp.tools.read_tools.execute_platform") as mock_audit:
            tools["inspect_rows"](
                source_key="gl",
                scenario_id="actuals",
                period_start="2026-01",
                period_end="2026-01",
                columns=["period", "amount"],
                out="render",
            )

    mock_inspect.assert_called_once()
    mock_audit.assert_called_once()
    kwargs = mock_inspect.call_args.kwargs
    assert kwargs["scenario_id"] == "ACTUALS"
    assert kwargs["period_start"] == "2026-01"
    assert kwargs["period_end"] == "2026-01"
    assert kwargs["columns"] == ["period", "amount"]
    assert kwargs["ch_client"] is fake_ch


def test_inspect_rows_agent_output_is_capped(tools):
    rows = [{"period": "2026-01", "amount": i} for i in range(150)]
    with patch("precis_mcp.tools.read_tools.engine_inspect_rows") as mock_inspect:
        mock_inspect.return_value = {
            "source_key": "gl",
            "columns": ["period", "amount"],
            "rows": rows,
            "row_count": 150,
            "limit": 200,
            "truncated": False,
            "query": {"backend_kind": "clickhouse", "sql": "SELECT ..."},
        }
        with patch("precis_mcp.tools.read_tools.execute_platform"):
            result = tools["inspect_rows"](
                source_key="gl",
                scenario_id="actuals",
                out="agent",
            )

    assert len(result["rows"]) == 100
    assert result["agent_truncated"] is True


def test_inspect_rows_render_output_is_capped(tools):
    rows = [{"period": "2026-01", "amount": i} for i in range(150)]
    with patch("precis_mcp.tools.read_tools.engine_inspect_rows") as mock_inspect:
        mock_inspect.return_value = {
            "source_key": "gl",
            "columns": ["period", "amount"],
            "rows": rows,
            "row_count": 150,
            "limit": 200,
            "truncated": False,
            "query": {"backend_kind": "clickhouse", "sql": "SELECT ..."},
        }
        with patch("precis_mcp.tools.read_tools.execute_platform"):
            result = tools["inspect_rows"](
                source_key="gl",
                scenario_id="actuals",
                out="render",
            )

    assert len(result["rows"]) == 100
    assert result["agent_truncated"] is True
    assert result["row_count"] == 150


def test_inspect_rows_validation_error_does_not_audit(tools):
    with patch("precis_mcp.tools.read_tools.engine_inspect_rows") as mock_inspect:
        from precis_mcp.engine.inspect import InspectionError

        mock_inspect.side_effect = InspectionError("bad source")
        with patch("precis_mcp.tools.read_tools.execute_platform") as mock_audit:
            result = tools["inspect_rows"](
                source_key="missing",
                scenario_id="actuals",
            )

    assert result["error_type"] == "validation"
    mock_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: run_statement constructs correct request
# ---------------------------------------------------------------------------

def test_run_statement_default_scenarios(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](period_start="2025-01", period_end="2025-12")

    mock_exec.assert_called_once()
    request = mock_exec.call_args[0][0]
    assert request["context"]["period_start"] == "2025-01"
    assert request["context"]["period_end"] == "2025-12"
    assert len(request["blocks"]) == 1
    assert request["blocks"][0]["model"] == "statement:pnl"
    assert request["blocks"][0]["scenario"] == "actuals"
    assert request["_strict_dimensions"] is False


def test_run_statement_custom_scenarios(tools):
    scenarios = [
        {"scenario": "actuals", "alias": "Actuals"},
        {"scenario": "budget", "alias": "Budget"},
    ]
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](
            statement="full_pnl",
            scenarios=scenarios,
            period_start="2026-01",
            period_end="2026-03",
        )

    request = mock_exec.call_args[0][0]
    assert len(request["blocks"]) == 2
    assert request["blocks"][0]["model"] == "statement:full_pnl"
    assert request["blocks"][1]["model"] == "statement:full_pnl"
    assert request["blocks"][0]["alias"] == "Actuals"
    assert request["blocks"][1]["alias"] == "Budget"


def test_run_statement_with_dimensions(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](dimensions=["period"])

    request = mock_exec.call_args[0][0]
    assert request["dimensions"] == ["period"]


def test_run_statement_with_scale(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](scale=3, decimals=1)

    request = mock_exec.call_args[0][0]
    assert request["context"]["scale"] == 3
    assert request["context"]["decimals"] == 1


def test_run_statement_with_filters(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](filters={"department": "Cloud"})

    request = mock_exec.call_args[0][0]
    assert request["filters"] == {"department": "Cloud"}


def test_run_statement_scenario_id_resolution(tools):
    """ACTUALS (scenario_id) should resolve to actuals (registry alias)."""
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_statement"](
            scenarios=[{"scenario": "ACTUALS", "alias": "Actuals"}],
        )

    request = mock_exec.call_args[0][0]
    assert request["blocks"][0]["scenario"] == "actuals"


def test_run_statement_rejects_scenario_entry_without_selector(tools):
    """A scenarios entry missing the 'scenario' field must error, not
    silently default to actuals (which rendered identical columns)."""
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        result = tools["run_statement"](
            scenarios=[
                {"key": "actuals", "alias": "Actuals"},
                {"key": "budget", "alias": "Budget"},
            ],
        )

    mock_exec.assert_not_called()
    assert result["error_type"] == "validation"
    assert '"scenario"' in result["error"]
    assert "actuals_vs_budget" in result["error"]


# ---------------------------------------------------------------------------
# Test: run_metric constructs correct request
# ---------------------------------------------------------------------------

def test_run_metric_basic(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["revenue"],
            period_start="2025-01",
            period_end="2025-12",
        )

    mock_exec.assert_called_once()
    request = mock_exec.call_args[0][0]
    assert len(request["blocks"]) == 1
    # Single-metric calls also route through the 'metrics:' ref — semantically
    # equivalent to 'metric:revenue' but keeps run_metric's block construction
    # uniform across single- and multi-metric requests.
    assert request["blocks"][0]["model"] == "metrics:revenue"
    assert request["blocks"][0]["scenario"] == "actuals"
    assert request["_strict_dimensions"] is True


def test_run_metric_with_dimensions(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["hours_billable"],
            dimensions=["project"],
        )

    request = mock_exec.call_args[0][0]
    assert request["dimensions"] == ["project"]


def test_run_metric_multiple_metrics(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["hours_worked", "hours_billable"],
            dimensions=["cost_centre"],
        )

    request = mock_exec.call_args[0][0]
    # One block per scenario; all metrics carried via 'metrics:' ref. With the
    # default single scenario (actuals) that's one block.
    assert len(request["blocks"]) == 1
    assert request["blocks"][0]["model"] == "metrics:hours_worked,hours_billable"


def test_run_metric_multi_metric_multi_scenario(tools):
    """N scenarios × M metrics produces N blocks, not N×M.

    Regression for the bug where the formatter silently dropped all but one
    metric because run_metric fanned blocks out per (scenario, metric) pair.
    """
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["revenue", "billable_hours"],
            scenarios=[
                {"scenario": "actuals", "alias": "Actuals"},
                {"scenario": "prior_year", "alias": "PY"},
            ],
        )

    request = mock_exec.call_args[0][0]
    assert len(request["blocks"]) == 2
    assert {b["alias"] for b in request["blocks"]} == {"Actuals", "PY"}
    for b in request["blocks"]:
        assert b["model"] == "metrics:revenue,billable_hours"


def test_run_metric_time_series(tools):
    """Replaces get_monthly_trend."""
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["revenue"],
            dimensions=["period"],
            period_start="2023-01",
            period_end="2025-12",
        )

    request = mock_exec.call_args[0][0]
    assert request["dimensions"] == ["period"]
    assert request["context"]["period_start"] == "2023-01"


def test_run_metric_multi_scenario(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        tools["run_metric"](
            metrics=["revenue"],
            scenarios=[
                {"scenario": "actuals", "alias": "Actuals"},
                {"scenario": "budget", "alias": "Budget"},
            ],
            dimensions=["cost_centre"],
        )

    request = mock_exec.call_args[0][0]
    # 1 metric × 2 scenarios = 2 blocks
    assert len(request["blocks"]) == 2


def test_run_metric_rejects_scenario_entry_without_selector(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        result = tools["run_metric"](
            metrics=["revenue"],
            scenarios=[{"key": "budget", "alias": "Budget"}],
        )

    mock_exec.assert_not_called()
    assert result["error_type"] == "validation"
    assert '"scenario"' in result["error"]


# ---------------------------------------------------------------------------
# Test: error handling — _safe_execute wraps exceptions
# ---------------------------------------------------------------------------

def test_safe_execute_catches_resolver_error(tools):
    from precis_mcp.engine.resolver import ResolverError
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.side_effect = ResolverError("test error")
        result = tools["run_statement"]()

    assert "error" in result
    assert result["error_type"] == "validation"
    assert "test error" in result["error"]


def test_safe_execute_catches_generic_exception(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.side_effect = RuntimeError("CH connection failed")
        result = tools["run_metric"](metrics=["revenue"])

    assert "error" in result
    assert result["error_type"] == "execution"
    # Driver exception text (which can embed SQL) must not reach the
    # caller — only the exception class name does.
    assert "CH connection failed" not in result["error"]
    assert "RuntimeError" in result["error"]


def test_safe_execute_catches_filter_error(tools):
    from precis_mcp.engine.filter_resolver import FilterResolutionError
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.side_effect = FilterResolutionError("bad filter")
        result = tools["run_statement"](filters={"bad": "filter"})

    assert "error" in result
    assert result["error_type"] == "validation"


# ---------------------------------------------------------------------------
# Test: caption generation
# ---------------------------------------------------------------------------

def test_run_statement_caption(tools):
    scenarios = [
        {"scenario": "actuals", "alias": "Actuals"},
        {"scenario": "budget", "alias": "Budget"},
    ]
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        result = tools["run_statement"](
            statement="pnl",
            scenarios=scenarios,
            period_start="2026-01",
            period_end="2026-03",
        )

    caption = result["caption"]
    assert caption["description"] == "P&L Statement"
    assert caption["scenarios"] == ["Actuals", "Budget"]
    assert caption["period"] == "2026-01 to 2026-03"
    assert "generated_at" in caption


def test_run_metric_caption(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        result = tools["run_metric"](
            metrics=["revenue", "gross_margin_pct"],
            scenarios=[{"scenario": "actuals", "alias": "Actuals"}],
            period_start="2026-01",
            period_end="2026-12",
        )

    caption = result["caption"]
    assert caption["description"] == "Revenue, Gross Margin %"
    assert caption["scenarios"] == ["Actuals"]
    assert caption["period"] == "2026-01 to 2026-12"


def test_run_statement_caption_with_filters(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        result = tools["run_statement"](
            statement="pnl",
            period_start="2026-01",
            period_end="2026-03",
            filters={"org_structure": "dept:cloud_infra"},
        )

    assert result["caption"]["filters"] == {"org_structure": "dept:cloud_infra"}


def test_run_statement_caption_with_scale_label(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": [], "scale_label": "€ thousands"}
        result = tools["run_statement"](
            statement="pnl",
            period_start="2026-01",
            period_end="2026-03",
        )

    assert result["caption"]["scale"] == "€ thousands"


def test_run_statement_caption_with_top_level_scale(tools):
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": [], "scale": 3}
        result = tools["run_statement"](
            statement="pnl",
            period_start="2026-01",
            period_end="2026-03",
        )

    assert result["caption"]["scale"] == "€ thousands"


def test_caption_no_description_for_unknown_tool(tools):
    """Caption is only attached by run_statement / run_metric, so there's no
    'unknown tool' path — but we can verify that a basic statement caption has
    no extra keys beyond the expected set."""
    with patch('precis_mcp.tools.read_tools.execute_report') as mock_exec:
        mock_exec.return_value = {"rows": []}
        result = tools["run_statement"](
            statement="pnl",
            period_start="2026-01",
            period_end="2026-01",
        )

    caption = result["caption"]
    allowed = {"description", "scenarios", "period", "filters", "scale", "generated_at"}
    assert set(caption.keys()).issubset(allowed)


# ---------------------------------------------------------------------------
# Consumer-facing rounding — the engine keeps full precision; the LLM/render
# return rounds to each figure's display decimals, except when suppressed for
# precision-sensitive internal callers (Extract refresh).
# ---------------------------------------------------------------------------

from precis_mcp.tools.read_tools import (  # noqa: E402
    _round_values_for_consumer,
    _suppress_consumer_rounding,
)


class TestConsumerRounding:
    def test_rounds_values_to_item_decimals(self):
        result = {
            "scenarios": [{"alias": "Actuals"}],
            "rows": [{"item": {"decimals": 1}, "values": {"Actuals": 12.3456}}],
        }
        out = _round_values_for_consumer(result)
        assert out["rows"][0]["values"]["Actuals"] == 12.3

    def test_variance_percent_column_decimals_override_item(self):
        # A variance-% scenario column carries its own decimals (on the column),
        # which take precedence over the per-metric item decimals.
        result = {
            "scenarios": [{"alias": "Var %", "format": "percent", "decimals": 1}],
            "rows": [{"item": {"decimals": 0}, "values": {"Var %": -8.76}}],
        }
        out = _round_values_for_consumer(result)
        assert out["rows"][0]["values"]["Var %"] == -8.8

    def test_contextvar_suppresses_rounding(self):
        result = {
            "scenarios": [{"alias": "Actuals"}],
            "rows": [{"item": {"decimals": 0}, "values": {"Actuals": 1234.567}}],
        }
        token = _suppress_consumer_rounding.set(True)
        try:
            out = _round_values_for_consumer(result)
        finally:
            _suppress_consumer_rounding.reset(token)
        assert out["rows"][0]["values"]["Actuals"] == 1234.567  # full precision

    def test_none_and_non_numeric_values_untouched(self):
        result = {
            "scenarios": [{"alias": "A"}],
            "rows": [{"item": {"decimals": 0}, "values": {"A": None}}],
        }
        out = _round_values_for_consumer(result)
        assert out["rows"][0]["values"]["A"] is None


# ---------------------------------------------------------------------------
# search_hierarchy — least-restrictive cross-scenario member scoping
# ---------------------------------------------------------------------------


def test_search_hierarchy_no_readable_scenarios_returns_nothing(tools, fake_ch):
    """A caller with no readable scenario sees no members at all."""
    _set_auth("u1", allowed=[])
    try:
        result = tools["search_hierarchy"](dimension="cost_centre")
        assert result["records"] == []
        assert result["hierarchy_nodes"] == []
        # No master-data queries were issued.
        assert not [q for q, _ in fake_ch.queries if "dim_" in q or "master" in q]
    finally:
        _clear_auth()


def test_search_hierarchy_applies_union_scope_filter(tools, fake_ch, monkeypatch):
    """When every readable scenario restricts a dimension, the member query
    carries the permitted-id filter (union across scenarios)."""
    import precis_mcp.engine.scope_enforcer as se

    monkeypatch.setattr(
        se, "union_read_member_sets",
        lambda perms, cat, ch: {"cost_centre": {"CC-01", "CC-02"}},
    )
    _set_auth("u1", allowed=["ACTUALS"])
    try:
        tools["search_hierarchy"](dimension="cost_centre")
        scoped = [
            params for _, params in fake_ch.queries
            if params and "scope_ids" in params
        ]
        assert scoped, "member query did not carry the scope filter"
        assert scoped[0]["scope_ids"] == ["CC-01", "CC-02"]
    finally:
        _clear_auth()


def test_search_hierarchy_admin_unfiltered(tools, fake_ch):
    """Admins see everything — no scope predicate on the member query."""
    _set_auth("admin", allowed=[], is_admin=True)
    try:
        tools["search_hierarchy"](dimension="cost_centre")
        assert not [
            params for _, params in fake_ch.queries
            if params and "scope_ids" in params
        ]
    finally:
        _clear_auth()


# ---------------------------------------------------------------------------
# list_variants — visibility filter on returned variants
# ---------------------------------------------------------------------------


def test_list_variants_hides_unreadable_variants(tools, fake_ch):
    fake_ch.set_response(
        "FROM semantic.scenarios",
        FakeQueryResult(
            column_names=["scenario_id", "alias", "name", "kind", "variant_of"],
            result_rows=[
                ("BUD-2026", "budget", "Budget", "BUDGET", None),
                ("BUD-2026-V1", "budget_v1", "Budget V1", "BUDGET", "BUD-2026"),
                ("BUD-2026-V2", "budget_v2", "Budget V2", "BUDGET", "BUD-2026"),
            ],
        ),
    )
    _set_auth("u1", allowed=["BUD-2026", "BUD-2026-V1"])
    try:
        result = tools["list_variants"](scenario_id="BUD-2026")
        ids = {v["scenario_id"] for v in result["variants"]}
        assert ids == {"BUD-2026-V1"}
        assert result["count"] == 1
    finally:
        _clear_auth()
