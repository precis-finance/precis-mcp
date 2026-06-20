# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

from typing import Any

from precis_mcp.engine.catalogue import BaseMetric, Catalogue, MetricPredicate
from precis_mcp.engine.resolver import GrainSpec
from precis_mcp.engine.retriever import GROUPING_COL, DataQuery


class IbisRetrieverError(Exception):
    """Raised when a federated Ibis query cannot be built or executed."""


def _table_from_source(conn: Any, source_view: str):
    """Resolve a catalogue source_view on an Ibis connection.

    Ibis backends differ slightly in how they expose schemas/catalogues. Try the
    full name first, then split the final component into ``database=...``.
    """
    try:
        return conn.table(source_view)
    except Exception:
        if "." not in source_view:
            raise
        database, table_name = source_view.rsplit(".", 1)
        return conn.table(table_name, database=database)


def _predicate_expr(table: Any, pred: MetricPredicate):
    col = table[pred.column]
    if pred.op == "eq":
        return col == pred.value
    if pred.op == "neq":
        return col != pred.value
    if pred.op == "gt":
        return col > pred.value
    if pred.op == "gte":
        return col >= pred.value
    if pred.op == "lt":
        return col < pred.value
    if pred.op == "lte":
        return col <= pred.value
    if pred.op == "in":
        return col.isin(pred.values)
    if pred.op == "not_in":
        return ~col.isin(pred.values)
    if pred.op == "is_null":
        return col.isnull()
    if pred.op == "is_not_null":
        return col.notnull()
    raise IbisRetrieverError(f"Unsupported predicate op: {pred.op!r}")


def _and_expr(exprs: list[Any]) -> Any | None:
    if not exprs:
        return None
    result = exprs[0]
    for expr in exprs[1:]:
        result = result & expr
    return result


def _filtered_value(table: Any, metric: BaseMetric):
    value = table[metric.source_column]
    if metric.sign == "abs":
        value = value.abs()
    elif metric.sign == "negate":
        value = -value

    predicate = _and_expr([_predicate_expr(table, pred) for pred in metric.where])
    if predicate is None:
        return value

    # Ibis exposes if-else both as a top-level helper in recent versions and as
    # a boolean expression method in older versions. Keep this isolated so the
    # rest of the retriever is backend-agnostic.
    try:
        import ibis  # type: ignore

        return ibis.ifelse(predicate, value, 0)
    except Exception:
        return predicate.ifelse(value, 0)


def _metric_aggregation(table: Any, metric: BaseMetric):
    if metric.aggregation != "sum" or metric.rollup_method != "sum":
        raise IbisRetrieverError(
            f"Federated metric {metric.key!r} is unsupported: "
            f"aggregation={metric.aggregation!r}, rollup_method={metric.rollup_method!r}. "
            "Phase one supports only aggregation=sum and rollup_method=sum."
        )
    return _filtered_value(table, metric).sum().name(metric.key)


def _base_filter_exprs(
    table: Any,
    data_query: DataQuery,
    dimension_filters: dict[str, list[str]] | None,
) -> list[Any]:
    exprs = [
        table["scenario"] == data_query.scenario_id,
        table["period"] >= data_query.period_start,
        table["period"] <= data_query.period_end,
    ]
    if dimension_filters:
        # Empty value list = resolved deny-all scope; isin([]) matches
        # nothing, which is the required fail-closed behaviour.
        for col, values in sorted(dimension_filters.items()):
            exprs.append(table[col].cast("string").isin(values))
    return exprs


def build_ibis_query(
    data_query: DataQuery,
    catalogue: Catalogue,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    conn: Any,
):
    domain = catalogue.domains.get(data_query.domain)
    if domain is None:
        raise KeyError(f"Unknown catalogue domain: {data_query.domain!r}")
    if domain.backend_kind != "ibis":
        raise IbisRetrieverError(
            f"Domain {data_query.domain!r} is not configured for Ibis"
        )

    table = _table_from_source(conn, domain.source_view)
    base_filter = _and_expr(_base_filter_exprs(table, data_query, dimension_filters))
    if base_filter is not None:
        table = table.filter(base_filter)

    metrics: list[BaseMetric] = []
    for key in data_query.metric_keys:
        metric = catalogue.metrics[key]
        if not isinstance(metric, BaseMetric):
            raise IbisRetrieverError(
                f"Metric {key!r} is derived; only base metrics can be retrieved"
            )
        metrics.append(metric)

    aggregations = [_metric_aggregation(table, metric) for metric in metrics]
    if dimensions:
        # Group by the physical view column, naming the output back to the
        # catalogue dimension name so the row reader keys on the catalogue name.
        # The engine cannot join master data across backends, so a derived axis
        # is only groupable here if the foreign view denormalises its column.
        dim_to_col = {cd.key: cd.source for cd in domain.dimensions if cd.source}
        available = set(table.columns)
        group_keys = []
        for dim in dimensions:
            col = dim_to_col.get(dim, dim)
            if col not in available:
                raise IbisRetrieverError(
                    f"Dimension {dim!r} is not groupable on federated domain "
                    f"{data_query.domain!r}: column {col!r} is not present on the "
                    f"foreign view {domain.source_view!r}. Federated reads cannot "
                    "join master data across backends — denormalise the column "
                    "onto the foreign view to enable this breakdown."
                )
            group_keys.append(table[col].name(dim))
        return table.group_by(group_keys).aggregate(aggregations)
    return table.aggregate(aggregations)


def execute_ibis_query(expr: Any, conn: Any) -> list[dict]:
    result = conn.execute(expr)
    if hasattr(result, "to_dict"):
        return result.to_dict(orient="records")
    return [dict(row) for row in result]


def execute_ibis_queries(
    data_query: DataQuery,
    catalogue: Catalogue,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    conn: Any,
) -> list[list[dict]]:
    expr = build_ibis_query(data_query, catalogue, dimensions, dimension_filters, conn)
    return [execute_ibis_query(expr, conn)]


def rollup_detail_rows(
    detail_rows: list[dict],
    dimensions: list[str],
    metric_keys: list[str],
    grains: GrainSpec,
) -> list[list[dict]]:
    """Additively roll up Ibis detail rows into the unified grain contract.

    The federated Ibis MVP accepts only ``aggregation=sum`` and
    ``rollup_method=sum`` metrics (enforced by ``_metric_aggregation``), so
    subtotals and grand totals can be computed from the returned detail grain
    without changing semantics. This is intentionally not used by ClickHouse,
    where ``avg``, ``closing``, and ``count_distinct`` require server-side
    grouping-set semantics.
    """
    if not dimensions:
        return [detail_rows]

    sets = _requested_grouping_sets(dimensions, grains)
    if not sets:
        return [[]]
    if sets == [list(dimensions)]:
        return [detail_rows]

    rolled_rows: list[dict] = []
    for live_dims in sets:
        rolled_rows.extend(
            _aggregate_rows_for_set(detail_rows, dimensions, metric_keys, live_dims)
        )
    return [rolled_rows]


def _requested_grouping_sets(
    dimensions: list[str],
    grains: GrainSpec,
) -> list[list[str]]:
    sets: list[list[str]] = []
    if grains.detail:
        sets.append(list(dimensions))
    if grains.subtotals:
        for level in range(len(dimensions) - 1, 0, -1):
            sets.append(dimensions[:level])
    if grains.grand_total:
        sets.append([])
    return sets


def _aggregate_rows_for_set(
    rows: list[dict],
    dimensions: list[str],
    metric_keys: list[str],
    live_dims: list[str],
) -> list[dict]:
    live = set(live_dims)
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    non_null: dict[tuple[str, ...], set[str]] = {}

    if not rows and not live_dims:
        row = _empty_rollup_row(dimensions, metric_keys, live)
        return [row]

    for row in rows:
        key = tuple(str(row.get(dim, "")) for dim in live_dims)
        if key not in groups:
            groups[key] = _empty_rollup_row(dimensions, metric_keys, live, row)
            non_null[key] = set()

        out = groups[key]
        for metric in metric_keys:
            val = row.get(metric)
            if val is None:
                continue
            out[metric] = float(out.get(metric) or 0.0) + float(val)
            non_null[key].add(metric)

    for key, out in groups.items():
        for metric in metric_keys:
            if metric not in non_null[key]:
                out[metric] = None

    return list(groups.values())


def _empty_rollup_row(
    dimensions: list[str],
    metric_keys: list[str],
    live_dims: set[str],
    source_row: dict | None = None,
) -> dict:
    row: dict[str, Any] = {}
    mask = 0
    n = len(dimensions)
    for idx, dim in enumerate(dimensions):
        if dim in live_dims:
            row[dim] = "" if source_row is None else source_row.get(dim, "")
        else:
            row[dim] = ""
            mask |= 1 << (n - 1 - idx)
    for metric in metric_keys:
        row[metric] = 0.0
    row[GROUPING_COL] = mask
    return row
