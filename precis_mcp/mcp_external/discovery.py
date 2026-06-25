# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""RFC 9728 OAuth-protected-resource discovery.

Advertises that the `/mcp` resource server delegates token issuance to the
Keycloak `precis` realm.  External MCP clients (claude.ai, ChatGPT) hit
``/.well-known/oauth-protected-resource`` to discover the AS endpoint and
PKCE/DCR configuration; from there they negotiate directly against Keycloak.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request

from precis_mcp import oidc


router = APIRouter()


def _excel_addin_client_id() -> str | None:
    """The Excel add-in's public client_id to advertise, or ``None`` when the
    surface is not enabled on this deployment.

    The add-in is a public (PKCE, no-secret) OAuth client distinct from the SPA's
    confidential client. It is discovered by the add-in from this metadata so the
    pane stays zero-config:

    - **Mode C** (external IdP): an explicitly registered public client, named by
      ``EXCEL_ADDIN_CLIENT_ID``.
    - **Mode B** (bundled Keycloak): the ``precis-excel-addin`` realm client, when
      ``KC_ENABLE_EXCEL_ADDIN`` is on (the same gate the realm reconcile uses).

    Absent in both → the add-in surface is off and nothing is advertised.
    """
    explicit = os.environ.get("EXCEL_ADDIN_CLIENT_ID", "").strip()
    if explicit:
        return explicit
    if os.environ.get("KC_ENABLE_EXCEL_ADDIN", "").strip().lower() == "true":
        return "precis-excel-addin"
    return None


def excel_addin_enabled() -> bool:
    """Whether the Excel add-in surface is enabled on this deployment.

    The single predicate behind both the advertised client_id (above) and the
    server-hosted static bundle + manifest (``excel_static``): true when either
    identity gate is set (``EXCEL_ADDIN_CLIENT_ID`` for mode C, or
    ``KC_ENABLE_EXCEL_ADDIN`` for the bundled Keycloak client).
    """
    return _excel_addin_client_id() is not None


def _excel_addin_scopes() -> list[str]:
    """Scopes the add-in should request for the `/mcp` resource (the standard
    RFC 9728 ``scopes_supported``).

    Default ``["openid"]`` — with the RFC 8707 resource indicator (below) the
    authorization server binds the audience, so no resource scope is needed
    (Keycloak / Auth0 / Ping). For an IdP that binds the audience through a scope
    instead — Entra, Okta — set ``EXCEL_ADDIN_SCOPE`` to the space-separated
    request scope, e.g. ``openid offline_access api://<app-id>/access_as_user``
    (include ``offline_access`` so the add-in gets a refresh token).
    """
    raw = os.environ.get("EXCEL_ADDIN_SCOPE", "").strip()
    if raw:
        return raw.split()
    return ["openid"]


def _resource_parameter_supported() -> bool:
    """Whether the authorization server honours the RFC 8707 ``resource`` parameter.

    A capability hint, kept independent of ``scopes_supported`` — a conformant AS
    can support *both* a resource scope and the resource parameter, so the two
    must not be inferred from each other. True by default (the standards-pure MCP
    shape: Keycloak / Auth0 / Ping). Set ``EXCEL_ADDIN_RESOURCE_INDICATOR=false``
    for an AS that rejects it (Entra's v2 endpoint returns ``AADSTS901002``); the
    audience is then bound via the request scope (``EXCEL_ADDIN_SCOPE``) instead.
    """
    raw = os.environ.get("EXCEL_ADDIN_RESOURCE_INDICATOR", "").strip().lower()
    return raw not in ("0", "false", "no")


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> dict:
    """Point external MCP hosts at the Keycloak realm's OIDC discovery."""
    base = oidc.public_base(str(request.base_url))
    resource = oidc.mcp_audience() or f"{base}/mcp"
    meta: dict = {
        "resource": resource,
        "authorization_servers": [oidc.config.issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://precis.finance/docs/mcp",
    }
    # Précis extension: the Excel add-in's public client_id, so the add-in
    # auto-configures from the /mcp URL alone (IdP-agnostic). Absent when the
    # add-in surface is not enabled on this deployment.
    excel_client_id = _excel_addin_client_id()
    if excel_client_id:
        meta["precis_excel_client_id"] = excel_client_id
        # How the add-in should request a token for this resource, so it stays
        # generic across IdPs. `scopes_supported` is the standard RFC 9728 field;
        # `precis_resource_indicator` is a Précis extension flagging whether the AS
        # honours the RFC 8707 `resource` parameter. Defaults preserve the
        # Keycloak/Auth0/Ping shape (scope=openid + resource indicator); Entra/Okta
        # advertise a request scope and turn the resource indicator off.
        meta["scopes_supported"] = _excel_addin_scopes()
        meta["precis_resource_parameter_supported"] = _resource_parameter_supported()
    return meta
