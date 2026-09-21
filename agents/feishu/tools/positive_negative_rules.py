"""Read-only query tool for the versioned positive-negative rule pack."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f  # noqa: E402
from _positive_negative_list.rules import (  # noqa: E402
    DEFAULT_VERSION,
    cognition_source,
    load_cognition_pack,
    query_rules,
    rule_pack_source,
)


async def positive_negative_rules(query: str, version: str = DEFAULT_VERSION, limit: int = 8) -> str:
    """Query stable positive-negative rules and the cognition standard they sit in.

    Args:
        query: Text to match against rule titles, categories, and keywords.
        version: Exact version identifier, defaulting to ``6.0-shadow``.
        limit: Maximum number of deterministic rule fragments to return.

    Returns:
        JSON with the matched rules, the rule-pack fingerprint, and the 认知口径
        (what the list is, how 正面/负面 both read as one person's growth, and how
        a report about them is written).  The cognition text is read from
        ``skills/positive-negative-list/cognition.yaml`` and is not embedded in
        this tool, so revising the standard is a content change, not a code change;
        ``cognition_source`` fingerprints the file that was actually read.
    """
    if not isinstance(query, str) or not query.strip():
        return _f.dumps_result({"ok": False, "error": "query must be a non-empty string"})
    if not isinstance(version, str) or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 32:
        return _f.dumps_result(
            {"ok": False, "error": "version must be a string and limit must be an integer from 1 to 32"}
        )
    try:
        rules: list[dict[str, Any]] = query_rules(query, version, limit)
        source = rule_pack_source(version)
        # 认知口径与规则**一起**返回, 不另开工具: 判断一条行为算哪一档时, "正负面都算成长"
        # 是同一句话的下半句。分成两个工具, 就给了"只查了规则、没查口径"一条合法的捷径,
        # 而报告写歪恰恰是那么发生的。口径缺失时 ``load_cognition_pack`` 失败关闭。
        cognition = load_cognition_pack()
        cognition_fingerprint = cognition_source()
    except ValueError as exc:
        return _f.dumps_result({"ok": False, "error": str(exc)})
    # ``source`` carries the rule file fingerprint so an agent can cite "which
    # pack, which bytes" without shelling out to md5sum — the tool now answers
    # the provenance question itself.  ``cognition_source`` does the same for the
    # cognition standard, which is the part most likely to be revised by content.
    return _f.dumps_result(
        {
            "ok": True,
            "version": version,
            "source": source,
            "cognition_source": cognition_fingerprint,
            "cognition": cognition.as_dict(),
            "rules": rules,
            "match_count": len(rules),
        }
    )
