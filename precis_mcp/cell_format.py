# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Presentation helpers shared across every render target.

Two pure functions — no I/O, no openpyxl — so they live in the open core and are
imported by every consumer that maps engine output to a rendering surface:

- :func:`favorability` — resolves a variance cell's value + effect to the
  engine's favourable / unfavourable signal. The **single source of truth** for
  the sign-aware variance rule (a positive revenue delta is favourable, a
  positive cost delta is unfavourable). The openpyxl Excel writer, the React
  table, and the Excel add-in formatter all read this instead of re-deriving the
  sign logic (docs/precis-excel-addin-spec.md §5.2).
- :func:`excel_number_format` — the Excel custom number-format string (en-dash
  negatives) for a metric format + decimal count. Shared by the openpyxl writer
  and the add-in grid manifest so the en-dash convention is encoded once.
"""
from __future__ import annotations

# en-dash (U+2013), not a hyphen — the brand negative sign.
_EN_DASH = "–"


def favorability(
    value: float | int | None,
    variance_effect: str,
) -> str | None:
    """Resolve a variance value to ``"favorable"`` / ``"unfavorable"`` / ``None``.

    The sign-aware rule, in one place: a ``natural`` effect treats a positive
    delta as favourable (revenue up), an ``inverse`` effect treats a positive
    delta as unfavourable (cost up), and ``neutral`` — or a zero / missing
    value — carries no signal. Drive variance colour off this, never off the
    raw sign of the value.
    """
    if variance_effect == "neutral" or value is None or value == 0:
        return None
    if variance_effect == "inverse":
        return "unfavorable" if value > 0 else "favorable"
    # natural (default)
    return "favorable" if value > 0 else "unfavorable"


def excel_number_format(fmt: str, decimals: int = 0) -> str:
    """Return an Excel custom number-format string (en-dash negatives).

    ``percent`` is always one decimal place. The engine emits human-readable
    percents (e.g. ``15.3``, not ``0.153``), so the ``%`` is a **literal suffix**
    (``"%"``) — Excel's percent operator would multiply the value by 100 and show
    1530%. ``currency`` / ``number`` honour ``decimals``.
    """
    if fmt == "percent":
        return f'0.0"%";{_EN_DASH}0.0"%"'
    if fmt == "currency" or fmt == "number":
        dec_part = ("." + "0" * decimals) if decimals > 0 else ""
        return f"#,##0{dec_part};{_EN_DASH}#,##0{dec_part}"
    return f"#,##0;{_EN_DASH}#,##0"
