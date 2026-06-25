# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""MCP JSON-RPC handlers for `/mcp`.

Second transport above the shared tool surface.  Converges with the
LangGraph path at `process_tool_call`; below that gate nothing diverges.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from precis_mcp import oidc
from precis_mcp.auth import (
    AuthContext,
    set_auth_context,
    clear_auth_context,
    clear_call_scope,
)
from precis_mcp.concurrency import TooBusy, read_limiter
from precis_mcp.dispatch import (
    _make_agent_wrapper,
    build_descriptors,
    build_tool_json_schema,
    process_tool_call,
)
from precis_mcp.auth import (
    AccountDisabledError,
    AuthError,
    load_permissions,
    resolve_user_id,
    UserPermissions,
)
from precis_mcp.mcp_external.framing import (
    FramingError,
    MCP_APP_MIME,
    frame_tool_result,
    list_widget_resources,
    mcp_tool_description,
    mcp_tool_variants,
    read_widget_bundle,
    resolve_mcp_tool,
    widget_resource_meta,
    widget_uri_for,
)

logger = logging.getLogger(__name__)


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "precis"
SERVER_VERSION = "0.2.2"

# Synthetic, MCP-only tool whose *result* is the orientation block. The MCP
# `instructions` field (initialize) is dropped by claude.ai and only partly read
# by ChatGPT (first ~512 chars), so a tool result is the one channel that
# delivers the full guidance to the model on both hosts (reactively, when the
# model calls it). Not in TOOL_CATALOGUE — no impact on the Précis app.
ORIENTATION_TOOL = "precis_orientation"
_ORIENTATION_TOOL_ENTRY: dict = {
    "name": ORIENTATION_TOOL,
    "description": (
        "Call this first. Returns how to use Précis over this connector: the "
        "data model (scenarios, metrics, statements, dimensions), the "
        "reporting-tool variants, and how to build charts. Read it before "
        "composing queries."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}


# ---------------------------------------------------------------------------
# Audit + telemetry helpers — `precis.transport=mcp` on every span and a
# matching `security_audit_log` row keep MCP traffic visible alongside the
# LangGraph path.  Best-effort: a failed audit write never blocks the call.
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _write_audit(
    *,
    event_type: str,
    actor_id: str,
    details: dict,
    scenario_id: str | None = None,
) -> None:
    """Append a row to `security_audit_log`.  All MCP-originated rows carry
    `transport: mcp` in the JSONB details so trace / audit queries can filter
    by channel."""
    payload = dict(details)
    payload.setdefault("transport", "mcp")
    from precis_mcp import db
    db.write_security_audit(
        event_type, actor_id, scenario_id=scenario_id, details=payload,
    )


# ---------------------------------------------------------------------------
# Tool index — built once per process from the live catalogue ref.
# ---------------------------------------------------------------------------

_descriptors: dict[str, Any] | None = None
_schemas_by_name: dict[str, dict] | None = None
_wrappers: dict[str, Any] | None = None


def _ensure_tools_loaded() -> None:
    global _descriptors, _schemas_by_name, _wrappers
    if _descriptors is not None:
        return
    from precis_mcp.catalogue_ref import _catalogue_ref  # open: MCP reads the live ref
    descriptors = build_descriptors(_catalogue_ref)
    _descriptors = descriptors
    # JSON Schema is derived Pydantic-natively (no LangChain) only for the
    # tools actually advertised over MCP.
    _schemas_by_name = {
        n: build_tool_json_schema(d)
        for n, d in descriptors.items()
        if d.mcp_read
    }
    _wrappers = {n: _make_agent_wrapper(d) for n, d in descriptors.items()}


def _advertised_tools(permissions: UserPermissions) -> list[dict]:
    """Subset of the tool surface visible over the external MCP transport.

    Exposure is opt-in: only tools whose catalogue entry sets
    `mcp_read: True` are advertised.  `access` (the agent-facing role gate)
    is a separate concern — a tool defaulting to `access="read"` is NOT
    published unless it also opts in via `mcp_read`.  Scenario-scoped tools
    are dropped when the user holds no scenario assignments — there is no
    scenario they could legitimately reference.
    """
    _ensure_tools_loaded()
    assert _descriptors is not None and _schemas_by_name is not None
    # Orientation tool first so the model sees the "call this first" hint early.
    out: list[dict] = [dict(_ORIENTATION_TOOL_ENTRY)]
    for name, desc in _descriptors.items():
        if not desc.mcp_read:
            continue
        if desc.scenario_params and not permissions.scenarios:
            continue
        schema = _schemas_by_name.get(name) or {"type": "object", "properties": {}}
        # Strip params that can't be used over MCP (`out` is pinned per variant;
        # report_id/position belong to the report path, in the Précis app, not here).
        schema = _strip_dead_params(schema)
        # MCP-accurate description (overrides the Précis docstring, which documents
        # out=/report/HITL — false here); fall back to the docstring's first
        # line for any tool without an explicit override.
        base_desc = (
            mcp_tool_description(name)
            or (desc.func.__doc__ or name).strip().split("\n")[0]
        )
        # A tool may be advertised as several variants (render + `_data`); each
        # is a distinct MCP tool name with `out` pinned. Only the render
        # variant (the bare name in `_WIDGET_URI`) carries `_meta.ui`, so the
        # host renders a widget for it and not for the `_data` variant.
        # `mcp_tool_variants` only returns variants whose output mode is
        # available (e.g. the Précis `_excel` variant is dropped when file
        # download isn't configured), so no further per-variant gating here.
        for mcp_name, pinned_out in mcp_tool_variants(name):
            entry: dict = {
                "name": mcp_name,
                "description": _variant_description(base_desc, pinned_out, name),
                "inputSchema": schema,
            }
            w_uri = widget_uri_for(mcp_name)
            if w_uri:
                entry["_meta"] = {
                    "ui": {"resourceUri": w_uri, "visibility": ["model", "app"]},
                    "ui/resourceUri": w_uri,  # legacy mirror per the ext-apps SDK
                }
            out.append(entry)
    return out


# Params that are never usable over the MCP transport, stripped from every
# advertised schema: `out` is pinned per variant; `report_id`/`position` are
# out='report' params and report is rejected on MCP (the report builder is in
# the Précis app, not here). Keeping them would advertise dead arguments the
# model might try.
_DEAD_MCP_PARAMS = frozenset({"out", "report_id", "position"})


def _strip_dead_params(schema: dict) -> dict:
    """Remove the params that can't be used over MCP (`_DEAD_MCP_PARAMS`)."""
    props = schema.get("properties")
    if not isinstance(props, dict) or not (_DEAD_MCP_PARAMS & set(props)):
        return schema
    stripped = {
        **schema,
        "properties": {k: v for k, v in props.items() if k not in _DEAD_MCP_PARAMS},
    }
    req = stripped.get("required")
    if isinstance(req, list):
        stripped["required"] = [r for r in req if r not in _DEAD_MCP_PARAMS]
    return stripped


def _variant_description(base: str, pinned_out: str, tool_name: str) -> str:
    """Append a disambiguating hint to split tools so the model picks the right
    variant; non-split tools keep their docstring summary unchanged."""
    from precis_mcp.mcp_external.framing import _SPLIT_TOOLS
    if tool_name not in _SPLIT_TOOLS:
        return base
    if pinned_out == "agent":
        return (
            f"{base} Returns the raw figures (and a `data_ref`) for your own "
            f"analysis or to build a chart — pass the `data_ref` to "
            f"eval_chart_transform. Does not show the user a table."
        )
    if pinned_out == "excel":
        return (
            f"{base} Generates an Excel file and returns a download link. Present "
            f"that link to the user as a clickable Markdown link verbatim — it is "
            f"the only way they receive the file. The link expires, so tell them "
            f"to click promptly."
        )
    return f"{base} Shows the user a formatted table."


# ---------------------------------------------------------------------------
# Authentication — per-request inside the handler (no middleware on /mcp)
# ---------------------------------------------------------------------------


class _AuthRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail


def _check_mcp_audience(claims: dict) -> None:
    """RFC 8707 audience binding.

    Enforced whenever an audience is resolvable (`PRECIS_BASE_URL` set, or
    an explicit `KC_MCP_AUDIENCE`); see `oidc.mcp_audience`.  Off only on a
    dev box with no base URL configured.
    """
    expected = oidc.mcp_audience()
    if not expected:
        return
    aud = claims.get("aud")
    aud_list = [aud] if isinstance(aud, str) else (aud or [])
    if expected not in aud_list:
        # Don't fail silently. A token that verified (valid signature + issuer)
        # but lacks our /mcp audience almost always means Keycloak is stamping a
        # stale audience after a PRECIS_BASE_URL change, or a self-registered
        # (DCR) client — e.g. claude.ai — never received the precis-mcp scope.
        # Surface the actionable cause; reads only the token's own claims, so no
        # admin credential is involved.
        logger.warning(
            "MCP audience mismatch: token aud=%s, expected %r. If PRECIS_BASE_URL "
            "changed or a new connector self-registered, re-run the Keycloak realm "
            "reconcile (the keycloak-realm-apply one-shot) to restamp the audience "
            "mapper and re-promote the precis-mcp default client scope.",
            aud_list, expected,
        )
        raise _AuthRejected(
            401, f"Token audience does not include {expected!r}"
        )


def _authenticate(request: Request) -> AuthContext:
    token = oidc.extract_session_token(request)
    if not token:
        raise _AuthRejected(401, "Missing Authorization header")
    try:
        claims = oidc.verify_keycloak_token(token)
    except Exception as exc:
        logger.info("MCP token verification failed: %s", exc)
        raise _AuthRejected(401, "Invalid token")
    _check_mcp_audience(claims)
    # Resolve the platform user_id from the token (default: precis_user_id, with
    # a preferred_username fallback for DCR clients; configurable per gap 2 via
    # PRECIS_IDENTITY_CLAIM / PRECIS_IDENTITY_COLUMN).
    user_id = resolve_user_id(claims)
    if not user_id:
        raise _AuthRejected(403, "Token identity claim missing or unmatched")
    try:
        permissions = load_permissions(user_id)
    except AccountDisabledError as exc:
        raise _AuthRejected(403, str(exc))
    except AuthError:
        raise _AuthRejected(403, f"User {user_id!r} not provisioned")
    return AuthContext(user_id=user_id, permissions=permissions)


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------


def _rpc_result(rpc_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Per-session capability cache.
# MCP sessions live across requests; the client sends `initialize` once.
# Captured here keyed by the access token's `jti` claim so framing decisions
# (UI capability) survive the lifetime of one token.  Stateless in the sense
# that nothing persists past process restart — acceptable for the first cut.
# ---------------------------------------------------------------------------


_session_caps: dict[str, dict] = {}


def _session_key(claims_jti: str | None) -> str:
    return claims_jti or "anonymous"


def _apply_presentation_defaults(tool_name: str, cleaned: dict, params) -> None:
    """MCP-only presentation defaults for finance tables: thousands (scale=3)
    and one decimal. Précis seeds these from a per-conversation report context;
    this transport has none, so without a default the engine falls back to
    units/0 which reads poorly for finance. The model can still override by
    passing `scale` / `decimals` explicitly. Not applied in the Précis app —
    this lives only in the MCP dispatch path."""
    if tool_name not in ("run_statement", "run_metric"):
        return
    if "scale" in params and cleaned.get("scale") is None:
        cleaned["scale"] = 3
    if "decimals" in params and cleaned.get("decimals") is None:
        cleaned["decimals"] = 1


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


async def _handle_initialize(
    params: dict,
    claims_jti: str | None,
    auth_ctx: AuthContext,
    request: Request,
    claims_sub: str | None,
) -> dict:
    client_caps = params.get("capabilities") or {}
    _session_caps[_session_key(claims_jti)] = client_caps
    client_info = params.get("clientInfo") or {}
    _write_audit(
        event_type="mcp_session_started",
        actor_id=auth_ctx.user_id,
        details={
            "oauth_sub": claims_sub or "",
            "source_ip": _client_ip(request),
            "client_name": client_info.get("name") or "",
            "client_version": client_info.get("version") or "",
            "protocol_version": params.get("protocolVersion") or "",
        },
    )
    from precis_mcp.mcp_external.instructions import get_mcp_instructions
    result: dict = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            # We serve ui:// widget bundles as resources (resources/read).
            "resources": {"listChanged": False},
            # MCP Apps extension: declare UI support so hosts that render
            # widgets know to fetch the ui:// resources our tools reference.
            "extensions": {
                "io.modelcontextprotocol/ui": {"mimeTypes": [MCP_APP_MIME]},
            },
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }
    # Orientation + data model + cross-tool flows the host folds into the
    # model's context (the MCP analogue of Précis's skill layer). Best-effort —
    # empty string if it can't be composed.
    instructions = get_mcp_instructions()
    if instructions:
        result["instructions"] = instructions
    return result


async def _handle_tools_list(auth_ctx: AuthContext) -> dict:
    set_auth_context(auth_ctx)
    try:
        return {"tools": _advertised_tools(auth_ctx.permissions)}
    finally:
        clear_auth_context()


# ---------------------------------------------------------------------------
# Render-block seam — open builders native, Précis registered.
# ---------------------------------------------------------------------------
# The open MCP transport renders its own two block types directly from pure
# builders (no `streaming`/`emit_blocks` dependency). The Précis platform
# registers extra render builders — the chart widget — at startup via
# `register_mcp_render_builder`, so charts (Redis + turn-scoped) stay out of the
# open package. A builder takes the JSON-normalised tool result + the tool args +
# the auth context and returns a single block dict, or None.

from precis_mcp.inspection_grid_builder import build_inspection_grid_block
from precis_mcp.table_builder import build_financial_table_block


def _render_financial_table(
    data: dict, tool_args: dict, auth_ctx: AuthContext,
) -> dict | None:
    caption = data.get("caption") if isinstance(data, dict) else None
    # The MCP render variant is the surface the Excel add-in consumes, so it
    # carries the resolved `nf` / `alerts` enrichment (add-in spec §5).
    return build_financial_table_block(data, caption=caption, for_excel=True)


def _render_inspection_grid(
    data: dict, tool_args: dict, auth_ctx: AuthContext,
) -> dict | None:
    return build_inspection_grid_block(data)


_MCP_RENDER_BUILDERS: dict[str, Any] = {
    "run_statement": _render_financial_table,
    "run_metric": _render_financial_table,
    "inspect_rows": _render_inspection_grid,
}


def register_mcp_render_builder(tool_name: str, builder) -> None:
    """Register a render-block builder for a tool's `out='render'` variant.

    The open package registers the finance-table and inspection-grid builders
    natively; the Précis platform calls this at startup to add its premium
    render surfaces (charts) without the open package importing them.
    """
    _MCP_RENDER_BUILDERS[tool_name] = builder


def _derive_render_block(
    tool_name: str, result: Any, tool_args: dict, auth_ctx: AuthContext,
) -> dict | None:
    """Derive the structured block for `out='render'` via the render-builder
    seam. The open builders (finance table, inspection grid) run natively; the
    Précis chart builder is registered at startup. Returns None when no
    builder is registered for the tool — the caller then falls back to the raw
    data envelope. Results are JSON-normalised first so a builder sees exactly
    what Précis's renderers see (the `json.dumps(..., default=str)` round-trip)."""
    builder = _MCP_RENDER_BUILDERS.get(tool_name)
    if builder is None:
        return None
    try:
        data = json.loads(json.dumps(result, default=str))
    except (TypeError, ValueError):
        logger.exception("MCP render: result not serialisable for %s", tool_name)
        return None
    try:
        return builder(data, tool_args, auth_ctx)
    except Exception:
        logger.exception("MCP render builder failed for %s", tool_name)
        return None


async def _handle_tools_call(params: dict, auth_ctx: AuthContext) -> dict:
    from precis_mcp.observability import get_tracer
    tracer = get_tracer(__name__)

    mcp_name = params.get("name") or ""
    args = dict(params.get("arguments") or {})

    with tracer.start_as_current_span("mcp.tool_call") as span:
        span.set_attribute("precis.transport", "mcp")
        span.set_attribute("precis.user_id", auth_ctx.user_id)
        span.set_attribute("precis.tool_name", mcp_name)

        if not mcp_name:
            span.set_attribute("precis.is_error", True)
            _audit_call(auth_ctx, name="", out_mode="", outcome="bad_request",
                        error="missing name")
            return {
                "isError": True,
                "content": [{"type": "text", "text": "Missing 'name' in params"}],
            }

        # Orientation tool — synthetic, MCP-only; its result is the guidance
        # block. No engine call, no descriptor; just return the instructions.
        if mcp_name == ORIENTATION_TOOL:
            from precis_mcp.mcp_external.instructions import get_mcp_instructions
            _audit_call(auth_ctx, name=mcp_name, out_mode="", outcome="ok")
            return {
                "content": [{"type": "text", "text": get_mcp_instructions()}],
                "isError": False,
            }

        # An advertised MCP name may be a variant (`run_statement_data`); resolve
        # it back to the underlying tool plus the `out` mode the variant pins.
        name, pinned_out = resolve_mcp_tool(mcp_name)
        span.set_attribute("precis.out_mode", pinned_out)

        _ensure_tools_loaded()
        assert _descriptors is not None and _wrappers is not None
        desc = _descriptors.get(name)
        if desc is None:
            span.set_attribute("precis.is_error", True)
            _audit_call(auth_ctx, name=mcp_name, out_mode="", outcome="not_found")
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Tool not found: {mcp_name}"}],
            }
        # Opt-in exposure: must be flagged mcp_read, and (defense-in-depth)
        # still hold a read-class access level — so a tool mistakenly
        # flagged mcp_read on a write/admin entry can never be invoked here.
        if not desc.mcp_read or desc.access not in ("read", "general"):
            span.set_attribute("precis.is_error", True)
            _audit_call(auth_ctx, name=mcp_name, out_mode="", outcome="not_exposed")
            return {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": f"Tool not exposed over MCP: {mcp_name}",
                }],
            }

        from precis_mcp.catalogue_ref import _catalogue_ref
        set_auth_context(auth_ctx)
        scenario_id: str | None = None
        try:
            cleaned, err = process_tool_call(
                desc, args, {}, catalogue=_catalogue_ref.current,
            )
            scenario_id = _first_scenario(cleaned, desc.scenario_params)
            if err:
                span.set_attribute("precis.is_error", True)
                _audit_call(auth_ctx, name=mcp_name, out_mode=pinned_out,
                            outcome="denied", error=err,
                            scenario_id=scenario_id)
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": err}],
                }
            wrapper = _wrappers[name]
            # Pin the variant's `out` for tools that accept it; strip it for
            # tools whose signature doesn't declare it.
            import inspect as _inspect
            sig_params = _inspect.signature(desc.func).parameters
            if "out" in sig_params:
                cleaned["out"] = pinned_out
            else:
                cleaned.pop("out", None)
            _apply_presentation_defaults(name, cleaned, sig_params)
            try:
                # Bound concurrent read/engine work per user (and globally) so a
                # workbook refresh fan-out can't flood ClickHouse. The slot wraps
                # only the engine call, not the gate/render work around it.
                async with read_limiter.acquire(auth_ctx.user_id):
                    result = await asyncio.to_thread(wrapper, **cleaned)
            except TooBusy as exc:
                span.set_attribute("precis.is_error", True)
                _audit_call(auth_ctx, name=mcp_name, out_mode=pinned_out,
                            outcome="throttled", error=str(exc),
                            scenario_id=scenario_id)
                # Surfaced as a retryable tool error: the JSON-RPC envelope is
                # always HTTP 200, so the busy signal rides in the result, not
                # the status. The client's own concurrency gate normally keeps
                # this from ever tripping — it's the backstop for a client that
                # doesn't self-throttle.
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": str(exc)}],
                }
            except Exception as exc:
                logger.exception("MCP tool %s failed", mcp_name)
                span.set_attribute("precis.is_error", True)
                span.record_exception(exc)
                _audit_call(auth_ctx, name=mcp_name, out_mode=pinned_out,
                            outcome="exception", error=str(exc),
                            scenario_id=scenario_id)
                return {
                    "isError": True,
                    # Detail stays in the server log — driver exceptions can
                    # embed SQL or internal paths.
                    "content": [{"type": "text", "text": (
                        f"Tool '{mcp_name}' failed with an internal "
                        f"{type(exc).__name__}; details were logged."
                    )}],
                }
            try:
                # Render variants derive the block the widget binds to; `_data`
                # variants (out='agent') don't — they have no widget.
                render_block = (
                    _derive_render_block(name, result, cleaned, auth_ctx)
                    if pinned_out == "render" else None
                )
                framed = frame_tool_result(
                    tool_name=name,
                    out=pinned_out,
                    tool_result=result,
                    render_block=render_block,
                )
                _audit_call(auth_ctx, name=mcp_name, out_mode=pinned_out,
                            outcome="ok", scenario_id=scenario_id)
                return framed
            except FramingError as exc:
                span.set_attribute("precis.is_error", True)
                _audit_call(auth_ctx, name=mcp_name, out_mode=pinned_out,
                            outcome="framing_error", error=str(exc),
                            scenario_id=scenario_id)
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": str(exc)}],
                }
        finally:
            clear_auth_context()
            clear_call_scope()


def _first_scenario(args: dict, scenario_params: tuple[str, ...]) -> str | None:
    for p in scenario_params:
        v = args.get(p)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                sid = first.get("scenario_id") or first.get("id")
                if isinstance(sid, str):
                    return sid
        if isinstance(v, dict):
            sid = v.get("scenario_id") or v.get("id")
            if isinstance(sid, str):
                return sid
    return None


def _audit_call(
    auth_ctx: AuthContext,
    *,
    name: str,
    out_mode: str,
    outcome: str,
    error: str | None = None,
    scenario_id: str | None = None,
) -> None:
    details: dict[str, Any] = {
        "tool_name": name,
        "out": out_mode,
        "outcome": outcome,
    }
    if error:
        details["error"] = error[:500]
    _write_audit(
        event_type="mcp_tool_call",
        actor_id=auth_ctx.user_id,
        details=details,
        scenario_id=scenario_id,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("")
async def mcp_endpoint(request: Request):
    """JSON-RPC entry point.  Handles `initialize`, `tools/list`, `tools/call`."""
    try:
        auth_ctx = _authenticate(request)
    except _AuthRejected as exc:
        logger.info("MCP auth rejected (%s): %s", exc.status_code, exc.detail)
        headers: dict[str, str] = {}
        if exc.status_code == 401:
            # RFC 9728 / MCP auth spec: point the client at the
            # protected-resource-metadata document so its OAuth bootstrap
            # can discover the authorization server without guessing the URL.
            meta_url = (
                f"{oidc.public_base(str(request.base_url))}"
                f"/.well-known/oauth-protected-resource"
            )
            headers["WWW-Authenticate"] = (
                f'Bearer resource_metadata="{meta_url}"'
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )

    # Cache the jti for per-session capability lookup.  Re-parse claims here
    # so initialize's _session_caps key can survive across calls within the
    # life of a single access token, and so the session-start audit can name
    # the OAuth subject.
    token = oidc.extract_session_token(request) or ""
    try:
        claims = oidc.verify_keycloak_token(token)
        claims_jti = claims.get("jti")
        claims_sub = claims.get("sub")
    except Exception:
        claims_jti = None
        claims_sub = None

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"detail": "Body must be JSON-RPC 2.0"},
        )

    if isinstance(body, list):
        responses = []
        for rpc in body:
            resp = await _dispatch_one(
                rpc, auth_ctx, claims_jti, claims_sub, request,
            )
            if resp is not None:
                responses.append(resp)
        # Batch of only notifications: no reply. 202 Accepted, body-less.
        if not responses:
            return Response(status_code=202)
        return JSONResponse(content=responses)

    resp = await _dispatch_one(body, auth_ctx, claims_jti, claims_sub, request)
    if resp is None:
        # Notification (no id): no reply. 202 Accepted, body-less — a status that
        # carries a body would desync Content-Length and reset the connection.
        return Response(status_code=202)
    return JSONResponse(content=resp)


async def _dispatch_one(
    rpc: dict,
    auth_ctx: AuthContext,
    claims_jti: str | None,
    claims_sub: str | None,
    request: Request,
) -> dict | None:
    if not isinstance(rpc, dict):
        return _rpc_error(None, -32600, "Invalid Request: not a JSON-RPC object")
    rpc_id = rpc.get("id")
    method = rpc.get("method")
    params = rpc.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # Notifications (no id) get no response.
    is_notification = "id" not in rpc

    try:
        if method == "initialize":
            return _rpc_result(rpc_id, await _handle_initialize(
                params, claims_jti, auth_ctx, request, claims_sub,
            ))
        if method == "tools/list":
            return _rpc_result(rpc_id, await _handle_tools_list(auth_ctx))
        if method == "tools/call":
            return _rpc_result(
                rpc_id, await _handle_tools_call(params, auth_ctx),
            )
        if method == "resources/list":
            return _rpc_result(rpc_id, {"resources": list_widget_resources()})
        if method == "resources/read":
            uri = params.get("uri") or ""
            html = read_widget_bundle(uri)
            if html is None:
                # MCP RESOURCE_NOT_FOUND. Only built ui:// widget bundles
                # resolve; any other URI (incl. path-traversal attempts) 404s.
                return _rpc_error(rpc_id, -32002, f"Resource not found: {uri}")
            # claude.ai's ui.domain is hashed from the MCP server URL it
            # connected with; derive that URL the same way the audience does.
            server_url = f"{oidc.public_base(str(request.base_url))}/mcp"
            return _rpc_result(rpc_id, {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": MCP_APP_MIME,
                        "text": html,
                        # Hosts require a CSP + unique domain on the template
                        # or they won't render the widget (MCP Apps spec).
                        "_meta": widget_resource_meta(uri, server_url=server_url),
                    },
                ],
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if is_notification:
            return None
        return _rpc_error(rpc_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        logger.exception("MCP dispatch failed for method=%s", method)
        if is_notification:
            return None
        return _rpc_error(rpc_id, -32603, "Internal error; details were logged")
