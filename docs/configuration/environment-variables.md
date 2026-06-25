# Environment variable reference

Every knob in precis-mcp is an environment variable — there is no config file
beyond your model in `instance/` and the Compose bundles in `deploy/`. The
deploy bundles read variables from `deploy/.env` (copy `deploy/.env.example`
in the repository and edit); outside Compose, export them in the service's
environment.

**Secrets.** Any variable can instead be supplied as `<NAME>_FILE` pointing at
a mounted file (Docker Compose `secrets:`, Kubernetes projected volumes, Vault
Agent sidecars). `precis_mcp/secrets.py` resolves these at startup; a plain
value wins if both are set. The tables below flag the variables that normally
hold credentials.

## Deployment axes

Consumed by Docker Compose and `scripts/deploy-mcp.sh`, not by the server
process itself.

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `bundled-clickhouse,bundled-keycloak,bundled-proxy` | Which optional services the multi-user stack runs. Drop `bundled-clickhouse` to use your own ClickHouse; drop `bundled-keycloak` for a direct external IdP; drop `bundled-proxy` to terminate TLS at your own ingress; append `backup` to run the [backup scheduler sidecar](../operations/backups.md); append `ingestion` and/or `ingestion-watch` for the [ingestion daemons](ingestion.md#scheduling). |
| `PRECIS_DATA_MODE` | unset | Provisioning preset for `deploy-mcp.sh --data-mode`: `byo` / `bundle-empty` / `bundle-sample`. See [ClickHouse data modes](../deployment/clickhouse-data-modes.md). |
| `PRECIS_AUTH_MODE` | `keycloak` (multi-user), `devkey` (dev server) | Identity mode selector: `devkey` / `keycloak` / `oidc`. See [Remote access — sign-in & identity modes](../deployment/oauth-keycloak.md). |
| `PRECIS_INGRESS_MODE` | `bundled` | Ingress preset for `deploy-mcp.sh --ingress-mode`: `bundled` (the Caddy auto-HTTPS proxy) / `byo` (your own ingress fronts the `127.0.0.1`-published ports). |
| `PRECIS_DOMAIN` | empty | Bare hostname (the host part of `PRECIS_BASE_URL`, no scheme) the bundled Caddy proxy serves and obtains its Let's Encrypt certificate for. Required when `bundled-proxy` is in `COMPOSE_PROFILES`; ignored otherwise. |

## Application image

Consumed by Docker Compose when it brings the stack up (and at build time), not
by the server process itself.

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_MCP_TAG` | current release | Tag of `ghcr.io/precis-finance/precis-mcp` the app services pull. Compose uses this image when present and falls back to building from source when it is absent (or on `up --build`). Pin a version or a `@sha256:` digest for an immutable re-pull — see [Upgrading](../operations/upgrades.md#dependency-and-image-pinning). |
| `PRECIS_EXTRAS` | empty | Optional warehouse drivers baked into the image **at build time** (comma-separated: `bigquery,snowflake,mssql,databricks`). Only used on the build-from-source path; the published image is the core build without extras. |

## ClickHouse (the read layer)

| Variable | Default | Purpose |
|---|---|---|
| `CHHOST` | `localhost` | ClickHouse host. The bundled service name is `clickhouse`. |
| `CHPORT` | `8123` (`8443` when `CHSECURE`) | HTTP(S) port. |
| `CHUSER` | `default` | Username. Also seeds the bundled service — keep in sync. |
| `CHPASSWORD` | empty | Password. **Secret.** |
| `CHDATABASE` | `default` | Default database of the ClickHouse connection (also names the database the bundled service creates at first start). The pipeline's own tables always live in the fixed `live` / `staging` / `semantic` databases — see the [schema contract](clickhouse-schema-contract.md). |
| `CHSECURE` | `false` | TLS on the ClickHouse link (flips the default port to 8443). |
| `CHCACERT` | unset | CA certificate path to pin (with `CHSECURE`). |
| `CHVERIFY` | unset | Set `false` to accept a self-signed dev certificate. |
| `PRECIS_CLICKHOUSE_POOL_MAXSIZE` | `32` | Per-host size of the shared ClickHouse HTTP connection pool. Set it at or above `PRECIS_MAX_CONCURRENT_READS_GLOBAL` so the read-concurrency cap, not pool exhaustion, is what bounds a query burst. |

## Read concurrency (the `/mcp` read path)

In-process caps that keep a fan-out of read-tool calls — e.g. an Excel workbook
recalculating many `PRECIS.*` cells at once — from flooding ClickHouse. The caps
are per server process; a request that can't get a slot within the wait gets a
retryable "busy" tool error.

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_MAX_CONCURRENT_READS_PER_USER` | `6` | Max in-flight read-tool calls for any one user. The working control — contains a single client's burst and stops one user starving others. |
| `PRECIS_MAX_CONCURRENT_READS_GLOBAL` | `32` | Max in-flight read-tool calls across all users. A box guard; keep it at or below the ClickHouse pool size. |
| `PRECIS_READ_SLOT_WAIT_SECONDS` | `3` | How long a call waits for a free slot before returning the retryable busy error. |

## PostgreSQL (platform state — multi-user bundle only)

Users, profiles, and load history. The single-user trial runs without
Postgres.

| Variable | Default | Purpose |
|---|---|---|
| `PGHOST` / `PGPORT` | `localhost` / `5432` | Platform DB endpoint. |
| `PGUSER` | `postgres` | Username. |
| `PGPASSWORD` | empty | Password. **Secret.** |
| `PLATFORM_DB_NAME` | `precis_platform` | Database name. |
| `PGSSLMODE` / `PGSSLROOTCERT` | libpq defaults | TLS mode / CA path for the platform DB link. |
| `PG_CONNECT_TIMEOUT` | `5` | Per-attempt connect timeout (seconds). |
| `PG_POOL_MIN` / `PG_POOL_MAX` / `PG_POOL_TIMEOUT` | `0` / `10` / `10` | Connection-pool bounds and acquire timeout. |

## Identity — shared

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_BASE_URL` | empty | Public origin of your server (`https://precis.example.com`). OAuth URLs and the `/mcp` audience derive from it. Required for the multi-user bundle. |
| `PRECIS_AUTH_PREFLIGHT` | `false` | Run an issuer/JWKS/audience conformance check at boot and fail fast on misconfiguration. |
| `PRECIS_IDENTITY_CLAIM` | `precis_user_id` (falls back to `preferred_username`) | Signed token claim carrying the user identity. |
| `PRECIS_IDENTITY_COLUMN` | `id` | `users` column the claim is matched against (`id` or `external_id`). |

## Identity — mode A: single-user dev key

The local trial (`precis_mcp.server`). Three deliberate gates: the explicit
enable flag, a strong key, and a localhost bind.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_MCP_DEV_SERVER` | unset | Must be exactly `1` for the dev server to start. |
| `MCP_DEV_KEY` | unset | Shared bearer key, 32+ chars (`openssl rand -hex 32`). **Secret.** Required. |
| `MCP_BIND_HOST` | `127.0.0.1` | Listen address. Use `0.0.0.0` only inside an isolated container network. |
| `MCP_BIND_PORT` | `8768` | Listen port. |

## Identity — mode B: bundled Keycloak

| Variable | Default | Purpose |
|---|---|---|
| `KC_BASE_URL_INTERNAL` | `http://localhost:8080/auth` | Container-to-container Keycloak URL. |
| `KC_BASE_URL_PUBLIC` | `${PRECIS_BASE_URL}/auth` | Public Keycloak URL (behind your ingress). |
| `KC_REALM` | `precis` | Realm name. |
| `KC_CLIENT_ID` | `precis-spa` | Pre-registered public client. |
| `KC_MCP_AUDIENCE` | `${PRECIS_BASE_URL}/mcp` | RFC 8707 audience the `/mcp` resource requires. |
| `KC_REDIRECT_URI` | `${PRECIS_BASE_URL}/api/auth/callback` | OAuth redirect URI. |
| `KC_HOSTNAME_ADMIN` | `http://localhost:8080/auth` | URL the Keycloak admin console is served at. Defaults to localhost to match the reference ingress, which blocks `/auth/admin/` at the edge — reach the console from the host or an SSH tunnel. Set to the public URL only if your ingress allowlists the path. |
| `KC_BOOTSTRAP_ADMIN_USERNAME` | `admin` | Keycloak admin (used by the realm-reconcile sidecar and the admin CLI). |
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | unset | Keycloak admin password. **Secret.** Required. |
| `KC_DB_PASSWORD` | unset | Postgres role password for Keycloak's own schema. **Secret.** Required. |

## Identity — mode C: direct external OIDC IdP

Set `PRECIS_AUTH_MODE=oidc` and drop `bundled-keycloak` from
`COMPOSE_PROFILES`. Per-IdP walkthroughs:
[external IdP recipes](../deployment/external-idp-recipes.md).

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | unset | Your IdP's issuer URL. Required in mode C. |
| `OIDC_JWKS_URL` | unset | JWKS endpoint for your IdP's issuer. Required in mode C. |
| `OIDC_AUDIENCE` | `${PRECIS_BASE_URL}/mcp` | Audience the `/mcp` resource requires, verbatim. |
| `OIDC_CLIENT_ID` | unset | Your pre-registered client. |
| `OIDC_CLIENT_SECRET` | unset | Client secret for confidential clients. **Secret.** |

## Excel add-in (optional)

The Excel add-in is a separate **public** OAuth client (PKCE, no secret) of the
`/mcp` endpoint. These knobs are advertised in the `/mcp` host's
protected-resource metadata so the add-in auto-configures from the `/mcp` URL
alone — leave them all unset and the add-in surface is simply off. The two
token-shape knobs are independent; set them to match your IdP (see
[external IdP recipes](../deployment/external-idp-recipes.md)).

| Variable | Default | Purpose |
|---|---|---|
| `KC_ENABLE_EXCEL_ADDIN` | unset | **Mode B:** set `true` to provision and advertise the bundled-Keycloak `precis-excel-addin` public client (the realm reconcile creates it; absence deletes it). |
| `EXCEL_ADDIN_CLIENT_ID` | unset | **Mode C:** the public client_id you registered in your external IdP for the add-in. Wins over the mode-B default when set. |
| `EXCEL_ADDIN_SCOPE` | `openid` | Exact scope the add-in requests (RFC 9728 `scopes_supported`). Keep the default for an IdP that binds the audience via the resource indicator (Keycloak/Auth0/Ping). For an IdP that binds it via a scope (Entra/Okta), set the full request scope, e.g. `openid profile email offline_access api://<app-id>/access_as_user` (include `offline_access` so the add-in gets a refresh token, and `profile email` so the task pane can show the signed-in user's name/email instead of an opaque subject id). |
| `EXCEL_ADDIN_RESOURCE_INDICATOR` | `true` | Whether the add-in sends the RFC 8707 `resource` parameter. Leave `true` for the MCP-conformant shape; set `false` for an AS that rejects it (Entra's v2 endpoint returns `AADSTS901002`). |
| `EXCEL_ADDIN_DIST_DIR` | `excel-addin/dist` | Directory of the built add-in bundle the server hosts at `/excel` (with a host-templated manifest at `/excel/manifest.xml`). The published image bakes this directory during the Docker build. Override only when serving a separately built bundle. When the add-in is enabled and this directory exists, the server serves the bundle same-origin with `/mcp`, so no `/mcp` `CORS_ORIGINS` entry is needed. |

## Your model

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_INSTANCE_DIR` | the bundled demo `instance/` | **Compose bundles only:** host path mounted read-only over the container's `instance/` (`catalogue/`, `semantic/`, `integrations/`, `scenarios.yml`). Outside Docker the instance directory is the source checkout's `instance/`. |
| `PRECIS_INTEGRATIONS_ROOT` | `instance/integrations` | Override for the integrations registry root alone. |
| `COMPANY_NAME` | `your organisation` | Name woven into the MCP instructions served to clients. |
| `INSPECTION_ROW_CAP` | `10000` | Hard cap on rows returned by row-level inspection. |
| `USER_DATA_DIR` | `/data/users` | Per-user file storage root. The Compose bundle mounts a persistent `user_data` named volume here; only advanced bind-mount deployments need to manage host-path ownership. |

## Ingestion

### Per-source database credentials

A `Source` names a `secret_ref`; credentials are read from `<SECRET_REF>_*`
(uppercased). The suffixes depend on the source `kind`:

- **postgres** — `_HOST`, `_DATABASE`, `_USER`, `_PASSWORD` (**secret**), `_PORT` (default `5432`), `_SSLMODE`, `_SSLROOTCERT`
- **mssql** — as postgres, plus `_DRIVER` (default `ODBC Driver 18 for SQL Server`); default `_PORT` is `1433`
- **snowflake** — `_ACCOUNT`, `_USER`, `_PASSWORD`, `_DATABASE`, `_WAREHOUSE`, `_SCHEMA` (optional), `_ROLE` (optional)
- **bigquery** — `_PROJECT_ID`, `_DATASET_ID` (optional), `_CREDENTIALS_JSON` (service-account key JSON as a value secret, `*_FILE`-compatible; omit for ADC) — see the [BigQuery worked example](ingestion.md#bigquery-service-account-credentials)
- **databricks** — `_SERVER_HOSTNAME`, `_HTTP_PATH`, `_ACCESS_TOKEN`, `_CATALOG` (optional), `_SCHEMA` (optional)

Example: `secret_ref: customer_pg` (kind `postgres`) → `CUSTOMER_PG_HOST`,
`CUSTOMER_PG_DATABASE`, `CUSTOMER_PG_USER`, `CUSTOMER_PG_PASSWORD`, … The
non-Postgres kinds need their optional driver extra (`pip install
'precis-mcp[snowflake]'`). See
[Ingestion & data sources](ingestion.md#credentials).

In both compose bundles the `CUSTOMER_PG_*` variables default to the bundled
Postgres and the `fpa_actuals` mock-source database the sample-data generator
populates; override them in the environment (or `deploy/.env`) to point the
demo instance's bindings at your own warehouse.

### S3-compatible object store (optional — install the `s3` extra: `pip install ".[s3]"`)

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_S3_BUCKET` | unset | Bucket name; setting it enables the backend. |
| `PRECIS_S3_PREFIX` | empty | Key prefix within the bucket. |
| `PRECIS_S3_REGION` | unset | Region. |
| `PRECIS_S3_ENDPOINT_URL` | unset | Custom endpoint (MinIO and other S3-compatibles). |
| `PRECIS_S3_ACCESS_KEY_ID` / `PRECIS_S3_SECRET_ACCESS_KEY` | unset | Credentials. **Secret.** |

### SFTP drop folder (optional — install the `sftp` extra: `pip install ".[sftp]"`)

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_SFTP_HOST` | unset | Host; setting it enables the backend. |
| `PRECIS_SFTP_USER` | empty | Username. |
| `PRECIS_SFTP_PASSWORD` | unset | Password (or use `PRECIS_SFTP_KEY_PATH`). **Secret.** |
| `PRECIS_SFTP_KEY_PATH` | unset | Private-key file for key-based auth. |
| `PRECIS_SFTP_PORT` | `22` | Port. |
| `PRECIS_SFTP_PREFIX` | empty | Remote path prefix. |
| `PRECIS_SFTP_KNOWN_HOSTS` | unset | Path to a known_hosts file covering the host (generate with `ssh-keyscan -p <port> <host>`). In the compose bundle, bind-mount the file and point this at the container path. |
| `PRECIS_SFTP_HOST_KEY` | unset | The server's public host key inline, `<key-type> <base64>` (one line of `ssh-keyscan` output; the hostname field is tolerated). Alternative to a known_hosts file. |

Host-key verification is mandatory: set `PRECIS_SFTP_KNOWN_HOSTS` **or**
`PRECIS_SFTP_HOST_KEY`. With neither, the SFTP store refuses to start —
connecting unverified would let a man-in-the-middle capture the credentials
and the data.

### Daemons

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_SCHEDULER_INTERVAL_SECONDS` | `60` | Tick interval of the binding scheduler. |
| `PRECIS_WATCHER_INTERVAL_SECONDS` | `30` | Tick interval of the drop-folder watcher. |

## Backups (optional — `COMPOSE_PROFILES` += `backup`)

The backup configuration itself is declarative — `instance/backup.yml`, see
[Backups & restore](../operations/backups.md). The environment carries only
the pieces that can't live in YAML:

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_BACKUP_CH_CONFIG` | unset | Host path of the rendered ClickHouse backup-disk config (written by `backup init`), mounted into the clickhouse service. |
| `BACKUP_WRITER_ACCESS_KEY_ID` / `BACKUP_WRITER_SECRET_ACCESS_KEY` | unset | S3 writer credential (no delete permission on a WORM bucket). **Secret.** Names follow `credentials.writer` in backup.yml. |
| `BACKUP_READER_ACCESS_KEY_ID` / `BACKUP_READER_SECRET_ACCESS_KEY` | unset | S3 reader credential, used by restore/list. **Secret.** |
| `BACKUP_ALERT_WEBHOOK_URL` | unset | POSTed on any non-success run outcome (JSON: run id, outcome, failed stores). |
| `PRECIS_BACKUP_CH_TIMEOUT` | `3600` | ClickHouse `BACKUP`/`RESTORE` command timeout (seconds). |

Local-volume destinations need none of the credentials.

## Telemetry (optional — install the `telemetry` extra: `pip install ".[telemetry]"`)

| Variable | Default | Purpose |
|---|---|---|
| `PRECIS_TELEMETRY_ENABLED` | `false` | Master switch for OpenTelemetry instrumentation. |
| `PRECIS_TELEMETRY_CAPTURE_CONTENT` | `false` | Also capture request/response content on spans. Privacy-sensitive — leave off unless your collector is trusted with data. |
| `OTEL_SERVICE_NAME` | `precis-mcp` | Service name on emitted spans. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | OTLP HTTP receiver. |
| `OTEL_PYTHON_EXCLUDED_URLS` | unset | URL patterns excluded from HTTP auto-instrumentation. |
| `OTEL_RESOURCE_ATTRIBUTES` | unset | Extra `key=value` resource attributes, passed through. |

## What each surface requires

**Single-user local trial** (`deploy/docker-compose.local.yml`):
`ENABLE_MCP_DEV_SERVER=1`, `MCP_DEV_KEY`, and the `CH*` connection (the local
bundle wires the bundled ClickHouse for you). Everything else is optional.

**Multi-user bundle** (`deploy/docker-compose.yml`): the `CH*` and `PG*`
connections, `PRECIS_BASE_URL`, and one identity mode — bundled Keycloak
(`KC_BOOTSTRAP_ADMIN_PASSWORD`, `KC_DB_PASSWORD`) or external OIDC
(`OIDC_ISSUER` + client registration).
