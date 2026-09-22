"""报告渲染 —— 三条硬要求都落在这里。

## 日报最大的失败模式不是数字错, 是没人读

所以:

1. **异常排最前, 正常只给计数。** BAD 与 UNKNOWN 逐条展开在最上面; OK 的那些收成一行
   「N 项正常」, 不列名字也不逐条占行(为什么连名字都不列, 见 `render()` 里那段注释)。
2. **每个指标带基线与方向。** 每条展开项都带 `基线` 与方向两栏, 让「加了指标却没给基线」
   在渲染时就显形 —— 而 `Finding` 那层已经把两个字段做成必填。
3. **「未测到」与「零」两栏, 不混。** UNKNOWN 有自己的一节, 标题就叫「未测到(观测缺口)」,
   且每条带「为什么没量到」。把它们并进 BAD 会让人去查服务, 而问题在探针; 并进 OK 则是
   **观测缺口伪装成健康** —— 量 `live render` 得 0 行那次就是这样错过去的。

## 可读性: 三个改动, 各自对着一个实测到的读不下去的地方

2026-09-21 第一份真实日报发出来之后, 负责人的原话是「可读性太差」。具体差在哪:

* **看不出全局。** 抬头只有一个 `【有观测缺口】`, 要把整篇读完才知道是 1 项缺口还是 13 项。
  现在抬头下面无条件加一行 `异常 N · 未测到 N · 正常 N` —— 三个数一眼看完, 决定要不要往下读;
* **每条挤成一行。** 原格式是 `名称: 实测 (基线 X | 方向 Y)` 再跟两条缩进小字, 一条 200 多
  字符, 名称和实测值淹在括号里。现在名称单独占行, 实测/基线/方向并排在缩进的第二行;
* **相同原因重复 N 遍。** 未测到往往是同一个成因(某个来源读不到)波及好几个指标, 原来每条
  都把那段原因完整抄一遍。现在按原因归组, 原因只出现一次, 组内列指标名与各自基线。

刻意**不做**硬换行: 飞书客户端自己按屏宽折行, 这边再按猜的宽度折一次会变成两套折行叠加,
中文长句里出现半行空白。行长由结构控制, 不由 `textwrap` 控制。

## 为什么纯文本而不是飞书卡片

卡片的 schema 变过, 而这条链路要在故障时还能工作。纯文本少一个会变的依赖。
"""

from __future__ import annotations

from findings import BAD, OK, UNKNOWN, Report

#: 单条消息的字符上限。飞书自定义机器人对超长文本会整条拒收 —— 那等于「群里安静」,
#: 与一切正常不可区分。所以宁可截断并在末尾说明截断了。
MAX_CHARS = 8000

#: 单条「原因」在群消息里的字符上限。实测最长一条 120 字, 一条就顶掉三条别的缺口的位置。
#: 截断而不是不显示: 原因是定位起点, 缺了就只知道「有东西没测到」而不知道从哪查。
REASON_CLIP = 60


def _clip(text: str, limit: int = REASON_CLIP) -> str:
    """超长就截断并留省略号 —— 留省略号是为了让人知道后面还有, 而不是以为原因就这么短。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _anomaly_block(idx: int, finding) -> list[str]:
    """一条异常占两行: 名称、实测+基线+方向。

    名称单独占行是为了让「哪一项坏了」在扫视时就能抓到 —— 挤进括号堆里的名称等于没写。
    `基线` 与方向仍在同一行内出现(硬要求 2), 只是不再跟名称抢位置。

    **不输出 `semantics`。** 那是「为什么这个数糟」的解释, 每条一行、每行一两句, 实测让
    一条异常从两行涨到三四行。群消息要回答的是「今天有没有事、是哪一件」, 解释属于查的时候
    才要看的东西 —— 而且 semantics 对同一项指标每天一字不变, 天天重发就是天天刷同一句话。
    它仍留在 `Finding` 上, 多维表格与命令行照旧能取到。
    """
    return [
        f"  {idx}. {finding.name}",
        f"       实测 {finding.value}    基线 {finding.baseline}    {finding.direction}",
    ]


def _unknown_groups(unknowns) -> list[str]:
    """未测到按**原因**归组, 原因只写一次。

    实测第一份日报里 13 项里有多项共用同一句原因(某个来源读不到波及好几个指标), 每条抄一遍
    之后整节里同一段话出现好几次, 而真正不同的信息(是哪几个指标瞎了)被埋在重复文本之间。

    归组的键是 `reason` 原文。**不做模糊归并**: 两段只差几个字的原因很可能是两个不同的缺口,
    合起来会让其中一个消失在另一个的标题下。
    """
    groups: dict[str, list] = {}
    for f in unknowns:
        groups.setdefault(f.reason or "(未给原因)", []).append(f)

    out: list[str] = []
    for i, (reason, items) in enumerate(groups.items(), 1):
        out.append(f"  {i}. " + ", ".join(f.name for f in items))
        # 原因**截断**而不是整段抄: 实测有一条原因 120 字(连「本卡不回改 src/ 埋点」这种
        # 对收信人无用的交代都在里面), 一条就顶掉三条别的缺口。够定位就行, 全文在 cron 日志。
        out.append(f"       原因: {_clip(reason)}")
        # 不再逐项列 `direction`。它是「未测到为什么要紧」的说明(如「未测到 ≠ 零花费」),
        # 每项一句且每天一字不变 —— 而那个道理由本节标题「观测缺口, 不等于零」讲一次就够。
        # 基线仍逐项给: 归组省掉的是重复的话, 不是每项的刻度(硬要求 2)。
        if len(items) > 1:
            for f in items:
                out.append(f"       · {f.name} 基线 {f.baseline}")
        else:
            out.append(f"       基线 {items[0].baseline}")
    return out


def render(report: Report) -> str:
    """渲染一份报告。异常在最前, 正常只给计数, 未测到独立一节。"""
    anomalies = report.of(BAD)
    unknowns = report.of(UNKNOWN)
    healthy = report.of(OK)

    head = "【异常】" if anomalies else ("【有观测缺口】" if unknowns else "【正常】")
    lines = [f"{head} HaiTun {report.tier} · {report.timestamp}"]
    if report.findings:
        # 三个数放在抬头下面: 抬头只说最坏的那一档, 而「1 项缺口」与「13 项缺口」是两件事。
        lines.append(f"异常 {len(anomalies)} · 未测到 {len(unknowns)} · 正常 {len(healthy)}")

    if anomalies:
        lines.append("")
        lines.append(f"■ 异常 {len(anomalies)} 项 —— 需处理")
        for i, f in enumerate(anomalies, 1):
            lines += _anomaly_block(i, f)

    if unknowns:
        lines.append("")
        # 标题刻意写「观测缺口」而不是「采集失败」: 后者听起来像脚本的小毛病, 而它的
        # 实际含义是「这一项本轮没有人在看」, 与服务挂了同级。
        lines.append(f"■ 未测到 {len(unknowns)} 项(观测缺口, 不等于零)")
        lines += _unknown_groups(unknowns)

    if healthy:
        lines.append("")
        # 只给计数, **不列名字**。那串名字实测 262 字符且每天一字不变 —— 天天重发一份不变的
        # 清单, 读的人第二天起就会跳过整节, 而跳过的习惯会连带盖住上面的异常。
        #
        # 计数本身要留: 它是「有多少项在被看着」, 少了这个数就分不清「一切正常」与
        # 「采集只跑了三项而那三项正常」—— 后者正是 render 末尾那句要防的事。
        lines.append(f"□ 正常 {len(healthy)} 项")

    if report.cost_body:
        # 排在异常与未测到**之后**: 归因明细的行数随会话数涨, 放前面会把异常挤出视野, 而
        # 超长截断砍掉的正是末尾。四个带基线的成本数已经作为 Finding 排在上面了, 所以这段
        # 被截断也不会丢掉结论 —— 丢的是明细。
        lines.append("")
        lines.append(report.cost_body)

    if not report.findings:
        lines.append("")
        lines.append("■ 一个指标都没采集到 —— 这不是「一切正常」, 是采集整体没跑起来。")

    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        # 截断也要出声: 静默丢尾部会让异常掉出视野, 而异常排在最前面所以截掉的是正常那段。
        text = text[: MAX_CHARS - 80].rstrip() + f"\n…(超长已截断, 全文 {len(text)} 字符, 见宿主上的 cron 日志)"
    return text


def render_failure(tier: str, timestamp: str, detail: str) -> str:
    """「日报生成失败」通知。

    这条通知存在的唯一理由: 没有它, 脚本挂掉的表现是**群里安静**, 而安静与「一切正常」
    不可区分。所以它必须由 cron 那一层在**非零退出**时无条件发出, 且正文带上 stderr 尾部
    —— 只说「失败了」会让人从零开始查。
    """
    return "\n".join(
        [
            f"【生成失败】HaiTun {tier} · {timestamp}",
            "",
            "■ 监控脚本自身非零退出 —— 本轮指标全部未采集。",
            "  这条消息的含义是「不知道生产是什么状态」, 不是「生产正常」。",
            "",
            "错误尾部:",
            (detail.strip() or "(无 stderr 输出)")[-2000:],
        ]
    )


def render_heartbeat(timestamp: str, cycles: int, window_hours: int) -> str:
    """即时档心跳。

    为什么即时档也要心跳: 即时档只在**发现异常时**出声(否则 5 分钟一条会被无视, 继而被
    静音), 但这意味着「即时档自己死了」与「一直没异常」在群里长得一模一样。心跳是这条链路
    的唯一存活证据。

    每天一条(与日报同一档频率), 内容是「过去 N 小时跑了多少轮」。**给轮数而不是只说
    「存活」**: 轮数少于预期说明 cron 漏跑了 —— 那是一种只有对比才看得出的故障。
    """
    expected = window_hours * 60 // 5
    return "\n".join(
        [
            f"【心跳】HaiTun 即时档存活 · {timestamp}",
            "",
            f"过去 {window_hours} 小时执行 {cycles} 轮(按 5 分钟一轮, 预期约 {expected} 轮)。",
            "  轮数明显少于预期 = cron 漏跑, 即时档的沉默不能当作「无异常」。",
            "  即时档只在发现异常时出声, 因此这条心跳是它唯一的存活证据。",
        ]
    )
