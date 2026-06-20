# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import os
from typing import Any

from precis_mcp.engine.catalogue import Catalogue, DomainCatalogue
from precis_mcp.engine.filter_resolver import resolve_filters
from precis_mcp.engine.retriever import _SAFE_COLUMN_RE
from precis_mcp.engine.scope_enforcer import enforce_cross_scenario_scope


DEFAULT_INSPECT_LIMIT = 200
MAX_INSPECT_LIMIT = int(os.getenv("INSPECTION_ROW_CAP", "10000"))


class InspectionError(ValueError):
    """Raised when an inspection request cannot be planned safely."""


def list_inspection_sources(catalogue: Catalogue) -> list[dict[str, Any]]:
    """Return inspect-enabled catalogue domains."""
    sources: list[dict[str, Any]] = []
    for source_key, domain in sorted(catalogue.domains.items()):
        if not domain.inspect_enabled:
            continue
        sources.append(
            {
                "source_key": source_key,
                "source_view": domain.source_view,
                "backend_kind": domain.backend_kind,
                "inspect_columns": list(domain.inspect_columns),
                "filter_dimensions": _filter_dimensions(catalogue, domain),
            }
        )
    return sources


def get_inspection_schema(catalogue: Catalogue, source_key: str) -> dict[str, Any]:
    """Return the configured inspection projection and filter dimensions."""
    domain = _inspect_domain(catalogue, source_key)
    return {
        "source_key": source_key,
        "source_view": domain.source_view,
        "backend_kind": domain.backend_kind,
        "inspect_columns": list(domain.inspect_columns),
        "filter_dimensions": _filter_dimensions(catalogue, domain),
    }


def inspect_rows(
    catalogue: Catalogue,
    source_key: str,
    *,
    filters: dict[str, str] | None = None,
    columns: list[str] | None = None,
    limit: int | None = None,
    scenario_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    ch_client=None,
    ibis_backends: dict[str, object] | None = None,
    per_scenario_scope: dict[str, object | None] | None = None,
) -> dict[str, Any]:
    """Run a capped row-level query against an inspect-enabled domain."""
    domain = _inspect_domain(catalogue, source_key)
    projected_columns = _project_columns(domain, columns)
    effective_limit = _effective_limit(limit)
    dimension_filters = _resolve_dimension_filters(
        catalogue,
        source_key,
        filters,
        ch_client,
        per_scenario_scope,
    )

    if domain.backend_kind == "ibis":
        rows, query_metadata = _inspect_ibis_rows(
            domain,
            projected_columns,
            dimension_filters,
            effective_limit,
            scenario_id,
            period_start,
            period_end,
            ibis_backends,
        )
    else:
        rows, query_metadata = _inspect_clickhouse_rows(
            domain,
            projected_columns,
            dimension_filters,
            effective_limit,
            scenario_id,
            period_start,
            period_end,
            ch_client,
        )

    truncated = len(rows) > effective_limit
    if truncated:
        rows = rows[:effective_limit]

    return {
        "source_key": source_key,
        "columns": projected_columns,
        "rows": rows,
        "row_count": len(rows),
        "limit": effective_limit,
        "truncated": truncated,
        "query": query_metadata,
    }


def _inspect_domain(catalogue: Catalogue, source_key: str) -> DomainCatalogue:
    domain = catalogue.domains.get(source_key)
    if domain is None:
        raise InspectionError(f"Unknown inspection source: {source_key!r}")
    if not domain.inspect_enabled:
        raise InspectionError(f"Inspection is not enabled for source {source_key!r}")
    if not domain.inspect_columns:
        raise InspectionError(f"Inspection source {source_key!r} has no columns")
    return domain


def _filter_dimensions(
    catalogue: Catalogue,
    domain: DomainCatalogue,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_filter_dimension(
        key: str,
        label: str,
        source_column: str,
    ) -> None:
        if not key or key in seen:
            return
        seen.add(key)
        result.append(
            {
                "key": key,
                "label": label,
                "source": key,
                "source_column": source_column,
            }
        )

    def walk_parents(leaf_source: str, dim_key: str, source_column: str) -> None:
        dim = catalogue.dimensions.get(dim_key)
        if dim is None:
            return
        for parent_key in dim.parents:
            parent = catalogue.dimensions.get(parent_key)
            add_filter_dimension(
                parent_key,
                parent.label if parent is not None else parent_key,
                source_column,
            )
            walk_parents(leaf_source, parent_key, source_column)

    bound_leaf_columns: dict[str, str] = {}
    for dim in domain.dimensions:
        if not dim.filterable or dim.source_inline:
            continue
        # dim.key is the catalogue dimension name; dim.source is the view column.
        bound_leaf_columns.setdefault(dim.key, dim.source)
        master_dim = catalogue.dimensions.get(dim.key)
        add_filter_dimension(
            dim.key,
            dim.label or (master_dim.label if master_dim is not None else dim.key),
            dim.source,
        )
        walk_parents(dim.source, dim.key, dim.source)

    for dim_key, dim in catalogue.dimensions.items():
        if not dim.is_ragged or dim.leaf_dimension not in bound_leaf_columns:
            continue
        add_filter_dimension(
            dim_key,
            dim.label,
            bound_leaf_columns[dim.leaf_dimension],
        )
    return result


def _project_columns(domain: DomainCatalogue, columns: list[str] | None) -> list[str]:
    allowed = set(domain.inspect_columns)
    if columns is None:
        return list(domain.inspect_columns)
    requested = list(columns)
    unknown = [col for col in requested if col not in allowed]
    if unknown:
        raise InspectionError(
            f"Requested columns are not enabled for inspection: {unknown}"
        )
    if not requested:
        raise InspectionError("Inspection projection cannot be empty")
    return requested


def _effective_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_INSPECT_LIMIT
    if limit <= 0:
        raise InspectionError("Inspection limit must be greater than zero")
    return min(limit, MAX_INSPECT_LIMIT)


def _resolve_dimension_filters(
    catalogue: Catalogue,
    source_key: str,
    filters: dict[str, str] | None,
    ch_client,
    per_scenario_scope: dict[str, object | None] | None,
) -> dict[str, list[str]] | None:
    dimension_filters: dict[str, list[str]] | None = None
    if filters:
        if ch_client is None:
            raise InspectionError("A ClickHouse client is required to resolve filters")
        dimension_filters = resolve_filters(
            filters,
            catalogue,
            ch_client,
            domain=source_key,
        ) or None

    if per_scenario_scope is not None:
        if ch_client is None:
            raise InspectionError("A ClickHouse client is required to resolve scope")
        dimension_filters = enforce_cross_scenario_scope(
            per_scenario_scope,
            dimension_filters,
            source_key,
            catalogue,
            ch_client,
        )

    return dimension_filters


def _inspect_clickhouse_rows(
    domain: DomainCatalogue,
    columns: list[str],
    dimension_filters: dict[str, list[str]] | None,
    limit: int,
    scenario_id: str | None,
    period_start: str | None,
    period_end: str | None,
    ch_client,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if ch_client is None:
        raise InspectionError("A ClickHouse client is required for this source")

    sql, params = _clickhouse_sql(
        domain,
        columns,
        dimension_filters,
        limit + 1,
        scenario_id,
        period_start,
        period_end,
    )
    result = ch_client.query(sql, parameters=params)
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return rows, {
        "backend_kind": "clickhouse",
        "backend": domain.backend,
        "source_view": domain.source_view,
        "sql": sql,
        "parameters": params,
    }


def _clickhouse_sql(
    domain: DomainCatalogue,
    columns: list[str],
    dimension_filters: dict[str, list[str]] | None,
    limit: int,
    scenario_id: str | None,
    period_start: str | None,
    period_end: str | None,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"inspect_limit": limit}
    conditions = _base_conditions(
        dimension_filters,
        scenario_id,
        period_start,
        period_end,
        params,
    )
    if domain.versioned:
        conditions.append("commit_id != '__uncommitted__'")

    select_cols = ", ".join(_quote_identifier(col) for col in columns)
    sql = f"SELECT {select_cols}\nFROM {domain.source_view}"
    if conditions:
        sql += "\nWHERE " + "\nAND ".join(conditions)
    sql += "\nLIMIT {inspect_limit:UInt64}"
    return sql, params


def _base_conditions(
    dimension_filters: dict[str, list[str]] | None,
    scenario_id: str | None,
    period_start: str | None,
    period_end: str | None,
    params: dict[str, Any],
) -> list[str]:
    conditions: list[str] = []
    if scenario_id is not None:
        params["scenario_id"] = scenario_id
        conditions.append("scenario = {scenario_id:String}")
    if period_start is not None:
        params["period_start"] = period_start
        conditions.append("period >= {period_start:String}")
    if period_end is not None:
        params["period_end"] = period_end
        conditions.append("period <= {period_end:String}")
    if dimension_filters:
        # Empty value list = resolved deny-all scope — emit the predicate so
        # it matches nothing; skipping it would invert deny into allow-all.
        for col, values in sorted(dimension_filters.items()):
            if not _SAFE_COLUMN_RE.match(col):
                raise InspectionError(f"Invalid dimension column name: {col!r}")
            param_key = f"dimf_{col}"
            params[param_key] = values
            conditions.append(f"toString({col}) IN ({{{param_key}:Array(String)}})")
    return conditions


def _inspect_ibis_rows(
    domain: DomainCatalogue,
    columns: list[str],
    dimension_filters: dict[str, list[str]] | None,
    limit: int,
    scenario_id: str | None,
    period_start: str | None,
    period_end: str | None,
    ibis_backends: dict[str, object] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if ibis_backends is None or domain.backend not in ibis_backends:
        raise InspectionError(
            f"Source {domain.domain!r} requires Ibis backend {domain.backend!r}"
        )

    from precis_mcp.engine.ibis_retriever import _and_expr, _table_from_source

    conn = ibis_backends[domain.backend]
    table = _table_from_source(conn, domain.source_view)
    exprs = []
    if scenario_id is not None:
        exprs.append(table["scenario"] == scenario_id)
    if period_start is not None:
        exprs.append(table["period"] >= period_start)
    if period_end is not None:
        exprs.append(table["period"] <= period_end)
    if dimension_filters:
        # Empty value list = resolved deny-all scope; isin([]) matches nothing.
        for col, values in sorted(dimension_filters.items()):
            exprs.append(table[col].cast("string").isin(values))
    filter_expr = _and_expr(exprs)
    if filter_expr is not None:
        table = table.filter(filter_expr)
    table = table.select([table[col] for col in columns])
    table = table.limit(limit + 1)
    compiled = None
    try:
        compiled = str(table.compile())
    except Exception:
        compiled = repr(table)
    result = conn.execute(table)
    if hasattr(result, "to_dict"):
        rows = result.to_dict(orient="records")
    else:
        rows = [dict(row) for row in result]
    return rows, {
        "backend_kind": "ibis",
        "backend": domain.backend,
        "source_view": domain.source_view,
        "sql": compiled,
        "parameters": {},
    }


def _quote_identifier(column: str) -> str:
    if not _SAFE_COLUMN_RE.match(column):
        raise InspectionError(f"Invalid column name: {column!r}")
    return f"`{column}`"
