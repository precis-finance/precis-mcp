# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Transform a unified engine response into a structured ``financial_table`` block.

Open-core module: a pure ``unified result → financial_table block`` transform
with no streaming, Redis, or turn-scoped dependencies (only ``engine.formatter``
for scale labels). It is the finance-table half of the render
boundary: the open MCP transport and the SSE/report paths all build
the same block from here.

Consumed by Précis's render path:

1. the SSE emitter for the chat transcript path (``run_metric`` /
   ``run_statement`` with ``out='render'``).
2. the report block sink for the ``run_metric(out='report')`` /
   ``run_statement(out='report')`` path.
3. the Excel statement emitter.

Input is the unified result schema: top-level ``kind`` / ``dimensions`` /
``scenarios`` / ``scale`` plus a flat list of grain-tagged ``rows``, each
``{grain, dimensions, item, values}``. The one public entry point is
:func:`build_financial_table_block`; :func:`pivot_rows_to_grid` is the shared
statement-crosstab pivot, reused by the Excel statement emitter.

The returned dict is already in the shape the frontend finance-table
renderer expects (``type``, ``title``, ``columns``, ``rows``, optional
``scale_label`` / ``caption``).
"""

from __future__ import annotations

from collections import OrderedDict

from precis_mcp.cell_format import excel_number_format, favorability

EM_DASH = "—"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _row_type(item: dict) -> str:
    """Map a unified ``item.style`` to the renderer's ``row_type``."""
    style = item.get("style", "default")
    return style if style in ("header", "subtotal", "total") else "line_item"


def _scenario_columns(scenarios: list[dict]) -> list[dict]:
    """Build right-aligned scenario columns from the unified ``scenarios`` list."""
    cols: list[dict] = []
    for s in scenarios:
        col: dict = {"key": s["alias"], "label": s["alias"], "align": "right"}
        if s.get("variance"):
            col["variance"] = True
        if s.get("format"):
            col["format"] = s["format"]
        if s.get("decimals") is not None:
            col["decimals"] = s["decimals"]
        cols.append(col)
    return cols


def _values(row: dict, aliases: list[str]) -> dict:
    raw = row.get("values", {})
    return {a: raw.get(a) for a in aliases}


def _add_scale_label(result: dict, scale: int) -> None:
    if scale:
        from precis_mcp.engine.formatter import SCALE_LABELS
        label = SCALE_LABELS.get(scale, "")
        if label:
            result["scale_label"] = label


# ---------------------------------------------------------------------------
# Excel add-in enrichment
# ---------------------------------------------------------------------------


def _enrich_block_for_excel(block: dict) -> None:
    """Add resolved Excel ``nf`` per value column + sparse per-row ``alerts``.

    In-place. Lets the Excel add-in build its spill grid and apply formatting
    without re-implementing the number-format string or the variance sign rule
    (favorability) client-side — both stay single-sourced in
    ``precis_mcp.cell_format``. The add-in pivots the existing ``columns`` /
    ``rows`` into its 2-D spill matrix itself (pure layout, no logic), reading
    ``nf`` and ``alerts`` for the format pass.

    Off by default (see ``build_financial_table_block(for_excel=…)``) so the
    high-volume SSE / report paths stay lean; only the MCP render variant — the
    surface the add-in calls — turns it on. See
    docs/precis-excel-addin-spec.md §5 (the in-place enrichment supersedes the
    separate ``grid`` projection the spec originally sketched in §5.1).
    """
    columns = block.get("columns", [])
    value_cols = columns[1:] if columns else []
    for c in value_cols:
        c["nf"] = excel_number_format(c.get("format") or "currency", c.get("decimals") or 0)
    for r in block.get("rows", []):
        if r.get("row_type") == "separator":
            continue
        # Per-row number format: a statement's rows are metrics with their own
        # format (currency, percent, ratio), authoritative per cell — it overrides
        # the scenario column's default so a percent line isn't shown as currency.
        # Metrics-as-columns breakdowns carry no per-row format, so the column nf
        # still applies there.
        if r.get("format"):
            r["nf"] = excel_number_format(r["format"], r.get("decimals") or 0)
        vals = r.get("values", {})
        row_effect = r.get("variance_effect", "natural")
        alerts: dict[str, str] = {}
        for c in value_cols:
            if not c.get("variance"):
                continue
            effect = c.get("variance_effect") or row_effect
            fav = favorability(vals.get(c["key"]), effect)
            if fav is not None:
                alerts[c["key"]] = fav
        if alerts:
            r["alerts"] = alerts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_financial_table_block(
    data: dict,
    caption: dict | None = None,
    for_excel: bool = False,
) -> dict | None:
    """Transform a unified engine response into a ``financial_table`` block.

    Returns ``None`` if there is nothing to render (no scenarios or no rows).
    The unified ``item`` / ``scenarios`` carry all display fields (format,
    decimals, variance_effect, style, indent), so no catalogue lookup is needed.

    ``for_excel=True`` additionally stamps resolved Excel ``nf`` per value column
    and sparse per-row ``alerts`` (see :func:`_enrich_block_for_excel`); the MCP
    render variant the Excel add-in consumes passes it, the SSE / report paths
    don't.
    """
    if not isinstance(data, dict):
        return None
    scenarios = data.get("scenarios", [])
    rows_data = data.get("rows", [])
    if not scenarios or not rows_data:
        return None

    dimensions = data.get("dimensions", [])
    kind = data.get("kind", "metric")
    scale = data.get("scale", 0)
    desc = caption.get("description", "") if isinstance(caption, dict) else ""

    if not dimensions:
        block = _build_aggregate_table(rows_data, scenarios, desc, caption)
    elif kind == "statement":
        block = _build_crosstab_table(rows_data, scenarios, dimensions, desc, caption)
    else:
        block = _build_dimension_table(rows_data, scenarios, dimensions, desc, caption)

    _add_scale_label(block, scale)
    if for_excel:
        _enrich_block_for_excel(block)
    return block


# ---------------------------------------------------------------------------
# Case 1: no dimensions — items as rows, scenarios as columns
# ---------------------------------------------------------------------------


def _build_aggregate_table(
    rows_data: list, scenarios: list[dict], desc: str, caption: dict | None,
) -> dict:
    aliases = [s["alias"] for s in scenarios]
    columns = [{"key": "label", "label": "Line", "align": "left"}] + _scenario_columns(scenarios)

    out_rows: list[dict] = []
    for r in rows_data:
        item = r["item"]
        if item.get("separator_above"):
            out_rows.append({"row_type": "separator"})
        out_rows.append({
            "row_type": _row_type(item),
            "label": item.get("label", item.get("key", "")),
            "values": _values(r, aliases),
            "indent": item.get("indent", 0),
            "format": item.get("format", "currency"),
            "decimals": item.get("decimals"),
            "variance_effect": item.get("variance_effect", "natural"),
        })

    if desc:
        title = desc
    elif len(aliases) == 1:
        title = aliases[0]
    elif len(aliases) > 1:
        title = "Scenario Comparison"
    else:
        title = "Report"

    block: dict = {"type": "financial_table", "title": title, "columns": columns, "rows": out_rows}
    if caption:
        block["caption"] = caption
    return block


# ---------------------------------------------------------------------------
# Case 2: metric breakdown — dimension rows with grain-based subtotals/total
# ---------------------------------------------------------------------------


def _build_dimension_table(
    rows_data: list, scenarios: list[dict], dimensions: list[str], desc: str, caption: dict | None,
) -> dict:
    aliases = [s["alias"] for s in scenarios]
    dim_label = " / ".join(d.replace("_", " ").title() for d in dimensions)
    columns = [{"key": "label", "label": dim_label, "align": "left"}] + _scenario_columns(scenarios)

    detail = [r for r in rows_data if r.get("grain") == "detail"]
    subtotals = [r for r in rows_data if r.get("grain") == "subtotal"]
    grand = next((r for r in rows_data if r.get("grain") == "grand_total"), None)

    multi_metric = len({r["item"]["key"] for r in detail}) > 1

    # >1 metric → metrics become columns (grouped under the metric when there is
    # also >1 scenario). Single-metric tables keep scenarios as the columns.
    if multi_metric:
        return _build_metric_columns_table(rows_data, scenarios, dimensions, desc, caption)

    def _combo_label(r: dict) -> str:
        dv = " / ".join(str(v) for v in r["dimensions"].values()) or EM_DASH
        return f"{dv} {EM_DASH} {r['item']['label']}" if multi_metric else dv

    def _fmt(r: dict) -> str:
        return r["item"].get("format", "currency")

    def _dec(r: dict):
        return r["item"].get("decimals")

    def _ve(r: dict) -> str:
        return r["item"].get("variance_effect", "natural")

    out_rows: list[dict] = []

    if len(dimensions) >= 2:
        # Group detail rows by the leftmost dimension; attach level-1 subtotals.
        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        for r in detail:
            gk = str(r["dimensions"].get(dimensions[0], EM_DASH))
            groups.setdefault(gk, []).append(r)
        sub_by_key = {
            str(s["dimensions"].get(dimensions[0], "")): s
            for s in subtotals if len(s["dimensions"]) == 1
        }
        for gk, items in groups.items():
            st = sub_by_key.get(gk)
            out_rows.append({
                "row_type": "group_header", "label": gk,
                "values": _values(st, aliases) if st else {},
                "indent": 0, "format": _fmt(items[0]), "decimals": _dec(items[0]),
                "variance_effect": _ve(items[0]),
            })
            for r in items:
                rem = {d: r["dimensions"].get(d, "") for d in dimensions[1:]}
                label = " / ".join(str(v) for v in rem.values())
                if multi_metric:
                    label = f"{label} {EM_DASH} {r['item']['label']}"
                out_rows.append({
                    "row_type": "line_item", "label": label, "values": _values(r, aliases),
                    "indent": 1, "format": _fmt(r), "decimals": _dec(r), "variance_effect": _ve(r),
                })
            if st:
                out_rows.append({
                    "row_type": "subtotal", "label": f"Subtotal {EM_DASH} {gk}",
                    "values": _values(st, aliases), "indent": 0,
                    "format": _fmt(st), "decimals": _dec(st), "variance_effect": _ve(st),
                })
    else:
        for r in detail:
            out_rows.append({
                "row_type": "line_item", "label": _combo_label(r), "values": _values(r, aliases),
                "indent": 0, "format": _fmt(r), "decimals": _dec(r), "variance_effect": _ve(r),
            })

    if grand:
        out_rows.append({
            "row_type": "total", "label": "Total", "values": _values(grand, aliases),
            "indent": 0, "format": grand["item"].get("format", "currency"),
            "decimals": grand["item"].get("decimals"),
            "variance_effect": grand["item"].get("variance_effect", "natural"),
        })

    if desc:
        title = f"{desc} {EM_DASH} By {dim_label}"
    elif len(aliases) > 1:
        title = f"Scenario Comparison {EM_DASH} By {dim_label}"
    else:
        title = f"By {dim_label}"

    block: dict = {"type": "financial_table", "title": title, "columns": columns, "rows": out_rows}
    if caption:
        block["caption"] = caption
    return block


# ---------------------------------------------------------------------------
# Case 2b: metric breakdown with >1 metric — metrics become columns
# ---------------------------------------------------------------------------


def _build_metric_columns_table(
    rows_data: list, scenarios: list[dict], dimensions: list[str], desc: str, caption: dict | None,
) -> dict:
    """Metric breakdown with >1 metric: metrics across columns.

    Columns are ordered metric-outer, scenario-inner. With >1 scenario the
    metric label spans a top header row (``group``) and each scenario sits
    beneath it; with a single scenario the metric label *is* the column. The
    per-metric display attributes (``format`` / ``decimals`` /
    ``variance_effect``) live on the column, so rows carry dimension labels and
    a values map only. Rows keep the same leftmost-dimension grouping +
    subtotals as the single-metric layout — and because each subtotal/grand row
    is merged across all metrics here, the multi-metric subtotal collapse of the
    old label-appending layout no longer happens.
    """
    aliases = [s["alias"] for s in scenarios]
    multi_block = len(aliases) > 1
    scen_by_alias = {s["alias"]: s for s in scenarios}

    # Metric order + per-metric display attributes (first occurrence wins —
    # matches the formatter's item emission order).
    metric_info: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows_data:
        item = r["item"]
        metric_info.setdefault(item["key"], item)
    metrics = list(metric_info.keys())

    def _col_key(mkey: str, alias: str) -> str:
        return f"{mkey}|{alias}"

    dim_label = " / ".join(d.replace("_", " ").title() for d in dimensions)
    columns: list[dict] = [{"key": "label", "label": dim_label, "align": "left"}]
    for mkey in metrics:
        minfo = metric_info[mkey]
        for alias in aliases:
            scen = scen_by_alias.get(alias, {})
            col: dict = {
                "key": _col_key(mkey, alias),
                "label": alias if multi_block else minfo.get("label", mkey),
                "align": "right",
                "format": minfo.get("format", "currency"),
                "variance_effect": minfo.get("variance_effect", "natural"),
            }
            if multi_block:
                col["group"] = minfo.get("label", mkey)
            if minfo.get("decimals") is not None:
                col["decimals"] = minfo["decimals"]
            # A variance-% scenario formats its whole column as percent,
            # overriding the metric's own format/decimals.
            if scen.get("format") == "percent":
                col["format"] = "percent"
                col["decimals"] = scen.get("decimals", 1)
            if scen.get("variance"):
                col["variance"] = True
            columns.append(col)

    # Merge the flat (dimension × metric) rows into one row per
    # (grain, dimensions), with values spanning every metric × scenario column.
    merged: "OrderedDict[tuple, dict]" = OrderedDict()
    for r in rows_data:
        dkey = tuple(sorted(r["dimensions"].items()))
        gk = (r.get("grain"), dkey)
        entry = merged.setdefault(
            gk, {"grain": r.get("grain"), "dimensions": r["dimensions"], "values": {}}
        )
        mkey = r["item"]["key"]
        for alias, v in r.get("values", {}).items():
            entry["values"][_col_key(mkey, alias)] = v

    detail = [e for e in merged.values() if e["grain"] == "detail"]
    subtotals = [e for e in merged.values() if e["grain"] == "subtotal"]
    grand = next((e for e in merged.values() if e["grain"] == "grand_total"), None)

    out_rows: list[dict] = []
    if len(dimensions) >= 2:
        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        for e in detail:
            gk = str(e["dimensions"].get(dimensions[0], EM_DASH))
            groups.setdefault(gk, []).append(e)
        sub_by_key = {
            str(s["dimensions"].get(dimensions[0], "")): s
            for s in subtotals if len(s["dimensions"]) == 1
        }
        for gk, entries in groups.items():
            st = sub_by_key.get(gk)
            out_rows.append({
                "row_type": "group_header", "label": gk,
                "values": st["values"] if st else {}, "indent": 0,
            })
            for e in entries:
                rem = {d: e["dimensions"].get(d, "") for d in dimensions[1:]}
                out_rows.append({
                    "row_type": "line_item",
                    "label": " / ".join(str(v) for v in rem.values()),
                    "values": e["values"], "indent": 1,
                })
            if st:
                out_rows.append({
                    "row_type": "subtotal", "label": f"Subtotal {EM_DASH} {gk}",
                    "values": st["values"], "indent": 0,
                })
    else:
        for e in detail:
            out_rows.append({
                "row_type": "line_item",
                "label": " / ".join(str(v) for v in e["dimensions"].values()) or EM_DASH,
                "values": e["values"], "indent": 0,
            })

    if grand:
        out_rows.append({
            "row_type": "total", "label": "Total", "values": grand["values"], "indent": 0,
        })

    if desc:
        title = f"{desc} {EM_DASH} By {dim_label}"
    elif multi_block:
        title = f"Scenario Comparison {EM_DASH} By {dim_label}"
    else:
        title = f"By {dim_label}"

    block: dict = {"type": "financial_table", "title": title, "columns": columns, "rows": out_rows}
    if caption:
        block["caption"] = caption
    return block


# ---------------------------------------------------------------------------
# Case 3: statement breakdown — crosstab (dim-combos → columns, lines → rows)
# ---------------------------------------------------------------------------


def pivot_rows_to_grid(rows_data: list, dimensions: list[str]) -> dict:
    """Pivot flat statement rows into a line × dimension-combo grid.

    Shared by the render crosstab and the Excel statement emitter. Returns:
      - ``item_order`` / ``items``: statement-line order + their `item` dicts;
      - ``columns``: ordered column descriptors, **grand total first** then each
        detail dim-combo, each ``{"id", "label", "is_total"}``;
      - ``values``: ``{(col_id, item_key, alias): value}``.
    """
    item_order: list[str] = []
    items: dict[str, dict] = {}
    detail_cols: "OrderedDict[str, str]" = OrderedDict()  # col_id -> label
    values: dict[tuple, object] = {}

    def _combo_id(dim_values: dict) -> str:
        return "|".join(str(dim_values.get(d, "")) for d in dimensions)

    def _combo_label(dim_values: dict) -> str:
        return " / ".join(str(v) for v in dim_values.values()) or EM_DASH

    has_total = False
    for r in rows_data:
        item = r["item"]
        ik = item["key"]
        if ik not in items:
            items[ik] = item
            item_order.append(ik)
        grain = r.get("grain")
        if grain == "grand_total":
            has_total = True
            col_id = "__total__"
        else:
            col_id = _combo_id(r["dimensions"])
            if col_id not in detail_cols:
                detail_cols[col_id] = _combo_label(r["dimensions"])
        for alias, v in r.get("values", {}).items():
            values[(col_id, ik, alias)] = v

    columns: list[dict] = []
    if has_total:
        columns.append({"id": "__total__", "label": "Total", "is_total": True})
    for col_id, label in detail_cols.items():
        columns.append({"id": col_id, "label": label, "is_total": False})

    return {"item_order": item_order, "items": items, "columns": columns, "values": values}


def _build_crosstab_table(
    rows_data: list, scenarios: list[dict], dimensions: list[str], desc: str, caption: dict | None,
) -> dict:
    aliases = [s["alias"] for s in scenarios]
    multi_block = len(aliases) > 1
    grid = pivot_rows_to_grid(rows_data, dimensions)

    columns: list[dict] = [{"key": "label", "label": "Line", "align": "left"}]
    col_meta = {s["alias"]: s for s in scenarios}

    def _col_key(col_label: str, alias: str) -> str:
        return f"{col_label}|{alias}" if multi_block else col_label

    # Column order: grand total first (user preference), then dim-combos. Per scenario.
    for c in grid["columns"]:
        for alias in aliases:
            key = _col_key(c["label"], alias)
            label = f"{c['label']} {EM_DASH} {alias}" if multi_block else c["label"]
            col: dict = {"key": key, "label": label, "align": "right"}
            if col_meta.get(alias, {}).get("variance"):
                col["variance"] = True
            columns.append(col)

    rows: list[dict] = []
    for ik in grid["item_order"]:
        item = grid["items"][ik]
        if item.get("separator_above"):
            rows.append({"row_type": "separator"})
        row_values: dict = {}
        for c in grid["columns"]:
            for alias in aliases:
                row_values[_col_key(c["label"], alias)] = grid["values"].get((c["id"], ik, alias))
        rows.append({
            "row_type": _row_type(item),
            "label": item.get("label", ik),
            "values": row_values,
            "indent": item.get("indent", 0),
            "format": item.get("format", "currency"),
            "decimals": item.get("decimals"),
            "variance_effect": item.get("variance_effect", "natural"),
        })

    dim_label = " / ".join(d.replace("_", " ").title() for d in dimensions) if dimensions else "Group"
    if desc:
        title = f"{desc} {EM_DASH} By {dim_label}"
    elif multi_block:
        title = f"Scenario Comparison {EM_DASH} By {dim_label}"
    else:
        title = f"By {dim_label}"

    block: dict = {"type": "financial_table", "title": title, "columns": columns, "rows": rows}
    if caption:
        block["caption"] = caption
    return block
