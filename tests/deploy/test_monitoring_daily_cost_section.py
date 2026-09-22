"""第 3 类指标 + 成本一节接进日报的判据。

## 验收门槛在 `test_missing_usage_never_counted_as_zero`

本卡的门槛判据: **造一批缺 usage 的记录 → 日报必须报「未测到」而不是把总花费算低。**
它断言三件事同时成立: 缺 usage 的回合有独立计数、总额标成下限、报告正文出现「下限」字样。
少任何一件, 缺 usage 就会以「今天花得少」的形式进报告。

## 判据落在它声称的那一层

本文件测**报告层**: 写真的 jsonl 文件到临时目录、用 `pricing/` 里真的单价表、断言真的渲染
输出字符串。**不 import 内核** —— 报告层在宿主跑, `import psi_agent` 在那儿过不去, 一个
import 得进来的判据就不是在测生产走的那条路。

## 兜底链判据用仓库里不存在的资源名

目录名一律 `nonexistent-` 前缀。用真名会让「目录在但内容不全」这类**提前终止**全绿通过 ——
踩过一次: `isdir` 判据全绿抓不到, 因为目录确实存在。

## 「未测到」与「零」每条分开断言

阴性用例断言 `status == UNKNOWN`, 不断言「值为 0」。观测缺口伪装成健康是这一层最贵的失败
模式 —— 量 `live render` 得 0 行那次就是这样错过去的。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_MON = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "monitoring"
_PRICING = _MON / "pricing"


def _load(name: str) -> Any:
    """按路径加载 —— 这些脚本住在 `deploy/` 下, 不属任何包。"""
    if str(_MON) not in sys.path:
        sys.path.insert(0, str(_MON))
    spec = importlib.util.spec_from_file_location(name, _MON / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cost = _load("cost")
findings_mod = _load("findings")
probes_cost = _load("probes_cost")
probes_spend = _load("probes_spend")
render_mod = _load("render")
run_mod = _load("run")

UNKNOWN = findings_mod.UNKNOWN
BAD = findings_mod.BAD
OK = findings_mod.OK

DAY = "2026-09-18"


def _turn(
    *,
    session_id: str = "s1",
    model: str | None = "deepseek-chat",
    reported: bool = True,
    prompt: int | None = 1000,
    cached: int | None = 0,
    completion: int | None = 100,
    req_bytes: int | None = 120_000,
    ttft_s: float | None = 1.5,
    tools_exposed: int | None = 66,
    tools_total: int | None = 232,
    duration_s: float | None = 3.0,
    event: str = "turn",
) -> dict[str, object]:
    """一行 metrics jsonl。**字段名抄自内核实测**, 不照方案文档猜。

    注意首字延迟字段是 `ttft_s` —— 方案文档写的是 `ttft`, 而 `session/agent.py` 实际落的
    带 `_s` 后缀。按文档那个名字读会一条样本都取不到, 然后报成「未测到」。
    """
    row: dict[str, object] = {
        "ts": f"{DAY}T10:00:00+08:00",
        "event": event,
        "session_id": session_id,
        "usage_reported": reported,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": None if prompt is None else prompt + (completion or 0),
        "cached_tokens": cached,
        "reasoning_tokens": None,
        "duration_s": duration_s,
    }
    if event == "turn":
        row["req_bytes"] = req_bytes
        row["ttft_s"] = ttft_s
        row["tools_exposed"] = tools_exposed
        row["tools_total"] = tools_total
    return row


def _today() -> str:
    """今天的天名。

    端到端判据必须用**今天**的文件名: `run._collect_cost()` 按 `days=[today]` 读, 这是生产
    实际走的那条路。改成 patch 掉 `read_sources` 让它读全部天文件反而会绕开这段逻辑 —— 而
    `run.cost` 与本文件的 `cost` 是同一个模块对象, 那种 patch 还会让 lambda 递归调用自己
    (实测 RecursionError, 且被 `_guard` 吞成一条「未测到」, 看起来像实现有 bug)。
    """
    return _dt.date.today().isoformat()


def _write(root: Path, rows: list[dict[str, object]], *, day: str = DAY) -> None:
    metrics = root / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    with (metrics / f"{day}.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _reads(*roots: Path) -> list[Any]:
    return [cost.read_source(p.name, p) for p in roots]


@pytest.fixture
def pricing() -> Any:
    return cost.load_pricing(directory=_PRICING, version="v2-2026-09-15")


def _by_name(items: list[Any], name: str) -> Any:
    for item in items:
        if item.name == name:
            return item
    raise AssertionError(f"没有名为 {name!r} 的指标; 实际有 {[i.name for i in items]}")


# ==========================================================================
# 验收门槛: 缺 usage 不得静默算作 0 元
# ==========================================================================


def test_missing_usage_never_counted_as_zero(tmp_path: Path, pricing: Any) -> None:
    """**本卡的门槛判据。** 造一批缺 usage 的记录 → 报「未测到」而不是把总花费算低。

    对照组同时在场: 6 个正常回合 + 4 个缺 usage 回合。若缺 usage 被当 0 元, 总额会等于
    只有 6 个回合的那个数, 而报告读起来一切正常 —— 那正是「观测缺口伪装成健康」。
    """
    root = tmp_path / "gateway"
    good = [_turn(session_id=f"ok{i}") for i in range(6)]
    missing = [
        _turn(session_id=f"gap{i}", reported=False, model=None, prompt=None, completion=None, cached=None)
        for i in range(4)
    ]
    _write(root, good + missing)

    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    # 1. 缺 usage 的回合被单独计数, 没有被算进金额。
    assert totals.total.no_usage == 4, "缺 usage 的回合必须单独计数"
    assert totals.total.turns == 10

    # 2. 总额是下限, 不是等号。
    assert totals.is_lower_bound is True
    assert "4 个回合无 usage" in totals.lower_bound_note()

    # 3. 金额恰好等于那 6 个算得出的回合 —— 缺的 4 个既没被当 0 混进去充数,
    #    也没让整个总额塌成 None。
    only_good = cost.summarize(_reads_of_rows(tmp_path / "onlygood", good), pricing)
    assert totals.total.amount == pytest.approx(only_good.total.amount)
    assert only_good.is_lower_bound is False, "对照组必须不是下限, 否则上面那条等式没有意义"

    # 4. 占比指标报 BAD 且明说总花费是下限 —— 不是 OK 也不是静默的 0。
    share = _by_name(probes_spend.collect(totals, reads), "未测到 usage 回合占比")
    assert share.status == BAD, "有回合缺 usage 时占比指标必须报异常"
    assert "4/10" in share.value
    assert "下限" in share.semantics

    # 5. 渲染出的正文里「下限」字样在场。报告是给人读的, 前四条全对而正文不写下限,
    #    读者仍然会把那个金额当成当日总花费。
    body = cost.render_cost_section(totals)
    assert "下限" in body
    total_line = _by_name(probes_spend.collect(totals, reads), "当日总花费")
    assert total_line.value.startswith("≥"), f"下限成立时必须标 ≥, 实际 {total_line.value!r}"


def _reads_of_rows(root: Path, rows: list[dict[str, object]]) -> list[Any]:
    _write(root, rows)
    return _reads(root)


def test_no_usage_share_is_ok_only_when_every_row_has_usage(tmp_path: Path, pricing: Any) -> None:
    """正向对照: 全部带 usage 时占比才是 OK 且不标下限。

    没有这条, 上面那条门槛判据可能因为「这个指标恒为 BAD」而通过。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(session_id=f"s{i}") for i in range(5)])
    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    assert totals.is_lower_bound is False
    share = _by_name(probes_spend.collect(totals, reads), "未测到 usage 回合占比")
    assert share.status == OK
    assert share.value.startswith("0.0%")
    assert cost.render_cost_section(totals).count("下限") == 0


def test_unpriced_model_also_marks_lower_bound(tmp_path: Path, pricing: Any) -> None:
    """模型不在单价表里也算下限 —— 用**仓库单价表里不存在的模型名**。

    `usage_reported=true` 但模型未定价时金额同样算不出。若只看 `usage_reported`, 这批回合
    会被当成 0 元静默吃掉。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(model="nonexistent-model-v9"), _turn()])
    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    assert totals.total.unpriced == 1
    assert totals.is_lower_bound is True
    assert "nonexistent-model-v9" in totals.lower_bound_note()
    share = _by_name(probes_spend.collect(totals, reads), "未测到 usage 回合占比")
    assert share.status == BAD, "有未定价回合时同样必须报异常"


# ==========================================================================
# tools_exposed=N of M 的比值
# ==========================================================================


def test_exposed_equals_total_reports_narrowing_not_in_effect(tmp_path: Path) -> None:
    """N == M 且 M 是全量 → 必须报「收窄未生效」。

    `232 of 232` 是实际发生过的那个数: 收窄机制上线后生产从来没有 EXPOSED.txt, 每层都走
    「未声明」分支暴露全量, 而日志同时汇报「机制已启用」。**光看 N 看不出来** —— 232 这个
    数字本身不越任何基线, 只有拿它跟 M 比才是异常。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=232, tools_total=232)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")

    assert item.status == BAD
    assert "收窄未生效" in item.value
    assert "232 of 232" in item.value
    # 报告必须把「日志说已启用」这件事写进去, 否则读者会拿那行日志推翻这条指标。
    assert "机制已启用" in item.semantics


def test_exposed_above_baseline_reports_anomaly(tmp_path: Path) -> None:
    """阴性: N != M 但 N 高于基线 → 仍报异常, 且与「收窄未生效」区分开。

    两者的修法不同(补清单文件 vs 调分层规则), 所以措辞必须不一样。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=150, tools_total=232)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")

    assert item.status == BAD
    assert "150 of 232" in item.value
    assert "收窄未生效" not in item.value, "N < M 时不该说完全未生效 —— 那会指向错误的修法"


def test_exposed_at_baseline_is_ok(tmp_path: Path) -> None:
    """正向对照: N == 66 且 N < M 时为 OK。

    没有这条, 上面两条可能因为「这个指标恒为 BAD」而通过。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=66, tools_total=232)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")
    assert item.status == OK
    assert "66 of 232" in item.value


def test_exposed_equals_total_is_bad_even_at_the_expected_count(tmp_path: Path) -> None:
    """`66 of 66` 也必须报异常 —— **这条是变异复核补出来的。**

    变异「只看 N 不比 N/M」时, `232 of 232` 那条判据仍然转红了, 但转红的原因是 232 > 66 越了
    基线, 而不是比值被比过 —— 也就是说那条判据当时并没有真的在盯比值。`66 of 66` 是能把两者
    分开的唯一入参: N 正好等于基线, 只看 N 一定判 OK, 只有真的比了 N 与 M 才会报异常。

    这个状态在生产里不是假想: 分层只暴露一层时 M 就是那一层的全量, N == M 而 N 不大。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=66, tools_total=66)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")

    assert item.status == BAD, "N == M 时即使 N 等于基线也必须报异常"
    assert "收窄未生效" in item.value


def test_exposed_baseline_names_the_expected_number(tmp_path: Path) -> None:
    """基线里必须出现「应是 66」那个数。

    `232 of 232` 单看无异常, 只有知道应是 66 才叫异常 —— **没有基线的裸数字不会触发任何人的
    警觉**。所以基线字段里必须真的带着那个数, 不能是「应更少」这种无刻度的话。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=232, tools_total=232)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")
    assert str(probes_cost.EXPECTED_EXPOSED) in item.baseline


def test_exposed_unknown_when_only_one_of_the_pair_present(tmp_path: Path) -> None:
    """只有 N 没有 M → 报未测到, **不拿 N 单独下结论**。

    缺 M 时比值无从计算, 而拿 N 跟基线比会在收窄完全失效时给出「N 正常」的结论。
    """
    root = tmp_path / "gateway"
    row = _turn()
    del row["tools_total"]
    _write(root, [row])
    item = _by_name(probes_cost.collect(_reads(root)), "工具暴露比值 N/M")
    assert item.status == UNKNOWN
    assert "tools_total" in item.reason


# ==========================================================================
# 兜底链: metrics/ 不存在 / 三份缺一份
# ==========================================================================


def test_missing_metrics_dir_reports_unknown_and_report_still_builds(tmp_path: Path) -> None:
    """`metrics/` 整个不存在 → 报未测到, 且**日报其余部分照出, 不整份崩掉**。

    首次部署前这个目录必然不存在。崩掉的话第一次部署就触发「日报生成失败」, 而那条通知本该
    留给真故障 —— 第一天就被当噪音的通知, 之后真出事也不会有人看。
    """
    root = tmp_path / "nonexistent-appdata-root"
    root.mkdir()  # appdata 根在, 但 metrics/ 不在 —— 这是兜底链的第 2 档
    reads = _reads(root)

    assert reads[0].status == cost.UNKNOWN
    assert "metrics/" in reads[0].reason

    items = probes_cost.collect(reads)
    assert items, "整类不该消失 —— 消失与「一切正常」不可区分"
    assert all(i.status == UNKNOWN for i in items)
    # 每条都带 reason: UNKNOWN 不给理由等于又一个「群里安静」。
    assert all(i.reason for i in items)


def test_appdata_root_absent_is_distinct_from_metrics_dir_absent(tmp_path: Path) -> None:
    """兜底链的第 1 档与第 2 档措辞必须不同 —— 用仓库里不存在的目录名。

    「容器没起过」与「首次部署前正常」是两种不同的处置。目录在但内容不全会让兜底链提前终止,
    而 `isdir` 判据全绿抓不到 —— 所以这里必须分别构造两种状态并断言 reason 不同。
    """
    absent_root = tmp_path / "nonexistent-container-alpha"
    present_root = tmp_path / "nonexistent-container-beta"
    present_root.mkdir()

    r_absent = cost.read_source("alpha", absent_root)
    r_present = cost.read_source("beta", present_root)

    assert r_absent.status == cost.UNKNOWN
    assert r_present.status == cost.UNKNOWN
    assert "appdata 根不存在" in r_absent.reason
    assert "metrics/" in r_present.reason
    assert r_absent.reason != r_present.reason


def test_metrics_dir_present_but_no_day_file_is_its_own_rung(tmp_path: Path) -> None:
    """第 3 档: `metrics/` 在、但目标天文件不在 → 仍是未测到而非 0。

    这一档与第 2 档刻意分开。目录确实存在, 所以任何 `isdir` 判据都会绿 —— 提前终止就藏在
    这里。
    """
    root = tmp_path / "nonexistent-container-gamma"
    (root / "metrics").mkdir(parents=True)
    read = cost.read_source("gamma", root, days=[__import__("datetime").date(2026, 9, 18)])
    assert read.status == cost.UNKNOWN
    assert "没有目标天文件" in read.reason


def test_one_of_three_sources_missing_is_unknown_not_zero(tmp_path: Path, pricing: Any) -> None:
    """三份里缺一份 → 报「未测到」而不是当 0, 且**缺的那份不从报告里消失**。

    消失与「这人今天没用」不可区分 —— 一人一容器, 所以那等于一个人的花费凭空蒸发。
    """
    gateway = tmp_path / "gateway"
    luolin = tmp_path / "luolin"
    missing = tmp_path / "nonexistent-chengxx"
    _write(gateway, [_turn(session_id="g1")])
    _write(luolin, [_turn(session_id="l1")])

    reads = [
        cost.read_source("gateway", gateway),
        cost.read_source("luolin", luolin),
        cost.read_source("chengxx", missing),
    ]
    totals = cost.summarize(reads, pricing)

    assert [s.name for s in totals.unknown_sources()] == ["chengxx"]
    assert totals.is_lower_bound is True, "有来源没读到时总额必须是下限"
    assert "1 个来源未测到" in totals.lower_bound_note()

    items = probes_spend.collect(totals, reads)
    source_item = _by_name(items, "成本来源 chengxx")
    assert source_item.status == UNKNOWN
    assert "不等于零花费" in source_item.direction or "≠ 零花费" in source_item.direction

    # 缺的那份没有变成一格 0 —— 否则按容器归因里会出现一个「chengxx: 0.0000 CNY」,
    # 那读起来就是「这人今天没花钱」。
    assert "chengxx" not in totals.by_container

    body = cost.render_cost_section(totals)
    assert "chengxx" in body, "缺的来源必须出现在正文里, 不能静默跳过"


def test_all_sources_missing_makes_daily_total_unknown_not_ok(tmp_path: Path, pricing: Any) -> None:
    """**三个来源全未读到时「当日总花费」必须是 UNKNOWN, 不是 OK。**

    2026-09-21 生产试跑实测的第三个假阳性。原先判定是 `bad if amount > 50 else ok` ——
    只看金额, 没看有没有量到。三个来源全没读到, 总额 `0.0000`, 于是落进「□ 正常」栏,
    而同一份报告紧接着写着「总花费是下限: 3 个来源未测到」, 两处给出相反的归类。

    方向比前两个假阳性更危险: 这条是把「什么都没量到」报成「花费正常」。埋点上线后哪天
    jsonl 路径改了读不到, 日报会继续每天说花费正常, 而没人会来骂。
    """
    reads = [cost.read_source(n, tmp_path / f"nonexistent-{n}") for n in ("gateway", "luolin", "chengxx")]
    totals = cost.summarize(reads, pricing)

    assert totals.total.amount == 0.0, "前提: 一行都没算出来时金额确实是 0"
    assert totals.total.priced_rows == 0, "判据是计费行数而不是金额 —— 0 元有两种成因"

    item = _by_name(probes_spend.collect(totals, reads), "当日总花费")
    assert item.status == UNKNOWN, "一行都没算出来时 0 元是「没量到」, 不是「没花钱」"
    assert "没量到" in item.reason
    assert "3 个来源未测到" in item.reason, "要说清缺的是什么, 不能只说未测到"


def test_rows_present_but_none_priced_is_still_unknown(tmp_path: Path, pricing: Any) -> None:
    """有行、但一行都算不出金额 → 仍然 UNKNOWN。

    这条是变异复核补出来的。上一条(三个来源全读不到)一行都没有, 于是「计费行数」与「总行数」
    恰好都是 0 —— 把 `priced_rows += 1` 挪到 `if rc.amount is not None` 外面(即改成数所有行,
    不管算不算得出金额), 上一条照旧全绿。那等于判据其实没压在「算出来了」这个语义上, 而
    「有行但全都没 usage」正是埋点半残时最可能的现场。

    这里的行有 `event`/`session_id`、`usage_reported=False`、token 全 None: 读得到、数得出,
    就是算不出钱。此时 0 元依然是「没量到」。
    """
    root = tmp_path / "gateway"
    rows = [_turn(session_id="g1", reported=False, prompt=None, cached=None, completion=None)]
    _write(root, rows, day=_today())

    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    assert totals.total.turns == 1, "前提: 行确实读到了, 不是空目录"
    assert totals.total.no_usage == 1
    assert totals.total.priced_rows == 0, "读到了不等于算出来了 —— 判据压的是后者"

    item = _by_name(probes_spend.collect(totals, reads), "当日总花费")
    assert item.status == UNKNOWN, "有行但一行都没算出金额, 0 元仍是「没量到」"
    assert "没量到" in item.reason


def test_zero_spend_with_real_rows_is_measured_not_a_gap(tmp_path: Path, pricing: Any) -> None:
    """正向对照: 真读到了行、金额确实算出来是 0 → OK, **不是**未测到。

    跟上一条配对。只测阴性会让修法退化成「总花费永远报未测到」—— 那是把一个假阳性换成
    一个假阴性, 而假阴性更难发现。

    造「真的没花钱」用的是 token 全 0 的一行, 不是全零单价表: `probes_spend.probe_per_turn`
    内部 `cost.load_pricing(version=...)` **不带 directory**, 所以塞一张临时单价表它读不到
    (会去默认目录找那个版本号, 然后 PricingError)。这个耦合是实测撞出来的。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(session_id="g1", prompt=0, cached=0, completion=0)], day=_today())

    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    assert totals.total.amount == 0.0
    assert totals.total.priced_rows == 1, "这一行是算出来的, 只是 token 数为 0"
    assert totals.is_lower_bound is False

    item = _by_name(probes_spend.collect(totals, reads), "当日总花费")
    assert item.status == OK, "真的没花钱是数据, 不是观测缺口"
    assert "0.0000" in item.value
    assert not item.value.startswith("≥"), "不是下限就不该标 ≥"


def test_all_three_sources_read_when_all_present(tmp_path: Path, pricing: Any) -> None:
    """正向对照: 三份都在时三份都读到, 按容器三格都在。

    一人一容器是成本归因维度, 少读一份就是少算一个人。
    """
    roots = []
    for name in ("gateway", "luolin", "chengxx"):
        root = tmp_path / name
        _write(root, [_turn(session_id=f"{name}-s1")])
        roots.append(root)

    reads = _reads(*roots)
    totals = cost.summarize(reads, pricing)

    assert all(r.status == cost.OK for r in reads)
    assert set(totals.by_container) == {"gateway", "luolin", "chengxx"}
    assert totals.is_lower_bound is False


# ==========================================================================
# 样本极少: 0 条 / 1 条不崩且标出样本量
# ==========================================================================


def test_percentile_of_empty_sample_is_none_not_zero() -> None:
    """0 条样本 → `None`, **不是 0.0**。

    `0.0 秒延迟`会被读成极好的消息, 而它的真实含义是一条都没量到。
    """
    assert probes_cost.percentile([], 50) is None
    assert probes_cost.percentile([], 95) is None


def test_percentile_of_single_sample_does_not_crash() -> None:
    """1 条样本 → p50 与 p95 都等于那一条, 不抛异常。

    `statistics.quantiles` 在 n < 2 时直接抛 `StatisticsError` —— 那会让新部署的第一个小时
    每次日报都「生成失败」。
    """
    assert probes_cost.percentile([2.5], 50) == pytest.approx(2.5)
    assert probes_cost.percentile([2.5], 95) == pytest.approx(2.5)


def test_tiny_sample_reports_its_size_and_warns_it_is_not_statistical(tmp_path: Path) -> None:
    """样本极少时不崩, 且**明确标出样本量**并说明百分位不具统计意义。

    3 条样本的 p95 就是最大值。不标样本量的话它会被当统计量读, 然后据此做容量决策。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(req_bytes=n, ttft_s=1.0) for n in (1000, 2000, 3000)])
    items = probes_cost.collect(_reads(root))

    bytes_item = _by_name(items, "请求体字节数 p50/p95")
    assert bytes_item.status in (OK, BAD)
    assert "样本仅 3 条" in bytes_item.value
    assert "不具统计意义" in bytes_item.value

    ttft_item = _by_name(items, "首字延迟 p50/p95")
    assert "样本仅 3 条" in ttft_item.value


def test_zero_turns_reports_unknown_with_sample_absent(tmp_path: Path) -> None:
    """一行 turn 都没有(文件在但空) → 报未测到而不是 p50=0。"""
    root = tmp_path / "gateway"
    _write(root, [])
    items = probes_cost.collect(_reads(root))
    for name in ("请求体字节数 p50/p95", "首字延迟 p50/p95"):
        item = _by_name(items, name)
        assert item.status == UNKNOWN, f"{name} 在零样本下必须是未测到"
        assert item.reason


def test_large_sample_reports_plain_count(tmp_path: Path) -> None:
    """样本足够时只报条数, 不再挂「不具统计意义」那句。

    正向对照 —— 否则那句警告可能恒定出现, 上面那条判据就不吃劲。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(req_bytes=1000 + i) for i in range(12)])
    item = _by_name(probes_cost.collect(_reads(root)), "请求体字节数 p50/p95")
    assert "样本 12 条" in item.value
    assert "不具统计意义" not in item.value


# ==========================================================================
# 首字延迟必须取 ttft_s, 不取 SSE 响应头
# ==========================================================================


def test_ttft_read_from_ttft_s_field(tmp_path: Path) -> None:
    """首字延迟取 `turn` 行的 `ttft_s`, 数值真的来自那个字段。

    **不能用 `AI response status: 200`** —— 那只有 50-60ms, 量的是 litellm 立刻回的 SSE
    响应头。用它会得出「延迟很好」而用户实际在等好几秒。这里造一批 8 秒的样本: 若实现读错
    字段, 一条样本都取不到, 会报未测到而不是 8 秒。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(ttft_s=8.0) for _ in range(6)])
    item = _by_name(probes_cost.collect(_reads(root)), "首字延迟 p50/p95")

    assert item.status == BAD, "8 秒首字延迟必须越过基线"
    assert "8.00s" in item.value
    assert "ttft_s" in item.semantics


def test_turns_without_ttft_are_unknown_not_zero(tmp_path: Path) -> None:
    """`ttft_s` 为 None(取消/报错的回合没有首 token) → 不当 0 计入。

    补 0 会把这些回合拌进百分位, 把 p50 拉向零 —— 正好在上游最不健康的时候让延迟显得最好。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(ttft_s=None) for _ in range(4)])
    item = _by_name(probes_cost.collect(_reads(root)), "首字延迟 p50/p95")
    assert item.status == UNKNOWN
    assert "ttft_s" in item.reason


def test_boolean_is_not_treated_as_a_number(tmp_path: Path) -> None:
    """JSON 里的 `true` 不能变成 1.0 混进延迟样本。

    `bool` 是 `int` 子类, 不显式拒掉的话一行坏数据会变成一条「1 秒」的样本。
    """
    root = tmp_path / "gateway"
    row = _turn()
    row["ttft_s"] = True
    _write(root, [row])
    item = _by_name(probes_cost.collect(_reads(root)), "首字延迟 p50/p95")
    assert item.status == UNKNOWN, "布尔值必须被拒, 不能当成 1 秒样本"


# ==========================================================================
# 诚实性: 单价表版本号 + 「未与账单对过」
# ==========================================================================


def test_cost_section_prints_pricing_version_and_not_reconciled(tmp_path: Path, pricing: Any) -> None:
    """成本一节必须带单价表版本号与「未与账单对过」。

    少了版本号, 两天之间的金额跳变分不清是调价还是用量变化; 少了「未与账单对过」, 一个抄错
    的单价会以「实测成本」的名义传下去。本期不做对账已由负责人定, 所以这行字是唯一的诚实性
    保障。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn()])
    totals = cost.summarize(_reads(root), pricing)
    body = cost.render_cost_section(totals)

    assert "v2-2026-09-15" in body
    assert cost.NOT_RECONCILED in body
    assert "未与账单对过" in body


def test_pricing_version_also_rides_on_the_finding(tmp_path: Path, pricing: Any) -> None:
    """版本号还要跟着**指标条目**走, 不只在正文里。

    正文排在报告末尾, 超长时被截断掉的正是它 —— 而四个成本数作为 Finding 排在前面。版本号
    只写在正文的话, 截断后金额还在、版本号没了。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn()])
    reads = _reads(root)
    totals = cost.summarize(reads, pricing)
    items = probes_spend.collect(totals, reads)

    total_item = _by_name(items, "当日总花费")
    assert "v2-2026-09-15" in total_item.semantics
    assert "未与账单对过" in total_item.semantics


def test_switching_pricing_table_changes_amount_and_version(tmp_path: Path) -> None:
    """换一版真的单价表 → 金额不同且版本号随之变。

    用 `pricing/` 里真的两份文件, 不造临时字典 —— 造字典测的是测试自己, 而生产读的是那个
    目录。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn()])
    reads = _reads(root)

    v1 = cost.summarize(reads, cost.load_pricing(directory=_PRICING, version="v1-2026-09-01"))
    v2 = cost.summarize(reads, cost.load_pricing(directory=_PRICING, version="v2-2026-09-15"))

    assert v1.pricing_version != v2.pricing_version
    assert v1.total.amount != v2.total.amount
    assert "v1-2026-09-01" in cost.render_cost_section(v1)


# ==========================================================================
# 缓存与字节数分节写, 谁也不解释对方
# ==========================================================================


def test_bytes_section_says_cache_does_not_save_bytes(tmp_path: Path) -> None:
    """字节数一节要明说**缓存不减少要传的字节**。

    首字延迟由上传带宽决定(≈ 字节数 ÷ 230KB/s)。不写这句的后果是有人拿「缓存命中率高」
    解释延迟没改善, 然后去调缓存而不是去裁上下文。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(req_bytes=120_000) for _ in range(6)])
    item = _by_name(probes_cost.collect(_reads(root)), "请求体字节数 p50/p95")

    assert "缓存" in item.semantics
    assert "不减少" in item.semantics or "不省" in item.semantics
    assert "230" in item.semantics, "要带上带宽数字, 否则读者无法把字节换算成秒"


def test_latency_findings_do_not_mention_cache_hit_rate(tmp_path: Path) -> None:
    """延迟那条不拿缓存命中率解释自己。

    两件事分节写: 缓存命中率只出现在钱那一节。这里断言延迟条目里没有「命中率」字样 ——
    出现了就说明两节又缠在一起了。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn() for _ in range(6)])
    item = _by_name(probes_cost.collect(_reads(root)), "首字延迟 p50/p95")
    assert "命中率" not in item.semantics
    assert "命中率" not in item.value


def test_cache_hit_rate_appears_only_in_money_section(tmp_path: Path, pricing: Any) -> None:
    """缓存命中率出现在钱那一节, 且附一句它不影响首字延迟。"""
    root = tmp_path / "gateway"
    _write(root, [_turn(prompt=1000, cached=800)])
    totals = cost.summarize(_reads(root), pricing)
    body = cost.render_cost_section(totals)

    assert "缓存命中" in body
    assert cost.CACHE_SEMANTICS in body
    assert "不省字节" in cost.CACHE_SEMANTICS


# ==========================================================================
# 压缩: 次数与耗时, 零次是实测值不是缺口
# ==========================================================================


def test_compaction_read_from_compaction_rows(tmp_path: Path) -> None:
    """压缩耗时与次数从 `event="compaction"` 行读。

    实测 41.5s x 22 次是批一上线后的头号成本。这里按那个量级造数据。
    """
    root = tmp_path / "gateway"
    rows = [_turn()] + [_turn(event="compaction", duration_s=41.5) for _ in range(22)]
    _write(root, rows)
    items = probes_cost.collect(_reads(root))

    count = _by_name(items, "压缩次数")
    assert count.status == BAD
    assert "22 次" in count.value

    dur = _by_name(items, "压缩耗时 p50/p95")
    assert dur.status == BAD
    assert "41.5s" in dur.value


def test_zero_compactions_is_measured_not_a_gap(tmp_path: Path) -> None:
    """有 turn 行但没有 compaction 行 → 「0 次」是**实测值**, 不是未测到。

    这是「未测到」与「零」那条规则的反向: 把真的 0 报成未测到会制造假缺口, 而运维学会忽略
    这一栏之后, 真的缺口也一起被忽略了。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn() for _ in range(3)])
    count = _by_name(probes_cost.collect(_reads(root)), "压缩次数")

    assert count.status == OK
    assert count.value == "0 次"
    assert "不是观测缺口" in count.semantics


def test_no_rows_at_all_makes_compaction_unknown(tmp_path: Path) -> None:
    """连 turn 行都没有 → 压缩报未测到。

    与上一条成对: 没有 turn 行作证时, 「0 次压缩」无法与「什么都没量到」区分。
    """
    root = tmp_path / "gateway"
    _write(root, [])
    item = _by_name(probes_cost.collect(_reads(root)), "压缩次数与耗时")
    assert item.status == UNKNOWN


def test_compaction_share_of_spend(tmp_path: Path, pricing: Any) -> None:
    """压缩占总花费比例 —— 压缩独立成一维, 不并进触发它的回合。"""
    root = tmp_path / "gateway"
    rows = [_turn(session_id="s1")] + [_turn(event="compaction", prompt=50_000, completion=2_000) for _ in range(3)]
    _write(root, rows)
    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    share = _by_name(probes_spend.collect(totals, reads), "压缩占总花费比例")
    assert share.status == BAD, "压缩占大头时必须报异常"
    assert "3 次压缩" in share.semantics
    assert totals.by_use[cost.USE_COMPACTION].compactions == 3


# ==========================================================================
# 接进日报: 在骨架里加节, 不另起脚本
# ==========================================================================


def _boom(*_a: object, **_k: object):
    """让第 1、2 类探针的外部命令全挂 —— 本文件不碰 docker/curl。"""
    raise OSError("no docker/curl in test")


def test_build_daily_includes_cost_findings_and_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 3 类与成本一节真的进了 `run.build_daily()` 的输出。

    判据落在**日报骨架那个函数**上, 不是只测两个探针模块 —— 否则探针全绿而日报里没有这一节,
    判据照样通过(「判据必须落在它声称的那一层」)。
    """
    for name in ("gateway", "luolin", "chengxx"):
        # 写成今天的天名 —— `_collect_cost()` 按 `days=[today]` 读, 那是生产的路径。
        _write(tmp_path / name, [_turn(session_id=f"{name}-1")], day=_today())
    monkeypatch.setenv(
        "PSI_COST_APPDATA_ROOTS",
        ",".join(f"{n}={tmp_path / n}" for n in ("gateway", "luolin", "chengxx")),
    )

    report = run_mod.build_daily(run_mod.Config(), runner=_boom)
    names = [f.name for f in report.findings]

    for expected in ("请求体字节数 p50/p95", "工具暴露比值 N/M", "首字延迟 p50/p95", "压缩次数"):
        assert expected in names, f"日报缺第 3 类指标 {expected}"
    for expected in ("当日总花费", "每回合成本 p50/p95", "压缩占总花费比例", "未测到 usage 回合占比"):
        assert expected in names, f"日报缺成本指标 {expected}"

    assert report.cost_body, "成本正文必须挂在报告上"
    assert "未与账单对过" in report.cost_body


def test_daily_report_renders_with_metrics_dir_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`metrics/` 不存在时**整份日报照出**, 第 1、2 类的结果不受影响。

    这是「首次部署」那条路径。用仓库里不存在的目录名构造。
    """
    monkeypatch.setenv(
        "PSI_COST_APPDATA_ROOTS",
        ",".join(f"{n}={tmp_path / f'nonexistent-{n}'}" for n in ("gateway", "luolin", "chengxx")),
    )
    report = run_mod.build_daily(run_mod.Config(), runner=_boom)
    text = render_mod.render(report)

    assert text, "日报不得为空"
    assert "未测到" in text
    # 三个来源逐个成条, 不合成一条 —— 一人一容器, 谁的数据没采到决定去看哪台。
    for name in ("gateway", "luolin", "chengxx"):
        assert f"成本来源 {name}" in text


def test_render_puts_anomalies_before_cost_body(tmp_path: Path, pricing: Any) -> None:
    """异常排在成本正文**之前**。

    成本正文的行数随会话数涨, 放前面会把异常挤出视野; 而超长截断砍掉的正是末尾。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn(tools_exposed=232, tools_total=232)])
    reads = _reads(root)
    totals = cost.summarize(reads, pricing)

    report = findings_mod.Report(tier="日报", timestamp="ts")
    report.extend(probes_cost.collect(reads))
    report.extend(probes_spend.collect(totals, reads))
    report.cost_body = cost.render_cost_section(totals)
    text = render_mod.render(report)

    assert text.index("■ 异常") < text.index("■ 成本"), "异常必须排在成本正文之前"
    assert text.startswith("【异常】")


def test_pricing_error_degrades_to_unknown_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单价表读不出来 → 一条未测到, 日报照出, **不是非零退出**。

    冒到 `main` 会变成「日报生成失败」, 而那条通知本该留给真故障 —— 第 1、2 类其实都采到了,
    不该被一份读不出的单价表带垮。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn()], day=_today())
    monkeypatch.setenv("PSI_COST_APPDATA_ROOTS", f"gateway={root}")

    def _explode(*_a: object, **_k: object):
        raise cost.PricingError("单价表目录不存在: nonexistent-pricing-dir")

    monkeypatch.setattr(run_mod.cost, "load_pricing", _explode)

    report = run_mod.build_daily(run_mod.Config(), runner=_boom)
    item = _by_name(report.findings, "成本汇总")
    assert item.status == UNKNOWN
    assert "nonexistent-pricing-dir" in item.reason
    # 第 3 类指标不该被带垮 —— 它们不需要单价表。
    assert _by_name(report.findings, "首字延迟 p50/p95").status in (OK, BAD)


def test_metrics_read_once_for_both_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两节共用一次读盘。

    各读一次的话, 跨午夜或撞上轮转时两节会看到不同的行, 而「两节数字对不上」没法自证是哪一
    节错。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn()], day=_today())
    monkeypatch.setenv("PSI_COST_APPDATA_ROOTS", f"gateway={root}")

    calls: list[object] = []
    # 数**底层单来源读取**的次数, 不去包 `read_sources` 自己 —— `run.cost` 与本文件的 `cost`
    # 是同一个模块对象, 包住 `read_sources` 再从包装里调它会无限递归(实测 RecursionError)。
    # 数 `read_source` 还更严格: 它按来源计次, 重复读盘无论从哪一层发起都会露出来。
    real = cost.read_source

    def _counting(name, root_, *, days=None):
        calls.append(name)
        return real(name, root_, days=days)

    monkeypatch.setattr(cost, "read_source", _counting)
    run_mod.build_daily(run_mod.Config(), runner=_boom)
    assert len(calls) == 1, f"单来源的 jsonl 应只读一次, 实际 {len(calls)} 次: {calls}"


def test_schema_chars_reported_as_missing_upstream_field(tmp_path: Path) -> None:
    """工具 schema 字符数: 埋点没这个字段 → 报未测到, **不拿个数乘估计值冒充实测**。

    编出来的数字以「实测」名义进报告, 比报「未测到」坏得多。缺口写成文字由人决定要不要回改
    上游卡 —— 本卡不改 `src/`。
    """
    root = tmp_path / "gateway"
    _write(root, [_turn() for _ in range(6)])
    item = _by_name(probes_cost.collect(_reads(root)), "工具 schema 字符数")

    assert item.status == UNKNOWN
    assert "埋点无此字段" in item.reason
    assert "289774" in item.baseline, "基线要带上曾实测的那个数, 否则这条缺口没有刻度"
    assert probes_cost.MISSING_UPSTREAM_INPUTS, "缺口必须在模块里留下文字记录"
