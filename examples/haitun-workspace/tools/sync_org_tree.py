"""Org tree tool — auto-build the LLM wiki work tree from Feishu's org chart.

Backs "工作树按组织层级自动汇总为一棵树, 每个节点是一名成员" (R2 of the company TODO
management design): read the company's org chart from Feishu, and write one wiki page
per person with their resolved manager/reports, plus a root index page you can
traverse from.

This is separate from ``company-todo-sync``'s own wiki writes: that skill writes each
person's **history** (todo snapshots + "当前目标"); this tool writes each person's
**position in the org** (a distinct ``## 组织关系`` section on the same page, spliced
in without touching the rest). Run this whenever the org chart changes (new hire, team
reorg) — it is idempotent and safe to re-run on a stable org.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _org_tree_impl as _org


async def sync_org_tree(
    root_department_id: str = "0",
    boss_open_id: str = "",
    user_id_type: str = "open_id",
) -> str:
    """Rebuild the wiki work tree (one page per person) from Feishu's live org chart.

    Fetches every member under ``root_department_id`` (default "0" = whole company),
    then each person's full record — which carries Feishu's own ``leader_user_id``,
    their **direct manager** — and derives the tree from that (not from department
    leadership, which answers "who runs this department" rather than "who is this
    person's manager" — the two differ whenever a manager isn't their report's
    department head).

    For each person, writes (or updates) their wiki summary page (title = their
    Feishu display name — the same page ``company-todo-sync`` maintains): only the
    page's ``## 组织关系`` section is replaced, listing their manager as a
    ``[[wikilink]]`` and their direct reports the same way; the page's other
    sections (目标/历史索引, owned by ``company-todo-sync`` or the person
    themself) are read back and preserved untouched. Also rewrites the root index
    page 《公司工作树》 as a nested `[[wikilink]]` list from the resolved root
    downward — that page is a pure derived view and is always fully overwritten.

    Handles the shapes real org data can produce rather than assuming a clean tree:
    a person whose ``leader_user_id`` doesn't resolve to anyone in the fetched
    roster is reported in ``unresolved_leaders`` (and treated as a root, not
    silently dropped or guessed at) — common when their manager sits outside
    ``root_department_id`` or outside the app's 通讯录权限范围. A leadership
    cycle (A reports to B, B reports to A — real org data occasionally has these
    from a stale edit) is detected and reported in ``cycles_detected`` rather than
    infinite-looping the tree walk. If ``boss_open_id`` is given it anchors the
    tree explicitly; any other person with no resolvable manager then shows up in
    ``anomalous_roots`` for human review rather than being silently absorbed as if
    they also reported to nobody by design.

    Args:
        root_department_id: Department to sync from, "0" = whole company (default).
            Narrow this to sync just one project/team's subtree.
        boss_open_id: The company's boss open_id, to anchor the tree root
            explicitly. Omit to let the org chart's own resolved root (whoever has
            no manager) anchor it — fine for a single-root org, ambiguous for one
            with several.
        user_id_type: Id form used throughout — open_id (default), union_id, user_id.

    Returns:
        JSON with ok, member_count, root (name), root_open_id, pages_written,
        pages_failed, root_page_written, and (only when present) unresolved_leaders,
        cycles_detected, anomalous_roots with explanatory *_note fields — or ok=false
        with a message (e.g. empty roster, likely a 通讯录权限范围 misconfiguration)
        on failure.
    """
    result = await _org.sync_org_tree_impl(root_department_id, boss_open_id, user_id_type)
    return _org.dumps_result(result)
