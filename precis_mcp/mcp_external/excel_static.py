# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Server-hosted Excel add-in bundle + host-templated Office manifest.

Mirrors the MCP-widget bundle convention in ``framing.py``: a built frontend
artifact under a package-relative ``dist/``, served only when the surface is
enabled *and* the bundle is present, with path-traversal guarding. The
manifest's asset URLs are rewritten from the manifest-template origin to this
deployment's serving origin using ``oidc.public_base`` — the same origin helper
discovery and the widget server use — so the add-in loads same-origin with
``/mcp`` and does not require a ``CORS_ORIGINS`` entry for `/mcp`.

Both transports mount this: the open standalone (``app_open``) and the Précis
agent app (``agui``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from precis_mcp import oidc
from precis_mcp.mcp_external.discovery import excel_addin_enabled

logger = logging.getLogger(__name__)

# The placeholder origin baked into the checked-in manifest template, rewritten
# to the serving origin at request time. Clients should use /excel/manifest.xml,
# not this template origin.
_DEV_ORIGIN = "https://localhost:3000"

# URL prefix the bundle is served under (and the manifest's asset origin).
_MOUNT = "/excel"

# office.js is loaded from Microsoft's CDN by the task pane — the only external
# script origin the add-in needs.
_OFFICE_JS_ORIGIN = "https://appsforoffice.microsoft.com"


def _issuer_origin() -> str | None:
    """``scheme://host`` of the configured OAuth issuer, or ``None``.

    Used for the manifest ``<AppDomains>`` (the sign-in dialog navigates to the
    IdP) and the CSP ``connect-src`` (the task pane fetches the issuer's OpenID
    configuration and token endpoint during sign-in). A bundled-Keycloak issuer
    is same-origin with ``/excel`` (``'self'`` already covers it); an external
    IdP's is a separate origin.
    """
    parts = urlsplit(getattr(oidc.config, "issuer", "") or "")
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def _excel_headers() -> dict[str, str]:
    """Security headers for ``/excel``, set by the app so they are identical
    behind any proxy (Caddy on the open server, hardened nginx on the commercial
    demo, a customer's own ingress).

    The add-in is a public, read-only static bundle that *must* be framable by
    Excel and load office.js cross-origin — the opposite of the SPA's posture —
    so this policy is scoped to ``/excel`` and touches nothing else on the origin:

    - open CORS, because Excel-on-the-web fetches ``functions.json`` + the
      manifest cross-origin from Microsoft's runtime origin (the bearer token
      only ever goes to ``/mcp``, so nothing sensitive is exposed);
    - ``Cross-Origin-Resource-Policy: cross-origin`` so the Office host can load
      the ribbon icons;
    - a tight CSP that locks script/style/font/img/connect but omits
      ``frame-ancestors`` and ``X-Frame-Options`` so Excel (web and desktop) can
      frame the task pane, with ``connect-src`` carrying the OAuth issuer for the
      sign-in discovery/token fetches.

    A hardening proxy must not re-add ``frame-ancestors`` / ``X-Frame-Options``
    on ``/excel`` or it will smother this.
    """
    connect = "'self'"
    issuer_origin = _issuer_origin()
    if issuer_origin:
        connect = f"'self' {issuer_origin}"
    csp = (
        "default-src 'self'; "
        f"script-src 'self' {_OFFICE_JS_ORIGIN}; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        f"connect-src {connect}; "
        "object-src 'none'; "
        "base-uri 'self'"
    )
    return {
        "Access-Control-Allow-Origin": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": csp,
    }


_REQUIRED_DIST_FILES = (
    "taskpane.html",
    "functions.js",
    "functions.json",
    "auth-callback.html",
)


def _repo_root() -> Path:
    # precis_mcp/mcp_external/excel_static.py → repo root is three parents up
    # (matches framing.py's `parent.parent.parent` bundle resolution).
    return Path(__file__).resolve().parent.parent.parent


def _dist_dir() -> Path:
    """The built add-in bundle directory. Operator-overridable so a deployment
    can point at a bundle it built itself."""
    override = os.environ.get("EXCEL_ADDIN_DIST_DIR", "").strip()
    return Path(override) if override else _repo_root() / "excel-addin" / "dist"


def _manifest_template() -> Path:
    # The checked-in manifest is the template; only its asset origin (and the
    # OAuth issuer AppDomain) are rewritten per deployment.
    return _repo_root() / "excel-addin" / "manifest.xml"


def excel_addin_bundle_available() -> bool:
    """True when the add-in is enabled AND the built bundle is usable."""
    dist = _dist_dir()
    return excel_addin_enabled() and dist.is_dir() and all(
        (dist / name).is_file() for name in _REQUIRED_DIST_FILES
    )


router = APIRouter()


def _add_root_app_domain(xml: str, origin: str) -> str:
    """Add an AppDomain to the root manifest only.

    ``VersionOverrides`` has its own schema and does not allow ``AppDomains``.
    Keep this as a targeted text transform so we preserve the checked-in Office
    manifest formatting and namespace prefixes.
    """
    if f"<AppDomain>{origin}</AppDomain>" in xml:
        return xml

    vo_at = xml.find("<VersionOverrides")
    root_part = xml if vo_at == -1 else xml[:vo_at]
    tail = "" if vo_at == -1 else xml[vo_at:]

    close_at = root_part.find("</AppDomains>")
    if close_at != -1:
        return (
            root_part[:close_at]
            + f"    <AppDomain>{origin}</AppDomain>\n  "
            + root_part[close_at:]
            + tail
        )

    hosts_at = root_part.find("  <Hosts>")
    if hosts_at == -1:
        return xml
    app_domains = (
        "  <AppDomains>\n"
        f"    <AppDomain>{origin}</AppDomain>\n"
        "  </AppDomains>\n"
    )
    return root_part[:hosts_at] + app_domains + root_part[hosts_at:] + tail


@router.get("/excel/manifest.xml")
async def excel_manifest(request: Request) -> Response:
    """The Office add-in manifest, host-templated for this deployment.

    Rewrites the dev origin to ``{public_base}/excel`` (where the bundle is
    served) and adds the OAuth issuer's origin to ``<AppDomains>`` so the sign-in
    dialog can navigate to the IdP regardless of mode.
    """
    if not excel_addin_bundle_available():
        return PlainTextResponse("Excel add-in not enabled", status_code=404)
    template = _manifest_template()
    if not template.is_file():
        return PlainTextResponse("Excel add-in manifest unavailable", status_code=404)

    base = oidc.public_base(str(request.base_url)).rstrip("/")
    xml = template.read_text(encoding="utf-8").replace(_DEV_ORIGIN, f"{base}{_MOUNT}")

    issuer_origin = _issuer_origin()
    if issuer_origin:
        xml = _add_root_app_domain(xml, issuer_origin)
    return Response(content=xml, media_type="application/xml", headers=_excel_headers())


@router.get("/excel/{path:path}")
async def excel_asset(path: str) -> Response:
    """Serve a file from the built add-in bundle, guarding path traversal — the
    path is client-supplied, so the resolved target must stay within ``dist``."""
    if not excel_addin_bundle_available():
        return PlainTextResponse("Excel add-in not enabled", status_code=404)
    dist = _dist_dir().resolve()
    target = (dist / path).resolve()
    if target != dist and dist not in target.parents:
        return PlainTextResponse("not found", status_code=404)
    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(target, headers=_excel_headers())
