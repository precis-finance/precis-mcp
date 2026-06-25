# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for precis_mcp/table_builder.py.

Focus: the metrics-as-columns layout (>1 metric). When a metric breakdown has
more than one metric, metrics move across the columns (metric-outer,
scenario-inner) with a grouped two-level header when there is also >1 scenario,
per-metric display attributes on the columns, and dimension-only rows. The
single-metric layout (scenarios as columns) is unchanged.

Pure block-shaping logic — no I/O, so this is a unit test.
"""
from __future__ import annotations

from precis_mcp.table_builder import build_financial_table_block


def _item(key, label, fmt="currency", decimals=0, ve="natural"):
    return {
        "key": key, "label": label, "style": "default", "indent": 0,
        "format": fmt, "decimals": decimals, "variance_effect": ve,
    }


def _row(grain, dims, item, values):
    return {"grain": grain, "dimensions": dims, "item": item, "values": values}


def _result(scenarios, dimensions, rows, kind="metric"):
    return {
        "kind": kind, "dimensions": dimensions, "scenarios": scenarios,
        "scale": 0, "rows": rows,
    }


def _block(data) -> dict:
    block = build_financial_table_block(data)
    assert block is not None
    return block


# ---------------------------------------------------------------------------
# >1 metric × >1 scenario × ≥2 dimensions — grouped metric columns
# ---------------------------------------------------------------------------

class TestMetricColumns:
    def _data(self):
        scen = [{"alias": "Actuals"}, {"alias": "Budget"}]
        rows = [
            _row("detail", {"cost_centre": "CC1", "project": "P1"},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 120.0, "Budget": 110.0}),
            _row("detail", {"cost_centre": "CC1", "project": "P1"},
                 _item("mgn", "Margin %", "percent", 1), {"Actuals": 18.2, "Budget": 17.0}),
            _row("detail", {"cost_centre": "CC1", "project": "P2"},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 95.0, "Budget": 90.0}),
            _row("detail", {"cost_centre": "CC1", "project": "P2"},
                 _item("mgn", "Margin %", "percent", 1), {"Actuals": 21.4, "Budget": 20.1}),
            _row("subtotal", {"cost_centre": "CC1"},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 215.0, "Budget": 200.0}),
            _row("subtotal", {"cost_centre": "CC1"},
                 _item("mgn", "Margin %", "percent", 1), {"Actuals": 19.8, "Budget": 18.5}),
            _row("grand_total", {},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 215.0, "Budget": 200.0}),
            _row("grand_total", {},
                 _item("mgn", "Margin %", "percent", 1), {"Actuals": 19.8, "Budget": 18.5}),
        ]
        return _result(scen, ["cost_centre", "project"], rows)

    def test_columns_metric_outer_scenario_inner(self):
        cols = _block(self._data())["columns"]
        assert cols[0]["key"] == "label"
        assert [c["key"] for c in cols[1:]] == [
            "rev|Actuals", "rev|Budget", "mgn|Actuals", "mgn|Budget",
        ]

    def test_group_spans_metric_leaf_is_scenario(self):
        cols = _block(self._data())["columns"][1:]
        assert [c["group"] for c in cols] == ["Revenue", "Revenue", "Margin %", "Margin %"]
        assert [c["label"] for c in cols] == ["Actuals", "Budget", "Actuals", "Budget"]

    def test_per_metric_format_and_decimals_live_on_columns(self):
        by = {c["key"]: c for c in _block(self._data())["columns"][1:]}
        assert by["rev|Actuals"]["format"] == "currency"
        assert by["rev|Actuals"]["decimals"] == 0
        assert by["mgn|Actuals"]["format"] == "percent"
        assert by["mgn|Actuals"]["decimals"] == 1

    def test_subtotal_and_total_span_all_metrics(self):
        # Regression: the old label-appending layout collapsed multi-metric
        # subtotals to a single metric. Merged rows must carry every column.
        block = _block(self._data())
        sub = next(r for r in block["rows"] if r["row_type"] == "subtotal")
        assert set(sub["values"]) == {"rev|Actuals", "rev|Budget", "mgn|Actuals", "mgn|Budget"}
        assert sub["values"]["rev|Actuals"] == 215.0
        assert sub["values"]["mgn|Actuals"] == 19.8
        total = next(r for r in block["rows"] if r["row_type"] == "total")
        assert set(total["values"]) == {"rev|Actuals", "rev|Budget", "mgn|Actuals", "mgn|Budget"}

    def test_rows_are_dimension_combos_not_per_metric(self):
        rows = _block(self._data())["rows"]
        labelled = [(r["row_type"], r.get("label")) for r in rows]
        assert ("group_header", "CC1") in labelled
        assert ("line_item", "P1") in labelled
        assert ("line_item", "P2") in labelled
        # The metric is a column, so it is never appended to a detail row label.
        details = [r for r in rows if r["row_type"] == "line_item"]
        assert all("—" not in (r["label"] or "") for r in details)


# ---------------------------------------------------------------------------
# >1 metric × 1 scenario — metrics are ungrouped columns
# ---------------------------------------------------------------------------

class TestMetricColumnsSingleScenario:
    def test_single_scenario_metrics_are_ungrouped_columns(self):
        scen = [{"alias": "Actuals"}]
        rows = [
            _row("detail", {"project": "P1"},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 100.0}),
            _row("detail", {"project": "P1"},
                 _item("hc", "Headcount", "number", 0), {"Actuals": 5.0}),
        ]
        cols = _block(_result(scen, ["project"], rows))["columns"]
        assert [c["label"] for c in cols] == ["Project", "Revenue", "Headcount"]
        assert all("group" not in c for c in cols[1:])


# ---------------------------------------------------------------------------
# 1 metric — unchanged: scenarios stay as the columns
# ---------------------------------------------------------------------------

class TestSingleMetricUnchanged:
    def test_single_metric_keeps_scenario_columns_and_row_format(self):
        scen = [{"alias": "Actuals"}, {"alias": "Budget"}]
        rows = [
            _row("detail", {"project": "P1"},
                 _item("rev", "Revenue", "currency", 0), {"Actuals": 100.0, "Budget": 110.0}),
        ]
        block = _block(_result(scen, ["project"], rows))
        cols = block["columns"]
        assert [c["key"] for c in cols] == ["label", "Actuals", "Budget"]
        assert all("group" not in c for c in cols)
        # single-metric layout keeps format / decimals on the row
        row = block["rows"][0]
        assert row["format"] == "currency"
        assert row["decimals"] == 0


# ---------------------------------------------------------------------------
# for_excel=True — resolved `nf` per value column + sparse per-row `alerts`
# (the add-in enrichment, docs/precis-excel-addin-spec.md §5)
# ---------------------------------------------------------------------------

class TestForExcelEnrichment:
    def _data(self):
        # aggregate (no dimensions): Actuals + a Variance column.
        scen = [{"alias": "Actuals"}, {"alias": "Variance", "variance": True}]
        rows = [
            _row("detail", {},
                 _item("rev", "Revenue", "currency", 0, ve="natural"),
                 {"Actuals": 100.0, "Variance": 10.0}),
            _row("detail", {},
                 _item("cost", "Direct Cost", "currency", 0, ve="inverse"),
                 {"Actuals": 60.0, "Variance": 5.0}),
            _row("detail", {},
                 _item("mgn", "Margin %", "percent", 1, ve="natural"),
                 {"Actuals": 40.0, "Variance": -2.0}),
        ]
        return _result(scen, [], rows)

    def test_off_by_default(self):
        block = _block(self._data())
        assert all("nf" not in c for c in block["columns"])
        assert all("alerts" not in r for r in block["rows"])

    def test_nf_on_value_columns_only(self):
        block = build_financial_table_block(self._data(), for_excel=True)
        assert "nf" not in block["columns"][0]  # the label column
        for c in block["columns"][1:]:
            assert c["nf"]  # resolved Excel number-format string

    def test_percent_column_gets_percent_format(self):
        # The Margin % row's metric format is percent → its cells are percent,
        # but in the aggregate layout format lives on the row, so assert the row.
        block = build_financial_table_block(self._data(), for_excel=True)
        margin = next(r for r in block["rows"] if r.get("label") == "Margin %")
        assert margin["format"] == "percent"

    def test_alerts_resolved_per_cell(self):
        block = build_financial_table_block(self._data(), for_excel=True)
        by_label = {r["label"]: r for r in block["rows"] if "label" in r}
        # Revenue variance +10, natural → favorable
        assert by_label["Revenue"]["alerts"]["Variance"] == "favorable"
        # Direct Cost variance +5, inverse → unfavorable
        assert by_label["Direct Cost"]["alerts"]["Variance"] == "unfavorable"
        # Margin variance -2, natural → unfavorable
        assert by_label["Margin %"]["alerts"]["Variance"] == "unfavorable"

    def test_non_variance_columns_never_alert(self):
        block = build_financial_table_block(self._data(), for_excel=True)
        for r in block["rows"]:
            assert "Actuals" not in r.get("alerts", {})
