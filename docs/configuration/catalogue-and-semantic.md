# Catalogue & semantic model

This page walks one real example end to end — a small professional-services P&L —
so you can see how every layer connects. By the end you'll be able to read any
metric back to the SQL it runs and the field a client receives.

## The data layers

Précis holds your data in a fixed pipeline. Each layer has one job; keeping them
separate is what lets you change one without breaking the others.

```text
your source → staging.*  → live.*   → semantic.* → catalogue
              (transient)   (landed)   (engine shape)  (what's exposed)
```

| Layer | Owner | Objective |
|---|---|---|
| **staging** | platform | Transient landing target for a load and the source of the atomic swap. You never read or model it; it mirrors `live`'s shape. |
| **live.\*** | you — `instance/live/*.sql` + bindings | The canonical landed copy, at the grain you ingest. Holds exactly what you load, nothing more. |
| **semantic.\*** | you — `instance/semantic/*.sql` (platform for trivial pass-throughs) | Reshape `live` into the columns and grain the engine expects. Business meaning lives here. |
| **catalogue** | you — `instance/catalogue/*.yml` | Declare what's exposed: metrics, dimensions, statements. |

Read it top-down: ingestion fills `staging` and swaps it into `live`; semantic
views turn `live` into engine shape; the catalogue names semantic objects. **The
engine reads only `semantic.*`** — never `live` or `staging` directly. That
indirection is the point: you can reshape `live` (add a column, change the grain)
without editing a single metric, because the semantic view absorbs the change.

**The catalogue addresses semantic by name, not by schema.** A domain's
`source_view` and a dimension's `source.table` name a *semantic object*
(`v_pnl`, `dim_account`) — the platform resolves it in the `semantic` schema. A
`live.`-qualified reference is **rejected at load**: if the catalogue could point
the engine at `live`, the indirection above would be a fiction. And when a
dimension or fact needs no transform, you do **not** write a pass-through view —
the platform materialises the trivial `semantic.<x> AS SELECT * FROM live.<x>`
for you, so the catalogue always has a semantic object to name without the
boilerplate.

**Federated domains are the one exception.** A domain with `backend_kind: ibis`
reads its facts *in place* on your warehouse instead of from ClickHouse, so its
`source_view` addresses that foreign backend rather than `semantic.*`. Its
dimensions are still resolved against ClickHouse `semantic.*`. See
[ClickHouse domain vs. Ibis federated domain](adding-metrics-and-dimensions.md#clickhouse-domain-vs-ibis-federated-domain).

### Designing the staging/live grain

Three rules decide what to land and at what grain — get these right before you
write a binding:

- **Land only the grain you need.** `staging`/`live` define the grain Précis
  stores and the grain it serves the engine from. Don't replicate your whole
  warehouse: if a DWH or other suitable source already holds the transactional
  long tail, leave it there and read it through a
  [federated domain](adding-metrics-and-dimensions.md#clickhouse-domain-vs-ibis-federated-domain)
  rather than landing detail you'll only ever aggregate.
- **The live grain is the partition grain.** A period load runs `REPLACE
  PARTITION '<period>'` — it atomically replaces *everything* at that partition
  grain (see [Ingestion](ingestion.md#how-a-load-works)). So if one table holds
  several plan scenarios and you want to reload them independently, give each
  scenario its **own** `live`/`staging` table; otherwise every load replaces all
  scenarios at once. Then union the per-scenario tables back together in the
  **semantic** layer.
- **Reshape in semantic, not in live.** `live` is the shape you *ingest*;
  `semantic` is the shape the engine *needs*. Renames, joins, scenario unions,
  excluding non-postable rows, denormalising an attribute — every transform
  belongs in a semantic view, never in a contorted landed table.

## The two layers

You describe your model in two layers, kept separate on purpose:

- **Semantic layer** — SQL views that say *what your data means*: what a P&L row
  is, which accounts are revenue, how a period rolls up. This is where business
  logic lives.
- **Catalogue** — YAML that says *what gets exposed*: which metrics, dimensions,
  and statements exist, and how each is computed and formatted. This is the
  surface clients see.

The catalogue sits on top of the semantic layer and refers to it by column name.
**The names must line up** — and the engine checks this at startup, so a mismatch
is an error you see immediately, not a wrong number you discover later.

```text
instance/
  catalogue/        # YAML — what gets exposed
    pnl.yml             a domain: its source view + metrics
    dimensions.yml      the dimension registry (how you slice)
    statements.yml      named collections of metrics
  semantic/         # SQL — what the data means
    dims/               dimension master-data views
    views/              fact/metric views the engine queries
```

This directory is *your* configuration — it describes your model and ships with
your deployment, separate from the installed package.

The example below uses a services-business model (revenue, delivery costs,
margins, headcount). Substitute your own accounts and metrics; the mechanics are
the same.

---

## Layer 1 — the semantic views

### A dimension view

A dimension is a thing you slice by — account, cost centre, period. Each owns a
master-data view under `semantic/dims/`. Here is the whole account dimension:

```sql
-- semantic/dims/dim_account.sql
-- The chart of accounts, from the ERP master. Excludes non-postable header rows.
SELECT
    account_code,
    account_name,
    account_type,
    fs_line          -- which financial-statement line this account belongs to
FROM live.dim_account
WHERE is_active = TRUE
  AND account_type != 'HEADER'
ORDER BY account_code
```

The columns it exposes — `account_code`, `account_name`, `account_type`,
`fs_line` — are the names the catalogue will refer to. Remember `fs_line`: the
revenue metric uses it.

### A fact view

A fact view is what the engine actually queries for numbers. It produces one tidy
table at a known grain — one row per *(account, cost centre, period, scenario)* —
with a measure column. Here is the P&L view, **abridged** to its shape (the full
view also unions plan/forecast scenarios and statistical sections like hours and
FTEs):

```sql
-- semantic/views/v_pnl.sql  (abridged)
WITH unified AS (
    -- Actuals, from the posted general ledger
    SELECT
        account_code AS account,
        cost_centre  AS cost_centre,
        period       AS period,
        'ACTUALS'    AS scenario,
        SUM(amount)  AS amount
    FROM live.fact_gl
    GROUP BY account_code, cost_centre, period

    -- … UNION ALL the budget/forecast scenarios, plus statistical
    --   sections (hours, FTEs) — omitted here …
)
SELECT
    u.account,
    ad.fs_line,                 -- pulled in from the account dimension
    u.cost_centre,
    u.period,
    u.scenario,                 -- which dataset this number is from
    u.amount                    -- the measure
FROM unified u
LEFT JOIN live.dim_account ad ON u.account = ad.account_code
```

The columns this view exposes are the contract the catalogue builds on:

| Column | Role | Used by |
|---|---|---|
| `account`, `cost_centre`, `period` | dimension keys — what you group by | metric dimensions |
| `fs_line` | an account attribute — what you filter on | the `revenue` metric's filter |
| `scenario` | which dataset (actuals, a budget, a forecast) | scenario selection at query time |
| `amount` | the measure the engine sums | every base metric's `source_column` |

---

## Layer 2 — the catalogue

### Binding a domain to its view

A **domain** is a group of metrics that share one source view — the P&L
metrics over the P&L view, the pipeline metrics over the pipeline view. Each
domain is one catalogue file, and the file names the semantic view it sits on.
This one line is the join between the two layers:

```yaml
# catalogue/pnl.yml
domain: pnl
source_view: semantic.v_pnl     # ← every metric below queries this view

dimensions:                     # which columns of the view you may slice by
  - { key: cost_centre, label: Cost Centre, source: cost_centre }
  - { key: period,      label: Period,       source: period }
```

`key:` is the **catalogue dimension name** — the single name clients use in both
`dimensions: ["cost_centre"]` and `filters: {"cost_centre": …}`; `source:` names
the **physical view column** the engine groups by and filters against. They're
equal here only because the view column is named like the dimension — they
diverge when the column is a raw key (`key: cost_centre, source: cost_centre_id`).
`key` must be a dimension defined in the registry below; `source` must be a real
column in `v_pnl`.

### A base metric

A base metric reads the measure column directly, optionally filtered, then
aggregates and formats. Here is `revenue`:

```yaml
  - key: revenue
    label: Revenue
    description: "Total recognised project revenue for the period."
    calculation_note: "Sum of credit-side journal entries on revenue accounts (fs_line = 'Revenue') from gl.actuals. Stored as negative in the ledger; sign: abs converts to positive for display."
    where:                         # restrict to revenue accounts…
      - column: fs_line
        op: eq
        value: Revenue
    source_column: amount          # …sum this column…
    aggregation: sum               # the SQL aggregate over source rows
    rollup_method: sum             # how aggregated values combine across periods
    sign: abs                      # ledger stores revenue negative; flip to positive
    format: currency
    fs_group: Revenue              # which statement section the metric belongs to
```

Two of these look similar but answer different questions: `aggregation` is the
SQL aggregate applied to the source rows (`sum`, `count`, `avg`, …);
`rollup_method` is how the already-aggregated values combine when periods roll
up — `sum` for flows like revenue, `closing` for balances (take the last
period's value rather than adding), `avg` for rates.

Read it as a query against the source view:

```sql
SELECT SUM(amount)
FROM   semantic.v_pnl
WHERE  fs_line = 'Revenue'
  AND  scenario = :scenario       -- chosen at query time
GROUP BY :requested_dimensions    -- e.g. cost_centre, period
```

Every field traces somewhere: `where` and `source_column` reference columns in
`v_pnl`; `sign` and `format` shape the output; `key` becomes the field name the
client receives.

`description` and `calculation_note` carry no engine logic — but they are not
optional polish. `list_kpis` surfaces both to the client, and an AI agent reads
them to choose which metric answers a question and to interpret the number it
gets back. `description` is the one-line *what this is*; `calculation_note` is
the *how it's derived, and any sign or scale gotcha* — here, that revenue is
stored negative and flipped by `sign: abs`. Write them for a reader who can't
see the SQL, because that is exactly the agent's situation.

#### The `where` predicate

`where` is a **portable filter** — a list of structured predicates, ANDed
together. It replaces raw SQL filter strings so the same metric definition works
against a native ClickHouse view *or* a **federated** source — a table the
engine reads in place on your warehouse through Ibis, instead of from
ClickHouse (see
[Adding metrics & dimensions](adding-metrics-and-dimensions.md)).
The engine compiles the predicates to whichever backend the source view uses.

```yaml
    where:
      - column: account_type
        op: in
        values: [Revenue, OtherIncome]   # `in`/`not_in` take `values:` (a list)
      - column: is_intercompany
        op: eq
        value: false                     # other ops take a single `value:`
```

Supported `op`s: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`,
`is_null`, `is_not_null`. The last two take neither `value` nor `values`.
`where` is the only filter grammar — raw SQL filter strings are rejected at load.

### A derived metric

A derived metric has no `source_column` — it's a `formula` over other metric
keys. Each input is aggregated independently first, then combined:

```yaml
  - key: gross_margin
    label: Gross Margin
    description: "Revenue net of direct delivery cost."
    calculation_note: "revenue − direct_cost. Positive means revenue exceeds direct cost."
    formula: "revenue - direct_cost"       # references two other metric keys
    format: currency
    fs_group: Margins

  - key: gross_margin_pct
    label: "Gross Margin %"
    formula: "gross_margin / revenue * 100"  # derived metrics can build on derived metrics
    format: percent
    fs_group: Margins
```

`revenue` and `direct_cost` here are the *exact keys* of other metrics in the
catalogue. A typo is a load-time error, not a silent zero.

### A statement

A statement is an ordered list of metric keys — what the engine assembles into a
financial table:

```yaml
# catalogue/statements.yml
statements:
  pnl:
    label: "P&L Statement"
    lines:
      - revenue
      - direct_cost
      - gross_margin
      - gross_margin_pct
      - separator              # a visual rule, not a metric
      - indirect_cost
      - contribution_margin
      - sga
      - ebitda
      - ebitda_margin_pct
```

Each line is a metric key from the catalogue. Asking for the `pnl` statement runs
each metric and stacks the results in this order.

---

## How you slice — the dimension registry

`catalogue/dimensions.yml` defines each dimension once: its master-data view, its
key column, its display attribute, and its place in any hierarchy. Every dimension
is one of three kinds: a **leaf** owns a master table; a **derived** dimension
reads its members from a column on another dimension's table; a **ragged**
hierarchy presents several levels as one browsable axis. The `account` dimension
is a leaf, mapping onto the SQL view from earlier:

```yaml
# catalogue/dimensions.yml
account:
  label: Account
  attributes:                       # the descriptive fields this dimension carries
    name: { label: Account Name }
  display_attribute: name           # which attribute is shown as the member label
  source:
    table: semantic.dim_account     # ← the dimension view from Layer 1
    key_column: account_code
    attribute_mapping:              # attribute → column on the source view
      name: account_name
  parents:                          # the hierarchy this dimension rolls up into
    fs_line:      { source_column: fs_line }
    account_type: { source_column: account_type }
```

The first four fields are the dimension's **display contract**. `attributes`
*declares* the descriptive fields a member carries (here just `name`);
`attribute_mapping` *wires* each one to a real column on the source view. The
names rarely match — attribute `name` ← column `account_name` — and keeping
them separate means renaming a column never reaches a client.
`display_attribute` picks which attribute is shown as the member label, so a
client sees "Consulting Revenue", not the raw key `4000`. An optional
`sort_attribute` (not shown) sets member order — e.g. sort employees by `code`
while displaying `name`; omit it and members order by key. Every name in
`attribute_mapping`, `display_attribute`, and `sort_attribute` must be one of
the keys declared in `attributes`, or the catalogue refuses to load.

`parents` declares hierarchy bottom-up: every account belongs to an `fs_line` and
an `account_type`. Those become **derived dimensions** — dimensions whose members
are attribute values of another. This is how the `revenue` metric can filter on
`fs_line = 'Revenue'` even though `fs_line` isn't its own table: it's an attribute
of `account`.

**A parent can be derived or a leaf.** The parents above name derived dimensions —
`fs_line` is just a column value on `dim_account`, with no master data of its own.
But a parent can equally be a **first-class leaf**: a `project` dimension can
declare `client` as a parent (`source_column: client_id`), where `client` is a
leaf with its own `dim_client` table, attributes, and display name. Use a derived
parent when the level is only a grouping label; use a leaf parent when it's an
entity in its own right — one you also want to filter, give attributes, or roll up
further. Either way the parent entry names the dimension *and* the column on *this*
table that holds its key.

**Multi-level hierarchies.** A parent can have its own parent. A cost centre
rolls up to a department, and a department to a division:

```yaml
cost_centre:                        # the leaf — owns the master table
  label: Cost Centre
  source: { table: semantic.dim_cost_centre, key_column: cost_centre }
  parents:
    department: { source_column: department }

department:
  label: Department
  derived_from: { dimension: cost_centre, source_column: department }
  parents:
    division: { source_column: division }   # the next level up

division:
  label: Division
  derived_from: { dimension: cost_centre, source_column: division }
```

Note where `derived_from` points: **both** `department` and `division` derive
from `cost_centre` — the leaf that owns the table — not from each other, because
the `department` and `division` columns both live on `dim_cost_centre`.
`derived_from` only says *where each level's members are read*; `parents` is what
threads the levels into a chain. The loader walks that chain at load time, so a
filter on `division` resolves all the way down to the cost-centre IDs it covers
(`SELECT cost_centre FROM dim_cost_centre WHERE division = ?`). The same shape
models SKU → subcategory → category, or point-of-sale → region → country.

**Ragged hierarchies.** The derived dimensions above let a client group by *one*
level — by `department`, or filter by `division`. A **ragged** dimension goes
further: it presents the whole chain as a single sliceable axis, so one request
returns the rollup at every level at once — All → Division → Department → Cost
Centre — the way you drill an EPM hierarchy.

```yaml
org_structure:
  label: Organisational Structure
  ragged: true
  root_label: "— All Cost Centres —"    # the synthetic top node
  leaf_dimension: cost_centre           # where the tree bottoms out
  levels:                               # ordered root → leaf
    - { dimension: division,    display_prefix: "[D] " }
    - { dimension: department,  display_prefix: "[BU] " }
    - { dimension: cost_centre, display_prefix: "[CC] " }
  source:
    type: generated                     # built from the levels above, no extra table
```

A ragged dimension **reuses** dimensions you already declared (`division`,
`department`, `cost_centre`) — it does not redefine them. `levels` lists them
root-to-leaf; `display_prefix` tags each level in the output so a client can tell
a department node from a cost-centre node; `source: { type: generated }` tells the
platform to build the tree from those levels rather than from a parent-child
table. The result is one dimension key, `org_structure`, that a client slices to
see every level of the org at once. Model a SKU rollup (All → Category →
Subcategory → SKU) or sales geography (All → Country → Region → POS) the same way.

A domain's `dimensions:` block (in `pnl.yml`) is the subset of these you can slice
*that view* by. The registry defines all dimensions; each domain opts into the
ones its view supports.

**Role-playing dimensions.** To slice a fact by the *same* master in two roles —
e.g. a transfer with both a primary and a counterparty cost centre — give the
second role its own leaf dimension over a second view of the same table
(`semantic/dims/dim_counterparty_cc.sql` = `SELECT * FROM live.dim_cost_centre`),
and bind each to its own fact column; they then filter and roll up independently.
The separate view is needed because the auto pass-through generator keys views by
table stem, so two dimensions sharing one table would collide.

---

## Scenarios

A **scenario** identifies which dataset a number comes from — actuals, a budget, a
forecast. It's the `scenario` column in the fact view, and clients choose one (or
compare two) at query time. You don't define scenarios in a catalogue file; the
engine exposes whatever scenario values exist in your data.

---

## Tracing one query

Putting it together — *"revenue by cost centre for the P&L, actuals":*

1. The client asks for metric `revenue`, grouped by `cost_centre`.
2. The engine looks up `revenue` in `pnl.yml`, sees `source_view: semantic.v_pnl`.
3. It runs `SELECT SUM(amount) … WHERE fs_line = 'Revenue' AND scenario = 'ACTUALS' GROUP BY cost_centre`.
4. It applies `sign: abs` and `format: currency`.
5. It returns rows keyed by `cost_centre`, with the metric under the field name
   `revenue`.

Every step is something you declared. Nothing is implicit.

---

## The naming contract

The one rule that ties it all together: **the same name appears in every layer.**

```
semantic view column   →   catalogue reference        →   client field
   amount                    source_column: amount
   fs_line                   where: [{column: fs_line}]
   account_code              key_column: account_code
   revenue (metric key)      lines: [revenue, …]            revenue
```

If a name doesn't line up — a metric points at a column the view doesn't have, a
statement lists a metric key that doesn't exist, a dimension maps to a missing
column — the engine reports it at startup and refuses to serve an inconsistent
model. Fix the name; restart; it's caught before any client sees a number.

You don't have to wait for a restart to find out, though. Run the model check
ahead of time — it validates the catalogue *and* confirms the semantic views it
names exist in ClickHouse, without starting the server or changing anything:

```bash
python -m precis_mcp.clickhouse_init --scope open --check
```

See [What your ClickHouse must contain](clickhouse-schema-contract.md) for the
full preflight.

## Related

- [Ingestion & data sources](ingestion.md) — getting data into the views above.
- [Quickstart](../getting-started/quickstart.md)
