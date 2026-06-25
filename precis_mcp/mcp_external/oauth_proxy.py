# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""OAuth-proxy shim — compatibility fallback for claude.ai's remote-MCP connector.

Historically claude.ai's connector implemented the MCP 2025-03-26 spec: it
ignored the ``authorization_servers`` pointer in our protected-resource metadata
and constructed ``/authorize``, ``/token``, ``/register`` and the AS metadata
document relative to the MCP server's own origin (anthropics/claude-ai-mcp#82,
closed "not planned"). This router hosts those endpoints at the origin and
forwards them to Keycloak so that legacy path can still complete.

That origin-relative path only engages when the ``401`` omits the
``WWW-Authenticate: Bearer resource_metadata="…"`` header. Because we always send
that header, an up-to-date client — claude.ai, ChatGPT, Claude Code — instead
reads ``/.well-known/oauth-protected-resource`` (discovery.py), follows it to the
Keycloak issuer, and talks to Keycloak directly, never touching this shim. The
shim is therefore a compatibility fallback rather than the primary path, kept
because a client's discovery behaviour is version- and configuration-dependent
and the shim is transparent (mints no tokens, holds no secrets) and cheap.

The proxy is transparent: it mints no tokens, holds no secrets, adds no
policy.  ``/register`` and ``/token`` are forwarded to Keycloak
server-to-server (internal URL); ``/authorize`` is a browser 302 to
Keycloak's public authorize endpoint.  It exposes nothing that ``/auth/``
doesn't already expose publicly — these are origin-path aliases that exist
solely to satisfy claude.ai's base-relative URL construction.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from precis_mcp import oidc
from precis_mcp.auth_mode import AuthMode, AuthModeError, resolve_auth_mode

logger = logging.getLogger(__name__)

_FORWARD_TIMEOUT = 15.0


async def _keycloak_only() -> None:
    """Gate the shim to mode B (bundled Keycloak).

    This proxy exists only for claude.ai's pre-RFC-9728 connector against the
    bundled Keycloak — it builds Keycloak-shaped endpoints. In mode C (direct
    external OIDC) it would advertise the wrong endpoints, and modern clients use
    RFC 9728 discovery (discovery.py) to reach the external IdP directly, so the
    shim is both wrong and unnecessary → 404.
    """
    try:
        mode = resolve_auth_mode(default=AuthMode.KEYCLOAK)
    except AuthModeError:
        mode = None
    if mode is not AuthMode.KEYCLOAK:
        raise HTTPException(
            status_code=404,
            detail="OAuth proxy shim is available only with bundled Keycloak (mode B)",
        )


router = APIRouter(dependencies=[Depends(_keycloak_only)])


def _registration_endpoint_internal() -> str:
    c = oidc.config
    return f"{c.base_url_internal}/realms/{c.realm}/clients-registrations/openid-connect"


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> dict:
    """RFC 8414 AS metadata at the origin (old-spec location claude.ai uses).

    The endpoints point back at our origin so claude.ai's view is
    self-consistent — though claude.ai ignores these values and rebuilds
    them base-relative regardless.  ``issuer`` is our origin (the AS
    claude.ai believes it's talking to is this proxy); the access token it
    ultimately receives carries Keycloak's ``iss``, which only the ``/mcp``
    middleware inspects.
    """
    base = oidc.public_base(str(request.base_url))
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none", "client_secret_post", "client_secret_basic",
        ],
        # No `scopes_supported`: advertising a scope set drives a client to request
        # exactly those scopes at dynamic registration, and Keycloak then derives
        # the client's scope set from the request — dropping the realm-default
        # `precis-mcp` audience scope, so the token loses its `/mcp` audience and
        # 401s. Omitting it lets the legacy-path client register with no scopes and
        # inherit the realm defaults (incl. the audience scope), mirroring the
        # modern path. Same reasoning as the protected-resource doc (discovery.py).
    }


@router.post("/register")
async def register(request: Request) -> Response:
    """Forward an anonymous DCR request to Keycloak's registration endpoint."""
    body = await request.body()
    async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT) as client:
        resp = await client.post(
            _registration_endpoint_internal(),
            content=body,
            headers={"Content-Type": "application/json"},
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.get("/authorize")
async def authorize(request: Request) -> RedirectResponse:
    """302 the browser to Keycloak's authorize endpoint, query forwarded."""
    target = oidc.config.authorize_endpoint
    qs = request.url.query
    return RedirectResponse(url=f"{target}?{qs}" if qs else target, status_code=302)


@router.post("/token")
async def token(request: Request) -> Response:
    """Forward the token request body verbatim to Keycloak's token endpoint.

    PKCE means the public DCR client sends no secret; a confidential client
    sends ``Authorization: Basic`` which we pass through.
    """
    body = await request.body()
    headers = {
        "Content-Type": request.headers.get(
            "content-type", "application/x-www-form-urlencoded"
        ),
    }
    authz = request.headers.get("authorization")
    if authz:
        headers["Authorization"] = authz
    async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT) as client:
        resp = await client.post(
            oidc.config.token_endpoint, content=body, headers=headers,
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
