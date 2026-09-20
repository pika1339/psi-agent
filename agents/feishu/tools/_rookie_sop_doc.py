"""把 SOP 清单渲染成飞书文档的块, 以及把文档块读回成勾选状态 —— 纯逻辑, 不碰飞书。

2.0 起渲染支持新模板: 头部静态区(欢迎词/伙伴特质)、模块内 H3 小组标题、
验收人 mention、acceptance 里的 @人 与链接片段、尾部 FAQ 问答区、流程图图片块。
数据仍然以明细表为唯一事实来源: 文档只是新人勾选的界面, 勾完由
rookie_sop_sync_doc 把 done 状态同步回表, 所以 HR 日报的数据源完全不用改。
"""

from __future__ import annotations

import re

# ruff: noqa: RUF001
from datetime import date
from typing import Any

import _rookie_sop_config as _cfg

# 飞书文档块类型(见 _feishu_impl 的块类型表)
BLOCK_TEXT = 2
BLOCK_HEADING2 = 4
BLOCK_HEADING3 = 5
BLOCK_HEADING4 = 6
BLOCK_TODO = 17
BLOCK_DIVIDER = 22
BLOCK_IMAGE = 27

# 普通条目只有一个 todo, 勾上即完成。
ROLE_DONE = "done"
# 分节小计块(「到岗准备 3/5」那一行)的角色标记, item_id 位置放模块名
ROLE_TALLY = "tally"
# 流程图图片块 —— 不参与勾选, 只占 slots 一位保证配对不串位
ROLE_IMAGE = "image"
# 超链接后面的统一备注 —— 加粗蓝字在飞书里未必被认成「可点」, 明说比指望领会可靠
LINK_NOTE = "（必读材料，请点击超链接打开阅读）"
# 阅读类条目拆成两个理解勾选(见 build_doc_blocks), 各有自己的角色;
# 「已阅读」已去掉 —— 读没读不重要, 重要的是懂没懂。
ROLE_GOT_IT = "ok"
ROLE_UNCLEAR = "unclear"
# 角色选择那一项的两个框。与 config/rookie_sop.yaml 里的 id 一致。
ROLE_ITEM_ID = "role_confirmed"
ROLE_IS_DEV = "isdev"
ROLE_IS_NONDEV = "isnondev"

_MODULE_EMOJI = {
    "到岗准备": "🏢",
    "必读材料": "📚",
    "搞清楚谁是谁": "🤝",
    "每天怎么干活": "📅",
    "制度知晓": "📋",
    "开发环境": "💻",
}


def _text_run(content: str, bold: bool = False, grey: bool = False) -> dict[str, Any]:
    style: dict[str, Any] = {}
    if bold:
        style["bold"] = True
    if grey:
        style["text_color"] = 5  # 飞书字色枚举: 5 = 灰
    return {"text_run": {"content": content, "text_element_style": style}}


def _linked_run(content: str, url: str, bold: bool = False) -> dict[str, Any]:
    """超链接挂在**原文字**上, 不单独占一行罗列 URL —— 排版更干净。"""
    style: dict[str, Any] = {"link": {"url": url}}
    if bold:
        style["bold"] = True
    return {"text_run": {"content": content, "text_element_style": style}}


def _mention_element(open_id: str) -> dict[str, Any]:
    """文档内 @人 元素。mention_user 与 text_run 平级, 不是 run 的子字段。"""
    return {"mention_user": {"user_id": open_id}}


def render_runs(
    text: str,
    *,
    mentions: dict[str, Any] | None = None,
    links: tuple[tuple[str, str], ...] = (),
    bold: bool = False,
    grey: bool = False,
    rich: bool = False,
) -> list[dict[str, Any]]:
    """把一行文字切成飞书 elements: @名字 → mention_user, links 片段 → 链接 run。

    先按链接片段切分(长片段优先, 防止短片段先命中), 再在每段普通文字里按
    @名字 切出 mention。没配映射的 @名字(如 @小组长)保持纯文本。
    """
    mentions = mentions or {}
    # 链接片段切分
    segments: list[tuple[str, str]] = [(text, "")]
    for frag, url in sorted(links, key=lambda x: -len(x[0])):
        if not frag:
            continue
        expanded: list[tuple[str, str]] = []
        for seg, seg_url in segments:
            if seg_url:
                expanded.append((seg, seg_url))
                continue
            parts = seg.split(frag)
            for j, part in enumerate(parts):
                if part:
                    expanded.append((part, ""))
                if j < len(parts) - 1:
                    expanded.append((frag, url))
        segments = expanded

    names = sorted(mentions.keys(), key=len, reverse=True)
    runs: list[dict[str, Any]] = []
    for seg, seg_url in segments:
        if seg_url:
            runs.append(_linked_run(seg, seg_url, bold=bold))
            continue
        rest = seg
        while rest:
            hit_at = -1
            hit_name = ""
            for name in names:
                pos = rest.find(f"@{name}")
                if pos >= 0 and (hit_at < 0 or pos < hit_at):
                    hit_at, hit_name = pos, name
            if hit_at < 0:
                if rich:
                    runs.extend(_rich_runs(rest, bold=bold, grey=grey))
                else:
                    runs.append(_text_run(rest, bold=bold, grey=grey))
                break
            if hit_at > 0:
                if rich:
                    runs.extend(_rich_runs(rest[:hit_at], bold=bold, grey=grey))
                else:
                    runs.append(_text_run(rest[:hit_at], bold=bold, grey=grey))
            runs.append(_mention_element(str(mentions[hit_name])))
            rest = rest[hit_at + len(hit_name) + 1 :]
    if not runs:
        if rich:
            runs.extend(_rich_runs(text, bold=bold, grey=grey))
        else:
            runs.append(_text_run(text, bold=bold, grey=grey))
    return runs


def _rich_runs(text: str, *, bold: bool = False, grey: bool = False) -> list[dict[str, Any]]:
    """FAQ 富文本行内标记: **加粗** / ##背景高亮## / ~~灰色~~。

    普通条目不用这套(rich 只对 FAQ 开启), 标记本身不会出现在正文里。
    """
    parts = re.split(r"(\*\*.+?\*\*|##.+?##|~~.+?~~)", text)
    runs: list[dict[str, Any]] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            runs.append(_text_run(part[2:-2], bold=True, grey=grey))
        elif part.startswith("##") and part.endswith("##"):
            runs.append(_highlight_run(part[2:-2]))
        elif part.startswith("~~") and part.endswith("~~"):
            runs.append(_text_run(part[2:-2], bold=bold, grey=True))
        else:
            runs.append(_text_run(part, bold=bold, grey=grey))
    return runs


def _highlight_run(content: str) -> dict[str, Any]:
    """背景高亮 run —— 与模板一致(background_color 4 = 黄)。"""
    return {
        "text_run": {
            "content": content,
            "text_element_style": {"background_color": 4},
        }
    }


def _item_id_of(row: dict[str, Any]) -> str:
    key = str(row.get("记录键") or "")
    return key.rsplit(":", 1)[-1] if ":" in key else key


def _todo(elements: list[dict[str, Any]], done: bool) -> dict[str, Any]:
    return {"block_type": BLOCK_TODO, "todo": {"elements": elements, "style": {"done": done}}}


def _group_heading(group: dict[str, Any]) -> dict[str, Any]:
    emoji = str(group.get("emoji") or "").strip()
    name = str(group.get("name") or "").strip()
    content = f"{emoji} {name}" if emoji else name
    return {
        "block_type": BLOCK_HEADING3,
        "heading3": {"elements": [_text_run(content)], "style": {}},
    }


def _module_heading(
    module: str,
    *,
    emoji: str,
    checker: str,
    mentions: dict[str, Any],
    done_n: int,
    total: int,
) -> dict[str, Any]:
    """模块标题: emoji + 名称 + 进度角标 + 验收人(mention)。

    角标固定在 elements 第 2 位 —— 同步时的 update_tallies 靠位置换它,
    所以「验收人」runs 必须排在角标后面, 换角标时才不会把验收人冲掉。
    """
    title = f"{emoji} {module}" if emoji else module
    elements: list[dict[str, Any]] = [
        _text_run(title),
        _text_run(f"　{done_n}/{total}", grey=True),
    ]
    if checker:
        elements.append(_text_run("   验收人"))
        if str(mentions.get(checker) or "").strip():
            elements.append(_mention_element(str(mentions[checker])))
        else:
            elements.append(_text_run(f"@{checker}"))
    return {"block_type": BLOCK_HEADING2, "heading2": {"elements": elements, "style": {}}}


def module_emoji(module: str) -> str:
    return _MODULE_EMOJI.get(module, "▸")


def _rows_by(rows: list[dict[str, Any]], module: str, group: str = "") -> list[dict[str, Any]]:
    out = [r for r in rows if str(r.get("模块") or "") == module]
    if group:
        out = [r for r in out if str(r.get("组") or "") == group]
    return out


def _render_header(
    cfg: dict[str, Any], rows: list[dict[str, Any]], blocks: list[dict[str, Any]], slots: list[tuple[str, str]]
) -> None:
    """文档头部: 欢迎词/说明行/伙伴特质 3 条/完整 SOP 链接。"""
    header = cfg.get("header") or {}
    intro = header.get("intro")
    if isinstance(intro, list):
        for line in intro:
            if not isinstance(line, dict):
                continue
            text = str(line.get("text") or "")
            if not text:
                continue
            if bool(line.get("heading")):
                # 欢迎词这类引导行: 与模板一致渲染成 H2(加粗)。
                # 占一个 tally slot —— provision_doc 按位配对会筛中 H2;
                # update_tallies 反查不到 "__welcome__" 的明细行会直接跳过, 无害。
                blocks.append(
                    {
                        "block_type": BLOCK_HEADING2,
                        "heading2": {"elements": [_text_run(text, bold=True)], "style": {}},
                    }
                )
                slots.append(("__welcome__", ROLE_TALLY))
            else:
                blocks.append(
                    {
                        "block_type": BLOCK_TEXT,
                        "text": {
                            "elements": [_text_run(text, bold=bool(line.get("bold")), grey=bool(line.get("grey")))],
                            "style": {},
                        },
                    }
                )
    if header.get("sop_link"):
        sop_url = str(cfg.get("sop_doc_url") or "").strip()
        if sop_url:
            blocks.append(
                {
                    "block_type": BLOCK_TEXT,
                    "text": {
                        "elements": [
                            _linked_run("📖 完整新人入职 SOP 原文", sop_url, bold=True),
                            _text_run(LINK_NOTE, grey=True),
                        ],
                        "style": {},
                    },
                }
            )
    partner_rows = _rows_by(rows, _cfg.PARTNERS_MODULE)
    if partner_rows:
        # 与模板一致: 「🟡 我们喜欢的伙伴　0/3」是第一个模块标题, 伙伴 3 条跟在下面
        done_n = sum(1 for r in partner_rows if str(r.get("状态") or "") == "已完成")
        emoji = str(header.get("partners_emoji") or "").strip()
        checker = str(header.get("partners_checker") or "").strip()
        heading_elements = [
            _text_run(f"{emoji} {_cfg.PARTNERS_MODULE}" if emoji else _cfg.PARTNERS_MODULE),
            _text_run(f"　{done_n}/{len(partner_rows)}", grey=True),
        ]
        if checker:
            heading_elements.append(_text_run("   验收人"))
            if str((cfg.get("mentions") or {}).get(checker) or "").strip():
                heading_elements.append(_mention_element(str(cfg["mentions"][checker])))
            else:
                heading_elements.append(_text_run(f"@{checker}"))
        blocks.append(
            {
                "block_type": BLOCK_HEADING2,
                "heading2": {"elements": heading_elements, "style": {}},
            }
        )
        slots.append((_cfg.PARTNERS_MODULE, ROLE_TALLY))
        for row in partner_rows:
            partner_title = str(row.get("项") or "").strip()
        partner_done = str(row.get("状态") or "") == "已完成"
        blocks.append(_todo([_text_run(partner_title, bold=True)], partner_done))
        slots.append((_item_id_of(row), ROLE_DONE))


def _render_modules(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    slots: list[tuple[str, str]],
    layout: dict[str, Any],
    mentions: dict[str, Any],
) -> None:
    """按 layout 顺序渲染各模块; layout 为空(旧 config)时按 rows 聚合顺序渲染。

    组内条目的渲染顺序以 config 声明为准(明细行没有组字段, 靠 item_id 对回 config)。
    """
    sop_items = _cfg.load_sop(cfg)
    # 按 config 顺序取每个 (模块, 组) 的条目列表 —— 保持声明顺序, 避免依赖明细行顺序
    by_mod_group: dict[tuple[str, str], list[Any]] = {}
    for i in sop_items:
        if i.module == _cfg.PARTNERS_MODULE:
            continue
        by_mod_group.setdefault((i.module, i.group), []).append(i)

    layout_modules = layout.get("modules") or []
    ordered: list[dict[str, Any]] = []
    if layout_modules:
        for lm in layout_modules:
            name = str(lm.get("name") or "")
            if any(i.module == name for i in sop_items):
                ordered.append(lm)
    else:
        seen: list[str] = []
        for row in rows:
            module = str(row.get("模块") or "")
            if module and module not in seen and module != _cfg.PARTNERS_MODULE:
                seen.append(module)
        ordered = [{"name": m, "emoji": "", "checker": "", "flowchart": False} for m in seen]

    row_by_item = {_item_id_of(r): r for r in rows}

    # 无 config 条目(旧调用路径, 如 provision_doc 不传 cfg): 退化为按 rows 顺序渲染,
    # 与 V3 行为一致 —— 否则条目会因 config 为空而整个不渲染(实测踩过)。
    fallback = not by_mod_group

    for lm in ordered:
        module = str(lm.get("name") or "")
        module_rows = [r for r in rows if str(r.get("模块") or "") == module]
        done_n = sum(1 for r in module_rows if str(r.get("状态") or "") == "已完成")
        blocks.append({"block_type": BLOCK_DIVIDER, "divider": {}})
        blocks.append(
            _module_heading(
                module,
                emoji=str(lm.get("emoji") or ""),
                checker=str(lm.get("checker") or ""),
                mentions=mentions,
                done_n=done_n,
                total=len(module_rows),
            )
        )
        slots.append((module, ROLE_TALLY))

        if fallback:
            for row in module_rows:
                _render_item_fallback(row, blocks, slots)
            continue

        groups = lm.get("groups")
        if isinstance(groups, list) and groups:
            for group in groups:
                gname = str(group.get("name") or "")
                blocks.append(_group_heading(group))
                for item in by_mod_group.get((module, gname), []):
                    row = row_by_item.get(item.item_id)
                    if row is not None:
                        _render_item(cfg, item, row, blocks, slots)
        else:
            for item in by_mod_group.get((module, ""), []):
                row = row_by_item.get(item.item_id)
                if row is not None:
                    _render_item(cfg, item, row, blocks, slots)

        if lm.get("flowchart"):
            # 飞书 API 画不了真流程图(流程图块是空画布), 用图片块占位 ——
            # provision_doc 配对到它之后上传 PNG 并 replace_image。
            blocks.append({"block_type": BLOCK_IMAGE, "image": {"token": ""}})
            slots.append((f"flowchart:{module}", ROLE_IMAGE))


def _render_item_fallback(row: dict[str, Any], blocks: list[dict[str, Any]], slots: list[tuple[str, str]]) -> None:
    """无 config 时的条目渲染 —— 与 V3 行为一致: 普通 todo / 必读链接+两个勾选 /
    不适用标记 / 角色互选框。"""
    item_id = _item_id_of(row)
    title = str(row.get("项") or "").strip()
    acceptance = str(row.get("验收标准") or "").strip()
    status = str(row.get("状态") or "")
    url = str(row.get("必读链接") or "").strip()
    done = status == "已完成"

    if status == "不适用":
        blocks.append(
            {
                "block_type": BLOCK_TEXT,
                "text": {"elements": [_text_run(f"⚪ {title}　不适用", grey=True)], "style": {}},
            }
        )
        return

    if item_id == ROLE_ITEM_ID:
        blocks.append(_todo([_text_run("👤 我是研发人员")], False))
        slots.append((item_id, ROLE_IS_DEV))
        blocks.append(_todo([_text_run("👤 我是非研发人员")], False))
        slots.append((item_id, ROLE_IS_NONDEV))
        return

    if url:
        blocks.append(
            {
                "block_type": BLOCK_TEXT,
                "text": {
                    "elements": [
                        _text_run("📖 "),
                        _linked_run(title, url, bold=True),
                        _text_run(LINK_NOTE, grey=True),
                    ],
                    "style": {},
                },
            }
        )
        blocks.append(_todo([_text_run("💡 已完全理解")], done))
        slots.append((item_id, ROLE_GOT_IT))
        blocks.append(_todo([_text_run("❓ 未完全理解（会找人问清楚）")], False))
        slots.append((item_id, ROLE_UNCLEAR))
        return

    elements = [_text_run(title, bold=True)]
    if acceptance:
        elements.append(_text_run(f"　{acceptance}", grey=True))
    blocks.append(_todo(elements, done))
    slots.append((item_id, ROLE_DONE))


def _render_item(
    cfg: dict[str, Any], item: Any, row: dict[str, Any], blocks: list[dict[str, Any]], slots: list[tuple[str, str]]
) -> None:
    """渲染单条: 不适用标记 / 角色互选 / 必读链接+理解勾选 / 普通 todo。

    ``item`` 是 config 里的 SopItem(提供 links/got_it_note 等渲染态),
    ``row`` 是明细行(提供状态)。
    """
    item_id = item.item_id
    title = item.title
    acceptance = item.acceptance
    status = str(row.get("状态") or "")
    url = item.url
    done = status == "已完成"
    mentions = cfg.get("mentions") or {}
    links = item.links
    got_it_note = item.got_it_note

    if status == "不适用":
        blocks.append(
            {
                "block_type": BLOCK_TEXT,
                "text": {"elements": [_text_run(f"⚪ {title}　不适用", grey=True)], "style": {}},
            }
        )
        return

    if item_id == ROLE_ITEM_ID:
        # 角色选择: 两个互斥的勾选框(入口卡无回调, 角色放在文档里勾)。
        blocks.append(_todo([_text_run("👤 我是研发人员")], False))
        slots.append((item_id, ROLE_IS_DEV))
        blocks.append(_todo([_text_run("👤 我是非研发人员")], False))
        slots.append((item_id, ROLE_IS_NONDEV))
        return

    if url:
        blocks.append(
            {
                "block_type": BLOCK_TEXT,
                "text": {
                    "elements": [
                        _text_run("📖 "),
                        _linked_run(title, url, bold=True),
                        _text_run(LINK_NOTE, grey=True),
                    ],
                    "style": {},
                },
            }
        )
        got_it_elements = [_text_run("💡 已完全理解")]
        if got_it_note:
            got_it_elements.append(_text_run(f"　{got_it_note}", grey=True))
        blocks.append(_todo(got_it_elements, done))
        slots.append((item_id, ROLE_GOT_IT))
        blocks.append(_todo([_text_run("❓ 未完全理解（会找人问清楚）")], False))
        slots.append((item_id, ROLE_UNCLEAR))
        return

    elements = render_runs(title, mentions=mentions, links=links, bold=True)
    if acceptance:
        elements.extend(render_runs(f"　{acceptance}", mentions=mentions, links=links, grey=True))
    blocks.append(_todo(elements, done))
    slots.append((item_id, ROLE_DONE))


def _render_faq(cfg: dict[str, Any], blocks: list[dict[str, Any]], slots: list[tuple[str, str]]) -> None:
    """尾部 FAQ 问答区 —— 纯静态, 问答本身不进 slots(不参与勾选同步)。

    但 FAQ 的 H2 标题在 provision_doc 的配对里会被 _tracked_children 筛中,
    所以它也要占一个 slot 位(role=tally、item_id 用「FAQ」占位) ——
    update_tallies 对它反查不到明细行会直接跳过, 无害。
    """
    faq = cfg.get("faq")
    if not isinstance(faq, list) or not faq:
        return
    blocks.append({"block_type": BLOCK_DIVIDER, "divider": {}})
    blocks.append(
        {
            "block_type": BLOCK_HEADING2,
            "heading2": {"elements": [_text_run("🙋 新人常见问题（FAQ）")], "style": {}},
        }
    )
    slots.append(("FAQ", ROLE_TALLY))
    for entry in faq:
        if not isinstance(entry, dict):
            continue
        q = str(entry.get("q") or "").strip()
        raw_a = entry.get("a")
        a = [str(x).strip() for x in raw_a if str(x).strip()] if isinstance(raw_a, list) else str(raw_a or "").strip()
        if not q:
            continue
        blocks.append(
            {
                "block_type": BLOCK_HEADING4,
                "heading4": {"elements": [_text_run(q)], "style": {}},
            }
        )
        # 答案支持 @人 与链接片段(如「来源:新人入职 SOP · 试运行」挂 SOP 链接)
        faq_links: tuple[tuple[str, str], ...] = ()
        raw_links = entry.get("links")
        if isinstance(raw_links, list):
            parsed = []
            for link_entry in raw_links:
                if isinstance(link_entry, dict):
                    t = str(link_entry.get("text") or "").strip()
                    u = str(link_entry.get("url") or "").strip()
                    if t and u:
                        parsed.append((t, u))
            faq_links = tuple(parsed)
        answer_lines = a if isinstance(a, list) else [a]
        for answer_line in answer_lines:
            blocks.append(
                {
                    "block_type": BLOCK_TEXT,
                    "text": {
                        "elements": render_runs(
                            str(answer_line), mentions=cfg.get("mentions") or {}, links=faq_links, rich=True
                        ),
                        "style": {},
                    },
                }
            )


def build_doc_blocks(
    rows: list[dict[str, Any]],
    *,
    name: str,
    today: date | None = None,
    sop_url: str = "",
    cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """渲染文档根节点的块, 并给出「第 N 个可追踪块对应哪个条目」的顺序表。

    返回 (blocks, slots): slots 与 blocks 里**可追踪块**(todo / 分节小计 heading2 /
    图片块)的出现顺序一一对应, 供 provision_doc 与飞书返回的块按位配对。
    cfg 为 None 时退化为旧版渲染(无头部/组/验收人/FAQ), 老调用不受影响。

    阅读类条目(有必读链接)排两个理解勾选:
        📖 标题(超链接)
        ☐ 已完全理解　{got_it_note}        ☐ 未完全理解(会找人问清楚)
    「已阅读」已去掉 —— 读没读不重要, 重要的是懂没懂。
    """
    if cfg is None:
        cfg = {}
    layout = _cfg.load_layout(cfg)
    mentions = layout.get("mentions") or {}

    blocks: list[dict[str, Any]] = []
    slots: list[tuple[str, str]] = []
    _render_header(cfg, rows, blocks, slots)
    _render_modules(cfg, rows, blocks, slots, layout, mentions)
    _render_faq(cfg, blocks, slots)
    return blocks, slots


def read_doc_state(blocks: list[dict[str, Any]], block_map: dict[str, str]) -> tuple[dict[str, bool], list[str]]:
    """从文档块读回 ({item_id: 是否完成}, [勾了「未完全理解」的 item_id])。

    ``block_map`` 是 {block_id: "item_id:role"} —— 建文档时存下的映射。靠它而不是
    文字标记认条目, 所以文档正文没有多余字符; 新人自己新增的块不在映射里, 自然被
    忽略(当作他自己的笔记)。

    阅读类条目: 「已完全理解」与「未完全理解」**任选一个**即算完成 —— 后者也是
    有效回答(读过了、如实说没懂), 动作已经走完, 不该继续催。但勾了「未完全理解」
    的条目会进 unclear 列表单独报给 HR, 所以「没懂」不会被悄悄放过。
    普通条目(role=ROLE_DONE)只有一个 todo, 勾上即完成。
    """
    ticked: dict[str, dict[str, bool]] = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get("block_type") != BLOCK_TODO:
            continue
        mapped = block_map.get(str(block.get("block_id") or ""))
        if not mapped or ":" not in mapped:
            continue
        item_id, role = mapped.rsplit(":", 1)
        # 小计块(role=tally)不是条目 —— 它的 item_id 位置放的是模块名。
        # 不跳过的话模块名会混进 state, 被当成一个「未完成的条目」参与判定。
        if role == ROLE_TALLY:
            continue
        todo = block.get("todo")
        done = bool((todo or {}).get("style", {}).get("done")) if isinstance(todo, dict) else False
        ticked.setdefault(item_id, {})[role] = done

    state: dict[str, bool] = {}
    unclear: list[str] = []
    for item_id, roles in ticked.items():
        if item_id == ROLE_ITEM_ID:
            # 角色项: 两个框互斥, 都勾了以「非研发」为准 —— 宁可让研发项显示为
            # 不适用(可由 HR 或本人改回), 也不要给非研发的人压 5 个他做不了的项。
            picked = bool(roles.get(ROLE_IS_DEV)) or bool(roles.get(ROLE_IS_NONDEV))
            state[item_id] = picked
            continue
        if roles.get(ROLE_UNCLEAR):
            unclear.append(item_id)
        if ROLE_GOT_IT in roles or ROLE_UNCLEAR in roles:
            # 阅读类: 两个选项**任选一个**即算这一条完成 —— 需求如此。
            # 「未完全理解」也是一种有效回答: 新人已经读过、并如实反馈没懂,
            # 这条动作就算走完了; 剩下的是找人问清楚, 由 unclear 单独报给 HR,
            # 不该因此把这一项一直挂在未完成里催他。
            state[item_id] = bool(roles.get(ROLE_GOT_IT)) or bool(roles.get(ROLE_UNCLEAR))
        else:
            state[item_id] = bool(roles.get(ROLE_DONE))
    return state, unclear


def read_role_choice(blocks: list[dict[str, Any]], block_map: dict[str, str]) -> str:
    """新人在文档里勾的角色: "dev" / "nondev" / ""(还没勾)。

    两个框都勾了以「非研发」为准 —— 宁可让 5 个研发项显示为不适用(可改回),
    也不要给非研发的人压一堆他做不了的项。
    """
    is_dev = False
    is_nondev = False
    for block in blocks:
        if not isinstance(block, dict) or block.get("block_type") != BLOCK_TODO:
            continue
        mapped = block_map.get(str(block.get("block_id") or ""))
        if not mapped or ":" not in mapped:
            continue
        item_id, role = mapped.rsplit(":", 1)
        if item_id != ROLE_ITEM_ID:
            continue
        todo = block.get("todo")
        done = bool((todo or {}).get("style", {}).get("done")) if isinstance(todo, dict) else False
        if role == ROLE_IS_DEV and done:
            is_dev = True
        elif role == ROLE_IS_NONDEV and done:
            is_nondev = True
    if is_nondev:
        return "nondev"
    return "dev" if is_dev else ""


def diff_state(doc_state: dict[str, bool], rows: list[dict[str, Any]]) -> list[str]:
    """文档里已完成、而表里还没记完成的 item_id。

    刻意为之: 只认「未完成 → 完成」这一个方向。反向(表里已完成、文档里被取消勾选)
    不做撤销 —— 已完成是既成事实, 让新人取消勾选就能抹掉记录, 会让 HR 日报的数据
    变得不可信; 真要撤销应当由人工改表。
    """
    by_id = {_item_id_of(r): r for r in rows}
    out: list[str] = []
    for item_id, done in doc_state.items():
        if not done:
            continue
        row = by_id.get(item_id)
        if row is None:
            continue
        if str(row.get("状态") or "") == "未完成":
            out.append(item_id)
    return out
