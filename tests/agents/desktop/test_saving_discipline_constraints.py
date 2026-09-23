"""阶段三「约束纪律」的三条规则必须留在 saving-decision 规则集里。

为什么要钉这三条: 它们都不是新功能, 而是**人工复核**(2026-09-22, 用户逐题结论)指出的三处真实
缺口。三者的共同形状是「答案里数字全对、结论也站得住, 但少了一句该说的话」—— 所以既不报错,
也不会被工具调用计数发现, 只能靠规则集里那条纪律在不在来保证:

* **预算约束**: T22 在 6000 元预算下推荐了 6155 元的机器, **没提超预算**。每个数字都对, 但用户
  被推荐了一台超出预算的机器而不知情。
* **需求闭环**: T10 结论正确(二手不进国补), 但没给下一步路径, 用户读完不知道该做什么。
* **叠加口径**: A2-15 断言「结算价 >= 10000 时先扣券**不再减少**国补」。资料卡写明基数是
  「扣完优惠后的成交价」(`fact-cards/guobu-2026.yaml` 的 ``price_basis``), 于是扣券把基数压到
  封顶门槛以下时国补**确实会减少** —— 那条断言在边界上不成立, 而它被写成了无条件规则。

第 10/11/12 条的编号与开头那句计数是**一件事的两半**: 本次改动前那句写着 "all 8 below apply",
而实际有 9 条 —— 计数早就漂了且没人发现, 因为没有任何东西同时看这两处。所以下面第一条判据把
计数与编号一起钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "agents" / "desktop" / "skills" / "saving-decision" / "SKILL.md"


def _skill_text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _slide(text: str, start_marker: str, end_marker: str) -> str:
    """取两个标记之间的内容 —— 标记缺失时 ``index`` 直接抛, 不静默退化成空串。"""
    return text[text.index(start_marker) : text.index(end_marker)]


def _constraints_block() -> str:
    return _slide(_skill_text(), "[Mandatory Constraints]", "[Execution Strategy]")


def test_the_declared_constraint_count_matches_the_numbered_constraints() -> None:
    """开头声明的条数必须等于实际编号的条数。

    这是本文件里唯一一条**结构性**判据: 它不看规则内容, 只看「声称几条」与「写了几条」是否
    一致。漂了的计数比缺一条规则更难发现 —— 读者会以为后面还有, 而写规则的人会以为前面没写。
    """
    block = _constraints_block()
    declared_match = re.search(r"all (\d+) below apply", block)
    assert declared_match is not None, "开头没有声明约束条数"
    declared = int(declared_match.group(1))

    numbered = [int(n) for n in re.findall(r"^(\d+)\. In saving tasks", block, re.MULTILINE)]
    assert numbered == list(range(1, declared + 1)), f"开头声明 {declared} 条, 实际编号 {numbered} —— 两者必须逐条对齐"


def test_budget_discipline_is_stated() -> None:
    """推荐类任务必须显式声明是否在预算内, 超预算须明说并给替代。"""
    block = _constraints_block()
    assert "fits the budget the user actually gave" in block, "没要求显式声明是否在用户预算内"
    assert "within-budget alternative" in block, "超预算时没要求给预算内的替代"


def test_closure_discipline_is_stated() -> None:
    """结论之外必须给下一步可执行动作, 且 [Final Output] 的结构里要有这一项。"""
    block = _constraints_block()
    assert "close the loop" in block, "没要求给出下一步可执行动作"

    text = _skill_text()
    final_output = _slide(text, "[Final Output]", "[Tool Usage Guide]")
    assert "next step" in final_output, "[Final Output] 的结构里没有下一步这一项"


def test_stacking_is_a_rules_question_and_the_base_is_post_coupon() -> None:
    """叠加口径: 基数在扣券之后 + 可叠性由规则决定(不许任一侧的默认)。"""
    block = _constraints_block()
    assert "post-coupon" in block, "没写明国补基数是扣券后的成交价"
    assert "先扣券不再减少国补" in block, "没点名那条在边界上不成立的绝对化断言"
    assert "10000" in block, "没给出封顶门槛这个条件(15% / 1500 -> 10000)"
    assert "大概率不能叠" in block, "没点名那条无依据的悲观默认"

    local = _slide(_skill_text(), "## Local Consumption Vouchers", "## Saving Scenario Checklist")
    assert "叠加" in local, "地方券小节没有记叠加口径 —— A2 题读到的正是这一节"


def test_annotation_vocabulary_is_english_only() -> None:
    """标注词表统一为英文五件套(2026-09-22 决定)。

    为什么单独钉: 原先**两个提示词在教两套** —— saving-decision 的 SKILL.md 教英文
    (`[Confirmed]` / `[Inferred]` / ...), 而 system.py 的「强制监督规则」教中文
    (`[已确认]` / `[推断]` / `[需验证]`)。实测代价: 答案是中英混用的
    (A2-10 同时出现 `[Cannot Confirm]` 与 `[已确认]` / `[需验证]`),
    而判分器当时只认中文词 —— 英文标注等于白标。

    所以这条同时钉三件事: 词表在、写死「只许英文」、以及**提示词模板里不许再教中文标注**。
    """
    text = _skill_text()
    assert "[Annotation Vocabulary" in text, "SKILL.md 没有统一的标注词表那一节"
    vocab = _slide(text, "[Annotation Vocabulary", "[Scenario Isolation]")
    assert "English only" in vocab, "没写死「只许英文」"
    for tag in ("[Confirmed]", "[Inferred]", "[Pending Verification]", "[Unverified]", "[Cannot Confirm]"):
        assert tag in vocab, f"词表缺 {tag}"
    for zh in ("已确认", "推断", "需验证", "未验证", "无法确认"):
        assert zh in vocab, f"词表没点名禁止中文等价词 {zh}"

    # 提示词模板里不得再教中文标注 —— 这里才是中英混用的源头。
    for pkg in ("desktop", "feishu"):
        src = (_REPO / "agents" / pkg / "systems" / "system.py").read_text(encoding="utf-8")
        for zh in ("`[已确认]`", "`[推断]`", "`[需验证]`"):
            assert zh not in src, f"{pkg}/system.py 仍在教中文标注 {zh}"
