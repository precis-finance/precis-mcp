# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from precis_mcp.engine.catalogue import (
    BaseMetric,
    Catalogue,
    DerivedMetric,
    _metric_refs,
    resolve_statement,
)
from precis_mcp.engine.scenario_registry import (
    RealScenarioRef,
    ScenarioRef,
    ScenarioRegistry,
    ShiftedScenarioRef,
    VariancePctScenarioRef,
    VarianceScenarioRef,
)


# ---------------------------------------------------------------------------
# Execution plan data classes
# ---------------------------------------------------------------------------

@dataclass
class ResolvedBlock:
    """A block from the report request, with model references resolved."""
    alias: str                      # display label for this column group
    scenario_key: str               # scenario alias, optionally with modifiers
    metric_keys: list[str]          # resolved metric keys (statements expanded, no 'separator')
    display_items: list[str]        # metric keys + 'separator' entries, preserving order
    is_statement: bool = False      # True only for 'statement:' models (drives layout kind)
    display_format: str = ""
    color_code: bool = False


@dataclass
class DataQuery:
    """A query to execute against ClickHouse."""
    scenario_key: str               # scenario alias/key (e.g. 'actuals', 'actuals_py')
    scenario_id: str                # ClickHouse scenario value (e.g. 'ACTUALS')
    period_start: str               # YYYY-MM
    period_end: str                 # YYYY-MM
    metric_keys: list[str]          # base metric keys needed from this query
    domain: str = "pnl"            # catalogue domain
    modifiers: dict[str, str] = field(default_factory=dict)  # e.g. {"uncommitted": "", "commit": "abc123"} (inferred from base metrics)
    time_offset: int = 0            # shifted scenario offset in months (e.g. -12 for prior_year)


@dataclass
class ComputedScenarioEval:
    """A computed scenario to evaluate after data queries."""
    scenario_key: str               # e.g. 'actuals_vs_budget'
    formula: str                    # e.g. 'actuals - budget'
    dependencies: list[str]         # scenario keys this depends on


@dataclass(frozen=True)
class GrainSpec:
    """Which aggregation grains the engine emits for a dimensioned request.

    detail      — one row per full dimension combination.
    subtotals   — right-to-left dimension-prefix subtotals.
    grand_total — the single fully-aggregated row.
    """
    detail: bool = True
    subtotals: bool = False
    grand_total: bool = False


@dataclass
class ExecutionPlan:
    """Complete plan for executing a report request."""
    blocks: list[ResolvedBlock]
    data_queries: list[DataQuery]
    computed_evals: list[ComputedScenarioEval]  # topologically sorted
    dimensions: list[str]           # e.g. ['period'], ['cost_centre'], []
    period_start: str               # from request context
    period_end: str                 # from request context
    all_metric_keys: list[str]      # deduplicated, all metrics needed across all blocks
    all_base_metric_keys: list[str] # only base metrics (need SQL queries)
    all_derived_metric_keys: list[str]  # only derived metrics (computed in-memory)
    query_mode: str = "aggregate"   # always 'aggregate'
    grains: GrainSpec = field(default_factory=GrainSpec)


class ResolverError(Exception):
    """Raised when request validation or resolution fails."""
    pass


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def _validate_period_format(period: str, field_name: str) -> None:
    if not _PERIOD_RE.match(period):
        raise ResolverError(
            f"Invalid period format for {field_name!r}: {period!r}. Expected YYYY-MM."
        )


def shift_period(period: str, months: int) -> str:
    """Shift YYYY-MM by N months. E.g. shift_period('2025-06', -12) -> '2024-06'"""
    year, month = int(period[:4]), int(period[5:7])
    total_months = year * 12 + (month - 1) + months
    new_year, new_month = divmod(total_months, 12)
    return f"{new_year:04d}-{new_month + 1:02d}"


# ---------------------------------------------------------------------------
# Scenario modifier parser
# ---------------------------------------------------------------------------

# Valid modifier keys (whitelist to prevent injection via crafted strings)
_VALID_MODIFIERS: frozenset[str] = frozenset({
    "uncommitted",          # include committed + uncommitted (no commit_id filter)
    "uncommitted_delta",    # only uncommitted changes
    "commit",               # time-travel: state as of a specific commit
    "commit_delta",         # changes in a single commit
    "fork",                 # fork modifier
})


def parse_scenario_modifiers(scenario_ref: str) -> tuple[str, dict[str, str]]:
    """Parse scenario modifiers from a scenario reference string.

    Format: ``base_key&modifier1&modifier2=value``

    Returns:
        (base_key, modifiers_dict) — e.g. ("budget", {"uncommitted": ""})

    Raises:
        ResolverError on unknown modifier keys.
    """
    if "&" not in scenario_ref:
        return scenario_ref, {}

    parts = scenario_ref.split("&")
    base_key = parts[0]
    modifiers: dict[str, str] = {}

    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
        else:
            key, value = part, ""

        if key not in _VALID_MODIFIERS:
            raise ResolverError(
                f"Unknown scenario modifier: {key!r}. "
                f"Valid modifiers: {', '.join(sorted(_VALID_MODIFIERS))}"
            )
        modifiers[key] = value

    return base_key, modifiers


def strip_scenario_modifiers(scenario_ref: str) -> str:
    """Return only the base scenario key, stripping any modifiers.

    Useful for access checks and catalogue lookups outside the engine.
    """
    if "&" not in scenario_ref:
        return scenario_ref
    return scenario_ref.split("&", 1)[0]


# ---------------------------------------------------------------------------
# Metric dependency resolution
# ---------------------------------------------------------------------------

def _resolve_metric_dependencies(
    metric_keys: set[str],
    catalogue: Catalogue,
) -> tuple[list[str], list[str], list[str]]:
    """Expand metric keys to include all transitive dependencies.

    Returns:
        (all_keys, base_keys, derived_keys) — each list is topologically sorted
        (dependencies before dependents).
    """
    # BFS/DFS to collect all required metrics
    all_keys: set[str] = set()
    to_visit = list(metric_keys)

    while to_visit:
        key = to_visit.pop()
        if key in all_keys:
            continue
        if key not in catalogue.metrics:
            raise ResolverError(f"Unknown metric key: {key!r}")
        all_keys.add(key)
        metric = catalogue.metrics[key]
        if isinstance(metric, DerivedMetric):
            refs = _metric_refs(metric.formula)
            for ref in refs:
                if ref not in all_keys:
                    to_visit.append(ref)

    # Topological sort using Kahn's algorithm
    dep_graph: dict[str, set[str]] = {}
    for key in all_keys:
        metric = catalogue.metrics[key]
        if isinstance(metric, DerivedMetric):
            deps = _metric_refs(metric.formula) & all_keys
        else:
            deps = set()
        dep_graph[key] = deps

    # in-degree relative to all_keys only
    in_degree: dict[str, int] = {k: 0 for k in all_keys}
    for key, deps in dep_graph.items():
        for dep in deps:
            # dep must come before key, so key has higher in-degree
            in_degree[key] += 1

    queue = [k for k, d in in_degree.items() if d == 0]
    sorted_keys: list[str] = []
    while queue:
        # sort for determinism
        queue.sort()
        node = queue.pop(0)
        sorted_keys.append(node)
        # find nodes that depend on `node`
        for key, deps in dep_graph.items():
            if node in deps:
                in_degree[key] -= 1
                if in_degree[key] == 0:
                    queue.append(key)

    base_keys = [k for k in sorted_keys if isinstance(catalogue.metrics[k], BaseMetric)]
    derived_keys = [k for k in sorted_keys if isinstance(catalogue.metrics[k], DerivedMetric)]

    return sorted_keys, base_keys, derived_keys


def _topological_sort_dependencies(
    computed_keys: list[str],
    dependency_fn,
) -> list[str]:
    """Return computed keys in dependency order using a generic dependency fn."""
    computed_set = set(computed_keys)
    dep_graph: dict[str, set[str]] = {
        key: set(dependency_fn(key)) & computed_set for key in computed_keys
    }
    in_degree: dict[str, int] = {key: 0 for key in computed_keys}
    for key, deps in dep_graph.items():
        for _dep in deps:
            in_degree[key] += 1

    queue = sorted([key for key, degree in in_degree.items() if degree == 0])
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for key, deps in dep_graph.items():
            if node in deps:
                in_degree[key] -= 1
                if in_degree[key] == 0:
                    queue.append(key)
        queue.sort()

    if len(result) != len(computed_keys):
        raise ResolverError("Circular dependency detected in computed scenarios")
    return result


def _resolve_registry_ref(
    scenario_registry: ScenarioRegistry | None,
    key: str,
) -> ScenarioRef | None:
    if scenario_registry is None:
        return None
    try:
        return scenario_registry.resolve_key(key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main resolve function
# ---------------------------------------------------------------------------

def resolve(
    request: dict,
    catalogue: Catalogue,
    scenario_registry: ScenarioRegistry | None = None,
) -> ExecutionPlan:
    """Parse and validate a report request, resolve to an execution plan.

    Args:
        request: Report request dict with keys: filters, dimensions, blocks
        catalogue: Loaded catalogue for metrics/statements/dimensions
        scenario_registry: Semantic scenario registry. Required for scenario
            resolution; catalogue scenario YAML is no longer a fallback.

    Returns:
        ExecutionPlan ready for the retriever

    Raises:
        ResolverError on validation failure
    """
    if scenario_registry is None:
        raise ResolverError("ScenarioRegistry is required for scenario resolution")

    # ------------------------------------------------------------------
    # Stage 1: Request Validation
    # ------------------------------------------------------------------

    # 1a. Validate required top-level fields — support both old and new format
    # New format: context = {period_start, period_end}, filters = {dim_key: value}
    # Old format: filters = {period_start, period_end, bu?, ...}
    context = request.get("context")
    if context is not None and isinstance(context, dict):
        # New format: period info in context
        if "period_start" not in context:
            raise ResolverError("context.period_start is required")
        if "period_end" not in context:
            raise ResolverError("context.period_end is required")
        period_start: str = context["period_start"]
        period_end: str = context["period_end"]
    elif "filters" in request and isinstance(request["filters"], dict):
        # Legacy format: period info in filters
        filters_raw = request["filters"]
        if "period_start" not in filters_raw:
            raise ResolverError("filters.period_start is required")
        if "period_end" not in filters_raw:
            raise ResolverError("filters.period_end is required")
        period_start = filters_raw["period_start"]
        period_end = filters_raw["period_end"]
    else:
        raise ResolverError("Request must have a 'context' dict (or legacy 'filters' dict)")

    # 1b. Validate period format
    _validate_period_format(period_start, "period_start")
    _validate_period_format(period_end, "period_end")

    if period_start > period_end:
        raise ResolverError(
            f"period_start {period_start!r} must be <= period_end {period_end!r}"
        )

    # 1c. Validate blocks
    if "blocks" not in request or not isinstance(request["blocks"], list):
        raise ResolverError("Request must have a 'blocks' list")

    raw_blocks: list[dict] = request["blocks"]
    if len(raw_blocks) == 0:
        raise ResolverError("Request must have at least one block")

    # 1d. Dimensions
    dimensions: list[str] = request.get("dimensions", [])

    # 1e. Parse and validate each block
    resolved_blocks: list[ResolvedBlock] = []

    for i, raw_block in enumerate(raw_blocks):
        if "model" not in raw_block:
            raise ResolverError(f"Block {i} is missing 'model'")
        if "scenario" not in raw_block:
            raise ResolverError(f"Block {i} is missing 'scenario'")

        model_ref: str = raw_block["model"]
        scenario_ref: str = raw_block["scenario"]
        alias: str = raw_block.get("alias", scenario_ref)

        # Parse modifiers from scenario reference (e.g. "budget&uncommitted")
        base_scenario_key, _block_modifiers = parse_scenario_modifiers(scenario_ref)

        registry_ref = _resolve_registry_ref(scenario_registry, base_scenario_key)
        if registry_ref is None:
            raise ResolverError(
                f"Block {i}: unknown scenario {base_scenario_key!r}"
            )

        # Parse model reference
        is_statement = False
        if model_ref.startswith("metric:"):
            metric_key = model_ref[len("metric:"):]
            if metric_key not in catalogue.metrics:
                raise ResolverError(
                    f"Block {i}: unknown metric key {metric_key!r}"
                )
            display_items = [metric_key]
            metric_keys = [metric_key]

        elif model_ref.startswith("metrics:"):
            # Comma-separated multi-metric block. Produces one column per scenario
            # with multiple metric rows — same shape the formatter expects from
            # statement: blocks. Metric keys are snake_case identifiers, so a
            # comma-split is safe.
            keys_str = model_ref[len("metrics:"):]
            metric_keys = [k.strip() for k in keys_str.split(",") if k.strip()]
            if not metric_keys:
                raise ResolverError(
                    f"Block {i}: 'metrics:' requires at least one metric key"
                )
            for mk in metric_keys:
                if mk not in catalogue.metrics:
                    raise ResolverError(
                        f"Block {i}: unknown metric key {mk!r}"
                    )
            display_items = list(metric_keys)

        elif model_ref.startswith("statement:"):
            is_statement = True
            stmt_name = model_ref[len("statement:"):]
            if stmt_name not in catalogue.statements:
                raise ResolverError(
                    f"Block {i}: unknown statement {stmt_name!r}"
                )
            # resolve_statement returns flat list with 'separator' entries
            display_items = resolve_statement(catalogue, stmt_name)
            metric_keys = [item for item in display_items if item != "separator"]

        else:
            raise ResolverError(
                f"Block {i}: model reference must start with 'metric:', "
                f"'metrics:', or 'statement:'. Got: {model_ref!r}"
            )

        display_format = ""
        color_code = False
        display_format = scenario_registry.display_format_for(base_scenario_key)
        color_code = scenario_registry.color_code_for(base_scenario_key)

        resolved_blocks.append(ResolvedBlock(
            alias=alias,
            scenario_key=scenario_ref,
            metric_keys=metric_keys,
            display_items=display_items,
            is_statement=is_statement,
            display_format=display_format,
            color_code=color_code,
        ))

    # ------------------------------------------------------------------
    # Stage 2: Resolution
    # ------------------------------------------------------------------

    # Collect all unique scenario keys referenced across blocks.
    # Block scenario_keys may contain modifiers (e.g. "budget&uncommitted").
    # We strip modifiers for base scenario lookups but keep full keys for identity.
    all_scenario_keys: set[str] = {block.scenario_key for block in resolved_blocks}

    # Build base-key → modifiers mapping for block-level scenario keys.
    # Formula-expanded dependencies (from computed scenarios) are always pure
    # scenario aliases — they never carry modifiers.
    _base_key_cache: dict[str, str] = {}  # full_key → base_key
    _modifiers_cache: dict[str, dict[str, str]] = {}  # full_key → modifiers

    def _base_key(full_key: str) -> str:
        """Resolve a (possibly modified) scenario key to its base scenario key."""
        if full_key not in _base_key_cache:
            base, mods = parse_scenario_modifiers(full_key)
            _base_key_cache[full_key] = base
            _modifiers_cache[full_key] = mods
        return _base_key_cache[full_key]

    def _modifiers_for(full_key: str) -> dict[str, str]:
        _base_key(full_key)  # ensure cached
        return _modifiers_cache.get(full_key, {})

    def _registry_ref(base_key: str) -> ScenarioRef | None:
        return _resolve_registry_ref(scenario_registry, base_key)

    def _registry_or_error(base_key: str) -> ScenarioRef:
        ref = _registry_ref(base_key)
        if ref is None:
            raise ResolverError(f"Unknown scenario {base_key!r}")
        return ref

    def _computed_deps_for(base_key: str) -> set[str]:
        scenario = _registry_or_error(base_key)
        if isinstance(scenario, VarianceScenarioRef | VariancePctScenarioRef):
            return {scenario.left.key, scenario.right.key}
        return set()

    def _computed_formula_for(key: str) -> str:
        base = _base_key(key)
        scenario = _registry_or_error(base)
        if isinstance(scenario, VariancePctScenarioRef):
            return f"({scenario.left.key} - {scenario.right.key}) / abs({scenario.right.key}) * 100"
        if isinstance(scenario, VarianceScenarioRef):
            return f"{scenario.left.key} - {scenario.right.key}"
        raise ResolverError(f"Scenario {key!r} is not computed")

    # Separate into data/shifted and computed.
    # For computed scenarios: expand their formula refs recursively so that
    # all transitively required data/shifted scenarios get DataQuery entries.
    # Shifted scenarios are self-contained — they do NOT pull in their base as a
    # separate DataQuery; the base chain is resolved internally.
    data_and_shifted_keys: list[str] = []
    computed_keys: list[str] = []

    def _collect_computed_deps(key: str, visited: set[str]) -> None:
        """Recursively collect scenario keys, expanding computed formulas only.

        Shifted scenario bases are NOT expanded here — a shifted scenario produces
        its own DataQuery via _resolve_shifted_chain (no separate base query).

        Keys coming from formula refs are pure scenario aliases (no modifiers).
        """
        if key in visited:
            return
        visited.add(key)
        base = _base_key(key)
        scenario = _registry_or_error(base)
        if isinstance(scenario, VarianceScenarioRef | VariancePctScenarioRef):
            if _modifiers_for(key):
                raise ResolverError(
                    f"Scenario modifiers are not supported on computed scenarios. "
                    f"'{key}' resolves to generated scenario '{base}'."
                )
            for ref in (scenario.left.key, scenario.right.key):
                _collect_computed_deps(ref, visited)
        # Real and shifted scenarios produce one DataQuery directly.

    expanded_scenario_keys: set[str] = set()
    for key in all_scenario_keys:
        _collect_computed_deps(key, expanded_scenario_keys)

    # Now classify generated comparisons separately from real/shifted refs.
    for key in sorted(expanded_scenario_keys):
        base = _base_key(key)
        scenario = _registry_or_error(base)
        if isinstance(scenario, VarianceScenarioRef | VariancePctScenarioRef):
            computed_keys.append(key)
        else:
            data_and_shifted_keys.append(key)

    # Build DataQuery entries — keyed by full scenario key (including modifiers)
    # since same base scenario with different modifiers produces different data.
    data_queries_map: dict[str, DataQuery] = {}

    for key in data_and_shifted_keys:
        base = _base_key(key)
        modifiers = _modifiers_for(key)
        scenario = _registry_or_error(base)
        if isinstance(scenario, RealScenarioRef):
            data_queries_map[key] = DataQuery(
                scenario_key=key,
                scenario_id=scenario.scenario_id,
                period_start=period_start,
                period_end=period_end,
                metric_keys=[],
                modifiers=modifiers,
            )
        elif isinstance(scenario, ShiftedScenarioRef):
            data_queries_map[key] = DataQuery(
                scenario_key=key,
                scenario_id=scenario.base.scenario_id,
                period_start=shift_period(period_start, scenario.time_offset_months),
                period_end=shift_period(period_end, scenario.time_offset_months),
                metric_keys=[],
                modifiers=modifiers,
                time_offset=scenario.time_offset_months,
            )
        else:
            raise ResolverError(f"Scenario {base!r} does not resolve to a data query")

    # Build ComputedScenarioEval entries (topologically sorted)
    sorted_computed = _topological_sort_dependencies(
        computed_keys,
        lambda key: _computed_deps_for(_base_key(key)),
    )

    computed_evals: list[ComputedScenarioEval] = []
    for key in sorted_computed:
        computed_evals.append(ComputedScenarioEval(
            scenario_key=key,
            formula=_computed_formula_for(key),
            dependencies=sorted(_computed_deps_for(_base_key(key))),
        ))

    # Collect all metric keys requested across all blocks
    requested_metric_keys: set[str] = set()
    for block in resolved_blocks:
        requested_metric_keys.update(block.metric_keys)

    # Resolve transitive metric dependencies
    all_metric_keys_sorted, all_base_metric_keys, all_derived_metric_keys = (
        _resolve_metric_dependencies(requested_metric_keys, catalogue)
    )

    # Infer domain from base metrics (all base metrics in a request share a domain)
    domain = "pnl"
    if all_base_metric_keys:
        first_base = catalogue.metrics[all_base_metric_keys[0]]
        if isinstance(first_base, BaseMetric):
            domain = first_base.domain

    # ------------------------------------------------------------------
    # Validate dimensions against domain
    # ------------------------------------------------------------------
    strict_dimensions = request.get("_strict_dimensions", True)

    if dimensions:
        domain_cat = catalogue.domains.get(domain)
        if domain_cat:
            valid_dim_keys = {cd.key for cd in domain_cat.dimensions}
            # Also accept parent dimension keys (e.g. 'division', 'department',
            # 'quarter', 'fiscal_year') — derived dimensions whose leaf is
            # bound to this domain. These can be used in GROUP BY.
            def _add_parents(dim_key: str, visited: set[str]) -> None:
                if dim_key in visited:
                    return
                visited.add(dim_key)
                dim = catalogue.dimensions.get(dim_key)
                if dim:
                    for parent_key in dim.parents:
                        valid_dim_keys.add(parent_key)
                        _add_parents(parent_key, visited)

            for cd in domain_cat.dimensions:
                _add_parents(cd.key, set())
            for dim_name in dimensions:
                if dim_name not in valid_dim_keys:
                    available = [
                        f"'{cd.key}' ({cd.label})"
                        for cd in domain_cat.dimensions
                    ]
                    # Find which domains DO have this dimension
                    alt_domains = [
                        d_name
                        for d_name, d_cat in catalogue.domains.items()
                        if any(cd.key == dim_name for cd in d_cat.dimensions)
                    ]
                    hint = ""
                    if alt_domains:
                        hint = (
                            f" This dimension is available in domain(s): "
                            f"{', '.join(alt_domains)}."
                        )
                    if strict_dimensions:
                        raise ResolverError(
                            f"Dimension '{dim_name}' is not available in the "
                            f"'{domain}' domain (source: {domain_cat.source_view}). "
                            f"Available dimensions for this domain: "
                            f"{', '.join(available)}.{hint}"
                        )
                    elif not alt_domains:
                        # Non-strict mode still rejects dimensions that don't
                        # exist in ANY domain — prevents invalid SQL hitting CH.
                        all_available = sorted({
                            cd.key
                            for d_cat in catalogue.domains.values()
                            for cd in d_cat.dimensions
                        })
                        raise ResolverError(
                            f"Dimension '{dim_name}' does not exist in any domain. "
                            f"Available dimensions: {', '.join(all_available)}."
                        )

    # Assign metric_keys and domain to each DataQuery
    base_metric_keys_set = set(all_base_metric_keys)
    for dq in data_queries_map.values():
        dq.metric_keys = list(all_base_metric_keys)
        dq.domain = domain

    # Deduplicate DataQuery: if two scenario_keys produce identical
    # (scenario_id, period_start, period_end, modifiers), merge into one.
    # Modifiers must be part of the key — same base scenario with different
    # modifiers produces different SQL and different data.
    dedup_map: dict[tuple, DataQuery] = {}
    data_queries: list[DataQuery] = []
    for key in sorted(data_queries_map.keys()):
        dq = data_queries_map[key]
        modifiers_frozen = tuple(sorted(dq.modifiers.items()))
        dedup_key = (dq.scenario_id, dq.period_start, dq.period_end, modifiers_frozen)
        if dedup_key not in dedup_map:
            dedup_map[dedup_key] = dq
            data_queries.append(dq)
        # If already present, the existing DataQuery covers both scenario_keys.

    grains_raw = request.get("grains")
    if isinstance(grains_raw, dict):
        grains_spec = GrainSpec(
            detail=bool(grains_raw.get("detail", True)),
            subtotals=bool(grains_raw.get("subtotals", False)),
            grand_total=bool(grains_raw.get("grand_total", False)),
        )
    else:
        grains_spec = GrainSpec()

    return ExecutionPlan(
        blocks=resolved_blocks,
        data_queries=data_queries,
        computed_evals=computed_evals,
        dimensions=dimensions,
        period_start=period_start,
        period_end=period_end,
        all_metric_keys=all_metric_keys_sorted,
        all_base_metric_keys=all_base_metric_keys,
        all_derived_metric_keys=all_derived_metric_keys,
        grains=grains_spec,
    )
