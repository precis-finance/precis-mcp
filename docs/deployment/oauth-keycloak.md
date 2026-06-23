# Remote access — sign-in & identity modes

When more than one person uses your server, you need real sign-in. MCP clients
authenticate to a remote server with **OAuth 2.1 + PKCE** and discover the
sign-in endpoint via [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), so you
need an OIDC authorization server in front of Précis-MCP.

You pick the sign-in posture at install with **one variable, `PRECIS_AUTH_MODE`**:

| Mode | `PRECIS_AUTH_MODE` | Sign-in | When |
|---|---|---|---|
| **A** | `devkey` | One static key, localhost only | Trial / demo / your own machine — see the [Quickstart](../getting-started/quickstart.md). Single-user. |
| **B** | `keycloak` | The **bundled Keycloak** (optionally federated to your IdP) | You want a ready-to-run sign-in provider. The default multi-user path. |
| **C** | `oidc` | **Your own OIDC IdP** (Auth0, Okta, Entra, Ping, …), trusted directly | You already run an IdP and want no extra moving parts. |

The modes differ only in *which issuer signs the token*. The token check, the
per-user permission model, and provisioning are identical across all three.

!!! note "The dev-key server is a separate entrypoint — not part of B/C"
    Mode A is a distinct, localhost-only process (`python -m precis_mcp.server`),
    enabled by `ENABLE_MCP_DEV_SERVER=1` and **off by default**. The multi-user
    modes B/C run a different entrypoint (`precis_mcp.app_open`, the OAuth-gated
    `/mcp` server) and the production bundle doesn't include the dev server.
    `PRECIS_AUTH_MODE` is enforced on **both** entrypoints: if you set
    `PRECIS_AUTH_MODE=keycloak` or `oidc`, the dev server **refuses to start even
    if `ENABLE_MCP_DEV_SERVER=1`** — so a B/C-configured host can't accidentally
    run a no-auth server. (And `app_open` likewise refuses `devkey`.)

## Mode B — bundled Keycloak (`PRECIS_AUTH_MODE=keycloak`)

Précis-MCP ships a ready-to-run Keycloak in the bundled `docker-compose`, with a
committed realm (sign-in client, the `precis_user_id` user mapper, and the
audience mapper for `/mcp`) plus a per-deploy reconcile. You don't configure OIDC
by hand.

| Variable | Purpose | Default |
|---|---|---|
| `PRECIS_AUTH_MODE` | `keycloak` | — |
| `PRECIS_BASE_URL` | Public address of your server; the other URLs derive from it | — |
| `KC_BASE_URL_INTERNAL` | Backend → Keycloak (container topology) | `http://localhost:8080/auth` |
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | Keycloak realm-admin password | — |
| `KC_REALM` / `KC_CLIENT_ID` | Realm + client | `precis` / `precis-spa` |
| `KC_MCP_AUDIENCE` | Expected token audience on `/mcp` | derived from `PRECIS_BASE_URL` |

Bring it up:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

This pulls the pinned published image (`ghcr.io/precis-finance/precis-mcp`,
selected by `PRECIS_MCP_TAG`) and starts your data store, the bundled Keycloak
with its seeded realm, and the server. Pulling a pinned release is the default;
`docker compose … up -d --build` builds from source instead (rolling `main` /
forks). From a workstation, `scripts/deploy-mcp.sh` does the same, pull-first —
see the [production checklist](production-checklist.md#3-deploy). To let your users sign in with your corporate IdP, point Keycloak's
identity-brokering at it (OIDC **or SAML**) — Keycloak stays the issuer and the
above doesn't change; the walkthrough is
[Sign in with your corporate IdP](keycloak-brokering.md). This is also the
path for **claude.ai / ChatGPT**: their connectors self-register (DCR), which
the bundled Keycloak supports.

## Mode C — your own OIDC IdP (`PRECIS_AUTH_MODE=oidc`)

No Keycloak runs. The verifier points straight at your IdP. Register a client in
your IdP and tell Précis-MCP about the issuer:

| Variable | Purpose |
|---|---|
| `PRECIS_AUTH_MODE` | `oidc` |
| `OIDC_ISSUER` | Your IdP's issuer URL (used verbatim — keep any trailing slash) |
| `OIDC_JWKS_URL` | JWKS endpoint, if not derivable from the issuer's discovery |
| `OIDC_AUDIENCE` | The audience your IdP stamps for the `/mcp` resource |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Your pre-registered client (secret only if confidential) |
| `PRECIS_IDENTITY_CLAIM` | Which token claim carries identity (default `precis_user_id`) |
| `PRECIS_IDENTITY_COLUMN` | `id` (the claim value *is* the user id) or `external_id` (look it up) |

Two things to know:

- **Pre-registered client, not DCR.** Most enterprise IdPs disallow dynamic
  client registration, so you register the client yourself and set
  `OIDC_CLIENT_ID`. Modern MCP clients (Claude Code, ChatGPT, a first-party
  frontend) then sign in via discovery. **claude.ai/ChatGPT's public connectors
  need DCR** — if your IdP doesn't offer it, use mode B (Keycloak brokering).
- **Stable identity claim.** Map `PRECIS_IDENTITY_CLAIM` to a *stable, unique*
  claim (e.g. an immutable subject / `oid`), not a mutable email. If it differs
  from your Précis-MCP user ids, set `PRECIS_IDENTITY_COLUMN=external_id` and
  store the IdP value with `create-user --external-id` (below).

## Create the first admin and provision users

Being signed in by the IdP grants nothing — each user must also exist in
Précis-MCP with a profile. The first admin is seeded with the **admin CLI** (the
UI can't bootstrap itself). Run it inside the server container —
`docker compose -f deploy/docker-compose.yml exec precis-mcp python -m …` —
which already has the platform-DB connection configured:

```bash
python -m precis_mcp.admin_cli create-admin --id alice
# mode C (external IdP — no Keycloak account, the IdP owns the credential):
python -m precis_mcp.admin_cli create-admin --id alice --no-keycloak --external-id <idp-subject>
```

Then provision and grant access:

```bash
python -m precis_mcp.admin_cli create-user --id bob
python -m precis_mcp.admin_cli profile create --file analyst.yml
python -m precis_mcp.admin_cli assign --user bob --profile analyst
```

A user with no profile authenticates but can read nothing — assign a profile to
grant access. What a profile contains — scenario patterns, roles, domain and
dimension scopes, with worked examples — is documented in
[User profiles & permissions](../configuration/user-profiles.md).

## Verify

Check the issuer/JWKS/audience config before go-live:

```bash
python -m precis_mcp.admin_cli check-auth          # static + reachability
python -m precis_mcp.admin_cli check-auth --no-fetch   # static only
```

You can also have the server fail fast at startup on a misconfig by setting
`PRECIS_AUTH_PREFLIGHT=1`. Then confirm end-to-end: a client completes sign-in,
a query succeeds, and a token with the wrong `/mcp` audience is rejected.

## Connect a client

A modern MCP client discovers the sign-in endpoint from your server and runs the
OAuth flow itself — you give it the server URL (and, for mode C, the
pre-registered `client_id` if the client needs it configured):

```jsonc
{
  "mcpServers": {
    "precis": { "url": "https://<your-host>/mcp" }
  }
}
```

## Related

- [Quickstart (local, single-user)](../getting-started/quickstart.md)
- [Catalogue & semantic model](../configuration/catalogue-and-semantic.md)
