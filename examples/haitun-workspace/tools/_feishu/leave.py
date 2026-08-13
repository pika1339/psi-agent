"""Feishu Leave (请假) — read the todo board's "请假表" sub-sheet and judge date overlaps.

Split out of ``_feishu_impl.py`` by domain, following the same shape as
``attendance.py`` / ``sheet.py``. This module reaches the shared client/token
layer through ``_core`` so that everything patched on ``_feishu_impl``
(``_invoke``, ``_get_client``, ``_get_valid_uat``, ...) keeps taking effect
here.

There is no Feishu endpoint that answers "who is on leave" — ``/approval/v4/instances``
only supports creating an instance or looking one up by id (no per-person enumeration),
and ``attendance:task:readonly`` returns clock results (Normal/Late/Early/Lack), not leave
records. So leave is tracked as a plain sub-sheet next to the todo board (same
``sheet_token``, a "请假表" worksheet: 姓名/开始日期/结束日期/类型/是否整天/备注), and this
module does the one thing that must not be left to a model: date-interval overlap
judgment. Getting that wrong once mis-marks someone's leave status for a whole cycle.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import _feishu_impl as _core

# Column header synonyms tolerate minor label drift without hardcoding a fixed layout —
# same discipline as feishu-todo-board-sync: structure is discovered each run.
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "名字", "负责人"),
    "start": ("开始日期", "起始日期", "开始时间"),
    "end": ("结束日期", "截止日期", "结束时间"),
    "type": ("类型", "请假类型", "假期类型"),
    "full_day": ("是否整天", "整天", "全天"),
    "note": ("备注", "说明"),
}

# Feishu Sheets' values API returns a date-formatted cell as an Excel/Lotus serial
# number (days since 1899-12-30, the same epoch Excel uses) rather than "YYYY-MM-DD" —
# a plain str() of the raw cell would silently misparse every date in the column.
_SERIAL_EPOCH = date(1899, 12, 30)


def _parse_cell_date(raw: str) -> date | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        serial = float(text)
    except ValueError:
        return None
    try:
        return _SERIAL_EPOCH + timedelta(days=int(serial))
    except (OverflowError, ValueError):
        return None


def _match_header(cell: str, keys: tuple[str, ...]) -> bool:
    normalized = cell.strip()
    return any(normalized == k or k in normalized for k in keys)


def _index_columns(header_row: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for field, synonyms in _HEADER_SYNONYMS.items():
        for idx, cell in enumerate(header_row):
            if _match_header(cell, synonyms):
                columns[field] = idx
                break
    return columns


def _parse_full_day(raw: str) -> bool:
    text = raw.strip().casefold()
    return text in ("是", "true", "1", "yes", "y", "整天", "全天")


def _daterange(start: date, end: date) -> list[str]:
    days = (end - start).days
    if days < 0:
        return []
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


async def _resolve_sheet_id(token: str, sheet_name: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a worksheet tab title to its SHEET_ID. Returns (sheet_id, error_result)."""
    meta = await _core._invoke(_core._build_sheet_meta_request(token))
    if not meta["ok"]:
        return None, meta
    sheets = meta["data"].get("sheets", []) if isinstance(meta["data"], dict) else []
    target = sheet_name.strip()
    for sh in sheets if isinstance(sheets, list) else []:
        title = sh.get("title", "")
        if title == target:
            sheet_id = sh.get("sheet_id") or sh.get("sheetId")
            if sheet_id:
                return str(sheet_id), None
    available = [sh.get("title", "") for sh in sheets if isinstance(sh, dict)]
    return None, _core._error(f"No worksheet named {target!r} in this spreadsheet. Available: {available}")


async def query_leave_impl(
    sheet_token: str,
    sheet_name: str,
    date_from: str,
    date_to: str,
    names_json: str = "",
) -> dict[str, Any]:
    """Read the "请假表" sub-sheet and return each person's leave dates that overlap the window."""
    if not sheet_token.strip():
        return _core._error("sheet_token is required (same spreadsheet_token as the todo board).")
    if not sheet_name.strip():
        return _core._error("sheet_name is required — the worksheet tab title, e.g. '请假表'.")
    query_start = _parse_cell_date(date_from)
    query_end = _parse_cell_date(date_to)
    if query_start is None:
        return _core._error(f"date_from {date_from!r} is not a valid date (expected YYYY-MM-DD).")
    if query_end is None:
        return _core._error(f"date_to {date_to!r} is not a valid date (expected YYYY-MM-DD).")
    if query_end < query_start:
        return _core._error("date_to must not be before date_from.")

    names_filter: set[str] | None = None
    if names_json.strip():
        try:
            parsed = json.loads(names_json)
        except ValueError as exc:
            return _core._error(f"names_json is not valid JSON: {exc}")
        if not isinstance(parsed, list) or not all(isinstance(n, str) for n in parsed):
            return _core._error("names_json must be a JSON array of name strings.")
        names_filter = {n.strip() for n in parsed if n.strip()}

    sheet_id, err = await _resolve_sheet_id(sheet_token.strip(), sheet_name)
    if err is not None:
        return err
    assert sheet_id is not None

    values_res = await _core._invoke(_core._build_sheet_values_request(sheet_token.strip(), sheet_id))
    if not values_res["ok"]:
        return values_res
    value_range = values_res["data"].get("valueRange", {}) if isinstance(values_res["data"], dict) else {}
    raw_rows = value_range.get("values") or []
    rows = [[_core._flatten_sheet_cell(c) for c in (row if isinstance(row, list) else [])] for row in raw_rows]
    if not rows:
        return {
            "ok": True,
            "date_from": query_start.isoformat(),
            "date_to": query_end.isoformat(),
            "results": [],
            "count": 0,
        }

    header, *data_rows = rows
    columns = _index_columns(header)
    missing = [f for f in ("name", "start", "end") if f not in columns]
    if missing:
        return _core._error(
            f"Could not find required column(s) {missing} in the header row {header!r}. "
            "Expected something like 姓名 / 开始日期 / 结束日期."
        )

    def cell(row: list[str], field: str) -> str:
        idx = columns.get(field)
        return row[idx] if idx is not None and idx < len(row) else ""

    results: list[dict[str, Any]] = []
    for row_num, row in enumerate(data_rows, start=2):  # header is row 1
        name = cell(row, "name").strip()
        if not name:
            continue
        if names_filter is not None and name not in names_filter:
            continue
        start = _parse_cell_date(cell(row, "start"))
        end = _parse_cell_date(cell(row, "end"))
        if start is None or end is None:
            continue
        overlap_start = max(start, query_start)
        overlap_end = min(end, query_end)
        if overlap_start > overlap_end:
            continue
        results.append(
            {
                "row": row_num,
                "name": name,
                "leave_type": cell(row, "type").strip(),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "is_full_day": _parse_full_day(cell(row, "full_day")),
                "note": cell(row, "note").strip(),
                "overlap_start": overlap_start.isoformat(),
                "overlap_end": overlap_end.isoformat(),
                "hit_dates": _daterange(overlap_start, overlap_end),
            }
        )

    return {
        "ok": True,
        "date_from": query_start.isoformat(),
        "date_to": query_end.isoformat(),
        "results": results,
        "count": len(results),
    }
