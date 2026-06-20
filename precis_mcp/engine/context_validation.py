# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Validation utilities for report context and call-time filter checking.

Two use cases:
  1. **Context validation** — run once at conversation start to validate the
     user's saved report_context against live master data (catalogue + ClickHouse).
  2. **Call-time filter validation** — run before each report tool execution to
     check that filters are relevant to the cubes being queried.

Both share the same underlying check functions. The filter resolver
(``filter_resolver.py``) handles *resolution* (filter → leaf IDs). This module
handles *validation* (does this filter make sense for this report?).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from precis_mcp.engine.catalogue import (
    BaseMetric,
    Catalogue,
    DerivedMetric,
    resolve_statement,
)
from precis_mcp.engine.filter_resolver import (
    FilterResolutionError,
    _find_filter_target,
)
from precis_mcp.engine.scenario_registry import ScenarioRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """A single validation issue found during context or filter checking."""
    field: str          # e.g. "filters", "statement", "scenarios"
    severity: str       # "error" or "warning"
    detail: str         # human-readable description
    dim_key: str = ""   # for filter issues: the dimension key
    value: str = ""     # for filter issues: the filter value


@dataclass
class ContextValidationResult:
    """Result of validating a report_context against master data."""
    valid_context: dict
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "warning" for i in self.issues)


@dataclass
class FilterValidationResult:
    """Result of validating filters for a specific report tool call."""
    cleaned_filters: dict
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DimensionValidationResult:
    """Result of validating dimensions for a report tool call."""
    cleaned_dimensions: list[str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DimensionMapResult:
    """Result of validating a dimension-map arg (filters, commit_scope, ...).

    A dimension-map arg is a ``{dim_key: value_or_list}`` dict whose keys
    must (a) exist in the catalogue and (b) be relevant to the tool's
    target (report domains, plan datasets, etc.).
    """
    cleaned: dict
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_domains_for_statement(
    statement_key: str,
    catalogue: Catalogue,
) -> set[str]:
    """Resolve a statement key to the set of domains it spans.

    Expands concat statements recursively, resolves each metric key to its
    domain (following derived metric formulas to base metrics).
    """
    try:
        metric_keys = resolve_statement(catalogue, statement_key)
    except Exception:
        return set()

    domains: set[str] = set()
    for key in metric_keys:
        if key == "separator":
            continue
        domain = _resolve_metric_domain(key, catalogue)
        if domain:
            domains.add(domain)
    return domains


def _resolve_metric_domain(
    metric_key: str,
    catalogue: Catalogue,
) -> str | None:
    """Resolve a metric key to its base domain, following derived formulas."""
    from precis_mcp.engine.catalogue import _metric_refs

    # Defensive: callers occasionally pass non-string metric keys when the
    # LLM wraps the arg as [{"metric": "X"}]. The pre-flight pipeline
    # normalises this for tool calls, but other entry paths exist
    # (report_context, tests, future callers). Returning None here matches
    # the "unknown metric" contract and avoids an unhashable-dict crash.
    if not isinstance(metric_key, str):
        return None

    visited: set[str] = set()
    to_check: list[str] = [metric_key]
    while to_check:
        key = to_check.pop()
        if key in visited:
            continue
        visited.add(key)
        metric = catalogue.metrics.get(key)
        if metric is None:
            continue
        if isinstance(metric, BaseMetric):
            return metric.domain
        if isinstance(metric, DerivedMetric):
            to_check.extend(_metric_refs(metric.formula))
    return None


def _get_domains_for_tool_call(
    tool_name: str,
    tool_args: dict,
    catalogue: Catalogue,
) -> set[str]:
    """Determine which domains a report tool call will query.

    For run_statement: resolve the statement to its constituent domains.
    For run_metric: resolve each metric key to its domain.
    """
    if tool_name == "run_statement":
        statement = tool_args.get("statement", "pnl")
        return _resolve_domains_for_statement(statement, catalogue)

    elif tool_name == "run_metric":
        metrics = tool_args.get("metrics", [])
        domains: set[str] = set()
        for key in metrics:
            domain = _resolve_metric_domain(key, catalogue)
            if domain:
                domains.add(domain)
        return domains

    return set()


def _get_valid_filter_keys_for_domains(
    domains: set[str],
    catalogue: Catalogue,
) -> set[str]:
    """Get the set of valid filter keys for a set of domains.

    A filter key is valid if the dimension it references is reachable from
    a CubeDimension bound to at least one of the specified domains.

    Returns the union of:
    - Leaf dimension keys bound to these domains (direct filter)
    - All ancestor dimension keys reachable via parent chains
    - Ragged hierarchy dimension keys that resolve to bound leaf dimensions
    """
    valid_keys: set[str] = set()

    # Collect which catalogue dimensions are bound to these domains. Inline
    # axes have no master data and are not filterable, so they are excluded.
    bound_leaf_keys: set[str] = set()
    for domain_name in domains:
        domain_cat = catalogue.domains.get(domain_name)
        if not domain_cat:
            continue
        for cd in domain_cat.dimensions:
            if cd.source_inline:
                continue
            bound_leaf_keys.add(cd.key)

    # Walk parent chains to collect all reachable dimensions
    def _walk_parents(dim_key: str, visited: set[str]) -> None:
        if dim_key in visited:
            return
        visited.add(dim_key)
        valid_keys.add(dim_key)
        dim = catalogue.dimensions.get(dim_key)
        if dim:
            for parent_key in dim.parents:
                _walk_parents(parent_key, visited)

    visited: set[str] = set()
    for leaf_key in bound_leaf_keys:
        _walk_parents(leaf_key, visited)

    # Add ragged hierarchy dimensions that resolve to bound leaf dimensions
    for dim_key, dim in catalogue.dimensions.items():
        if dim.is_ragged and dim.leaf_dimension in bound_leaf_keys:
            valid_keys.add(dim_key)

    return valid_keys


# ---------------------------------------------------------------------------
# Context validation (conversation start)
# ---------------------------------------------------------------------------

def validate_report_context(
    report_context: dict,
    catalogue: Catalogue,
    scenario_registry: ScenarioRegistry | None = None,
) -> ContextValidationResult:
    """Validate report_context fields against the catalogue.

    Checks that don't require ClickHouse (fast, synchronous):
    - Filter keys exist as valid dimension filter keys in the catalogue
    - Statement key exists in the catalogue
    - Scenario keys exist in the semantic scenario registry when provided,
      otherwise in the catalogue fallback

    Filter *value* validation (does the value exist in master data?) requires
    ClickHouse and is deferred to call-time validation or can be done
    separately with ``validate_filter_values()``.

    Args:
        report_context: The user's report_context dict from their profile.
        catalogue: Loaded Catalogue instance.

    Returns:
        ContextValidationResult with valid_context and any issues found.
    """
    issues: list[ValidationIssue] = []
    ctx = dict(report_context)

    # 1. Validate filter keys exist in catalogue
    filters = ctx.get("filters", {})
    if filters:
        valid_filters = {}
        for dim_key, value in filters.items():
            try:
                _find_filter_target(dim_key, catalogue)
                valid_filters[dim_key] = value
            except FilterResolutionError:
                issues.append(ValidationIssue(
                    field="filters",
                    severity="error",
                    detail=(
                        f"Filter key '{dim_key}' is not a recognised dimension "
                        f"filter in the catalogue. The saved default "
                        f"'{dim_key}={value}' has been removed."
                    ),
                    dim_key=dim_key,
                    value=str(value),
                ))
        ctx["filters"] = valid_filters

    # 2. Validate statement key
    statement = ctx.get("statement")
    if statement and statement not in catalogue.statements:
        issues.append(ValidationIssue(
            field="statement",
            severity="error",
            detail=(
                f"Statement '{statement}' not found in the catalogue. "
                f"Available: {', '.join(sorted(catalogue.statements.keys()))}."
            ),
        ))
        del ctx["statement"]

    # 3. Validate scenario keys
    scenarios = ctx.get("scenarios", [])
    if scenarios:
        valid_scenarios = []
        for s in scenarios:
            scenario_key = s.get("scenario", "") if isinstance(s, dict) else ""
            scenario_ok = False
            normalized = scenario_key
            if scenario_registry is not None:
                try:
                    normalized = scenario_registry.normalize_key(scenario_key)
                    scenario_ok = True
                except Exception:
                    scenario_ok = False

            if scenario_ok:
                if isinstance(s, dict) and normalized != scenario_key:
                    s = {**s, "scenario": normalized}
                valid_scenarios.append(s)
            else:
                issues.append(ValidationIssue(
                    field="scenarios",
                    severity="warning",
                    detail=(
                        f"Scenario '{scenario_key}' not found in the scenario registry. "
                        f"Removed from defaults."
                    ),
                ))
        if valid_scenarios != scenarios:
            ctx["scenarios"] = valid_scenarios

    return ContextValidationResult(valid_context=ctx, issues=issues)


def validate_filter_values(
    filters: dict,
    catalogue: Catalogue,
    ch_client,
) -> list[ValidationIssue]:
    """Validate that filter values exist in master data (requires ClickHouse).

    For each filter key-value pair, resolves to leaf IDs using the filter
    resolver. If resolution returns zero results, the value doesn't exist.

    Args:
        filters: Filter dict from report_context.
        catalogue: Loaded Catalogue instance.
        ch_client: ClickHouse client.

    Returns:
        List of ValidationIssue for invalid values.
    """
    from precis_mcp.engine.filter_resolver import resolve_filters

    issues: list[ValidationIssue] = []

    for dim_key, value in filters.items():
        try:
            # Resolve this single filter — will raise if invalid key or zero results
            resolve_filters({dim_key: str(value)}, catalogue, ch_client)
        except FilterResolutionError as e:
            issues.append(ValidationIssue(
                field="filters",
                severity="error",
                detail=(
                    f"Filter '{dim_key}={value}' is invalid: {e}. "
                    f"Use search_hierarchy('{value}') to find valid values."
                ),
                dim_key=dim_key,
                value=str(value),
            ))
        except Exception as e:
            # Generic exceptions (e.g. driver errors) can embed SQL — keep
            # the detail in the log; the FilterResolutionError branch above
            # carries the engine-authored, caller-safe guidance.
            logger.warning("Filter value validation failed for %s=%s: %s", dim_key, value, e)
            issues.append(ValidationIssue(
                field="filters",
                severity="warning",
                detail=(
                    f"Could not validate filter '{dim_key}={value}' "
                    f"(internal {type(e).__name__}; details were logged)"
                ),
                dim_key=dim_key,
                value=str(value),
            ))

    return issues


# ---------------------------------------------------------------------------
# Call-time filter validation (per tool call)
# ---------------------------------------------------------------------------

def validate_filters_for_report(
    filters: dict,
    tool_name: str,
    tool_args: dict,
    catalogue: Catalogue,
) -> FilterValidationResult:
    """Validate filters against the cubes that will be queried by this report.

    Checks (deterministic, no ClickHouse needed):
    1. Is each filter dimension relevant to ANY cube in this report?
       If not → warning, remove from filters for this call.
    2. Is each filter key valid in the catalogue at all?
       If not → error.

    Value existence checks (does the value exist in master data) are NOT done
    here — they are handled by the filter resolver at execution time, which
    already raises FilterResolutionError with helpful messages.

    Args:
        filters: Merged filters (context defaults + agent overrides).
        tool_name: "run_statement" or "run_metric".
        tool_args: Full tool args (needed to resolve statement/metrics).
        catalogue: Loaded Catalogue instance.

    Returns:
        FilterValidationResult with cleaned filters, warnings, and errors.
    """
    if not filters:
        return FilterValidationResult(cleaned_filters={})

    warnings: list[str] = []
    errors: list[str] = []
    cleaned = dict(filters)

    # Determine which domains this report will query
    domains = _get_domains_for_tool_call(tool_name, tool_args, catalogue)
    if not domains:
        # Can't determine domains — pass filters through unchanged
        return FilterValidationResult(cleaned_filters=cleaned)

    # Get the set of filter keys that are relevant to these domains
    valid_keys = _get_valid_filter_keys_for_domains(domains, catalogue)

    for dim_key, value in list(filters.items()):
        # Check 1: Is this filter key valid in the catalogue at all?
        try:
            _find_filter_target(dim_key, catalogue)
        except FilterResolutionError:
            errors.append(
                f"Unknown filter key '{dim_key}'. "
                f"Use search_hierarchy() to find valid filter keys."
            )
            del cleaned[dim_key]
            continue

        # Check 2: Is this filter key relevant to the cubes in this report?
        if dim_key not in valid_keys:
            # Build a helpful message about which domains are involved
            domain_list = ", ".join(sorted(domains))
            warnings.append(
                f"Filter '{dim_key}={value}' does not apply to this report "
                f"(queried domains: {domain_list}). "
                f"Filter removed for this call."
            )
            del cleaned[dim_key]

    return FilterValidationResult(
        cleaned_filters=cleaned,
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Dimension validation (call-time)
# ---------------------------------------------------------------------------

def _get_valid_dimension_keys_for_domains(
    domains: set[str],
    catalogue: Catalogue,
) -> set[str]:
    """Get the set of valid dimension column keys for GROUP BY in a report.

    A dimension key is valid if it is:
    - A CubeDimension key in at least one of the specified domains
    - A derived dimension whose leaf is bound to these domains
      (e.g. 'department' or 'quarter' for GROUP BY)
    Returns the union of all valid keys.
    """
    valid_keys: set[str] = set()

    # Collect cube dimension keys and bound leaf dimension keys
    bound_leaf_keys: set[str] = set()
    for domain_name in domains:
        domain_cat = catalogue.domains.get(domain_name)
        if not domain_cat:
            continue
        for cd in domain_cat.dimensions:
            valid_keys.add(cd.key)
            if not cd.source_inline:
                bound_leaf_keys.add(cd.key)

    # Walk parent chains: derived dimensions whose leaf is bound are valid
    def _walk_parents(dim_key: str, visited: set[str]) -> None:
        if dim_key in visited:
            return
        visited.add(dim_key)
        dim = catalogue.dimensions.get(dim_key)
        if dim:
            for parent_key in dim.parents:
                valid_keys.add(parent_key)
                _walk_parents(parent_key, visited)

    visited: set[str] = set()
    for leaf_key in bound_leaf_keys:
        _walk_parents(leaf_key, visited)

    return valid_keys


def validate_dimensions_for_report(
    dimensions: list,
    tool_name: str,
    tool_args: dict,
    catalogue: Catalogue,
) -> DimensionValidationResult:
    """Validate dimension keys against the cubes that will be queried.

    Checks (deterministic, no ClickHouse needed):
    1. Coerce each entry to a string (handles stray types from LLM).
    2. Is the dimension key valid in ANY domain in the catalogue?
       If not → error with list of valid dimensions.
    3. Is the dimension key relevant to the domains in THIS report?
       If not → warning (partial coverage is OK for multi-domain statements).

    Args:
        dimensions: Raw dimensions list (may contain non-strings from LLM).
        tool_name: "run_statement" or "run_metric".
        tool_args: Full tool args (needed to resolve statement/metrics).
        catalogue: Loaded Catalogue instance.

    Returns:
        DimensionValidationResult with cleaned list, warnings, and errors.
    """
    if not dimensions:
        return DimensionValidationResult(cleaned_dimensions=[])

    warnings: list[str] = []
    errors: list[str] = []
    cleaned: list[str] = []

    # Determine which domains this report will query
    domains = _get_domains_for_tool_call(tool_name, tool_args, catalogue)

    # Build the valid sets
    report_valid = (
        _get_valid_dimension_keys_for_domains(domains, catalogue)
        if domains else set()
    )
    all_valid = _get_valid_dimension_keys_for_domains(
        set(catalogue.domains.keys()), catalogue,
    )

    for raw_dim in dimensions:
        dim = str(raw_dim).strip().lower()
        if not dim:
            continue

        if dim in report_valid:
            cleaned.append(dim)
        elif dim in all_valid:
            # Valid dimension but not in this report's domains
            domain_list = ", ".join(sorted(domains)) if domains else "unknown"
            warnings.append(
                f"Dimension '{dim}' is not available in this report's domains "
                f"({domain_list}) — partial results may occur."
            )
            cleaned.append(dim)  # allow through (multi-domain soft mode)
        else:
            # Not valid in any domain
            hint_parts = sorted(all_valid)
            errors.append(
                f"Unknown dimension '{dim}'. "
                f"Valid dimensions: {', '.join(hint_parts)}. "
                f"Use 'period' for period breakdown, or 'quarter'/'fiscal_year' for higher-level grouping."
            )

    return DimensionValidationResult(
        cleaned_dimensions=cleaned,
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Generic dimension-map validation (filters, commit_scope, ...)
# ---------------------------------------------------------------------------

def _get_valid_keys_for_plan_datasets(catalogue: Catalogue) -> set[str]:
    """Get valid dimension keys across all plan datasets.

    A key is valid if it is a ``PlanDatasetDimension.key`` on any dataset,
    the referenced master dimension, or an ancestor of that master
    dimension via parent chains.
    """
    valid_keys: set[str] = set()
    bound_leaf_keys: set[str] = set()

    for ds in catalogue.plan_datasets.values():
        for pds_dim in ds.dimensions:
            valid_keys.add(pds_dim.key)
            if pds_dim.source:
                bound_leaf_keys.add(pds_dim.source)

    def _walk_parents(dim_key: str, visited: set[str]) -> None:
        if dim_key in visited:
            return
        visited.add(dim_key)
        valid_keys.add(dim_key)
        dim = catalogue.dimensions.get(dim_key)
        if dim:
            for parent_key in dim.parents:
                _walk_parents(parent_key, visited)

    visited: set[str] = set()
    for leaf_key in bound_leaf_keys:
        _walk_parents(leaf_key, visited)

    return valid_keys


def _resolve_report_domain_keys(
    catalogue: Catalogue, tool_name: str, tool_args: dict,
) -> set[str]:
    domains = _get_domains_for_tool_call(tool_name, tool_args, catalogue)
    return _get_valid_filter_keys_for_domains(domains, catalogue)


# Target-resolver registry.  Maps a descriptor's dimension_map_args value
# (e.g. "report_domains", "plan_datasets") to a callable that returns the
# valid-keys set for the current tool call.
_TARGET_RESOLVERS: dict = {
    "report_domains": _resolve_report_domain_keys,
    "plan_datasets": lambda cat, tool_name, tool_args: (
        _get_valid_keys_for_plan_datasets(cat)
    ),
}


def validate_dimension_map(
    m: dict,
    target_key: str,
    tool_name: str,
    tool_args: dict,
    catalogue: Catalogue,
) -> DimensionMapResult:
    """Validate a dimension-map arg against the catalogue and a target.

    Performs two checks per key:
      1. Schema — key exists in the catalogue (resolvable via
         ``_find_filter_target``).
      2. Relevance — key belongs to the target's valid-keys set (report
         domains, plan datasets, ...).

    Unknown target_key is a programmer error and raises.

    Args:
        m:          The dimension-map dict (e.g. filters, commit_scope).
        target_key: Target-resolver key from ``ToolDescriptor.dimension_map_args``.
        tool_name:  Tool name (for resolver context).
        tool_args:  Full tool args (for resolvers that inspect them).
        catalogue:  Loaded Catalogue.

    Returns:
        DimensionMapResult with ``cleaned`` (irrelevant keys dropped),
        warnings (relevance drops), and errors (schema failures).
    """
    if not m:
        return DimensionMapResult(cleaned={})

    resolver = _TARGET_RESOLVERS.get(target_key)
    if resolver is None:
        raise ValueError(f"Unknown dimension-map target: {target_key!r}")

    valid_keys = resolver(catalogue, tool_name, tool_args)

    cleaned = dict(m)
    warnings: list[str] = []
    errors: list[str] = []

    for dim_key, value in list(m.items()):
        # Schema check
        try:
            _find_filter_target(dim_key, catalogue)
        except FilterResolutionError:
            errors.append(
                f"Unknown dimension key '{dim_key}'. "
                f"Use search_hierarchy() to find valid keys."
            )
            del cleaned[dim_key]
            continue

        # Relevance check
        if dim_key not in valid_keys:
            warnings.append(
                f"Dimension '{dim_key}={value}' is not applicable here; "
                f"removed for this call."
            )
            del cleaned[dim_key]

    return DimensionMapResult(cleaned=cleaned, warnings=warnings, errors=errors)
