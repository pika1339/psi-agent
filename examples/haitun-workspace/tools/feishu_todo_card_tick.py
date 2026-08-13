"""Handle a tick on a TODO-list card: mark the linked Feishu task complete.

Dispatched by the card's ``action_handlers`` map, one row at a time. The Channel applies
a generic visual placeholder immediately (per-row consumption, other rows stay live) —
this tool moves the *authoritative* state (the Feishu task, and optionally the mentor
ledger), then edits the card in place to replace that placeholder with the row's real
done state plus a 「撤销」button for the next round, so a misclick is recoverable.

The card edit is done via ``_todo_card_impl._apply_row_transition``, which rebuilds the
**whole card** from a self-contained ``card_state`` blob embedded in the clicked button
(see that module's docstring for why: Feishu's own per-click card snapshot silently goes
stale after the first click on a multi-row card, so trusting it to splice just one row
would regress earlier rows' buttons back to a generic placeholder). Cards sent before
that existed fall back to a legacy single-row splice with no such protection.

See ``_todo_card_impl._UNDO_ROUNDS`` for why undo is a bounded number of rounds rather
than truly unlimited — a Feishu card action id is single-use forever once clicked, so
every round's tick/untick id had to be pre-registered at send time.
"""

from __future__ import annotations

import json
import time
from typing import Any

import _feishu_api_impl as _api
import _todo_card_impl as _impl


async def feishu_todo_card_tick(card_action_json: str = "", user_key: str = "") -> str:
    """Mark the Feishu task behind one ticked TODO row complete, and offer an undo.

    The handler for a TODO card row's 「标记完成」button (``feishu_todo_card_send``).
    Session injects the ``<feishu_card_action>`` payload as ``card_action_json``; the
    clicked row's ``task_guid``, title, and round come from its ``value``.

    Completion is written as ``task.completed_at`` = now in **milliseconds** with
    ``update_fields: ["completed_at"]`` — without ``update_fields`` Feishu returns success
    and changes nothing. A row carrying no ``task_guid`` is reported as ticked-only, since
    there is no task to move; that is not an error. If the row is wired to a mentor ledger
    (``ledger_record_id`` plus the card's ``ledger_app_token``/``ledger_table_id``), that
    record's 状态 field is also written to 已交付, so the Bitable does not drift from the
    card. Retrying a tick on an already-completed task (Feishu error 1470400) is reported
    as ``task_updated: false`` with the underlying error, not silently swallowed.

    The Channel already gave the row an instant generic "consumed" look before this runs.
    This tool then edits the card a second time (``feishu_message_edit_card``, via
    ``_todo_card_impl._apply_row_transition``) to replace that with the real
    struck-through row **and a 「撤销」button** for the next round — unless this row has
    used up all ``_todo_card_impl._UNDO_ROUNDS``, in which case it locks in as done with
    no further button. Do not announce the click in chat; reply only if the task update
    failed.

    Fast clicking is coalesced by the Channel: if the payload arrives wrapped in
    ``<feishu_card_action_batch>``, call this tool once per ``<feishu_card_action>`` inside
    it (skipping one silently loses that task's completion), then send at most one summary
    reply for the whole batch.

    Args:
        card_action_json: The ``<feishu_card_action>`` JSON (injected by Session).
        user_key: The clicker's open_id. Pass it so the task is completed as that person
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
                {"task": {"completed_at": str(int(time.time() * 1000))}, "update_fields": ["completed_at"]},
                ensure_ascii=False,
            ),
            paths_json=json.dumps({"task_guid": task_guid}, ensure_ascii=False),
            prefer="tenant",
            user_key=user_key,
        )
        task_updated = bool(task_result.get("ok"))

    await _impl._sync_ledger_status(value, "已交付")

    message_id = str(payload.get("message_id") or "")
    card_edit_status = "skipped_no_message_id"
    if message_id:
        card_edit_status = await _impl._apply_row_transition(
            message_id=message_id,
            payload=payload,
            value=value,
            index=index,
            new_done=True,
            new_round=round_,
            fallback_title=title,
            fallback_task_guid=task_guid,
            user_key=user_key,
        )

    result = (
        task_result
        if task_result is not None
        else {"ok": True, "ticked": True, "task_updated": False, "reason": "row has no task_guid", "title": title}
    )
    result = {**result, "task_updated": task_updated, "card_edit": card_edit_status}
    return json.dumps(result, ensure_ascii=False, default=str)
