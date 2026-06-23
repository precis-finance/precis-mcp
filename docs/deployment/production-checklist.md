# Production deployment — first-run checklist

The [quickstart](../getting-started/quickstart.md) gets a single-user trial
running in minutes. This is the other path: a multi-user server real people
sign in to, on infrastructure you operate.

Précis-MCP is **self-hostable on standard infrastructure, plus a modeling
step** — it is not a turnkey appliance, and the reason is intrinsic, not a
packaging gap. It is a *governed semantic layer*: someone has to describe your
metrics and statements over *your* chart of accounts and warehouse (step 4
below). The deployment itself is scripted and the dependencies are ordinary
(ClickHouse, Postgres, an identity provider); the modeling is the work only you
can do, because it encodes your definitions of revenue, margin, and
utilisation. Budget for it like a BI semantic-model project, not a software
install.

Work the steps in order. Each ends with **the failure it guards against** —
do not move on until the check passes.

## 1. Provision the host

`scripts/install-precis-mcp.sh` installs Docker, sets the firewall, and creates
`/opt/precis-mcp` on the target box. You need SSH access as root via a host
alias.

```bash
bash scripts/install-precis-mcp.sh --server YOUR_HOST
```

**Guards against:** deploying onto a box without Docker / with a closed
firewall, which surfaces later as opaque connection failures.

## 2. Configure `deploy/.env`

Copy `deploy/.env.example` and set, at minimum, the three deployment axes and
their secrets:

- **Identity** (`PRECIS_AUTH_MODE`): `keycloak` (bundled, mode B) or `oidc`
  (your existing IdP, mode C — set `OIDC_ISSUER`, `OIDC_JWKS_URL`,
  `OIDC_AUDIENCE`, `OIDC_CLIENT_ID/SECRET`, `PRECIS_IDENTITY_CLAIM/COLUMN`).
- **Data** (`PRECIS_DATA_MODE`): `byo` (your ClickHouse — set `CHHOST` etc.),
  `bundle-empty`, or `bundle-sample`.
- **Ingress** (`PRECIS_INGRESS_MODE`): `bundled` (Caddy auto-HTTPS on 80/443
  for `PRECIS_DOMAIN` — needs public DNS + ACME reachability) or `byo` (front
  the loopback-published ports with your own proxy; reference vhost in
  `deploy/nginx/`).

Secrets follow the `*_FILE` convention (a path to a mounted file) or plain env
vars — see the [environment variable reference](../configuration/environment-variables.md)
and [Security model](security-model.md).

**Guards against:** a silently-disabled audience check (a configured deploy
*must* resolve an `/mcp` audience, or any same-issuer token is accepted — the
server refuses to start without it when `PRECIS_AUTH_PREFLIGHT` is on).

## 3. Deploy

```bash
bash scripts/deploy-mcp.sh --server YOUR_HOST \
  --data-mode bundle-empty --auth-mode keycloak
```

This rsyncs the tree, **pulls the published release image**
(`ghcr.io/precis-finance/precis-mcp`), and brings up the stack (server + the
bundled dependencies your axes selected). Pulling a pinned release is the
default first-run path; pin a specific version with `--tag <version>` (it sets
`PRECIS_MCP_TAG`). Build from source instead — for tracking rolling `main`, a
fork, or baking warehouse drivers — with `--build`; `--extras bigquery` (or
`snowflake`/`mssql`/`databricks`) implies `--build`, since the published image
ships without warehouse drivers.

**Guards against:** hand-assembling `docker compose` invocations and missing a
profile — the script selects the right profiles for your three axes.

## 4. Describe your model

Your model lives in an `instance/` directory — `catalogue/`, `semantic/`,
`integrations/`, `scenarios.yml`. Fork the shipped `instance/` template
and replace its contents with your definitions, then provision the schema and
verify it:

```bash
# schema only (no demo data)
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open
# coherence check — catalogue ↔ ClickHouse ↔ scenario registry
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.clickhouse_init --scope open --check
```

See [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
and [Ingestion](../configuration/ingestion.md) to load your data.

**Guards against:** serving an incoherent model — `--check` prints one `ok`
per check (catalogue, each semantic view, the scenario registry) and exits
non-zero naming exactly what is missing.

## 5. Seed identity and access

No admin exists at install. Seed the first one, then build profiles and assign
them (the CLI runs inside the server container):

```bash
docker compose -f deploy/docker-compose.yml exec precis-mcp \
  python -m precis_mcp.admin_cli create-admin --id you@example.com
# mode C: add --no-keycloak (the external IdP owns the credential)
```

Then author and assign profiles — see
[User profiles & permissions](../configuration/user-profiles.md). In mode B,
verify the realm came up and the claim mapping is right with
`admin_cli check-auth`.

**Guards against:** a running server nobody can administer, and profiles whose
`allow:` typos silently lock users out (verify with
`admin_cli show-access --user <id>`).

## 6. Smoke-test before announcing

```bash
curl -fsS https://YOUR_DOMAIN/readyz        # 200 + {"status":"ready"} when CH + Postgres are reachable
```

Then, as a signed-in user: list scenarios, run one metric, and confirm
`admin_cli show-access --user <id>` resolves the access you intended.

**Guards against:** announcing a server that returns `503` from `/readyz`
(a dependency is unreachable) or serves figures the permission model wasn't
meant to expose.

## After first run

- Wire `/readyz` to your load balancer / Kubernetes readiness probe so a
  degraded instance is drained, not served.
- Set up [backups](../operations/backups.md) and rehearse a restore.
- Point telemetry at your stack — [Observability](../operations/observability.md).
- Plan [credential rotation](../operations/rotating-credentials.md).

## What is not yet provided

A single-host `docker compose` deployment is the supported shape today. There
is **no Helm chart or Terraform module** for multi-node / HA orchestration yet
— use the compose manifest (`deploy/docker-compose.yml`) and `deploy-mcp.sh`
as the reference for your own IaC if you need HA. ClickHouse cluster sizing for
larger deployments is a review, not a preset.
