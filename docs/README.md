# Précis-MCP

**Précis-MCP lets an AI assistant query your financial model — safely and
read-only — over the [Model Context Protocol](https://modelcontextprotocol.io).**

You point it at your financial data (in ClickHouse), describe your model in a
catalogue, and any MCP-capable client — Claude or another agent — can ask
questions and get back consistent, defensible numbers: metrics, financial
statements, and row-level detail.

## What it does

- **Run metrics and statements** — ask for a P&L, a variance, a margin, a
  utilisation figure, and get a formatted table back.
- **Inspect row-level detail** — drill from a number into the rows behind it.
- **Discover the model** — list the scenarios (the datasets you compare:
  actuals, budgets, forecasts), metrics, and dimensions available before
  composing a query.

Numbers come from one place: a catalogue you define on top of a semantic
layer — SQL views that state what your data means. The same definition serves
every client, so two people asking the same question get the same answer.

## What it doesn't do

Précis-MCP is **read-only**. It never writes to or changes your data. It does
not render charts, export Excel files, or execute code — it returns data and
tables. If your client supports rich widgets, it renders the finance-table and
inspection views; otherwise it gets the same data as structured content.

It is the open core of the **Précis** platform, which adds the agentic
finance workspace on top of this same engine: a conversational agent and UI,
planning with write-back, and an extended finance toolset (reports, routines,
charts, Excel). Nothing here depends on it.

## Is this for me?

Use Précis-MCP if you have financial data you want an AI assistant to query
**accurately and consistently**, on infrastructure you control, without giving
it the ability to change anything. You bring the data and a description of your
model; it serves the queries.

## Where to start

| You want to… | Go to |
|---|---|
| Run a single-user server in a few minutes | [Quickstart](getting-started/quickstart.md) |
| Understand the moving parts first | [How Précis-MCP works](getting-started/concepts.md) |
| Expose a secure multi-user server (bundled Keycloak or your own OIDC IdP) | [Remote access — sign-in & identity modes](deployment/oauth-keycloak.md) |
| Sign in with your corporate IdP (SSO) through the bundled Keycloak | [Keycloak brokering](deployment/keycloak-brokering.md) |
| Connect Auth0 / Okta / Entra / Ping directly | [External IdP recipes](deployment/external-idp-recipes.md) |
| Use live read-only functions in Excel | [Précis for Excel](excel/index.md) |
| Run against bundled or your own ClickHouse (and provision it) | [ClickHouse data modes](deployment/clickhouse-data-modes.md) |
| Describe your own metrics and statements | [Catalogue & semantic model](configuration/catalogue-and-semantic.md) |
| Know what your ClickHouse must contain | [ClickHouse schema contract](configuration/clickhouse-schema-contract.md) |
| Load your own data | [Ingestion & data sources](configuration/ingestion.md) |
| Control what each user may read | [User profiles & permissions](configuration/user-profiles.md) |
| Wire a new data source, end to end | [Onboarding a data source](operations/onboarding-ingestion.md) |
| Look up any configuration knob | [Environment variable reference](configuration/environment-variables.md) |
| See what tools your MCP client gets | [MCP tool reference](reference/mcp-tools.md) |
| Back it up — and prove it restores | [Backups & restore](operations/backups.md) |
| Upgrade to a new release | [Upgrading](operations/upgrades.md) |
| Monitor it and alert on failed loads | [Observability](operations/observability.md) |
| Decode an error message | [Troubleshooting](operations/troubleshooting.md) |
| Answer your security review | [Security model](deployment/security-model.md) |
| Add a metric or dimension to your model | [Adding metrics & dimensions](configuration/adding-metrics-and-dimensions.md) |
| Add a custom read tool to the server | [Adding read tools](development/adding-read-tools.md) |

Stuck, or want a guided evaluation? Help configuring precis-mcp, deployment
assistance, and demo environments are an email away —
[hello@precis.finance](mailto:hello@precis.finance).
