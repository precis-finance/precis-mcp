/*
 * OAuth redirect target. Keycloak redirects here (same origin as the add-in)
 * with ?code & ?state (or ?error); we hand them back to the task pane via
 * messageParent and the dialog closes. See src/oauth.ts.
 */

/* global Office, window */

Office.onReady(() => {
  const p = new URLSearchParams(window.location.search);
  const error = p.get("error");
  const payload = error
    ? { error: p.get("error_description") || error }
    : { code: p.get("code"), state: p.get("state") };
  Office.context.ui.messageParent(JSON.stringify(payload));
});
