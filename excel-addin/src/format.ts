/*
 * The ribbon formatter — the add-in's Office.js renderer of the financial_table
 * block. It reads the cached block's format intent (row roles, indent,
 * per-column nf, resolved per-cell alerts) and applies it positionally over the
 * already-spilled range. Values are untouched.
 *
 * Each renderer owns its palette; the favorability logic is single-sourced
 * server-side and arrives pre-resolved in row.alerts.
 *
 * All operations are queued; the caller wraps this in Excel.run and a single
 * context.sync() (one batched apply).
 */

import { FinancialTableBlock } from "./functions/block";

/* global Excel */

const SLATE_900 = "#0F172A"; // header fill
const SLATE_100 = "#F1F5F9"; // subtotal fill
const SLATE_200 = "#E2E8F0"; // total fill
const WHITE = "#FFFFFF";
const GREEN = "#16A34A"; // favourable variance
const RED = "#DC2626"; // unfavourable variance

/** Queue the Précis formatting of `spill` from `block`. No context.sync(). */
export function applyBlockFormat(spill: Excel.Range, block: FinancialTableBlock): void {
  const cols = block.columns ?? [];
  // Mirror blockToGrid: separators are dropped, so data rows align 1:1 with the
  // spilled rows beneath the single header row.
  const dataRows = (block.rows ?? []).filter((r) => r.row_type !== "separator");
  const nCols = cols.length;
  if (nCols === 0 || dataRows.length === 0) {
    return;
  }
  // Grouped (multi-metric × multi-scenario) blocks spill a two-row header
  // (metric group labels above scenario labels) — mirror blockToGrid so the
  // format ops land on the right rows.
  const grouped = cols.some((c) => c.group);
  // Derive the header-row count from the ACTUAL spill height (total rows − data
  // rows) so the number-format array always matches the range — robust to a
  // spill produced by an older bundle version. Fall back to the block's own
  // expectation (grouped → 2) if the spill height looks off.
  const derived = spill.rowCount - dataRows.length;
  const headerRows = derived === 1 || derived === 2 ? derived : grouped ? 2 : 1;

  // 0. Reset prior formatting first. Applying is additive, so a re-run with
  //    changed columns (e.g. a variance column that's now a plain scenario)
  //    would otherwise leave stale variance colours / fills / borders behind.
  //    Queued before the apply ops, so within one sync it clears then restyles.
  spill.clear(Excel.ClearApplyTo.formats);

  // 1. Number formats — one 2-D array over the whole spill (header + label col
  //    are text; value cells take their column's nf).
  const nf: string[][] = [];
  for (let h = 0; h < headerRows; h++) {
    nf.push(cols.map(() => "@"));
  }
  for (const row of dataRows) {
    const line: string[] = ["@"];
    for (let c = 1; c < nCols; c++) {
      // A statement row carries its own format (currency vs percent vs ratio);
      // it wins over the scenario column's default. Breakdown rows have no nf,
      // so the column nf applies.
      line.push(row.nf ?? cols[c].nf ?? "General");
    }
    nf.push(line);
  }
  // The anchor cell [0][0] holds the =PRECIS.* formula. Text format ("@") makes
  // Excel treat an edited formula as a literal string (it stops executing), so
  // keep the anchor General — the rest of the label column stays text.
  nf[0][0] = "General";
  spill.numberFormat = nf;

  // 2. Alignment: label column left, value columns right.
  spill.getColumn(0).format.horizontalAlignment = "Left";
  for (let c = 1; c < nCols; c++) {
    spill.getColumn(c).format.horizontalAlignment = "Right";
  }

  // 3. Header row(s): two when grouped (metric group labels + scenario labels).
  for (let h = 0; h < headerRows; h++) {
    const header = spill.getRow(h);
    header.format.fill.color = SLATE_900;
    header.format.font.color = WHITE;
    header.format.font.bold = true;
    header.format.horizontalAlignment = "Center";
  }
  if (headerRows >= 2) {
    // Metric group labels read from the start of each group (can't merge).
    spill.getRow(0).format.horizontalAlignment = "Left";
  }
  // The dimension/label header sits on the bottom header row, left-aligned.
  spill.getCell(headerRows - 1, 0).format.horizontalAlignment = "Left";

  // 4. Per data row: role styling + indent + variance colour.
  for (let i = 0; i < dataRows.length; i++) {
    const row = dataRows[i];
    const r = headerRows + i; // spill row index (after the header row(s))
    const rowRange = spill.getRow(r);

    if (row.row_type === "subtotal") {
      rowRange.format.fill.color = SLATE_100;
      rowRange.format.font.bold = true;
      rowRange.format.borders.getItem("EdgeTop").style = "Continuous";
    } else if (row.row_type === "total") {
      rowRange.format.fill.color = SLATE_200;
      rowRange.format.font.bold = true;
      rowRange.format.borders.getItem("EdgeTop").style = "Continuous";
      rowRange.format.borders.getItem("EdgeBottom").style = "Continuous";
    } else if (row.row_type === "group_header" || row.row_type === "header") {
      rowRange.format.font.bold = true;
    }

    const indent = row.indent ?? 0;
    if (indent > 0) {
      spill.getCell(r, 0).format.indentLevel = indent;
    }

    const alerts = row.alerts ?? {};
    for (let c = 1; c < nCols; c++) {
      const a = alerts[cols[c].key];
      if (a === "favorable") {
        spill.getCell(r, c).format.font.color = GREEN;
      } else if (a === "unfavorable") {
        spill.getCell(r, c).format.font.color = RED;
      }
    }
  }
}
