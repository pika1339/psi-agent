"""`deploy/haitun/monitoring/cost.py` 的判据 —— 全部落在成本报告这一层。

## 判据为什么长这样

1. **判据必须落在它声称的那一层。** 本文件测的是报告层: 读真的 jsonl 文件、用真的单价表
   JSON、断言真的渲染输出。**不 import 内核** —— 报告层在宿主上跑, `import psi_agent` 在那
   里过不去, 一个 import 得进来的判据就不是在测生产的那条路。曾有 docstring 说测 AI 层却调
   Session 层函数, 连变异复核都照不出来。

2. **兜底链判据用仓库里不存在的资源名。** 目录名一律带 `nonexistent-` 前缀或用
   `.invalid` 保留名。用真名会让「目录在但内容不全」这类**提前终止**全绿通过 —— 踩过一次,
   `isdir` 判据全绿抓不到, 因为目录确实存在。

3. **「未测到」与「零」每条都分开断言。** 阴性用例断言 `status == UNKNOWN` / `amount is
   None` / `is_lower_bound`, 而不是「值为 0」。这两者混起来是本层最贵的失败模式。

## 单价表用仓库里的真表, 不造临时字典

「换一版单价表 → 金额不同且版本号随之变」这条判据必须换**真的第二份文件**, 否则测的是
测试自己造的字典, 而生产读的是 `pricing/` 目录。仓库里因此有 v1 与 v2 两版。
"""

from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_MON = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "monitoring"
_PRICING = _MON / "pricing"


def _load(name: str) -> Any:
    """按路径加载 —— 这些脚本住在 `deploy/` 下, 不属任何包。

    与 `tests/deploy/test_monitoring_delivery.py` 同款做法。返回标成 `Any`: 判据要替换
    模块属性, 而 `ModuleType` 上没有这些名字。
    """
    if str(_MON) not in sys.path:
        sys.path.insert(0, str(_MON))
    spec = importlib.util.spec_from_file_location(name, _MON / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cost = _load("cost")


# --------------------------------------------------------------------------
# 造数据的小工具
# --------------------------------------------------------------------------


def _turn(
    session_id: str = "s1",
    *,
    model: str | None = "deepseek-chat",
    reported: bool = True,
    prompt: int | None = 1000,
    cached: int | None = 0,
    completion: int | None = 100,
    reasoning: int | None = None,
    event: str = "turn",
) -> dict[str, object]:
    """一行 metrics jsonl。**字段名抄自内核实测**(`session/agent.py` 的
    `_usage_fields()` 与两处 `metrics.record()`), 不是照方案文档猜的。"""
    return {
        "ts": "2026-09-18T10:00:00+08:00",
        "event": event,
        "session_id": session_id,
        "usage_reported": reported,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": None if prompt is None else (prompt + (completion or 0)),
        "cached_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def _write_source(root: Path, day: str, rows: list[dict[str, object]]) -> None:
    metrics = root / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    with (metrics / f"{day}.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def v1() -> Any:
    return cost.load_pricing(directory=_PRICING, version="v1-2026-09-01")


# --------------------------------------------------------------------------
# 单价表
# --------------------------------------------------------------------------


def test_pricing_table_carries_version_and_effective_date() -> None:
    """仓库里每一版单价表都带 `version` 与 `effective_date`(W#8 的前半)。

    缺版本号的表会让「这个金额按哪版算的」永远答不上来, 于是历史不可比。
    """
    files = sorted(_PRICING.glob("*.json"))
    assert files, "pricing/ 目录里必须有单价表"
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["version"], f"{path.name} 缺 version"
        _dt.date.fromisoformat(raw["effective_date"])
        assert raw["currency"] and int(raw["unit_tokens"]) > 0
        for name, item in raw["models"].items():
            # 三类单价必须分开列。少一类就意味着那类 token 静默免费。
            assert {"prompt", "cached", "completion"} <= set(item), f"{path.name}:{name} 单价不全"


def test_cached_price_is_lower_than_prompt_price() -> None:
    """缓存命中单价必须**低于**普通 prompt 单价, 且两者不相等。

    相等的表会让「分开乘」这件事在算术上无从验证 —— 混算与分算得出同一个数, 于是下面那条
    混算判据会假绿。
    """
    for path in sorted(_PRICING.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for name, item in raw["models"].items():
            assert item["cached"] < item["prompt"], f"{path.name}:{name} 缓存单价没有更低"


def test_load_pricing_picks_latest_effective_not_later_than_date(v1: Any) -> None:
    """选版按 `effective_date`, 不按文件 mtime。

    mtime 区分不了「投放」与「就地编辑」, 拿它当判据两个方向都会误判。
    """
    early = cost.load_pricing(directory=_PRICING, on_date=_dt.date(2026, 9, 10))
    late = cost.load_pricing(directory=_PRICING, on_date=_dt.date(2026, 9, 20))
    assert early.version == "v1-2026-09-01"
    assert late.version == "v2-2026-09-15"
    assert early.version == v1.version


def test_missing_pricing_dir_raises_instead_of_empty_table() -> None:
    """单价表目录不存在时**抛异常**, 不兜底成空表。

    空表会让每个模型都落进「未定价」, 报出一份全是下限的报告 —— 读起来像上游没报 usage,
    把配置问题伪装成上游问题。

    目录名用仓库里不存在的名字, 不用真名。
    """
    with pytest.raises(cost.PricingError):
        cost.load_pricing(directory=_PRICING / "nonexistent-pricing-dir-xyz")


def test_named_version_that_does_not_exist_raises(tmp_path: Path) -> None:
    """指名一个不存在的版本要报错, 不许静默回落到别的版本。

    静默回落的后果: 报告打着「按旧价重算」的旗号, 实际用的是新价。
    """
    with pytest.raises(cost.PricingError, match="nonexistent-version"):
        cost.load_pricing(directory=_PRICING, version="nonexistent-version-v99")


def test_model_missing_a_price_class_raises(tmp_path: Path) -> None:
    """模型缺某一类单价时抛错, 不默认成 0。

    默认成 0 会让那一类 token 静默免费 —— 账单对不上, 而报告显示一切正常。
    """
    bad = tmp_path / "pricing"
    bad.mkdir()
    (bad / "v9-2026-01-01.json").write_text(
        json.dumps(
            {
                "version": "v9",
                "effective_date": "2026-01-01",
                "currency": "CNY",
                "unit_tokens": 1000000,
                "models": {"m": {"prompt": 1.0, "cached": 0.5}},  # 缺 completion
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(cost.PricingError, match="completion"):
        cost.load_pricing(directory=bad)


# --------------------------------------------------------------------------
# 算钱: 缓存分开乘
# --------------------------------------------------------------------------


def test_cached_tokens_priced_separately_from_uncached_prompt(v1: Any) -> None:
    """**分开乘**: 未命中部分用 prompt 单价, 命中部分用 cached 单价。

    这条判据钉死的是算术本身, 而不是「调了哪个函数」。混起来算(把整个 prompt_tokens 乘
    prompt 单价)的实现会被这条打红, 因为下面显式算出了两种结果并断言不相等。
    """
    price = v1.models["deepseek-chat"]
    unit = float(v1.unit_tokens)
    rc = cost.cost_of_row(_turn(prompt=10000, cached=8000, completion=500), v1)

    expected = (2000 * price.prompt + 8000 * price.cached + 500 * price.completion) / unit
    naive = (10000 * price.prompt + 500 * price.completion) / unit

    assert rc.amount == pytest.approx(expected)
    # 混算得出的是另一个数 —— 若实现混算了, 上一行就会失败。这一行保证两者确实可区分。
    assert naive != pytest.approx(expected)


def test_cached_exceeding_prompt_does_not_produce_negative_amount(v1: Any) -> None:
    """上游把 cached 报得比 prompt 大时, 未命中部分按 0 算, 不出负金额。

    负金额会悄悄冲掉别的回合的花费, 让总额低于真实值。
    """
    rc = cost.cost_of_row(_turn(prompt=100, cached=5000, completion=0), v1)
    assert rc.amount is not None and rc.amount >= 0.0


def test_reasoning_not_double_charged_when_inside_completion(v1: Any) -> None:
    """`reasoning_in_completion=true` 时 reasoning token 不单独计费。

    它是 `completion_tokens` 的明细项而非附加项; 再乘一遍等于同一批 token 收两遍钱。
    """
    without = cost.cost_of_row(_turn(prompt=100, cached=0, completion=500, reasoning=None), v1)
    with_r = cost.cost_of_row(_turn(prompt=100, cached=0, completion=500, reasoning=400), v1)
    assert with_r.amount == pytest.approx(without.amount)


def test_reasoning_charged_when_excluded_from_completion(tmp_path: Path) -> None:
    """反过来: 上游把 reasoning 排除在 completion 之外时, 它要单独乘。

    这条与上一条成对, 证明那个布尔开关真的在控制乘法, 而不是恒不计费。
    """
    d = tmp_path / "pricing"
    d.mkdir()
    (d / "v9-2026-01-01.json").write_text(
        json.dumps(
            {
                "version": "v9",
                "effective_date": "2026-01-01",
                "currency": "CNY",
                "unit_tokens": 1000,
                "models": {
                    "m": {
                        "prompt": 1.0,
                        "cached": 0.1,
                        "completion": 2.0,
                        "reasoning": 5.0,
                        "reasoning_in_completion": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    p = cost.load_pricing(directory=d)
    rc = cost.cost_of_row(_turn(model="m", prompt=1000, cached=0, completion=100, reasoning=200), p)
    assert rc.amount == pytest.approx((1000 * 1.0 + 100 * 2.0 + 200 * 5.0) / 1000)


# --------------------------------------------------------------------------
# 阴性: 没有 usage 的回合
# --------------------------------------------------------------------------


def test_row_without_usage_is_not_zero_but_none(v1: Any) -> None:
    """`usage_reported=false` 的行**算不出金额**, 返回 `None` 而不是 0.0。

    0.0 是「这个回合免费」的断言, 而真相是「花了多少未知」。两者混起来后, 当日总花费
    会在上游最不健康的时候显得最低 —— 观测缺口伪装成健康。
    """
    rc = cost.cost_of_row(_turn(reported=False, model=None, prompt=None, completion=None), v1)
    assert rc.amount is None
    assert rc.gap == "no_usage"
    # 显式排除「实现返回了 0.0」这种塌法。
    assert rc.amount != 0.0


def test_missing_usage_reported_key_treated_as_not_reported(v1: Any) -> None:
    """连 `usage_reported` 键都没有的行按「没报」处理。

    老版本埋点写的行不带这个键; 当成报了会凭 null token 算出 0 元。
    """
    row = _turn()
    del row["usage_reported"]
    row["prompt_tokens"] = None
    row["completion_tokens"] = None
    rc = cost.cost_of_row(row, v1)
    assert rc.amount is None and rc.gap == "no_usage"


def test_no_usage_turns_get_their_own_column_and_total_is_lower_bound(tmp_path: Path, v1: Any) -> None:
    """无 usage 的回合进**单独一栏**, 且当日总花费标成下限。

    这条同时验收「不得静默算作 0 元」与「要能让调用方打印 N 个回合无 usage」两句要求。
    """
    root = tmp_path / "gw"
    _write_source(
        root,
        "2026-09-18",
        [
            _turn("s1", prompt=1000, cached=0, completion=100),
            _turn("s2", reported=False, model=None, prompt=None, completion=None),
            _turn("s3", reported=False, model=None, prompt=None, completion=None),
        ],
    )
    totals = cost.summarize([cost.read_source("gateway", root)], v1)

    assert totals.total.no_usage == 2
    assert totals.total.turns == 3
    assert totals.is_lower_bound

    note = totals.lower_bound_note()
    assert "2 个回合无 usage" in note
    assert "下限" in note

    # 金额只含算得出的那一个回合, 但必须被标注成下限 —— 不是悄悄当成全部。
    assert totals.total.amount == pytest.approx(cost.cost_of_row(_turn("s1"), v1).amount)
    text = cost.render_cost_section(totals)
    assert "下限" in text and "2 个回合无 usage" in text


def test_unknown_model_counted_as_unpriced_not_free(tmp_path: Path, v1: Any) -> None:
    """单价表里没有的模型记「未定价」, 不当 0 元, 也不拿别的模型的价顶上。

    模型名用仓库单价表里**不存在**的名字。
    """
    rc = cost.cost_of_row(_turn(model="nonexistent-model-v99"), v1)
    assert rc.amount is None and rc.gap == "unpriced"

    root = tmp_path / "gw"
    _write_source(root, "2026-09-18", [_turn(model="nonexistent-model-v99")])
    totals = cost.summarize([cost.read_source("gateway", root)], v1)
    assert totals.total.unpriced == 1
    assert totals.unpriced_models == ["nonexistent-model-v99"]
    assert totals.is_lower_bound
    assert "未定价" in totals.lower_bound_note()


# --------------------------------------------------------------------------
# 阴性: 三份来源缺一份 / metrics 目录不存在
# --------------------------------------------------------------------------


def test_missing_appdata_root_reports_unknown_not_zero(tmp_path: Path) -> None:
    """appdata 根不存在 → UNKNOWN 带 reason, **不是 0 行**。

    路径用仓库里不存在的名字。
    """
    read = cost.read_source("luolin", tmp_path / "nonexistent-appdata-root-xyz")
    assert read.status == cost.UNKNOWN
    assert read.reason and "不存在" in read.reason
    assert read.rows == []


def test_metrics_dir_absent_reports_unknown_and_does_not_crash(tmp_path: Path, v1: Any) -> None:
    """**`metrics/` 目录整个不存在** → 报「未测到」并且不崩。

    这是首次部署前的必然状态。下一张卡的日报依赖这个行为: 崩掉的话第一次部署就触发
    「日报生成失败」, 而那条通知本该留给真故障。
    """
    root = tmp_path / "appdata"
    root.mkdir()  # 根在, metrics/ 不在 —— 兜底链的第二档
    read = cost.read_source("gateway", root)
    assert read.status == cost.UNKNOWN
    assert "metrics/" in read.reason

    # 整条链: collect 级别也不崩, 且渲染得出文本。
    totals = cost.summarize([read], v1)
    assert totals.is_lower_bound
    text = cost.render_cost_section(totals)
    assert "未测到" in text
    assert totals.pricing_version in text


def test_metrics_dir_exists_but_no_target_day_file_still_unknown(tmp_path: Path) -> None:
    """`metrics/` **在**但没有目标天文件 → 仍报「未测到」。

    与上一条刻意分成两个档: 「目录在但内容不全会让兜底链提前终止」踩过一次, `isdir`
    判据全绿抓不到, 因为目录确实存在。所以这里断言的是目录存在**且**结果仍是 UNKNOWN。
    """
    root = tmp_path / "appdata"
    (root / "metrics").mkdir(parents=True)
    assert (root / "metrics").is_dir()  # 与上一条的区别就在这里
    read = cost.read_source("gateway", root, days=[_dt.date(2026, 9, 18)])
    assert read.status == cost.UNKNOWN
    assert "没有目标天文件" in read.reason


def test_one_of_three_sources_missing_reports_unknown_not_zero(tmp_path: Path, v1: Any) -> None:
    """**三份里缺一份 → 报「未测到」而不是当 0。**

    缺的那份不许从报告里消失: 消失与「这人今天没用」不可区分, 而一人一容器意味着那是
    一整个人的账。
    """
    gw, luolin = tmp_path / "gw", tmp_path / "luolin"
    _write_source(gw, "2026-09-18", [_turn("s1")])
    _write_source(luolin, "2026-09-18", [_turn("s2")])
    sources = [
        ("gateway", str(gw)),
        ("luolin", str(luolin)),
        ("chengxx", str(tmp_path / "nonexistent-chengxx-root")),
    ]

    reads = cost.read_sources(sources, days=[_dt.date(2026, 9, 18)])
    assert len(reads) == 3, "缺的那份必须留一条记录, 不许跳过"

    totals = cost.summarize(reads, v1)
    unknown = totals.unknown_sources()
    assert [s.name for s in unknown] == ["chengxx"]
    assert totals.is_lower_bound, "有来源没读到时总额必须是下限"
    assert "1 个来源未测到(chengxx)" in totals.lower_bound_note()

    text = cost.render_cost_section(totals)
    assert "chengxx" in text and "未测到" in text
    # 缺的那份不许被渲染成一格 0 元。
    assert "chengxx: 0.0000" not in text


def test_sources_are_configurable_not_hardcoded_in_function_body() -> None:
    """来源路径可配置 —— 生产拓扑是过渡态, 写死等于每次搬家改代码。"""
    custom = cost.sources_from_env({"PSI_COST_APPDATA_ROOTS": "a=/tmp/nonexistent-a,b=/tmp/nonexistent-b"})
    assert custom == (("a", "/tmp/nonexistent-a"), ("b", "/tmp/nonexistent-b"))
    # 未配时回落到生产三个容器 —— 名字与实测的 bind mount 一致。
    assert [n for n, _ in cost.sources_from_env({})] == ["gateway", "luolin", "chengxx"]


def test_malformed_lines_counted_not_silently_dropped(tmp_path: Path, v1: Any) -> None:
    """坏行单独计数。一行坏掉意味着有花费算不进来, 而「少算了钱」与「花得少」在总额里
    长得一样。"""
    root = tmp_path / "gw"
    metrics = root / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "2026-09-18.jsonl").write_text(
        json.dumps(_turn("s1")) + "\n" + "{这不是 JSON\n" + "[1,2,3]\n",
        encoding="utf-8",
    )
    read = cost.read_source("gateway", root)
    assert read.status == cost.OK
    assert len(read.rows) == 1
    assert read.malformed == 2
    assert "2 行解析失败" in cost.render_cost_section(cost.summarize([read], v1))


# --------------------------------------------------------------------------
# 阴性: 换一版单价表
# --------------------------------------------------------------------------


def test_switching_pricing_version_changes_amount_and_printed_version(tmp_path: Path) -> None:
    """**换一版单价表 → 同一批 jsonl 算出不同金额, 且报告打印的版本号随之变。**

    两个断言都要: 只断言金额变, 版本号打印被删掉也照样绿; 只断言版本号变, 金额写死成
    常量也照样绿。
    """
    root = tmp_path / "gw"
    rows = [_turn("s1", prompt=10000, cached=4000, completion=800), _turn("s2", prompt=2000, completion=50)]
    _write_source(root, "2026-09-18", rows)
    reads = cost.read_sources([("gateway", str(root))], days=[_dt.date(2026, 9, 18)])

    p1 = cost.load_pricing(directory=_PRICING, version="v1-2026-09-01")
    p2 = cost.load_pricing(directory=_PRICING, version="v2-2026-09-15")
    t1 = cost.summarize(reads, p1)
    t2 = cost.summarize(reads, p2)

    assert t1.total.amount != pytest.approx(t2.total.amount), "换版后金额必须变"
    assert t1.pricing_version == "v1-2026-09-01"
    assert t2.pricing_version == "v2-2026-09-15"

    text1 = cost.render_cost_section(t1)
    text2 = cost.render_cost_section(t2)
    assert "v1-2026-09-01" in text1 and "v2-2026-09-15" not in text1
    assert "v2-2026-09-15" in text2 and "v1-2026-09-01" not in text2
    # 金额本身也要出现在文本里 —— 否则「打印带版本号」可以靠一行抬头糊过去。
    assert f"{t1.total.amount:.4f}" in text1
    assert f"{t2.total.amount:.4f}" in text2


def test_every_printed_report_carries_version_and_not_reconciled(tmp_path: Path, v1: Any) -> None:
    """任何成本数字打印时都带单价表版本号, 且带「未与账单对过」。

    本期不做对账(要登上游控制台, 无法自动化, 负责人已定), 这行字是唯一的诚实性保障。
    """
    root = tmp_path / "gw"
    _write_source(root, "2026-09-18", [_turn("s1")])
    totals = cost.summarize([cost.read_source("gateway", root)], v1)
    text = cost.render_cost_section(totals)
    assert v1.version in text
    assert cost.NOT_RECONCILED in text
    assert "未与账单对过" in text

    # 空报告(什么都没采到)也必须带这两样 —— 那正是最容易被读成「没花钱」的一份。
    empty = cost.summarize([], v1)
    empty_text = cost.render_cost_section(empty)
    assert v1.version in empty_text and cost.NOT_RECONCILED in empty_text


# --------------------------------------------------------------------------
# 归因维度
# --------------------------------------------------------------------------


def test_compaction_attributed_separately_from_normal_turns(tmp_path: Path, v1: Any) -> None:
    """压缩独立归因, 不并进触发它的回合(W#7)。

    实测压缩 41.5s x 22 次; 并进去则单回合成本看着便宜而月账单对不上。
    """
    root = tmp_path / "gw"
    _write_source(
        root,
        "2026-09-18",
        [
            _turn("s1", prompt=1000, completion=100),
            _turn("s1", prompt=50000, completion=800, event="compaction"),
        ],
    )
    totals = cost.summarize([cost.read_source("gateway", root)], v1)

    assert set(totals.by_use) == {cost.USE_TURN, cost.USE_COMPACTION}
    assert totals.by_use[cost.USE_COMPACTION].compactions == 1
    assert totals.by_use[cost.USE_TURN].turns == 1
    # 压缩那行贵得多 —— 并进回合就看不出来了。
    assert totals.by_use[cost.USE_COMPACTION].amount > totals.by_use[cost.USE_TURN].amount
    assert "压缩" in cost.render_cost_section(totals)


def test_four_attribution_dimensions_each_sum_to_the_total(tmp_path: Path, v1: Any) -> None:
    """四个维度各自加起来都等于总额 —— 不等说明有行没被归进任何一格。"""
    gw, luolin = tmp_path / "gw", tmp_path / "luolin"
    _write_source(gw, "2026-09-18", [_turn("s1"), _turn("s2", event="compaction")])
    _write_source(luolin, "2026-09-18", [_turn("s3", prompt=7777, cached=1000)])
    totals = cost.summarize(cost.read_sources([("gateway", str(gw)), ("luolin", str(luolin))]), v1)

    for name, store in (
        ("by_container", totals.by_container),
        ("by_session", totals.by_session),
        ("by_model", totals.by_model),
        ("by_use", totals.by_use),
    ):
        assert sum(b.amount for b in store.values()) == pytest.approx(totals.total.amount), name

    assert set(totals.by_container) == {"gateway", "luolin"}
    assert set(totals.by_session) == {"s1", "s2", "s3"}


def test_unrelated_events_are_ignored(tmp_path: Path, v1: Any) -> None:
    """不认识的 event 不进汇总。内核不做 event 白名单, 所以过滤落在本层。"""
    root = tmp_path / "gw"
    _write_source(root, "2026-09-18", [_turn("s1"), {"event": "nonexistent-event-kind", "session_id": "s9"}])
    totals = cost.summarize([cost.read_source("gateway", root)], v1)
    assert totals.total.turns == 1
    assert "s9" not in totals.by_session


def test_bool_token_count_rejected(v1: Any) -> None:
    """JSON `true` 不许变成 1 个 token。`bool` 是 `int` 子类, 不显式拒就会混进计数。"""
    rc = cost.cost_of_row(_turn(prompt=True, cached=0, completion=0), v1)  # type: ignore[arg-type]
    assert rc.prompt_tokens is None


# --------------------------------------------------------------------------
# 端到端: 命令行入口
# --------------------------------------------------------------------------


def test_cli_prints_version_and_exits_zero_when_nothing_measured(tmp_path: Path) -> None:
    """端到端跑 `monthly_cost.py`: 三份全缺时**退出码 0**, 输出带版本号与「未测到」。

    这条落在进程层而不是函数层 —— 日报那侧是 `python3 monthly_cost.py` 地跑, 而「首次
    部署前 metrics/ 不存在」必须走通这条真路径, 不能只在函数里验。
    """
    env = {
        **_clean_env(),
        "PSI_COST_APPDATA_ROOTS": ",".join(
            f"{n}={tmp_path / f'nonexistent-{n}-root'}" for n in ("gateway", "luolin", "chengxx")
        ),
    }
    proc = subprocess.run(
        [sys.executable, str(_MON / "monthly_cost.py"), "--date", "2026-09-18"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_MON),
    )
    assert proc.returncode == 0, proc.stderr
    assert "未测到" in proc.stdout
    assert "未与账单对过" in proc.stdout
    assert "v" in proc.stdout


def test_cli_exits_nonzero_when_pricing_unreadable(tmp_path: Path) -> None:
    """单价表读不出来时**非零退出**, 不报一份全是下限的报告。

    异常照旧往外传这件事需要自己的判据: 曾有一次变异四条全绿, 暴露的正是「异常往外传」
    没人盯。cron 那层靠非零退出发「生成失败」。
    """
    env = {**_clean_env(), "PSI_COST_PRICING": "nonexistent-version-v99"}
    proc = subprocess.run(
        [sys.executable, str(_MON / "monthly_cost.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_MON),
    )
    assert proc.returncode == 2
    assert "单价表" in proc.stderr


def _clean_env() -> dict[str, str]:
    """跑子进程用的干净环境。

    显式剔掉本层的两个环境变量: 开发机上若恰好设了, 判据会静默量到另一份数据。
    """
    return {k: v for k, v in os.environ.items() if k not in ("PSI_COST_PRICING", "PSI_COST_APPDATA_ROOTS")}


def test_upstream_input_gaps_are_documented_not_silently_patched() -> None:
    """埋点缺口只记录、不在本层偷偷补 —— 分层立场的判据。

    本层若自己往 jsonl 里补金额字段, 分层就名存实亡了。这条断言那份缺口说明存在且非空。
    """
    assert isinstance(cost.MISSING_UPSTREAM_INPUTS, tuple)
    assert all(isinstance(s, str) and s for s in cost.MISSING_UPSTREAM_INPUTS)


def test_cost_module_does_not_import_kernel() -> None:
    """本层**不 import 内核**: 内核在容器里, 报告在宿主, import 过不去。

    判据走 `ast` 而不是试 import, 也不是正则: 在这个仓库里 `psi_agent` 恰好 import 得进来
    (测试跑在仓库里), 所以「试一下能不能 import」在这里恒绿, 量不到生产的那条路; 而正则
    会把 docstring 里解释「为什么不 import 内核」的那句散文当成真的 import 语句 ——
    「改名自查正则漏掉分段拼接路径」是同一类坑的另一面。`ast` 看的是真的 import 节点。
    """
    for name in ("cost.py", "monthly_cost.py"):
        tree = ast.parse((_MON / name).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offenders = [m for m in imported if m == "psi_agent" or m.startswith("psi_agent.")]
        assert not offenders, f"{name} import 了内核: {offenders}"
