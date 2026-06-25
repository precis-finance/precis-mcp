#!/usr/bin/env bash
# deploy-mcp.sh — Deploy / refresh the OPEN precis-mcp bundle on a server.
#
# The open counterpart to scripts/deploy.sh (which deploys the Précis platform
# stack). Runs from your local machine: rsyncs the working tree to the box and
# drives `docker compose` there against deploy/docker-compose.yml.
#
# Prereqs: scripts/install-precis-mcp.sh has provisioned the box (Docker,
# firewall, /opt/precis-mcp). SSH access as root via the host alias.
#
# Usage:
#   bash scripts/deploy-mcp.sh                      # sync + PULL published image + up + health (default)
#   bash scripts/deploy-mcp.sh --server HOST        # target host (default: precis-mcp)
#   bash scripts/deploy-mcp.sh --sync-only          # just rsync code + instance, stop
#   bash scripts/deploy-mcp.sh --tag 0.1.1          # pull a specific published tag (sets PRECIS_MCP_TAG)
#   bash scripts/deploy-mcp.sh --build              # build the image from source instead of pulling (rolling main / forks)
#   bash scripts/deploy-mcp.sh --no-build           # explicit pull (the default; kept for back-compat)
#   bash scripts/deploy-mcp.sh --data-mode MODE     # provision ClickHouse (see below)
#   bash scripts/deploy-mcp.sh --extras bigquery    # bake a warehouse driver into the image (bigquery|snowflake|mssql|databricks; comma-separate for several). Re-run on a live instance to add one later — rebuilds (cached layers) + recreates.
#   bash scripts/deploy-mcp.sh --data-only          # provision + exit (no Keycloak/server)
#   bash scripts/deploy-mcp.sh --auth-mode MODE     # identity mode (see below)
#
# Ingress mode (the ingress axis, also settable via PRECIS_INGRESS_MODE;
# default bundled — `--ingress-mode bundled|byo`). Orthogonal to the other
# two axes:
#   bundled    bundled Caddy proxy (bundled-proxy profile) — auto-HTTPS on
#              80/443 for PRECIS_DOMAIN (set it in deploy/.env; needs public
#              DNS + 80/443 reachable for ACME). The turnkey default.
#   byo        no bundled proxy — you front the 127.0.0.1-published ports with
#              your own ingress (reference vhost in deploy/nginx/, host helper
#              scripts/install-precis-mcp.sh --nginx).
#
# Auth mode (the identity axis, also settable via PRECIS_AUTH_MODE; default
# keycloak). Orthogonal to the data mode — pick one of each:
#   keycloak   bundled Keycloak (mode B) — the default multi-user path; the
#              keycloak + realm-apply services run (bundled-keycloak profile).
#   oidc       external OIDC IdP (mode C) — Keycloak is NOT started; set
#              OIDC_ISSUER (+ OIDC_JWKS_URL/AUDIENCE/CLIENT_ID/SECRET and
#              PRECIS_IDENTITY_CLAIM/COLUMN) in deploy/.env. precis-mcp verifies
#              the customer issuer. (devkey / mode A is the single-user local
#              bundle — use deploy/docker-compose.local.yml, not this script.)
#
# Data mode (the connection × provisioning preset, also settable via the
# PRECIS_DATA_MODE env var; default: none — deploy without provisioning, for
# code iteration on an already-provisioned box):
#
#   byo            external ClickHouse you run (set CHHOST in deploy/.env); the
#                  bundled clickhouse service is NOT started. Schema-only
#                  provisioning via `clickhouse_init`.
#   bundle-empty   bundled ClickHouse, schema-only provisioning (clickhouse_init).
#                  Empty schema ready for your own ingestion.
#   bundle-sample  bundled ClickHouse, populated with synthetic eval data
#                  (python -m precis_mcp.sample_data). The trial / demo on-ramp.
#
# Schema provisioning runs the open `clickhouse_init` (the migrate.py analog for
# ClickHouse) against the mounted instance/; the sample path runs the generator,
# which bootstraps the mock Postgres source itself and drives ingestion. Both
# run via `docker compose run --no-deps` so data is provisioned before the
# auth/server phase. (`--seed` is a back-compat alias for bundle-sample.)

set -euo pipefail

SERVER="precis-mcp"
REMOTE_DIR="/opt/precis-mcp"
PROJECT="$(basename "$REMOTE_DIR")"    # compose project = data-volume prefix (precis-mcp_*)
MOCK_SOURCE_DB="fpa_actuals"           # the sample-data generator's PGDATABASE default

DO_SYNC=true
# Pull the published GHCR image by default; build from source only on --build.
# This matches the prevailing self-hosted-OSS convention (Authentik, Plausible,
# Supabase, n8n, …): the first-run path pulls a pinned release, and building is
# the opt-in for rolling `main` / forks / warehouse extras. PRECIS_MCP_TAG (in
# deploy/.env, default in the compose) selects which published tag to pull.
DO_BUILD=false
DO_UP=true
SYNC_ONLY=false
DATA_ONLY=false
TEARDOWN=false
DATA_MODE="${PRECIS_DATA_MODE:-}"           # byo | bundle-empty | bundle-sample | "" (none)
AUTH_MODE="${PRECIS_AUTH_MODE:-keycloak}"   # keycloak (mode B) | oidc (mode C)
INGRESS_MODE="${PRECIS_INGRESS_MODE:-bundled}"  # bundled (Caddy) | byo (own ingress)
EXTRAS="${PRECIS_EXTRAS:-}"                  # comma-separated warehouse drivers baked into the image: bigquery,snowflake,mssql,databricks
MCP_TAG="${PRECIS_MCP_TAG:-}"               # published image tag to pull (--tag); empty = the compose default

while [ $# -gt 0 ]; do
    case "$1" in
        --server)      SERVER="$2"; shift 2; continue ;;
        --server=*)    SERVER="${1#*=}"; shift; continue ;;
        --sync-only)   SYNC_ONLY=true ;;
        --build)       DO_BUILD=true ;;                  # build from source instead of pulling the published image
        --no-build)    DO_BUILD=false ;;                 # explicit pull (the default; kept for back-compat)
        --tag)         MCP_TAG="$2"; shift 2; continue ;;   # published image tag to pull (sets PRECIS_MCP_TAG)
        --tag=*)       MCP_TAG="${1#*=}"; shift; continue ;;
        --data-mode)   DATA_MODE="$2"; shift 2; continue ;;
        --data-mode=*) DATA_MODE="${1#*=}"; shift; continue ;;
        --auth-mode)   AUTH_MODE="$2"; shift 2; continue ;;
        --auth-mode=*) AUTH_MODE="${1#*=}"; shift; continue ;;
        --ingress-mode)   INGRESS_MODE="$2"; shift 2; continue ;;
        --ingress-mode=*) INGRESS_MODE="${1#*=}"; shift; continue ;;
        --extras)         EXTRAS="$2"; shift 2; continue ;;
        --extras=*)       EXTRAS="${1#*=}"; shift; continue ;;
        --seed)        DATA_MODE="bundle-sample" ;;      # back-compat alias
        --data-only)   DATA_ONLY=true ;;
        --teardown)    TEARDOWN=true ;;
        *)             echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

# --data-only implied seeding before modes existed; preserve that default.
if $DATA_ONLY && [ -z "$DATA_MODE" ]; then DATA_MODE="bundle-sample"; fi

# Optional warehouse drivers (--extras / PRECIS_EXTRAS): comma-separated names,
# baked into the image at build via the PRECIS_EXTRAS build arg. Validate the
# components against the supported set before we touch the remote.
if [ -n "$EXTRAS" ]; then
    for _e in $(echo "$EXTRAS" | tr ',' ' '); do
        case "$_e" in
            bigquery|snowflake|mssql|databricks) ;;
            *) echo "Invalid --extras component: ${_e} (bigquery | snowflake | mssql | databricks)"; exit 1 ;;
        esac
    done
    # Extras bake into the image at build time; the published image is the core
    # build without them. So --extras forces the build path (it cannot be pulled).
    if ! $DO_BUILD; then
        echo "  --extras (${EXTRAS}) requires building the image — enabling the build path (the published image ships without warehouse drivers)."
        DO_BUILD=true
    fi
fi

# Data axis → its COMPOSE profile. byo excludes the bundled ClickHouse service;
# bundle-* (and the no-mode default) include it.
case "$DATA_MODE" in
    byo)                        DATA_PROFILE=""; CHHOST_SEED="" ;;
    bundle-empty|bundle-sample) DATA_PROFILE="bundled-clickhouse"; CHHOST_SEED="clickhouse" ;;
    "")                         DATA_PROFILE="bundled-clickhouse"; CHHOST_SEED="clickhouse" ;;  # deploy-only, bundled
    *) echo "Invalid --data-mode: ${DATA_MODE} (byo | bundle-empty | bundle-sample)"; exit 1 ;;
esac

# Auth axis → its COMPOSE profile. keycloak (mode B) runs the bundled Keycloak;
# oidc (mode C) drops it and points the verifier at the customer IdP (OIDC_*).
case "$AUTH_MODE" in
    keycloak) AUTH_PROFILE="bundled-keycloak" ;;
    oidc)     AUTH_PROFILE="" ;;
    devkey)   echo "devkey (mode A) is the single-user local trial — use deploy/docker-compose.local.yml, not this multi-user bundle."; exit 1 ;;
    *) echo "Invalid --auth-mode: ${AUTH_MODE} (keycloak | oidc)"; exit 1 ;;
esac

# Ingress axis → its COMPOSE profile. bundled runs the Caddy auto-HTTPS proxy;
# byo leaves the 127.0.0.1 ports for an ingress the operator runs.
case "$INGRESS_MODE" in
    bundled) INGRESS_PROFILE="bundled-proxy" ;;
    byo)     INGRESS_PROFILE="" ;;
    *) echo "Invalid --ingress-mode: ${INGRESS_MODE} (bundled | byo)"; exit 1 ;;
esac

# Combine the three axes into COMPOSE_PROFILES (comma-joined, empties dropped).
_profiles=()
[ -n "$DATA_PROFILE" ] && _profiles+=("$DATA_PROFILE")
[ -n "$AUTH_PROFILE" ] && _profiles+=("$AUTH_PROFILE")
[ -n "$INGRESS_PROFILE" ] && _profiles+=("$INGRESS_PROFILE")
PROFILES="$(IFS=,; echo "${_profiles[*]}")"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"

echo "╔══════════════════════════════════════════╗"
echo "║  Précis-MCP (open) Deploy                ║"
echo "║  Server: ${SERVER}                       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Teardown (clean reset) ───────────────────────────────────────────
# Drops the stack AND its named data volumes — the only clean way to re-init
# from scratch. It exists because the DB/Keycloak secrets are baked into the
# volumes at first init: removing just the code dir (or deploy/.env) leaves
# orphaned volumes whose secrets no longer match a freshly generated
# deploy/.env, which then fails opaquely on the next container recreate.
if $TEARDOWN; then
    echo "=== Teardown: dropping the '${PROJECT}' stack + its data volumes on ${SERVER} ==="
    ssh "$SERVER" "
        set -euo pipefail
        cd ${REMOTE_DIR}
        docker compose -p '${PROJECT}' -f deploy/docker-compose.yml down -v --remove-orphans
        echo '  stack + data volumes removed. deploy/.env was kept (delete it too for entirely new secrets).'
    "
    echo "Teardown complete. Re-run without --teardown for a fresh install."
    exit 0
fi

# ── Guard: refuse the commercial instance in the open bundle ──────────
# The open bundle serves an OPEN-shaped instance (plan lands statically in
# live.fact_plan, no plan-write catalogue). The monorepo's instance/ is the
# COMMERCIAL example (plan via planning.entries), marked by
# catalogue/plan_datasets.yml — which the open overlay drops at publish time.
# Deploying it into the open bundle silently shadows the image's open instance
# and breaks the generator (live.fact_plan missing). Catch it before the sync.
_inst="${PRECIS_INSTANCE_DIR:-${PROJECT_DIR}/instance}"
if [ -f "${_inst}/catalogue/plan_datasets.yml" ]; then
    echo "ERROR: ${_inst} is the COMMERCIAL instance (catalogue/plan_datasets.yml" >&2
    echo "       present), not the open one — the open bundle needs the open instance." >&2
    echo "       Deploy from the public mirror, or assemble the open tree first and" >&2
    echo "       deploy from there:" >&2
    echo "         python scripts/publish_open.py --out build/precis-mcp-mirror --skip-tests" >&2
    echo "         cd build/precis-mcp-mirror && bash scripts/deploy-mcp.sh --server ${SERVER} ..." >&2
    exit 1
fi

# ── 1. Sync code + instance ──────────────────────────────────────────
# rsync the working tree (not git): copies code AND the gitignored instance/
# in one pass, and lets us iterate on deploy files without commit churn.
# deploy/.env is protected (never synced, never --delete'd) so server secrets
# survive a re-sync.
if $DO_SYNC; then
    echo "=== 1. Sync code + instance → ${SERVER}:${REMOTE_DIR} ==="
    rsync -az --delete \
        --exclude='.git/' --exclude='.venv/' --exclude='node_modules/' \
        --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' \
        --exclude='.mypy_cache/' --exclude='/.env' --exclude='deploy/.env' \
        --exclude='deploy/.env.local' \
        "${PROJECT_DIR}/" "${SERVER}:${REMOTE_DIR}/"
    echo ""
fi
$SYNC_ONLY && { echo "--sync-only: done."; exit 0; }

# ── 2. Ensure deploy/.env (generate secrets once; require PRECIS_BASE_URL) ──
# Idempotent: passwords are generated only when deploy/.env is absent. The
# operator must set PRECIS_BASE_URL (deployment-specific) before the server +
# Keycloak phase; the data phase does not need it.
echo "=== 2. Ensure deploy/.env on ${SERVER} ==="
ssh "$SERVER" "
    set -euo pipefail
    cd ${REMOTE_DIR}
    umask 077
    if [ ! -f deploy/.env ]; then
        # Volume-aware guard: the DB/Keycloak secrets are baked into the data
        # volumes at first init. If those volumes already exist but deploy/.env
        # is gone, minting fresh secrets now would silently mismatch the data
        # (auth failures on the next container recreate). Fail closed.
        if docker volume ls -q | grep -qx \"${PROJECT}_postgres_data\"; then
            echo 'ERROR: data volumes for this project exist, but deploy/.env is missing.' >&2
            echo '  Their secrets were baked into the volumes at first init; minting fresh' >&2
            echo '  ones would mismatch the data. Restore deploy/.env from backup, or do a' >&2
            echo '  clean reset:  bash scripts/deploy-mcp.sh --teardown' >&2
            exit 1
        fi
        {
            echo 'COMPOSE_PROFILES=${PROFILES}'
            echo 'PRECIS_AUTH_MODE=${AUTH_MODE}'
            echo \"PGUSER=precis\"
            echo \"PGPASSWORD=\$(openssl rand -hex 24)\"
            echo '# ClickHouse host: the bundled service name for bundle-* modes;'
            echo '# your external host (REQUIRED) for --data-mode byo:'
            echo 'CHHOST=${CHHOST_SEED}'
            echo \"CHUSER=precis\"
            echo \"CHPASSWORD=\$(openssl rand -hex 24)\"
            echo \"CHDATABASE=precis\"
            echo \"KEYCLOAK_ADMIN_USERNAME=admin\"
            echo \"KEYCLOAK_ADMIN_PASSWORD=\$(openssl rand -hex 24)\"
            echo \"KC_DB_PASSWORD=\$(openssl rand -hex 24)\"
            echo '# External OIDC (mode C only — PRECIS_AUTH_MODE=oidc): set these'
            echo '# to your IdP, and drop bundled-keycloak from COMPOSE_PROFILES.'
            echo 'OIDC_ISSUER='
            echo 'OIDC_JWKS_URL='
            echo 'OIDC_AUDIENCE='
            echo 'OIDC_CLIENT_ID='
            echo 'OIDC_CLIENT_SECRET='
            echo 'PRECIS_IDENTITY_CLAIM='
            echo 'PRECIS_IDENTITY_COLUMN='
            echo '# Excel add-in (optional): the image bakes the hosted bundle at /excel.'
            echo '# Mode B: set true to provision the bundled-Keycloak public PKCE client.'
            echo 'KC_ENABLE_EXCEL_ADDIN='
            echo '# Optional override; defaults to \${PRECIS_BASE_URL}/excel/auth-callback.html'
            echo 'KC_ADDIN_REDIRECT_URIS='
            echo '# Mode C: register a public PKCE client in your IdP and advertise it here.'
            echo 'EXCEL_ADDIN_CLIENT_ID='
            echo 'EXCEL_ADDIN_SCOPE='
            echo 'EXCEL_ADDIN_RESOURCE_INDICATOR='
            echo \"# REQUIRED before the Keycloak/server phase — set to your public origin:\"
            echo \"PRECIS_BASE_URL=\"
            echo '# Bare hostname for the bundled Caddy proxy (the host part of'
            echo '# PRECIS_BASE_URL). Required with the bundled-proxy profile:'
            echo 'PRECIS_DOMAIN='
        } > deploy/.env
        echo '  generated deploy/.env (set PRECIS_BASE_URL before the server phase)'
    else
        echo '  keeping existing deploy/.env'
    fi
"
echo ""

# Persist the warehouse-driver selection into deploy/.env (idempotent upsert) so
# every ${COMPOSE} call below — which reads --env-file deploy/.env — bakes it
# into the image via the PRECIS_EXTRAS build arg, and survives later redeploys.
if [ -n "$EXTRAS" ]; then
    ssh "$SERVER" "
        set -euo pipefail
        cd ${REMOTE_DIR}
        umask 077
        if grep -q '^PRECIS_EXTRAS=' deploy/.env; then
            sed -i 's|^PRECIS_EXTRAS=.*|PRECIS_EXTRAS=${EXTRAS}|' deploy/.env
        else
            echo 'PRECIS_EXTRAS=${EXTRAS}' >> deploy/.env
        fi
    "
    echo "  set PRECIS_EXTRAS=${EXTRAS} in deploy/.env (rebuild bakes in: ${EXTRAS})"
    echo ""
fi

# Persist the published image tag (--tag) into deploy/.env (idempotent upsert) so
# every ${COMPOSE} call below pulls the same tag, and later redeploys stay pinned
# to it. Absent --tag, the compose default (PRECIS_MCP_TAG) applies.
if [ -n "$MCP_TAG" ]; then
    ssh "$SERVER" "
        set -euo pipefail
        cd ${REMOTE_DIR}
        umask 077
        if grep -q '^PRECIS_MCP_TAG=' deploy/.env; then
            sed -i 's|^PRECIS_MCP_TAG=.*|PRECIS_MCP_TAG=${MCP_TAG}|' deploy/.env
        else
            echo 'PRECIS_MCP_TAG=${MCP_TAG}' >> deploy/.env
        fi
    "
    echo "  set PRECIS_MCP_TAG=${MCP_TAG} in deploy/.env (pulls ghcr.io/precis-finance/precis-mcp:${MCP_TAG})"
    echo ""
fi

# ── 3. Provision ClickHouse (mode-gated; runs before the auth/server phase) ──
# bundle-sample runs the sample-data generator (which creates its mock-source
# DB and applies the platform migrations itself); byo / bundle-empty run the
# open clickhouse_init schema provisioner against the mounted instance/. All
# via `compose run --no-deps`, so data lands before auth.
if [ -n "$DATA_MODE" ]; then
    echo "=== 3. Provision ClickHouse (mode=${DATA_MODE}) ==="
    ssh "$SERVER" "
        set -euo pipefail
        cd ${REMOTE_DIR}
        # Get the app image the provisioning one-shots will run: pull the
        # published tag by default, or build from source on --build.
        $($DO_BUILD && echo "${COMPOSE} build precis-mcp" || echo "${COMPOSE} pull precis-mcp")
        case '${DATA_MODE}' in
          bundle-sample)
            ${COMPOSE} up -d postgres clickhouse
            for _ in \$(seq 1 30); do
                ${COMPOSE} exec -T postgres pg_isready -U precis >/dev/null 2>&1 && break
                sleep 2
            done
            # The generator targets ClickHouse too — wait for its first-boot to
            # finish (slower than Postgres on a fresh pull) or sample_data races
            # ahead and hits 'connection refused' on :8123.
            for _ in \$(seq 1 30); do
                ${COMPOSE} exec -T clickhouse wget -qO- http://localhost:8123/ping >/dev/null 2>&1 && break
                sleep 2
            done
            # Generate in Postgres → trigger ingestion → apply semantic.* views.
            ${COMPOSE} run --rm --no-deps -e PGDATABASE=${MOCK_SOURCE_DB} \
                precis-mcp python -m precis_mcp.sample_data
            ;;
          bundle-empty)
            ${COMPOSE} up -d clickhouse
            for _ in \$(seq 1 30); do
                ${COMPOSE} exec -T clickhouse wget -qO- http://localhost:8123/ping >/dev/null 2>&1 && break
                sleep 2
            done
            ${COMPOSE} run --rm --no-deps \
                precis-mcp python -m precis_mcp.clickhouse_init --scope open
            ;;
          byo)
            # External ClickHouse (CHHOST). Nothing bundled to start; the
            # provisioner connects to your cluster.
            grep -q '^CHHOST=.\\+' deploy/.env || {
                echo 'ERROR: --data-mode byo needs CHHOST set in deploy/.env (your external ClickHouse host).' >&2
                exit 1
            }
            ${COMPOSE} run --rm --no-deps \
                precis-mcp python -m precis_mcp.clickhouse_init --scope open
            ;;
        esac
        echo '  provisioning complete.'
        # Verify against the bundled CH (byo's external CH may not be exec-able here).
        if [ '${DATA_MODE}' != 'byo' ]; then
            ${COMPOSE} exec -T clickhouse clickhouse-client -q \
                \"SELECT count() FROM system.tables WHERE database='semantic'\" || true
        fi
    "
    echo ""
fi
$DATA_ONLY && { echo "--data-only: done (no Keycloak/server brought up)."; exit 0; }

# ── 4. Server up (migrate + precis-mcp; + Keycloak realm-apply in mode B) ──
# Requires PRECIS_BASE_URL set in deploy/.env. In mode B the keycloak +
# realm-apply one-shots run (bundled-keycloak profile); in mode C (oidc) they are
# skipped and precis-mcp verifies the customer IdP via OIDC_*.
echo "=== 4. Bring up the bundle (auth-mode=${AUTH_MODE}) ==="
ssh "$SERVER" "
    set -euo pipefail
    cd ${REMOTE_DIR}
    grep -q '^PRECIS_BASE_URL=.\\+' deploy/.env || {
        echo 'ERROR: PRECIS_BASE_URL is empty in deploy/.env — set it before the server phase.' >&2
        exit 1
    }
    if [ '${AUTH_MODE}' = 'oidc' ]; then
        grep -q '^OIDC_ISSUER=.\\+' deploy/.env || {
            echo 'ERROR: --auth-mode oidc needs OIDC_ISSUER set in deploy/.env (your external IdP issuer).' >&2
            exit 1
        }
    fi
    # Guard on the .env's actual profile list (not the flag): COMPOSE_PROFILES
    # in an existing deploy/.env is what decides whether the Caddy proxy runs.
    if grep -q '^COMPOSE_PROFILES=.*bundled-proxy' deploy/.env; then
        grep -q '^PRECIS_DOMAIN=.\\+' deploy/.env || {
            echo 'ERROR: the bundled-proxy profile (Caddy) needs PRECIS_DOMAIN set in deploy/.env (bare hostname, no scheme).' >&2
            exit 1
        }
    fi
    # Build from source on --build; otherwise pull the published tag explicitly
    # (so a stale locally-built image of the same tag can't shadow the release)
    # and bring the stack up without building.
    $($DO_BUILD && echo "${COMPOSE} up -d --build" || echo "${COMPOSE} pull precis-mcp && ${COMPOSE} up -d")
    ${COMPOSE} ps
"
echo ""

# ── 5. Health check ──────────────────────────────────────────────────
echo "=== 5. Health check ==="
ssh "$SERVER" "
    set +e
    echo -n 'precis-mcp /health: '; curl -sf http://127.0.0.1:8769/health && echo || echo FAIL
    echo -n 'discovery doc:      '; curl -sf http://127.0.0.1:8769/.well-known/oauth-protected-resource >/dev/null && echo OK || echo FAIL
"
echo ""
echo "=== deploy-mcp.sh complete ==="
