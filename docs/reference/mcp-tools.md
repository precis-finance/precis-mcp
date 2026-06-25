# MCP tool reference

The tools an MCP client sees on the authenticated `/mcp` endpoint, with their
parameters and behaviours. The single-user dev server
([quickstart](../getting-started/quickstart.md)) serves the same underlying
tools over Streamable HTTP. Every tool is **read-only**.

## Conventions that apply to every tool

- **Auth and scope.** Every call carries the caller's bearer token. Tools
  that take a scenario reference (`scenarios`, `scenario_id`) are checked
  against the caller's [profile](../configuration/user-profiles.md) before
  execution, and the profile's domain/dimension scope is applied *inside*
  the query — out-of-scope rows simply don't appear.
- **Errors are data.** Tools don't raise; failures return
  `{"error": "...", "error_type": "..."}` so the calling model can read the
  message and correct itself.
- **Concurrency is bounded.** A burst of read calls — e.g. a spreadsheet
  recalculating many cells at once — is capped per user (and globally) so it
  can't overload the read layer. Over the cap, a call returns a retryable
  "busy" tool error rather than queueing indefinitely; retry shortly. A
  well-behaved client keeps its own in-flight requests below the cap and never
  sees it. See the [concurrency caps](../configuration/environment-variables.md#read-concurrency-the-mcp-read-path).
- **Results arrive on both channels.** The JSON result is returned as
  `structuredContent` and as JSON text in `content` — a client without
  widget support loses nothing but the rendering.
- **Names are catalogue keys.** Metric keys, statement names, dimension
  keys, and scenario aliases all come from *your*
  [catalogue](../configuration/catalogue-and-semantic.md) and scenario
  registry. The discovery tools below return the valid values; the listings
  here use the bundled demo model for examples.

## Variants and widgets

A widget is bound to a tool *definition*, so the two reporting tools are
advertised twice — once rendering, once raw:

| Advertised name | Returns |
|---|---|
| `run_statement` / `run_metric` | the formatted table; linked to the **financial-table** widget |
| `run_statement_data` / `run_metric_data` | the raw figures, for the model to reason over |
| `inspect_rows` | row detail; linked to the **inspection-grid** widget |
| everything else | plain data |

Widgets follow the MCP Apps extension (`_meta.ui.resourceUri` on the tool,
bundle fetched via `resources/read`); a widget is only advertised when its
bundle is built. Hosts that don't render widgets still get the full result as
JSON. Three parameters are stripped from every advertised schema (`out`,
`report_id`, `position` — output paths that don't exist on this transport).
The reporting tools also advertise a few Excel-related parameters (`target`,
`layout`, `filename`, `sheet_name`, `overwrite`); they belong to the Précis
platform's Excel output mode and have no effect here.

## Orientation

### `precis_orientation` — call first

No parameters. Returns the orientation text for this deployment: the data
model (scenarios, metrics, statements, dimensions), how the tool variants
relate, and usage guidance. It exists because hosted clients drop or truncate
the MCP `initialize` instructions — a tool *result* is the one channel that
reliably reaches the model. A well-behaved client calls it once before
composing queries.

## Discovery

### `list_scenarios`

No parameters. The scenarios the caller may read — id, alias, name, kind,
status — plus the generated comparison vocabulary (variance and shifted
views). Scenarios the caller's profile doesn't grant are omitted entirely.

### `list_kpis`

No parameters. The metric catalogue: every metric's key, label, format,
domain, and description, plus per domain the `dimension_keys` (one list of
catalogue dimension names, each valid in both `filters` and `dimensions`) and
`axis_only_dimensions` (inline federated axes valid only in `dimensions`). This
is the map for composing `run_metric` calls.

### `search_hierarchy`

| Parameter | Type | Notes |
|---|---|---|
| `query` | string, optional | Free-text match on names/codes (`"cloud"`, `"Smith"`). Omit to list a whole dimension. |
| `dimension` | string, optional | Restrict to one dimension key (`cost_centre`, `employee`, …). Recommended when listing. |

Finds valid member ids before filtering. Returns `records` (leaf members,
with the exact filter key to use) and `hierarchy_nodes` (rollup nodes —
filter with the node id exactly as returned). Visibility is scoped: a member
is returned only if at least one readable scenario allows it.

### `list_variants`

| Parameter | Type | Notes |
|---|---|---|
| `scenario_id` | string, required | Scenario-gated. |

What-if variants of a scenario, where the model defines them.

## Reporting

### `run_statement` / `run_statement_data`

A financial statement — rows are the statement's lines, columns are
scenarios, optionally crossed with a dimension breakdown.

| Parameter | Type | Notes |
|---|---|---|
| `statement` | string | Statement key from the catalogue (defaults to `pnl`). |
| `scenarios` | list of objects | Each `{"scenario": "<key>", "alias": "<display label>"}` — registry keys, variance keys, shifted views. Scenario-gated. |
| `period_start` / `period_end` | string | `YYYY-MM` range. |
| `filters` | object | Dimension key → member id(s). Keys from `dimension_keys`; member ids from `search_hierarchy`. |
| `dimensions` | list of strings | Breakdown dimension keys — the same `dimension_keys` (e.g. `["period"]`, `["cost_centre"]`). |
| `scale` | int | Currency scaling power: `0` units, `3` thousands, `6` millions. |
| `decimals` | int | Decimal places. |

A statement may span several domains; `period` and `cost_centre` break down
every line, other dimensions break down only the lines from compatible
domains (the rest show aggregates).

### `run_metric` / `run_metric_data`

One or more metrics broken down by dimensions — anything that isn't a
standard statement: revenue by project, utilisation by employee, a GL
drill-down.

| Parameter | Type | Notes |
|---|---|---|
| `metrics` | list of strings, required | Metric keys — all from the same domain (`list_kpis` shows each metric's domain). |
| *(rest)* | | Identical to `run_statement`: `scenarios`, `period_start`/`period_end`, `filters`, `dimensions`, `scale`, `decimals`. |

Rows are the dimension members; columns are metrics × scenarios. Breakdown
dimensions must be available in the metrics' domain.

## Row-level inspection

### `list_inspection_sources`

No parameters. The row-level sources the catalogue exposes for
drill-through (domains with `inspect_enabled: true`).

### `get_inspection_schema`

| Parameter | Type | Notes |
|---|---|---|
| `source_key` | string, required | From `list_inspection_sources`. |

The columns and filterable dimensions of one inspection source — call it
before composing an `inspect_rows` query.

### `inspect_rows`

The drill-through from a figure to the rows behind it.

| Parameter | Type | Notes |
|---|---|---|
| `source_key` | string, required | From `list_inspection_sources`. |
| `scenario_id` | string, required | Scenario-gated — required so the permission check runs before any rows are read. |
| `filters` | object | Semantic dimension keys from the source's schema. |
| `columns` | list of strings | Restrict output columns (must be within the source's configured `inspect_columns`). |
| `limit` | int | Row cap; the server enforces a hard ceiling (`INSPECTION_ROW_CAP`, default 10 000). |
| `period_start` / `period_end` | string | `YYYY-MM` range. |

Returns a capped sample of rows plus the inspection grid for rendering
hosts. Profile scope applies to the rows themselves.

## Ingestion status

Data-freshness questions, answerable by any user from the client — "is April
in yet?", "when was this last loaded?". These read the
[ingestion](../configuration/ingestion.md) configuration and audit trail;
they carry no financial figures and no scenario parameter.

### `list_load_history`

| Parameter | Type | Notes |
|---|---|---|
| `binding_id` | string, optional | Restrict to one data feed. |
| `dataset_id` | string, optional | Restrict to one dataset across sources. |
| `period` | string, optional | One accounting period. |
| `status` | string, optional | `running`, `success`, or a `failed_*` bucket. |
| `limit` | int | Default 50, capped at 200. Most recent first. |

Load attempts from the `load_history` audit trail; filters AND together.

### `get_load_status`

| Parameter | Type | Notes |
|---|---|---|
| `load_id` | string, required | From `list_load_history`. |

One load's full detail — timestamps, status, rows landed, error message.

### `list_bindings`

| Parameter | Type | Notes |
|---|---|---|
| `source_id` | string, optional | Restrict to one source. |
| `target` | string, optional | Restrict to one `live.*` target. |

The configured data feeds with their schedules.

### `get_binding`

| Parameter | Type | Notes |
|---|---|---|
| `binding_id` | string, required | From `list_bindings`. |

One feed's full configuration: source, target, schedule, extract parameters
(operator metadata — the extract query is visible here, credentials never
are; they live in environment variables, not in the configuration).

## A typical first session

```text
precis_orientation                  → how this deployment's model works
list_scenarios                      → "actuals" and "budget" exist
list_kpis                           → revenue lives in domain pnl; can break by cost_centre
search_hierarchy(dimension="cost_centre", query="cloud")
                                    → the member ids worth filtering on
run_metric_data(metrics=["revenue"], scenarios=[{"scenario": "actuals"}],
                dimensions=["cost_centre"], period_start="2026-01",
                period_end="2026-03")
                                    → the figures
inspect_rows(source_key="gl", scenario_id="actuals",
             filters={"cost_centre": "CC-CLOUD-01"})
                                    → the rows behind the number
```

## Related

- [Adding read tools](../development/adding-read-tools.md) — the developer
  contract for extending this surface.
- [User profiles & permissions](../configuration/user-profiles.md) — the
  scoping every call above passes through.
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md) —
  where the keys these tools accept are defined.
