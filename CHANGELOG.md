# Changelog

Notable changes to the `precis-mcp` open package. Format follows
[Keep a Changelog](https://keepachangelog.com/); the question every entry
answers is *"does this sync break my compose stack, my `instance/` files, or
my client integration?"*

<!-- Maintainers: add entries under [Unreleased] as part of every publish
     ritual (scripts/publish_open.py); move them under a dated heading when
     the sync is pushed to the mirror. -->

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
