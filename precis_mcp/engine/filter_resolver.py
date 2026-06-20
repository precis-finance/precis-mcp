# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Resolve dimension filters to leaf-level IDs for WHERE clauses.

Takes a ``filters`` dict from the MCP tool API, looks up each key in the
catalogue (dimension key — leaf, derived, or ragged), queries the source data
to resolve to leaf values, and returns a dict keyed by source-view column
names ready for the retriever's WHERE clause generation.

No knowledge of scenarios or periods — those are handled by the resolver.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from precis_mcp.engine.catalogue import (
    Catalogue,
    Dimension,
)

logger = logging.getLogger(__name__)


class FilterResolutionError(Exception):
    """Raised when a filter key cannot be resolved."""


# ---------------------------------------------------------------------------
# Lookup result — what a filter key resolves to
# ---------------------------------------------------------------------------

@dataclass
class _FilterTarget:
    """Internal: describes what a filter key maps to."""
    dimension: Dimension
    resolution_type: str    # 'leaf', 'derived', or 'ragged'


# ---------------------------------------------------------------------------
# Catalogue lookup
# ---------------------------------------------------------------------------

def _find_filter_target(
    filter_key: str,
    catalogue: Catalogue,
) -> _FilterTarget:
    """Identify which dimension and resolution strategy a filter key maps to.

    Every dimension key (leaf, derived, or ragged) is a valid filter key.

    Raises FilterResolutionError if the key matches nothing.
    """
    dim = catalogue.dimensions.get(filter_key)
    if dim is None:
        valid_keys = sorted(catalogue.dimensions.keys())
        raise FilterResolutionError(
            f"Unknown filter key {filter_key!r}. "
            f"Valid filter keys: {valid_keys}"
        )

    if dim.is_leaf:
        return _FilterTarget(dimension=dim, resolution_type="leaf")
    elif dim.is_derived:
        return _FilterTarget(dimension=dim, resolution_type="derived")
    elif dim.is_ragged:
        return _FilterTarget(dimension=dim, resolution_type="ragged")
    else:
        raise FilterResolutionError(
            f"Dimension {filter_key!r} has no resolution strategy "
            f"(not leaf, derived, or ragged)"
        )


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_leaf_filter(
    dim: Dimension,
    value: str,
    ch_client,
) -> list[str]:
    """Resolve a leaf dimension filter — verify the member exists.

    For leaf dimensions the value IS the member ID, so we just verify it
    exists in the source table.  Returns [value] if found, empty list if not.
    """
    assert dim.source is not None
    result = ch_client.query(
        f"SELECT toString({dim.source.key_column}) FROM {dim.source.table} "
        f"WHERE toString({dim.source.key_column}) = {{val:String}} LIMIT 1",
        parameters={"val": value},
    )
    return [row[0] for row in result.result_rows]


def _resolve_derived_filter(
    dim: Dimension,
    value: str,
    ch_client,
) -> list[str]:
    """Resolve a derived dimension filter to leaf IDs.

    Uses the pre-computed transitive resolution to find the source table
    and filter column, then queries for matching leaf IDs.
    """
    # The transitive map on a derived dimension tells us how to reach
    # each leaf dimension it's related to
    if not dim._transitive:
        raise FilterResolutionError(
            f"Derived dimension {dim.key!r} has no transitive resolution "
            f"(parent chain may not connect to any leaf dimension)"
        )

    # Use the first transitive resolution (typically there's only one)
    resolution = next(iter(dim._transitive.values()))
    result = ch_client.query(
        f"SELECT DISTINCT toString({resolution.leaf_key_column}) "
        f"FROM {resolution.source_table} "
        f"WHERE toString({resolution.filter_column}) = {{val:String}}",
        parameters={"val": value},
    )
    return [row[0] for row in result.result_rows]


def _resolve_ragged_filter(
    dim: Dimension,
    value: str,
    ch_client,
) -> list[str]:
    """Resolve a ragged hierarchy filter using the materialised rollup view.

    The rollup view maps node_id → leaf_column.
    Convention: semantic.dim_{leaf_dim}_{ragged_key}_rollup
    """
    if not dim.leaf_dimension:
        raise FilterResolutionError(
            f"Ragged dimension {dim.key!r} has no leaf_dimension"
        )

    # Determine view name
    if dim.ragged_source and dim.ragged_source.type == "provided" and dim.ragged_source.table:
        view = dim.ragged_source.table
    else:
        view = f"semantic.dim_{dim.leaf_dimension}_{dim.key}_rollup"

    # Get the leaf key column from transitive resolution
    resolution = dim._transitive.get(dim.leaf_dimension)
    if not resolution:
        raise FilterResolutionError(
            f"Ragged dimension {dim.key!r} has no transitive resolution "
            f"for leaf_dimension {dim.leaf_dimension!r}"
        )

    result = ch_client.query(
        f"SELECT toString({resolution.leaf_key_column}) FROM {view} "
        f"WHERE node_id = {{node_id:String}}",
        parameters={"node_id": value},
    )
    return [row[0] for row in result.result_rows]


# ---------------------------------------------------------------------------
# View column mapping
# ---------------------------------------------------------------------------

def _map_to_view_column(
    dim: Dimension,
    catalogue: Catalogue,
    domain: str,
) -> str:
    """Find the source-view column name for a dimension in a given domain.

    For leaf dimensions: looks up the CubeDimension whose ``key``
    matches ``dim.key`` in the specified domain and returns its ``source``
    (the physical view column).
    For derived dimensions: resolves to the leaf dimension's view column.
    For ragged dimensions: resolves to the leaf dimension's view column.
    """
    # Determine which leaf dimension to look up in the domain
    if dim.is_leaf:
        lookup_key = dim.key
    elif dim.is_derived or dim.is_ragged:
        # Find the leaf dimension from transitive resolution
        if dim._transitive:
            lookup_key = next(iter(dim._transitive.values())).leaf_dimension
        else:
            lookup_key = dim.leaf_dimension if dim.is_ragged else dim.key
    else:
        lookup_key = dim.key

    domain_cat = catalogue.domains.get(domain)
    if domain_cat:
        for cd in domain_cat.dimensions:
            if cd.key == lookup_key:
                return cd.source

    # Fallback: use the leaf dimension's key_column
    leaf_dim = catalogue.dimensions.get(lookup_key)
    if leaf_dim and leaf_dim.source:
        return leaf_dim.source.key_column
    return lookup_key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_single_value(
    filter_key: str,
    filter_value: str,
    catalogue: Catalogue,
    ch_client,
) -> tuple[str, list[str]]:
    """Resolve one filter key/value pair to leaf dimension key + leaf IDs.

    Unlike ``resolve_filters``, this does NOT map to view columns.
    It returns the catalogue-level leaf dimension key so the caller can
    decide how to use the results (e.g. for lock resolution, scope checks).

    Returns:
        (leaf_dimension_key, leaf_ids)

    Raises:
        FilterResolutionError: When the key is unknown or resolves to nothing.
    """
    target = _find_filter_target(filter_key, catalogue)
    dim = target.dimension

    if target.resolution_type == "leaf":
        leaves = _resolve_leaf_filter(dim, filter_value, ch_client)
        leaf_dim_key = dim.key
    elif target.resolution_type == "derived":
        leaves = _resolve_derived_filter(dim, filter_value, ch_client)
        leaf_dim_key = next(iter(dim._transitive.values())).leaf_dimension
    elif target.resolution_type == "ragged":
        leaves = _resolve_ragged_filter(dim, filter_value, ch_client)
        leaf_dim_key = dim.leaf_dimension
    else:
        raise FilterResolutionError(
            f"Unknown resolution type {target.resolution_type!r}"
        )

    if not leaves:
        raise FilterResolutionError(
            f"Filter {filter_key!r} = {filter_value!r} resolved to zero records."
        )

    return leaf_dim_key, leaves


def resolve_filters(
    filters: dict[str, str],
    catalogue: Catalogue,
    ch_client,
    domain: str = "pnl",
) -> dict[str, list[str]]:
    """Resolve dimension filters to leaf-level IDs for WHERE clauses.

    Args:
        filters:    Dict of filter_key → filter_value from the MCP tool API.
                    Keys are dimension keys (leaf, derived, or ragged).
        catalogue:  Loaded Catalogue instance.
        ch_client:  ClickHouse client for resolution queries.
        domain:     Domain name for mapping dimension → source view column.
                    Defaults to ``"pnl"``.

    Returns:
        Dict of source-view column name → list of leaf IDs.
        E.g. ``{"cost_centre": ["CC-CLOUD-01", "CC-CLOUD-02"]}``

        When multiple filter keys resolve to the same leaf dimension,
        their leaf sets are intersected.

    Raises:
        FilterResolutionError: When a filter key is unknown or resolves to
            zero records.
    """
    if not filters:
        return {}

    # Accumulate leaf sets per leaf dimension key
    leaf_sets: dict[str, list[set[str]]] = {}
    # Track which dimension object to use for view column mapping
    dim_for_mapping: dict[str, Dimension] = {}

    for filter_key, filter_value in filters.items():
        target = _find_filter_target(filter_key, catalogue)
        dim = target.dimension

        if target.resolution_type == "leaf":
            leaves = _resolve_leaf_filter(dim, filter_value, ch_client)
            leaf_dim_key = dim.key
        elif target.resolution_type == "derived":
            leaves = _resolve_derived_filter(dim, filter_value, ch_client)
            # Get the leaf dimension key from transitive
            leaf_dim_key = next(iter(dim._transitive.values())).leaf_dimension
        elif target.resolution_type == "ragged":
            leaves = _resolve_ragged_filter(dim, filter_value, ch_client)
            leaf_dim_key = dim.leaf_dimension
        else:
            raise FilterResolutionError(
                f"Unknown resolution type {target.resolution_type!r}"
            )

        if not leaves:
            if target.resolution_type == "ragged":
                raise FilterResolutionError(
                    f"Filter {filter_key!r} = {filter_value!r} resolved to zero records. "
                    f"The value was not found in the {dim.label} hierarchy. "
                    f"Use search_hierarchy to discover valid node IDs and "
                    f"pass the exact node_id returned (some verticals prefix node_ids, e.g. 'dept:...', 'div:...')."
                )
            else:
                raise FilterResolutionError(
                    f"Filter {filter_key!r} = {filter_value!r} resolved to zero records. "
                    f"No matching {dim.label} found. "
                    f"Use search_hierarchy to discover valid values."
                )

        if leaf_dim_key not in dim_for_mapping:
            dim_for_mapping[leaf_dim_key] = dim
        if leaf_dim_key not in leaf_sets:
            leaf_sets[leaf_dim_key] = []
        leaf_sets[leaf_dim_key].append(set(leaves))

    # Intersect leaf sets per leaf dimension, map to view column names
    result: dict[str, list[str]] = {}
    for leaf_dim_key, sets in leaf_sets.items():
        if not sets:
            continue
        intersection = sets[0]
        for s in sets[1:]:
            intersection &= s
        # Use the first filter's dimension for mapping (they all resolve
        # to the same leaf dimension)
        mapping_dim = dim_for_mapping[leaf_dim_key]
        view_col = _map_to_view_column(mapping_dim, catalogue, domain)
        result[view_col] = sorted(intersection)

    return result
