"""A TODO-list card: many rows, each tickable on its own, each linked to a Feishu task.

Distinct from ``feishu_message_send_card`` (one card, one answer) because a todo list is
consumed row by row. The single-use machinery still applies **per action id**, so a click
cannot be replayed — but each row gets a bounded chain of alternating tick/untick ids
(see ``_todo_card_impl._UNDO_ROUNDS``) instead of just one, so a misclick can be undone
and the row ticked again. Every button also carries the whole card's current state (see
``_todo_card_impl``'s module docstring for why — Feishu's own click-time card snapshot is
not safe to trust once more than one row has been touched). ``feishu_todo_card_tick`` /
``feishu_todo_card_untick`` do the actual task-completion, ledger-sync, and card-editing
work; this file only builds and sends the initial card, reusing ``_todo_card_impl`` for
the shared rendering logic.
"""

from __future__ import annotations

import json

import _feishu_impl as _f
import _todo_card_impl as _impl


async def feishu_todo_card_send(
    receive_id: str,
    items_json: str,
    title: str = "今日 TODO",
    subtitle: str = "",
    shape: str = "circle",
    receive_id_type: str = "open_id",
    user_key: str = "",
    ledger_app_token: str = "",
    ledger_table_id: str = "",
) -> str:
    """Send one card listing a person's todos, each row ticked (and un-ticked) independently.

    Use this instead of ``feishu_message_send_card`` whenever the recipient must act on
    **several** items from one message (今日待办清单). Each row shows a shape marker, its
    title as a link to the matching Feishu task, an optional detail line, and its own
    「标记完成」button. Ticking a row rewrites that row as ``● ~~已完成~~`` and adds a
    「撤销」button in its place — the row can flip between done/open up to
    ``_todo_card_impl._UNDO_ROUNDS`` times (a misclick is recoverable) before locking in
    its final state; other rows keep working throughout, which a normal card cannot do
    (its first click retires the whole card).

    Each row's tick/untick actions are dispatched to ``feishu_todo_card_tick`` /
    ``feishu_todo_card_untick`` respectively, so mark/reopen the underlying Feishu task
    there. Rows already marked ``done`` at send time are rendered read-only with no
    button — they never entered this tick/untick flow, so there is nothing to undo.

    ``items_json`` is a JSON array (max 40) of objects::

        [{"title": "写周报", "task_guid": "abc-123", "detail": "周五 18:00 前",
          "shape": "square", "done": false, "link": "https://...",
          "ledger_record_id": "recXXXX"}]

    - ``title`` — the todo text (required; a blank one becomes "任务 N").
    - ``task_guid`` — the Feishu task this row links to, from
      ``POST /open-apis/task/v2/tasks``. Rendered as an applink; the task API's response
      carries no web URL, so do not wait for one.
    - ``link`` — an explicit URL that overrides the applink (use it for a doc instead).
    - ``shape`` — per-row shape: circle ○● / square □■ / diamond ◇◆ / triangle △▲ /
      star ☆★ / check ☐☑. Falls back to the card-level ``shape``.
    - ``detail`` — a second line under the title (deadline, acceptance criteria).
    - ``done`` — pre-completed rows render struck-through with no button (see above).
    - ``ledger_record_id`` — this row's record in the mentor-ledger Bitable table named by
      ``ledger_app_token``/``ledger_table_id`` (below). When set, ticking/unticking writes
      that record's 状态 field (已交付 / 待开始) so the ledger does not drift from what the
      card shows. Omit to skip ledger sync entirely (e.g. a card unrelated to a ledger).

    Create the Feishu tasks **before** calling this so每行都有 ``task_guid``; a row without
    one still ticks, it just is not clickable through to a task.

    Args:
        receive_id: Who gets the card — usually the doer's ``ou_...`` open_id.
        items_json: JSON array of todo objects, described above.
        title: Card header text.
        subtitle: A line above the progress counter (date, mentor, source table).
        shape: Default shape for rows that do not set their own.
        receive_id_type: Auto-detected from the id prefix; only set for a bare user_id.
        user_key: Send as this person instead of the bot. Omit for the bot's own identity.
        ledger_app_token: The mentor-ledger Bitable app_token rows with a
            ``ledger_record_id`` live in. Required for ledger sync to fire; ignored
            otherwise.
        ledger_table_id: The table id within ``ledger_app_token``. Same rule as above.
    """
    if not isinstance(items_json, str):
        return "[Error] items_json must be a JSON string containing an array"
    try:
        raw_items = json.loads(items_json)
    except ValueError as exc:
        return f"[Error] items_json is not valid JSON: {exc}"
    if not isinstance(raw_items, list) or not raw_items:
        return "[Error] items_json must be a non-empty JSON array of todo objects"
    if len(raw_items) > _impl._MAX_ITEMS:
        return f"[Error] too many todos ({len(raw_items)}); split into cards of at most {_impl._MAX_ITEMS}"
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        return "[Error] every item in items_json must be a JSON object"

    card, handlers = _impl._build_todo_card(
        items=items,
        title=title,
        subtitle=subtitle,
        shape=shape,
        ledger_app_token=ledger_app_token.strip(),
        ledger_table_id=ledger_table_id.strip(),
    )
    if not handlers:
        return "[Error] every todo is already done; nothing to send"
    result = await _f.send_card_impl(
        receive_id,
        json.dumps(card, ensure_ascii=False),
        receive_id_type,
        user_key or None,
        "{}",
        json.dumps(handlers, ensure_ascii=False),
        True,
    )
    return _f.dumps_result(result)
