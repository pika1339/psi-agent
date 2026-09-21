"""SOP 清单解析 —— 纯逻辑，不碰飞书，便于单测。

刻意为之: 清单本身在 agent 包 config/rookie_sop.yaml 里, 改 SOP 不用改代码
(与 config/handbook_onboarding.yaml 同一模式)。2.0 起支持「模块 > 小组 > 条目」
三层结构与头部/FAQ 静态区(见 config 文件头注释)。
"""

from __future__ import annotations

# ruff: noqa: RUF002
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

ROLE_DEV = "dev"
ROLE_NONDEV = "nondev"

# 头部伙伴特质 3 条的模块名 —— 它们渲染在文档头部、没有模块标题, 但照样进
# 明细表(参与进度/催办), 所以需要一个模块名给明细行归类。
PARTNERS_MODULE = "我们喜欢的伙伴"


@dataclass(frozen=True)
class SopItem:
    item_id: str
    module: str
    title: str
    acceptance: str
    window_days: int
    dev_only: bool
    # 填了 url 的是必读材料: 详情页渲染成链接 + 两个理解勾选,
    # 而不是笼统的「完成」—— 阅读类的验收就是「读过并理解」。
    url: str = ""
    # 所属小组名(文档里渲染成 H3 标题; 无小组时为空)
    group: str = ""
    # 必读材料的「已完全理解」勾选后追加的说明文字
    got_it_note: str = ""
    # acceptance 里的链接片段: [{text: 片段文字, url: 链接}], 渲染成超链接 run
    links: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _item_from_raw(
    raw: dict[str, Any],
    module_name: str,
    window_days: int,
    group: str = "",
) -> SopItem | None:
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    raw_links = raw.get("links")
    links: tuple[tuple[str, str], ...] = ()
    if isinstance(raw_links, list):
        parsed = []
        for entry in raw_links:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            url = str(entry.get("url") or "").strip()
            if text and url:
                parsed.append((text, url))
        links = tuple(parsed)
    window = raw.get("window_days")
    item_window = window if isinstance(window, int) and window > 0 else window_days
    return SopItem(
        item_id=item_id,
        module=module_name,
        title=str(raw.get("title") or "").strip() or item_id,
        acceptance=str(raw.get("acceptance") or "").strip(),
        window_days=item_window,
        dev_only=bool(raw.get("dev_only")),
        url=str(raw.get("url") or "").strip(),
        group=group,
        got_it_note=str(raw.get("got_it_note") or "").strip(),
        links=links,
    )


def load_sop(cfg: dict[str, Any]) -> list[SopItem]:
    """把 yaml 展开成扁平条目列表, 保持声明顺序。

    兼容两层(modules → items)与三层(modules → groups → items)结构:
    小组存在时条目带 group 字段, 文档渲染据此画 H3 小组标题。
    头部伙伴特质 3 条也展开进来(模块名 PARTNERS_MODULE), 它们参与进度。
    """
    items: list[SopItem] = []
    # 头部伙伴特质排最前 —— 入口卡按明细行顺序列模块, 伙伴要在卡片最上面
    header = cfg.get("header")
    if isinstance(header, dict):
        partners = header.get("partners")
        p_window = header.get("window_days", 7)
        if isinstance(partners, list):
            for raw in partners:
                if not isinstance(raw, dict):
                    continue
                p_win = p_window if isinstance(p_window, int) and p_window > 0 else 7
                item = _item_from_raw(raw, PARTNERS_MODULE, p_win)
                if item is not None:
                    items.append(item)
    modules = cfg.get("modules")
    if not isinstance(modules, list):
        return items
    for module in modules:
        if not isinstance(module, dict):
            continue
        module_name = str(module.get("name") or "").strip()
        window_days = module.get("window_days", 1)
        window = window_days if isinstance(window_days, int) and window_days > 0 else 1
        raw_groups = module.get("groups")
        if isinstance(raw_groups, list) and raw_groups:
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                group_name = str(group.get("name") or "").strip()
                raw_items = group.get("items")
                if not isinstance(raw_items, list):
                    continue
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    item = _item_from_raw(raw, module_name, window, group_name)
                    if item is not None:
                        items.append(item)
            continue
        raw_items = module.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = _item_from_raw(raw, module_name, window)
            if item is not None:
                items.append(item)
    return items


def due_date(onboard_date: date, window_days: int) -> date:
    """SOP 的「Day 1-3」表示第 1 到第 3 天, 所以窗口 3 天的截止日是入职日 +2。"""
    window = window_days if window_days > 0 else 1
    return onboard_date + timedelta(days=window - 1)


def day_index(onboard_date: date, today: date) -> int:
    """入职第 N 天, 自然日计数, 入职日为第 1 天。

    刻意为之: 不跳过周末 —— SOP 的 Day 1-7 本身就是自然日窗口。
    """
    return (today - onboard_date).days + 1


def applicable_items(items: list[SopItem], role: str) -> list[SopItem]:
    """按角色过滤。角色未确认("")时也排除 dev_only, 免得进度分母虚高。"""
    if role.strip().casefold() == ROLE_DEV:
        return list(items)
    return [i for i in items if not i.dev_only]


def load_layout(cfg: dict[str, Any]) -> dict[str, Any]:
    """渲染侧布局: header/mentions/faq/模块结构(带 emoji/checker/组), 供
    build_doc_blocks 直接使用 —— 明细行里没有组与验收人信息, 这些只活在 config 里。
    """
    header = cfg.get("header")
    header = header if isinstance(header, dict) else {}
    faq = cfg.get("faq")
    faq = faq if isinstance(faq, list) else []
    mentions = cfg.get("mentions")
    mentions = mentions if isinstance(mentions, dict) else {}
    modules_out: list[dict[str, Any]] = []
    for module in cfg.get("modules") or []:
        if not isinstance(module, dict):
            continue
        name = str(module.get("name") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "emoji": str(module.get("emoji") or "").strip(),
            "checker": str(module.get("checker") or "").strip(),
            "flowchart": bool(module.get("flowchart")),
        }
        groups = module.get("groups")
        if isinstance(groups, list) and groups:
            entry["groups"] = [
                {
                    "name": str(g.get("name") or "").strip(),
                    "emoji": str(g.get("emoji") or "").strip(),
                }
                for g in groups
                if isinstance(g, dict)
            ]
        modules_out.append(entry)
    return {
        "header": header,
        "faq": faq,
        "mentions": mentions,
        "modules": modules_out,
    }
