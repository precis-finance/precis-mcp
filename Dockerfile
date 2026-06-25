# Dockerfile.open — staged image build for the open `precis-mcp` package.
#
# The open compose bundles (deploy/docker-compose*.yml) build
# with `context: ..` + `dockerfile: Dockerfile`, so they pick this up unchanged
# in the open repo. It is NOT buildable in the Précis monorepo — the root pyproject.toml
# there is the Précis import-linter config, not the open package metadata.
#
# Lean vs the Précis root Dockerfile: gcc only (psycopg2-binary build), no
# LibreOffice (server-side workbook generation is a Précis feature), the open
# dependency subset from the package pyproject, and an open default CMD.
#
# Pinned to bookworm (not the floating python:3.12-slim, which now resolves to
# trixie): the PGDG repo line below installs the bookworm-pgdg suite, and the
# postgresql-client-16 it serves links libpq5/glibc built for bookworm. A
# trixie userland makes that dependency unsatisfiable. Keep the codename here in
# lockstep with the PGDG suite below.
FROM node:20-bookworm-slim AS excel-addin-builder

WORKDIR /build/excel-addin

# Build the server-hosted Excel add-in bundle from source. Node/npm stay in this
# builder stage; the final Python runtime image receives only dist/.
COPY excel-addin/package.json excel-addin/package-lock.json ./
RUN npm ci
COPY excel-addin ./
RUN npm run build

FROM python:3.12-slim-bookworm

WORKDIR /app

# gcc is kept defensively for any dependency without a manylinux wheel on slim
# (the data + DB stack — psycopg3-binary, clickhouse-connect, pyarrow — all ship
# wheels, so this is belt-and-braces, not a known requirement).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# pg_dump/pg_restore for the backup subsystem (precis_mcp/backup/). The client
# major must match the bundled postgres:16 server — bookworm's stock
# postgresql-client is v15, so pull 16 from PGDG. Bump together with the
# compose postgres image tag.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer — pinned + hashed lockfile (compiled from the package
# metadata; regenerate with `make lock` after editing dependencies). Copied
# first so the layer caches across source-only changes. The lockfile is named
# requirements-open.lock in the Précis monorepo and renamed at export, like this file.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# The package itself — dependencies already satisfied above, hence --no-deps.
COPY pyproject.toml ./
COPY precis_mcp ./precis_mcp
RUN pip install --no-cache-dir --no-deps .

# Optional warehouse drivers, opted into at build via PRECIS_EXTRAS (comma-
# separated warehouse names: bigquery, snowflake, mssql, databricks). The core
# above stays hash-pinned; the opted-in driver is version-pinned by
# constraints-extras.txt — the industry norm for optional connectors
# (dbt/Grafana/Airflow version-pin their drivers; none hash-lock them, because
# pip --require-hashes is all-or-nothing across the transitive closure). mssql
# additionally needs the Microsoft ODBC system driver, installed first when
# requested.
ARG PRECIS_EXTRAS=""
COPY constraints-extras.txt ./
RUN if echo ",${PRECIS_EXTRAS}," | grep -q ",mssql,"; then \
        apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
        && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
           | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
           > /etc/apt/sources.list.d/mssql-release.list \
        && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
        && rm -rf /var/lib/apt/lists/*; \
    fi
RUN if [ -n "$PRECIS_EXTRAS" ]; then \
        pip install --no-cache-dir -c constraints-extras.txt ".[${PRECIS_EXTRAS}]"; \
    fi

# The rest of the open tree: the demo instance fixture (baked as the default;
# the compose mounts a deployer's own over /app/instance), deploy assets
# (reconcile, nginx), scripts (migrate.py), and the Excel add-in manifest
# template. .dockerignore keeps it lean.
COPY . .

# Built Excel add-in bundle served at /excel by precis_mcp.mcp_external.excel_static.
COPY --from=excel-addin-builder /build/excel-addin/dist ./excel-addin/dist

# Non-root runtime user. /data/ingest backs the ingest_dropbox volume, and
# /data/users backs the per-user file-store volume. /backups is shared with the
# bundled ClickHouse server (uid 101), so it is sticky-world-writable like /tmp
# — each writer owns its files; volumes inherit these permissions when first
# initialised from this image.
RUN groupadd -g 10001 precis \
    && useradd -u 10001 -g precis -m -d /home/precis precis \
    && mkdir -p /data/ingest /data/users \
    && chown precis:precis /data/ingest /data/users \
    && mkdir -p /backups && chmod 1777 /backups
ENV HOME=/home/precis
USER precis

# 8768 — single-user dev MCP server (precis_mcp.server); 8769 — multi-user ASGI
# (precis_mcp.app_open). The local-trial bundle overrides CMD to run the former.
EXPOSE 8768 8769

CMD ["uvicorn", "precis_mcp.app_open:app", "--host", "0.0.0.0", "--port", "8769"]
