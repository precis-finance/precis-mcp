/* global CustomFunctions */

import {
  blockToGrid,
  flattenToGrid,
  objectsToGrid,
  FinancialTableBlock,
  MetricDataResult,
} from "./block";
import { getMcpUrl, getToken, setBlock } from "../config";
import { callTool } from "../mcp";

/** Resolve the configured /mcp URL + bearer, or throw a cell-friendly error. */
function requireConn(): { mcpUrl: string; token: string } {
  const mcpUrl = getMcpUrl();
  const token = getToken();
  if (!mcpUrl) {
    throw new Error("Set your Précis /mcp URL in the task pane.");
  }
  if (!token) {
    throw new Error("Sign in to Précis in the task pane.");
  }
  return { mcpUrl, token };
}

/**
 * Spills a Précis financial statement into the grid.
 *
 * Rows are statement lines (Revenue → EBITDA); columns are scenarios. Fetches a
 * live `financial_table` block from the configured `/mcp` (`run_statement`,
 * reading `structuredContent`) and spills it via `blockToGrid`. Style the spill
 * with "Apply Précis formatting" on the Home ribbon. Trailing args are optional.
 *
 * @customfunction STATEMENT
 * @helpurl https://docs.precis.finance/excel/functions/#precisstatement
 * @param statementKey Statement key, e.g. "pnl".
 * @param periodStart Range start, YYYY-MM (e.g. "2026-01").
 * @param periodEnd Range end, YYYY-MM (e.g. "2026-05").
 * @param scenarios Comma-separated scenario keys; optional alias via "key as Alias", e.g. "actuals as Act,budget as Bud,actuals_vs_budget as Var".
 * @param dimensions Comma-separated dimensions to break by, e.g. "cost_centre".
 * @param filters key=value pairs (comma-separated); multi-value with "|", e.g. "cost_centre=CC-100|CC-200".
 * @param scale Currency scaling power: 0=units, 3=thousands, 6=millions.
 * @param decimals Decimal places.
 * @param invocation Invocation handler (provides the anchor address).
 * @requiresAddress
 * @returns A statement grid (line items × scenarios) that spills into the cells.
 */
export async function statement(
  statementKey: string,
  periodStart: string,
  periodEnd: string,
  scenarios = "",
  dimensions = "",
  filters = "",
  scale = NaN,
  decimals = NaN,
  invocation: CustomFunctions.Invocation
  // a statement grid mixes string labels and numbers; the metadata plugin maps
  // `any[][]` to a matrix of "any" (unions like `(string|number)[][]` are unsupported).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any[][]> {
  const mcpUrl = getMcpUrl();
  const token = getToken();
  if (!mcpUrl) {
    throw new Error("Set your Précis /mcp URL in the task pane.");
  }
  if (!token) {
    throw new Error("Sign in to Précis in the task pane.");
  }
  const args: Record<string, unknown> = {
    statement: statementKey,
    period_start: periodStart,
    period_end: periodEnd,
  };
  applyCommonArgs(args, scenarios, dimensions, filters, scale, decimals);

  const block = await callTool<FinancialTableBlock>(mcpUrl, token, "run_statement", args);
  // Cache the block keyed by this cell's anchor so the ribbon formatter can
  // style the spill (re-warmed on every recalc). See spec §5.1.
  if (invocation.address) {
    setBlock(invocation.address, block);
  }
  return blockToGrid(block);
}

export function splitCsv(s: string | undefined): string[] {
  return (s || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

/**
 * Parse one scenario entry into the MCP `{scenario, alias?}` shape. Supports an
 * optional display alias via "key as Alias", e.g. "actuals as Act" →
 * { scenario: "actuals", alias: "Act" }.
 */
export function parseScenario(entry: string): { scenario: string; alias?: string } {
  const m = entry.match(/^(.*?)\s+as\s+(.+)$/i);
  return m ? { scenario: m[1].trim(), alias: m[2].trim() } : { scenario: entry };
}

/**
 * Parse "key=value" pairs (comma-separated) into a filters dict. A value may be
 * multi-valued with "|" → an array, e.g. "cost_centre=CC-100|CC-200,region=EMEA"
 * → { cost_centre: ["CC-100","CC-200"], region: "EMEA" }.
 */
export function parseFilters(s: string | undefined): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const pair of splitCsv(s)) {
    const eq = pair.indexOf("=");
    if (eq < 0) {
      continue;
    }
    const key = pair.slice(0, eq).trim();
    if (!key) {
      continue;
    }
    const vals = pair
      .slice(eq + 1)
      .split("|")
      .map((v) => v.trim())
      .filter(Boolean);
    if (vals.length > 0) {
      out[key] = vals.length > 1 ? vals : vals[0];
    }
  }
  return out;
}

/**
 * Fold the optional reporting args (scenarios / dimensions / filters / scale /
 * decimals) into the MCP tool-call args, omitting any that weren't supplied.
 * Shared by run_statement and run_metric (Excel passes null for omitted args, so
 * empty strings and non-finite numbers are treated as "use the server default").
 */
export function applyCommonArgs(
  args: Record<string, unknown>,
  scenarios: string,
  dimensions: string,
  filters: string,
  scale: number,
  decimals: number
): void {
  const scen = splitCsv(scenarios).map(parseScenario);
  if (scen.length > 0) {
    args.scenarios = scen;
  }
  const dims = splitCsv(dimensions);
  if (dims.length > 0) {
    args.dimensions = dims;
  }
  const filt = parseFilters(filters);
  if (Object.keys(filt).length > 0) {
    args.filters = filt;
  }
  if (Number.isFinite(scale)) {
    args.scale = scale;
  }
  if (Number.isFinite(decimals)) {
    args.decimals = decimals;
  }
}

/**
 * Spills a Précis metric breakdown into the grid.
 *
 * One or more metrics broken down by dimensions (rows) and scenarios (columns).
 * `layout` selects the shape: "report" (default) is a styled hierarchical grid
 * (group headers, subtotals, total — style it with "Apply Précis formatting");
 * "extract" is a flat, formula-friendly leaf table (dimensions as columns,
 * ribbon-free). Trailing args may be omitted.
 *
 * @customfunction METRIC
 * @helpurl https://docs.precis.finance/excel/functions/#precismetric
 * @param metrics Comma-separated metric keys, e.g. "revenue,utilisation".
 * @param periodStart Range start, YYYY-MM.
 * @param periodEnd Range end, YYYY-MM.
 * @param dimensions Comma-separated dimensions to break by, e.g. "cost_centre".
 * @param scenarios Comma-separated scenario keys; optional alias via "key as Alias", e.g. "actuals as Act,budget as Bud".
 * @param filters key=value pairs (comma-separated); multi-value with "|", e.g. "cost_centre=CC-100|CC-200".
 * @param layout "report" (default) or "extract".
 * @param scale Currency scaling power: 0=units, 3=thousands, 6=millions.
 * @param decimals Decimal places.
 * @param invocation Invocation handler (provides the anchor address).
 * @requiresAddress
 * @returns A metric grid that spills into the cells.
 */
export async function metric(
  metrics: string,
  periodStart: string,
  periodEnd: string,
  dimensions = "",
  scenarios = "",
  filters = "",
  layout = "report",
  scale = NaN,
  decimals = NaN,
  invocation: CustomFunctions.Invocation
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any[][]> {
  const mcpUrl = getMcpUrl();
  const token = getToken();
  if (!mcpUrl) {
    throw new Error("Set your Précis /mcp URL in the task pane.");
  }
  if (!token) {
    throw new Error("Sign in to Précis in the task pane.");
  }

  const metricList = splitCsv(metrics);
  if (metricList.length === 0) {
    throw new Error('Pass at least one metric, e.g. "revenue".');
  }

  const args: Record<string, unknown> = {
    metrics: metricList,
    period_start: periodStart,
    period_end: periodEnd,
  };
  applyCommonArgs(args, scenarios, dimensions, filters, scale, decimals);

  if ((layout || "report").trim().toLowerCase() === "extract") {
    // Flat leaf table: the raw _data variant + the extract flatten (ribbon-free).
    const data = await callTool<MetricDataResult>(mcpUrl, token, "run_metric_data", args);
    return flattenToGrid(data);
  }

  // Report layout: the render variant's block → cache for the ribbon → spill.
  const block = await callTool<FinancialTableBlock>(mcpUrl, token, "run_metric", args);
  if (invocation.address) {
    setBlock(invocation.address, block);
  }
  return blockToGrid(block);
}

/**
 * Lists or searches dimension members and ragged-hierarchy nodes.
 *
 * `search_hierarchy` returns two sections — leaf **records** (members) and ragged
 * **hierarchy_nodes** (rollup nodes). `output` chooses which one to spill. Omit
 * `query` to list all members of `dimension`; pass `query` for a free-text search.
 *
 * @customfunction HIERARCHY
 * @helpurl https://docs.precis.finance/excel/functions/#precishierarchy
 * @param dimension Dimension key, e.g. "cost_centre" (recommended when listing).
 * @param query Free-text search, e.g. "cloud"; omit to list all members.
 * @param output "records" (leaf members, default) or "nodes" (ragged hierarchy nodes).
 * @returns A flat table of the chosen section that spills into the cells.
 */
export async function hierarchy(
  dimension = "",
  query = "",
  output = "records"
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any[][]> {
  const { mcpUrl, token } = requireConn();
  const args: Record<string, unknown> = {};
  if ((dimension || "").trim()) {
    args.dimension = dimension.trim();
  }
  if ((query || "").trim()) {
    args.query = query.trim();
  }
  const res = await callTool<{ records?: unknown[]; hierarchy_nodes?: unknown[] }>(
    mcpUrl,
    token,
    "search_hierarchy",
    args
  );
  const wantNodes = (output || "records").trim().toLowerCase().startsWith("node");
  const rows = (wantNodes ? res.hierarchy_nodes : res.records) ?? [];
  return objectsToGrid(rows as Record<string, unknown>[]);
}

/**
 * Lists the available KPIs / metric catalogue as a flat table (keys, labels,
 * domains, formats, available dimensions). Discover valid metric keys for
 * `=PRECIS.METRIC(...)`.
 *
 * @customfunction KPIS
 * @helpurl https://docs.precis.finance/excel/functions/#preciskpis
 * @returns A flat table of the metric catalogue that spills into the cells.
 */
export async function kpis(): Promise<
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  any[][]
> {
  const { mcpUrl, token } = requireConn();
  // list_kpis returns a bare list, framed as { value: [...] } on the transport.
  const res = await callTool<{ value?: unknown[] } | unknown[]>(mcpUrl, token, "list_kpis", {});
  const list = Array.isArray(res) ? res : (res?.value ?? []);
  return objectsToGrid(list as Record<string, unknown>[]);
}

/**
 * Lists the available scenarios as a flat table — the keys/aliases to pass to
 * `=PRECIS.STATEMENT` / `=PRECIS.METRIC`. `section` picks which part of the
 * registry: "real" (concrete scenarios, default), "comparisons" (generated
 * variance keys such as actuals_vs_budget), "shifted" (period-shifted), or
 * "aliases". Scope-filtered to what the signed-in user can read.
 *
 * @customfunction SCENARIOS
 * @helpurl https://docs.precis.finance/excel/functions/#precisscenarios
 * @param section "real" (default), "comparisons", "shifted", or "aliases".
 * @returns A flat table of scenarios that spills into the cells.
 */
export async function scenarios(
  section = ""
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any[][]> {
  const { mcpUrl, token } = requireConn();
  const res = await callTool<{ registry?: Record<string, unknown[]> }>(
    mcpUrl,
    token,
    "list_scenarios",
    {}
  );
  const registry = res.registry ?? {};
  const s = (section || "real").trim().toLowerCase();
  const key = s.startsWith("comp")
    ? "comparisons"
    : s.startsWith("shift")
      ? "shifted"
      : s.startsWith("alias")
        ? "compatibility_aliases"
        : "real";
  return objectsToGrid((registry[key] ?? []) as Record<string, unknown>[]);
}
