"""提示词让模型读的技能路径必须真能读到。

背景: 系统提示词里 20 多处示范 `read skills/<name>/SKILL.md`, 而文件工具的**相对路径解析到
user workspace**, 技能却住在 **agent 包**(见 `tools/read.py` docstring)。两个根在装机形态下
本来就不同 (`--default-agent {app}` 对 `--default-workspace {Desktop}/haitun交付`), 于是那些
示范**每一条都是死路径**, 而 `read` 失败只返回一句 `[Error] File not found: ...` 字符串 ——
不是异常、不进日志, 模型换个做法继续。2026-09-22 那次正负面清单报告就是这么丢掉了整套认知:
会话读了 `skills/positive-negative-list/SKILL.md`, 落到的却是**生产机的旧文件**, 本地那份带
「认知标准」的新版一次都没进上下文。

这些用例钉住两半:
1. `read` 自己认 `skills/...` 这个字面量(workspace 没有再问 agent 包);
2. 技能索引带上 `path` 属性 —— 模型从此不必猜绝对路径。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import anyio
import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
TOOLS_DIR_STR = str(TOOLS_DIR)

from psi_agent.session.runtime_context import path_scope  # noqa: E402


def _tools_module(name: str):
    """按裸名 import tools 目录里的模块 —— 与加载器给它们互相可见的形状一致。

    裸名 import 是 `read` 拿 `_runtime_paths` 的方式, 所以用例照抄; 但必须拿到**本包**那一份:
    desktop 包也有同名文件, 谁先上 `sys.path` 谁赢(见 `agents/desktop/AGENTS.md` 对
    `_runtime_paths` 裸名冲突的记载), 故这里显式校验来源。
    """
    if sys.path[0] != TOOLS_DIR_STR:
        sys.path.insert(0, TOOLS_DIR_STR)
    module = importlib.import_module(name)
    origin = module.__file__
    assert origin, f"{name} 没有 __file__"
    assert TOOLS_DIR in Path(origin).resolve().parents, origin
    return module


def _feishu_system_module():
    """按文件位置加载本包的 `systems/system.py`(邻居用例 `test_project_deep_learning_skill` 同款)。"""
    systems_dir = TOOLS_DIR.parent / "systems"
    module_name = "psi_test_skill_path_index_system"
    spec = importlib.util.spec_from_file_location(module_name, systems_dir / "system.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(systems_dir))
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module, systems_dir


@pytest.fixture
def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    """``(agent_root, workspace)`` —— 两个根是两个目录, 各带自己的 skills。"""
    agent = tmp_path / "agent"
    workspace = tmp_path / "workspace"
    (agent / "skills" / "demo-skill").mkdir(parents=True)
    (agent / "skills" / "demo-skill" / "SKILL.md").write_text("# demo skill (agent copy)\n", encoding="utf-8")
    (workspace / "skills" / "workspace-skill").mkdir(parents=True)
    (workspace / "skills" / "workspace-skill" / "SKILL.md").write_text("# workspace copy\n", encoding="utf-8")
    return agent, workspace


async def test_relative_skill_path_reads_from_the_agent_package(two_roots: tuple[Path, Path]) -> None:
    """本次要修的那一条: 相对 `skills/...` 不再指向一个不存在的 workspace 路径。"""
    agent, workspace = two_roots
    read_tool = _tools_module("read")

    with path_scope(workspace=str(workspace), agent=str(agent)):
        out = await read_tool.read("skills/demo-skill/SKILL.md")

    assert "demo skill (agent copy)" in out, out
    assert not out.startswith("[Error]"), out


async def test_workspace_file_of_the_same_name_still_wins(two_roots: tuple[Path, Path]) -> None:
    """回落只补空缺, 不夺权: workspace 里真有这个名字时读的是它。"""
    agent, workspace = two_roots
    read_tool = _tools_module("read")

    with path_scope(workspace=str(workspace), agent=str(agent)):
        out = await read_tool.read("skills/workspace-skill/SKILL.md")
        assert "workspace copy" in out, out

        (workspace / "skills" / "demo-skill").mkdir(parents=True)
        (workspace / "skills" / "demo-skill" / "SKILL.md").write_text("# workspace copy\n", encoding="utf-8")
        out = await read_tool.read("skills/demo-skill/SKILL.md")
    assert "workspace copy" in out, out


async def test_missing_skill_still_reports_the_path_it_tried(two_roots: tuple[Path, Path]) -> None:
    """两处都没有时仍要给出错误 —— 回落不是"什么都读得到"。"""
    agent, workspace = two_roots
    read_tool = _tools_module("read")

    with path_scope(workspace=str(workspace), agent=str(agent)):
        out = await read_tool.read("skills/no-such-skill/SKILL.md")

    assert out.startswith("[Error] File not found:"), out


async def test_paths_outside_skills_do_not_fall_back(two_roots: tuple[Path, Path]) -> None:
    """只有 `skills/` 这一前缀回落; 别的相对读仍严格落在 workspace。"""
    agent, workspace = two_roots
    (agent / "SOUL.md").write_text("# agent soul\n", encoding="utf-8")
    read_tool = _tools_module("read")

    with path_scope(workspace=str(workspace), agent=str(agent)):
        out = await read_tool.read("SOUL.md")

    assert out.startswith("[Error] File not found:"), out


async def test_single_root_mode_is_unchanged(two_roots: tuple[Path, Path]) -> None:
    """agent 与 workspace 同根时(未绑 ContextVar / 单根部署)行为逐字不变。"""
    _, workspace = two_roots
    read_tool = _tools_module("read")

    with path_scope(workspace=str(workspace), agent=""):
        out = await read_tool.read("skills/workspace-skill/SKILL.md")

    assert "workspace copy" in out, out


async def test_index_entries_carry_a_resolvable_path(two_roots: tuple[Path, Path], tmp_path: Path) -> None:
    """索引发的是 `path`, 而不是让模型去猜 —— 且那个 path 必须真的存在。"""
    agent, _workspace = two_roots
    module, systems_dir = _feishu_system_module()
    try:
        module.__dict__["_GLOBAL_AGENT_SKILLS_DIR"] = anyio.Path(str(tmp_path / "no-global-skills"))
        skills_xml = await module._build_skills_index(anyio.Path(str(agent)))
    finally:
        if sys.path and sys.path[0] == str(systems_dir):
            sys.path.pop(0)
        sys.modules.pop("psi_test_skill_path_index_system", None)

    assert 'name="demo-skill"' in skills_xml, skills_xml
    marker = 'path="'
    assert marker in skills_xml, skills_xml
    paths = [line.split(marker, 1)[1].rstrip('"').strip() for line in skills_xml.splitlines() if marker in line]
    assert paths, skills_xml
    for raw in paths:
        assert await anyio.Path(raw).is_file(), f"索引给出的路径不存在: {raw}"
    assert any(Path(raw).name == "SKILL.md" and Path(raw).parent.name == "demo-skill" for raw in paths), paths
