# Excel function reference

All functions live in the **`PRECIS`** namespace and **spill** their result into
the grid. Style a spilled table with the **Format** ribbon button; re-fetch every
cell with **Refresh**.

!!! note "Argument separator"
    Examples below use `;`. Excel uses your locale's list separator — `,` in some
    locales, `;` in others. Skip an optional middle argument with two separators,
    e.g. `…;;…`. Trailing optional arguments can simply be omitted.

Discover valid keys with [`=PRECIS.KPIS()`](#preciskpis) (metric keys),
[`=PRECIS.SCENARIOS()`](#precisscenarios) (scenario keys), and
[`=PRECIS.HIERARCHY(…)`](#precishierarchy) (dimension members).

---

## PRECIS.STATEMENT

A financial statement — rows are statement lines (Revenue → EBITDA), columns are
scenarios.

```
=PRECIS.STATEMENT(statement, periodStart, periodEnd, [scenarios], [dimensions], [filters], [scale], [decimals])
```

| Argument | Required | Description |
|---|---|---|
| `statement` | yes | Statement key, e.g. `"pnl"`, `"full_pnl"`. |
| `periodStart` | yes | Range start, `YYYY-MM`, e.g. `"2026-01"`. |
| `periodEnd` | yes | Range end, `YYYY-MM`. |
| `scenarios` | no | Comma-separated scenario keys. Optional display alias via `key as Alias`. |
| `dimensions` | no | Comma-separated dimensions to break by, e.g. `"cost_centre"`. |
| `filters` | no | `key=value` pairs, comma-separated; multi-value with `\|`. |
| `scale` | no | Currency scaling power: `0`=units, `3`=thousands, `6`=millions. |
| `decimals` | no | Decimal places. |

```excel
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals as Act,budget as Bud,actuals_vs_budget as Var")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py as YoY")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals";"cost_centre")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals";;"cost_centre=CC-100|CC-200")
=PRECIS.STATEMENT("full_pnl";"2026-01";"2026-04";"actuals";;;6;1)
```

---

## PRECIS.METRIC

A metric breakdown — one or more metrics, broken down by dimensions (rows) and
scenarios (columns).

```
=PRECIS.METRIC(metrics, periodStart, periodEnd, [dimensions], [scenarios], [filters], [layout], [scale], [decimals])
```

| Argument | Required | Description |
|---|---|---|
| `metrics` | yes | Comma-separated metric keys, e.g. `"revenue,utilisation"`. |
| `periodStart` | yes | Range start, `YYYY-MM`. |
| `periodEnd` | yes | Range end, `YYYY-MM`. |
| `dimensions` | no | Comma-separated dimensions to break by. |
| `scenarios` | no | Comma-separated scenario keys; alias via `key as Alias`. |
| `filters` | no | `key=value` pairs, comma-separated; multi-value with `\|`. |
| `layout` | no | `report` (default — styled, hierarchical) or `extract` (flat, formula-friendly leaf table). |
| `scale` | no | `0`/`3`/`6`. |
| `decimals` | no | Decimal places. |

With more than one metric **and** more than one scenario, the report layout adds
a two-row header: the metric label sits above its scenario columns (a spill can't
merge cells, so the label appears at the start of each group). The `extract`
layout returns a flat leaf table with the dimensions as columns — use it for
downstream formulas; it isn't styled by **Format**.

```excel
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"cost_centre")
=PRECIS.METRIC("revenue,utilisation";"2026-01";"2026-05";"department,cost_centre";"actuals as Act,budget as Bud")
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"project";"actuals";;"extract")
```

---

## Comparison and prior-year scenarios

Any scenario column in [`STATEMENT`](#precisstatement) or
[`METRIC`](#precismetric) can be a **generated** scenario, not just a real one:

| Key pattern | Example | Gives you |
|---|---|---|
| `<alias>_py` / `<alias>_pp` | `actuals_py` | the scenario shifted a year (`_py`, −12 months) or a period (`_pp`, −1 month) back |
| `<left>_vs_<right>` | `actuals_vs_budget` | signed variance, `left − right` |
| `<left>_vs_<right>_pct` | `actuals_vs_actuals_py_pct` | percentage variance |

Year-on-year is just a comparison against a period-shifted scenario —
`actuals_vs_actuals_py` (signed) or `actuals_vs_actuals_py_pct` (percent).
`prior_year` / `prior_period` work as aliases for `actuals_py` / `actuals_pp`.

```excel
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py as YoY")
=PRECIS.STATEMENT("pnl";"2026-01";"2026-05";"actuals,actuals_vs_actuals_py_pct as YoY%")
=PRECIS.METRIC("revenue";"2026-01";"2026-05";"cost_centre";"actuals,budget,actuals_vs_budget as Var")
```

Comparison columns are **colour-coded** favourable/unfavourable when you click
**Format**: green/red follows each metric's variance polarity (revenue up is
green, cost up is red), so a cost variance is never mis-coloured. Discover the
live keys with `=PRECIS.SCENARIOS("shifted")` and `=PRECIS.SCENARIOS("comparisons")`.

---

## PRECIS.HIERARCHY

List or search dimension members and ragged-hierarchy nodes.

```
=PRECIS.HIERARCHY(dimension, [query], [output])
```

| Argument | Required | Description |
|---|---|---|
| `dimension` | yes (to list) | Dimension key, e.g. `"cost_centre"`, `"employee"`. |
| `query` | no | Free-text search, e.g. `"cloud"`; omit to list all members. |
| `output` | no | `records` (leaf members, default) or `nodes` (ragged rollup nodes). |

```excel
=PRECIS.HIERARCHY("cost_centre")
=PRECIS.HIERARCHY("cost_centre";;"nodes")
=PRECIS.HIERARCHY("employee";"smith")
```

---

## PRECIS.KPIS

The metric catalogue as a flat table — keys, labels, domains, formats, and the
dimensions available for each. Use it to find valid metric keys for
[`=PRECIS.METRIC`](#precismetric).

```
=PRECIS.KPIS()
```

```excel
=PRECIS.KPIS()
```

---

## PRECIS.SCENARIOS

The available scenarios as a flat table — the keys/aliases to pass to
[`=PRECIS.STATEMENT`](#precisstatement) and [`=PRECIS.METRIC`](#precismetric).
Scope-filtered to what you can read.

```
=PRECIS.SCENARIOS([section])
```

| Argument | Required | Description |
|---|---|---|
| `section` | no | `real` (concrete scenarios, default), `comparisons` (generated variance keys such as `actuals_vs_budget`), `shifted` (period-shifted keys such as `actuals_py`), or `aliases` (compatibility aliases, e.g. `prior_year` → `actuals_py`). |

See [Comparison and prior-year scenarios](#comparison-and-prior-year-scenarios) for what
the `comparisons` and `shifted` keys mean and how to use them as columns.

```excel
=PRECIS.SCENARIOS()
=PRECIS.SCENARIOS("comparisons")
=PRECIS.SCENARIOS("shifted")
```