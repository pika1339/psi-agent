"""成本一节的四个数, 每个带基线与方向。**算钱全部委托 `cost.py`, 本模块一分钱都不算。**

## 为什么还要这一层

`cost.render_cost_section()` 出的是一整段文本(合计/按容器/按模型/按会话), 它进日报的正文。
但日报的排序规则是**异常排最前、正常压一行**, 而那要求每个数是一条带 `status` 的 `Finding`
—— 一整段文本没法参与排序, 会永远沉在报告底部没人看。

所以本模块把四个数各做成一条 `Finding`(判断超没超基线), 金额本身仍由 `cost.summarize()`
算出来: 这里只读 `CostTotals` 的字段做比较, 不乘任何单价。

## 「未测到 usage」不得静默算作 0 元

这是本卡的验收门槛。`cost.Bucket.amount` **只累加算得出的行**, 算不出的进 `no_usage` /
`unpriced` 计数, 因此 `is_lower_bound` 为真时合计是**下限**而不是总额。本模块据此:

* 「当日总花费」那条在下限成立时一律标 `≥` 与「下限」字样, 不打等号;
* 「未测到 usage 回合占比」单独成一条指标, 且**只要大于 0 就是 BAD** —— 它意味着有花费没
  被算进来, 而「少算了钱」与「花得少」在总额里长得一模一样;
* 来源缺失(某个容器读不到)走 `cost.CostTotals.unknown_sources()`, 报 UNKNOWN 不报 0。

## 单价表版本号与「未与账单对过」

`render_cost_section()` 的抬头无条件带这两样。本模块另外把它们做成一条 `Finding`, 理由是
正文可能因超长被截断(`render.MAX_CHARS`), 而截断掉的恰好是排在后面的正文 —— 版本号跟着
指标条目走才截不掉。本期**不做对账**(要登上游控制台, 无法自动化, 负责人已定), 那行字是
唯一的诚实性保障。

## 不谈延迟

缓存命中省钱但**不省字节**, 而首字延迟由上传带宽决定。延迟与字节数在 `probes_cost.py`
那一节, 本模块只谈钱, 两节谁也不解释对方。
"""

from __future__ import annotations

from collections.abc import Sequence

import cost
from findings import Finding, bad, ok, unknown
from probes_cost import percentile, sample_note

#: 当日总花费告警线。按生产三容器、每人每天几十回合的量级定的粗线 —— 它的作用不是精确预算
#: (本期不做预算熔断), 而是让「某天忽然翻几倍」有个能触发注意的刻度。
DAILY_TOTAL_WARN = 50.0

#: 每回合成本 p95 告警线。
PER_TURN_P95_WARN = 1.0

#: 压缩占总花费比例告警线(%)。实测压缩 41.5s x 22 次是头号成本项, 占比过高说明上下文
#: 一直在撞顶 —— 该裁的是上下文, 不是压缩本身。
#:
#: 曾是 30%。实测这个比例稳定在 70-85%(2026-09-22 那份 74.0%) —— 压缩是头号成本项这件事
#: 是**已知的常态**, 不是每天新发生的事故。定在 30% 等于每天重播一遍同一个结论。
#:
#: 重定到 85%: 越线才意味着比这个已经很糟的常态更糟。这个数字本身每天进多维表格, 它从
#: 74% 爬到 80% 这种变化看表能看出来, 而看表是主动行为 —— 群消息留给需要有人马上动手的事。
COMPACTION_SHARE_WARN_PCT = 85.0


def _money(amount: float, currency: str) -> str:
    return f"{amount:.4f} {currency}"


def probe_daily_total(totals: cost.CostTotals) -> Finding:
    """当日总花费。下限成立时标 `≥`, 不打等号。

    ## 一行都没算出来时必须是 UNKNOWN, 不是 OK

    2026-09-21 生产试跑实测: 三个来源全未读到(metrics 还没落盘), 总额 `0.0000`, 而原先
    的判定是 `bad if amount > 50 else ok` —— **只看金额, 没看有没有量到**。于是「什么都
    没量到」被判成「花费正常」, 落进「□ 正常」那一栏。同一份报告里紧接着自己写着「总花费
    是下限: 3 个来源未测到」, 两处给出了相反的归类。

    这个方向比报错更危险: wss 那条把健康报成故障(会有人来骂), 这条把无数据报成健康
    (没人会来骂)。等埋点上线后哪天 jsonl 路径改了读不到, 日报会继续每天说「花费正常」。

    `amount == 0.0` 的两种成因必须分开 —— 一分钱没花, 与一行都没算出来。判据是
    `total.priced_rows`, 不是金额。有部分行算得出、只是偏低时仍可以 OK, 但保留下限口径。
    """
    cur = totals.currency
    lower = totals.is_lower_bound
    baseline = f"< {_money(DAILY_TOTAL_WARN, cur)}/天"
    direction = "越高越糟; 下限成立时真实值只会更高"
    semantics = f"单价表 {totals.pricing_version} · {cost.NOT_RECONCILED}; 本期只打印不对账, 这行字是唯一的诚实性保障"
    if not totals.total.priced_rows:
        # 一行都没算出金额。此时 0.0000 不是「没花钱」而是「没量到」。
        note = totals.lower_bound_note() if lower else "当日没有任何 turn/compaction 行"
        return unknown(
            "当日总花费",
            reason=f"没有任何计费行, 0 元是「没量到」而不是「没花钱」 —— {note}",
            baseline=baseline,
            direction=direction,
            semantics=semantics,
        )
    value = f"{'≥ ' if lower else ''}{_money(totals.total.amount, cur)}"
    if lower:
        value += f" (下限 —— {totals.lower_bound_note()})"
    make = bad if totals.total.amount > DAILY_TOTAL_WARN else ok
    # 下限成立时这个数**照样进表**: 它是真算出来的钱, 只是不含算不出的那些行。把下限当
    # 「没量到」留空会丢掉唯一的花费趋势; 所以下限这件事用 `花费是否下限` 那一列表达 ——
    # 一个数字列表达不了「这是下限」, 硬要表达就只能留空或减记, 两者都更坏。
    return make(
        "当日总花费",
        value,
        baseline,
        direction,
        semantics,
        num=round(totals.total.amount, 4),
        num_warn=DAILY_TOTAL_WARN,
        unit=cur,
    )


def probe_per_turn(totals: cost.CostTotals, reads: Sequence[cost.SourceRead]) -> Finding:
    """每回合成本 p50/p95。

    从行级重算一遍单行金额, 但**用的是 `cost.cost_of_row()`** —— 百分位需要每行一个数,
    而 `CostTotals` 只有聚合值。仍然是报告层算钱, 没有第二套单价逻辑。

    `amount is None` 的行(无 usage / 未定价)**不当 0 计入百分位**: 混进去会把 p50 拉向零,
    正好在上游最不健康的时候让每回合成本显得最低。
    """
    pricing = cost.load_pricing(version=totals.pricing_version)
    amounts: list[float] = []
    skipped = 0
    for read in reads:
        for row in read.rows:
            if str(row.get(cost.F_EVENT, "") or "") != cost.EVENT_TURN:
                continue
            rc = cost.cost_of_row(row, pricing, container=read.name)
            if rc.amount is None:
                skipped += 1
            else:
                amounts.append(rc.amount)

    cur = totals.currency
    baseline = f"p95 < {_money(PER_TURN_P95_WARN, cur)}/回合"
    direction = "越高越糟 —— 乘以日回合数就是日成本"
    if not amounts:
        return unknown(
            "每回合成本 p50/p95",
            reason=(
                f"{skipped} 个回合的金额算不出(无 usage 或模型未定价), 没有可用样本"
                if skipped
                else "没有 turn 行可读(metrics 未落盘, 或当日无回合)"
            ),
            baseline=baseline,
            direction=direction,
        )

    p50 = percentile(amounts, 50) or 0.0
    p95 = percentile(amounts, 95) or 0.0
    note = sample_note(len(amounts))
    if skipped:
        note += f", 另有 {skipped} 个回合算不出金额未计入"
    make = bad if p95 > PER_TURN_P95_WARN else ok
    return make(
        "每回合成本 p50/p95",
        f"p50 {_money(p50, cur)} / p95 {_money(p95, cur)} ({note})",
        baseline,
        direction,
        f"单价表 {totals.pricing_version} · {cost.NOT_RECONCILED}",
    )


def probe_compaction_share(totals: cost.CostTotals) -> Finding:
    """压缩占总花费比例。

    压缩是 `by_use` 里独立的一格(不并进触发它的回合), 所以这个比例直接可取。
    """
    baseline = f"< {COMPACTION_SHARE_WARN_PCT:.0f}%"
    direction = "越高越糟 —— 占比高说明上下文一直在撞顶, 该裁上下文而不是调压缩"
    bucket = totals.by_use.get(cost.USE_COMPACTION)
    total_amount = totals.total.amount

    if total_amount <= 0:
        return unknown(
            "压缩占总花费比例",
            reason=(
                "总花费为 0 且有算不出的行, 比例无意义 —— 分母是下限"
                if totals.is_lower_bound
                else "总花费为 0(当日无可计费回合), 比例无分母"
            ),
            baseline=baseline,
            direction=direction,
        )

    amount = bucket.amount if bucket is not None else 0.0
    count = bucket.compactions if bucket is not None else 0
    share = amount * 100.0 / total_amount
    semantics = f"{count} 次压缩共 {_money(amount, totals.currency)}"
    if totals.is_lower_bound:
        semantics += "; 分子分母都是下限, 比例仅供参考"
    make = bad if share > COMPACTION_SHARE_WARN_PCT else ok
    return make("压缩占总花费比例", f"{share:.1f}%", baseline, direction, semantics)


def probe_no_usage_share(totals: cost.CostTotals) -> Finding:
    """「未测到 usage」回合占比 —— **本卡的验收门槛。**

    只要大于 0 就是 BAD, 不设容忍区间: 这些回合的花费**没有被算进总额**, 所以总额是下限。
    把它们静默当 0 元的后果是当日花费在上游最不健康的时候显得最低, 而报告读起来一切正常。

    分母用 `turns + compactions`(所有计费行)而不是只用 turns: 压缩行同样可能缺 usage,
    只按 turns 算会让占比偏低。
    """
    counted = totals.total.turns + totals.total.compactions
    no_usage = totals.total.no_usage
    unpriced = totals.total.unpriced
    baseline = "0%"
    direction = "大于 0 即意味着总花费被低估 —— 是下限不是总额"

    if counted == 0:
        unknown_sources = totals.unknown_sources()
        reason = (
            f"{len(unknown_sources)} 个来源未测到({', '.join(s.name for s in unknown_sources)}), 没有任何计费行"
            if unknown_sources
            else "没有任何 turn/compaction 行(metrics 未落盘, 或当日无回合)"
        )
        return unknown("未测到 usage 回合占比", reason=reason, baseline=baseline, direction=direction)

    share = no_usage * 100.0 / counted
    value = f"{share:.1f}% ({no_usage}/{counted} 个回合)"
    semantics = (
        f"这 {no_usage} 个回合的花费未计入总额, 因此当日总花费是**下限**, 不是总额"
        if no_usage
        else "全部计费行都带 usage, 总额未因缺 usage 被低估"
    )
    if unpriced:
        semantics += f"; 另有 {unpriced} 个回合模型未定价({', '.join(totals.unpriced_models)}), 同样未计入"

    make = bad if no_usage or unpriced else ok
    return make("未测到 usage 回合占比", value, baseline, direction, semantics)


def collect(totals: cost.CostTotals, reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """成本一节的四条指标 + 每个未测到的来源一条。

    来源缺失逐个成条而不是合成一条: 三个容器是三个人, 「谁的数据没采到」决定去看哪台容器。
    """
    out = [
        probe_daily_total(totals),
        probe_per_turn(totals, reads),
        probe_compaction_share(totals),
        probe_no_usage_share(totals),
    ]
    for source in totals.unknown_sources():
        out.append(
            unknown(
                f"成本来源 {source.name}",
                reason=source.reason,
                baseline="应能读到 metrics jsonl",
                direction="未测到 ≠ 零花费 —— 该容器的花费整个不在总额里",
            )
        )
    if totals.malformed_rows:
        out.append(
            bad(
                "metrics 坏行数",
                f"{totals.malformed_rows} 行",
                "0 行",
                "大于 0 即有花费未计入总额",
                "坏行不静默丢弃 —— 「少算了钱」与「花得少」在总额里无法区分",
            )
        )
    return out
