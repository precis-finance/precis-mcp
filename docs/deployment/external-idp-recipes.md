# External IdP integration recipes (mode C / federated mode B)

> Per-IdP configuration recipes for connecting Précis-MCP to **Auth0, Okta,
> Microsoft Entra ID, and Ping** as the OIDC issuer, plus the DCR support matrix
> that decides which identity mode fits which IdP. Companion to
> [Remote access — sign-in & identity modes](oauth-keycloak.md).
>
> Mode C (direct external OIDC trust) is fully supported on the Précis-MCP
> side — issuer/JWKS/audience override, claim→column mapping, pre-registered
> client, and the boot-time conformance check (`PRECIS_AUTH_PREFLIGHT`). These
> recipes describe what an operator must configure **on the IdP side** to
> satisfy the token contract.
>
> **Verification caveat:** compiled from official vendor documentation (cited
> inline). IdP consoles change, and some behaviours rest on concept/guide pages
> or inference rather than a normative reference — where that's the case it is
> flagged. Validate the exact field labels and the RFC 8707 behaviours against
> a live tenant before committing an architecture.

---

## 1. How to choose (read first)

**Your IdP's anonymous-DCR posture decides whether mode C is viable for the
public connectors (claude.ai / ChatGPT), or whether you need mode B
(Keycloak brokering).** Only **Auth0** (after enabling it) and **PingFederate**
support anonymous RFC 7591 Dynamic Client Registration. **Okta, Entra ID, and
PingOne cloud SaaS do not.** Since claude.ai/ChatGPT self-register via DCR, an
Okta/Entra/PingOne estate **cannot point those connectors at mode C** — the
path is **mode B**, where the bundled Keycloak supplies the DCR surface and the
RFC 8707 audience stamping while federating authentication upstream
([brokering walkthrough](keycloak-brokering.md)).

Entra and Okta are the two most common enterprise IdPs, so in practice **most
deployments land on mode B (brokered), not mode C.** Mode C is the clean path
for Auth0 and PingFederate, and for *first-party frontends / pre-registered
clients* on any IdP (which don't need DCR).

A second gap compounds it: **Okta and Entra do not honour RFC 8707 `resource`
indicators** (Entra rejects `resource` with `AADSTS901002`; Okta's `aud` is a
static per-authorization-server setting). So even the *no-DCR* mode-C path (SPA /
pre-registered client) against Okta/Entra needs either a proxy that rewrites
`resource`→`scope`, a relaxed audience check, or — again — mode B, where Keycloak
stamps the `aud`.

## 2. Cross-IdP summary matrix

| Dimension | Auth0 | Okta | Entra ID | PingFederate | PingOne (cloud) |
|---|---|---|---|---|---|
| **Custom access-token claim** | Post-login Action `setCustomClaim`; **must be namespaced** (`https://precis/…`) on API-audience tokens | **Custom Authorization Server** only (org server can't); EL expression; **API Access Mgmt paid add-on** | Optional claims / directory-extension / claims-mapping policy; shape the **resource** app's token, not the client's | ATM attribute contract + contract fulfillment + OGNL (most flexible) | Resource custom attributes + scopes; can't touch built-in resources |
| **Stable join key** | any namespaced claim value (use a stored internal id) | EL over a UD attribute (`user.login` / custom) | **`oid` (+`tid`)** — never email/upn (mutable); `sub` is pairwise | datastore/adapter attribute via OGNL | resource attribute → user id |
| **`aud` source** | registered **API Identifier** | custom AS **Audience** field (static) | **API client-ID GUID** (v2) / App ID URI (v1) | **Resource URIs** on the ATM | resource identifier |
| **RFC 8707 `resource`** | only with **Resource Parameter Compatibility Profile** on | **No** (static per-AS aud) | **No** (`AADSTS901002`; scope/`.default` model) | **Yes** (native, pre-register the URI) | **Not documented** |
| **Anonymous DCR (RFC 7591)** | **Yes**, but off by default + operational prereqs | **No** (needs SSWS/`okta.clients.manage`) | **No** (Graph/portal only) | **Yes** (`/as/clients.oauth2`, optional IAT + policies) | **No** (Worker-token API) |
| **Discovery / JWKS / RS256** | Yes | Yes | Yes (issuer quirk, below) | Yes (use a **JWT** ATM, not reference) | Yes |
| **Keycloak can broker to it (mode B)** | Yes (OIDC) | Yes (OIDC) | Yes (built-in `MicrosoftIdentityProvider`, OIDC/SAML) | Yes (OIDC/SAML) | Yes (OIDC) |
| **Recommended Précis-MCP posture** | **Mode C** viable (DCR + 8707 both configurable) | **Mode B** for public connectors; mode C only for pre-registered clients + proxy/relaxed aud | **Mode B** for public connectors; mode C needs scope-rewrite + `oid` join | **Mode C** viable (DCR + 8707 both native) | **Mode B** (no DCR, no 8707) |

## 3. Per-IdP recipes

Each recipe has three parts: what to configure in the IdP, the matching
`deploy/.env` block on the Précis-MCP side (mode C — set
`PRECIS_AUTH_MODE=oidc` and drop `bundled-keycloak` from `COMPOSE_PROFILES`),
and verification. Verification is the same everywhere:

```bash
python -m precis_mcp.admin_cli check-auth     # issuer / JWKS / audience reachability
```

then set `PRECIS_AUTH_PREFLIGHT=1` so the server fails fast at boot on a
misconfiguration, and confirm end to end: a client signs in, a query succeeds,
and a token with the wrong `/mcp` audience is rejected.

### 3.1 Auth0 — mode C viable

Anonymous DCR and RFC 8707 are both available, so Auth0 carries the full
mode-C surface, public connectors included.

**Configure in Auth0:**

1. **Register the API** (Applications → APIs). Its **Identifier** — an
   absolute URI that is never dereferenced — becomes the token `aud`. Using
   your `/mcp` URL keeps it self-describing.
2. **Emit the identity claim** with a post-login **Action**:
   `api.accessToken.setCustomClaim('https://precis/precis_user_id', event.user.app_metadata.precis_user_id)`.
   **Namespacing is mandatory** on API-audience tokens — a non-namespaced
   claim is silently dropped; the namespace URL is opaque. Store each user's
   Précis-MCP id in `app_metadata` at provisioning. You cannot overwrite
   `sub`/`aud` — emit a dedicated claim and have the verifier read it.
3. **Enable RFC 8707** via the **Resource Parameter Compatibility Profile**
   so Auth0 honours `resource=<identifier>`; otherwise clients must send the
   legacy `audience=` parameter (if both are present, `audience` wins). One
   token targets one API audience.
4. **For the public connectors, enable anonymous DCR**
   (`enable_dynamic_client_registration` in tenant settings — off by
   default; the endpoint is `/oidc/register`). DCR clients are third-party
   (`tpc_`), receive only `authorization_code` + `refresh` + PKCE, and
   **need a domain-level (promoted) connection plus a pre-set API grant**
   before they can obtain tokens. Registration has no per-client gating —
   monitor and rate-limit the endpoint. (Auth0 is steering future MCP
   registration toward CIMD.)
5. **Pre-register a client** for any first-party frontend or static client;
   note its client id (and secret, if confidential).

**Précis-MCP side:**

```bash
PRECIS_AUTH_MODE=oidc
OIDC_ISSUER=https://<tenant>.auth0.com/      # keep the trailing slash — used verbatim
OIDC_JWKS_URL=https://<tenant>.auth0.com/.well-known/jwks.json
OIDC_AUDIENCE=<api-identifier>               # the API Identifier from step 1
OIDC_CLIENT_ID=<pre-registered-client-id>    # for static clients; DCR clients bring their own
PRECIS_IDENTITY_CLAIM=https://precis/precis_user_id
# PRECIS_IDENTITY_COLUMN=id (default): the claim carries the platform user id itself
```

Discovery: `https://{tenant}.auth0.com/.well-known/openid-configuration`;
JWKS at `/.well-known/jwks.json`; RS256 default.

Sources: [create-custom-claims](https://auth0.com/docs/secure/tokens/json-web-tokens/create-custom-claims), [resource-param-compatibility-profile](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile), [dynamic-client-registration](https://auth0.com/docs/get-started/applications/dynamic-client-registration).

### 3.2 Okta — mode B for public connectors

No anonymous DCR and no RFC 8707: **claude.ai/ChatGPT cannot connect to an
Okta-backed mode C** — route those users through the
[bundled Keycloak brokered to Okta](keycloak-brokering.md). The recipe below
is the mode-C path for **pre-registered clients** (a first-party frontend,
Claude Code with a configured client id).

**Configure in Okta:**

1. **Create (or pick) a Custom Authorization Server** (Security → API →
   Authorization Servers) — the org/default server **cannot emit custom
   claims on access tokens**. **Licensing:** custom authorization servers
   require the **API Access Management** add-on — paid in production,
   pre-provisioned in free/trial orgs, which hides the cost during
   evaluation.
2. **Set the audience**: the server's **Audience** field is the static `aud`
   of every token it issues. There is **no RFC 8707** — the audience is
   per-server, selected by which `/oauth2/<id>/v1/…` endpoint the client
   uses, not by a `resource` parameter. One custom AS per resource audience
   is the workaround.
3. **Add the identity claim** (custom AS → Claims → Add): name
   `precis_user_id`, token type **Access Token**, value type Expression —
   e.g. `user.login` or a custom UD attribute.
4. **Pre-register the client** (Applications). DCR
   (`/oauth2/v1/clients`) requires an org credential (SSWS token or
   `okta.clients.manage`) — public connectors cannot self-register.

**Précis-MCP side:**

```bash
PRECIS_AUTH_MODE=oidc
OIDC_ISSUER=https://<org>.okta.com/oauth2/<authServerId>
OIDC_JWKS_URL=https://<org>.okta.com/oauth2/<authServerId>/v1/keys
OIDC_AUDIENCE=<custom-AS-audience>           # the static Audience from step 2
OIDC_CLIENT_ID=<pre-registered-client-id>
PRECIS_IDENTITY_CLAIM=precis_user_id         # the claim name from step 3
PRECIS_IDENTITY_COLUMN=external_id           # store user.login (or your UD attribute) per user
```

Discovery/JWKS/RS256: yes, per authorization server. *(The RFC 8707 "no" is
inferred from Okta's static-audience model — no Okta doc claims `resource`
support.)*

Sources: [customize-tokens](https://developer.okta.com/docs/guides/customize-tokens-returned-from-okta/main/), [customize-authz-server](https://developer.okta.com/docs/guides/customize-authz-server/main/), [API Access Management](https://developer.okta.com/docs/concepts/api-access-management/), [DCR API](https://developer.okta.com/docs/reference/api/oauth-clients/).

### 3.3 Microsoft Entra ID — mode B for public connectors

No DCR at all (app registration is portal/Graph only) and no RFC 8707
(`resource` yields `AADSTS901002`): **public connectors cannot connect to an
Entra-backed mode C** — route them through the
[bundled Keycloak brokered to Entra](keycloak-brokering.md). The recipe below
is the mode-C path for pre-registered clients.

**Configure in Entra:**

1. **Register the API app** (the resource): **Expose an API** → set the
   **Application ID URI** (`api://<client-id>`) and add a scope. Set
   `requestedAccessTokenVersion: 2` in the app manifest so v2 tokens are
   issued; the `aud` of a v2 token is the API app's **client-ID GUID**
   (v1 uses the App ID URI instead).
2. **Register the client app** (the caller) with the redirect URIs your
   client needs; grant it the API scope from step 1. Clients request
   `scope=api://<id>/<scope>` (or `/.default`) — never `resource=`.
3. **Use `oid` (+ `tid`) as the join key — never email/UPN.** `oid` is the
   immutable per-tenant user GUID; `email`/`upn`/`preferred_username` are
   mutable and reassignable; `sub` is *pairwise* (per-app, useless as a
   cross-system key). Verify `tid` equals your tenant — the same `oid` in
   two tenants is two different users. `oid` is present in v2 access tokens
   by default, so no claims customisation is needed for this path.
4. *(Only if you need a custom claim instead of `oid`)* — access tokens are
   shaped by the **resource app registration**, not the client ("to validate
   access-token changes, request a token for your application, not Graph").
   Add a **directory-extension claim** (`extn.precisUserId`) or a
   **claims-mapping policy** (`acceptMappedClaims: true`, **single-tenant
   only** — never set it on a multitenant app).

**Précis-MCP side:**

```bash
PRECIS_AUTH_MODE=oidc
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_JWKS_URL=https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
OIDC_AUDIENCE=<api-app-client-id-guid>       # v2 tokens
OIDC_CLIENT_ID=<client-app-id>
PRECIS_IDENTITY_CLAIM=oid
PRECIS_IDENTITY_COLUMN=external_id           # store each user's oid GUID
```

Discovery quirk: pin the verifier to the **tenant-specific** metadata
(`https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`;
JWKS at `/discovery/v2.0/keys`) so `iss` is concrete — the `common` endpoint
returns the literal `{tenantid}` template.

Sources: [optional-claims](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims), [access-tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens), [expose-web-apis](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-configure-app-expose-web-apis), [claims-customization](https://learn.microsoft.com/en-us/entra/identity-platform/reference-claims-customization).

### 3.4 Ping — mode C viable on **PingFederate** only

**Caveat:** "Ping" spans three products with different OAuth stacks —
**PingOne** (cloud SaaS), **PingFederate** (on-prem), and **PingOne Advanced
Identity Cloud / PingAM** (ex-ForgeRock). The `/oauth2/register` "Allow Open
DCR" docs that surface under "PingOne" are usually **AIC/PingAM**, not PingOne
cloud SaaS. Ping docs are JS-rendered and harder to deep-link — validate
against a live tenant.

**PingFederate (mode C viable — DCR and RFC 8707 both native):**

1. **Create an Access Token Management (ATM) instance** of type **JWT**
   (not reference/opaque — the token must be JWKS-verifiable). Declare
   `precis_user_id` in the **attribute contract** and bind it in **Contract
   Fulfillment** from an IdP adapter / datastore, with **OGNL** transforms
   where needed (the most flexible claim pipeline of the four vendors).
2. **Pre-register your `/mcp` URI** in the ATM's **Resource URIs** — RFC
   8707 is native: the `resource` request parameter selects the ATM instance
   by matching those URIs (an unregistered URI errors).
3. **DCR for public connectors**: anonymous registration at
   `/as/clients.oauth2`, configurable with or without an initial access
   token and constrained by **Client Registration Policies**.

**PingOne cloud (mode B):** custom claims via **Resource → custom
Attributes** (built-in `openid`/`PingOne API` resources can't be modified),
`aud` from the resource identifier, **no anonymous DCR** (Worker-token
Application API only), **RFC 8707 not documented** — route public connectors
through [brokering](keycloak-brokering.md).

**Précis-MCP side (PingFederate):**

```bash
PRECIS_AUTH_MODE=oidc
OIDC_ISSUER=https://<pingfederate-host>
OIDC_JWKS_URL=https://<pingfederate-host>/pf/JWKS
OIDC_AUDIENCE=<resource-uri-from-the-ATM>
OIDC_CLIENT_ID=<pre-registered-client-id>    # for static clients; DCR clients bring their own
PRECIS_IDENTITY_CLAIM=precis_user_id
PRECIS_IDENTITY_COLUMN=external_id           # or id, if the fulfilled value is the platform id
```

Sources: [PF access-token mapping](https://docs.pingidentity.com/pingfederate/12.3/administrators_reference_guide/pf_configure_access_token_mapping.html), [PF dynamic registration](https://docs.pingidentity.com/pingfederate/13.0/developers_reference_guide/pf_dynamic_registra_endpoint.html), [PingOne customize access token](https://docs.pingidentity.com/pingone/applications/p1_customize_access_token.html).

## 3.5 The pre-registered-client path (mode C, no DCR)

When the IdP has no anonymous DCR (Okta / Entra / PingOne) or the client is a
first-party frontend, the OAuth client is **registered manually** in the IdP and
its id configured into Précis-MCP + the client. This is the supported
alternative to DCR:

1. **Register a client in the IdP** — confidential (with a secret) or
   public+PKCE. Note its `client_id` (and secret if confidential). Set the
   redirect URI(s) for the SPA browser flow.
2. **Tell Précis-MCP the pre-registered client** — set `OIDC_CLIENT_ID` (and
   `OIDC_CLIENT_SECRET` for a confidential client) on the deployment. These
   override the bundled-Keycloak default (`precis-spa`).
3. **Configure the MCP client / first-party frontend** with that same
   `client_id`.
4. **Discovery already points the client at the IdP** — the RFC 9728
   protected-resource metadata advertises `authorization_servers = [issuer]`,
   so a modern client reaches the IdP's authorize/token endpoints
   directly. No registration call is made.

**The Keycloak `oauth_proxy` shim is disabled in mode C** (it builds
Keycloak-shaped endpoints and exists only for claude.ai against bundled
Keycloak). Modern clients (Claude Code, ChatGPT, a first-party frontend) use the
RFC 9728 discovery path above and never touch it.

**claude.ai / ChatGPT remain DCR-only** — they cannot use a pre-registered
client_id, so against a no-DCR IdP they are unsupported in mode C; use
**mode B** instead ([Keycloak brokering](keycloak-brokering.md) supplies
DCR).

### 3.5.1 Excel add-in against a no-DCR IdP (Entra / Okta direct)

The Excel add-in is a **pre-registered public client**, so the DCR gap that
blocks claude.ai/ChatGPT does not apply to it — it can connect to a no-DCR IdP
in mode C directly. It also handles the second gap (Okta/Entra not honouring the
RFC 8707 `resource` parameter) by reading its token-request shape from the
`/mcp` host's protected-resource metadata, so you configure the shape on the
server and the add-in follows it.

1. **Register a public client** (Authorization Code + PKCE, no secret) in your
   IdP for the add-in, separate from the SPA's client. Set its redirect URI to
   the server-hosted add-in callback
   (`https://<your-instance>/excel/auth-callback.html`) and allow the hosted
   Précis origin (`https://<your-instance>`) wherever the IdP requires a web
   origin for browser PKCE token exchange. On **Entra**, register the callback
   under the **Single-page application** platform, not **Web** — the add-in
   redeems the code with a browser `fetch`, and Entra only enables cross-origin
   token redemption for SPA-typed redirect URIs (a Web-typed URI clears the
   `AADSTS50011` redirect-mismatch error but then fails with `AADSTS9002326`).
2. **Point the `/mcp` host at it** — set `EXCEL_ADDIN_CLIENT_ID` to that
   client_id.
3. **Advertise the token shape** for an IdP that binds the audience via a scope
   rather than `resource` (Entra, Okta):

   ```bash
   EXCEL_ADDIN_CLIENT_ID=<the public client_id>
   EXCEL_ADDIN_SCOPE="openid profile email offline_access api://<api-app-id>/access_as_user"
   EXCEL_ADDIN_RESOURCE_INDICATOR=false
   ```

   The add-in then requests exactly that scope and omits `resource`. For an IdP
   that does honour `resource` (Auth0 with the compatibility profile, Ping),
   leave both knobs unset — the add-in keeps the default RFC 8707 shape.
4. **The `/mcp` audience check is unchanged.** The token's `aud` must still
   match `OIDC_AUDIENCE`; with Entra that is the API app's client-ID GUID, set
   by the `api://…/access_as_user` scope above.

Include `offline_access` in the scope so the add-in receives a refresh token —
Entra issues one only when that scope is explicitly requested — and `profile
email` so the id_token carries the user's name/email and the task pane shows the
signed-in user rather than the opaque subject id. The add-in keeps the token in
session memory only; it is never written to the workbook or disk.

## 4. Choosing the identity claim

Map `PRECIS_IDENTITY_CLAIM` / `PRECIS_IDENTITY_COLUMN` to the IdP's *stable*
identifier, which differs per IdP: Entra **`oid`** (and assert `tid`), Okta a
UD attribute emitted via a custom-AS claim, Auth0 a stored `app_metadata` id
emitted as a namespaced claim, Ping a datastore/adapter attribute. Email is
the wrong choice everywhere — it is mutable and reassignable. When the claim
value differs from your Précis-MCP user ids, set
`PRECIS_IDENTITY_COLUMN=external_id` and store the IdP value at provisioning
with `create-user --external-id` (see
[Remote access](oauth-keycloak.md)).

## 5. Related documents

- [Remote access — sign-in & identity modes](oauth-keycloak.md) — the three
  identity modes, the token contract, and the bundled-Keycloak setup these
  recipes plug into.
- [Sign in with your corporate IdP (mode B, brokered)](keycloak-brokering.md)
  — the brokered-path walkthrough recommended above for Okta / Entra /
  PingOne deployments.
- [Environment variable reference](../configuration/environment-variables.md)
  — the `OIDC_*` / `PRECIS_IDENTITY_*` configuration surface (mode C).
