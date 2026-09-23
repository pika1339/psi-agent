"""提示词让模型去读的 skill 路径, 必须真的能读到。

为什么单独一条: `read` 与其它文件工具把**相对路径**解析到 **user workspace**, 而 skill 住在
**agent 包**。安装形态下两者不同根(`--default-agent {app}` 对
`--default-workspace {Desktop}\\haitun交付`), 于是提示词里写死的 `skills/<name>/SKILL.md`
指向一个**不存在的路径**, 整套规则被静默跳过。

实测代价(2026-09-21, A2 地方消费券 18 题): 提示词第 4 条让模型读
`skills/saving-decision/SKILL.md`, 而该文件只存在于 agent 包 ——
A2-01 首次 `read` 就报 `File not found: ...\\workspace\\skills\\saving-decision\\SKILL.md`,
A2-14 干脆没读。**凡读到该文件的题都调了 `voucher_rules`, 没读到的都没调**, 完美相关:
整条链路的校验环(voucher_rules)从未被走过, 因为规则文件根本没加载。

这类缺陷与 `test_voucher_chain.py` 记的是同一个形状 —— **每一层自己都好, 没人验中间那段**。
那座接缝在"工具之间", 这座在"提示词与文件系统之间"。
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

_REPO = Path(__file__).resolve().parents[3]
_AGENT = _REPO / "agents" / "desktop"
_SYSTEM = _AGENT / "systems" / "system.py"

#: 兜底根不得指向真实 ``~/.agent/skills`` —— 否则本机装了同名 skill 时判据结果取决于环境。
_EMPTY_GLOBAL = Path(__file__).resolve().parent / "_no_such_global_skills_dir_"

sys.path.insert(0, str(_AGENT / "systems"))


def _system_source() -> str:
    return _SYSTEM.read_text(encoding="utf-8")


def _load_helper(global_skills_dir: Path | None = None):
    """取 system.py 里的 ``_collect_skill_dirs`` + ``_resolve_skill_path``, 避免 import 整个模块。

    只截取这两段来 exec, 所以它们用到的模块级名字要自己补上: ``contextlib`` 与
    ``_GLOBAL_AGENT_SKILLS_DIR``。补漏的表现是 ``NameError``, 不是静默通过。
    """
    ns: dict[str, Any] = {
        "__name__": "saving_skill_path_probe",
        "contextlib": contextlib,
        "anyio": anyio,
        "_GLOBAL_AGENT_SKILLS_DIR": anyio.Path(str(global_skills_dir or _EMPTY_GLOBAL)),
    }
    src = _system_source()
    start = src.index("async def _collect_skill_dirs(")
    end = src.index("async def _build_skills_index(", start)
    # exec over this repo's own controlled source: the point is to exercise the real helper.
    exec(compile(src[start:end], str(_SYSTEM), "exec"), ns)
    return ns["_resolve_skill_path"]


async def _resolve_skill_path(agent_dir: anyio.Path, skill: str) -> anyio.Path | None:
    return await _load_helper()(agent_dir, skill)


def test_the_saving_rule_set_exists_where_the_prompt_looks_for_it() -> None:
    """仓库里必须真的装着 saving-decision 规则集, 否则下面几条判据无事可判。"""
    assert (_AGENT / "skills" / "saving-decision" / "SKILL.md").is_file()


def test_resolve_skill_path_returns_an_absolute_path() -> None:
    """解析出来的必须是**绝对**路径 —— 相对路径会被 read 解析回 workspace, 等于没修。"""
    resolved = anyio.run(_resolve_skill_path, anyio.Path(str(_AGENT)), "saving-decision")
    assert resolved is not None, "_resolve_skill_path 没解析出 saving-decision"
    assert Path(str(resolved)).is_absolute(), f"不是绝对路径: {resolved}"
    assert Path(str(resolved)).is_file(), f"解析出来的路径不存在: {resolved}"


def test_resolve_skill_path_returns_none_for_a_missing_skill() -> None:
    """找不到就返回 None —— 调用方据此换成"规则未安装"那段, 而不是指向不存在的文件。"""
    resolved = anyio.run(_resolve_skill_path, anyio.Path(str(_AGENT)), "no-such-skill-xyz")
    assert resolved is None


def test_prompt_injects_a_resolved_path_not_a_bare_relative_one() -> None:
    """钉住注入点: 那次 format 必须传 path=, 且模板不再写死相对路径。

    这是本文件的主要理由 —— 上面几条只测 helper; helper 写对了而注入点仍是无参的
    `_SAVING_GATE_SECTION`, 缺陷原样存在。
    """
    src = _system_source()
    assert "_SAVING_GATE_SECTION.format(path=str(" in src, "注入点没有传解析后的路径: 又把相对路径写回提示词了"
    assert "{path}" in src, "模板里没有 {path} 占位符"

    section = src[src.index("_SAVING_GATE_SECTION = ") : src.index("_SAVING_GATE_SECTION_MISSING = ")]
    assert not re.search(r"`skills/saving-decision/SKILL\.md`", section), "核心规则段里仍有写死的相对路径"


def test_missing_rule_set_does_not_instruct_reading_a_nonexistent_file() -> None:
    """规则集缺失时注入的那段不得再让模型去读文件 ——
    "去读这个文件" + 文件不存在, 正是模型开始凭记忆编规则的地方。"""
    src = _system_source()
    missing = src[src.index("_SAVING_GATE_SECTION_MISSING = ") :]
    assert "skills/saving-decision/SKILL.md" not in missing, "缺失分支仍指示读取不存在的文件"
    assert "{path}" not in missing, "缺失分支不该再带路径占位符"


@pytest.mark.parametrize("skill", ["saving-decision", "workflow"])
def test_known_skills_resolve(skill: str) -> None:
    resolved = anyio.run(_resolve_skill_path, anyio.Path(str(_AGENT)), skill)
    assert resolved is not None, f"{skill} 没解析出来"


def test_agent_package_wins_over_the_global_fallback_root(tmp_path: Path) -> None:
    """agent 包根优先于兜底根 —— 与 ``_build_skills_index`` 的 "nearest wins" 同向。

    两个根都有同名 skill 时必须给 agent 包那份: 索引里列的是哪一份, 提示词就得指哪一份。
    """
    global_root = tmp_path / "global"
    (global_root / "skills" / "saving-decision").mkdir(parents=True)
    (global_root / "skills" / "saving-decision" / "SKILL.md").write_text("global", encoding="utf-8")

    resolved = anyio.run(_load_helper(global_root), anyio.Path(str(_AGENT)), "saving-decision")
    assert resolved is not None
    assert Path(str(resolved)).is_relative_to(_AGENT), f"取到了非 agent 包那份: {resolved}"


def test_a_skill_present_only_in_the_global_root_still_resolves(tmp_path: Path) -> None:
    """只在兜底根存在的 skill 也要解析得出来。

    ``_build_skills_index`` 会把它列进索引, 于是提示词可能指向它 (``~/.agent/skills`` 是
    出厂内容之外的层)。helper 若只看 agent 包根就会返回 None, 注入点随即说"规则未安装",
    而索引里明明列着 —— 两处查找对 skills 住哪儿的看法必须一致。
    """
    global_root = tmp_path / "global"
    (global_root / "skills" / "only-in-global").mkdir(parents=True)
    (global_root / "skills" / "only-in-global" / "SKILL.md").write_text("x", encoding="utf-8")

    resolved = anyio.run(_load_helper(global_root), anyio.Path(str(_AGENT)), "only-in-global")
    assert resolved is not None, "只在兜底根的 skill 没解析出来"
    assert Path(str(resolved)).is_file()


def test_helper_reuses_collect_skill_dirs_so_the_two_lookups_agree() -> None:
    """helper 必须复用 ``_collect_skill_dirs``, 不得自己再拼一次路径规则。

    "在哪里找 skill" 有两份实现就会分叉 —— 这正是本仓反复吃亏的那类缺陷
    (``_build_skills_index`` 用的是 collect 出来的那份; helper 自己拼字符串的话,
    两处对"什么算一个 skill"的判断可以各说各话)。
    """
    src = _system_source()
    helper = src[src.index("async def _resolve_skill_path(") : src.index("async def _build_skills_index(")]
    assert "_collect_skill_dirs(" in helper, "helper 没有复用 _collect_skill_dirs"
    assert '/ "SKILL.md"' not in helper, "helper 又自己拼了 SKILL.md 路径"


def test_the_voucher_rules_gate_lives_in_the_system_prompt_not_only_the_rule_set() -> None:
    """券规则门控必须**同时**出现在系统提示词里, 不能只待在 SKILL.md。

    为什么单独钉一条(实测代价, 2026-09-22): SKILL.md 是靠提示词第 4 条让模型用 read 去加载的,
    而**那个 read 会被跳过** —— A2-14 有一轮 17 次工具调用里一次 read 都没有, 于是 SKILL.md 里的
    门控根本没进上下文, ``voucher_rules`` 调用数为 0。把最小门控放进提示词后, 同一题即便没读
    SKILL.md 也正确调用了 ``voucher_rules``。

    所以这段**不是冗余**: 删掉它就退回"门控取决于模型愿不愿意先读文件"。
    """
    section = _system_source()
    section = section[section.index("_SAVING_GATE_SECTION = ") : section.index("_SAVING_GATE_SECTION_MISSING = ")]
    assert "voucher_rules" in section, "系统提示词里没有 voucher_rules 门控"
    assert "地方消费券" in section, "门控没点明适用于地方消费券"
    # 必须写清触发条件是"以任何方式拿到条款", 否则只在开页面时才生效。
    assert "any means" in section, "没写清触发条件是'以任何方式'"
