"""Org tree sync - auto-build the LLM wiki work tree from Feishu's org chart.

Backs R2/R3 of the company TODO management design ("按组织层级自动汇总为一棵树,
每个节点是一名成员"). Two Feishu-side facts make this a tool rather than something
left to a model to improvise each run:

1. Each user record from ``contact/v3/users/batch`` already carries its own
   ``leader_user_id`` - the person's *direct manager*, straight from Feishu's org
   chart. This is more precise than deriving "who reports to whom" from department
   leadership (a department's ``primary_leader_ids`` answers "who runs this
   department", not "who is Zhang San's manager" - those differ whenever someone's
   manager isn't their department head). So the tree is built person-by-person off
   ``leader_user_id``, not department-by-department.
2. Building the tree safely requires handling shapes a prompt would get wrong on a
   bad day: a manager who resolves to nobody in the fetched roster (dangling edge -
   report it, don't invent a name), a leadership cycle (A's manager is B, B's manager
   is A - walking it naively infinite-loops), and more than one person with no
   resolvable manager (more than one "root" - report all of them rather than picking
   one arbitrarily and hiding the ambiguity).

Wiki write discipline: every person's summary page (title = their name, the same
page ``company-todo-sync`` maintains) gets one delimited section, ``## 组织关系``,
replaced idempotently on each sync. This module never touches the ``## 当前目标`` or
``## 历史索引`` sections that page also carries - those are owned by
``company-todo-sync`` / the person themself. The root index page (title
"公司工作树") is fully owned by this module and rewritten wholesale each run (it is
a pure derived view, not something a human edits).

Not yet verified against a real Feishu tenant (no live org data available at write
time) - the id-space handling for ``leader_user_id`` (which id type it comes back as
relative to the batch query's ``user_id_type``) is defensive rather than confirmed.
See the docstring on ``sync_org_tree_impl`` for what to check on first real run.
"""

from __future__ import annotations

import json
import re
from typing import Any

import _feishu_impl as _feishu
import _llm_wiki_impl as _wiki

_ORG_SECTION_HEADING = "## 组织关系"
_SECTION_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)
_ROOT_PAGE_TITLE = "公司工作树"
_BATCH_SIZE = 50


def dumps_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def _splice_section(body: str, heading: str, new_section_body: str) -> str:
    """Replace (or insert) one ``## heading`` section in a Markdown body, leaving the
    rest of the document untouched. The section runs from its heading line to the
    next ``## `` heading (or end of document)."""
    new_block = f"{heading}\n{new_section_body.strip()}\n"
    start = body.find(heading)
    if start == -1:
        # No existing section: prepend it (org relationship is the page's most
        # load-bearing fact, so it goes first - consistent across every person page).
        rest = body.strip()
        return f"{new_block}\n{rest}\n" if rest else f"{new_block}\n"
    # Find the next "## " heading after this one to bound the section.
    after_heading = start + len(heading)
    next_match = _SECTION_HEADING_RE.search(body, after_heading)
    end = next_match.start() if next_match else len(body)
    before = body[:start]
    after = body[end:]
    return f"{before}{new_block}\n{after}".rstrip() + "\n"


async def _fetch_all_members(
    root_department_id: str, user_id_type: str
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    res = await _feishu.list_department_members_impl(
        department_id=root_department_id,
        department_id_type="open_department_id",
        user_id_type=user_id_type,
        recursive=True,
    )
    if not res.get("ok"):
        return [], res
    members = res.get("members", [])
    return members if isinstance(members, list) else [], None


async def _fetch_user_records(
    user_ids: list[str], user_id_type: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Batch-fetch full user records (for leader_user_id) in chunks of _BATCH_SIZE."""
    records: list[dict[str, Any]] = []
    for i in range(0, len(user_ids), _BATCH_SIZE):
        chunk = user_ids[i : i + _BATCH_SIZE]
        res = await _feishu.get_users_batch_impl(user_ids=",".join(chunk), user_id_type=user_id_type)
        if not res.get("ok"):
            return records, res
        users = res.get("users", [])
        records.extend(u for u in users if isinstance(u, dict))
    return records, None


def _build_id_maps(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Two lookups (by open_id, by user_id) so leader_user_id can be resolved
    regardless of which id space Feishu returns it in relative to the query."""
    by_open_id: dict[str, dict[str, Any]] = {}
    by_user_id: dict[str, dict[str, Any]] = {}
    for r in records:
        oid = str(r.get("open_id") or "")
        uid = str(r.get("user_id") or "")
        if oid:
            by_open_id[oid] = r
        if uid:
            by_user_id[uid] = r
    return by_open_id, by_user_id


def _resolve_manager_open_id(leader_user_id: str, by_open_id: dict[str, Any], by_user_id: dict[str, Any]) -> str | None:
    if not leader_user_id:
        return None
    if leader_user_id in by_open_id:
        return leader_user_id
    hit = by_user_id.get(leader_user_id)
    if hit is not None:
        return str(hit.get("open_id") or "") or None
    return None


def _detect_cycle(open_id: str, manager_of: dict[str, str], seen: set[str] | None = None) -> list[str] | None:
    """Walk up the manager chain from open_id; return the cycle's open_ids if one
    exists, else None. Guards the tree walk from infinite-looping on bad org data."""
    seen = seen or set()
    chain: list[str] = []
    cur = open_id
    while cur:
        if cur in seen:
            return chain
        seen.add(cur)
        chain.append(cur)
        cur = manager_of.get(cur, "")
        if not cur:
            return None
    return None


def _build_children_map(manager_of: dict[str, str]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for person, manager in manager_of.items():
        if manager:
            children.setdefault(manager, []).append(person)
    return children


def _render_tree_lines(
    root_open_id: str,
    children: dict[str, list[str]],
    names: dict[str, str],
    depth: int = 0,
    visited: set[str] | None = None,
) -> list[str]:
    visited = visited or set()
    if root_open_id in visited:
        label = names.get(root_open_id, root_open_id)
        return [f"{'  ' * depth}- [[{label}]] (warning: cycle detected, truncated)"]
    visited.add(root_open_id)
    name = names.get(root_open_id, root_open_id)
    lines = [f"{'  ' * depth}- [[{name}]]"]
    for child in sorted(children.get(root_open_id, []), key=lambda oid: names.get(oid, oid)):
        lines.extend(_render_tree_lines(child, children, names, depth + 1, visited))
    return lines


async def sync_org_tree_impl(
    root_department_id: str = "0",
    boss_open_id: str = "",
    user_id_type: str = "open_id",
) -> dict[str, Any]:
    """Rebuild the wiki work tree from Feishu's org chart. See module docstring.

    First real-run checklist (cannot verify without a live tenant): confirm
    ``get_users_batch_impl``'s ``leader_user_id`` for a known manager/report pair
    resolves correctly through ``_resolve_manager_open_id`` - if Feishu returns it in
    a different id space than expected, every person will show up as an "unresolved
    root" instead of correctly nesting.
    """
    members, err = await _fetch_all_members(root_department_id, user_id_type)
    if err is not None:
        return err
    if not members:
        return _error(f"No members found under department {root_department_id!r} - check 通讯录权限范围.")

    ids = [m.get("open_id") or m.get("user_id") or "" for m in members]
    ids = [i for i in ids if i]
    records, err = await _fetch_user_records(ids, user_id_type)
    if err is not None:
        return err
    if not records:
        return _error("Member roster fetched but no user records resolved - check contact scopes.")

    by_open_id, by_user_id = _build_id_maps(records)
    names: dict[str, str] = {oid: str(r.get("name") or oid) for oid, r in by_open_id.items()}

    manager_of: dict[str, str] = {}
    unresolved_leader: dict[str, str] = {}
    for oid, rec in by_open_id.items():
        raw_leader = str(rec.get("leader_user_id") or "")
        if not raw_leader:
            continue
        resolved = _resolve_manager_open_id(raw_leader, by_open_id, by_user_id)
        if resolved is None:
            unresolved_leader[oid] = raw_leader
        elif resolved != oid:  # a self-referencing leader_user_id is nonsensical data - drop it
            manager_of[oid] = resolved

    cycles: list[list[str]] = []
    seen_in_any_cycle: set[str] = set()
    for oid in by_open_id:
        if oid in seen_in_any_cycle:
            continue
        cyc = _detect_cycle(oid, manager_of)
        if cyc:
            cycles.append([names.get(c, c) for c in cyc])
            seen_in_any_cycle.update(cyc)
            # Break the cycle so tree rendering below doesn't loop: drop the edge
            # from the last node in the detected chain back to its manager.
            manager_of.pop(cyc[-1], None)

    roots = [oid for oid in by_open_id if oid not in manager_of]
    if boss_open_id.strip() and boss_open_id.strip() in by_open_id:
        # An explicit boss always anchors the tree even if org data suggests
        # multiple roots - the other "roots" are then reported as anomalies, not
        # silently absorbed, since they likely indicate missing leader_user_id data.
        anchor = boss_open_id.strip()
        anomalous_roots = [oid for oid in roots if oid != anchor]
    else:
        anchor = roots[0] if roots else next(iter(by_open_id))
        anomalous_roots = [oid for oid in roots if oid != anchor]

    children = _build_children_map(manager_of)

    written: list[str] = []
    failed: list[dict[str, str]] = []
    for oid in by_open_id:
        name = names[oid]
        manager_oid = manager_of.get(oid)
        manager_line = f"上级: [[{names[manager_oid]}]]" if manager_oid else "上级: (无, 根节点)"
        reports = sorted(children.get(oid, []), key=lambda c: names.get(c, c))
        reports_line = "下属: " + (", ".join(f"[[{names[r]}]]" for r in reports) if reports else "(无)")
        section_body = f"{manager_line}\n{reports_line}"

        read_res = await _wiki.wiki_read_impl(name)
        existing_body = read_res.get("content", "") if read_res.get("ok") else ""
        new_body = _splice_section(existing_body, _ORG_SECTION_HEADING, section_body)
        write_res = await _wiki.wiki_write_impl(title=name, content=new_body, tags="person", overwrite=True)
        if write_res.get("ok"):
            written.append(name)
        else:
            failed.append({"name": name, "message": str(write_res.get("message", ""))})

    tree_lines = _render_tree_lines(anchor, children, names)
    root_body = (
        f"组织根页: 从 {names[anchor]} 向下遍历全公司工作树, "
        f"读法见各节点页的[{_ORG_SECTION_HEADING}]区块。\n\n" + "\n".join(tree_lines)
    )
    root_write = await _wiki.wiki_write_impl(
        title=_ROOT_PAGE_TITLE, content=root_body, tags="cycle,project", overwrite=True
    )

    result: dict[str, Any] = {
        "ok": True,
        "member_count": len(by_open_id),
        "root": names[anchor],
        "root_open_id": anchor,
        "pages_written": len(written),
        "pages_failed": failed,
        "root_page_written": bool(root_write.get("ok")),
    }
    if unresolved_leader:
        result["unresolved_leaders"] = {names.get(k, k): v for k, v in unresolved_leader.items()}
        result["unresolved_leaders_note"] = (
            "这些人的 leader_user_id 在本次拉取的名单里找不到对应记录(常见于其上级不在本次 "
            f"department_id={root_department_id!r} 的范围内, 或该记录被通讯录权限范围排除), "
            "本次这些人被当作根节点处理, 不代表组织架构真的没有上级。"
        )
    if cycles:
        result["cycles_detected"] = cycles
        result["cycles_note"] = "检测到环形上下级引用(数据异常), 已在树里截断显示, 需要人工核实飞书通讯录。"
    if anomalous_roots:
        result["anomalous_roots"] = [names[oid] for oid in anomalous_roots]
        result["anomalous_roots_note"] = (
            "这些人也没有可解析的上级, 但树里只挂在 boss_open_id 指定的根下, 请核实是否也应汇报给某人。"
        )
    return result
