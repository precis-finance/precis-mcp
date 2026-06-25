/*
 * Authorization Code + PKCE sign-in via the Office Dialog API. The add-in is a
 * public client (no secret); the PKCE exchange runs in the browser from the
 * server-hosted add-in origin (`/excel` on the Précis instance).
 *
 * IdP-agnostic discovery — nothing is user-supplied; the /mcp URL is derived
 * from the serving origin, and everything else is discovered from it:
 *   1. {origin}/.well-known/oauth-protected-resource (RFC 9728) → the issuer and
 *      the add-in's client_id (advertised by the /mcp host).
 *   2. {issuer}/.well-known/openid-configuration → the authorize + token
 *      endpoints (no hardcoded Keycloak path).
 * So the same flow works against the bundled Keycloak (mode B) and a customer's
 * external OIDC IdP (mode C).
 */

/* global Office, crypto, window, fetch, atob, btoa, TextEncoder, TextDecoder, URLSearchParams, URL */

import {
  getMcpUrl,
  getRefreshToken,
  setToken,
  setRefreshToken,
  setUserLabel,
  setOidcConfig,
  getAuthorizationEndpoint,
  getTokenEndpoint,
  getClientId,
  getScopes,
  resourceParamSupported,
  hasOidcConfig,
} from "./config";

function base64url(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(): string {
  const a = new Uint8Array(32);
  crypto.getRandomValues(a);
  return base64url(a);
}

async function s256(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function redirectUri(): string {
  return new URL("auth-callback.html", window.location.href).toString();
}

/**
 * Best-effort display label for the signed-in user, decoded from the id_token's
 * identity claims (the id_token is issued to this client, so reading it here is
 * its intended use — unlike the access token, which is opaque to us). Falls back
 * down name → preferred_username → email → sub; returns undefined if there's no
 * id_token or it can't be parsed (display-only, never gates anything).
 */
function userLabelFromIdToken(idToken: string | undefined): string | undefined {
  if (!idToken) {
    return undefined;
  }
  try {
    const part = idToken.split(".")[1];
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
    const claims = JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
    const pick = (k: string): string | undefined => {
      const v = claims[k];
      return typeof v === "string" && v.trim() ? v.trim() : undefined;
    };
    return pick("name") ?? pick("preferred_username") ?? pick("email") ?? pick("sub");
  } catch {
    return undefined;
  }
}

/**
 * Discover the OIDC sign-in config from the configured /mcp URL — the issuer and
 * client_id from the protected-resource metadata, then the authorize/token
 * endpoints from the issuer's OpenID configuration. Caches the result. Throws a
 * message-bearing Error if the instance hasn't enabled the add-in (no client_id)
 * or discovery fails.
 */
export async function discoverConfig(mcpUrl: string): Promise<void> {
  const origin = new URL(mcpUrl).origin;
  const prRes = await fetch(`${origin}/.well-known/oauth-protected-resource`, {
    headers: { Accept: "application/json" },
  });
  if (!prRes.ok) {
    throw new Error(`protected-resource metadata HTTP ${prRes.status}`);
  }
  const pr = await prRes.json().catch(() => ({}));
  const issuer = (pr.authorization_servers ?? [])[0];
  if (!issuer) {
    throw new Error("no authorization_servers in /mcp metadata");
  }
  const clientId = pr.precis_excel_client_id;
  if (!clientId) {
    throw new Error("this instance hasn't enabled the Excel add-in");
  }
  const issuerBase = String(issuer).replace(/\/+$/, "");
  const oidcRes = await fetch(`${issuerBase}/.well-known/openid-configuration`, {
    headers: { Accept: "application/json" },
  });
  if (!oidcRes.ok) {
    throw new Error(`OpenID configuration HTTP ${oidcRes.status}`);
  }
  const oidc = await oidcRes.json().catch(() => ({}));
  if (!oidc.authorization_endpoint || !oidc.token_endpoint) {
    throw new Error("OpenID configuration is missing endpoints");
  }
  // Token-request shape, server-advertised so the add-in stays IdP-agnostic:
  //   scopes_supported (RFC 9728)         → the exact scope set to request
  //   precis_resource_parameter_supported → whether to send RFC 8707 `resource`
  // Defaults preserve the MCP-conformant Keycloak shape (scope=openid + resource).
  // We request exactly the advertised scopes — no client-side composition — so the
  // operator controls the scope per IdP (and we never silently add `offline_access`,
  // which on Keycloak would upgrade the refresh token to a long-lived offline token).
  const scopesSupported = Array.isArray(pr.scopes_supported)
    ? (pr.scopes_supported as unknown[]).map(String).filter(Boolean)
    : [];
  setOidcConfig(mcpUrl, {
    authorizationEndpoint: String(oidc.authorization_endpoint),
    tokenEndpoint: String(oidc.token_endpoint),
    clientId: String(clientId),
    scopes: scopesSupported.length ? scopesSupported : ["openid"],
    resourceParam: pr.precis_resource_parameter_supported !== false,
  });
}

/**
 * Run the PKCE Authorization Code flow via the Office Dialog API. Discovers the
 * OIDC config from the configured /mcp URL first. The access token is cached in
 * the shared global on success; `onStatus` reports progress. Resolves true once a
 * token is acquired so the caller can flip to the connected state.
 */
export async function signIn(onStatus: (msg: string) => void): Promise<boolean> {
  const mcpUrl = getMcpUrl();
  if (!mcpUrl) {
    onStatus("Couldn't determine the Précis instance URL.");
    return false;
  }

  if (!hasOidcConfig(mcpUrl)) {
    onStatus("Discovering sign-in…");
    try {
      await discoverConfig(mcpUrl);
    } catch (e) {
      onStatus(`Couldn't start sign-in from that /mcp URL: ${(e as Error).message}`);
      return false;
    }
  }

  const authorizationEndpoint = getAuthorizationEndpoint() as string;
  const clientId = getClientId() as string;
  const verifier = randomString();
  const state = randomString();
  const challenge = await s256(verifier);

  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri(),
    scope: getScopes().join(" "),
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
  });
  if (resourceParamSupported()) {
    params.set("resource", mcpUrl); // RFC 8707 audience (omitted for ASes that reject it, e.g. Entra)
  }
  const authorizeUrl = `${authorizationEndpoint}?${params.toString()}`;

  onStatus("Opening sign-in…");
  return new Promise<boolean>((resolve) => {
    Office.context.ui.displayDialogAsync(
      authorizeUrl,
      { height: 60, width: 30, promptBeforeOpen: false },
      (result) => {
        if (result.status !== Office.AsyncResultStatus.Succeeded) {
          onStatus(`Could not open sign-in: ${result.error?.message ?? ""}`);
          resolve(false);
          return;
        }
        const dialog = result.value;
        dialog.addEventHandler(Office.EventType.DialogMessageReceived, async (arg) => {
          dialog.close();
          try {
            const msg = JSON.parse((arg as { message: string }).message);
            if (msg.error) {
              onStatus(`Sign-in error: ${msg.error}`);
              resolve(false);
              return;
            }
            if (msg.state !== state) {
              onStatus("Sign-in failed: state mismatch.");
              resolve(false);
              return;
            }
            onStatus("Exchanging token…");
            setToken(await exchangeCode(msg.code, verifier, mcpUrl, clientId));
            resolve(true);
          } catch (e) {
            onStatus(`Sign-in failed: ${(e as Error).message}`);
            resolve(false);
          }
        });
        dialog.addEventHandler(Office.EventType.DialogEventReceived, () => {
          onStatus("Sign-in window closed.");
          resolve(false);
        });
      }
    );
  });
}

async function exchangeCode(
  code: string,
  verifier: string,
  mcpUrl: string,
  clientId: string
): Promise<string> {
  const tokenEndpoint = getTokenEndpoint();
  if (!tokenEndpoint) {
    throw new Error("no token endpoint discovered");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(),
    client_id: clientId,
    code_verifier: verifier,
  });
  if (resourceParamSupported()) {
    body.set("resource", mcpUrl);
  }
  const res = await fetch(tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.access_token) {
    throw new Error(json.error_description || json.error || `token HTTP ${res.status}`);
  }
  setRefreshToken(json.refresh_token);
  setUserLabel(userLabelFromIdToken(json.id_token));
  return json.access_token as string;
}

/**
 * Silently mint a fresh access token from the stored refresh token (called on a
 * 401). Returns false if there's no usable refresh token or the IdP rejects it
 * (the session has ended) — the caller then treats the add-in as disconnected.
 */
export async function tryRefresh(): Promise<boolean> {
  const tokenEndpoint = getTokenEndpoint();
  const clientId = getClientId();
  const refresh = getRefreshToken();
  if (!tokenEndpoint || !clientId || !refresh) {
    return false;
  }
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refresh,
    client_id: clientId,
    // Re-send the scope so a scope-bound IdP (Entra) mints the refreshed access
    // token for the same API; harmless for the resource-indicator path.
    scope: getScopes().join(" "),
  });
  const mcpUrl = getMcpUrl();
  if (resourceParamSupported() && mcpUrl) {
    body.set("resource", mcpUrl);
  }
  try {
    const res = await fetch(tokenEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok || !json.access_token) {
      return false;
    }
    setToken(json.access_token);
    setRefreshToken(json.refresh_token);
    if (json.id_token) {
      setUserLabel(userLabelFromIdToken(json.id_token));
    }
    return true;
  } catch {
    return false;
  }
}
