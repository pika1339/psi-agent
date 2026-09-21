"""MCP tools must actually reach the tool registry (both agent packs).

`_mcp` builds its callables **inside `_mcp`** and injects them into the host module's
globals. Their `__module__` was therefore `'_mcp'`, while `ToolRegistry._exec_tool_files`
keeps only attributes whose `__module__` equals the module it is scanning — a guard meant
to skip imported names like `json` / `logger`. The two collided, and the result was silent:

    present in `dir(module)`, absent from the registry, no warning, no error.

Measured 2026-09-21 on `agents/desktop`: 95 tools loaded and **every** MCP tool missing —
`serper_google_search` (so no general web search), all six `browser_*` (so no browsing),
and `canvas_call`. Non-MCP tools in the same pack loaded fine, which is what made it look
like a credential or packaging problem. The packaged build has the same defect, so the
2026-08-18 evaluation baseline was measured with neither search nor a browser.

These judgements are deliberately end-to-end through the real loader: a test that only
imported the module would pass even while the registry drops everything, which is exactly
how this survived.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from psi_agent.session.content_roots import ContentRoot
from psi_agent.session.tool_registry import ToolRegistry

#: `tests/agents/desktop/<this file>` -> repo root -> `agents/`
_AGENTS_ROOT = Path(__file__).resolve().parents[3] / "agents"

#: The two packs ship a byte-identical `tools/_mcp.py` (verified by hash), so one file
#: covers both rather than duplicating the judgement twice.
PACKS = ["desktop", "feishu"]

#: Tools that only exist as MCP injections — nothing in the pack's source defines them
#: with `async def`, so they can only appear if the injection survived the scan.
#: (`serper` itself is sync by design and is not expected in the table.)
MCP_TOOLS = [
    "serper_google_search",
    "serper_call",
    "browser_navigate",
    "browser_snapshot",
    "browser_call",
]


@pytest.mark.parametrize("pack", PACKS)
@pytest.mark.anyio
async def test_mcp_tools_survive_the_registry_scan(pack: str) -> None:
    """Every MCP-injected tool must be in the registry for the pack that declares it."""
    root = ContentRoot(name=pack, path=_AGENTS_ROOT / pack, priority=0)
    registry = await ToolRegistry.load_content_roots([root], f"test-mcp-registration-{pack}")
    names = set(registry.tools)

    assert names, f"{pack}: 工具表是空的, 判据本身失效了"

    missing = [tool for tool in MCP_TOOLS if tool not in names]
    assert not missing, (
        f"{pack}: 这些 MCP 工具没进工具表 {missing}。"
        "最可能的原因: _mcp 注入的可调用对象没有认领宿主模块名, 于是被 "
        "ToolRegistry._exec_tool_files 的 __module__ 守卫静默丢掉。"
    )


@pytest.mark.parametrize("pack", PACKS)
def test_injected_tools_claim_their_host_module(pack: str) -> None:
    """The invariant behind the judgement above, stated directly.

    Asserting on `__module__` (not just on presence) means a future refactor that injects
    by some other route still has to satisfy the registry's scan rule.

    The bare `search` module is imported under a temporarily extended `sys.path` because
    that is exactly how the pack's private modules resolve at runtime; the module is
    dropped from `sys.modules` afterwards so the next parametrisation re-imports the other
    pack's copy instead of reusing this one.
    """
    tools_dir = _AGENTS_ROOT / pack / "tools"
    sys.path.insert(0, str(tools_dir))
    try:
        sys.modules.pop("search", None)
        module = importlib.import_module("search")
        assert module.__name__ == "search"
        for name in ("serper_google_search", "serper_call"):
            func = getattr(module, name)
            assert func.__module__ == "search", f"{pack}: {name}.__module__ == {func.__module__!r}, 会被注册表跳过"
    finally:
        sys.modules.pop("search", None)
        sys.path.remove(str(tools_dir))
