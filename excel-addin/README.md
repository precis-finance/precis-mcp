# Précis for Excel — Office Add-in

A **read-only** Excel 365 add-in: custom functions (`=PRECIS.STATEMENT(...)`,
`=PRECIS.METRIC(...)`) that spill live Précis financial data into the grid, plus a
ribbon command that applies Précis formatting. The add-in is an **MCP client** of a
Précis instance's `/mcp` endpoint — it hosts no backend of its own. It ships an
add-in-only XML manifest (the unified JSON manifest is preview-only for custom
functions and is not used here).

In a deployment, the Précis server hosts the built bundle at `/excel` and serves a
host-templated manifest at `/excel/manifest.xml`; see the Excel section of the
Précis documentation (<https://docs.precis.finance>) for install and OAuth setup.
This README covers local development.

---

## Dev / test loop — Excel on the web (no Windows Excel needed)

Custom functions **and** the shared runtime are both supported on Excel on the web, so
the entire inner loop runs in a browser against a `localhost` dev server.

**Prerequisites:** Node 18+, a Microsoft 365 work/school (or consumer) account, and an
Excel workbook saved on **OneDrive / SharePoint** (web sideloading injects the manifest
into a cloud document — a blank in-browser sheet won't do).

```bash
npm install
npx office-addin-dev-certs install          # trust the localhost HTTPS cert in your browser
npm run start -- web --document "<OneDrive-or-SharePoint Excel URL>"
```

`npm run start -- web` builds, serves `https://localhost:3000`, and sideloads the
manifest into the document. (Manual fallback: in Excel web, **Home > Add-ins > More
Settings > Upload My Add-in** → `manifest.xml`.)

**Smoke test:** open the task pane (it loads, proving the shared runtime is up) and type
`=PRECIS` in a cell — autocomplete should list the namespace, proving the custom
functions registered. To exercise the full path, sign in and run `=PRECIS.SCENARIOS()`
against a live instance.

Stop + unregister: `npm stop`.

### Gotchas
- **Office caches custom functions** — your changes may not appear on reload. Clear the
  Office cache (`npx office-addin-cache clear`, or on web simply re-sideload). Shared-runtime
  force-refresh: `Office.context.document.settings.set('Office.ForceRefreshCustomFunctionsCache', true)` + `saveAsync()`.
- **`#NAME?`** = the function didn't register (re-add the add-in, clear cache, restart).
- `functions.json` is generated at build time from the `@customfunction` JSDoc tags.

---

## Build

```bash
npm run build      # outputs the bundle to dist/
```

A Précis deployment builds this and serves `dist/` at `/excel`; the manifest's asset
URLs and the OAuth issuer are templated to the deployment's origin by the server.