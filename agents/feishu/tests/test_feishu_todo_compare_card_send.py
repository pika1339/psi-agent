"""feishu_todo_compare_card_send 结构钉 —— 16:00 对比卡片布局固定,不随 AI 漂移。

2026-09-21 布局改版:GFM 表格(挤成一团、<br> 在卡片表格里不可靠)换成
每成员一个分块(加粗姓名 + 上期/本期/搞定情况 逐条换行);「;」连接的条目拆行;
status 的「承接3」类数字歧义规范成「承接 3 项」。
"""

from __future__ import annotations

import importlib
import json


def _build(mod, **kwargs):
    rows = kwargs.pop("rows", [{"member": "张三", "prev": "1. a;2. b", "curr": "1. a;2. c", "status": "b→搞定;c 新开"}])
    align_notes = kwargs.pop("align_notes", "")
    mentor = kwargs.pop("mentor_name", "孙逊")
    cycle = kwargs.pop("cycle_date", "9.7")
    notes = kwargs.pop("notes", "")
    return mod._build_card_json(mentor, cycle, rows, align_notes, notes)


def test_card_uses_schema2_and_gfm_table() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod)
    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "markdown", "卡片主体是含 GFM 表格的 markdown"
    md = elements[0]["content"]
    assert "| 成员 | 上期 | 本期 | 搞定情况 |" in md
    assert "|---|---|---|---|" in md


def test_card_title_pins_group_and_cycle() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod, mentor_name="孙逊", cycle_date="9.7")
    assert card["header"]["title"]["content"] == "TODO 前后对比 · 孙逊组(9.7期)"


def test_member_block_layout_fixed() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod)
    md = card["body"]["elements"][0]["content"]
    assert "**👤 张三**" in md
    assert "1. a" in md and "2. b" in md and "2. c" in md
    assert "b→搞定" in md and "c 🆕 新开" in md


def test_semicolon_items_break_one_per_line() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod)
    md = card["body"]["elements"][0]["content"]
    assert "1. a<br>2. b" in md, "上期两条目在单元格内以 <br> 换行"
    assert "1. a<br>2. c" in md, "本期两条目在单元格内以 <br> 换行"


def test_status_digit_ambiguity_normalized() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    assert mod._normalize_status("承接3") == "承接 3 项"
    assert mod._normalize_status("新开2") == "新开 2 项"
    assert mod._normalize_status("搞定 1 项") == "搞定 1 项", "已是完整写法的不动"


def test_reminder_notes_and_board_link_order() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod, align_notes="王五|缺对齐依据", notes="上期=9.4、本期=9.7;未填报:无")
    elements = card["body"]["elements"]
    assert elements[0]["content"].startswith("请检查你手下成员的当期填报是否合理:")
    note = elements[-1]
    assert note["tag"] == "div", "说明与链接收进 div(schema 2.0 不支持 note 标签)"
    note_text = note["text"]["content"]
    assert "上期=9.4" in note_text and "看板表: https://" in note_text


def test_notes_semicolon_breaks_per_line() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    card = _build(mod, notes="上期=9.4、本期=9.7;未填报:无;请假免填:无")
    note_text = card["body"]["elements"][-1]["text"]["content"]
    assert "未填报:无" in note_text
    assert "请假免填:无" in note_text


def test_escape_and_split_helpers() -> None:
    mod = importlib.import_module("feishu_todo_compare_card_send")
    assert mod._md_escape("a\\b") == "a\\\\b"
    assert mod._split_items("a; b ;c") == ["a", "b", "c"]
    assert mod._split_items("") == []


def test_tool_rejects_missing_receiver_and_bad_rows() -> None:
    f = importlib.import_module("_feishu_impl")
    err = json.loads(f.dumps_result(f._error("receive_id is required (the mentor's open_id).")))
    assert err["ok"] is False
    err2 = json.loads(f.dumps_result(f._error("rows_json must be valid JSON: x")))
    assert "JSON" in err2["message"]
