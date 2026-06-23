# Quickstart — local, single-user

The fastest way to see Précis-MCP working: a server on your own machine,
authenticated with a single static key. Use it for a trial, a local demo, or to
check your catalogue before exposing the server to other people.

**You need:** Docker with the compose plugin, the single-user compose file (one
download — no full checkout required, because the published image bundles the
demo model), and an MCP client to point at it (Claude Code, Claude Desktop, or
any MCP-capable agent). **Time:** about 15 minutes, most of it the demo-data
generation in step 2.

For a multi-user server that real users sign in to, see
[Remote access](../deployment/oauth-keycloak.md).

## 1. Start the stack

Download the single-user compose file and start the stack — it **pulls the
published image**, which already contains the demo model, so there is nothing
to build and no instance to supply:

```bash
curl -O https://raw.githubusercontent.com/precis-finance/precis-mcp/main/deploy/docker-compose.local.yml
export MCP_DEV_KEY=$(openssl rand -hex 32)
docker compose -f docker-compose.local.yml up -d
```

(From a repository checkout the file is `deploy/docker-compose.local.yml` —
adjust the `-f` path. `up --build` builds the image from a checkout instead of
pulling; pin a release with `PRECIS_MCP_TAG`.)

That runs three containers: a bundled ClickHouse, a bundled Postgres (the
ingestion bookkeeping database, doubling as the demo's mock data source), and
the single-user MCP server, published on `127.0.0.1` only. The server is
guarded by three deliberate gates — an explicit enable flag, the 32+
character key you just minted, and the localhost bind — so it accepts only
you, on this machine.

Keep hold of the key (`echo $MCP_DEV_KEY`); your client sends it on every
request.

**You should see:** `docker compose -f docker-compose.local.yml ps`
lists three services — `clickhouse` and `postgres` (healthy) and
`precis-mcp` — all `Up`.

## 2. Populate the demo model

The bundled ClickHouse starts empty. Generate the synthetic demo dataset —
36 months of consistent financials for a fictional IT consultancy — **inside
the server container** (it has the package installed and the connections
configured):

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.sample_data
```

This is more than a seed script: it writes the dataset to the mock Postgres
source and then pulls it into ClickHouse through the same ingestion pipeline
a real deployment runs — bindings, validation, atomic swap, semantic views —
so what you're evaluating is the pipeline itself, not a shortcut around it.
Takes a few minutes; it ends with a validation report.

Then confirm the deployment is coherent:

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open --check
```

**You should see:** the generator's validation report (revenue, margin,
utilisation all `[OK]`), and the `--check` run printing one `ok` line per
check (catalogue, each semantic view, the scenario registry) before exiting
`0`. A `FAIL` line names exactly what's missing —
[troubleshooting](../operations/troubleshooting.md#clickhouse-and-the-model).

!!! note "Where commands run"
    The same pattern applies to every `python -m precis_mcp.*` command in
    these docs: `docker compose exec precis-mcp …` against the running
    stack, or a plain shell in a [source checkout](#running-without-docker)
    with the connection env vars exported.

Evaluating with your own model and data instead of the demo? Skip the
generator and provision the schema only — see
[Bring your own model](#5-bring-your-own-model) below,
[ClickHouse data modes](../deployment/clickhouse-data-modes.md) for the
provisioning presets, and [Ingestion](../configuration/ingestion.md) for
loading your data.

## 3. Connect your client

Point your MCP client at the local server, sending the key as a bearer token:

```jsonc
{
  "mcpServers": {
    "precis": {
      "url": "http://127.0.0.1:8768/mcp",
      "headers": { "Authorization": "Bearer <MCP_DEV_KEY>" }
    }
  }
}
```

## 4. Verify

Ask your client the kind of finance questions the catalogue is built for —
each exercises a different part of the server:

- **"Show the P&L for 2025 with comparatives."** — a statement with the right
  lines, signs, and subtotals (`run_statement`), not a flat column dump.
- **"Drill revenue down by cost centre."** — the same governed metric returned
  per cost centre, rolled up through the catalogue's dimensional hierarchy
  (`run_metric`).
- **"Show utilisation by month."** — a ratio whose denominator is averaged
  across periods, defined once in the catalogue rather than reconstructed in
  SQL per query (`run_metric`).
- **"What metrics can I report on?"** — the governed KPI catalogue
  (`list_kpis`), a curated metric vocabulary rather than a list of raw tables.

**You should see:** the client's tool list shows the Précis tools
(`precis_orientation`, `list_scenarios`, `list_kpis`, `run_metric`, …);
`list_kpis` returns the catalogue's governed metrics and `list_scenarios` the
demo's actuals / budget / forecast set; and the statement and metric answers
come back as figures aggregated from the semantic views and traceable to
source — never a value guessed over raw rows.

## 5. Bring your own model

Your model — `catalogue/`, `semantic/`, `integrations/`, `scenarios.yml` —
lives in an `instance/` directory; the image bundles a complete demo instance
showing the shape (it is what serves by default). To serve **your own** instead,
download the instance overlay alongside the compose file, set
`PRECIS_INSTANCE_DIR` to your directory, and bring the stack up with both files —
the overlay bind-mounts your model over the bundled one:

```bash
curl -O https://raw.githubusercontent.com/precis-finance/precis-mcp/main/deploy/docker-compose.instance.yml
PRECIS_INSTANCE_DIR=/path/to/your/instance docker compose \
  -f docker-compose.local.yml -f docker-compose.instance.yml up -d
```

Then provision the *schema only* (instead of running the demo generator):

```bash
docker compose -f docker-compose.local.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open
```

Metric values stay empty until your data is loaded —
[Ingestion](../configuration/ingestion.md) covers pulling from your sources.

See [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
for how to describe your metrics and statements.

## Running without Docker

The server is plain Python (3.12+). From a source checkout:

```bash
pip install -e ".[dev]"

export CHHOST=127.0.0.1 CHPORT=8123 CHUSER=default CHPASSWORD=...
export ENABLE_MCP_DEV_SERVER=1
export MCP_DEV_KEY=$(openssl rand -hex 32)

python -m precis_mcp.server
```

It binds `127.0.0.1:8768` by default (`MCP_BIND_HOST` / `MCP_BIND_PORT` to
override). In this mode the instance directory is the checkout's `instance/`
— replace its contents (or the directory itself) with your model.

To populate the demo model in this mode (`python -m precis_mcp.sample_data`),
you also need a reachable Postgres: export `PGHOST` / `PGUSER` / `PGPASSWORD`
(and `CUSTOMER_PG_HOST` / `CUSTOMER_PG_USER` / `CUSTOMER_PG_PASSWORD` /
`CUSTOMER_PG_DATABASE=fpa_actuals` pointing at the same server) before
running the generator. The compose stack wires all of this for you — going
Docker-less is for serving a model you populate by other means.

## Next steps

- Understand the moving parts → [How Précis-MCP works](concepts.md)
- Describe your own metrics → [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
- Load your own data → [Ingestion & data sources](../configuration/ingestion.md)
- Open it to other users → [Remote access](../deployment/oauth-keycloak.md)
- Every knob → [Environment variable reference](../configuration/environment-variables.md)
