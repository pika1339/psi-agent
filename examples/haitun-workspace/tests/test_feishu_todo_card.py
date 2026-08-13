"""Tests for the TODO-list card: initial rendering, tick/untick round handling,
undo-round exhaustion, mentor-ledger status sync, and — the regression this module
exists to catch — that state from an earlier click on one row survives a later click on
a different row (see ``_todo_card_impl``'s module docstring for why that used to break).
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_impl: Any = importlib.import_module("_todo_card_impl")
_api: Any = importlib.import_module("_feishu_api_impl")
_f: Any = importlib.import_module("_feishu_impl")
_send_mod: Any = importlib.import_module("feishu_todo_card")
_tick_mod: Any = importlib.import_module("feishu_todo_card_tick")
_untick_mod: Any = importlib.import_module("feishu_todo_card_untick")


def _find_buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            value = node.get("value")
            if node.get("tag") == "button" and isinstance(value, dict):
                found.append(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(card)
    return found


def _button_for_index(card: dict[str, Any], index: int) -> dict[str, Any]:
    matches = [v for v in _find_buttons(card) if v.get("todo_index") == index]
    assert len(matches) == 1, f"expected exactly one button for row {index}, got {len(matches)}"
    return matches[0]


# -- 初始建卡 -----------------------------------------------------------------


def test_build_card_registers_all_rounds_but_renders_only_round_zero() -> None:
    items = [
        {"title": "写周报", "task_guid": "t0"},
        {"title": "改文档", "task_guid": "t1"},
    ]
    card, handlers = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")

    # 每行预注册 2 * _UNDO_ROUNDS 个 id (tick + untick 各一半), 两行共 4 * _UNDO_ROUNDS。
    assert len(handlers) == 4 * _impl._UNDO_ROUNDS
    for index in range(2):
        for round_ in range(_impl._UNDO_ROUNDS):
            assert handlers[_impl._tick_action_id(index, round_)] == "feishu_todo_card_tick"
            assert handlers[_impl._untick_action_id(index, round_)] == "feishu_todo_card_untick"

    # 但初始卡片只渲染第 0 轮的「标记完成」按钮, 不提前暴露撤销按钮。
    rendered_actions = sorted(v["action"] for v in _find_buttons(card))
    assert rendered_actions == [_impl._tick_action_id(0, 0), _impl._tick_action_id(1, 0)]


def test_build_card_skips_handlers_for_predone_rows() -> None:
    items = [{"title": "已经做完的", "done": True}]
    card, handlers = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    assert handlers == {}
    assert _find_buttons(card) == []
    rendered = json.dumps(card, ensure_ascii=False)
    assert "~~已经做完的~~" in rendered


def test_build_card_embeds_full_card_state_in_every_button() -> None:
    items = [
        {"title": "写周报", "task_guid": "t0", "ledger_record_id": "rec0"},
        {"title": "改文档", "task_guid": "t1", "ledger_record_id": "rec1"},
    ]
    card, _ = _impl._build_todo_card(
        items=items, title="今日 TODO", subtitle="来源说明", shape="circle",
        ledger_app_token="app1", ledger_table_id="tbl1",
    )
    buttons = _find_buttons(card)
    assert len(buttons) == 2
    for button in buttons:
        state = _impl._parse_card_state(button["card_state"])
        assert state is not None
        assert state["title"] == "今日 TODO"
        assert state["subtitle"] == "来源说明"
        assert state["ledger_app_token"] == "app1"
        assert state["ledger_table_id"] == "tbl1"
        assert [row["title"] for row in state["rows"]] == ["写周报", "改文档"]
        assert all(row["done"] is False and row["round"] == 0 for row in state["rows"])


# -- action id 解析 -------------------------------------------------------------


def test_round_of_bare_legacy_id_is_round_zero() -> None:
    assert _impl._round_of("todo_tick_3") == 0


def test_round_of_reads_suffix() -> None:
    assert _impl._round_of("todo_tick_3_r7") == 7
    assert _impl._round_of("todo_untick_3_r19") == 19


# -- card_state 序列化 -----------------------------------------------------------


def test_parse_card_state_rejects_malformed_json() -> None:
    assert _impl._parse_card_state("not json") is None


def test_parse_card_state_rejects_missing_rows() -> None:
    assert _impl._parse_card_state(json.dumps({"title": "x"})) is None


def test_build_card_from_state_round_trips() -> None:
    state = {
        "title": "今日 TODO",
        "subtitle": "",
        "ledger_app_token": "",
        "ledger_table_id": "",
        "rows": [{"title": "写周报", "task_guid": "t0", "detail": "", "shape": "circle", "done": False, "round": 0}],
    }
    card = _impl._build_card_from_state(state)
    assert "进度: 0/1 已完成" in json.dumps(card, ensure_ascii=False)
    button = _find_buttons(card)[0]
    assert button["action"] == _impl._tick_action_id(0, 0)
    round_tripped = _impl._parse_card_state(button["card_state"])
    assert round_tripped == state


# -- _rebuild_row_in_card (legacy fallback splice) ------------------------------


def test_rebuild_row_replaces_only_targeted_slot() -> None:
    card = {
        "elements": [
            {"tag": "markdown", "content": "header"},
            {"tag": "hr"},
            {"tag": "markdown", "content": "row0 original"},
            {"tag": "hr"},
            {"tag": "markdown", "content": "row1 original"},
        ]
    }
    rebuilt = _impl._rebuild_row_in_card(card, 1, [{"tag": "markdown", "content": "row1 replaced"}])
    assert rebuilt is not None
    contents = [el.get("content") for el in rebuilt["elements"] if el.get("tag") == "markdown"]
    assert contents == ["header", "row0 original", "row1 replaced"]


def test_rebuild_row_out_of_range_returns_none() -> None:
    card = {"elements": [{"tag": "markdown", "content": "header"}]}
    assert _impl._rebuild_row_in_card(card, 0, []) is None


# -- 公共测试夹具 ----------------------------------------------------------------


async def _patched_call_api(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_call_api(method: str, uri: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, "uri": uri, **kwargs})
        return {"ok": True, "code": 0}

    monkeypatch.setattr(_api, "call_api_impl", fake_call_api)
    return calls


async def _patched_edit_card(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_edit(message_id: str, card_json: str, user_key: str = "") -> dict[str, Any]:
        calls.append({"message_id": message_id, "card": json.loads(card_json)})
        return {"ok": True}

    monkeypatch.setattr(_f, "edit_card_impl", fake_edit)
    return calls


async def _patched_bitable(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_update(app_token: str, table_id: str, records_json: str, *a: Any, **k: Any) -> dict[str, Any]:
        calls.append({"app_token": app_token, "table_id": table_id, "records": json.loads(records_json)})
        return {"ok": True}

    monkeypatch.setattr(_f, "update_bitable_records_impl", fake_update)
    return calls


def _click_payload(button_value: dict[str, Any], *, message_id: str = "om_1") -> str:
    """Build a ``<feishu_card_action>``-shaped payload for a real (card_state-carrying)
    button value — mirrors what the Channel actually sends, but without a usable ``card``
    field (the whole point of ``card_state`` is not needing it).
    """
    return json.dumps(
        {"message_id": message_id, "card": {}, "action": {"tag": "button", "value": button_value}},
        ensure_ascii=False,
    )


def _legacy_click_payload(
    *, action: str, index: int = 0, title: str = "写周报", card: dict[str, Any], **extra: Any
) -> str:
    """Build a click payload in the OLD per-field value shape (no ``card_state``) — for
    exercising the legacy single-row-splice fallback path only.
    """
    value = {"action": action, "todo_index": index, "todo_title": title, "task_guid": "tg-1", **extra}
    return json.dumps(
        {"message_id": "om_1", "card": card, "action": {"tag": "button", "value": value}},
        ensure_ascii=False,
    )


def _one_row_legacy_card(label: str = "写周报") -> dict[str, Any]:
    return {
        "elements": [
            {"tag": "markdown", "content": "进度: 0/1 已完成"},
            {"tag": "hr"},
            {"tag": "markdown", "content": f"○ **{label}**"},
        ]
    }


# -- tick 工具: card_state 路径（主路径） ----------------------------------------


async def test_tick_uses_card_state_and_rebuilds_whole_card(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0"}, {"title": "改文档", "task_guid": "t1"}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    row0_button = _button_for_index(card, 0)

    result = json.loads(await _tick_mod.feishu_todo_card_tick(_click_payload(row0_button)))
    assert result["card_edit"] == "ok"
    assert result["task_updated"] is True

    rebuilt = edit_calls[0]["card"]
    row0_after = _button_for_index(rebuilt, 0)
    assert row0_after["action"] == _impl._untick_action_id(0, 0)
    row1_after = _button_for_index(rebuilt, 1)
    assert row1_after["action"] == _impl._tick_action_id(1, 0), "未点击的行应保持原状"

    state = _impl._parse_card_state(row0_after["card_state"])
    assert state["rows"][0]["done"] is True
    assert state["rows"][0]["round"] == 0
    assert state["rows"][1]["done"] is False, "row1 的状态没有被 row0 的点击污染"


async def test_multi_row_clicks_do_not_regress_earlier_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact bug this rewrite fixes: ticking row 0 then row 1 must not reset row 0's
    already-applied 撤销 button back to a plain completed-with-no-button state.
    """
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0"}, {"title": "改文档", "task_guid": "t1"}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")

    # 点击行 0。
    row0_button = _button_for_index(card, 0)
    await _tick_mod.feishu_todo_card_tick(_click_payload(row0_button))
    card_after_row0 = edit_calls[0]["card"]

    # 关键: 行 1 的按钮是从「行 0 点击后重建的卡片」里取的 (即最新真相), 不是原始发卡时的。
    row1_button = _button_for_index(card_after_row0, 1)
    await _tick_mod.feishu_todo_card_tick(_click_payload(row1_button))
    card_after_row1 = edit_calls[1]["card"]

    # 行 0 在第二次编辑后仍应保持「已完成 + 撤销按钮」, 没有被行 1 的点击退回原状。
    row0_final = _button_for_index(card_after_row1, 0)
    assert row0_final["action"] == _impl._untick_action_id(0, 0)
    state = _impl._parse_card_state(row0_final["card_state"])
    assert state["rows"][0]["done"] is True
    assert state["rows"][1]["done"] is True


async def test_tick_at_last_round_locks_with_no_button(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0"}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    button = _button_for_index(card, 0)
    last_round_button = {**button, "action": _impl._tick_action_id(0, _impl._UNDO_ROUNDS - 1)}

    await _tick_mod.feishu_todo_card_tick(_click_payload(last_round_button))

    rebuilt = edit_calls[0]["card"]
    assert _find_buttons(rebuilt) == [], "用完所有轮次后应锁定为已完成态, 不再提供按钮"
    rendered = json.dumps(rebuilt, ensure_ascii=False)
    assert "写周报" in rendered and "~~" in rendered, "锁定态仍应是划线完成的展示"


async def test_tick_without_task_guid_reports_no_task_but_still_offers_undo(monkeypatch: pytest.MonkeyPatch) -> None:
    api_calls = await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    items = [{"title": "写周报", "task_guid": ""}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    button = _button_for_index(card, 0)

    result = json.loads(await _tick_mod.feishu_todo_card_tick(_click_payload(button)))

    assert api_calls == [], "没有 task_guid 就不该打任何任务 API"
    assert result["task_updated"] is False
    assert result["reason"] == "row has no task_guid"
    assert result["card_edit"] == "ok"
    assert len(edit_calls) == 1, "即便没有任务, 卡片仍要重绘出撤销入口"


async def test_tick_syncs_ledger_status_when_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    await _patched_edit_card(monkeypatch)
    bitable_calls = await _patched_bitable(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0", "ledger_record_id": "rec123"}]
    card, _ = _impl._build_todo_card(
        items=items, title="今日 TODO", subtitle="", shape="circle",
        ledger_app_token="app1", ledger_table_id="tbl1",
    )
    button = _button_for_index(card, 0)

    await _tick_mod.feishu_todo_card_tick(_click_payload(button))

    assert len(bitable_calls) == 1
    call = bitable_calls[0]
    assert call["app_token"] == "app1"
    assert call["table_id"] == "tbl1"
    assert call["records"] == [{"record_id": "rec123", "fields": {"状态": "已交付"}}]


async def test_tick_skips_ledger_sync_when_not_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    await _patched_edit_card(monkeypatch)
    bitable_calls = await _patched_bitable(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0"}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    button = _button_for_index(card, 0)

    await _tick_mod.feishu_todo_card_tick(_click_payload(button))

    assert bitable_calls == []


# -- untick 工具: card_state 路径 -------------------------------------------------


async def test_untick_uses_card_state_and_offers_next_round_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    api_calls = await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0"}]
    card, _ = _impl._build_todo_card(items=items, title="今日 TODO", subtitle="", shape="circle")
    tick_button = _button_for_index(card, 0)
    await _tick_mod.feishu_todo_card_tick(_click_payload(tick_button))
    card_after_tick = edit_calls[0]["card"]
    untick_button = _button_for_index(card_after_tick, 0)
    assert untick_button["action"] == _impl._untick_action_id(0, 0)

    result = json.loads(await _untick_mod.feishu_todo_card_untick(_click_payload(untick_button)))
    assert result["task_updated"] is True
    body = json.loads(api_calls[1]["body_json"])
    assert body["task"]["completed_at"] == "0", "重开任务是把 completed_at 写回字符串 0"
    assert body["update_fields"] == ["completed_at"]

    rebuilt = edit_calls[1]["card"]
    row0_after = _button_for_index(rebuilt, 0)
    assert row0_after["action"] == _impl._tick_action_id(0, 1), "撤销第 0 轮后, 下一次可勾的是第 1 轮"
    rendered = json.dumps(rebuilt, ensure_ascii=False)
    assert "~~写周报~~" not in rendered, "撤销后应恢复成未完成态的展示"


async def test_untick_at_final_round_locks_open_with_no_button(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive boundary check: the UI never actually offers an untick button whose
    round equals ``_UNDO_ROUNDS - 1`` (a tick at that round locks with no untick button —
    see ``test_tick_at_last_round_locks_with_no_button``), but if
    ``feishu_todo_card_untick`` were ever handed one anyway, it must lock rather than
    offer a round-``_UNDO_ROUNDS`` tick button that has no registered handler.
    """
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    last_round = _impl._UNDO_ROUNDS - 1
    state = {
        "title": "今日 TODO",
        "subtitle": "",
        "ledger_app_token": "",
        "ledger_table_id": "",
        "rows": [
            {"title": "写周报", "task_guid": "t0", "detail": "", "shape": "circle", "done": True, "round": last_round}
        ],
    }
    untick_button = {
        "action": _impl._untick_action_id(0, last_round),
        "todo_index": 0,
        "todo_title": "写周报",
        "task_guid": "t0",
        "card_state": _impl._serialize_card_state(state),
    }

    await _untick_mod.feishu_todo_card_untick(_click_payload(untick_button))

    assert _find_buttons(edit_calls[0]["card"]) == []


async def test_untick_reverts_ledger_status_when_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    await _patched_edit_card(monkeypatch)
    bitable_calls = await _patched_bitable(monkeypatch)

    items = [{"title": "写周报", "task_guid": "t0", "ledger_record_id": "rec123"}]
    card, _ = _impl._build_todo_card(
        items=items, title="今日 TODO", subtitle="", shape="circle",
        ledger_app_token="app1", ledger_table_id="tbl1",
    )
    tick_button = _button_for_index(card, 0)
    payload = _click_payload(tick_button)
    edit_calls = await _patched_edit_card(monkeypatch)  # re-patch to reset the list
    await _tick_mod.feishu_todo_card_tick(payload)
    untick_button = _button_for_index(edit_calls[0]["card"], 0)

    bitable_calls.clear()
    await _untick_mod.feishu_todo_card_untick(_click_payload(untick_button))

    assert bitable_calls == [
        {"app_token": "app1", "table_id": "tbl1", "records": [{"record_id": "rec123", "fields": {"状态": "待开始"}}]}
    ]


# -- 兼容旧卡: 无 card_state 时退回单行拼接 ----------------------------------------


async def test_tick_legacy_bare_id_without_card_state_uses_splice_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    api_calls = await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    payload = _legacy_click_payload(action="todo_tick_0", card=_one_row_legacy_card())
    result = json.loads(await _tick_mod.feishu_todo_card_tick(payload))

    assert result["ok"] is True
    assert result["task_updated"] is True
    assert result["card_edit"] == "ok"
    assert api_calls[0]["paths_json"] == json.dumps({"task_guid": "tg-1"}, ensure_ascii=False)

    rendered_actions = [v["action"] for v in _find_buttons(edit_calls[0]["card"])]
    assert rendered_actions == [_impl._untick_action_id(0, 0)], "旧式无轮次 id 按第 0 轮处理, 撤销按钮应为 r0"
    # 旧路径重建的按钮不带 card_state (它本来就没有全卡状态可用)。
    assert "card_state" not in _find_buttons(edit_calls[0]["card"])[0]


async def test_tick_missing_card_and_state_reports_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    value = {"action": "todo_tick_0", "todo_index": 0, "todo_title": "写周报", "task_guid": "tg-1"}
    payload = json.dumps(
        {"message_id": "om_1", "card": None, "action": {"tag": "button", "value": value}},
        ensure_ascii=False,
    )
    result = json.loads(await _tick_mod.feishu_todo_card_tick(payload))

    assert result["card_edit"] == "skipped_missing_card_in_payload"
    assert edit_calls == []


async def test_tick_legacy_card_with_no_matching_row_reports_slot_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    await _patched_call_api(monkeypatch)
    edit_calls = await _patched_edit_card(monkeypatch)

    value = {"action": "todo_tick_0", "todo_index": 0, "todo_title": "写周报", "task_guid": "tg-1"}
    payload = json.dumps(
        {"message_id": "om_1", "card": {}, "action": {"tag": "button", "value": value}},
        ensure_ascii=False,
    )
    result = json.loads(await _tick_mod.feishu_todo_card_tick(payload))

    assert result["card_edit"] == "skipped_row_slot_not_found"
    assert edit_calls == []

