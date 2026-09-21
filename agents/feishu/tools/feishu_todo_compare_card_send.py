"""Feishu TODO compare card — fixed-layout cycle compare card for the 16:00 push.

The 16:00 dynamic-ledger push delivers a per-mentor before/after comparison of this
cycle's TODOs. The card shape must NOT drift between runs (an LLM assembling card
JSON inline reorders columns or drops the board link occasionally), so this tool
owns the layout: caller passes rows + a few labels, the code builds the exact card
JSON (schema 2.0, one markdown element) and sends it.

Layout: one block per member (bold name + 「上期/本期/搞定情况」 lines), items
joined with 「;」 are split one line each — GFM tables render cramped on the card
and <br> inside cells is unreliable, so the table was replaced by this block form
(2026-09-21, readability complaint: text was jammed with no line breaks and
mentors stopped reading the card in favour of the raw board).
Below the blocks sit, in order: an optional notes line (period/definition/未填报名单
等说明, 「;」 also split per line), the alignment-pending notes, and the board link.
"""

from __future__ import annotations

import json
import re

import _feishu_impl as _f

_BOARD_LINK = "https://genuineknowledge.feishu.cn/wiki/H6icwLWn1iwpXAk73QMcA6MgnWc"
_REMINDER_LINE = "请检查你手下成员的当期填报是否合理:"

# 卡片布局契约:每成员一个分块(加粗姓名 + 上期/本期/搞定情况),提醒句/说明/存疑/链接,顺序不漂移。
_STATUS_DIGIT_RE = re.compile(r"(承接|新开|搞定|消失待确认)(\d+)$")

# 状态类别 → 图标(渲染层装饰,类别词本身由调用方产出,顺序匹配长的先替换)。
_STATUS_ICONS = [
    ("已验收完成", "✅"),
    ("疑似完成", "❓"),
    ("疑似承接", "❓"),
    ("承接", "🔄"),
    ("新开", "🆕"),
    ("请假免填", "🏖️"),
]


def _md_escape(value: str) -> str:
    """Escape markdown for card body text (no table context)."""
    return (value or "").replace("\\", "\\\\").replace("\r", "")


def _split_items(value: str) -> list[str]:
    """Split a 「;」-joined cell into one item per line."""
    return [s.strip() for s in str(value or "").split(";") if s.strip()]


def _normalize_status(status: str) -> str:
    """「承接3」 reads as “the 3rd item carries over”; rewrite as 「承接 3 项」.

    Status may join several verdicts with 「;」 (e.g. 承接2;新开1); normalize each.
    """
    return ";".join(
        _STATUS_DIGIT_RE.sub(lambda m: f"{m.group(1)} {m.group(2)} 项", seg.strip())
        for seg in str(status or "").split(";")
        if seg.strip()
    )


def _decorate_status(status: str) -> str:
    """Prefix each verdict class with an icon (rendering-only decoration)."""
    for word, icon in _STATUS_ICONS:
        status = status.replace(word, f"{icon} {word}")
    return status


def _md_cell(value: str) -> str:
    """Escape one GFM table cell; multi-item strings are split on 「;」 into <br> lines.

    v1.0 表格挤成一团的根因:调用方传的 prev/curr 是「;」串,单元格内没有换行
    标记;这里显式拆成 <br>,表格单元格内即可逐条换行。
    """
    items = _split_items(value)
    body = "<br>".join(
        _decorate_status(_normalize_status(item)) if _is_status_cell(item) else _md_escape(item)
        for item in items
    )
    return body.replace("|", "\\|").replace("\r", "")


def _is_status_cell(item: str) -> bool:
    return any(word in item for word, _ in _STATUS_ICONS)


def _field(text_content: str, is_short: bool) -> dict:
    return {"is_short": is_short, "text": {"tag": "lark_md", "content": text_content}}


def _build_card_json(mentor_name: str, cycle_date: str, rows: list[dict], align_notes: str, notes: str = "") -> dict:
    # 表格布局:成员 | 上期 | 本期 | 搞定情况 四列;单元格内条目按 <br> 逐条换行;
    # 说明与看板链接收进表格下方的 note(灰色小字)。
    table_lines = [
        "| 成员 | 上期 | 本期 | 搞定情况 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        member = _md_escape(str(row.get("member", "")).strip())
        prev = _md_cell(str(row.get("prev", "")))
        curr = _md_cell(str(row.get("curr", "")))
        status = _md_cell(str(row.get("status", "")))
        table_lines.append(f"| **👤 {member}** | {prev} | {curr} | {status} |")
    markdown = _REMINDER_LINE + "\n\n" + "\n".join(table_lines)

    elements: list[dict] = [{"tag": "markdown", "content": markdown}]
    # 飞书卡片 schema 2.0 不支持 note 标签(实测 ErrCode 200861 unsupported tag)——
    # 说明与看板链接用 div(lark_md)承载,样式与正文区分交给内容措辞。
    note_lines: list[str] = []
    if notes.strip():
        note_lines.append(_md_escape(notes.strip()))
    if align_notes.strip():
        note_lines.append(_md_escape(align_notes.strip()))
    note_lines.append(f"看板表: {_BOARD_LINK}")
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(note_lines)}})

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"TODO 前后对比 · {mentor_name}组({cycle_date}期)"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


async def feishu_todo_compare_card_send(
    receive_id: str,
    mentor_name: str,
    cycle_date: str,
    rows_json: str,
    receive_id_type: str = "open_id",
    align_notes: str = "",
    notes: str = "",
    user_key: str = "",
) -> str:
    """Send the fixed-layout cycle compare card to one mentor.

    Args:
        receive_id: Recipient id (mentor's open_id for private chat).
        mentor_name: Mentor display name (card title: ``TODO 前后对比 · <mentor>组(<date>期)``).
        cycle_date: Cycle date string used in the title.
        rows_json: JSON array of row objects, one per member:
            ``{"member": "张三", "prev": "上期条目简写", "curr": "本期条目简写", "status": "搞定情况"}``.
            prev/curr may join multiple items with 「;」; status uses 搞定/进行中/新开/消失待确认/请假顺延.
        receive_id_type: ``open_id`` (private) / ``chat_id`` (group); auto-corrected on send.
        align_notes: Optional alignment-pending notes (from align-pending.txt), rendered
            as one 「**对齐存疑**」 line below the table; empty = omitted.
        notes: Optional explanation line rendered as 「说明:...」 right below the table —
            period dates (上期=9.4、本期=9.7), 搞定定义, 未填报名单, 回流计数, and the
            mobile hint 「手机上点表格行可展开查看详情」. Empty = omitted.
        user_key: Identity for the send (usual convention); omitted uses the bot.
    """
    if not receive_id.strip():
        return _f.dumps_result(_f._error("receive_id is required (the mentor's open_id)."))
    if not mentor_name.strip():
        return _f.dumps_result(_f._error("mentor_name is required."))
    try:
        rows = json.loads(rows_json)
    except json.JSONDecodeError as exc:
        return _f.dumps_result(_f._error(f"rows_json must be valid JSON: {exc}"))
    if not isinstance(rows, list):
        return _f.dumps_result(_f._error("rows_json must be a JSON array of row objects."))

    card = _build_card_json(mentor_name.strip(), cycle_date.strip(), rows, align_notes, notes)
    outcome = await _f.send_card_impl(
        receive_id=receive_id.strip(),
        card_json=json.dumps(card, ensure_ascii=False),
        receive_id_type=receive_id_type,
        user_key=user_key,
    )
    return json.dumps(outcome, ensure_ascii=False, default=str)
