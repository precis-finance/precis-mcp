/*
 * Shared-runtime global state: the bearer token + discovered OIDC config. The
 * shared runtime is a single JS runtime, so this module-level state is shared
 * across the task pane, ribbon, and functions.
 *
 * The /mcp URL is not stored — it's derived from the serving origin (see
 * getMcpUrl). The token is **session-only** and never persisted; it's acquired
 * via the Dialog-OAuth PKCE flow.
 */

import { FinancialTableBlock } from "./functions/block";

interface PrecisGlobal {
  // OIDC sign-in config, discovered at sign-in from the /mcp host's
  // protected-resource metadata (RFC 9728) + the issuer's OpenID configuration.
  // Session-only — never user-supplied, never persisted. IdP-agnostic: the
  // endpoints come from discovery (not a hardcoded Keycloak path), and the
  // client_id is advertised by the /mcp host (`precis-excel-addin` for the
  // bundled Keycloak, or the external IdP's registered public client in mode C).
  oidcConfigForMcpUrl?: string;
  authorizationEndpoint?: string;
  tokenEndpoint?: string;
  clientId?: string;
  // Token-request shape, discovered from the /mcp host's protected-resource
  // metadata so the add-in stays IdP-agnostic: `scopes` is the exact scope set to
  // request (the Précis-namespaced `precis_excel_scopes`, default ["openid"]);
  // `resourceParam` is whether the AS honours the RFC 8707 `resource` parameter (default true —
  // the MCP-conformant shape; false for Entra/Okta, which bind the audience via
  // the request scope instead).
  scopes?: string[];
  resourceParam?: boolean;
  token?: string;
  // Keycloak refresh token (session-only). Used to silently mint a new access
  // token on a 401 so the add-in doesn't fall into "all #VALUE!" on expiry.
  refreshToken?: string;
  // Human label for the signed-in user, derived from the id_token's identity
  // claims (name / preferred_username / email). Session-only; display-only.
  userLabel?: string;
  // Format-intent cache: the custom function stashes the block it spilled,
  // keyed by its anchor address, so the ribbon formatter can apply nf / alerts /
  // row roles positionally without re-fetching. Re-warmed on every recalc.
  blocks?: Record<string, FinancialTableBlock>;
  // Auth-change listener (the task pane's re-render). Kept on the shared global,
  // not in module scope, so a disconnect raised in the functions bundle reaches
  // the task-pane bundle — they're separate webpack entries sharing one runtime.
  authListener?: () => void;
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function store(): PrecisGlobal {
  const g = globalThis as any;
  if (!g.__precis) {
    g.__precis = {};
  }
  return g.__precis as PrecisGlobal;
}
/* eslint-enable @typescript-eslint/no-explicit-any */

// Connection-state observer: lets the runtime (e.g. a 401-triggered disconnect
// inside a custom function) re-render the task pane to its disconnected state.
export function onAuthChange(fn: () => void): void {
  store().authListener = fn;
}
function notifyAuth(): void {
  try {
    store().authListener?.();
  } catch {
    /* a listener error must not break a tool call */
  }
}

/**
 * The /mcp URL, derived from the origin that served the add-in bundle. The
 * self-hosted server serves both the bundle (at /excel) and the API (at /mcp),
 * so /mcp is always `../mcp` relative to this page — no user input needed. The
 * relative resolution also tracks a path-prefixed mount. Everything downstream
 * (OIDC discovery, the RFC 8707 resource audience) keys off this single value.
 */
export function getMcpUrl(): string | undefined {
  try {
    return new URL("../mcp", window.location.href).toString().replace(/\/+$/, "");
  } catch {
    return undefined;
  }
}

export function setOidcConfig(mcpUrl: string, c: {
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
  scopes: string[];
  resourceParam: boolean;
}): void {
  const s = store();
  s.oidcConfigForMcpUrl = mcpUrl;
  s.authorizationEndpoint = c.authorizationEndpoint;
  s.tokenEndpoint = c.tokenEndpoint;
  s.clientId = c.clientId;
  s.scopes = c.scopes;
  s.resourceParam = c.resourceParam;
}

/** Scopes to request (exactly the advertised set; defaults to ["openid"]). */
export function getScopes(): string[] {
  const s = store().scopes;
  return s && s.length ? s : ["openid"];
}

/** Whether to send the RFC 8707 `resource` parameter (default true). */
export function resourceParamSupported(): boolean {
  return store().resourceParam !== false;
}

export function getAuthorizationEndpoint(): string | undefined {
  return store().authorizationEndpoint;
}

export function getTokenEndpoint(): string | undefined {
  return store().tokenEndpoint;
}

export function getClientId(): string | undefined {
  return store().clientId;
}

export function hasOidcConfig(mcpUrl: string): boolean {
  const s = store();
  return !!(
    s.oidcConfigForMcpUrl === mcpUrl &&
    s.authorizationEndpoint &&
    s.tokenEndpoint &&
    s.clientId
  );
}

export function setToken(token: string): void {
  store().token = token.trim() || undefined;
  notifyAuth();
}

export function getToken(): string | undefined {
  return store().token;
}

export function setRefreshToken(token: string | undefined): void {
  store().refreshToken = token?.trim() || undefined;
}

export function getRefreshToken(): string | undefined {
  return store().refreshToken;
}

export function setUserLabel(label: string | undefined): void {
  store().userLabel = label?.trim() || undefined;
}

export function getUserLabel(): string | undefined {
  return store().userLabel;
}

export function isConnected(): boolean {
  return !!store().token;
}

/** Clear the session token + discovered OIDC config + format cache (sign-out). */
export function disconnect(): void {
  const s = store();
  s.token = undefined;
  s.refreshToken = undefined;
  s.userLabel = undefined;
  clearOidcConfig(s);
  s.blocks = {};
  notifyAuth();
}

function clearOidcConfig(s = store()): void {
  s.oidcConfigForMcpUrl = undefined;
  s.authorizationEndpoint = undefined;
  s.tokenEndpoint = undefined;
  s.clientId = undefined;
  s.scopes = undefined;
  s.resourceParam = undefined;
}

export function setBlock(anchor: string, block: FinancialTableBlock): void {
  const s = store();
  if (!s.blocks) {
    s.blocks = {};
  }
  s.blocks[anchor] = block;
}

export function getBlock(anchor: string): FinancialTableBlock | undefined {
  return store().blocks?.[anchor];
}

/** Cached block anchor keys — for diagnostics (cache-miss reporting). */
export function blockKeys(): string[] {
  return Object.keys(store().blocks ?? {});
}
