"""第 3 类: 请求成本与延迟。纯标准库, 不 import 内核。

## 字段名抄自内核实测, 不照方案文档猜

方案文档把首字延迟那个字段写成 `ttft`, 而 B 卡 `session/agent.py` 实际落的是 **`ttft_s`**。
本模块按实测的那个名字读。两处 `metrics.record()` 调用的完整字段见
`session/agent.py` 的 `"turn"` 与 `"compaction"` 两处。

## 首字延迟不能用 `AI response status: 200`

那行日志只有 50-60ms, 量的是 litellm 立刻回的 **SSE 响应头**, 不是第一个 token。拿它当首字
延迟会得出「延迟很好」的结论, 而用户体感的等待全在它之后。所以只认 `turn` 行的 `ttft_s`
—— 那是 `ai_client.py` 在**第一个内容 delta 到达时**取的差值。

## 字节数与缓存命中分节写, 谁也不解释对方

**首字延迟由上传带宽决定**(≈ 字节数 ÷ 230KB/s 单流), 而**缓存命中只省钱、不省字节** ——
命中率再高, 要传的请求体一个字节都不少。这两件事在报告里是两节: 字节/延迟在本模块,
钱在 `cost.render_cost_section()`。混着写的后果是有人拿「缓存命中率高」来解释延迟没改善,
然后去调缓存而不是去裁上下文。

## 「未测到」与「零」

三份来源(一人一容器)由 `cost.read_sources()` 读, 缺哪份留一条 UNKNOWN **不跳过** ——
跳过的话那个容器整个从报告消失, 而消失与「这人今天没用」不可区分。样本数为 0 时 p50/p95
报 UNKNOWN 而不是 0.0: `0.0 秒延迟`会被读成极好, 实际是一条都没量到。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import cost
from findings import Finding, bad, ok, unknown

#: 收窄生效后应暴露的工具数。`232 of 232` 单看无异常, 只有知道**应是 66** 才叫异常 ——
#: 而收窄机制上线后生产从来没有过 EXPOSED.txt, 每层都走了「未声明」分支暴露全量, 期间
#: 请求体每回合带 289774 字符的工具 schema, 没有任何东西出过声。
EXPECTED_EXPOSED = 66

#: 单流上传带宽 KB/s, 实测值。首字延迟 ≈ 请求体字节数 ÷ 这个数。
UPLOAD_KBPS = 230

#: 请求体字节数 p95 告警线。工具 schema 实测 289774 字符, 占不可裁地板约 47%, 即地板本身
#: 约 600 KiB —— 那已经意味着两秒半的纯上传。所以 600 KiB 是「地板水位」不是舒适线。
REQ_BYTES_P95_WARN = 600 * 1024

#: 首字延迟 p95 告警线(秒)。按 600 KiB ÷ 230KB/s ≈ 2.6s 的地板留一倍余量。
TTFT_P95_WARN_S = 6.0

#: 压缩次数/天告警线。实测 41.5s x 22 次 —— 22 次就是出事的那天的数字。
COMPACTION_COUNT_WARN = 10

#: 压缩耗时 p50 告警线(秒)。
COMPACTION_P50_WARN_S = 20.0


def percentile(values: Sequence[float], q: float) -> float | None:
    """第 `q` 百分位(`q` 取 0..100), **样本为空返回 None 而不是 0.0**。

    返回 None 的理由是 `0.0` 会被读成「延迟极低」这种好消息, 而它的真实含义是一条都没量到
    —— 观测缺口伪装成健康正是这份报告最贵的失败模式。调用方据此报 UNKNOWN。

    样本只有 1 条时 p50 与 p95 都等于那一条, 不插值也不报错: 样本极少是**正常路径**
    (新部署的第一个小时就是这样), 崩掉的话第一次部署就触发「日报生成失败」。所以样本量
    必须一起进报告, 让读者自己判断这个百分位可不可信。

    线性插值法, 与 `statistics.quantiles` 的默认口径不同但对小样本更直观; 之所以自己写
    而不用标准库, 是 `quantiles` 在 n < 2 时直接抛 `StatisticsError`。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (q / 100.0)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _nums(rows: Sequence[dict[str, object]], field: str) -> list[float]:
    """取某字段的数值, **跳过 None 与非数值**。

    显式拒 `bool`: 它是 `int` 子类, JSON 里的 `true` 会变成 1.0 混进延迟样本里。
    缺失的行不补 0 —— 补 0 会把「没记到」拌进百分位, 把 p50 拉向零。
    """
    out: list[float] = []
    for row in rows:
        value = row.get(field)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value):
            out.append(float(value))
    return out


def _opt_int(value: object) -> int | None:
    """取一个整数计数, **缺失返回 None 而不是 0**。

    显式拒 `bool`: 它是 `int` 子类, JSON 里的 `true` 会变成 1, 于是一行坏数据能伪造出
    「暴露 1 个工具」这种看着极健康的读数。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _turns(reads: Sequence[cost.SourceRead]) -> list[dict[str, object]]:
    return [r for read in reads for r in read.rows if str(r.get(cost.F_EVENT, "") or "") == cost.EVENT_TURN]


def _compactions(reads: Sequence[cost.SourceRead]) -> list[dict[str, object]]:
    return [r for read in reads for r in read.rows if str(r.get(cost.F_EVENT, "") or "") == cost.EVENT_COMPACTION]


def sample_note(n: int) -> str:
    """样本量说明。**每个百分位都要带** —— 3 条样本的 p95 就是最大值, 不带样本量会被当统计量读。

    不带下划线前缀: `probes_spend` 也要用它(每回合成本的百分位同样需要标样本量), 而下划线
    在跨模块契约上的意思是「不该被外面 import」。
    """
    if n < 5:
        return f"样本仅 {n} 条, 百分位不具统计意义(p95≈最大值)"
    return f"样本 {n} 条"


def probe_request_bytes(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """请求体字节数 p50/p95 —— **最有解释力的单一指标**。

    首字延迟 ≈ 字节数 ÷ 230KB/s, 所以这个数直接决定用户等多久。裁上下文要按**字节**不按
    字符(一个中文字符 3 字节), 而缓存命中一个字节都不省。
    """
    turns = _turns(reads)
    samples = _nums(turns, "req_bytes")
    if not samples:
        return [
            unknown(
                "请求体字节数 p50/p95",
                reason=(
                    f"{len(turns)} 个回合行里没有一条带可用 req_bytes"
                    if turns
                    else "没有 turn 行可读(metrics 未落盘, 或当日无回合)"
                ),
                baseline=f"p95 < {REQ_BYTES_P95_WARN // 1024} KiB",
                direction="越高越糟 —— 首字延迟随字节数线性涨",
            )
        ]

    p50 = percentile(samples, 50) or 0.0
    p95 = percentile(samples, 95) or 0.0
    value = f"p50 {p50 / 1024:.1f} KiB / p95 {p95 / 1024:.1f} KiB ({sample_note(len(samples))})"
    semantics = (
        f"按 {UPLOAD_KBPS}KB/s 单流上传, p95 约合 {p95 / 1024 / UPLOAD_KBPS:.1f}s 纯上传耗时; "
        "缓存命中不减少要传的字节, 因此不改善这一项"
    )
    make = bad if p95 > REQ_BYTES_P95_WARN else ok
    return [
        make(
            "请求体字节数 p50/p95",
            value,
            f"p95 < {REQ_BYTES_P95_WARN // 1024} KiB",
            "越高越糟 —— 首字延迟随字节数线性涨",
            semantics,
            # 进表的数按 **KiB** 不按字节: 字节数是六位数, 在趋势表里一列六位数读不出量级变化。
            # 主数取 p95(基线写在它身上), p50 作附属 —— 它没有告警线, 见 `Finding.extra_nums`。
            num=round(p95 / 1024, 1),
            num_warn=float(REQ_BYTES_P95_WARN // 1024),
            unit="KiB",
            extra_nums={"p50": round(p50 / 1024, 1)},
        )
    ]


def probe_tools_exposed(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """`tools_exposed=N of M` 的**比值**, 不是只看 N。

    `232 of 232` 表示收窄**没生效**, 而日志同时汇报「机制已启用」—— 光看 N 看不出来。
    实测过: 收窄机制上线后生产从来没有 EXPOSED.txt, 每层都走「未声明」分支暴露全量, 零告警。

    所以判据分三档, N == M 那档单独成一条 BAD 并明说「收窄未生效」: 它与「N 略高于基线」
    是两种不同的修法(前者补清单文件, 后者调分层规则)。
    """
    turns = _turns(reads)
    pairs: list[tuple[int, int]] = []
    for row in turns:
        exposed_raw = _opt_int(row.get("tools_exposed"))
        total_raw = _opt_int(row.get("tools_total"))
        # 两个都要在才成一对: 缺 M 时比值无从计算, 而拿 N 单独跟基线比会在收窄完全失效时
        # 给出「N 正常」的结论 —— 那正是 `232 of 232` 被读成健康的那条路。
        if exposed_raw is None or total_raw is None:
            continue
        pairs.append((exposed_raw, total_raw))
    if not pairs:
        return [
            unknown(
                "工具暴露比值 N/M",
                reason=(
                    f"{len(turns)} 个回合行里没有一条同时带 tools_exposed 与 tools_total"
                    if turns
                    else "没有 turn 行可读(metrics 未落盘, 或当日无回合)"
                ),
                baseline=f"N ≈ {EXPECTED_EXPOSED}, 且 N < M",
                direction="N == M 表示收窄完全未生效",
            )
        ]

    # 取最近一条而不是平均: 比值是个配置状态量, 平均会把「今天改好了」与「一直坏着」抹平。
    exposed, total = pairs[-1]
    ratio = exposed * 100.0 / total if total else 0.0
    value = f"{exposed} of {total} ({ratio:.0f}%, {len(pairs)} 个回合)"
    baseline = f"N ≈ {EXPECTED_EXPOSED}, 且 N < M"

    if total and exposed == total:
        return [
            bad(
                "工具暴露比值 N/M",
                f"{value} —— 收窄未生效",
                baseline,
                "N == M 表示收窄完全未生效",
                "全量暴露时请求体每回合多带约 289774 字符工具 schema; "
                "日志可能同时汇报「机制已启用」—— 那句话只说明代码路径在, 不说明清单文件在",
            )
        ]
    if exposed > EXPECTED_EXPOSED:
        return [
            bad(
                "工具暴露比值 N/M",
                value,
                baseline,
                "越高越糟 —— 每多一个工具都是每回合都要上传的 schema",
                f"收窄生效了但暴露面比预期宽 {exposed - EXPECTED_EXPOSED} 个",
            )
        ]
    return [ok("工具暴露比值 N/M", value, baseline, "N == M 表示收窄完全未生效")]


def probe_ttft(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """首字延迟 p50/p95, 取 `turn` 行的 **`ttft_s`**。

    **不用 `AI response status: 200` 那行日志** —— 它只有 50-60ms, 量的是 litellm 立刻回的
    SSE 响应头, 不是第一个 token。用它会得出「延迟很好」而用户实际在等好几秒。
    """
    turns = _turns(reads)
    samples = _nums(turns, "ttft_s")
    baseline = f"p95 < {TTFT_P95_WARN_S:.0f}s"
    direction = "越高越糟 —— 这是用户等待的那段"
    if not samples:
        return [
            unknown(
                "首字延迟 p50/p95",
                reason=(
                    f"{len(turns)} 个回合行里没有一条带可用 ttft_s(取消/报错的回合没有首 token)"
                    if turns
                    else "没有 turn 行可读(metrics 未落盘, 或当日无回合)"
                ),
                baseline=baseline,
                direction=direction,
            )
        ]
    p50 = percentile(samples, 50) or 0.0
    p95 = percentile(samples, 95) or 0.0
    make = bad if p95 > TTFT_P95_WARN_S else ok
    return [
        make(
            "首字延迟 p50/p95",
            f"p50 {p50:.2f}s / p95 {p95:.2f}s ({sample_note(len(samples))})",
            baseline,
            direction,
            "取自 turn 行的 ttft_s(第一个内容 delta); 由上传带宽决定, 见请求体字节数一项",
            num=round(p95, 2),
            num_warn=TTFT_P95_WARN_S,
            unit="s",
            extra_nums={"p50": round(p50, 2)},
        )
    ]


def probe_compaction(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """压缩耗时与次数, 读 `event="compaction"` 行。

    实测 41.5s x 22 次, 是批一上线后的头号成本。压缩是**独立一行**不并进触发它的回合, 所以
    这里数的是行数, 不是 `compaction_triggered` 为真的回合数。

    零次压缩是**正常值不是缺口**: 当日回合少就不会触发。所以只有连 turn 行都没有时才报
    UNKNOWN —— 有 turn 行而没有 compaction 行, 是实测到的 0 次。
    """
    rows = _compactions(reads)
    turns = _turns(reads)
    count_baseline = f"< {COMPACTION_COUNT_WARN} 次/天"
    count_direction = "越多越糟 —— 每次都是一整轮模型调用的钱和时间"

    if not rows and not turns:
        return [
            unknown(
                "压缩次数与耗时",
                reason="没有任何 turn/compaction 行可读(metrics 未落盘)——「0 次压缩」与「没量到」必须分开",
                baseline=count_baseline,
                direction=count_direction,
            )
        ]

    out: list[Finding] = []
    count = len(rows)
    make = bad if count >= COMPACTION_COUNT_WARN else ok
    out.append(
        make(
            "压缩次数",
            f"{count} 次",
            count_baseline,
            count_direction,
            "0 次是实测值(当日回合少即不触发), 不是观测缺口 —— 本项有 turn 行作证",
            # 这里的 0 **可以**进表: 走到这一行说明有 turn 行作证, 是实测到的零次。
            # 与之相对的「没量到」在上面早已 return UNKNOWN, 进表是空格子。
            num=float(count),
            num_warn=float(COMPACTION_COUNT_WARN),
            unit="次",
        )
    )

    samples = _nums(rows, "duration_s")
    dur_baseline = f"p50 < {COMPACTION_P50_WARN_S:.0f}s"
    dur_direction = "越高越糟 —— 压缩期间用户在等"
    if not samples:
        if count:
            out.append(
                unknown(
                    "压缩耗时 p50/p95",
                    reason=f"{count} 行压缩记录里没有一条带可用 duration_s",
                    baseline=dur_baseline,
                    direction=dur_direction,
                )
            )
        return out

    p50 = percentile(samples, 50) or 0.0
    p95 = percentile(samples, 95) or 0.0
    make = bad if p50 > COMPACTION_P50_WARN_S else ok
    out.append(
        make(
            "压缩耗时 p50/p95",
            f"p50 {p50:.1f}s / p95 {p95:.1f}s ({sample_note(len(samples))})",
            dur_baseline,
            dur_direction,
            f"累计 {sum(samples):.0f}s 花在压缩上",
        )
    )
    return out


def probe_tool_schema_chars(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """工具 schema 字符数 —— **埋点没有这个字段, 所以只能报未测到。**

    方案文档把它列进第 3 类(曾实测 289774, 占不可裁地板约 47%), 但 B 卡实际落的 `turn` 行里
    没有 schema 字符数: 有的是 `req_bytes`(整个请求体)与 `tools_exposed`/`tools_total`(个数)。

    本卡**不回改埋点**(不在范围内), 也**不在这里拿个数乘一个估计值冒充实测** —— 那会让一个
    编出来的数字以「实测」的名义进报告, 比报「未测到」坏得多。缺口写成文字, 由人决定要不要
    回改上游卡。
    """
    return [
        unknown(
            "工具 schema 字符数",
            reason=(
                "埋点无此字段 —— turn 行只有 req_bytes(整个请求体)与 tools_exposed/tools_total(个数), "
                "没有 schema 序列化后的字符数。本卡不回改 src/ 埋点; 要这个数需回改上游 B 卡"
            ),
            baseline="曾实测 289774 字符, 占不可裁地板约 47%",
            direction="越高越糟 —— 它是请求体里最大的一块不可裁部分",
            semantics="可用「工具暴露比值 N/M」与「请求体字节数」间接判断: N == M 时这一块必然是全量",
        )
    ]


#: 算钱原料齐全但**第 3 类指标缺**的埋点字段。只记录, 不在本层偷偷补。
MISSING_UPSTREAM_INPUTS = (
    "工具 schema 字符数: turn 行无此字段。B 卡落了 req_bytes / tools_exposed / tools_total, "
    "但没有工具 schema 单独序列化后的长度, 因此「占不可裁地板 47%」这个口径当前算不出来。"
    "补法是在 session/prompt_budget.py 已有的 json.dumps(tool_defs) 处顺手取 len() 带进 record()。",
)


def collect(reads: Sequence[cost.SourceRead]) -> list[Finding]:
    """第 3 类全部指标。

    收 `reads` 而不是自己去读盘: 成本一节要用同一批行算钱, 读两遍会让两节在跨午夜或轮转时
    看到不同的数据, 而「两节数字对不上」无法自证是哪一节错。
    """
    out: list[Finding] = []
    out.extend(probe_request_bytes(reads))
    out.extend(probe_tools_exposed(reads))
    out.extend(probe_ttft(reads))
    out.extend(probe_compaction(reads))
    out.extend(probe_tool_schema_chars(reads))
    return out
