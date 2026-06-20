# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis_mcp.engine.catalogue import BaseMetric, Catalogue, DerivedMetric, MetricPredicate
from precis_mcp.engine.resolver import GrainSpec, shift_period
from precis_mcp.engine.types import ROLLED_UP, DimensionKey, RawResults

# Column names in dimension filters must be valid SQL identifiers.
_SAFE_COLUMN_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# The fact (domain source view) is always aliased so derived breakdown axes can
# join their leaf dimension table without column-name collisions. Every fact
# column reference is qualified with this alias; joined dim columns use d0/d1/…
_FACT = "t"

# Time-hierarchy dimensions are resolved as columns on the fact view, not via a
# join. Their rollup lives in the semantic layer (period parents are
# denormalised); a join would also entangle the non-additive closing-metric path.
_TIME_HIERARCHY_DIMS = {"period", "quarter", "fiscal_year"}


def _qualify(column: str) -> str:
    """Prefix a fact-view column with the fact alias. Literal source values
    (e.g. ``"1"`` for a row count) are not identifiers and pass through."""
    if _SAFE_COLUMN_RE.match(column):
        return f"{_FACT}.{column}"
    return column

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Minimal DataQuery / ExecutionPlan dataclasses
# (The resolver will produce these; defined here for independence.)
# ---------------------------------------------------------------------------

@dataclass
class DataQuery:
    """Represents a single ClickHouse query within an execution plan."""
    scenario_key: str          # e.g. 'actuals'
    scenario_id: str           # e.g. 'ACTUALS' — value stored in CH column
    period_start: str          # e.g. '2025-01'
    period_end: str            # e.g. '2025-12'
    metric_keys: list[str]     # base metric keys to fetch
    domain: str = "pnl"       # catalogue domain
    modifiers: dict[str, str] = field(default_factory=dict)  # e.g. {"uncommitted": ""}
    time_offset: int = 0       # shifted scenario offset in months (e.g. -12 for prior_year)


@dataclass
class ExecutionPlan:
    """Full execution plan produced by the resolver."""
    data_queries: list[DataQuery]
    dimensions: list[str]      # e.g. [], ['cost_centre'], ['period'], ['quarter']
    grains: GrainSpec = field(default_factory=GrainSpec)


# ---------------------------------------------------------------------------
# SQL expression builders
# ---------------------------------------------------------------------------

def _sql_literal(value: object) -> str:
    """Render a trusted catalogue literal into a ClickHouse SQL fragment."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def compile_predicates_to_sql(where: list[MetricPredicate]) -> str:
    """Compile a metric's ``where`` predicates into a ClickHouse boolean SQL
    fragment for use inside ``CASE WHEN {filt} THEN ...``. Empty -> ``1=1``.

    Values are trusted catalogue literals; columns are validated at load.
    """
    if not where:
        return "1=1"

    parts: list[str] = []
    for pred in where:
        if not _SAFE_COLUMN_RE.match(pred.column):
            raise ValueError(f"Invalid predicate column name: {pred.column!r}")
        col = f"{_FACT}.{pred.column}"
        if pred.op == "eq":
            parts.append(f"{col} = {_sql_literal(pred.value)}")
        elif pred.op == "neq":
            parts.append(f"{col} != {_sql_literal(pred.value)}")
        elif pred.op == "gt":
            parts.append(f"{col} > {_sql_literal(pred.value)}")
        elif pred.op == "gte":
            parts.append(f"{col} >= {_sql_literal(pred.value)}")
        elif pred.op == "lt":
            parts.append(f"{col} < {_sql_literal(pred.value)}")
        elif pred.op == "lte":
            parts.append(f"{col} <= {_sql_literal(pred.value)}")
        elif pred.op == "in":
            vals = ", ".join(_sql_literal(v) for v in pred.values)
            parts.append(f"{col} IN ({vals})")
        elif pred.op == "not_in":
            vals = ", ".join(_sql_literal(v) for v in pred.values)
            parts.append(f"{col} NOT IN ({vals})")
        elif pred.op == "is_null":
            parts.append(f"{col} IS NULL")
        elif pred.op == "is_not_null":
            parts.append(f"{col} IS NOT NULL")

    return " AND ".join(parts)


def build_metric_expression(metric: BaseMetric) -> str:
    """Build the SQL CASE WHEN expression for a single base metric.

    Returns the expression WITHOUT an alias — caller adds 'AS {key}'.

    sign values:
      raw     -> SUM(CASE WHEN {filter} THEN {col} ELSE 0 END)
      abs     -> SUM(CASE WHEN {filter} THEN ABS({col}) ELSE 0 END)
      negate  -> SUM(CASE WHEN {filter} THEN -{col} ELSE 0 END)
    """
    col = _qualify(metric.source_column)
    filt = compile_predicates_to_sql(metric.where)

    if metric.aggregation == "count_distinct":
        # `source_column` is the distinct key (e.g. opportunity_id, employee_id).
        return f"COUNT(DISTINCT CASE WHEN {filt} THEN {col} END)"

    if metric.aggregation == "count":
        # Counts rows matching the filter. `source_column` is ignored — if
        # the caller needs "count non-null values of column X", that's
        # count_distinct over an appropriate key, or a derived metric.
        # `sign` is not meaningful for count and is also ignored.
        return f"COUNT(CASE WHEN {filt} THEN 1 END)"

    if metric.sign == "abs":
        value_expr = f"ABS({col})"
    elif metric.sign == "negate":
        value_expr = f"-{col}"
    else:  # raw
        value_expr = col

    return f"SUM(CASE WHEN {filt} THEN {value_expr} ELSE 0 END)"


def build_avg_metric_expression(metric: BaseMetric) -> str:
    """Build avg-rollup expression: SUM(CASE WHEN ...) / COUNT(DISTINCT period).

    NULLIF guards against zero period count (empty result set).
    """
    col = _qualify(metric.source_column)
    filt = compile_predicates_to_sql(metric.where)

    if metric.sign == "abs":
        value_expr = f"ABS({col})"
    elif metric.sign == "negate":
        value_expr = f"-{col}"
    else:
        value_expr = col

    return (
        f"SUM(CASE WHEN {filt} THEN {value_expr} ELSE 0 END)"
        f" / NULLIF(COUNT(DISTINCT {_FACT}.period), 0)"
    )


# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------

def _base_metrics_for_query(
    data_query: DataQuery,
    catalogue: Catalogue,
) -> list[BaseMetric]:
    """Return the BaseMetric objects requested in data_query, in order."""
    metrics: list[BaseMetric] = []
    for key in data_query.metric_keys:
        m = catalogue.metrics.get(key)
        if m is None:
            raise KeyError(f"Unknown metric key: {key!r}")
        if not isinstance(m, BaseMetric):
            raise TypeError(
                f"Metric {key!r} is a DerivedMetric — only BaseMetrics can be fetched from SQL"
            )
        metrics.append(m)
    return metrics


@dataclass
class _Join:
    """A LEFT JOIN to a leaf dimension table for a derived breakdown axis."""
    alias: str          # d0, d1, …
    table: str          # semantic.dim_cost_centre
    fact_col: str       # the leaf's bound column on the fact view
    dim_key_col: str    # the leaf dim table's key column

    def sql(self) -> str:
        return (
            f"LEFT JOIN {self.table} {self.alias} "
            f"ON {_FACT}.{self.fact_col} = {self.alias}.{self.dim_key_col}"
        )


def _resolve_breakdowns(
    dimensions: list[str],
    catalogue: Catalogue,
    domain_cat,
) -> tuple[dict[str, str], list[_Join]]:
    """Resolve each breakdown axis to a qualified SQL expression plus any joins.

    - Bound axes (leaf dimensions, period, or an explicit derived binding) read a
      fact-view column: ``t.<column>``.
    - Time-hierarchy parents (quarter/fiscal_year) stay denormalised on the fact
      view: ``t.<name>``.
    - Other derived/parent axes join their leaf dimension table and group by the
      derived value column: ``d0.<column>``. One join per leaf, deduplicated.

    Returns ``(name_to_expr, joins)``. Raises ``KeyError`` for a derived axis
    whose leaf is not bound to this domain (so it cannot be joined).
    """
    bound = {cd.key: cd.source for cd in domain_cat.dimensions if cd.source}
    name_to_expr: dict[str, str] = {}
    joins: list[_Join] = []
    join_by_key: dict[tuple[str, str], _Join] = {}

    for name in dimensions:
        if name in bound:
            name_to_expr[name] = f"{_FACT}.{bound[name]}"
            continue
        if name in _TIME_HIERARCHY_DIMS:
            name_to_expr[name] = f"{_FACT}.{name}"
            continue

        dim = catalogue.dimensions.get(name)
        resolution = None
        if dim is not None:
            for leaf_key, res in dim._transitive.items():
                if leaf_key in bound:
                    resolution = res
                    break
        if resolution is None:
            raise KeyError(
                f"Dimension {name!r} is not groupable on domain "
                f"{domain_cat.domain!r}: it resolves to no leaf dimension bound "
                "to this domain."
            )

        fact_col = bound[resolution.leaf_dimension]
        join_key = (resolution.source_table, fact_col)
        join = join_by_key.get(join_key)
        if join is None:
            join = _Join(
                alias=f"d{len(joins)}",
                table=resolution.source_table,
                fact_col=fact_col,
                dim_key_col=resolution.leaf_key_column,
            )
            joins.append(join)
            join_by_key[join_key] = join
        name_to_expr[name] = f"{join.alias}.{resolution.filter_column}"

    return name_to_expr, joins


def _select_cols(
    metrics: list[BaseMetric],
    rollup_group: str | None,
    dimensions: list[str],
    name_to_expr: dict[str, str],
) -> str:
    """Build SELECT column list.

    Dimension axes are selected as ``<expr> AS <catalogue name>`` so the result
    reader keys rows by the catalogue name regardless of how (or where) the
    underlying column lives.

    rollup_group controls which expression builder to use:
      'avg'     -> build_avg_metric_expression
      otherwise -> build_metric_expression
    """
    parts: list[str] = [f"{name_to_expr[dim]} AS {dim}" for dim in dimensions]

    for m in metrics:
        if rollup_group == "avg":
            expr = build_avg_metric_expression(m)
        else:
            expr = build_metric_expression(m)
        parts.append(f"{expr} AS {m.key}")

    return "\n    , ".join(parts)


def _where_clause(
    data_query: DataQuery,
    rollup_group: str | None,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    closing_only: bool = False,
    closing_time_dims: list[str] | None = None,
    source_view: str | None = None,
    versioned: bool = True,
) -> tuple[str, dict]:
    """Build WHERE clause and params dict.

    Applies scenario modifiers from ``data_query.modifiers``:
    - No modifiers (default): ``commit_id != '__uncommitted__'``
      (committed-only for plan scenarios; harmless for actuals).
    - ``uncommitted``: no commit_id filter (shows everything).
    - ``uncommitted_delta``: ``commit_id = '__uncommitted__'`` only.
    - ``commit={id}``: all commits up to and including the given ID.
    - ``commit_delta={id}``: single commit only.

    closing_time_dims: when closing_only=True and the query groups by
    time-hierarchy columns (quarter, fiscal_year), list those columns here.
    Instead of filtering to period_end globally, a subquery selects the
    last period within each time-dimension group.
    """
    conditions: list[str] = []
    params: dict = {
        "scenario_id": data_query.scenario_id,
        "period_start": data_query.period_start,
        "period_end": data_query.period_end,
    }

    conditions.append(f"{_FACT}.scenario = {{scenario_id:String}}")

    if closing_only and closing_time_dims and source_view:
        # Full period range — the subquery below handles the per-group closing
        # filter. Time-hierarchy dims are fact columns, so the outer reference is
        # t-qualified while the self-contained subquery stays unqualified.
        conditions.append(
            f"{_FACT}.period >= {{period_start:String}} "
            f"AND {_FACT}.period <= {{period_end:String}}"
        )
        outer_cols = ", ".join(f"{_FACT}.{d}" for d in closing_time_dims)
        inner_cols = ", ".join(closing_time_dims)
        conditions.append(
            f"({outer_cols}, {_FACT}.period) IN ("
            f"SELECT {inner_cols}, max(period) "
            f"FROM {source_view} "
            f"WHERE scenario = {{scenario_id:String}} "
            f"AND period >= {{period_start:String}} AND period <= {{period_end:String}} "
            f"GROUP BY {inner_cols})"
        )
    elif closing_only:
        conditions.append(f"{_FACT}.period = {{period_end:String}}")
    else:
        conditions.append(
            f"{_FACT}.period >= {{period_start:String}} "
            f"AND {_FACT}.period <= {{period_end:String}}"
        )

    # rollup_group controls expression builder only — not a DB column, no WHERE filter

    if dimension_filters:
        # An empty value list is a resolved deny-all scope (e.g. a user filter
        # disjoint from their dimension scope) — emit the predicate so it
        # matches nothing. Skipping it would invert deny-all into allow-all.
        for col, values in sorted(dimension_filters.items()):
            if not _SAFE_COLUMN_RE.match(col):
                raise ValueError(f"Invalid dimension column name: {col!r}")
            param_key = f"dimf_{col}"
            params[param_key] = values
            conditions.append(
                f"toString({_FACT}.{col}) IN ({{{param_key}:Array(String)}})"
            )

    # ----- Commit-awareness modifiers -----
    # Only apply commit_id filters for versioned domains (those whose source
    # view includes a commit_id column — e.g. v_pnl, v_gl).  Actuals-only
    # domains (timesheets, payroll, utilisation) have no commit_id column.
    if versioned:
        modifiers = data_query.modifiers

        if "uncommitted_delta" in modifiers:
            # Only uncommitted changes
            conditions.append(f"{_FACT}.commit_id = '__uncommitted__'")
        elif "uncommitted" in modifiers:
            # Include everything (committed + uncommitted) — no commit_id filter
            pass
        elif "commit_delta" in modifiers:
            # Changes from a single commit only
            commit_id = modifiers["commit_delta"]
            params["mod_commit_id"] = commit_id
            conditions.append(f"{_FACT}.commit_id = {{mod_commit_id:String}}")
        elif "commit" in modifiers:
            # Time travel: state as of a specific commit (all commits up to and including)
            target_commit = modifiers["commit"]
            params["mod_target_commit"] = target_commit
            params["mod_scenario_id_commits"] = data_query.scenario_id
            conditions.append(
                f"{_FACT}.commit_id IN ("
                "SELECT commit_id FROM planning.commits "
                "WHERE scenario_id = {mod_scenario_id_commits:String} "
                "AND created_at <= ("
                "SELECT created_at FROM planning.commits "
                "WHERE commit_id = {mod_target_commit:String} LIMIT 1"
                ")"
                ")"
            )
        else:
            # Default: committed-only (exclude uncommitted changes).
            # For actuals, commit_id = '__actuals__' so this is harmless.
            conditions.append(f"{_FACT}.commit_id != '__uncommitted__'")

    where = "\nAND ".join(conditions)
    return where, params


# Column carrying the ClickHouse GROUPING() bitmask. Present only when more than
# the detail grain is requested; the bit for each dimension is 1 when that
# dimension is rolled up in the row, letting the reader tag the row's grain.
GROUPING_COL = "_grouping"


def _grouping_sets(dimensions: list[str], grains: GrainSpec) -> list[list[str]]:
    """Dimension subsets to aggregate at, derived from the requested grains.

    The full dimension list for detail, right-to-left prefixes for subtotals,
    and the empty list for the grand total.
    """
    sets: list[list[str]] = []
    if grains.detail:
        sets.append(list(dimensions))
    if grains.subtotals:
        for level in range(len(dimensions) - 1, 0, -1):
            sets.append(dimensions[:level])
    if grains.grand_total:
        sets.append([])
    return sets


def _group_clause_from_sets(
    dimensions: list[str],
    sets: list[list[str]],
    name_to_expr: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Build the GROUP BY clause and whether a GROUPING() tag column is needed
    from an explicit list of grouping sets.

    No dimensions, or a single set equal to the full dimension list, yields a
    plain GROUP BY (or nothing) and no tag column — identical SQL to a
    single-grain query. Anything else yields GROUP BY GROUPING SETS and signals
    that a GROUPING() column must be selected to tag each row's grain. The
    grouping keys are emitted as their resolved SQL expressions.
    """
    name_to_expr = name_to_expr or {}
    def _expr(d: str) -> str:
        return name_to_expr.get(d, d)
    if not dimensions:
        return "", False
    if sets == [list(dimensions)]:
        return f"GROUP BY {', '.join(_expr(d) for d in dimensions)}", False
    rendered = ", ".join("(" + ", ".join(_expr(d) for d in s) + ")" for s in sets)
    return f"GROUP BY GROUPING SETS ({rendered})", True


# ---------------------------------------------------------------------------
# Public: generate_sql
# ---------------------------------------------------------------------------

def generate_sql(
    data_query: DataQuery,
    catalogue: Catalogue,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    grains: GrainSpec = GrainSpec(),
) -> list[tuple[str, dict]]:
    """Generate SQL query(ies) for a DataQuery.

    Args:
        data_query:        The data query from the execution plan.
        catalogue:         The loaded catalogue.
        dimensions:        List of dimension names for GROUP BY (e.g. ['period'], ['cost_centre']).
        dimension_filters: Resolved dimension filters {view_col: [leaf_ids]}, or None.
        grains:            Which aggregation grains to emit. Default is detail only.

    Returns:
        List of (sql_string, params_dict) tuples.
        Up to 3 tuples (one per rollup_method group that has metrics).
        When no dimensions, returns a single aggregate row.
    """
    domain = catalogue.domains.get(data_query.domain)
    if domain is None:
        raise KeyError(f"Unknown catalogue domain: {data_query.domain!r}")
    source_view = domain.source_view
    versioned = domain.versioned

    all_base = _base_metrics_for_query(data_query, catalogue)

    # Detect which requested dimensions are period-hierarchy levels
    # (e.g. quarter, fiscal_year) so closing metrics can pick the last
    # period per group instead of the global period_end.
    closing_time_dims: list[str] = []
    period_dim = catalogue.dimensions.get("period")
    if period_dim:
        # Parent dimensions of period (e.g. quarter, fiscal_year) are period hierarchy levels
        period_parent_keys = set(period_dim.parents.keys())
        closing_time_dims = [d for d in dimensions if d in period_parent_keys]

    name_to_expr, joins = _resolve_breakdowns(dimensions, catalogue, domain)

    return _generate_aggregate_sql(
        data_query, all_base, source_view, dimensions, dimension_filters,
        versioned=versioned,
        closing_time_dims=closing_time_dims,
        grains=grains,
        name_to_expr=name_to_expr,
        joins=joins,
    )


def _from_clause(source_view: str, joins: list[_Join]) -> str:
    """The aliased FROM with any derived-axis leaf joins appended."""
    sql = f"FROM {source_view} {_FACT}"
    for join in joins:
        sql += f"\n{join.sql()}"
    return sql


def _full_grouping_expr(
    dimensions: list[str], time_dims: list[str], grouped_dims: set[str],
    name_to_expr: dict[str, str] | None = None,
) -> str:
    """SQL reproducing GROUPING(<all dims>) for the closing-totals query.

    Time dimensions (and any non-time dimension never used as a group key) are
    absent from that query's GROUP BY and always rolled up, so they contribute a
    constant bit; dimensions that are group keys use GROUPING(). Bit weights are
    most-significant-bit-first (first dimension = highest bit), matching how
    _row_to_dimension_key decodes the tag.
    """
    name_to_expr = name_to_expr or {}
    n = len(dimensions)
    terms: list[str] = []
    for i, d in enumerate(dimensions):
        weight = 1 << (n - 1 - i)
        if d in grouped_dims:
            terms.append(f"GROUPING({name_to_expr.get(d, d)}) * {weight}")
        else:
            terms.append(str(weight))
    return " + ".join(terms)


def _closing_totals_query(
    data_query: DataQuery,
    metrics: list[BaseMetric],
    source_view: str,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    versioned: bool,
    time_dims: list[str],
    time_rolled_sets: list[list[str]],
    name_to_expr: dict[str, str],
    joins: list[_Join],
) -> tuple[str, dict]:
    """Totals for closing metrics where a time dimension is rolled up.

    Closing is non-additive over time, so these grains take the value at the
    global period_end (closing_only, no per-group max-period subquery) and
    aggregate across the non-time dimensions. The time columns are not in this
    query's GROUP BY — they are emitted as rolled-up placeholders and the
    GROUPING() tag is rebuilt for the full dimension list.
    """
    non_time = [d for d in dimensions if d not in time_dims]
    # Project each time-rolled grain onto the non-time dimensions it keeps live,
    # de-duplicating (different time-rolled grains can share a projection).
    seen: set[tuple[str, ...]] = set()
    nt_sets: list[list[str]] = []
    for s in time_rolled_sets:
        proj = [d for d in s if d in non_time]
        key = tuple(proj)
        if key not in seen:
            seen.add(key)
            nt_sets.append(proj)

    grouped_dims = {d for s in nt_sets for d in s}

    select_parts: list[str] = [f"{name_to_expr[d]} AS {d}" for d in non_time]
    select_parts += [f"'' AS {t}" for t in time_dims]
    select_parts += [f"{build_metric_expression(m)} AS {m.key}" for m in metrics]
    select_parts.append(
        f"{_full_grouping_expr(dimensions, time_dims, grouped_dims, name_to_expr)} AS {GROUPING_COL}"
    )

    where, params = _where_clause(
        data_query,
        rollup_group="closing",
        dimensions=non_time,
        dimension_filters=dimension_filters,
        closing_only=True,
        versioned=versioned,
    )

    sql = (
        "SELECT\n    " + "\n    , ".join(select_parts) + "\n"
        + _from_clause(source_view, joins) + "\n"
        f"WHERE {where}"
    )
    # A lone grand-total set has no group keys — plain aggregate, no GROUPING SETS.
    if nt_sets != [[]]:
        rendered = ", ".join(
            "(" + ", ".join(name_to_expr[d] for d in s) + ")" for s in nt_sets
        )
        sql += f"\nGROUP BY GROUPING SETS ({rendered})"
    return sql, params


def _generate_aggregate_sql(
    data_query: DataQuery,
    metrics: list[BaseMetric],
    source_view: str,
    dimensions: list[str],
    dimension_filters: dict[str, list[str]] | None,
    versioned: bool = True,
    closing_time_dims: list[str] | None = None,
    grains: GrainSpec = GrainSpec(),
    name_to_expr: dict[str, str] | None = None,
    joins: list[_Join] | None = None,
) -> list[tuple[str, dict]]:
    """One query per rollup_method group present in metrics.

    Each query covers the requested grains via GROUP BY GROUPING SETS, with a
    GROUPING() tag column when more than the detail grain is asked for. The
    closing group falls back to detail only when a time-hierarchy dimension is
    present, because a rolled-up closing balance over time is non-additive and
    needs a dedicated query.
    """
    name_to_expr = name_to_expr or {}
    joins = joins or []

    # Group metrics by rollup_method
    groups: dict[str, list[BaseMetric]] = {"sum": [], "avg": [], "closing": []}
    for m in metrics:
        groups[m.rollup_method].append(m)

    results: list[tuple[str, dict]] = []

    def _build(rollup_group: str, where_extra: dict, sets: list[list[str]]) -> None:
        select_cols = _select_cols(
            groups[rollup_group], rollup_group=rollup_group, dimensions=dimensions,
            name_to_expr=name_to_expr,
        )
        where, params = _where_clause(
            data_query,
            rollup_group=rollup_group,
            dimensions=dimensions,
            dimension_filters=dimension_filters,
            versioned=versioned,
            **where_extra,
        )
        group_clause, tag = _group_clause_from_sets(dimensions, sets, name_to_expr)
        if tag:
            grouping_cols = ", ".join(name_to_expr[d] for d in dimensions)
            select_cols += f"\n    , GROUPING({grouping_cols}) AS {GROUPING_COL}"
        sql = (
            f"SELECT\n    {select_cols}\n"
            + _from_clause(source_view, joins) + "\n"
            f"WHERE {where}"
        )
        if group_clause:
            sql += f"\n{group_clause}"
        results.append((sql, params))

    requested_sets = _grouping_sets(dimensions, grains)

    if groups["sum"]:
        _build("sum", {}, requested_sets)

    if groups["avg"]:
        _build("avg", {}, requested_sets)

    if groups["closing"]:
        # When 'period' is a dimension, each row is already a single period so
        # the closing value is correct as-is. closing_only applies when period is
        # aggregated away or when grouping by parent time dims (quarter, fiscal_year).
        period_in_dims = "period" in dimensions
        closing_where = {
            "closing_only": not period_in_dims,
            "closing_time_dims": closing_time_dims or None,
            "source_view": source_view,
        }
        time_dims = list(closing_time_dims or []) + (["period"] if period_in_dims else [])
        if not time_dims:
            _build("closing", closing_where, requested_sets)
        else:
            # Grains that keep every time dimension live roll up additively and
            # use the normal closing query. Grains that roll up a time dimension
            # are non-additive over time and need the global-period_end query.
            time_live = [s for s in requested_sets if all(t in s for t in time_dims)]
            time_rolled = [s for s in requested_sets if not all(t in s for t in time_dims)]
            if time_live:
                _build("closing", closing_where, time_live)
            if time_rolled:
                results.append(
                    _closing_totals_query(
                        data_query, groups["closing"], source_view, dimensions,
                        dimension_filters, versioned, time_dims, time_rolled,
                        name_to_expr=name_to_expr, joins=joins,
                    )
                )

    return results


# ---------------------------------------------------------------------------
# Execution (thin ClickHouse wrapper)
# ---------------------------------------------------------------------------

def execute_queries(
    queries: list[tuple[str, dict]],
    ch_client,
) -> list[list[dict]]:
    """Execute SQL queries against ClickHouse.

    Uses ClickHouse native parameterised queries (``{name:Type}`` syntax)
    to avoid SQL injection.

    Returns a list of result row lists, one per query.
    Each row is a dict of column_name -> value.
    """
    result_sets: list[list[dict]] = []
    for sql, params in queries:
        result = ch_client.query(sql, parameters=params)
        rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
        result_sets.append(rows)

    return result_sets


# ---------------------------------------------------------------------------
# Row -> DimensionKey helpers
# ---------------------------------------------------------------------------

def _row_to_dimension_key(row: dict, dimensions: list[str]) -> DimensionKey:
    """Extract dimension values from a result row into a tuple.

    When a GROUPING() tag column is present (multi-grain GROUPING SETS queries),
    each dimension rolled up in this row is set to ROLLED_UP so subtotal and
    grand-total rows stay distinct from detail rows that share ClickHouse's
    default-filled values. Without the tag column every dimension is live.
    The bitmask is most-significant-bit-first: the first dimension argument
    occupies the highest bit.
    """
    mask = row.get(GROUPING_COL)
    if mask is None:
        return tuple(str(row[dim]) for dim in dimensions if dim in row)
    bits = int(mask)
    n = len(dimensions)
    return tuple(
        ROLLED_UP if (bits >> (n - 1 - i)) & 1 else str(row[dim])
        for i, dim in enumerate(dimensions)
    )


def _merge_row_into_results(
    scenario_results: dict[DimensionKey, dict[str, float | None]],
    row: dict,
    metric_keys: list[str],
    dimensions: list[str],
) -> None:
    """Merge a single result row into the scenario results dict."""
    dim_key = _row_to_dimension_key(row, dimensions)
    if dim_key not in scenario_results:
        scenario_results[dim_key] = {}
    for key in metric_keys:
        if key in row:
            val = row[key]
            scenario_results[dim_key][key] = float(val) if val is not None else None


# ---------------------------------------------------------------------------
# Time dimension shifting for shifted scenarios
# ---------------------------------------------------------------------------

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_FISCAL_YEAR_RE = re.compile(r"^(\d{4})$")


def _shift_time_value(value: str, dim_name: str, offset_months: int) -> str:
    """Shift a time dimension value by offset_months.

    Supports period (YYYY-MM), quarter (YYYY-QN), and fiscal_year (YYYY).
    """
    if dim_name == "period":
        return shift_period(value, offset_months)

    if dim_name == "quarter":
        m = _QUARTER_RE.match(value)
        if m:
            year, q = int(m.group(1)), int(m.group(2))
            # Convert to start month, shift, convert back
            start_month = (q - 1) * 3 + 1
            shifted = shift_period(f"{year:04d}-{start_month:02d}", offset_months)
            new_year, new_month = int(shifted[:4]), int(shifted[5:7])
            new_q = (new_month - 1) // 3 + 1
            return f"{new_year:04d}-Q{new_q}"
        return value

    if dim_name == "fiscal_year":
        m = _FISCAL_YEAR_RE.match(value)
        if m:
            year = int(m.group(1))
            # Shift by full years (offset_months / 12)
            shifted = shift_period(f"{year:04d}-01", offset_months)
            return shifted[:4]
        return value

    return value


# ---------------------------------------------------------------------------
# Public: retrieve
# ---------------------------------------------------------------------------

def retrieve(
    plan: ExecutionPlan,
    catalogue: Catalogue,
    ch_client,
    dimension_filters: dict[str, list[str]] | None = None,
    ibis_backends: dict[str, object] | None = None,
) -> RawResults:
    """Execute all data queries in the plan and return raw results.

    Merges results from multiple rollup_method groups into a unified result
    per scenario.

    Args:
        plan:              Execution plan from the resolver.
        catalogue:         Loaded catalogue.
        ch_client:         ClickHouse client (clickhouse-connect).
        dimension_filters: Resolved dimension filters {view_col: [leaf_ids]}, or None.

    Returns:
        RawResults: scenario_key -> dimension_key -> metric_key -> value
    """
    raw: RawResults = {}

    # Track which scenarios need time dimension remapping (shifted scenarios)
    # Identify which dimensions in the plan are time-based (period, quarter, fiscal_year)
    _TIME_DIMS = {"period", "quarter", "fiscal_year"}
    time_dim_indices: list[tuple[int, str]] = [
        (i, dim) for i, dim in enumerate(plan.dimensions) if dim in _TIME_DIMS
    ]

    time_remap: dict[str, int] = {}  # scenario_key -> inverse offset

    for dq in plan.data_queries:
        scenario_key = dq.scenario_key

        if scenario_key not in raw:
            raw[scenario_key] = {}

        if dq.time_offset != 0 and time_dim_indices:
            time_remap[scenario_key] = -dq.time_offset

        scenario_results = raw[scenario_key]

        domain = catalogue.domains.get(dq.domain)
        if domain is None:
            raise KeyError(f"Unknown catalogue domain: {dq.domain!r}")

        if domain.backend_kind == "ibis":
            if ibis_backends is None or domain.backend not in ibis_backends:
                raise RuntimeError(
                    f"Domain {dq.domain!r} requires Ibis backend {domain.backend!r}, "
                    "but no connection was provided"
                )
            from precis_mcp.engine.ibis_retriever import (
                execute_ibis_queries,
                rollup_detail_rows,
            )

            result_sets = execute_ibis_queries(
                dq,
                catalogue,
                plan.dimensions,
                dimension_filters,
                ibis_backends[domain.backend],
            )
            result_sets = rollup_detail_rows(
                result_sets[0],
                plan.dimensions,
                dq.metric_keys,
                plan.grains,
            )
        else:
            queries = generate_sql(dq, catalogue, plan.dimensions, dimension_filters, plan.grains)
            result_sets = execute_queries(queries, ch_client)

        for result_set in result_sets:
            for row in result_set:
                _merge_row_into_results(
                    scenario_results,
                    row,
                    dq.metric_keys,
                    plan.dimensions,
                )

    # Remap time dimension keys for shifted scenarios so they align
    # with the original requested period range (e.g. 2024-01 → 2025-01,
    # 2024-Q1 → 2025-Q1 for prior_year with time_offset=-12).
    if time_remap:
        for scenario_key, inverse_offset in time_remap.items():
            old_data = raw.get(scenario_key, {})
            new_data: dict[DimensionKey, dict[str, float | None]] = {}
            for dim_key, metrics in old_data.items():
                parts = list(dim_key)
                for idx, dim_name in time_dim_indices:
                    if parts[idx] != ROLLED_UP:
                        parts[idx] = _shift_time_value(parts[idx], dim_name, inverse_offset)
                new_data[tuple(parts)] = metrics
            raw[scenario_key] = new_data

    return raw
