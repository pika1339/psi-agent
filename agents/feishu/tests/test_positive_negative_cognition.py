"""正负面清单的认知标准: 本体在 cognition.yaml, 不在代码里。

背景: 2026-09-21 的正负面清单报告是"正 vs 负"的对立叙事, 而 08-22 全员会《之关系认知》
定的口径是 —— 清单是关系的"行为翻译", 正向值得被看见、负向需要被纠正, **两者都是同一个人的
成长记录**。把这段认知写进 skill 与 tool 的代码里, 等于每次改口径都要发一版; 所以它住在
技能包的 cognition.yaml, 由工具读取并带指纹返回。

这些用例钉住三件事: 口径确实来自那份 YAML; 工具把它交到模型手里; 代码里没有第二份。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

COGNITION = TOOLS_DIR.parent / "skills" / "positive-negative-list" / "cognition.yaml"

from _positive_negative_list.models import LedgerRecord  # noqa: E402  # ty: ignore[unresolved-import]

#: 认知必须覆盖的三个方向。少一个方向就是口径不完整。
DIRECTIONS = ("positive", "negative", "red_line")


def _python_sources() -> list[Path]:
    """会被扫描的代码文件: 工具包与提示词段。

    刻意扫**整棵** tools/ 与 systems/, 不逐个点名文件 —— 点名的话, 下次有人在某个新工具或
    新的提示词段里抄一段口径, 就正好从这条判据底下走过去, 而"抄一段口径"正是本次要防的形态。
    """
    roots = (TOOLS_DIR, TOOLS_DIR.parent / "systems")
    return [path for root in roots for path in sorted(root.rglob("*.py"))]


def test_cognition_pack_reads_the_skill_yaml() -> None:
    rules = importlib.import_module("_positive_negative_list.rules")
    pack = rules.load_cognition_pack()

    assert pack.version
    assert pack.source_document["url"].endswith("Shc6dwJGgoFSIkxv4mHcqnUnnnh")
    for direction in DIRECTIONS:
        entry = pack.reading[direction]
        assert entry["translates"], f"{direction} 少了行为翻译"
        assert entry["organizational_response"], f"{direction} 少了组织回应"
        assert entry["growth"], f"{direction} 少了成长读法"
    assert pack.ledger_discipline and pack.report_rules and pack.stance
    assert pack.premise and pack.order and pack.disclosure


def test_both_directions_are_read_as_one_persons_growth() -> None:
    """本次要修的就是这一条: 负向记的同样是成长值, 不是"罪状"也不是处罚。

    只写正向怎么读、把负向留成"需要纠正"就完了, 报告还是会写成表扬与批评的二分 —— 所以
    负向的成长读法是硬断言, 不是措辞偏好。
    """
    rules = importlib.import_module("_positive_negative_list.rules")
    pack = rules.load_cognition_pack()

    assert "成长" in pack.reading["positive"]["growth"]
    assert "成长" in pack.reading["negative"]["growth"]
    assert any("成长" in rule for rule in pack.report_rules)
    # 红线是这条线的例外: 信任彻底破裂, 由人工处理, 不算成长记录。
    assert "红线" in pack.reading["red_line"]["label"]


def test_cognition_source_fingerprint_matches_the_file() -> None:
    rules = importlib.import_module("_positive_negative_list.rules")
    source = rules.cognition_source()
    raw = COGNITION.read_bytes()

    assert source["file"] == "skills/positive-negative-list/cognition.yaml"
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()[:12]
    assert source["bytes"] == len(raw)
    # 未声明分层时命中老落点, 与规则包同一口径。
    assert source["layer"] == "legacy"


def test_cognition_text_is_not_duplicated_in_code() -> None:
    """口径正文只能有一份: 代码里出现同样的句子, 就是认知标准又被写死了一次。

    判据取自 YAML 自己解析出来的每一条正文, 不在用例里手抄 —— 手抄的清单会随 YAML 增删而
    悄悄漏掉新增的那几条。
    """
    rules = importlib.import_module("_positive_negative_list.rules")
    pack = rules.load_cognition_pack()
    probes = [
        pack.premise,
        pack.order,
        pack.disclosure,
        *(entry["growth"] for entry in pack.reading.values()),
        *pack.ledger_discipline,
        *pack.report_rules,
        *pack.notice.values(),
        *(item["answer"] for item in pack.stance),
    ]
    sources = {path: path.read_text(encoding="utf-8") for path in _python_sources()}

    for probe in probes:
        assert probe.strip(), "空的条目会让这条判据形同虚设"
        for path, text in sources.items():
            assert probe not in text, f"认知正文被写死在 {path} 里, 它只应存在于 cognition.yaml"


def test_meeting_snapshot_carries_the_cognition_section() -> None:
    """会议链路注入的是 references/analysis_rules.md, 而会议概览就是周期性的正负面报告。

    自动化上下文没有规则查询工具, 所以认知必须**内联**在这份快照里 —— 快照少了这一节,
    概览就会写回"正 vs 负"的对立叙事, 这正是本次要修的那个形态。
    """
    snapshot = TOOLS_DIR.parent / "skills" / "positive-negative-list" / "references" / "analysis_rules.md"
    text = snapshot.read_text(encoding="utf-8")

    assert "## 认知标准" in text
    assert "成长" in text
    # 快照要指出本体在哪, 否则改口径的人不知道还有第二处要同步。
    assert "cognition.yaml" in text


def test_reference_config_points_at_the_cognition_pack() -> None:
    """配置里的出处链接与包路径必须是活的, 免得指向一个不存在的包。"""
    config_path = TOOLS_DIR.parent / "config" / "positive-negative-list.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = config["documents"]["cognition"]

    assert entry["url"].endswith("Shc6dwJGgoFSIkxv4mHcqnUnnnh")
    assert (TOOLS_DIR.parent / entry["pack"]).is_file(), "配置指向的认知包不存在"


def test_rules_tool_hands_the_cognition_to_the_model() -> None:
    """规则与认知一次返回, 不留"只查了规则、没查口径"的捷径。"""
    rules = importlib.import_module("_positive_negative_list.rules")
    rules_tool = importlib.import_module("positive_negative_rules")
    payload = json.loads(asyncio.run(rules_tool.positive_negative_rules("及时反馈")))

    assert payload["ok"] is True
    assert payload["match_count"] >= 1
    assert payload["cognition"]["reading"]["negative"]["growth"]
    assert payload["cognition"]["report_rules"]
    assert payload["cognition_source"] == rules.cognition_source()


def test_analyze_tool_returns_the_cognition_view_without_internal_metadata() -> None:
    """汇总返回里要带面向用户的口径, 但不能把内部标识一起带出去。"""
    analyze = importlib.import_module("positive_negative_case_analyze")
    rules = importlib.import_module("_positive_negative_list.rules")
    records = json.dumps(
        [
            {
                "record_id": "rec_1",
                "fields": {
                    "事件描述": "未及时同步延期风险",
                    "正负面归属": "负面清单",
                    "员工姓名": "XXX",
                    "记录日期": "2026-08-28",
                    "填写人": "报告人",
                },
            },
            {
                "record_id": "rec_2",
                "fields": {
                    "事件描述": "主动补位完成闭环",
                    "正负面归属": "正面清单",
                    "员工姓名": "XXX",
                    "记录日期": "2026-08-29",
                    "填写人": "报告人",
                },
            },
        ],
        ensure_ascii=False,
    )
    payload = json.loads(asyncio.run(analyze.positive_negative_case_analyze(records_json=records)))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["ok"] is True
    view = payload["认知口径"]
    assert view["正向清单"]["growth"] and view["负面清单"]["growth"]
    assert view["报告怎么写"]
    assert payload["说明"] == rules.load_cognition_pack().disclosure
    for token in ("6.0-shadow", "cognition.yaml", "sha256", "version", "pn-", "source_document"):
        assert token not in serialized, f"{token} 不该出现在面向用户的返回里"


def test_notice_copy_comes_from_the_cognition_pack() -> None:
    """员工侧通知卡的说辞必须来自 cognition.yaml。

    卡片与通知文本都由代码确定性渲染, 拿不到 ``positive_negative_rules`` 的返回, 所以这一句
    是它们唯一的口径来源。负面卡此前只列"正确做法 / 立即补救 / 预防措施"三条 —— 员工收到的
    读起来就是一张罚单, 而不是一条成长记录。
    """
    notifications = importlib.import_module("_positive_negative_list.notifications")
    rules = importlib.import_module("_positive_negative_list.rules")
    pack = rules.load_cognition_pack()
    base = {
        "record_id": "rec_1",
        "occurred_at": "2026-09-01",
        "category": "工作方式方法",
        "fact_summary": "方案确定后未倒排",
    }
    negative_growth = pack.notice_line("negative")
    positive_growth = pack.notice_line("positive")
    assert "成长" in negative_growth, "负面那句不带成长框架, 就又成了罚单"

    negative = json.dumps(
        notifications.render_record_notice_card(LedgerRecord.from_mapping(base | {"nature": "negative"}), "王炜博"),
        ensure_ascii=False,
    )
    positive = json.dumps(
        notifications.render_record_notice_card(LedgerRecord.from_mapping(base | {"nature": "positive"}), "王炜博"),
        ensure_ascii=False,
    )
    negative_text = notifications._record_notice_text(LedgerRecord.from_mapping(base | {"nature": "negative"}))

    assert negative_growth in negative
    assert positive_growth in positive
    assert negative_growth in negative_text
    # 成长框架要排在整改三条之前, 否则读者先看到的是一份问题清单。
    assert negative.index(negative_growth) < negative.index("正确做法")


def test_missing_cognition_pack_fails_closed(monkeypatch, tmp_path) -> None:
    """口径缺失时不给兜底默认值。

    兜底会让工具带着一份代码里自造的认知继续跑, 而那正是这次要修的缺陷。失败关闭至少让
    缺口径这件事变得可见。
    """
    rules = importlib.import_module("_positive_negative_list.rules")
    skill_dir = tmp_path / "skills" / "positive-negative-list"
    skill_dir.mkdir(parents=True)
    shutil.copyfile(rules._rule_pack_path(rules.DEFAULT_VERSION), skill_dir / f"{rules.DEFAULT_VERSION}.yaml")
    monkeypatch.setattr(rules, "_config_dirs", lambda: [skill_dir])

    with pytest.raises(ValueError, match="cognition pack unavailable"):
        rules.load_cognition_pack()

    rules_tool = importlib.import_module("positive_negative_rules")
    payload = json.loads(asyncio.run(rules_tool.positive_negative_rules("及时反馈")))
    assert payload["ok"] is False
    assert payload["error"] == "cognition pack unavailable"
    # 错误会原样进 agent 的输出, 服务器目录结构不该跟着出去。
    assert "/" not in payload["error"]
    assert "\\" not in payload["error"]
