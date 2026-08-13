"""Feishu/Lark leave (请假) tool — read the todo board's "请假表" sub-sheet.

There is no Feishu endpoint that answers "who is on leave right now": the approval
API (``/approval/v4/instances``) only supports creating an instance or looking one up
by ``instance_id`` — no per-person enumeration — and ``feishu_attendance_query``
returns clock results (Normal/Late/Early/Lack), not leave records. So leave is tracked
as a plain sub-sheet living next to the todo board (same ``sheet_token``, worksheet
title e.g. "请假表": 姓名/开始日期/结束日期/类型/是否整天/备注), filled by the employee
(or their mentor).

What this tool owns is the one thing that must not be left to a model: date-interval
overlap judgment against a query window. Getting that wrong once mis-marks someone's
leave status — and the direction of that mistake (calling a present person "on leave")
silently drops their todos, so this is deterministic code, not a prompt instruction.

Requires ``PSI_FEISHU_APP_ID`` / ``PSI_FEISHU_APP_SECRET`` (same sheet-read scope as
``feishu_sheet_read``).
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f


async def feishu_leave_query(
    sheet_token: str,
    sheet_name: str,
    date_from: str,
    date_to: str,
    names_json: str = "",
) -> str:
    """Read a "请假表" sub-sheet and return each person's leave dates overlapping a window.

    The sub-sheet lives in the same spreadsheet as the todo board (``sheet_token``
    from the board's URL) as its own worksheet tab. Its header row is discovered each
    call — column order and exact wording may drift, matched by synonym (姓名/名字,
    开始日期/起始日期, ...) rather than fixed position — so re-ordering columns in
    Feishu does not break this tool.

    Args:
        sheet_token: The spreadsheet_token shared with the todo board (the part after
            ``/sheets/`` in the sheet URL; for a wiki-hosted sheet, resolve via
            ``feishu_api`` on ``GET /open-apis/wiki/v2/spaces/get_node`` first).
        sheet_name: The worksheet tab title holding the leave records, e.g. "请假表".
        date_from: Start of the query window, YYYY-MM-DD.
        date_to: End of the query window, YYYY-MM-DD (inclusive).
        names_json: Optional JSON array of names to restrict the result to
            (e.g. this cycle's roster) — e.g. '["张三", "李四"]'. Empty = everyone in
            the sheet.

    Returns:
        JSON with ok, date_from, date_to, count, and a ``results`` list of
        {row, name, leave_type, start_date, end_date, is_full_day, note,
        overlap_start, overlap_end, hit_dates} — one entry per person whose leave
        interval overlaps [date_from, date_to]. A person with no overlapping leave
        row is simply absent from ``results`` (not an error).
    """
    return _f.dumps_result(await _f.query_leave_impl(sheet_token, sheet_name, date_from, date_to, names_json))
