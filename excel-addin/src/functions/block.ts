/*
 * The add-in's half of the `financial_table` contract: pivot the block's
 * columns + rows into the 2-D matrix a custom function spills. Values only — the
 * format intent (`nf` / `alerts` / `row_type`) is read separately by the ribbon
 * formatter; it is not consumed here.
 */

export interface FinancialColumn {
  key: string;
  label?: string;
  format?: string;
  decimals?: number;
  nf?: string;
  variance?: boolean;
  group?: string;
}

export interface FinancialRow {
  row_type?: string;
  label?: string;
  indent?: number;
  values?: Record<string, number | null>;
  alerts?: Record<string, string>;
  // Per-row Excel number format (statement rows = metrics with their own
  // format). Takes precedence over the column nf when present.
  nf?: string;
}

export interface FinancialTableBlock {
  type: string;
  title?: string;
  columns: FinancialColumn[];
  rows: FinancialRow[];
}

/*
 * Formula-injection guard (OWASP CSV-injection class). A string cell that begins
 * with =, +, -, @, or a leading control char is parsed as a formula when it
 * re-enters Excel's cell parser — on Paste-Special→Values out of the spill, or
 * on CSV export and reopen. Custom-function spill results are rendered as literal
 * text (not re-evaluated), so for the live spill this is defence-in-depth; it
 * closes the downstream re-entry paths for every externally-sourced string
 * (dimension members, statement/metric labels, scenario aliases) before it lands
 * in a cell. The standard "'" text-qualifier prefix neutralises the trigger and
 * is hidden by Excel on re-entry. Numbers are never touched — figures spill as
 * real numbers, so negatives stay numeric.
 */
const FORMULA_TRIGGER = /^[=+\-@\t\r]/;

function sanitizeCell(v: string | number): string | number {
  return typeof v === "string" && FORMULA_TRIGGER.test(v) ? `'${v}` : v;
}

/** Apply the formula-injection guard to every string cell in a spill matrix. */
function sanitizeGrid(grid: (string | number)[][]): (string | number)[][] {
  return grid.map((row) => row.map(sanitizeCell));
}

/**
 * Pivot a `financial_table` block into the spill matrix: a header row of column
 * labels, then one row per data row (`label` in column 0, then each value
 * column keyed by `column.key`). Separator rows are dropped. Missing values
 * become "" so the cell is blank rather than `0`/error.
 */
export function blockToGrid(block: FinancialTableBlock): (string | number)[][] {
  const cols = block.columns ?? [];
  const grouped = cols.some((c) => c.group);
  const grid: (string | number)[][] = [];

  // Grouped (multi-metric × multi-scenario) blocks get a two-row header: the
  // metric group label above the first column of each group run (blank after —
  // a spill can't merge cells), then the scenario labels beneath.
  if (grouped) {
    const groupRow: (string | number)[] = [""];
    let prev: string | undefined;
    for (let i = 1; i < cols.length; i++) {
      const g = cols[i].group;
      groupRow.push(g && g !== prev ? g : "");
      prev = g;
    }
    grid.push(groupRow);
  }

  grid.push(cols.map((c) => c.label ?? c.key));

  for (const row of block.rows ?? []) {
    if (row.row_type === "separator") continue;
    const line: (string | number)[] = [row.label ?? ""];
    for (let i = 1; i < cols.length; i++) {
      const v = row.values?.[cols[i].key];
      line.push(v == null ? "" : v);
    }
    grid.push(line);
  }
  return sanitizeGrid(grid);
}

/*
 * The `_data` (agent) variant of run_metric returns the raw unified engine
 * result, not a block. `flattenToGrid` is the add-in's port of the server's
 * `_flatten_role_indexed` (extract layout, spec §6.2): leaf rows only,
 * dimensions as key columns, scenario aliases as value columns — a flat,
 * formula-friendly rectangle. Ribbon-free.
 */

export interface MetricRow {
  grain?: string;
  dimensions?: Record<string, string>;
  item?: { key?: string; label?: string };
  values?: Record<string, number | null>;
}

export interface MetricDataResult {
  kind?: string;
  dimensions?: string[];
  scenarios?: { alias?: string }[];
  rows?: MetricRow[];
}

/*
 * Generic "list of records → flat spill" for the discovery tools (search_hierarchy
 * sections, list_kpis): a header row of the union of keys (first-seen order), then
 * one row per record. Arrays are joined; objects are JSON-stringified; null → "".
 */
function cellValue(v: unknown): string | number {
  if (v == null) {
    return "";
  }
  if (typeof v === "number" || typeof v === "string") {
    return v;
  }
  if (typeof v === "boolean") {
    return v ? "TRUE" : "FALSE";
  }
  if (Array.isArray(v)) {
    return v.map((x) => String(x)).join(", ");
  }
  return JSON.stringify(v);
}

export function objectsToGrid(rows: Record<string, unknown>[]): (string | number)[][] {
  if (!rows || rows.length === 0) {
    return [["(no results)"]];
  }
  const cols: string[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    for (const k of Object.keys(r ?? {})) {
      if (!seen.has(k)) {
        seen.add(k);
        cols.push(k);
      }
    }
  }
  const grid: (string | number)[][] = [cols];
  for (const r of rows) {
    grid.push(cols.map((c) => cellValue((r ?? {})[c])));
  }
  return sanitizeGrid(grid);
}

export function flattenToGrid(result: MetricDataResult): (string | number)[][] {
  const dims = result.dimensions ?? [];
  const valueCols = (result.scenarios ?? []).map((s) => s.alias).filter(Boolean) as string[];
  const detail = (result.rows ?? []).filter((r) => r.grain === "detail");

  const itemKeys = new Set(detail.map((r) => r.item?.key).filter(Boolean));
  const multiMetric = result.kind !== "statement" && itemKeys.size > 1;
  const isStatement = result.kind === "statement";

  const keyCols = [...dims];
  if (isStatement) {
    keyCols.push("line_item");
  } else if (multiMetric) {
    keyCols.push("metric");
  }

  const grid: (string | number)[][] = [[...keyCols, ...valueCols]];
  for (const r of detail) {
    const line: (string | number)[] = [];
    for (const d of dims) {
      line.push(r.dimensions?.[d] ?? "");
    }
    if (isStatement) {
      line.push(r.item?.key ?? "");
    } else if (multiMetric) {
      line.push(r.item?.label ?? r.item?.key ?? "");
    }
    for (const alias of valueCols) {
      const v = r.values?.[alias];
      line.push(v == null ? "" : v);
    }
    grid.push(line);
  }
  return sanitizeGrid(grid);
}
