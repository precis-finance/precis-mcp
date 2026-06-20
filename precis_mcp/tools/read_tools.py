# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""MCP read tools — thin wrappers around the metric engine.

Two report tools (run_statement, run_metric) replace the previous seven.
Both support an ``out`` parameter controlling output destination:
  - 'render' (default): auto-rendered to user as styled table
  - 'agent': raw data returned to agent for reasoning
  - 'excel': Excel file generated, download link returned
  - 'report': persist result as a financial_table block on a saved report
    (via Précis's report emitters), return confirmation only
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import time
import uuid
from decimal import Decimal
from datetime import date
from datetime import datetime
from typing import TYPE_CHECKING

from datetime import timezone

from mcp.server.fastmcp import FastMCP
from precis_mcp.db import get_clickhouse_client
from precis_mcp.db import execute_platform
from precis_mcp.engine import execute_report
from precis_mcp.read_tool_hooks import (
    get_chart_cache,
    get_excel_dispatch,
    get_output_renderer,
)
from precis_mcp.engine.catalogue import (
    BaseMetric,
    DerivedMetric,
)
from precis_mcp.engine.inspect import InspectionError
from precis_mcp.engine.inspect import inspect_rows as engine_inspect_rows
from precis_mcp.engine.inspect import (
    get_inspection_schema as engine_get_inspection_schema,
)
from precis_mcp.engine.inspect import (
    list_inspection_sources as engine_list_inspection_sources,
)
from precis_mcp.engine.filter_resolver import FilterResolutionError, resolve_filters
from precis_mcp.engine.formatter import SCALE_LABELS
from precis_mcp.engine.resolver import ResolverError
from precis_mcp.engine.scenario_registry import (
    NonWritableScenarioError,
    UnknownScenarioError,
)
from precis_mcp.engine.scenario_store import ScenarioStore

if TYPE_CHECKING:
    from precis_mcp.catalogue_ref import CatalogueRef

logger = logging.getLogger(__name__)


_INSPECT_AGENT_ROW_CAP = 100
_INSPECT_AGENT_CHAR_CAP = 50_000


def _duplicate_alias_error(blocks: list[dict]) -> dict | None:
    """Return a validation error if two column blocks share an alias.

    Result values are keyed by alias, so duplicate aliases would collide
    (one column silently overwriting another). Reject before the engine runs.
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for b in blocks:
        alias = b.get("alias", "")
        if alias in seen and alias not in dupes:
            dupes.append(alias)
        seen.add(alias)
    if dupes:
        names = ", ".join(repr(a) for a in dupes)
        return {
            "error": (
                f"Duplicate scenario alias(es): {names}. Give each column a "
                "distinct alias."
            ),
            "error_type": "validation",
        }
    return None


def _invalid_scenarios_error(scenarios: list) -> dict | None:
    """Reject scenarios entries that lack a 'scenario' selector.

    The tool schema can only express ``list[object]`` — the inner shape is
    enforced here. A missing selector must fail loudly: silently defaulting
    it renders plausible-looking columns of the wrong scenario.
    """
    for sc in scenarios:
        if not isinstance(sc, dict) or not sc.get("scenario"):
            return {
                "error": (
                    f"Invalid scenarios entry: {sc!r}. Each entry must be an "
                    'object with a "scenario" field (a scenario key from '
                    'list_scenarios) and an optional "alias" display label, '
                    'e.g. [{"scenario": "actuals"}, '
                    '{"scenario": "budget", "alias": "Budget"}, '
                    '{"scenario": "actuals_vs_budget", "alias": "Variance"}].'
                ),
                "error_type": "validation",
            }
    return None


def _resolve_writable_id(value: str) -> str | dict:
    if not value:
        return {"error": "Scenario reference is empty.", "error_type": "invalid_scenario"}
    try:
        return ScenarioStore(get_clickhouse_client()).resolve_writable_id(value)
    except NonWritableScenarioError as exc:
        return {"error": str(exc), "error_type": "invalid_scenario"}
    except UnknownScenarioError:
        return {
            "error": (
                f"Unknown writable scenario '{value}'. Use a real scenario alias "
                "or canonical scenario_id from list_scenarios()."
            ),
            "error_type": "invalid_scenario",
        }


def _cap_inspection_for_agent(result: dict) -> dict:
    """Keep row-level data from flooding model context."""
    capped = dict(result)
    rows = list(capped.get("rows") or [])
    if len(rows) > _INSPECT_AGENT_ROW_CAP:
        capped["rows"] = rows[:_INSPECT_AGENT_ROW_CAP]
        capped["truncated"] = True
        capped["agent_truncated"] = True
        capped["agent_row_cap"] = _INSPECT_AGENT_ROW_CAP

    encoded = json.dumps(capped, default=str)
    if len(encoded) > _INSPECT_AGENT_CHAR_CAP:
        capped["rows"] = capped.get("rows", [])[:25]
        capped["truncated"] = True
        capped["agent_truncated"] = True
        capped["agent_char_cap"] = _INSPECT_AGENT_CHAR_CAP
    return capped


def _json_safe_inspection_result(result: dict) -> dict:
    """Convert DB / pandas scalars to JSON-clean values.

    Must produce output that survives ``json.dumps(..., allow_nan=False)`` —
    NaN/Inf in an SSE payload break the browser's ``JSON.parse`` (so the
    block silently drops) and 500 the ``/messages`` response. Pandas
    returns ``float('nan')`` for NULL cells via ``DataFrame.to_dict``.
    """
    # Lazy import: numpy/pandas only present in the Ibis path; the
    # ClickHouse path doesn't need them.
    try:
        import numpy as _np
    except ImportError:  # pragma: no cover
        _np = None
    try:
        import pandas as _pd
    except ImportError:  # pragma: no cover
        _pd = None

    def convert(value):
        if value is None:
            return None
        # pandas missing-value sentinels first — pd.NaT is a datetime
        # subclass, so the datetime branch below would otherwise
        # isoformat it to the string ``'NaT'`` instead of dropping it.
        # pd.isna raises on arrays; cells are scalars, but guard anyway.
        if _pd is not None:
            try:
                if _pd.isna(value):
                    return None
            except (TypeError, ValueError):
                pass
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        if isinstance(value, Decimal):
            if value.is_nan() or value.is_infinite():
                return None
            return float(value)
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if _np is not None and isinstance(value, _np.generic):
            return convert(value.item())
        return value

    cleaned = dict(result)
    cleaned["rows"] = [
        {key: convert(value) for key, value in row.items()}
        for row in list(result.get("rows") or [])
    ]
    return cleaned


def _record_inspection_audit(
    *,
    user_id: str,
    source_key: str,
    result: dict,
    filters: dict | None,
    columns: list[str] | None,
    duration_ms: int,
) -> None:
    """Best-effort audit row for inspection queries."""
    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    try:
        execute_platform(
            """
            INSERT INTO inspection_audit (
                user_id, source_key, backend_kind, backend, source_view,
                filters, columns, rendered_sql, query_params,
                row_count, duration_ms, truncated
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s, %s::jsonb,
                %s, %s, %s
            )
            """,
            (
                user_id,
                source_key,
                query.get("backend_kind", ""),
                query.get("backend", ""),
                query.get("source_view", ""),
                json.dumps(filters or {}),
                json.dumps(columns or result.get("columns") or []),
                query.get("sql", ""),
                json.dumps(query.get("parameters", {}), default=str),
                int(result.get("row_count", 0) or 0),
                duration_ms,
                bool(result.get("truncated", False)),
            ),
        )
    except Exception:
        logger.exception("Failed to write inspection audit row")


# Module-level handles to the registered report tools. Populated at the end
# of ``register_read_tools``. Consumed by ``get_refresh_dispatch`` (below)
# so that ``refresh_workbook`` can re-invoke the same pipelines the extract
# was generated from. Using closures over ``register_read_tools``'s inner
# callables avoids duplicating the request-building / aggregate-query logic.
_RUN_STATEMENT = None
_RUN_METRIC = None


# The engine no longer rounds values (formatter preserves full precision so
# Excel gets exact figures). The render block rounds at display time in the
# renderer, but the raw result returned to the LLM would otherwise carry
# float noise — so the agent/render return path rounds to each figure's
# display decimals. Internal callers that feed precision-sensitive sinks
# (Extract refresh) suppress this via the contextvar below; the Excel and
# chart-cache paths branch off before the return and are unaffected.
_suppress_consumer_rounding: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_suppress_consumer_rounding", default=False
)


def _round_values_for_consumer(result: dict) -> dict:
    """Round engine values to their display decimals, in place.

    Display decimals come from the per-metric ``item['decimals']``, except for
    variance-% columns whose decimals are stamped on the scenario column. Only
    applied to the LLM-facing render/agent return — Excel and the chart cache
    keep full precision.
    """
    if _suppress_consumer_rounding.get():
        return result
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        return result
    pct_decimals = {
        s["alias"]: s.get("decimals", 1)
        for s in result.get("scenarios", [])
        if s.get("format") == "percent" and s.get("alias")
    }
    for row in result["rows"]:
        item = row.get("item", {})
        item_dec = item.get("decimals", 0)
        values = row.get("values")
        if not isinstance(values, dict):
            continue
        for alias, v in values.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values[alias] = round(v, pct_decimals.get(alias, item_dec))
    return result


def get_refresh_dispatch():
    """Return a ``(tool_name, params) -> engine_result`` dispatch callable.

    Used by ``refresh_workbook`` to re-run the source of each Extract sheet.
    ``params`` is the stored ``source.params`` block (no ``out`` key); we
    force ``out='agent'`` so the pipeline returns the raw result dict
    without registering a new file or emitting an artifact block.
    """
    def dispatch(tool_name: str, params) -> dict:
        fn = {"run_statement": _RUN_STATEMENT, "run_metric": _RUN_METRIC}.get(tool_name)
        if fn is None:
            return {
                "error": f"refresh dispatch: unknown tool {tool_name!r}",
                "error_type": "validation",
            }
        kwargs = {k: v for k, v in dict(params).items() if v is not None}
        kwargs["out"] = "agent"
        # Extract refresh must mirror the full-precision figures the initial
        # Extract sheet was built from (the out='excel' path); suppress the
        # display-decimal rounding applied to LLM-facing returns.
        token = _suppress_consumer_rounding.set(True)
        try:
            return fn(**kwargs)
        finally:
            _suppress_consumer_rounding.reset(token)
    return dispatch


def register_read_tools(mcp: FastMCP, ref: "CatalogueRef"):
    """Register all read tools on the MCP server instance.

    Accepts a CatalogueRef so that the reload_catalogue tool can swap the active
    catalogue without restarting the server.
    """

    def _resolve_scenario(scenario_id: str) -> str:
        """Normalize a user-facing scenario reference for read execution.

        Handles modifier suffixes (e.g. "BUD-2026&uncommitted"):
        strips modifiers, resolves the base, re-appends modifiers.
        """
        from precis_mcp.engine.resolver import strip_scenario_modifiers

        base = strip_scenario_modifiers(scenario_id)
        modifier_suffix = scenario_id[len(base):]  # e.g. "&uncommitted" or ""

        try:
            from precis_mcp.engine.scenario_registry import load_scenario_registry

            registry = load_scenario_registry(get_clickhouse_client())
            return registry.normalize_key(base) + modifier_suffix
        except Exception:
            logger.debug("Could not normalize scenario via semantic registry", exc_info=True)

        return scenario_id

    def _normalise_scale(scale: int) -> int | dict:
        """Normalise scale to a valid power (0/3/6/9).

        Accepts common divisor values (1, 1000, 1_000_000, 1_000_000_000)
        and converts them. Returns a validation error dict for invalid values.
        """
        _DIVISOR_TO_POWER = {1: 0, 1000: 3, 1_000_000: 6, 1_000_000_000: 9}
        if scale in _DIVISOR_TO_POWER:
            return _DIVISOR_TO_POWER[scale]
        if scale in (0, 3, 6, 9):
            return scale
        return {
            "error": f"Invalid scale {scale}. Use 0 (units), 3 (thousands), "
                     f"6 (millions), or 9 (billions).",
            "error_type": "validation",
        }

    def _safe_execute(request: dict, _scope=None) -> dict:
        """Execute a report request with error handling.

        Returns the engine response on success, or an error dict on failure.
        Errors are returned as ``{"error": "...", "error_type": "..."}`` so the
        agent can reason about them and retry with corrected parameters.
        """
        try:
            ch = get_clickhouse_client()
            return execute_report(request, ref.current, ch_client=ch, scope=_scope)
        except (ResolverError, FilterResolutionError) as e:
            # Engine-authored messages — actionable guidance, safe to return.
            return {"error": str(e), "error_type": "validation"}
        except Exception as e:
            # Driver/internal exceptions can embed SQL or paths — generic
            # message to the caller, detail in the server log.
            logger.exception("Report execution failed")
            return {
                "error": (
                    f"Query execution failed with an internal "
                    f"{type(e).__name__}; details were logged."
                ),
                "error_type": "execution",
            }

    def _resolve_base_domain(metric_key: str) -> str | None:
        """Resolve a metric key to its base domain, following derived formulas."""
        from precis_mcp.engine.catalogue import _metric_refs

        visited: set[str] = set()
        to_check = [metric_key]
        while to_check:
            key = to_check.pop()
            if key in visited:
                continue
            visited.add(key)
            metric = ref.current.metrics.get(key)
            if metric is None:
                continue
            if isinstance(metric, BaseMetric):
                return metric.domain
            if isinstance(metric, DerivedMetric):
                to_check.extend(_metric_refs(metric.formula))
        return None

    def _inspection_ibis_backends(source_key: str) -> dict[str, object] | None:
        domain = ref.current.domains.get(source_key)
        if domain is None or domain.backend_kind != "ibis":
            return None
        from precis_mcp.engine.ibis_registry import get_ibis_backends

        return get_ibis_backends({domain.backend})

    def _resolve_inspection_scenario(scenario_id: str | None) -> str | None:
        if scenario_id is None:
            return None
        resolved = _resolve_writable_id(scenario_id)
        if isinstance(resolved, str):
            return resolved
        return scenario_id

    def _build_caption(
        tool_name: str,
        *,
        statement: str | None = None,
        metrics: list[str] | None = None,
        scenarios: list[dict] | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        filters: dict | None = None,
        dimensions: list[str] | None = None,
        result: dict | None = None,
    ) -> dict:
        """Build a caption dict with report metadata.

        Called inside the read tool after engine execution, using the tool's
        own resolved parameters. The caption is attached to ``result["caption"]``
        so both the render path (the Précis API endpoint) and the Excel export can use it
        without reconstructing it from cached args.
        """
        caption: dict = {}

        # --- Report description ---
        if tool_name == "run_statement" and statement:
            stmt = ref.current.statements.get(statement)
            caption["description"] = stmt.label if stmt else statement
        elif tool_name == "run_metric" and metrics:
            labels = []
            for m in metrics:
                metric_obj = ref.current.metrics.get(m)
                labels.append(metric_obj.label if metric_obj else m)
            caption["description"] = ", ".join(labels) if labels else "Metric Report"

        # --- Scenarios ---
        if scenarios:
            aliases = [s.get("alias", s.get("scenario", "")) for s in scenarios]
            if aliases:
                caption["scenarios"] = aliases

        # --- Period ---
        if period_start and period_end:
            caption["period"] = f"{period_start} to {period_end}"
        elif period_start:
            caption["period"] = period_start

        # --- Filters ---
        if filters and isinstance(filters, dict):
            caption["filters"] = filters

        # --- Dimensions ---
        if dimensions:
            caption["dimensions"] = dimensions

        # --- Scale ---
        if result:
            scale_label = result.get("scale_label")
            if not scale_label:
                scale = result.get("scale", result.get("metadata", {}).get("scale", 0))
                scale_label = SCALE_LABELS.get(scale, "") if scale else ""
            if scale_label:
                caption["scale"] = scale_label

        # --- Timestamp ---
        caption["generated_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        return caption

    # ------------------------------------------------------------------
    # Utility tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_scenarios() -> dict:
        """[UTILITY] List available scenarios and scenario registry metadata.

        Returns sections:
        - ``registry``: New scenario registry vocabulary from
          ``semantic.scenarios``. This is the target source of truth.

        The result is filtered to the caller's profile-derived scope —
        scenarios the caller has no read access to are omitted from
        ``real``; shifted and comparison entries whose underlying real
        scenario(s) are denied are also dropped. Admins see all.
        """
        from precis_mcp.auth import get_auth_context
        from precis_mcp.engine.scenario_registry import (
            load_scenario_registry,
        )

        registry_payload: dict = {
            "real": [], "shifted": [], "comparisons": [],
            "compatibility_aliases": [],
        }

        try:
            client = get_clickhouse_client()
            scenario_registry = load_scenario_registry(client)
            registry_payload = scenario_registry.to_reporting_vocabulary()
        except Exception:
            logger.warning("Could not query semantic.scenarios table", exc_info=True)

        permissions = get_auth_context().permissions

        def _visible(sid: str) -> bool:
            if permissions.is_admin:
                return True
            sp = permissions.scenarios.get(sid)
            return sp is not None and "read" in sp.tool_scopes

        real_rows = [
            row for row in registry_payload.get("real", [])
            if _visible(row["scenario_id"])
        ]
        shifted_rows = [
            row for row in registry_payload.get("shifted", [])
            if _visible(row["base_scenario_id"])
        ]
        comparison_rows = [
            row for row in registry_payload.get("comparisons", [])
            if all(_visible(sid) for sid in row.get("scenario_ids", []))
        ]
        # Compatibility aliases map an old key to a canonical real scenario_id
        # *or* alias. We keep an alias only if it resolves to a visible real
        # scenario; resolution goes through the registry.
        visible_real_keys = {row["scenario"] for row in real_rows}
        visible_real_aliases = {row["alias"] for row in real_rows}
        visible_real_ids = {row["scenario_id"] for row in real_rows}
        compat_rows = [
            row for row in registry_payload.get("compatibility_aliases", [])
            if row.get("resolves_to") in (
                visible_real_keys | visible_real_aliases | visible_real_ids
            )
        ]

        return {
            "registry": {
                "real": real_rows,
                "shifted": shifted_rows,
                "comparisons": comparison_rows,
                "compatibility_aliases": compat_rows,
            },
        }

    @mcp.tool()
    def list_kpis() -> list[dict]:
        """[UTILITY] List available KPIs with descriptions, domains, and dimensions.

        Returns the full metric catalogue — names, formats, formulas, descriptions,
        plus the **domain** each metric belongs to and which **dimensions** are
        available for that domain.

        Use this to:
        - Discover valid metric keys before calling ``run_metric``
        - Check which domain a metric belongs to
        - Verify which dimensions are available for a metric
        """
        def _dimension_metadata(domain_name: str | None) -> dict:
            if not domain_name:
                return {}
            domain_cat = ref.current.domains.get(domain_name)
            if not domain_cat:
                return {}

            from precis_mcp.engine.context_validation import (
                _get_valid_filter_keys_for_domains,
            )

            # One vocabulary: a catalogue dimension name is valid in both
            # ``filters`` and ``dimensions``. Inline axes are the only
            # breakdown-only exception (no master data, cannot be filtered).
            axis_only = [
                cd.key
                for cd in domain_cat.dimensions
                if cd.source_inline and not cd.filterable
            ]
            return {
                "dimension_keys": sorted(
                    _get_valid_filter_keys_for_domains({domain_name}, ref.current)
                ),
                "axis_only_dimensions": axis_only,
            }

        result = []
        for key, metric in ref.current.metrics.items():
            entry: dict = {
                "name": key,
                "label": metric.label,
                "format": metric.format,
                "fs_group": metric.fs_group,
            }
            if isinstance(metric, DerivedMetric):
                entry["type"] = "derived"
                entry["formula"] = metric.formula
                # Resolve to base domain
                base_domain = _resolve_base_domain(key)
                entry["domain"] = base_domain or "derived"
                entry.update(_dimension_metadata(base_domain))
            elif isinstance(metric, BaseMetric):
                entry["type"] = "base"
                entry["domain"] = metric.domain
                entry.update(_dimension_metadata(metric.domain))
            if metric.description:
                entry["description"] = metric.description
            if metric.calculation_note:
                entry["calculation_note"] = metric.calculation_note
            result.append(entry)
        return result

    @mcp.tool()
    def list_inspection_sources() -> list[dict]:
        """[UTILITY] List row-level sources available to inspect.

        Use this before ``inspect_rows`` when the user asks to see, inspect,
        or drill into underlying detail rows. Returns source keys, source
        views, output columns, and semantic dimensions that can be used as
        filters.
        """
        return engine_list_inspection_sources(ref.current)

    @mcp.tool()
    def get_inspection_schema(source_key: str) -> dict:
        """[UTILITY] Return the row-level inspection schema for one source.

        Args:
            source_key: Domain/source key from ``list_inspection_sources``,
                e.g. ``gl_federated`` or ``worklog_federated``.
        """
        try:
            return engine_get_inspection_schema(ref.current, source_key)
        except InspectionError as e:
            return {"error": str(e), "error_type": "validation"}

    @mcp.tool()
    def inspect_rows(
        source_key: str,
        scenario_id: str,
        filters: dict | None = None,
        columns: list[str] | None = None,
        limit: int | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        out: str = "render",
        filename: str | None = None,
        sheet_name: str | None = None,
        _scope=None,
    ) -> dict:
        """Inspect row-level detail from an enabled catalogue source.

        This is the structured drill-through tool for detail tables. Filters
        use semantic dimension keys from the source's ``dimensions`` section;
        output columns are restricted to configured ``inspect_columns``.

        Output modes:
        - ``out='render'``: returns a capped sample plus an inspection grid for the user.
        - ``out='agent'``: returns a capped sample for reasoning.
        - ``out='excel'``: generates an Excel file and returns a file artifact.

        ``scenario_id`` is required so the standard scenario permission gate
        runs before the row-level query executes.
        """
        if out not in {"render", "agent", "excel"}:
            return {
                "error": "Invalid out value. Use 'render', 'agent', or 'excel'.",
                "error_type": "validation",
            }

        try:
            start = time.perf_counter()
            ch = get_clickhouse_client()
            result = engine_inspect_rows(
                ref.current,
                source_key,
                filters=filters,
                columns=columns,
                limit=limit,
                scenario_id=_resolve_inspection_scenario(scenario_id),
                period_start=period_start,
                period_end=period_end,
                ch_client=ch,
                ibis_backends=_inspection_ibis_backends(source_key),
                per_scenario_scope=_scope,
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
        except InspectionError as e:
            return {"error": str(e), "error_type": "validation"}
        except (FilterResolutionError, ResolverError) as e:
            return {"error": str(e), "error_type": "validation"}
        except Exception as e:
            logger.exception("Inspection query failed")
            return {"error": f"Inspection query failed: {e}", "error_type": "execution"}

        try:
            from precis_mcp.auth import get_auth_context

            audit_user_id = get_auth_context().user_id
        except RuntimeError:
            audit_user_id = ""
        _record_inspection_audit(
            user_id=audit_user_id,
            source_key=source_key,
            result=result,
            filters=filters,
            columns=columns,
            duration_ms=duration_ms,
        )

        result = _json_safe_inspection_result(result)
        period_label = (
            f"{period_start} to {period_end}"
            if period_start and period_end
            else period_start or period_end
        )
        # TableCaption-shaped so ChartCaptionBar renders it the same way
        # as run_statement / run_metric. ``scenarios`` is a list for shape
        # parity even though inspect takes a single scenario_id.
        # duration_ms surfaces query cost to the user (a common ask for
        # row-level drill-through where slow queries are expected).
        result["caption"] = {
            "description": f"Inspection: {source_key}",
            "source_key": source_key,
            "scenarios": [scenario_id] if scenario_id else [],
            "period": period_label,
            "filters": filters or {},
            "duration_ms": duration_ms,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        }

        if out == "excel":
            dispatch = get_excel_dispatch()
            if dispatch is None:
                return {
                    "error": "out='excel' is not available in this deployment",
                    "error_type": "unsupported",
                }
            return dispatch.dispatch_inspection_excel(
                result=result,
                source_key=source_key,
                filename=filename,
                sheet_name=sheet_name,
            )
        if out in {"agent", "render"}:
            return _cap_inspection_for_agent(result)
        return result

    # ------------------------------------------------------------------
    # Report tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def run_statement(
        statement: str | None = None,
        scenarios: list[dict] | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        filters: dict | None = None,
        dimensions: list[str] | None = None,
        scale: int | None = None,
        decimals: int | None = None,
        out: str = "render",
        report_id: str = "",
        position: str = "end",
        target: str | None = None,
        layout: str = "report",
        filename: str | None = None,
        sheet_name: str | None = None,
        overwrite: bool = False,
        _scope=None,
    ) -> dict:
        """Run a financial statement report.

        All parameters except ``dimensions`` and ``out`` have automatic defaults
        from the active report context. Omit them to use defaults. Pass explicitly
        to override for this call only.

        Rows are defined by the **statement** (e.g. P&L lines from revenue to EBITDA).
        Columns are **scenarios** (e.g. Actuals, Budget, Variance), optionally crossed
        with **dimensions** (e.g. monthly breakdown, by cost centre).

        Output modes:
        - ``out='render'`` (default): auto-rendered to the user as a styled financial
          table — add brief commentary but do NOT reproduce the table.
        - ``out='agent'``: raw data returned for your reasoning — present findings
          in your own words.
        - ``out='excel'``: generates a styled Excel file and returns a download link.
          The data is NOT returned — if you need it for reasoning, call again with
          ``out='agent'``.
        - ``out='report'``: persist the result as a ``financial_table`` block on a
          report (``report_id`` required). Returns a confirmation only — no raw
          data. If you need the numbers, re-call with ``out='agent'``.

        Data model note: Statements may span multiple domains (e.g. full_pnl includes
        pnl + payroll metrics). Dimensions ``period`` and ``cost_centre`` work for all
        statements. Other dimensions produce partial results — metrics from compatible
        domains are broken down, others show aggregate values.

        Args:
            statement: Statement key from **Available Statements** in the
                data model description. Defaults from report context, or 'pnl'.
            scenarios: List of scenario dicts, each with 'scenario' (registry
                key) and optional 'alias' (display label), e.g.
                ``[{"scenario": "actuals"},
                {"scenario": "budget", "alias": "Budget"},
                {"scenario": "actuals_vs_budget", "alias": "Variance"}]``.
                Use the scenario keys from **Available Scenarios** in the data
                model description (real, shifted, and generated comparison
                types). If a key is not recognised, call ``list_scenarios``
                to discover valid keys. Defaults from report context.
            period_start: Range start (YYYY-MM). Defaults from report context.
            period_end: Range end (YYYY-MM). Defaults from report context.
            filters: Dimension filters. Defaults from report context.
                Pass {} to explicitly clear all filters.
            dimensions: Optional dimension keys to break by — the same
                catalogue dimension names used in ``filters``.
            scale: Currency scaling power. 0=units (default), 3=thousands,
                6=millions, 9=billions. Defaults from report context.
            decimals: Decimal places. Defaults from report context.
            out: Output mode — 'render' (default), 'agent', 'excel', or 'report'.
            report_id: Target report ID (required when ``out='report'``).
            position: Where to place the block when ``out='report'``:
                'end' (default), 'start', 'replace:<block_id>',
                'after:<block_id>', or 'before:<block_id>'. Use 'replace:'
                to rebase a cloned block for a new period.
            target: Only for ``out='excel'``. ``None`` (default) creates
                a new 24h-expiring registry row. ``'new'`` creates a
                persistent registry row (``expires_at=NULL``).
                ``'append'`` mutates an existing file in place (resolved
                by ``filename``) — same file_id, fresh sheet. ``'new'``
                and ``'append'`` both require ``filename``. All three
                paths return ``files_created: [file_id]`` and surface
                automatically; no follow-up surfacing call is needed.
            layout: Only for ``out='excel'``. ``'report'`` (default) writes
                a styled hierarchical sheet. ``'extract'`` writes a flat
                refreshable data table with named ranges and an Excel Table.
            filename: Only for ``out='excel'`` with ``target='new'`` /
                ``'append'``. The filename in the user's files dir. Must
                include ``.xlsx`` extension.
            sheet_name: Only for ``out='excel'``. Override the default
                sheet name (derived from the statement key).
            overwrite: Only for ``out='excel'``. On ``target='new'``, allow
                replacing an existing file. On ``target='append'``, allow
                replacing a colliding sheet.
        """
        # Sensible fallbacks for the MCP SSE dev flow (no report context injection).
        # In the calling layer, these are always set from Précis's report context.
        if statement is None:
            statement = "pnl"
        today = __import__("datetime").date.today()
        if period_start is None:
            period_start = f"{today.year}-01"
        if period_end is None:
            period_end = f"{today.year}-{today.month:02d}"
        if scenarios is None:
            scenarios = [{"scenario": "actuals", "alias": "Actuals"}]
        if scale is not None:
            scale = _normalise_scale(scale)
            if isinstance(scale, dict):
                return scale

        # Build context
        ctx: dict = {
            "period_start": period_start,
            "period_end": period_end,
        }
        if scale is not None:
            ctx["scale"] = scale
        if decimals is not None:
            ctx["decimals"] = decimals

        shape_error = _invalid_scenarios_error(scenarios)
        if shape_error:
            return shape_error

        # Build blocks from scenarios
        blocks = []
        for sc in scenarios:
            scenario_key = _resolve_scenario(sc["scenario"])
            alias = sc.get("alias", scenario_key)
            blocks.append({
                "model": f"statement:{statement}",
                "scenario": scenario_key,
                "alias": alias,
            })

        alias_error = _duplicate_alias_error(blocks)
        if alias_error:
            return alias_error

        request: dict = {
            "context": ctx,
            "blocks": blocks,
            # Statements span domains — use soft dimension validation
            "_strict_dimensions": False,
        }

        if filters:
            request["filters"] = filters
        if dimensions:
            request["dimensions"] = dimensions
            # Totals come from the engine as extra grains in the single retrieve
            # (detail + grand total for statements — no intermediate subtotals).
            request["grains"] = {"detail": True, "grand_total": True}

        result = _safe_execute(request, _scope=_scope)

        # Attach caption to result for render and excel paths
        if isinstance(result, dict) and "error" not in result:
            result["caption"] = _build_caption(
                "run_statement",
                statement=statement,
                scenarios=scenarios,
                period_start=period_start,
                period_end=period_end,
                filters=filters,
                dimensions=dimensions,
                result=result,
            )

        # Chart-result cache (data_ref) — Précis platform only. The open read path
        # writes no data_ref and touches no Redis; the cache is a registered
        # Précis chart-enablement step (read_tool_hooks).
        if isinstance(result, dict) and "error" not in result:
            _chart_cache = get_chart_cache()
            if _chart_cache is not None:
                data_ref = _chart_cache(
                    result=result,
                    tool_name="run_statement",
                    tool_args={
                        "statement": statement,
                        "scenarios": scenarios,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filters": filters,
                        "dimensions": dimensions,
                    },
                )
                if data_ref:
                    result["data_ref"] = data_ref

        if out == "excel":
            if isinstance(result, dict) and "error" not in result:
                dispatch = get_excel_dispatch()
                if dispatch is None:
                    return {
                        "error": "out='excel' is not available in this deployment",
                        "error_type": "unsupported",
                    }
                return dispatch.dispatch_excel_out(
                    result=result,
                    tool_name="run_statement",
                    tool_params={
                        "statement": statement,
                        "scenarios": scenarios,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filters": filters,
                        "dimensions": dimensions,
                    },
                    default_stem=statement,
                    target=target,
                    layout=layout,
                    filename=filename,
                    sheet_name=sheet_name,
                    overwrite=overwrite,
                )

        if out == "report":
            if isinstance(result, dict) and "error" in result:
                return result
            if not report_id:
                return {
                    "error": "report_id is required when out='report'",
                    "error_type": "validation",
                }
            renderer = get_output_renderer("report")
            if renderer is None:
                return {
                    "error": "out='report' is not available in this deployment",
                    "error_type": "unsupported",
                }
            try:
                from precis_mcp.auth import get_auth_context

                auth = get_auth_context()
                tool_args = {
                    "statement": statement,
                    "scenarios": scenarios,
                    "period_start": period_start,
                    "period_end": period_end,
                    "filters": filters,
                    "dimensions": dimensions,
                    "scale": scale,
                    "decimals": decimals,
                }
                return renderer(
                    result=result,
                    tool_name="run_statement",
                    tool_args=tool_args,
                    user_id=auth.user_id,
                    report_id=report_id,
                    position=position,
                )
            except RuntimeError:
                return {
                    "error": "out='report' requires authenticated agent flow",
                    "error_type": "auth",
                }
            except Exception as e:
                logger.exception("Report block write failed")
                return {
                    "error": f"Report block write failed: {e}",
                    "error_type": "execution",
                }

        return _round_values_for_consumer(result)

    @mcp.tool()
    def run_metric(
        metrics: list[str],
        scenarios: list[dict] | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        filters: dict | None = None,
        dimensions: list[str] | None = None,
        scale: int | None = None,
        decimals: int | None = None,
        out: str = "render",
        report_id: str = "",
        position: str = "end",
        target: str | None = None,
        layout: str = "report",
        filename: str | None = None,
        sheet_name: str | None = None,
        overwrite: bool = False,
        _scope=None,
    ) -> dict:
        """Run a metric breakdown / pivot report.

        All parameters except ``metrics``, ``dimensions``, and ``out`` have automatic
        defaults from the active report context. Omit them to use defaults. Pass
        explicitly to override for this call only.

        One or more **metrics** as measures, broken down by **dimensions** (rows) and
        **scenarios** (columns). Use this for any analysis that isn't a standard financial
        statement: revenue by project, utilisation by employee, headcount trends, GL drill-downs.

        Output modes:
        - ``out='render'`` (default): auto-rendered to the user as a styled table —
          add brief commentary but do NOT reproduce the table.
        - ``out='agent'``: raw data returned for your reasoning — present findings
          in your own words.
        - ``out='excel'``: generates a flattened Excel file and returns a download link.
          The data is NOT returned — if you need it for reasoning, call again with
          ``out='agent'``.
        - ``out='report'``: persist the result as a ``financial_table`` block on a
          report (``report_id`` required). Returns a confirmation only — no raw
          data. If you need the numbers, re-call with ``out='agent'``.

        Data model note: Each metric belongs to a **domain** (pnl, timesheets, payroll, gl).
        Dimensions are domain-specific — you can only break down by dimensions available
        in the metric's domain. Check Available Dimensions in the system prompt, or use
        ``list_kpis`` to see domain and available dimensions per metric.

        Args:
            metrics: List of metric keys. All must share the same domain.
            scenarios: List of scenario dicts, each with 'scenario' (registry
                key) and optional 'alias' (display label), e.g.
                ``[{"scenario": "actuals"},
                {"scenario": "budget", "alias": "Budget"},
                {"scenario": "actuals_vs_budget", "alias": "Variance"}]``.
                Use the scenario keys from **Available Scenarios** in the data
                model description (real, shifted, and generated comparison
                types). If a key is not recognised, call ``list_scenarios``
                to discover valid keys. Defaults from report context.
            period_start: Range start (YYYY-MM). Defaults from report context.
            period_end: Range end (YYYY-MM). Defaults from report context.
            filters: Dimension filters. Defaults from report context.
                Pass {} to explicitly clear all filters.
            dimensions: Dimension keys for row breakdown — the same catalogue
                dimension names used in ``filters``.
            scale: Currency scaling power. 0=units (default), 3=thousands,
                6=millions, 9=billions. Defaults from report context.
            decimals: Decimal places. Defaults from report context.
            out: Output mode — 'render' (default), 'agent', 'excel', or 'report'.
            report_id: Target report ID (required when ``out='report'``).
            position: Where to place the block when ``out='report'``:
                'end' (default), 'start', 'replace:<block_id>',
                'after:<block_id>', or 'before:<block_id>'. Use 'replace:'
                to rebase a cloned block for a new period.
            target: Only for ``out='excel'``. ``None`` (default) creates
                a new 24h-expiring registry row. ``'new'`` produces a
                persistent registry row (``expires_at=NULL``).
                ``'append'`` mutates an existing file in place — same
                file_id, fresh sheet. All three paths return
                ``files_created: [file_id]`` and surface automatically.
            layout: Only for ``out='excel'``. ``'report'`` (default) writes
                a styled flattened sheet. ``'extract'`` writes a flat
                refreshable data table with named ranges and an Excel Table.
            filename: Only for ``out='excel'`` with ``target='new'`` /
                ``'append'``. Filename in the user's files dir (``.xlsx``).
            sheet_name: Only for ``out='excel'``. Override the default
                sheet name.
            overwrite: Only for ``out='excel'``. ``target='new'`` → replace
                the file; ``target='append'`` → replace the colliding sheet.
        """
        # Sensible fallbacks for the MCP SSE dev flow (no report context injection).
        # In the calling layer, these are always set from Précis's report context.
        today = __import__("datetime").date.today()
        if period_start is None:
            period_start = f"{today.year}-01"
        if period_end is None:
            period_end = f"{today.year}-{today.month:02d}"
        if scenarios is None:
            scenarios = [{"scenario": "actuals", "alias": "Actuals"}]
        if scale is not None:
            scale = _normalise_scale(scale)
            if isinstance(scale, dict):
                return scale

        # Build context
        ctx: dict = {
            "period_start": period_start,
            "period_end": period_end,
        }
        if scale is not None:
            ctx["scale"] = scale
        if decimals is not None:
            ctx["decimals"] = decimals

        # Build blocks: one block per scenario, all metrics carried via
        # the 'metrics:' resolver ref. This matches the shape the formatter
        # expects (display_items = list of metrics, one column per block).
        shape_error = _invalid_scenarios_error(scenarios)
        if shape_error:
            return shape_error

        metric_ref = "metrics:" + ",".join(metrics)
        blocks = []
        for sc in scenarios:
            scenario_key = _resolve_scenario(sc["scenario"])
            alias = sc.get("alias", scenario_key)
            blocks.append({
                "model": metric_ref,
                "scenario": scenario_key,
                "alias": alias,
            })

        alias_error = _duplicate_alias_error(blocks)
        if alias_error:
            return alias_error

        request: dict = {
            "context": ctx,
            "blocks": blocks,
            # Metric reports use strict dimension validation
            "_strict_dimensions": True,
        }

        if filters:
            request["filters"] = filters
        if dimensions:
            request["dimensions"] = dimensions
            # Totals come from the engine as extra grains in the single retrieve:
            # detail + the right-to-left subtotal ladder + grand total.
            request["grains"] = {"detail": True, "subtotals": True, "grand_total": True}

        result = _safe_execute(request, _scope=_scope)

        # Attach caption to result for render and excel paths
        if isinstance(result, dict) and "error" not in result:
            result["caption"] = _build_caption(
                "run_metric",
                metrics=metrics,
                scenarios=scenarios,
                period_start=period_start,
                period_end=period_end,
                filters=filters,
                dimensions=dimensions,
                result=result,
            )

        # Chart-result cache (data_ref) — Précis platform only. The open read path
        # writes no data_ref and touches no Redis; the cache is a registered
        # Précis chart-enablement step (read_tool_hooks).
        if isinstance(result, dict) and "error" not in result:
            _chart_cache = get_chart_cache()
            if _chart_cache is not None:
                data_ref = _chart_cache(
                    result=result,
                    tool_name="run_metric",
                    tool_args={
                        "metrics": metrics,
                        "scenarios": scenarios,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filters": filters,
                        "dimensions": dimensions,
                    },
                )
                if data_ref:
                    result["data_ref"] = data_ref

        if out == "excel":
            if isinstance(result, dict) and "error" not in result:
                dispatch = get_excel_dispatch()
                if dispatch is None:
                    return {
                        "error": "out='excel' is not available in this deployment",
                        "error_type": "unsupported",
                    }
                metric_slug = "_".join(metrics[:3])
                if dimensions:
                    metric_slug += "_by_" + "_".join(dimensions[:2])
                return dispatch.dispatch_excel_out(
                    result=result,
                    tool_name="run_metric",
                    tool_params={
                        "metrics": metrics,
                        "scenarios": scenarios,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filters": filters,
                        "dimensions": dimensions,
                    },
                    default_stem=metric_slug,
                    target=target,
                    layout=layout,
                    filename=filename,
                    sheet_name=sheet_name,
                    overwrite=overwrite,
                )


        if out == "report":
            if isinstance(result, dict) and "error" in result:
                return result
            if not report_id:
                return {
                    "error": "report_id is required when out='report'",
                    "error_type": "validation",
                }
            renderer = get_output_renderer("report")
            if renderer is None:
                return {
                    "error": "out='report' is not available in this deployment",
                    "error_type": "unsupported",
                }
            try:
                from precis_mcp.auth import get_auth_context

                auth = get_auth_context()
                tool_args = {
                    "metrics": metrics,
                    "scenarios": scenarios,
                    "period_start": period_start,
                    "period_end": period_end,
                    "filters": filters,
                    "dimensions": dimensions,
                    "scale": scale,
                    "decimals": decimals,
                }
                return renderer(
                    result=result,
                    tool_name="run_metric",
                    tool_args=tool_args,
                    user_id=auth.user_id,
                    report_id=report_id,
                    position=position,
                )
            except RuntimeError:
                return {
                    "error": "out='report' requires authenticated agent flow",
                    "error_type": "auth",
                }
            except Exception as e:
                logger.exception("Report block write failed")
                return {
                    "error": f"Report block write failed: {e}",
                    "error_type": "execution",
                }

        return _round_values_for_consumer(result)

    # ------------------------------------------------------------------
    # Hierarchy / utility tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_hierarchy(
        query: str | None = None,
        dimension: str | None = None,
        out: str = "agent",
    ) -> dict:
        """[UTILITY] Search or list dimension members and hierarchy nodes.

        Two modes of operation:

        - **Search** (``query`` provided): filters dimension members and hierarchy
          nodes by free-text match. Use to find specific items by name or code.
        - **List** (``query`` omitted or ``None``): returns all members of the
          specified dimension (up to 200 records + 100 hierarchy nodes).
          Always pass ``dimension`` when listing to avoid a cross-dimension dump.

        Returns two sections:

        - **records**: leaf-level rows from master data tables. Each record shows
          the dimension key as the filter key with exact filter_usage.
        - **hierarchy_nodes**: rollup hierarchy nodes (for ragged hierarchies).
          Use these node_id values with the hierarchy key as the filter key
          (e.g. filters: {"org_structure": "Cloud & Infrastructure"}). Some
          verticals prefix node_ids (e.g. "dept:..."); always use the exact
          node_id returned in the result rather than guessing a prefix.

        Args:
            query: Free-text search string, e.g. 'cloud', 'Smith', 'T&M'.
                Omit to list all members of the dimension.
            dimension: Dimension key to restrict results (e.g. 'cost_centre',
                'employee', 'project', 'client', 'client_portfolio').
                Recommended when listing without a query.
            out: Output mode — 'agent' (default) or 'excel'. When 'excel',
                generates an Excel file with two sheets (Records + Hierarchy Nodes).
        """
        client = get_clickhouse_client()
        records: list[dict] = []
        hierarchy_nodes: list[dict] = []

        # Least-restrictive read scope: a member is visible when at least one
        # scenario the caller can read allows it. No readable scenario at all
        # means nothing is visible.
        from precis_mcp.auth import get_auth_context
        from precis_mcp.engine.scope_enforcer import union_read_member_sets

        permissions = get_auth_context().permissions
        deny_all = (
            permissions is not None
            and not permissions.is_admin
            and not any(
                "read" in sp.tool_scopes
                for sp in permissions.scenarios.values()
            )
        )
        permitted_members = union_read_member_sets(
            permissions, ref.current, client,
        )

        dims_to_search = {} if deny_all else ref.current.dimensions
        if dimension and dimension in dims_to_search:
            selected = {dimension: dims_to_search[dimension]}
            # Also include ragged hierarchies built on this leaf dimension, so
            # searching a leaf (e.g. "cost_centre") surfaces its rollup nodes
            # (e.g. the "org_structure" department/division nodes) in one call.
            for k, d in dims_to_search.items():
                if d.is_ragged and d.leaf_dimension == dimension:
                    selected[k] = d
            dims_to_search = selected

        for dim_key, dim in dims_to_search.items():
            # --- Search leaf dimensions (have source tables) ---
            if dim.is_leaf and dim.source:
                searchable_cols: list[str] = [dim.source.key_column]
                for attr_col in dim.source.attribute_mapping.values():
                    if attr_col and attr_col not in searchable_cols:
                        searchable_cols.append(attr_col)
                # Also include parent FK columns for richer search
                for parent_rel in dim.parents.values():
                    if parent_rel.source_column not in searchable_cols:
                        searchable_cols.append(parent_rel.source_column)

                select_cols = list(searchable_cols)

                conditions: list[str] = []
                params: dict = {}
                if permitted_members is not None and dim_key in permitted_members:
                    conditions.append(
                        f"toString({dim.source.key_column}) IN ({{scope_ids:Array(String)}})"
                    )
                    params["scope_ids"] = sorted(permitted_members[dim_key])
                if query:
                    or_clauses = [f"ilike(toString({col}), {{q:String}})" for col in searchable_cols]
                    conditions.append("(" + " OR ".join(or_clauses) + ")")
                    params["q"] = f"%{query}%"
                where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
                sql = (
                    f"SELECT DISTINCT {', '.join(select_cols)} "
                    f"FROM {dim.source.table} "
                    f"{where}"
                    f"ORDER BY {select_cols[0]} "
                    f"LIMIT {50 if query else 200}"
                )

                try:
                    result = client.query(sql, parameters=params)
                    for row in result.result_rows:
                        row_dict = dict(zip(result.column_names, row))
                        # Display attribute
                        display_val = ""
                        if dim.display_attribute and dim.display_attribute in dim.source.attribute_mapping:
                            display_col = dim.source.attribute_mapping[dim.display_attribute]
                            display_val = str(row_dict.get(display_col, ""))

                        entry: dict = {
                            "dimension": dim_key,
                            "code": str(row_dict.get(dim.source.key_column, "")),
                        }
                        if display_val:
                            entry["display_name"] = display_val
                        # Include parent dimension values
                        for parent_rel in dim.parents.values():
                            val = row_dict.get(parent_rel.source_column)
                            if val is not None:
                                entry[parent_rel.source_column] = str(val)
                        records.append(entry)
                except Exception:
                    continue

            # --- Search ragged hierarchy nodes ---
            if dim.is_ragged and dim.leaf_dimension:
                if dim.ragged_source and dim.ragged_source.type == "provided" and dim.ragged_source.table:
                    hierarchy_view = dim.ragged_source.table
                else:
                    hierarchy_view = f"semantic.dim_{dim.leaf_dimension}_{dim_key}"

                node_conditions = ["node_type != 'all'"]
                node_params: dict = {}
                if (
                    permitted_members is not None
                    and dim.leaf_dimension in permitted_members
                ):
                    resolution = dim._transitive.get(dim.leaf_dimension)
                    if resolution is None:
                        # Cannot map nodes to permitted leaves — fail closed.
                        node_conditions.append("1 = 0")
                    else:
                        if dim.ragged_source and dim.ragged_source.type == "provided" and dim.ragged_source.table:
                            rollup_view = dim.ragged_source.table
                        else:
                            rollup_view = f"semantic.dim_{dim.leaf_dimension}_{dim_key}_rollup"
                        node_conditions.append(
                            f"node_id IN (SELECT DISTINCT node_id FROM {rollup_view} "
                            f"WHERE toString({resolution.leaf_key_column}) "
                            f"IN ({{scope_leaf_ids:Array(String)}}))"
                        )
                        node_params["scope_leaf_ids"] = sorted(
                            permitted_members[dim.leaf_dimension]
                        )

                try:
                    if query:
                        node_conditions.append(
                            "(ilike(node_name, {q:String}) OR ilike(node_id, {q:String}))"
                        )
                        node_params["q"] = f"%{query}%"
                        node_sql = (
                            f"SELECT node_id, node_name, display_name, node_type "
                            f"FROM {hierarchy_view} "
                            f"WHERE {' AND '.join(node_conditions)} "
                            f"ORDER BY level, node_name "
                            f"LIMIT 20"
                        )
                    else:
                        node_sql = (
                            f"SELECT node_id, node_name, display_name, node_type "
                            f"FROM {hierarchy_view} "
                            f"WHERE {' AND '.join(node_conditions)} "
                            f"ORDER BY sort_key, level "
                            f"LIMIT 100"
                        )
                    node_result = client.query(node_sql, parameters=node_params)
                    for row in node_result.result_rows:
                        node_type = str(row[3])
                        node_id = str(row[0])
                        # For leaf-level nodes, also show direct dimension filter
                        if node_type == dim.leaf_dimension:
                            filter_usage = f'filters: {{"{dim.leaf_dimension}": "{node_id}"}}'
                        else:
                            filter_usage = f'filters: {{"{dim_key}": "{node_id}"}}'
                        hierarchy_nodes.append({
                            "dimension": dim.leaf_dimension,
                            "hierarchy": dim_key,
                            "node_id": node_id,
                            "node_name": str(row[1]),
                            "display_name": str(row[2]),
                            "node_type": node_type,
                            "filter_usage": filter_usage,
                        })
                except Exception:
                    continue

        result = {"records": records, "hierarchy_nodes": hierarchy_nodes}

        if out == "excel":
            dispatch = get_excel_dispatch()
            if dispatch is None:
                return {
                    "error": "out='excel' is not available in this deployment",
                    "error_type": "unsupported",
                }
            return dispatch.dispatch_hierarchy_excel(
                result=result,
                query=query,
                dimension=dimension,
            )

        return result

    @mcp.tool()
    def resolve_to_cc_list(filter_key: str, filter_value: str) -> list[str]:
        """[UTILITY] Resolve a dimension filter to its leaf cost centre IDs.

        Use this to preview which cost centres a filter resolves to before running
        a report or analysis tool.

        Args:
            filter_key: Filter key — a rollup hierarchy name (e.g. 'org_structure')
                        or a level column name (e.g. 'division', 'department')
            filter_value: The value to resolve — use search_hierarchy to discover valid values
        """
        client = get_clickhouse_client()
        resolved = resolve_filters({filter_key: filter_value}, ref.current, client)
        # Return all leaf IDs from all resolved dimensions
        result: list[str] = []
        for values in resolved.values():
            result.extend(values)
        return result

    @mcp.tool()
    def list_variants(scenario_id: str) -> dict:
        """List all variant/fork scenarios of a given parent scenario.

        Returns scenarios whose ``variant_of`` field matches the given
        scenario_id. Useful for finding alternative budget versions.

        Args:
            scenario_id: The parent scenario to find variants of.
        """
        ch = get_clickhouse_client()
        from precis_mcp.auth import get_auth_context
        from precis_mcp.engine.scenario_store import ScenarioStore

        permissions = get_auth_context().permissions

        def _visible(sid: str) -> bool:
            if permissions is None or permissions.is_admin:
                return True
            sp = permissions.scenarios.get(sid)
            return sp is not None and "read" in sp.tool_scopes

        store = ScenarioStore(ch)
        variants = [
            {
                "scenario_id": s.scenario_id,
                "alias": s.alias,
                "name": s.name,
                "status": s.status,
                "description": s.description,
                "created_by": s.created_by,
                "created_at": str(s.created_at),
                "base_scenario": s.base_scenario,
            }
            for s in store.list_variants(scenario_id)
            if _visible(s.scenario_id)
        ]

        return {
            "parent_scenario": scenario_id,
            "variants": variants,
            "count": len(variants),
        }

    @mcp.tool()
    def reload_catalogue() -> str:
        """Reload the metric catalogue from disk without restarting the server.

        Use this after editing any YAML file in instance/catalogue/ to make
        changes live immediately.
        """
        return ref.reload()

    # Expose the two report-running closures to ``get_refresh_dispatch`` so
    # ``refresh_workbook`` can re-invoke them with stored source params.
    global _RUN_STATEMENT, _RUN_METRIC
    _RUN_STATEMENT = run_statement
    _RUN_METRIC = run_metric
