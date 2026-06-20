# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Catalogue → Markdown prompt-section renderers (data-model sections).

Open-core module: pure ``catalogue → Markdown`` renderers for the data-model
sections of an LLM system prompt — available statements, scenarios, and
dimensions / filter keys. They depend only on the engine catalogue and the
scenario registry (no agent runtime, no streaming), so both the Précis
agent's system prompt and the open MCP ``instructions`` builder consume them
from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis_mcp.engine.catalogue import Catalogue
    from precis_mcp.engine.scenario_registry import ScenarioRegistry


def render_scenarios_table(
    catalogue: Catalogue | None,
    scenario_registry: "ScenarioRegistry | None" = None,
) -> str:
    """Render available reporting scenarios as a Markdown prompt section."""
    del catalogue

    lines = [
        "## Available Scenarios",
        "",
        'Pass scenarios as objects whose `scenario` field holds the key, e.g. `[{"scenario": "actuals"}, {"scenario": "budget", "alias": "Budget"}]`. Real scenarios come from `semantic.scenarios`; shifted and comparison scenarios are generated.',
        "",
        "### Data scenarios (stored data)",
        "",
        "| `scenario` | Scenario ID | Label | Status | Description |",
        "|---|---|---|---|---|",
    ]
    if scenario_registry is None:
        lines.append("| _Scenario registry unavailable_ |  |  |  |  |")
        return "\n".join(lines)

    vocabulary = scenario_registry.to_reporting_vocabulary()
    for row in vocabulary["real"]:
        lines.append(
            f"| `{row['scenario']}` | `{row['scenario_id']}` | {row['label']} | "
            f"{row.get('status') or ''} | {row.get('description') or ''} |"
        )

    lines.extend([
        "",
        "### Shifted scenarios (auto-offset periods)",
        "",
        "| `scenario` | Base | Offset | Label | Description |",
        "|---|---|---|---|---|",
    ])
    for row in vocabulary["shifted"]:
        offset = f"{row['time_offset_months']:+d} months"
        lines.append(f"| `{row['scenario']}` | `{row['base']}` | {offset} | {row['label']} | {row['description']} |")

    lines.extend([
        "",
        "**How shifted scenarios work:** When the period is Jan–Mar 2026 and you use `prior_year`, "
        "the engine automatically queries Jan–Mar 2025. You do NOT need to change the period — just use the key.",
        "",
        "### Computed scenarios (calculated from other scenarios)",
        "",
        "| `scenario` | Formula | Label | Description |",
        "|---|---|---|---|",
    ])
    for row in vocabulary["comparisons"]:
        formula = "generated"
        if row["type"] == "variance":
            formula = f"`{row['left']} - {row['right']}`"
        elif row["type"] == "variance_pct":
            formula = f"`({row['left']} - {row['right']}) / abs({row['right']}) * 100`"
        lines.append(f"| `{row['scenario']}` | {formula} | {row['label']} | {row['description']} |")

    lines.extend([
        "",
        "**Additional comparisons available on demand.** The table above is a curated default. "
        "The engine resolves any `{left}_vs_{right}` or `{left}_vs_{right}_pct` key where each side "
        "is a real alias, a shifted key (`{alias}_py`, `{alias}_pp`), or the compatibility aliases "
        "`prior_year` / `prior_period`. Examples that work even though they aren't listed: "
        "`budget_vs_forecast_q1_py` (current budget vs last year's forecast), "
        "`actuals_vs_prior_year_pct` (YoY % via the compatibility alias), "
        "`forecast_q1_vs_actuals_py` (current forecast vs last year's actuals). "
        "Use these when the user's question implies a cross-scenario time-shifted comparison; "
        "you do not need to confirm they exist first.",
    ])

    return "\n".join(lines)


def render_statements_table(catalogue: Catalogue) -> str:
    """Render all catalogue statements as a Markdown section for the system prompt."""
    lines = [
        "## Available Statements",
        "",
        "The `run_statement` tool accepts a `statement` parameter to control which metrics are included. "
        'When the user asks for "the full picture", "with FTEs", or a "comprehensive" view, use `full_pnl`. '
        'When they ask for a "summary" or "executive" view, use `executive_summary`.',
        "",
        "| Statement | Label | Description |",
        "|---|---|---|",
    ]
    for name, stmt in catalogue.statements.items():
        lines.append(f"| `{name}` | {stmt.label} | {stmt.description} |")

    return "\n".join(lines)


def _collect_domain_keys(
    domain_name: str,
    catalogue: Catalogue,
) -> tuple[list[str], list[str]]:
    """Collect the valid dimension keys for a single domain.

    A catalogue dimension name is valid in both ``filters`` and ``dimensions``.
    Inline axes are the only breakdown-only exception (no master data, cannot
    be filtered).

    Returns (dimension_keys, axis_only_keys) as sorted lists.
    """
    from precis_mcp.engine.context_validation import (
        _get_valid_filter_keys_for_domains,
    )
    domain_set = {domain_name}
    dimension_keys = sorted(_get_valid_filter_keys_for_domains(domain_set, catalogue))
    domain = catalogue.domains.get(domain_name)
    axis_only_keys = sorted(
        cd.key
        for cd in (domain.dimensions if domain else [])
        if cd.source_inline and not cd.filterable
    )
    return dimension_keys, axis_only_keys


def _collect_domain_metrics(catalogue: Catalogue) -> dict[str, list[str]]:
    """Group metric keys by domain. For derived metrics, resolve domain via formula refs."""
    from precis_mcp.engine.catalogue import BaseMetric, DerivedMetric, _metric_refs

    domain_metrics: dict[str, list[str]] = {}

    def _resolve_domain(key: str, visited: set[str] | None = None) -> str:
        if visited is None:
            visited = set()
        if key in visited:
            return ""
        visited.add(key)
        m = catalogue.metrics.get(key)
        if m is None:
            return ""
        if isinstance(m, BaseMetric):
            return m.domain
        if isinstance(m, DerivedMetric):
            for ref_key in _metric_refs(m.formula):
                d = _resolve_domain(ref_key, visited)
                if d:
                    return d
        return ""

    for key, metric in catalogue.metrics.items():
        if isinstance(metric, BaseMetric):
            domain = metric.domain
        else:
            domain = _resolve_domain(key)
        if domain:
            domain_metrics.setdefault(domain, []).append(key)

    return domain_metrics


def render_dimensions_table(catalogue: Catalogue) -> str:
    """Render available dimensions and filter keys per domain for the system prompt.

    Two sections:
    1. Per-domain quick reference — flat list of ALL valid dimension keys so the
       agent doesn't need to chain cube bindings + parent walks.
    2. Dimension reference — types, examples, and filtering rules.
    """
    lines = [
        "## Available Dimensions",
        "",
        "Each key below is a catalogue dimension name. Use the **same** key in "
        "both `filters` and `dimensions` — never a source-view column name.",
        "",
    ]

    # ------------------------------------------------------------------
    # Section 1: Per-domain quick reference (the agent's primary lookup)
    # ------------------------------------------------------------------
    lines.append("### Quick Reference — Valid Keys per Domain")
    lines.append("")

    domain_metrics = _collect_domain_metrics(catalogue)

    for domain_name, domain in catalogue.domains.items():
        if not domain.dimensions:
            continue
        dimension_keys, axis_only_keys = _collect_domain_keys(
            domain_name, catalogue,
        )
        metrics = domain_metrics.get(domain_name, [])
        lines.append(f"**`{domain_name}`**")
        if metrics:
            lines.append(f"- Metrics: {', '.join(f'`{m}`' for m in metrics)}")
        lines.append(
            "- Dimension keys (`filters` and `dimensions`): "
            f"{', '.join(f'`{k}`' for k in dimension_keys)}"
        )
        if axis_only_keys:
            lines.append(
                "- Axis-only keys (valid in `dimensions` only, not `filters`): "
                f"{', '.join(f'`{k}`' for k in axis_only_keys)}"
            )
        lines.append("")

    # ------------------------------------------------------------------
    # Section 2: Dimension reference (types + examples)
    # ------------------------------------------------------------------
    lines.append("### Dimension Reference")
    lines.append("")
    lines.append("The same key works as a filter and as a breakdown axis.")
    lines.append("")
    lines.append("| Dimension key | Type | Example |")
    lines.append("|---|---|---|")

    emitted: set[str] = set()

    # Leaf dimensions
    _leaf_examples = {
        "cost_centre": "CC-CLOUD-01",
        "account": "4100",
        "employee": "42",
        "project": "6",
        "period": "2025-03",
    }
    for dim_key, dim in catalogue.dimensions.items():
        if dim.is_leaf and dim_key not in emitted:
            example_val = _leaf_examples.get(dim_key, f"<{dim.label}>")
            lines.append(
                f'| `{dim_key}` | leaf | `{{"{dim_key}": "{example_val}"}}` |'
            )
            emitted.add(dim_key)

    # Derived dimensions
    for dim_key, dim in catalogue.dimensions.items():
        if dim.is_derived and dim.derived_from and dim_key not in emitted:
            lines.append(
                f'| `{dim_key}` | derived (from {dim.derived_from.dimension}) '
                f'| `{{"{dim_key}": "<{dim.label}>"}}` |'
            )
            emitted.add(dim_key)

    # Ragged hierarchies
    for dim_key, dim in catalogue.dimensions.items():
        if dim.is_ragged and dim_key not in emitted:
            has_prefix = any(rl.node_prefix for rl in dim.ragged_levels)
            if has_prefix:
                example = f'`{{"{dim_key}": "<node_id>"}}` — use `search_hierarchy`'
            else:
                example = f'`{{"{dim_key}": "<value>"}}` — use `search_hierarchy`'
            lines.append(f"| `{dim_key}` | ragged hierarchy | {example} |")
            emitted.add(dim_key)

    lines.append("")

    # ------------------------------------------------------------------
    # Filtering rules
    # ------------------------------------------------------------------
    lines.append("### Filtering Rules")
    lines.append("")
    lines.append("1. **Pick the filter key that matches the user's intent.** "
                 "If the user asks about a department, use `department` — not `cost_centre`. "
                 "The quick reference above lists every valid key per domain.")
    lines.append('2. **Leaf and derived filters** use plain values. '
                 'Example: `{"cost_centre": "CC-CLOUD-01"}` or '
                 '`{"department": "Cloud & Infrastructure"}`.')
    lines.append("3. **Ragged hierarchy filters** use node_id values. "
                 "Call `search_hierarchy` to find the exact value. "
                 'Example: `{"org_structure": "Cloud & Infrastructure"}`.')
    lines.append("4. **Axis-only breakdown keys** can be used only in `dimensions`, "
                 "not in `filters`. They are source-only columns on federated domains.")
    lines.append("")
    lines.append("Use `search_hierarchy` to find valid *values* for any filter key. "
                 "Do NOT use it to discover filter key names — the tables above are authoritative.")
    lines.append("")
    lines.append("Use `list_kpis` for metric descriptions, formats, and available dimensions.")

    return "\n".join(lines)
