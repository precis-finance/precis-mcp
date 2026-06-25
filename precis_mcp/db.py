# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Database connection helpers for ClickHouse, Redis, and PostgreSQL (platform)."""

import atexit
import json
import logging
import os
from contextlib import contextmanager
from typing import Any

import clickhouse_connect
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _env_truthy(name: str) -> bool:
    return os.getenv(name, '').strip().lower() in ('1', 'true', 'yes')


_ch_pool_mgr = None


def _clickhouse_pool_manager():
    """Process-wide urllib3 pool manager with an explicit per-host maxsize.

    clickhouse-connect otherwise shares a default manager whose per-host pool
    size is incidental, so a query burst can spawn (and discard) connections
    beyond it. Sizing it deliberately to ``PRECIS_CLICKHOUSE_POOL_MAXSIZE`` —
    set >= the read-concurrency global cap (``precis_mcp/concurrency.py``) —
    makes the read-path semaphore the binding limit on a burst, and lets every
    admitted query reuse a pooled connection instead of churning.
    """
    global _ch_pool_mgr
    if _ch_pool_mgr is None:
        from clickhouse_connect.driver import httputil

        maxsize = int(os.getenv('PRECIS_CLICKHOUSE_POOL_MAXSIZE', '32'))
        _ch_pool_mgr = httputil.get_pool_manager(maxsize=maxsize)
    return _ch_pool_mgr


def get_clickhouse_client(**overrides):
    """ClickHouse client — used for metadata, hierarchy lookups, and direct queries.

    TLS is opt-in via ``CHSECURE`` (default off). It must be enabled whenever
    ClickHouse is not co-located with the engine — a BYO / managed / ClickHouse
    Cloud target carries the full plan+actuals model, so a remote link must not
    be plaintext. When secure, the default port flips to 8443 (the HTTPS port),
    ``CHCACERT`` pins a CA, and ``CHVERIFY=false`` allows a self-signed dev cert.

    ``overrides`` are passed through to ``clickhouse_connect.get_client`` on
    top of the env-derived settings (e.g. ``send_receive_timeout`` for the
    long-running BACKUP/RESTORE commands).
    """
    secure = _env_truthy('CHSECURE')
    kwargs: dict[str, Any] = dict(
        host=os.getenv('CHHOST', 'localhost'),
        port=int(os.getenv('CHPORT', 8443 if secure else 8123)),
        username=os.getenv('CHUSER', 'default'),
        password=os.getenv('CHPASSWORD', ''),
    )
    if secure:
        kwargs['secure'] = True
        ca_cert = os.getenv('CHCACERT')
        if ca_cert:
            kwargs['ca_cert'] = ca_cert
        verify = os.getenv('CHVERIFY')
        if verify is not None:
            kwargs['verify'] = _env_truthy('CHVERIFY')
    kwargs.update(overrides)
    # Share the process-wide pool manager unless a caller pins its own (e.g. a
    # long-running BACKUP/RESTORE client with a different timeout profile).
    kwargs.setdefault('pool_mgr', _clickhouse_pool_manager())
    return clickhouse_connect.get_client(**kwargs)


def query_clickhouse(sql: str, parameters: dict | None = None) -> list[dict]:
    """Execute a ClickHouse query and return results as list of dicts.

    Handles client lifecycle. Returns empty list on no results.
    """
    client = get_clickhouse_client()
    result = client.query(sql, parameters=parameters or {})
    cols = result.column_names
    return [dict(zip(cols, row)) for row in result.result_rows]


def get_redis_client():
    """Redis client for the Précis platform (plan-write locks, HITL preview
    cache). Lazy import: no open path uses Redis — the ingestion lock is
    Postgres-backed — so the open package does not depend on it."""
    import redis

    return redis.Redis(
        host=os.getenv('REDIS_HOST', 'localhost'),
        port=int(os.getenv('REDIS_PORT', 6379)),
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# PostgreSQL — precis_platform (operational platform data)
#
# psycopg3 + a process-wide ConnectionPool. Every runtime platform query funnels
# through query_platform / execute_platform / transaction_platform, so pooling
# here removes the per-query connect+auth churn the old per-call connect pattern
# incurred. Connections default to dict rows, so `row['col']` access is unchanged
# for the ~280 call-sites. `pool.connection()` is the unit of transaction: it
# commits on clean exit and rolls back on any exception.
# ---------------------------------------------------------------------------


def _platform_conninfo() -> str:
    """libpq conninfo from the PG* env vars, including opt-in TLS.

    TLS is opt-in via ``PGSSLMODE`` (unset → libpq's ``prefer``, fine for a
    co-located/loopback platform DB). For a remote / managed / BYO-cloud DB set
    ``PGSSLMODE=verify-full`` plus ``PGSSLROOTCERT`` — not ``require``, which
    encrypts but validates no server identity (MITM-able).
    """
    params: dict[str, Any] = dict(
        host=os.getenv('PGHOST', 'localhost'),
        port=int(os.getenv('PGPORT', 5432)),
        user=os.getenv('PGUSER', 'postgres'),
        password=os.getenv('PGPASSWORD', ''),
        dbname=os.getenv('PLATFORM_DB_NAME', 'precis_platform'),
        # Bound a single connect attempt so an unreachable/misconfigured DB
        # fails fast instead of hanging the caller (and, with the pool, the
        # background reconnect worker).
        connect_timeout=int(os.getenv('PG_CONNECT_TIMEOUT', '5')),
    )
    sslmode = os.getenv('PGSSLMODE')
    if sslmode:
        params['sslmode'] = sslmode
        sslrootcert = os.getenv('PGSSLROOTCERT')
        if sslrootcert:
            params['sslrootcert'] = sslrootcert
    return make_conninfo(**params)


_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    """Return the process-wide platform-DB pool, creating it on first use.

    ``min_size`` defaults to **0**: creating the pool does NOT connect eagerly,
    so importing/booting with an unreachable DB never blocks — only an actual
    query opens a connection, and that is bounded by ``connect_timeout`` (per
    attempt) and the pool ``timeout`` (acquire). A deployment that wants warm
    connections sets ``PG_POOL_MIN`` > 0, accepting an eager connect at open.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _platform_conninfo(),
            min_size=int(os.getenv('PG_POOL_MIN', '0')),
            max_size=int(os.getenv('PG_POOL_MAX', '10')),
            timeout=float(os.getenv('PG_POOL_TIMEOUT', '10')),
            kwargs={'row_factory': dict_row},
            name='precis-platform',
            open=True,
        )
        atexit.register(close_platform_pool)
    return _pool


def open_platform_pool() -> None:
    """Create (and, if ``PG_POOL_MIN`` > 0, warm) the platform-DB pool.

    Call once at app-lifespan startup. With the default ``PG_POOL_MIN=0`` this
    just instantiates the pool without connecting; the first query establishes
    a connection. Mirrors the checkpointer's lifespan pattern in the
    Précis agent.
    """
    _get_pool()


def close_platform_pool() -> None:
    """Close the platform-DB pool. Idempotent; safe at shutdown.

    Called from the app-lifespan shutdown; also registered via ``atexit`` on
    first use so non-lifespan contexts (CLI, scripts) tear down cleanly too.
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_platform_db():
    """A standalone (non-pooled) psycopg3 connection to precis_platform.

    The runtime path uses the pooled helpers below; this is the escape hatch
    for a raw connection (caller closes it), same env vars + opt-in TLS as the
    pool. No runtime caller uses it today.
    """
    return psycopg.connect(_platform_conninfo(), row_factory=dict_row)  # type: ignore[call-overload]


def query_platform(sql: str, params: tuple | list | None = None) -> list[dict[str, Any]]:
    """Execute a read query against precis_platform and return rows as dicts."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # sql is dynamic by design at this helper boundary (psycopg3 types
            # execute() to LiteralString to discourage injection; the safety
            # here is the parameterised %s args, not a literal query).
            cur.execute(sql, params)  # type: ignore[arg-type]
            return cur.fetchall()


def execute_platform(sql: str, params: tuple | list | None = None) -> dict[str, Any] | None:
    """Execute a write query against precis_platform.

    Returns the first row if the query has a RETURNING clause, else None. The
    connection context commits on clean exit and rolls back on any exception.
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            if cur.description:
                return cur.fetchone()
            return None


@contextmanager
def transaction_platform():
    """Yield a cursor inside an explicit transaction against precis_platform.

    Use when you need multiple statements with rollback-on-error semantics —
    e.g. ``SELECT ... FOR UPDATE`` followed by ``UPDATE``, or a transaction-
    scoped advisory lock (`pg_advisory_xact_lock`, released at commit). The
    connection context commits on clean exit, rolls back on any exception, and
    returns the connection to the pool either way.

    Usage:
        with transaction_platform() as cur:
            cur.execute("SELECT ... FOR UPDATE", (...))
            row = cur.fetchone()
            cur.execute("UPDATE ...", (...))
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur


def _current_trace_id() -> str | None:
    """Hex trace id of the active OpenTelemetry span, or None off-span.

    Lets an audit row be joined to the trace of the turn that produced it. The
    admin CLI and other headless ops run off-span, so this returns None there —
    those rows carry no trace_id, which is expected.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


def write_security_audit(
    event_type: str,
    actor_id: str,
    *,
    target_user_id: str | None = None,
    scenario_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Append one row to ``security_audit_log`` — the single audit writer.

    Best-effort by contract: an audit write must never block or fail the
    operation it records, so every failure is logged and swallowed. ``trace_id``
    is derived from the active span automatically, so callers on the tool-call
    path get trace↔audit correlation for free.
    """
    try:
        execute_platform(
            """
            INSERT INTO security_audit_log
                (event_type, actor_id, target_user_id, scenario_id, details, trace_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                event_type,
                actor_id,
                target_user_id,
                scenario_id,
                json.dumps(details or {}, default=str),
                _current_trace_id(),
            ),
        )
    except Exception:
        logger.exception(
            "audit write failed (event=%s, actor=%s) — continuing",
            event_type, actor_id,
        )
