"""Handle an undo on a TODO-list card: reopen the linked Feishu task.

The counterpart to ``feishu_todo_card_tick``. Dispatched by a done row's 「撤销」button
(only present when that row has rounds left, see ``_todo_card_impl._UNDO_ROUNDS``).
Reopens the Feishu task, reverts the mentor-ledger 状态 if the row carries one, and edits
the card back to an open, tickable row for the next round — via
``_todo_card_impl._apply_row_transition`` (see that module's docstring for why the whole
card is rebuilt from an embedded ``card_state`` blob rather than splicing Feishu's own
click-time card snapshot).
"""

from __future__ import annotations

import json
from typing import Any

import _feishu_api_impl as _api
import _todo_card_impl as _impl


async def feishu_todo_card_untick(card_action_json: str = "", user_key: str = "") -> str:
    """Reopen the Feishu task behind one un-ticked TODO row, and offer to tick it again.

    The handler for a done TODO card row's 「撤销」button (``feishu_todo_card_send`` /
    ``feishu_todo_card_tick``). Session injects the ``<feishu_card_action>`` payload as
    ``card_action_json``; the clicked row's ``task_guid``, title, and round come from its
    ``value`` — same shape as ``feishu_todo_card_tick``, just the opposite direction.

    Reopening is written as ``task.completed_at`` = the string ``"0"`` with
    ``update_fields: ["completed_at"]`` — this is Feishu's documented way to reopen a task
    (see the ``feishu-task`` skill). A row carrying no ``task_guid`` is reported as
    un-ticked-only, since there is no task to move; that is not an error. If the row is
    wired to a mentor ledger, that record's 状态 field is reverted to 待开始, matching what
    a never-ticked row would show.

    The Channel already gave the clicked 「撤销」button an instant generic "consumed" look
    before this runs (it does not know this action means "go back to open" — that is
    exactly why this tool exists). This tool edits the card a second time
    (``feishu_message_edit_card``, via ``_todo_card_impl._apply_row_transition``) to
    restore the row to its open state with a fresh「标记完成」button for the next round —
    unless this was already the last round, in which case the row stays locked with no
    button (should not normally happen: the「撤销」button is only ever offered when a
    round remains, see ``feishu_todo_card_tick``).

    Args:
        card_action_json: The ``<feishu_card_action>`` JSON (injected by Session).
        user_key: The clicker's open_id. Pass it so the task is reopened as that person
            when the bot's own token is not a task member.
    """
    payload, error = _impl._parse_action(card_action_json)
    if payload is None:
        return error
    value = _impl._action_value(payload)
    task_guid = str(value.get("task_guid") or "").strip()
    title = str(value.get("todo_title") or "").strip() or "该待办"
    index_raw = value.get("todo_index")
    index = int(index_raw) if isinstance(index_raw, int) else 0
    action_id = str(value.get("action") or "")
    round_ = _impl._round_of(action_id)

    task_updated = False
    task_result: dict[str, Any] | None = None
    if task_guid:
        task_result = await _api.call_api_impl(
            "PATCH",
            "/open-apis/task/v2/tasks/:task_guid",
            body_json=json.dumps(
                {"task": {"completed_at": "0"}, "update_fields": ["completed_at"]},
                ensure_ascii=False,
            ),
            paths_json=json.dumps({"task_guid": task_guid}, ensure_ascii=False),
            prefer="tenant",
            user_key=user_key,
        )
        task_updated = bool(task_result.get("ok"))

    await _impl._sync_ledger_status(value, "待开始")

    message_id = str(payload.get("message_id") or "")
    card_edit_status = "skipped_no_message_id"
    if message_id:
        card_edit_status = await _impl._apply_row_transition(
            message_id=message_id,
            payload=payload,
            value=value,
            index=index,
            new_done=False,
            new_round=round_ + 1,
            fallback_title=title,
            fallback_task_guid=task_guid,
            user_key=user_key,
        )

    result = (
        task_result
        if task_result is not None
        else {"ok": True, "unticked": True, "task_updated": False, "reason": "row has no task_guid", "title": title}
    )
    result = {**result, "task_updated": task_updated, "card_edit": card_edit_status}
    return json.dumps(result, ensure_ascii=False, default=str)
