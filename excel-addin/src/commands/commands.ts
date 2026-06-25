/*
 * Ribbon commands. Loaded into the shared runtime via taskpane.html, so it
 * shares global state (the cached blocks + token) with the custom functions.
 */

/* global console, document, Excel, Office */

import { getBlock, setBlock, getMcpUrl, getToken, blockKeys } from "../config";
import { applyBlockFormat } from "../format";
import { FinancialTableBlock } from "../functions/block";
import { callTool } from "../mcp";
import { splitCsv, applyCommonArgs } from "../functions/functions";

Office.onReady(() => {
  // Office.js is ready.
});

/** Report progress/errors to the console and (if open) the task-pane status. */
function report(msg: string, isError = false): void {
  const line = `[Précis] ${msg}`;
  if (isError) {
    console.error(line);
  } else {
    console.log(line);
  }
  try {
    const el = document.getElementById("status");
    if (el) {
      el.textContent = msg;
    }
  } catch {
    /* no DOM available (task pane never opened) — console only */
  }
}

/**
 * "Apply Précis formatting" — resolve the selected spill's anchor, get its
 * format manifest (from the shared-runtime cache, or by re-fetching from the
 * anchor's =PRECIS.* formula on a cold/mismatched cache), and apply it over the
 * spill. Every exit reports a reason so failures aren't silent.
 */
async function applyPrecisFormatting(event: Office.AddinCommands.Event): Promise<void> {
  try {
    await Excel.run(async (context) => {
      const cell = context.workbook.getSelectedRange().getCell(0, 0);
      const parent = cell.getSpillParentOrNullObject();
      cell.load(["address", "formulas"]);
      parent.load(["address", "isNullObject", "formulas"]);
      await context.sync();

      const onAnchor = parent.isNullObject;
      const anchor = onAnchor ? cell : parent;
      const anchorAddress = onAnchor ? cell.address : parent.address;
      // The formula lives on the anchor; a spilled cell has none.
      const formula = String(
        (onAnchor ? (cell as Excel.Range) : (parent as Excel.Range)).formulas?.[0]?.[0] ?? ""
      );

      let block = getBlock(anchorAddress);
      if (!block) {
        report(
          `No cached format for ${anchorAddress} — re-fetching from the formula… ` +
            `(cached anchors: ${blockKeys().join(", ") || "none"})`
        );
        block = await refetchFromFormula(formula);
        if (block) {
          setBlock(anchorAddress, block);
        }
      }
      if (!block) {
        report(
          "Select a Précis spill (a =PRECIS.STATEMENT or =PRECIS.METRIC cell) and try again."
        );
        return;
      }

      const spill = anchor.getSpillingToRangeOrNullObject();
      spill.load(["isNullObject", "address", "rowCount", "columnCount"]);
      await context.sync();
      if (spill.isNullObject) {
        report(`${anchorAddress} isn't spilling (single value, or a #SPILL! error) — nothing to format.`);
        return;
      }

      const blockCols = (block.columns ?? []).length;
      const blockRows = (block.rows ?? []).filter((r) => r.row_type !== "separator").length;
      if (blockCols === 0 || blockRows === 0) {
        report(`Nothing to format — block has ${blockRows} rows × ${blockCols} cols.`);
        return;
      }

      applyBlockFormat(spill, block);
      await context.sync(); // single batched apply
      report(
        `Formatted ${spill.address} (block ${blockRows}×${blockCols}, spill ${spill.rowCount}×${spill.columnCount}).`
      );
    });
  } catch (e) {
    report(`Apply formatting failed: ${(e as Error).message}`, true);
  }
  event.completed();
}

/** Parse Excel formula args, honouring quotes + either `,` or `;` separators. */
function parseFormulaArgs(inner: string): string[] {
  const args: string[] = [];
  let cur = "";
  let inQuote = false;
  for (const ch of inner) {
    if (ch === '"') {
      inQuote = !inQuote;
    } else if (!inQuote && (ch === "," || ch === ";")) {
      args.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  args.push(cur.trim());
  return args;
}

function toNum(s: string | undefined): number {
  const n = Number((s ?? "").trim());
  return Number.isFinite(n) ? n : NaN;
}

/**
 * Cold-cache fallback: reconstruct the tool call from the anchor's =PRECIS.*
 * formula and re-fetch the block. Returns null if the cell isn't a formattable
 * PRECIS spill (or we can't reach /mcp). Mirrors the custom-function arg mapping.
 */
async function refetchFromFormula(formula: string): Promise<FinancialTableBlock | null> {
  const m = formula.match(/=\s*PRECIS\.(STATEMENT|METRIC)\s*\((.*)\)\s*$/i);
  if (!m) {
    return null;
  }
  const fn = m[1].toUpperCase();
  const a = parseFormulaArgs(m[2]);

  const mcpUrl = getMcpUrl();
  const token = getToken();
  if (!mcpUrl || !token) {
    report("Re-fetch needs a connection — open the task pane and sign in.");
    return null;
  }

  const args: Record<string, unknown> = {};
  let tool: string;
  if (fn === "STATEMENT") {
    // STATEMENT(statement, periodStart, periodEnd, scenarios, dimensions, filters, scale, decimals)
    args.statement = a[0];
    args.period_start = a[1];
    args.period_end = a[2];
    applyCommonArgs(args, a[3] ?? "", a[4] ?? "", a[5] ?? "", toNum(a[6]), toNum(a[7]));
    tool = "run_statement";
  } else {
    // METRIC(metrics, periodStart, periodEnd, dimensions, scenarios, filters, layout, scale, decimals)
    const layout = (a[6] ?? "report").toLowerCase();
    if (layout === "extract") {
      report("Extract layout is a flat table — nothing to format.");
      return null;
    }
    args.metrics = splitCsv(a[0]);
    args.period_start = a[1];
    args.period_end = a[2];
    applyCommonArgs(args, a[4] ?? "", a[3] ?? "", a[5] ?? "", toNum(a[7]), toNum(a[8]));
    tool = "run_metric";
  }
  return await callTool<FinancialTableBlock>(mcpUrl, token, tool, args);
}

/**
 * "Refresh" ribbon command — re-invoke every PRECIS.* function so they re-fetch
 * from /mcp (full rebuild = Ctrl+Alt+Shift+F9).
 */
async function refreshPrecisData(event: Office.AddinCommands.Event): Promise<void> {
  try {
    await Excel.run(async (context) => {
      context.workbook.application.calculate(Excel.CalculationType.fullRebuild);
      await context.sync();
    });
    report("Refreshed — Précis cells re-fetched.");
  } catch (e) {
    report(`Refresh failed: ${(e as Error).message}`, true);
  }
  event.completed();
}

Office.actions.associate("applyPrecisFormatting", applyPrecisFormatting);
Office.actions.associate("refreshPrecisData", refreshPrecisData);
