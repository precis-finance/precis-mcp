/*
 * Minimal MCP client for the Précis /mcp transport.
 *
 * The Précis /mcp endpoint is plain JSON-RPC 2.0 over a single HTTP POST (no
 * SSE, no session-id header — the server derives the session from the bearer),
 * so a custom function can call `tools/call` directly without an `initialize`
 * handshake. We call the render variant `run_statement` / `run_metric` and read
 * its `structuredContent` (the financial_table block).
 */

import { getToken, disconnect } from "./config";
import { tryRefresh } from "./oauth";

let _rpcId = 0;

/* global fetch */

/*
 * Concurrency gate. Excel's calc engine invokes custom functions in parallel, so
 * a workbook refresh fans out into one /mcp fetch per PRECIS cell at once. Bound
 * the in-flight fetches and queue the rest, turning the burst into an orderly
 * stream the instance (and its ClickHouse) absorbs smoothly. The shared runtime
 * means this module state spans every custom function in the workbook.
 */
const MAX_INFLIGHT = 5;
let _inflight = 0;
const _waiters: (() => void)[] = [];

function acquireSlot(): Promise<void> {
  if (_inflight < MAX_INFLIGHT) {
    _inflight++;
    return Promise.resolve();
  }
  return new Promise((resolve) => _waiters.push(resolve));
}

function releaseSlot(): void {
  const next = _waiters.shift();
  if (next) {
    next(); // hand the slot straight to the next waiter — count is unchanged
  } else {
    _inflight--;
  }
}

/**
 * Call an MCP tool and return its `structuredContent`. `T` is the caller's
 * expected shape — the render variant returns a financial_table block, the
 * `_data` variant the raw engine result. Throws a message-bearing Error on
 * transport / auth / tool error (a custom function surfaces it as the cell's
 * error text).
 */
export async function callTool<T = unknown>(
  mcpUrl: string,
  token: string,
  name: string,
  args: Record<string, unknown>
): Promise<T> {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: ++_rpcId,
    method: "tools/call",
    params: { name, arguments: args },
  });
  const post = (bearer: string): Promise<Response> =>
    fetch(mcpUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        Authorization: `Bearer ${bearer}`,
      },
      body,
    });

  await acquireSlot();
  try {
    let res: Response;
    try {
      res = await post(token);
      // Access token expired → refresh once and retry before giving up.
      if (res.status === 401 && (await tryRefresh())) {
        res = await post(getToken() ?? token);
      }
    } catch (e) {
      throw new Error(`Précis: cannot reach ${mcpUrl} (${(e as Error).message})`);
    }

    if (res.status === 401) {
      // Refresh failed / no refresh token — the SSO session is gone. Drop to
      // disconnected so the task pane reflects it and prompts re-sign-in.
      disconnect();
      throw new Error("Précis: session expired — sign in again in the task pane.");
    }
    if (!res.ok) {
      throw new Error(`Précis /mcp HTTP ${res.status}`);
    }

    const json = await res.json();
    if (json.error) {
      throw new Error(`Précis /mcp: ${json.error.message ?? "error"}`);
    }
    const result = json.result;
    if (!result || result.isError) {
      const text = result?.content?.[0]?.text ?? "tool error";
      throw new Error(`Précis: ${text}`);
    }
    if (!result.structuredContent) {
      throw new Error("Précis: no structuredContent in the response.");
    }
    return result.structuredContent as T;
  } finally {
    releaseSlot();
  }
}
