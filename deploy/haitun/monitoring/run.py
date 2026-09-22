"""监控入口 —— 宿主 cron 直接跑这个。纯标准库, 不 import 内核。

## 用法

    python3 run.py daily        # 日报: 全量采集 -> 写趋势表一行; 只在有异常时发消息
    python3 run.py fast         # 即时档: 仅公网可达性 + OOM, 只在有异常时出声
    python3 run.py heartbeat    # 即时档存活心跳
    python3 run.py selftest     # 不碰生产, 只验投递链路通不通

## 日报的主载体是多维表格, 不是消息

每天一条全量消息的结局是被无视, 然后被静音 —— 静音之后这套监控等于不存在。所以数字一律
进**趋势表**(一天一行, 一指标一列), 消息只在有异常或有观测缺口时发, 纯文本 + 表链接。

日报的存活证明因此落在**表里有没有今天这一行**上, 而不落在「每天有消息」上。

## 为什么不 import 内核

内核在容器里、这个脚本在宿主上, `import psi_agent` 压根过不去。而且**故意**不过去: 日报要
在容器全挂的时候还能发出来, 依赖容器里的任何东西都违背这个目的。

## 退出码

    0  跑完了(有异常也是 0 —— 异常通过消息内容表达, 不通过退出码)
    1  跑不下去(配置/环境问题), cron 包装层据此发「生成失败」
    2  投递失败(指标采到了但没发出去)

「有异常」不用非零退出的理由: cron 包装层把非零一律翻译成「日报生成失败」, 而
「生产有异常」与「监控自己坏了」是两件必须分开的事 —— 混在一起之后, 每次生产有事都会同时
收到一条说监控坏了的假消息。
"""

from __future__ import annotations

# ruff: noqa: T201  这是命令行脚本, stdout/stderr 就是它的输出通道。
import datetime as _dt
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bitable
import cost
import probes_cost
import probes_public
import probes_resources
import probes_spend
import render
from config import Config, local_timestamp
from findings import BAD, UNKNOWN, Report, unknown
from notify import post


def _guard(label: str, fn, *args, **kwargs):
    """跑一个采集器, 抛异常就变成一条 UNKNOWN, 不带垮其余的。

    为什么要这一层: 一个探针挂掉让整份脚本崩掉, 表现是「日报生成失败」—— 而那条通知本该
    留给真的故障。更糟的是第一次部署时(配置还没齐)必然触发, 于是这条最重要的通知第一天
    就被当成噪音。所以单个探针的失败降级成一条「未测到」, 整份报告照出。
    """
    try:
        result = fn(*args, **kwargs)
        return result if isinstance(result, list) else [result]
    except Exception as exc:
        return [
            unknown(
                label,
                reason=f"采集器抛异常: {type(exc).__name__}: {exc} | {traceback.format_exc(limit=2).splitlines()[-1]}",
                baseline="应能采集",
                direction="抛异常 = 这一类没人在看",
            )
        ]


def _collect_cost(cfg: Config) -> tuple[list, str]:
    """读一次 jsonl, 供第 3 类与成本一节共用。返回 (findings, 成本正文)。

    **只读一次盘。** 两节各读一次的话, 跨午夜或撞上轮转时两节会看到不同的行, 而「两节数字
    对不上」没法自证是哪一节错。

    `PricingError` 在这里降级成一条 UNKNOWN 而不是往外抛: 单价表读不出来是配置问题, 让它
    冒到 `main` 会变成非零退出继而「日报生成失败」—— 而那条通知本该留给真故障, 且第 1、2 类
    指标其实都采到了, 不该被一份读不出的单价表带垮。
    """
    reads = cost.read_sources(days=[_dt.date.today()])
    findings = list(probes_cost.collect(reads))
    try:
        totals = cost.summarize(reads, cost.load_pricing())
    except cost.PricingError as exc:
        findings.append(
            unknown(
                "成本汇总",
                reason=f"单价表读不出来, 当日金额全部未计算: {exc}",
                baseline="应能读到带版本号的单价表",
                direction="读不到 ≠ 零花费 —— 当日花费整个没算",
            )
        )
        return findings, ""
    findings.extend(probes_spend.collect(totals, reads))
    # brief: 群消息不带四张归因表 —— 那些明细每天都进多维表格, 在这里再抄一遍是第二个副本。
    # 下限与未测到照旧输出, 它们回答的是「这个数能不能信」, 不属于明细。
    return findings, cost.render_cost_section(totals, brief=True)


def build_daily(cfg: Config, *, runner=subprocess.run) -> Report:
    report = Report(tier="日报", timestamp=local_timestamp())
    report.extend(_guard("第 1 类 公网可达性", probes_public.collect, cfg, runner=runner))
    report.extend(_guard("第 2 类 资源与 OOM", probes_resources.collect, cfg, runner=runner))

    # 第 3 类与成本一节共用一次读盘, 所以一起进 `_guard` —— 任何一步抛异常都降级成一条
    # 「未测到」, 前两类照出。第一次部署时 `metrics/` 目录必然不存在, 那条路径由
    # `cost.read_source()` 报 UNKNOWN 正常走完, 不靠这里的兜底。
    guarded = _guard("第 3 类 成本与延迟", _collect_cost, cfg)
    # `_guard` 成功时把我们的二元组包成 `[(findings, body)]`; 抛异常时给的是 `[Finding]`。
    # 用 `isinstance(tuple)` 分辨而不是看 status —— 后者会把我们自己返回的 UNKNOWN 条目
    # (例如单价表读不出来那条)误判成异常路径, 于是正文静默丢掉。
    if guarded and isinstance(guarded[0], tuple):
        cost_findings, body = guarded[0]
    else:
        cost_findings, body = guarded, ""
    report.extend(cost_findings)
    report.cost_body = body
    return report


def build_fast(cfg: Config, *, runner=subprocess.run) -> Report:
    """即时档: 仅公网可达性 + OOM。

    刻意不含 `docker stats` / `docker exec` 那些 —— 5 分钟一轮, 慢探针会让两轮叠起来。
    """
    report = Report(tier="即时档", timestamp=local_timestamp())
    report.extend(_guard("第 1 类 公网可达性", probes_public.collect, cfg, runner=runner))
    report.extend(_guard("第 2 类 OOM", probes_resources.collect_fast, cfg, runner=runner))
    return report


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "daily"
    cfg = Config.load()

    if mode == "heartbeat":
        # 轮数由 cron 包装层通过第二个参数传进来(它数 stamp 文件)。拿不到就报 0 ——
        # 0 轮会被读者一眼看出不对, 而省略这一项会让心跳退化成一句无信息的「我还活着」。
        cycles = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
        text = render.render_heartbeat(local_timestamp(), cycles, window_hours=24)
        return 0 if post(cfg, text) else 2

    if mode == "selftest":
        # 不碰生产, 只验投递链路。第一次部署时先跑这个 —— 否则「webhook 没配对」会以
        # 「日报没来」的形式出现, 而那与「一切正常」不可区分。
        text = f"【自检】HaiTun 监控投递链路 · {local_timestamp()}\n\n这条消息只验证 webhook 通路, 未采集任何生产指标。"
        okay = post(cfg, text)
        print("投递成功" if okay else "投递失败 —— 见上方 stderr", file=sys.stderr)
        return 0 if okay else 2

    if mode == "daily":
        report = build_daily(cfg)
    elif mode == "fast":
        report = build_fast(cfg)
    else:
        print(f"未知模式 {mode!r}; 可用: daily / fast / heartbeat / selftest", file=sys.stderr)
        return 1

    # 日报的主载体是**趋势表**, 不是消息。一天一行, 重跑更新那一行。
    #
    # 写表失败不改退出码: 表是留痕, 消息是告警。让写表失败变成退出码 2 会让 cron 包装层
    # 发「日报生成失败」—— 而那条通知该留给采集失败, 不是留给一次飞书 API 抖动。失败已经
    # 由 `bitable.push` 打在 stderr 上(进 cron 日志), 不会静默。
    wrote = False
    if mode == "daily":
        wrote = bitable.push(report, cfg, day=_dt.date.today().isoformat())

    # **发群的门槛是「紧急」, 不是「有异常」。**
    #
    # 2026-09-22 负责人定的: 日报只进多维表格, 有大异常才群通知。起因是连着几份日报里
    # 的「异常」是同样那三条(请求体字节数、压缩耗时、压缩占比) —— 都是真问题, 但都是**已知
    # 的常态**, 不需要今天有人马上动手。天天发一遍的结果是收信人学会跳过整条消息, 而那会
    # 连带盖住真正该看的那几条。抬基线解决了这三条, 门槛这一层解决的是下一条同类问题。
    #
    # 「紧急」= 服务对外不可用: vhost 非 200、OAuth 回调断、oauth-proxy 8090 不通、wss 掉 0、
    # 窗内 OOM。判据在 `Finding.urgent` 上由各探针自己声明, 不在这儿按名字列清单 ——
    # 清单会和探针脱节, 而脱节的形态是新探针默认静默, 没有测试会红。
    #
    # 不紧急的异常去哪儿了: 进表。表里那一行带每个指标的数和它的告警线, 越线看得出来。
    # 趋势要主动看 —— 这正是负责人要的分工。
    # **这道门槛只管日报。即时档照旧「有异常就出声」。**
    #
    # 为什么不一起收: 即时档不写表, 消息是它唯一的载体。给它也上紧急门槛的话, 一条不紧急
    # 的异常(例: 证书剩 7 天到期)就彻底没有出口了 —— 那不是收短, 是把它删掉。
    #
    # 而且噪音不在即时档: 它只看 4-5 项, 全是服务活不活的事, 本来就没有成本那类天天越线的
    # 指标。负责人抱怨的是日报。
    urgent = report.urgent_anomalies() if mode == "daily" else report.of(BAD) + report.of(UNKNOWN)

    # **写表失败本身就是紧急事件** —— 这是把日报从「消息」改成「只进表」之后新出现的缺口:
    # 表成了唯一载体, 那么表没写进去就等于今天的监控整个没留下痕迹, 而这件事原先只打在
    # stderr 上(进 cron 日志, 而没人看 cron 日志)。
    #
    # 退出码仍然不改(见上面): 退出码 2 会让包装层发「日报生成失败」, 那条留给采集失败。
    # 这里加的是一条**发群**的理由, 两者是不同的通路。
    if mode == "daily" and not wrote:
        # 加进 `report` 而不是只加进 `urgent`: 消息正文是从 `report` 渲染的, 只塞进 urgent
        # 列表的话抬头会写着「紧急 1 项: 趋势表写入」而正文里找不到这一项。
        report.add(
            unknown(
                "趋势表写入",
                reason="本轮没写进多维表格 —— 日报现在只进表, 所以这等于今天的监控没留痕。原因见宿主 cron 日志",
                baseline="每天一行",
                direction="写不进 = 今天这一行不存在",
                urgent=True,
            )
        )
        urgent = report.urgent_anomalies()

    if not urgent:
        anomaly_note = ""
        if report.has_anomaly:
            # 有异常但都不紧急时**要在 stdout 说清楚**, 否则这一行读起来像「今天啥事没有」,
            # 而 cron 日志是唯一还能看到它的地方。
            anomaly_note = f"; 有 {len(report.of(BAD))} 项异常 / {len(report.of(UNKNOWN))} 项未测到, 均不紧急, 只进表"
        # 沉默的**理由按档不同**, 这一行要说对: 日报是「有异常但都不紧急」, 即时档根本没上
        # 紧急门槛, 它沉默只可能是因为一条异常都没有。2026-09-22 生产上即时档打出「无紧急
        # 项」—— 读起来像它也在按紧急过滤, 会让人以为即时档漏报了不紧急的异常。
        reason = "无紧急项" if mode == "daily" else "全绿"
        print(
            f"[monitor] {report.tier}{reason}, 不发消息。"
            f"{len(report.findings)} 项已采集{', 已写表' if wrote else ''}{anomaly_note}。"
        )
        return 0

    text = render.render(report)
    if mode == "daily":
        # 点名紧急项。日报的正文里紧急与不紧急的异常混在同一节里按采集顺序排, 而收到消息的人
        # 要先知道「是这几项把你喊来的」。
        #
        # 即时档不加这一行: 它发出来的每一条都是异常, 抬头已经写了「异常 N」, 再加一行是
        # 同一个数说两遍。
        text = f"⚠ 紧急 {len(urgent)} 项: {', '.join(f.name for f in urgent)}\n\n" + text
    if mode == "daily" and cfg.bitable_url:
        # 附表链接: 消息只说今天哪几项有事, 趋势要看表。没有链接的话, 收到告警的人得先去
        # 翻飞书找那张表叫什么。
        text += f"\n\n趋势表(一天一行): {cfg.bitable_url}"
    if mode == "daily" and not wrote:
        # 表没写进去要在消息里说 —— 否则「表停更了」只存在于 cron 日志里, 而没人看 cron 日志。
        text += "\n\n(注意: 本轮趋势表未写入, 原因见宿主 cron 日志。表里今天缺一行。)"

    # 纯文本。卡片那条路已删: 图上一根共用告警线会在各项告警线不同时画出假越线, 而读图的人
    # 先看到柱子与线的相对位置, 再看标题 —— 一行说明修不了它。趋势交给表, 消息只负责说事。
    return 0 if post(cfg, text) else 2


if __name__ == "__main__":
    sys.exit(main())
