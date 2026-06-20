# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pipeline orchestrator for the metric engine.

Chains the five stages together:
  1. Resolve  (resolver.resolve)
  2. Filter Resolution  (filter_resolver.resolve_filters)
  3. Retrieve  (retriever.retrieve / retriever.generate_sql)
  4. Transform  (transformer.transform)
  5. Format  (formatter.format_response)

Between stages 4 and 5, the orchestrator builds dimension display/sort
lookups from the catalogue metadata + a source-table query, and passes
them to the formatter as ``dim_formats``.
"""
from __future__ import annotations

import logging

from precis_mcp.engine import catalogue as catalogue_module
from precis_mcp.engine import formatter
from precis_mcp.engine import resolver
from precis_mcp.engine import retriever
from precis_mcp.engine import transformer
from precis_mcp.engine.filter_resolver import resolve_filters

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-exports — consumers can do: from precis_mcp.engine import Catalogue, ...
# ---------------------------------------------------------------------------

from precis_mcp.engine.catalogue import Catalogue, CatalogueError
from precis_mcp.engine.resolver import ResolverError


# ---------------------------------------------------------------------------
# Type-bridging helpers
# ---------------------------------------------------------------------------

def _to_retriever_query(resolver_query: resolver.DataQuery) -> retriever.DataQuery:
    """Convert resolver DataQuery to retriever DataQuery."""
    return retriever.DataQuery(
        scenario_key=resolver_query.scenario_key,
        scenario_id=resolver_query.scenario_id,
        period_start=resolver_query.period_start,
        period_end=resolver_query.period_end,
        metric_keys=resolver_query.metric_keys,
        domain=resolver_query.domain,
        modifiers=resolver_query.modifiers,
        time_offset=resolver_query.time_offset,
    )


def _to_formatter_block(
    resolved_block: resolver.ResolvedBlock,
    catalogue: catalogue_module.Catalogue,
) -> formatter.FormatterBlock:
    """Convert resolver ResolvedBlock to formatter FormatterBlock."""
    del catalogue
    return formatter.FormatterBlock(
        alias=resolved_block.alias,
        scenario_key=resolved_block.scenario_key,
        metric_keys=resolved_block.metric_keys,
        display_items=resolved_block.display_items,
        is_statement=resolved_block.is_statement,
        display_format=(
            resolved_block.display_format
        ),
        color_code=(
            resolved_block.color_code
        ),
    )


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------

def load_and_validate(catalogue_dir: str) -> Catalogue:
    """Load catalogue from directory — convenience wrapper for server startup."""
    return catalogue_module.load_catalogue(catalogue_dir)


def check_dimension_sources(catalogue: Catalogue) -> list[str]:
    """Probe ClickHouse for each leaf dimension's source table.

    Returns a list of warning strings for tables that don't exist or can't be
    reached.  Logs each warning.  Never raises — this is advisory only.
    """
    from precis_mcp.db import get_clickhouse_client

    warnings: list[str] = []
    try:
        ch = get_clickhouse_client()
    except Exception as exc:
        msg = f"Cannot check dimension sources — ClickHouse unavailable: {exc}"
        logger.warning(msg)
        return [msg]

    checked: set[str] = set()
    for key, dim in catalogue.dimensions.items():
        if not dim.is_leaf or dim.source is None:
            continue
        table = dim.source.table
        if table in checked:
            continue
        checked.add(table)
        try:
            ch.query(f"SELECT 1 FROM {table} LIMIT 0")
        except Exception:
            msg = f"Dimension '{key}': source table '{table}' is not reachable in ClickHouse"
            logger.warning(msg)
            warnings.append(msg)

    return warnings


# ---------------------------------------------------------------------------
# Dimension display/sort lookup helpers
# ---------------------------------------------------------------------------

def _resolve_master_dimension(
    cat: Catalogue,
    dim_name: str,
) -> catalogue_module.Dimension | None:
    """Resolve a dimension name to its master Dimension definition.

    A CubeDimension's ``key`` is the catalogue dimension name, so a native
    binding resolves through the direct lookup; the CubeDimension scan only
    matches inline axes, which have no master dimension and resolve to None.
    """
    if dim_name in cat.dimensions:
        return cat.dimensions[dim_name]
    for domain in cat.domains.values():
        for cd in domain.dimensions:
            if cd.key == dim_name:
                return cat.dimensions.get(cd.key)
    return None


def _reject_inline_dimension_filters(
    cat: Catalogue,
    domain: str,
    filters: dict,
) -> None:
    """Reject filters on federated source-only dimensions.

    Inline dimensions are reporting axes only in the first federated release:
    they can appear in ``dimensions`` for group-by, but they do not have CH
    master data for member validation, hierarchy expansion, or scope merging.
    """
    domain_cat = cat.domains.get(domain)
    if not domain_cat or not filters:
        return

    inline_keys = {
        cd.key
        for cd in domain_cat.dimensions
        if cd.source_inline and not cd.filterable
    }
    blocked = sorted(set(filters) & inline_keys)
    if blocked:
        joined = ", ".join(repr(key) for key in blocked)
        raise ResolverError(
            f"Federated-only dimension(s) {joined} can be used as reporting "
            "axes but cannot be used as filters in this phase"
        )


def _fetch_dimension_lookup(
    ch_client,
    dim: catalogue_module.Dimension,
    attrs_needed: set[str],
) -> dict[str, dict[str, str | None]]:
    """Query the dimension source table for attribute values.

    Returns ``{code: {attr_name: value, ...}, ...}`` for every leaf row.
    """
    if not dim.source:
        return {}

    key_col = dim.source.key_column
    select_cols: list[str] = [key_col]
    attr_to_col: dict[str, str] = {}
    for attr_name in sorted(attrs_needed):
        source_col = dim.source.attribute_mapping.get(attr_name, "")
        if source_col and source_col not in select_cols:
            select_cols.append(source_col)
        attr_to_col[attr_name] = source_col

    sql = f"SELECT DISTINCT {', '.join(select_cols)} FROM {dim.source.table}"
    result = ch_client.query(sql)
    cols = result.column_names

    lookup: dict[str, dict[str, str | None]] = {}
    for row in result.result_rows:
        row_dict = dict(zip(cols, row))
        code = str(row_dict[key_col])
        attrs: dict[str, str | None] = {}
        for attr_name, source_col in attr_to_col.items():
            val = row_dict.get(source_col)
            attrs[attr_name] = str(val) if val is not None else None
        lookup[code] = attrs
    return lookup


def _build_dim_formats(
    cat: Catalogue,
    dimensions: list[str],
    ch_client,
) -> dict[str, formatter.DimensionFormat] | None:
    """Build DimensionFormat lookups for dimensions that have display/sort attributes.

    Returns None when no dimension needs formatting (or no dimensions requested).
    Skips ``'period'`` which is a virtual dimension handled by the retriever.
    """
    if not dimensions or ch_client is None:
        return None

    dim_formats: dict[str, formatter.DimensionFormat] = {}

    for dim_name in dimensions:
        master_dim = _resolve_master_dimension(cat, dim_name)
        if master_dim is None:
            continue

        display_attr = master_dim.display_attribute
        sort_attr = master_dim.sort_attribute

        if not display_attr and not sort_attr:
            continue

        # Collect which attributes we need from the source table
        attrs_needed: set[str] = set()
        if display_attr and display_attr in master_dim.attributes:
            attrs_needed.add(display_attr)
        if sort_attr and sort_attr in master_dim.attributes:
            attrs_needed.add(sort_attr)

        if not attrs_needed or not master_dim.source:
            continue

        try:
            lookup = _fetch_dimension_lookup(ch_client, master_dim, attrs_needed)
        except Exception:
            logger.warning(
                "Failed to fetch dimension lookup for %r; "
                "falling back to raw codes",
                dim_name,
                exc_info=True,
            )
            continue

        dim_formats[dim_name] = formatter.DimensionFormat(
            display_attr=display_attr,
            sort_attr=sort_attr,
            lookup=lookup,
        )

    return dim_formats if dim_formats else None


# ---------------------------------------------------------------------------
# Main orchestration function
# ---------------------------------------------------------------------------

def execute_report(
    request: dict,
    catalogue: Catalogue,
    ch_client=None,
    scope=None,
    ibis_backends: dict[str, object] | None = None,
    scenario_registry=None,
) -> dict:
    """Execute a report request through the full pipeline.

    Args:
        request:    Report request dict (context, filters, dimensions, blocks).
                    Optional context keys:
                    - ``scale``: int (0/3/6/9) — scaling power for currency values.
                    - ``decimals``: int — override decimal places.
        catalogue:  Loaded Catalogue instance.
        ch_client:  ClickHouse client (None → dry-run / test mode).

    Returns:
        Formatted response dict.

    Raises:
        ResolverError:  On invalid request.
        CatalogueError: On catalogue issues.
    """

    # ------------------------------------------------------------------
    # Stage 0: Extract security scope (injected by the calling layer)
    # ------------------------------------------------------------------
    # Per-scenario scope: {scenario_id: ScopeSpec | None} or None
    per_scenario_scope: dict | None = scope

    # ------------------------------------------------------------------
    # Stage 1: Resolve request → ExecutionPlan
    # ------------------------------------------------------------------
    if scenario_registry is None and ch_client is not None:
        try:
            from precis_mcp.engine.scenario_registry import load_scenario_registry

            scenario_registry = load_scenario_registry(ch_client)
        except Exception:
            logger.warning("Could not load semantic scenario registry", exc_info=True)
    if scenario_registry is None:
        raise ResolverError("ScenarioRegistry is required for report execution")

    plan: resolver.ExecutionPlan = resolver.resolve(
        request,
        catalogue,
        scenario_registry=scenario_registry,
    )

    backend_kinds = {
        catalogue.domains[dq.domain].backend_kind
        for dq in plan.data_queries
        if dq.domain in catalogue.domains
    }
    if len(backend_kinds) > 1:
        raise ResolverError(
            "Requests cannot mix ClickHouse and federated Ibis domains in phase one"
        )
    if plan.computed_evals and len({
        catalogue.domains[dq.domain].backend
        for dq in plan.data_queries
        if dq.domain in catalogue.domains
    }) > 1:
        raise ResolverError(
            "Computed scenarios cannot mix ClickHouse and federated domains in phase one"
        )

    required_ibis_backends = {
        catalogue.domains[dq.domain].backend
        for dq in plan.data_queries
        if dq.domain in catalogue.domains
        and catalogue.domains[dq.domain].backend_kind == "ibis"
    }
    if required_ibis_backends and ibis_backends is None:
        from precis_mcp.engine.ibis_registry import get_ibis_backends

        ibis_backends = get_ibis_backends(required_ibis_backends)

    # ------------------------------------------------------------------
    # Stage 2: Dimension filter resolution
    # ------------------------------------------------------------------
    dimension_filters: dict[str, list[str]] | None = None

    # Infer domain from the plan's data queries (all share the same domain)
    request_domain = "pnl"
    if plan.data_queries:
        request_domain = plan.data_queries[0].domain

    # Resolve user-provided filters first (needed for scope consistency check)
    raw_filters = request.get("filters")
    if isinstance(raw_filters, dict):
        _reject_inline_dimension_filters(catalogue, request_domain, raw_filters)
    if raw_filters and ch_client is not None:
        dimension_filters = resolve_filters(
            raw_filters, catalogue, ch_client, domain=request_domain,
        )
        if not dimension_filters:
            dimension_filters = None

    # Stage 2b: Per-scenario scope enforcement (security)
    # Resolves each scenario's scope, merges with user filters, and
    # verifies all scenarios produce identical effective filters.
    if per_scenario_scope is not None and ch_client is not None:
        from precis_mcp.engine.scope_enforcer import (  # noqa: E402
            enforce_cross_scenario_scope,
        )
        dimension_filters = enforce_cross_scenario_scope(
            per_scenario_scope, dimension_filters, request_domain,
            catalogue, ch_client,
        )

    # ------------------------------------------------------------------
    # Stage 2b: Resolve fork modifiers (requires scenario metadata)
    # ------------------------------------------------------------------
    if ch_client is not None:
        from precis_mcp.engine.scenario_store import ScenarioStore

        scenario_store = ScenarioStore(ch_client)
        for dq in plan.data_queries:
            if "fork" in dq.modifiers:
                fork_target = dq.modifiers["fork"]
                if fork_target:
                    # Explicit fork target: use as scenario_id directly
                    dq.scenario_id = fork_target
                else:
                    parent = scenario_store.get_variant_parent(dq.scenario_id)
                    if parent:
                        dq.scenario_id = parent
                    else:
                        logger.warning(
                            "fork modifier on %r but no variant_of found; "
                            "using original scenario_id",
                            dq.scenario_id,
                        )

    # ------------------------------------------------------------------
    # Stage 3: Retrieve data
    # ------------------------------------------------------------------
    # Build a retriever-compatible ExecutionPlan (simpler type).
    retriever_queries = [_to_retriever_query(dq) for dq in plan.data_queries]
    retriever_plan = retriever.ExecutionPlan(
        data_queries=retriever_queries,
        dimensions=plan.dimensions,
        grains=plan.grains,
    )

    if ch_client is not None:
        retrieve_kwargs = {"dimension_filters": dimension_filters}
        if ibis_backends is not None:
            retrieve_kwargs["ibis_backends"] = ibis_backends
        raw_results: retriever.RawResults = retriever.retrieve(
            retriever_plan,
            catalogue,
            ch_client,
            **retrieve_kwargs,
        )
        # Map retriever results back using resolver's scenario keys.
        # retriever.retrieve keys by dq.scenario_key which we preserved,
        # so no remapping needed.
    else:
        # Dry-run / test mode: return empty results for every data query
        raw_results = {}
        for dq in plan.data_queries:
            raw_results[dq.scenario_key] = {}

    # ------------------------------------------------------------------
    # Stage 4: Transform — derived metrics + computed scenarios
    # ------------------------------------------------------------------
    transformed: transformer.ResultData = transformer.transform(
        raw_results,
        catalogue,
        plan.computed_evals,
        plan.all_metric_keys,
    )

    # ------------------------------------------------------------------
    # Stage 4b: Build dimension display/sort lookups
    # ------------------------------------------------------------------
    dim_formats = _build_dim_formats(catalogue, plan.dimensions, ch_client)

    # ------------------------------------------------------------------
    # Stage 5: Format → response dict
    # ------------------------------------------------------------------
    formatter_blocks = [_to_formatter_block(b, catalogue) for b in plan.blocks]

    # Extract presentation options from request context
    context = request.get("context", {})
    scale = int(context.get("scale", 0))
    raw_decimals = context.get("decimals")
    decimals_override: int | None = int(raw_decimals) if raw_decimals is not None else None

    response = formatter.format_response(
        results=transformed,
        blocks=formatter_blocks,
        catalogue=catalogue,
        dimensions=plan.dimensions,
        period_start=plan.period_start,
        period_end=plan.period_end,
        dim_formats=dim_formats,
        scale=scale,
        decimals=decimals_override,
    )

    return response
