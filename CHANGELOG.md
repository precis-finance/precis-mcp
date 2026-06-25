# Changelog

Notable changes to the `precis-mcp` open package. Format follows
[Keep a Changelog](https://keepachangelog.com/); the question every entry
answers is *"does this sync break my compose stack, my `instance/` files, or
my client integration?"*

<!-- Maintainers: add entries under [Unreleased] as part of every publish
     ritual (scripts/publish_open.py); move them under a dated heading when
     the sync is pushed to the mirror. -->

## [0.2.0] - 2026-06-25

Adds the **read-only Excel add-in** as an open feature, served by the published
image at `/excel`. No client-integration break — the `/mcp` read tools and their
response shapes are unchanged, and the add-in stays off until you enable it. One
compose change matters if you bring your own `instance/`: the single-user local
bundle now defaults to the image's baked-in demo instance; mount your own model
through the new `docker-compose.instance.yml` overlay (see **Changed**).

### Added

- **Excel add-in (read-only).** A Microsoft Excel add-in that brings live
  statements and metrics into the grid as `PRECIS.*` custom functions
  (`STATEMENT`, `METRIC`, `HIERARCHY`, `KPIS`, `SCENARIOS`), with **Format** and
  **Refresh** ribbon actions. It is an OAuth/MCP client of your own instance's
  `/mcp` endpoint — no separate account, and figures never leave your instance.
  The published image ships the built bundle and serves it at `/excel`; those
  static assets carry app-owned security headers (open CORS, framable CSP) so
  Office can host the task pane. Works in the bundled-Keycloak (including
  brokered) and direct-external-OIDC identity modes; dev-key mode is not
  supported. See [Précis for Excel](docs/excel/index.md).
- **Add-in OAuth client provisioning.** `EXCEL_ADDIN_ENABLED` gates the `/excel`
  surface. Bundled Keycloak: `KC_ENABLE_EXCEL_ADDIN` + `KC_ADDIN_REDIRECT_URIS`
  reconcile a gated public `precis-excel-addin` client (deleted again when the
  flag is off). External OIDC: `EXCEL_ADDIN_CLIENT_ID`, plus `EXCEL_ADDIN_SCOPE`
  and `EXCEL_ADDIN_RESOURCE_INDICATOR` for IdPs that bind the audience via a
  scope rather than the `resource` parameter. `EXCEL_ADDIN_DIST_DIR` overrides
  the served bundle directory.
- **Read-path concurrency caps.** Two semaphores bound the read path so a
  workbook refresh (one tool call per cell) cannot swamp ClickHouse:
  `PRECIS_MAX_CONCURRENT_READS_PER_USER` (per principal) and
  `PRECIS_MAX_CONCURRENT_READS_GLOBAL` (process-wide).
- **Package-only single-user quickstart.** `docker-compose.local.yml` pulls the
  published image and serves its baked-in demo instance with no instance mount,
  so first run needs no source checkout — download one compose file and bring
  the stack up.

### Changed

- **`deploy-mcp.sh` pulls the published image by default.** Building from source
  is now opt-in (`--build`, `--tag`, or `--extras`, which implies `--build`); the
  driver also waits for ClickHouse readiness before generating the sample
  bundle. Existing build workflows are unaffected via `--build`.
- **Bring-your-own `instance/` on the local bundle moves to an overlay.** The
  single-user `docker-compose.local.yml` now defaults to the image's demo
  instance with no mount. To run your own model, add the new
  `docker-compose.instance.yml` overlay, which bind-mounts your `instance/`.

## [0.1.1] - 2026-06-20

First release published as a container image. Beyond the published image and
the compose pull/build model, it carries an engine change to the catalogue
dimension contract and adds hierarchy-breakdown resolution — review the
**Changed** section below if you maintain `instance/` catalogues.

### Added

- Published container image `ghcr.io/precis-finance/precis-mcp`, built and
  pushed on each `v*` release. The compose bundles reference it by default and
  fall back to building from source when the tag is absent (`PRECIS_MCP_TAG`).
- Read-only MCP server: `run_statement`/`run_metric` (+`_data` variants),
  row-level inspection, discovery tools, ingestion status reads, and the
  `precis_orientation` tool; MCP Apps widgets (financial table, inspection
  grid) where the host supports them.
- Metric engine over a declarative model: YAML catalogue + SQL semantic
  layer in `instance/`, ClickHouse read layer with bundled/BYO data modes,
  federated read domains via Ibis (sum-only).
- Ingestion: declarative sources/bindings, extract → validate → atomic swap,
  cron scheduler and file-drop watcher daemons, `load_history` audit.
- Identity: three auth modes (dev key, bundled Keycloak — optionally
  brokered to a corporate IdP — or direct external OIDC), user profiles with
  scenario/domain/dimension scoping, per-call audit log.
- Backups: declarative `instance/backup.yml`, scheduled dump-tier bundles
  (Postgres + ClickHouse + instance config) to local or S3 destinations,
  checksum-verified restore and drills, `backup` compose profile.
- Deployment: single- and multi-user compose bundles, `deploy-mcp.sh`
  remote driver, optional OpenTelemetry instrumentation.
- Documentation site (`mkdocs`), `SECURITY.md`, DCO-based contribution flow
  via the one-way mirror.
- Hierarchy breakdowns without denormalisation: a derived/parent dimension
  (e.g. department, division, grade) can be used as a breakdown directly — the
  engine joins the leaf dimension at query time and groups by its value column,
  so adding a node to a hierarchy no longer means editing every fact view.
  Period parents (quarter/fiscal_year) stay fact-view columns; federated read
  domains require the axis to be a physical column on the foreign view, else the
  query errors clearly.

### Changed

- The app services now reference a published `image:` with a `build:` fallback.
  `docker compose up` pulls the release image by default; pass `--build` (or
  set `PRECIS_MCP_TAG` to a tag you build yourself) to build from source as
  before. Existing `up --build` workflows are unaffected.
- **Catalogue dimension contract — review your `instance/` catalogues.** A cube
  dimension's `key` is now the catalogue dimension name (the single key for both
  filters and breakdowns) and `source` is the physical view column (defaults to
  `key`). Result rows are keyed by the catalogue name, so a view can rename a
  dimension column without changing the agent vocabulary. Definitions that
  relied on the old behaviour — `key` as the master-dimension key, the raw
  column emitted in `GROUP BY`, view columns aliased to match internal keys —
  must be updated.
- `list_kpis` returns a single `dimension_keys` field, replacing the separate
  `available_dimensions` and `filter_keys` (client integration change).

### Fixed

- The deploy env template (`deploy/.env.example`), referenced throughout the
  docs, is now included in the published repository — a `.gitignore` glob had
  dropped it from every export.
