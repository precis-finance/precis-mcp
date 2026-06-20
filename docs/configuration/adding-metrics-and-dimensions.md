# Adding metrics & dimensions

This guide is the implementation contract for extending your instance's data
model: metrics, dimensions, statements, and domains. If you haven't read
[Catalogue & semantic model](catalogue-and-semantic.md) yet,
start there — it walks one example end to end and explains the two layers.
This page assumes that background and adds what you need to make changes
safely: the invariants, the two-backend differences, a worked example, the
traps, and a pre-flight checklist.

Scenarios are deliberately out of scope — they are not catalogue entities; see
[What this guide deliberately doesn't cover](#what-this-guide-deliberately-doesnt-cover).

---

## How a catalogue change flows through the engine

The catalogue is loaded at startup and drives both the model metadata clients
discover and the metric-engine runtime.

```
Catalogue YAML
  -> load_catalogue / validate_catalogue
  -> list_kpis metadata
  -> run_metric / run_statement request
  -> resolver
  -> filter + scope resolution
  -> ClickHouse or Ibis retriever
  -> transformer
  -> formatter
```

**Catalogue schema and validation** live in `precis_mcp/engine/catalogue.py`.
The loader reads every `.yml` file in the catalogue root (`instance/catalogue/`),
parses models such as `BaseMetric`, `DerivedMetric`, `Dimension`,
`CubeDimension`, `Statement`, and `DomainCatalogue`, computes dimension
transitive closure, and rejects invalid cross-references before the process
accepts traffic. Scenarios are not parsed from catalogue YAML — they live in
the `semantic.scenarios` table (seeded from `instance/scenarios.yml` by the
provisioner) and are surfaced at runtime by `ScenarioRegistry`
(`precis_mcp/engine/scenario_registry.py`).

**Request resolution** lives in `precis_mcp/engine/resolver.py`. It expands
statement references into metrics, expands derived-metric dependencies
(topologically sorted), resolves scenario references through
`ScenarioRegistry`, infers the metric domain, and validates that requested
breakdown dimensions are available in that domain.

**Filter and scope resolution** live in `precis_mcp/engine/filter_resolver.py`
and `precis_mcp/engine/scope_enforcer.py`. User filters and security scope use
catalogue dimension keys, not physical source-view column names. The resolver
maps those keys to concrete leaf IDs and then to the domain's source-view
columns.

**Retrieval** has two backends. `precis_mcp/engine/retriever.py` builds
ClickHouse SQL against `semantic.*` views. `precis_mcp/engine/ibis_retriever.py`
builds Ibis expressions for federated domains. Both return the same row shape
to the transformer.

**Transformation and formatting** live in `precis_mcp/engine/transformer.py`
and `precis_mcp/engine/formatter.py`. Derived metrics and generated
variance/shifted scenarios are evaluated after retrieval. Formatting applies
metric format/style metadata and optional dimension display/sort attributes.

**Client discoverability** is driven by the `list_kpis` tool in
`precis_mcp/tools/read_tools.py`. New metrics and dimensions surface to MCP
clients automatically when the catalogue metadata is correct — there is
nothing else to register.

---

## Invariants

- **Catalogue keys are API contracts.** A metric key is the name clients pass
  to `run_metric`, the name statements reference, and the field carried
  through engine results. A domain dimension key is the name clients pass in
  `dimensions`. Do not rename a key without updating every statement, formula,
  and downstream consumer that references it.

- **Base metrics read one physical column.** A `BaseMetric` has a
  `source_column`, optional structured `where:`, an `aggregation` (the SQL
  aggregate over source rows), and a `rollup_method` (how aggregated values
  combine across periods — `sum` for flows, `closing` for balances, `avg`
  for rates). Complex business logic belongs in the semantic view or in a
  derived metric, not in an ad hoc metric expression.

- **`where:` is the only row-filter grammar.** It is a list of structured
  predicates, ANDed together, that compiles to both ClickHouse SQL and Ibis.
  A raw-SQL filter string is rejected at load time with a `CatalogueError`.

- **`where:` predicates are fact-view columns only — they do not join
  hierarchies.** A metric `where:` is a row-level scan predicate compiled
  directly against the domain's source view. Unlike a breakdown axis or a
  request `filter` — both hierarchy-aware (a breakdown joins the leaf dimension
  table; a filter resolves an ancestor to leaf IDs) — a `where:` column must be
  physically present on the source view. `where: { column: department }` will
  **not** auto-join the cost-centre dimension; it needs a `department` column on
  the view, or the query fails with an unknown column. This is why attributes a
  metric filters on (e.g. `fs_line`, `account_type`) are denormalised onto the
  fact view even when they are also derived dimensions. To define a metric by a
  hierarchy attribute, denormalise it onto the view or compose a derived metric.

- **Derived metrics reference metric keys, not columns.** A
  `DerivedMetric.formula` is evaluated after retrieval using already-computed
  metric values. It supports arithmetic, parentheses, numeric constants,
  `abs()`, and simple conditional expressions (`x if y else z`, where the
  condition is truthy when non-null and non-zero — comparison operators are
  not supported). It does not access database columns.

- **A domain binds metrics to one source view.** `source_view` is the physical
  table/view the retriever reads. In ClickHouse domains this is normally a
  `semantic.*` view. In Ibis domains this is a table or view on one configured
  federated source.

- **Native dimensions are filterable; inline dimensions are axes only.** A
  domain `dimensions:` entry with `source:` references a first-class master
  dimension and can be used for filters and security scope. A
  `source_inline: true` entry exists only on an Ibis federated domain and can
  be used in `dimensions`, but not in `filters` or security scope.

- **One key, both surfaces.** A domain `dimensions:` binding is
  `key: <catalogue dimension name>` / `source: <physical view column>`. The
  catalogue name in `key` is what clients pass in **both** `filters` and
  `dimensions`; the engine translates it to the `source` column for the WHERE
  clause and the GROUP BY. If a domain maps `key: cost_centre` to
  `source: cost_centre_id`, clients filter with `{"cost_centre": "..."}` and
  group with `dimensions=["cost_centre"]` — never the column name. The source
  view can name its column however it likes.

- **Dimension hierarchies are bottom-up.** Leaf dimensions can declare
  `parents:`. Derived dimensions declare `derived_from:`. The loader computes
  transitive closure so ancestor filters can resolve to leaf IDs.

- **Hierarchy dimensions are groupable without a fact-view column (ClickHouse).**
  A derived/parent dimension (`department`, `division`, `grade`) needs neither a
  domain binding nor a denormalised column on the fact view to be a breakdown
  axis: the engine joins its leaf dimension table at query time and groups by the
  value column. The only precondition is that the domain binds the **leaf** the
  derived dimension descends from. Two exceptions: **federated** domains cannot
  join across backends, so a derived axis there must be a column on the foreign
  view (named to the catalogue key); and the period parents `quarter` /
  `fiscal_year` are read as fact-view columns, so the semantic view must expose
  them.

- **Statements are display contracts.** A statement is an ordered list of
  metric keys and `separator`, or a `concat:` of other statements and
  `separator`. It is not a query expression.

- **Validation must fail fast.** If the catalogue cannot load cleanly through
  `load_catalogue()`, the change is not acceptable. Runtime errors from
  missing columns are still possible, so the preflight check (below) and
  semantic-view parity remain part of the reviewer's job.

---

## ClickHouse domain vs. Ibis federated domain

| Concern | ClickHouse domain | Ibis federated domain |
|---|---|---|
| `backend_kind` | omitted or `clickhouse` | `ibis` |
| `backend` | optional; defaults to `clickhouse_default` | required source id — a `Source` declared in `instance/integrations/sources/<id>.yml`, credentials from `<SECRET_REF>_*` env vars |
| `source_view` | ClickHouse semantic view | foreign table/view visible to the federated source |
| `versioned` | defaults to `false`; set `true` for commit-aware plan domains (view needs `commit_id`) | must be `false` |
| aggregation support | full engine support | currently only `aggregation: sum`, `rollup_method: sum` |
| native dimension filters | resolved in ClickHouse and applied to source-view column | resolved in ClickHouse and applied as `IN (...)` in Ibis |
| inline dimensions | not supported | `source_inline: true`, `filterable: false`, axis only |

Supported federated source kinds are `postgres`, `mssql`, `snowflake`,
`bigquery`, and `databricks` (non-Postgres kinds need their optional driver
extra, e.g. `pip install 'precis-mcp[snowflake]'`); the shared
`precis_mcp/ingestion/ibis_backends.py::build_ibis_backend` factory is the
extension point for new kinds. Only `postgres` is exercised end-to-end today.
See
[Ingestion & data sources](ingestion.md) for how sources are
declared — federated reads and ingestion share the same `Source` objects, so
your warehouse is registered exactly once.

### Base metric vs. derived metric

Use a base metric when the source view has a numeric column to aggregate. Use
a derived metric when the value is arithmetic over other metrics in the same
domain.

```yaml
metrics:
  - key: sales_amount
    label: Sales amount
    source_column: amount
    aggregation: sum
    rollup_method: sum
    sign: raw
    format: currency
    fs_group: Sales
    where:
      - column: line_type
        op: eq
        value: SALE

  - key: sales_margin_pct
    label: Sales margin %
    formula: sales_margin / sales_amount
    format: percent
    fs_group: Sales
    style: ratio
    scale_exempt: true
```

### Binding a dimension to a view column

```yaml
dimensions:
  - key: cost_centre
    label: Cost centre
    source: cost_centre_id
```

`key` is the catalogue dimension name — the single key clients use in both
`filters` and `dimensions`. `source` is the physical column on this domain's
source view; the engine groups by it and filters against it, but clients never
name it. `list_kpis` exposes one `dimension_keys` list (valid in both surfaces)
plus `axis_only_dimensions` for inline federated axes that can only be grouped,
not filtered.

---

## Worked example

This example adds a new ClickHouse-backed domain, a few metrics, a derived
metric, and a statement. Adjust names and source columns for the model you are
extending — the bundled `instance/` directory is a complete working reference.

### Step 1: Confirm or create the semantic view

Create or update a semantic SQL view under `instance/semantic/views/` (or a
dimension view under `instance/semantic/dims/`). The provisioner applies these
to ClickHouse as `CREATE OR REPLACE VIEW` via
`precis_mcp/ingestion/semantic_runner.py` — re-run
`python -m precis_mcp.clickhouse_init --scope open` after editing. The view
must expose:

- `scenario`
- `period`
- the **leaf** dimension key columns the domain binds (the join keys for
  hierarchy breakdowns — e.g. `cost_centre`, `employee_id`); derived/parent
  columns (`department`, `division`, `grade`) are **not** needed, the engine
  joins the leaf dimension table for those
- the period parents `quarter` / `fiscal_year` if you want to group by them
- every column referenced by metric `source_column`
- every column referenced by metric `where:`
- `commit_id` if the domain is `versioned: true`

For a ClickHouse domain, the engine reads this view and joins leaf dimension
tables for derived breakdowns. For an Ibis domain, the engine cannot join across
backends: the foreign table/view must be denormalised enough to aggregate
without joins, including any hierarchy column you want as a breakdown axis
(named to the catalogue key).

### Step 2: Define or reuse master dimensions

Add first-class dimensions in the shared dimensions file
(`instance/catalogue/dimensions.yml`) only when the model needs master data,
filtering, display labels, or hierarchy resolution.

```yaml
dimensions:
  product:
    label: Product
    attributes:
      name: { label: Product Name }
    display_attribute: name
    source:
      table: semantic.dim_product
      key_column: product_id
      attribute_mapping:
        name: product_name
    parents:
      product_family:
        source_column: product_family

  product_family:
    label: Product family
    derived_from:
      dimension: product
      source_column: product_family
```

`product` is a **leaf** dimension because it owns a source table;
`product_family` is **derived** from a column on that table. Every dimension is
exactly one type — `source` (leaf), `derived_from` (derived), or `ragged: true`
(see below) — and the loader rejects a dimension that sets none or more than one.

A leaf's `attributes` block declares its descriptive fields; `attribute_mapping`
wires each one to a source-view column, `display_attribute` selects the member
label clients see, and an optional `sort_attribute` sets member order (omit it and
members order by key). Every name used in `attribute_mapping`, `display_attribute`,
or `sort_attribute` must be a key declared in `attributes`, or catalogue load
fails.

A filter on `product_family` resolves to product IDs, then maps to the domain
source-view column for `product`.

#### Choosing a dimension type

| Use a… | When the level… | Declares | You get |
|---|---|---|---|
| **Leaf** | has its own master data (codes, names, attributes) | `source:` (`table`, `key_column`, `attribute_mapping`) | filtering, display labels, attributes, a base for hierarchy |
| **Derived** | is only a column value on another dimension's table | `derived_from:` (`dimension`, `source_column`) | filtering and grouping by that value; no master data of its own |
| **Ragged** | is one of several levels you want exposed as a single browsable axis | `ragged: true`, `leaf_dimension`, `levels` | one dimension that rolls up every level at once |

A **ragged** dimension reuses dimensions you already declared as ordered `levels`
(root → leaf); the platform derives its rollup views from the leaf's master table
(`source: { type: generated }`) — you write no SQL. The loader enforces:

- `leaf_dimension` names a **leaf** dimension (one with `source`);
- `levels` is non-empty and its **last** entry equals `leaf_dimension`;
- every level references an existing dimension.

`root_label` and per-level `display_prefix` are optional presentation. A dimension
expresses **one** rollup path: to roll the same leaf up a different way — cost
centres by organisation *and* by geography, SKUs by category *and* by brand —
declare a **separate** ragged dimension with its own `levels`; do not overload one
dimension's `parents` chain to carry two trees.

### Step 3: Bind dimensions and metrics in a domain file

```yaml
domain: sales
source_view: semantic.v_sales
versioned: false

dimensions:
  - key: product
    label: Product
    source: product_id
  - key: period
    label: Period
    source: period

metrics:
  - key: sales_amount
    label: Sales amount
    description: Total recognised sales for the selected period.
    calculation_note: SUM(amount) where line_type = SALE.
    source_column: amount
    aggregation: sum
    rollup_method: sum
    sign: raw
    format: currency
    fs_group: Sales
    style: default
    where:
      - column: line_type
        op: eq
        value: SALE

  - key: sales_cost
    label: Sales cost
    source_column: amount
    aggregation: sum
    rollup_method: sum
    sign: raw
    format: currency
    fs_group: Sales
    variance_effect: inverse
    where:
      - column: line_type
        op: eq
        value: COST

  - key: sales_margin
    label: Sales margin
    formula: sales_amount - sales_cost
    format: currency
    fs_group: Sales
    style: subtotal
```

A domain may also declare `inspect_enabled: true` plus an `inspect_columns:`
list to expose row-level drill-through over its source view through the
inspection tools.

Mind the `versioned` flag: it **defaults to `false`** — the read-only/actuals
case, which needs no `commit_id` column. Set `versioned: true` **only** for
commit-aware plan domains; their source view must then carry a `commit_id` column
(each row tagged with the commit it belongs to) and the engine adds a commit
filter to every query against them. Federated domains must always be
`versioned: false`.

### Step 4: Add the metric to a statement

```yaml
statements:
  sales_summary:
    label: Sales summary
    description: Sales, cost, and margin.
    lines:
      - sales_amount
      - sales_cost
      - sales_margin
```

Use `concat:` when a statement is a composition of existing statements:

```yaml
statements:
  commercial_pack:
    label: Commercial pack
    concat:
      - sales_summary
      - separator
      - pipeline_summary
```

### Step 5: Validate

At minimum, run the loader against the catalogue root you changed:

```bash
python -c "from precis_mcp.engine.catalogue import load_catalogue; load_catalogue('instance/catalogue'); print('ok')"
```

For a stricter check that catches `source_view` typos, pass the semantic-views
root and the loader will additionally assert that every ClickHouse-backed
domain's `source_view` resolves to a `.sql` file on disk (Ibis-federated
domains are exempt):

```bash
python -c "from precis_mcp.engine.catalogue import load_catalogue; load_catalogue('instance/catalogue', semantic_views_root='instance/semantic/views'); print('ok')"
```

Then run the preflight against your live ClickHouse — it validates that the
catalogue parses, that every semantic view it names actually exists in
ClickHouse, and that `semantic.scenarios` is seeded, without applying
anything:

```bash
python -m precis_mcp.clickhouse_init --scope open --check
```

If you changed semantic views, re-run the provisioner
(`python -m precis_mcp.clickhouse_init --scope open`) so ClickHouse picks up
the `CREATE OR REPLACE VIEW` from the new SQL, and restart the server so it
loads the new catalogue.

---

## Adding federated source-only axes

For an Ibis federated domain, you may expose columns that exist only on the
foreign table as reporting axes.

```yaml
domain: source_detail
source_view: finance.source_detail
backend: customer_pg
backend_kind: ibis
versioned: false

dimensions:
  - key: cost_centre
    label: Cost centre
    source: cost_centre_id

  - key: document_ref
    label: Document
    source_inline: true
    filterable: false
```

`backend: customer_pg` references the `Source` declared in
`instance/integrations/sources/customer_pg.yml`. `cost_centre` is
native/filterable: its `key` is the master dimension and `source` is the
foreign column.
`document_ref` is axis-only: clients can group by it, but cannot filter or
scope on it. The engine rejects `filters: {"document_ref": ...}`.

Use inline dimensions for source-level detail such as document IDs, task
codes, supplier IDs, posting dates, approval states, or source-system labels
when there is no canonical master-data table in ClickHouse.

Do not use inline dimensions for values that need hierarchy, member search,
display-name lookup, or security scope. Those need first-class dimensions.

---

## Failure modes and traps

### The catalogue loads but the query fails

`validate_catalogue()` checks references inside YAML. It does not prove that
`source_column`, `where.column`, or a domain dimension `key` exists in the
physical source view. If a query fails with an unknown column, inspect the
semantic view or foreign source table. The
`python -m precis_mcp.clickhouse_init --scope open --check` preflight catches
a missing *view*; a missing *column* only surfaces at query time.

### A ClickHouse domain's `source_view` doesn't exist on disk

When `load_catalogue()` is called with the stricter Step-5 form (passing the
semantic-views root), the loader asserts that every ClickHouse-backed domain's
`source_view` maps to a `.sql` file under that directory tree. A
`source_view: semantic.v_foo` is resolved by taking the bare identifier after
the last `.` and looking for `v_foo.sql`; if no file matches, catalogue load
raises `CatalogueError`. Ibis-federated domains (`backend_kind: ibis`) are
exempt because their source view lives on the federated source, not on
disk. Verify
the SQL file's stem matches the bare identifier in `source_view`, and that the
semantic views have been applied to ClickHouse (re-run the provisioner).

### Using a source-view column name instead of the catalogue key

Both `filters` and `dimensions` take the **catalogue dimension name** (the
binding's `key`), never the physical column. A domain dimension binding is
`key: <catalogue name>` / `source: <view column>`: the engine translates `key`
to the `source` column for both the `GROUP BY` and the `WHERE`. With
`key: product, source: product_id`, use `dimensions: ["product"]` and
`filters: {"product": "..."}` — passing `"product_id"` is wrong. Filtering still
resolves the catalogue name through the master dimension's data, hierarchy, and
scope before building the `WHERE`, which is what lets role-playing dimensions and
cross-domain reuse work.

### A source-only inline dimension cannot be filtered

This is intentional. Inline dimensions have no master table and no hierarchy
resolver. Promote the concept to a first-class dimension if clients need
filters, security scope, search, or display labels.

### A derived metric returns null

Derived formulas propagate nulls and return null on division by zero. Ratio
metrics should expect this. Do not add fallback constants unless the business
definition explicitly requires them.

### A federated metric with `avg` or `closing` is rejected

Federated domains currently support only `aggregation: sum` and
`rollup_method: sum`. Land the data in ClickHouse or defer the metric until
federated rollup semantics are implemented.

### A statement fails after adding a line

Statement `lines:` entries must be metric keys or `separator`. Statement
`concat:` entries must be statement names or `separator`. A statement cannot
define both `lines:` and `concat:`.

### A metric appears in `list_kpis` but not in the expected statement

`list_kpis` reflects all catalogue metrics. Statement membership is separate.
Add the metric key to the right statement `lines:` or to a statement included
via `concat:`.

### The client picks the wrong dimension

`list_kpis` output is generated from catalogue metadata, and an AI client
composes queries from it. Check:

- the domain `dimensions:` list contains the intended key;
- the dimension `label` is clear;
- `description` and `calculation_note` on metrics distinguish similar metrics;
- inline axes appear under `axis_only_dimensions`, not `dimension_keys`.

---

## Do / Don't

### Metrics

| Do | Don't |
|---|---|
| Express row filters as structured `where:` predicates. | Try to smuggle a raw SQL filter string — it is rejected at load. |
| Put reusable row-shaping logic in the semantic view. | Encode complex `CASE`, regex, casts, or backend-specific functions in metric YAML. |
| Use derived metrics for arithmetic over metrics. | Add duplicate base metrics just to calculate a margin or ratio. |
| Set `scale_exempt: true` for ratios, hours, and counts that should not be currency-scaled (percent formats are always exempt). | Let operational counts be scaled as currency. |
| Set `variance_effect: inverse` for costs where higher actuals are unfavourable. | Rely on the label to imply variance colour semantics. |

### Dimensions

| Do | Don't |
|---|---|
| Create a first-class dimension when clients need filters, scope, hierarchy, search, or display labels. | Use `source_inline: true` for governed master data. |
| Set `key:` to the catalogue dimension name and `source:` to the physical view column. | Put the column name in `key`, or assume the column must match the catalogue name. |
| Declare parent relationships bottom-up on the child dimension. | Add ad hoc hierarchy logic in query code. |
| Use ragged dimensions for explicit multi-level rollup trees. | Model alternate rollup paths by overloading one dimension's parent chain. |

### Domains and statements

| Do | Don't |
|---|---|
| Keep one domain bound to one source view/backend. | Expect one domain to join multiple stores at query time. |
| Keep statement layout separate from metric definitions. | Put arithmetic or filtering in statement files. |
| Use `versioned: false` on federated/Ibis domains. | Mark a federated domain `versioned: true` — the loader rejects it. |
| Check `list_kpis` after catalogue changes. | Assume a running server sees catalogue edits — restart it. |

---

## Pre-flight checklist

- [ ] The domain `source_view` exists and exposes the required `scenario`, `period`, value, filter, and dimension columns.
- [ ] Every base metric has `key`, `label`, `source_column`, `aggregation`, `rollup_method`, `sign`, `format`, and `fs_group`.
- [ ] Every row filter is a structured `where:` predicate list — no raw SQL filter strings.
- [ ] Every derived metric formula references existing metric keys and uses only supported arithmetic.
- [ ] Every statement line references an existing metric key or `separator`.
- [ ] Every native domain dimension has `key`, `label`, and `source`; `key` is a first-class dimension name and `source` is the physical view column.
- [ ] Every inline dimension is on an Ibis domain and declares `source_inline: true` plus `filterable: false`.
- [ ] Any dimension clients need to filter/scope/search is first-class, not inline.
- [ ] Any new leaf dimension has a source table, key column, and optional display/sort attributes backed by real columns.
- [ ] `load_catalogue()` succeeds for the catalogue root (with `semantic_views_root` for the on-disk view check).
- [ ] `python -m precis_mcp.clickhouse_init --scope open --check` passes against the target ClickHouse.
- [ ] Any changed semantic views have been re-applied (`python -m precis_mcp.clickhouse_init --scope open`) and the server restarted.

---

## What this guide deliberately doesn't cover

- The conceptual walkthrough of the two layers and the naming contract. See
  [Catalogue & semantic model](catalogue-and-semantic.md).
- How data lands before it reaches a semantic view, and how `Source` objects
  are declared. See [Ingestion & data sources](ingestion.md).
- What the `live` and `semantic` databases must contain. See
  [ClickHouse schema contract](clickhouse-schema-contract.md).
- **Scenarios.** A scenario is a value in your data, not a catalogue entity.
  Real scenarios live in the `semantic.scenarios` table (seeded from
  `instance/scenarios.yml` by the provisioner); the reporting vocabulary on
  top of them (shifted views, variance keys) is generated at runtime by
  `ScenarioRegistry`. There is no YAML scenario surface to extend — adding a
  budget or forecast means loading data carrying that scenario value and
  seeding its row. Use the `list_scenarios` tool to see what the engine
  currently exposes.
