/*
 * Précis task pane. Two states:
 *   - disconnected → Sign in. Both the /mcp URL (derived from the serving origin)
 *     and the OIDC config (endpoints + client_id, auto-discovered from the /mcp
 *     host) are server-supplied — no URL field, no issuer/client field, no manual
 *     token. Sign-in is the only auth path.
 *   - connected → status + Refresh + Disconnect; formatting lives on the ribbon.
 * The token + discovered config are session-only. On load it also force-refreshes
 * the custom-functions metadata cache so new PRECIS.* functions register without
 * a remove + re-upload.
 */

/* global console, document, Excel, Office */

import { getMcpUrl, getUserLabel, isConnected, disconnect, onAuthChange } from "../config";
import { signIn } from "../oauth";

Office.onReady(() => {
  el("sideload-msg").style.display = "none";
  el("app-body").style.display = "block";

  // Shared-runtime CF metadata-cache force-refresh (dev convenience).
  try {
    Office.context.document.settings.set("Office.ForceRefreshCustomFunctionsCache", true);
    Office.context.document.settings.saveAsync();
  } catch (e) {
    console.error("CF cache force-refresh failed", e);
  }

  el("signin").onclick = onSignIn;
  el("disconnect").onclick = onDisconnect;
  el("refresh").onclick = onRefresh;

  // A 401-forced disconnect inside a custom function flips the pane back to
  // the disconnected state (instead of staying falsely "Connected").
  onAuthChange(() => {
    const connected = isConnected();
    render(connected);
    if (!connected) {
      setStatus("Session expired — sign in again.");
    }
  });

  render(isConnected());
});

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`missing element #${id}`);
  }
  return node;
}

function setStatus(msg: string): void {
  el("status").textContent = msg;
}

/** Toggle the connected / disconnected sections. */
function render(connected: boolean): void {
  el("disconnected").style.display = connected ? "none" : "block";
  el("connected").style.display = connected ? "block" : "none";
  if (connected) {
    const user = getUserLabel();
    el("connected-user").textContent = user ? `Signed in as ${user}` : "";
    el("connected-url").textContent = getMcpUrl() ?? "";
  }
}

async function onSignIn(): Promise<void> {
  const ok = await signIn(setStatus);
  if (ok) {
    render(true);
    setStatus('Signed in. Try =PRECIS.STATEMENT("pnl";"2026-01";"2026-05")');
  }
}

function onDisconnect(): void {
  disconnect();
  render(false);
  setStatus("Disconnected.");
}

async function onRefresh(): Promise<void> {
  setStatus("Refreshing Précis cells…");
  try {
    await Excel.run(async (context) => {
      // Full rebuild re-invokes the non-volatile PRECIS.* functions so they
      // re-fetch from /mcp (the Office.js equivalent of Ctrl+Alt+Shift+F9).
      context.workbook.application.calculate(Excel.CalculationType.fullRebuild);
      await context.sync();
    });
    setStatus("Refreshed — Précis cells re-fetched.");
  } catch (e) {
    setStatus(`Refresh failed: ${(e as Error).message}`);
  }
}