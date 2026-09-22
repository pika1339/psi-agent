"""指标的表示 —— 「未测到」与「零」在类型层面就分开。

## 为什么需要这个模块, 而不是让各探针直接返回字符串

报告最贵的失败模式不是数字算错, 是**观测缺口伪装成健康**。实测过一次: 量 `live render`
得 0 行, 结论写成「没渲染」, 而真相是那行日志是 DEBUG 级、生产跑 INFO —— 探针根本没有
机会看见它。`0` 与「看不见」在字符串里长得一模一样, 混进同一栏之后就再也分不开了。

于是状态是三值, 不是布尔:

    OK       量到了, 在基线内。
    BAD      量到了, 越了基线 —— 排到报告最前面。
    UNKNOWN  **没量到**。必须带 `reason`(为什么没量到), 渲染进独立一栏。

`baseline` 与 `direction` 是**必填字段**, 不是可选装饰。`232 of 232` 单看毫无异常, 只有
知道「应该是 66」才叫异常 —— 没有基线的裸数字不会触发任何人的警觉。把它们做成必填, 是
让「加一个指标却不给基线」在构造对象时就过不去, 而不是等 review 去抓。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 量到且在基线内。
OK = "ok"
#: 量到且越了基线。
BAD = "bad"
#: 没量到 —— 与「量到 0」不是一回事, 见模块 docstring。
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """一个指标的一次观测结果。

    字段划分对应报告的三条硬要求(异常最前 / 带基线与方向 / 未测到独立一栏):

    * `status`    决定它排在哪一栏;
    * `baseline`  与 `direction` 让读者不必知道背景也能判断这个数好不好;
    * `reason`    只在 UNKNOWN 时有意义 —— 「为什么没量到」本身就是要修的东西,
                  不写出来的话这一栏等于又一个「群里安静」。
    * `semantics` 给那些**读数容易被误读**的指标留的位置。例: wss=0 只证明收不到,
                  不证明发不出 —— 只起 gateway 也会真发飞书卡片。这句话必须跟着数字
                  一起进报告, 否则读者会照着反方向排查。
    """

    #: 指标名, 进报告的那一行。
    name: str
    #: 三值之一: OK / BAD / UNKNOWN。
    status: str
    #: 实测值的可读形式。UNKNOWN 时按约定写 "未测到"。
    value: str
    #: 期望值或期望区间, 例 "200" / ">= 3.0 GiB"。
    baseline: str
    #: 越界方向, 例 "应为 200" / "越低越糟"。
    direction: str
    #: 只在 UNKNOWN 时填: 为什么没量到。
    reason: str = ""
    #: 读数的语义澄清, 防误读。
    semantics: str = ""
    #: **机器可读的那个数**, 与它的告警线、单位。趋势表(`bitable.py`)只认这个字段。
    #:
    #: `value` 是给人读的字符串("1.2GiB / 3GiB (41%)"), 而表里要的是一个数。从 `value` 里
    #: 正则抠数字是错的做法: 抠不出来时最省事的写法是当 0, 而「0」与「没量到」在一列数字里
    #: 长得一模一样 —— 一条趋势线会因此从 81 掉到 0, 看起来像磁盘被清空了, 实际是探针瞎了。
    #: 所以这条要靠类型而不是靠解析。
    #:
    #: 留空(None)表示这一项没有单一数值可入表(例: "运行时 10, sysctl.d 无声明" 是两个数加
    #: 一句话)。留空的项仍然进报告正文, 只是不占趋势表的列。
    num: float | None = None
    #: `num` 的告警线, 同单位。填了 `num` 就必须填它 —— 见 `__post_init__`。
    num_warn: float | None = None
    #: `num` 的单位, 进表头。空串表示无量纲(次数这类)。
    unit: str = ""
    #: 同一指标下的**附属数值**, 例 `{"p50": 172.9}`。
    #:
    #: 为什么不把 p50 拆成独立的 Finding: 那样就得给它编一条告警线(`num_warn` 必填), 而
    #: p50 真的没有告警线 —— 这一对指标的基线是写在 p95 上的("p95 < 600 KiB")。编一条线
    #: 出来就是给表加一个假刻度, 比不进表坏。所以附属数**不要求**告警线。
    #:
    #: 与 `num` 同规: UNKNOWN 不得带 —— 没量到在表里是空格子。
    extra_nums: dict[str, float] = field(default_factory=dict)
    #: **这一项坏掉时值不值得马上把人喊来。**
    #:
    #: 默认 False —— 绝大多数指标不值得。值得的只有一类: **服务对外不可用**(vhost 非 200、
    #: oauth-proxy 8090 不通、进程被 OOM 杀掉、飞书长连接断)。那几项坏了, 用户此刻就在
    #: 撞墙, 而且修法通常是「重启点什么」, 越早越好。
    #:
    #: 为什么做成字段而不是在 `run.py` 里按名字列一张清单: 清单会和探针脱节 —— 加一个新探针
    #: 的人不会想到去改另一个文件里的名字清单, 于是新探针默认落进「静默」那一侧, 而这种漏
    #: 不会让任何测试变红。硬编码清单当判据把责任指反这件事踩过两次。放在字段上, 紧急性由
    #: 写探针的人在同一处声明。
    #:
    #: 与 `status` 是两个维度: `urgent=True` 只说「**如果**它坏了就要喊人」, 坏没坏由
    #: `status` 说。OK 的紧急项不发消息。
    urgent: bool = False

    def __post_init__(self) -> None:
        if self.status not in (OK, BAD, UNKNOWN):
            raise ValueError(f"status 只能是 ok/bad/unknown, 收到 {self.status!r}")
        # UNKNOWN 不给理由 = 又一个「群里安静」。构造期就拦, 不留到 review。
        if self.status == UNKNOWN and not self.reason:
            raise ValueError(f"{self.name}: UNKNOWN 必须带 reason")
        # UNKNOWN 不得带数。没量到就是没有数 —— 在趋势表里, 一个 0 与一个真实的低读数完全
        # 不可区分, 而折线图会把它画成一次暴跌。表里「没量到」的正确形态是**空格子**。
        #
        # 这一条**排在 num_warn 那条之前**: `unknown(..., num=0.0)` 同时触犯两条, 而先报
        # 「缺告警线」会把调用方引向「那我补个 num_warn」—— 补完就静默通过, 0 进了表。
        # 报错顺序决定了修法方向, 两条都在不等于顺序无关。
        if self.status == UNKNOWN and self.num is not None:
            raise ValueError(f"{self.name}: UNKNOWN 不得带 num —— 没量到在表里是空格子, 不是 0")
        # 附属数单独拦一条: 它不受上面那条管(`num` 可以是 None 而 extra_nums 有货), 而
        # `unknown(..., extra_nums={"p50": 0.0})` 进表和 `num=0.0` 是同一个坏结果。
        if self.status == UNKNOWN and self.extra_nums:
            raise ValueError(f"{self.name}: UNKNOWN 不得带 extra_nums —— 没量到在表里是空格子, 不是 0")
        # 有数就必须有线。一列 41 单看不知是宽裕还是快撞顶 —— 与 `baseline` 必填同一个道理。
        # 线也进表(一列 `xxx 告警线`), 这样看表的人不必回来翻代码。
        if self.num is not None and self.num_warn is None:
            raise ValueError(f"{self.name}: 填了 num 就必须填 num_warn —— 没有告警线的数字没有刻度")


def ok(
    name: str,
    value: str,
    baseline: str,
    direction: str,
    semantics: str = "",
    *,
    num: float | None = None,
    num_warn: float | None = None,
    unit: str = "",
    extra_nums: dict[str, float] | None = None,
    urgent: bool = False,
) -> Finding:
    return Finding(
        name=name,
        status=OK,
        value=value,
        baseline=baseline,
        direction=direction,
        semantics=semantics,
        num=num,
        num_warn=num_warn,
        unit=unit,
        extra_nums=dict(extra_nums or {}),
        urgent=urgent,
    )


def bad(
    name: str,
    value: str,
    baseline: str,
    direction: str,
    semantics: str = "",
    *,
    num: float | None = None,
    num_warn: float | None = None,
    unit: str = "",
    extra_nums: dict[str, float] | None = None,
    urgent: bool = False,
) -> Finding:
    return Finding(
        name=name,
        status=BAD,
        value=value,
        baseline=baseline,
        direction=direction,
        semantics=semantics,
        num=num,
        num_warn=num_warn,
        unit=unit,
        extra_nums=dict(extra_nums or {}),
        urgent=urgent,
    )


def unknown(
    name: str,
    reason: str,
    baseline: str,
    direction: str,
    semantics: str = "",
    *,
    urgent: bool = False,
) -> Finding:
    """没量到。**不要用 `ok(value="0")` 代替** —— 见模块 docstring。

    `urgent` 在这里同样有意义: 一个紧急项**没量到**与它坏掉同级 —— 探针瞎了的时候, 服务
    是死是活没人知道。所以 vhost 探针抛异常那条 UNKNOWN 也该喊人。
    """
    return Finding(
        name=name,
        status=UNKNOWN,
        value="未测到",
        baseline=baseline,
        direction=direction,
        reason=reason,
        semantics=semantics,
        urgent=urgent,
    )


@dataclass
class Report:
    """一次采集的全部结果。

    `tier` 用来区分日报与即时档 —— 两档的失败通知文案不同, 而「哪一档没出声」是判断
    故障范围的第一条线索。
    """

    tier: str
    #: 宿主本地时间的时间戳字符串。**宿主时区**, 见 `config.local_timestamp()` 的注释。
    timestamp: str
    findings: list[Finding] = field(default_factory=list)
    #: 成本一节的正文(`cost.render_cost_section()` 的输出), 空串表示这一档不含成本。
    #:
    #: 为什么不把它拆成一条条 `Finding`: 按容器/按模型/按会话那几张表是**归因明细**, 条目数
    #: 随会话数涨, 逐条进 `findings` 会把「异常排最前」那条规则冲掉 —— 几十行明细会把真正
    #: 的异常挤出视野。四个带基线的成本数仍然是 `Finding`(见 `probes_spend`), 明细走这里。
    cost_body: str = ""

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings: list[Finding]) -> None:
        self.findings.extend(findings)

    def of(self, status: str) -> list[Finding]:
        return [f for f in self.findings if f.status == status]

    @property
    def has_anomaly(self) -> bool:
        """有异常 —— 即时档据此决定要不要出声。

        **UNKNOWN 也算**: 探针瞎了与服务挂了在后果上同级, 都意味着这一轮没人在看。
        """
        return bool(self.of(BAD) or self.of(UNKNOWN))

    def urgent_anomalies(self) -> list[Finding]:
        """坏掉或没量到的**紧急**项 —— 日报据此决定要不要发群。

        与 `has_anomaly` 分开: 那个是「有没有事」(决定要不要在正文里排最前), 这个是「要不要
        现在把人喊来」。字节数涨了 20% 是有事但不紧急, vhost 502 是两者都成立。

        返回列表而不是 bool: 发群的那条消息要点名是哪几项紧急, 只给 bool 的话调用方得再筛
        一遍, 而两处筛法早晚会不一致。
        """
        return [f for f in self.findings if f.urgent and f.status in (BAD, UNKNOWN)]
