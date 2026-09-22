"""成本报告层 —— 读 metrics jsonl, 用带版本号的单价表算钱。

## 分层立场: 内核记 token, 本层算钱

内核(`src/psi_agent/metrics.py` + `session/`)只记 **token 数与模型 id**, 一个字段都不记金额。
金额由本模块算。理由是单价会变、有阶梯与缓存折扣: 把金额烤进埋点意味着单价一改**历史就不可
比**, 且无法按新价重算。

所以本模块**不回头往埋点里加金额字段**。反过来, 若发现埋点缺了算钱必需的原料, 也不在这里
偷偷补 —— 见文件末尾 `MISSING_UPSTREAM_INPUTS` 那段, 缺口写成文字由人决定要不要回改上游。

## 为什么纯标准库、不 import 内核

**内核在容器里, 报告在宿主。** `import psi_agent` 在宿主上过不去(宿主没装这个包, 也不该装
—— 装了就等于宿主多一份会漂移的副本)。两侧唯一的契约是 jsonl 的**字段名**, 因此本模块里
那些字段名字符串是刻意的重复, 不是漏抽象: 抽成共享常量就需要一个双方都 import 得到的模块,
而那个模块不存在。

字段名取自 B 卡 `session/agent.py` 的 `_usage_fields()` 与两处 `metrics.record()` 调用
**实测抄录**, 不是照方案文档猜的。

## 三条硬要求落在哪

1. **打印带单价表版本号 + 「未与账单对过」** —— `Pricing.version` 进每个渲染出口,
   `NOT_RECONCILED` 那行由 `render_cost_section()` 无条件输出。本期不做对账(要登上游控制台,
   无法自动化, 负责人已定), 这行字是唯一的诚实性保障。
2. **缓存命中不省字节, 只省钱** —— `cached` 单价单独乘。首字延迟是**上传带宽**决定的
   (≈ 字节数 ÷ 230KB/s), 缓存不改变要传的字节数。这两件事在报告里**分节写**:
   `render_cost_section()` 只谈钱, 字节/延迟归 D 卡那一侧, 谁也不拿来解释对方。
3. **「未测到 usage」的回合不静默算 0 元** —— 单独一栏 `no_usage_turns`, 且
   `CostTotals.is_lower_bound` 为真时所有金额都标成**下限**。

## 「未测到」与「零」在本模块里的四个出处

都不能塌成 0 —— 观测缺口伪装成健康是这份报告最贵的失败模式:

| 出处 | 表示 |
| --- | --- |
| `usage_reported=false` 的行 | `no_usage_turns` 计数, 金额不计入, 总额标为下限 |
| 单价表里没有的 model | `unpriced_turns` 计数, 同样标为下限 |
| 某个容器的 appdata 根不存在 | `SourceRead.status == UNKNOWN` 带 reason |
| `metrics/` 目录整个不存在 | 同上。**首次部署前它必然不存在**, 这是正常路径不是故障 |
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# jsonl 字段名 —— 抄自内核实测, 见模块 docstring
# ---------------------------------------------------------------------------

#: 一行一回合。
EVENT_TURN = "turn"
#: 压缩独立一行, 不并进触发它的那个回合(W#7)。并进去则单回合看着便宜而月账单对不上。
EVENT_COMPACTION = "compaction"

#: 上游到底报没报 usage。**与「某个字段有没有值」不是同一个问题** —— provider 可能报了
#: usage 但不带 `*_tokens_details` 子字典, 于是 reported=True 而明细为 null。
F_USAGE_REPORTED = "usage_reported"
F_MODEL = "model"
F_PROMPT = "prompt_tokens"
F_COMPLETION = "completion_tokens"
F_CACHED = "cached_tokens"
F_REASONING = "reasoning_tokens"
F_EVENT = "event"
F_SESSION = "session_id"
F_TS = "ts"

#: 打印在每一处金额旁边。本期不做对账, 这行是唯一的诚实性保障。
NOT_RECONCILED = "未与账单对过"

#: 缓存与延迟的关系 —— 防读者拿一个解释另一个。
CACHE_SEMANTICS = "缓存命中省钱但不省字节; 首字延迟由上传带宽决定(≈ 字节数 ÷ 230KB/s), 不随缓存命中率下降"


# ---------------------------------------------------------------------------
# 单价表
# ---------------------------------------------------------------------------


class PricingError(Exception):
    """单价表读不出来。

    **刻意往外抛而不是兜底成空表。** 空表会让每个模型都落进「未定价」, 报出一份全是下限
    的报告 —— 那读起来像「上游没报 usage」, 把配置问题伪装成上游问题。而调用方
    (`monthly_cost.py` / 日报)本来就要在非零退出时发「生成失败」, 抛出去正好走那条路。

    历史教训: 曾有一次变异复核四条全绿, 暴露的是「异常照旧往外传」这条没人盯。本异常
    有自己的判据。
    """


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的各类 token 单价, 单位是 `Pricing.unit_tokens` 个 token 的价钱。"""

    prompt: float
    cached: float
    completion: float
    reasoning: float = 0.0
    #: reasoning_tokens 是否已被算进 completion_tokens。OpenAI 兼容语义下
    #: `completion_tokens_details.reasoning_tokens` 是 completion 的**明细项**,
    #: 所以默认 True, 此时 reasoning 单价不参与乘法 —— 否则同一批 token 收两遍钱。
    reasoning_in_completion: bool = True


@dataclass(frozen=True)
class Pricing:
    """一版单价表。

    `version` 必须进每一处金额的打印(W#8): 没有版本号的成本数字无法重算, 也无法解释
    两天之间的跳变是调价还是用量变化。
    """

    version: str
    effective_date: str
    currency: str
    unit_tokens: int
    models: dict[str, ModelPrice]
    source: str = ""
    path: str = ""

    def price_of(self, model: str | None) -> ModelPrice | None:
        """取一个模型的单价; 表里没有则 `None`(调用方记为「未定价」, **不当 0**)。"""
        if not model:
            return None
        return self.models.get(model)


def _default_pricing_dir() -> Path:
    return Path(__file__).resolve().parent / "pricing"


def load_pricing(
    *,
    directory: str | os.PathLike[str] | None = None,
    version: str | None = None,
    on_date: _dt.date | None = None,
) -> Pricing:
    """读单价表。

    选版规则, 按优先级:

    1. 显式 `version`(或环境变量 `PSI_COST_PRICING`)—— 指名道姓要某一版。阴性判据就是
       这么换版的, 生产上重算旧账也走这条;
    2. 否则取 `effective_date` **不晚于** `on_date`(默认今天)的最新一版。

    **按 `effective_date` 选, 不按文件 mtime。** mtime 区分不了「投放」与「就地编辑」,
    拿它当判据两个方向都会误判。
    """
    directory = Path(directory) if directory is not None else _default_pricing_dir()
    want = version or (os.environ.get("PSI_COST_PRICING", "") or "").strip() or None

    if not directory.is_dir():
        raise PricingError(f"单价表目录不存在: {directory}")

    loaded: list[Pricing] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PricingError(f"单价表读不出来: {path} ({exc!r})") from exc
        loaded.append(_parse_pricing(raw, path))

    if not loaded:
        raise PricingError(f"单价表目录里没有 .json: {directory}")

    if want:
        for p in loaded:
            if p.version == want:
                return p
        have = ", ".join(sorted(p.version for p in loaded))
        raise PricingError(f"指名的单价表版本不存在: {want!r} (现有: {have})")

    on_date = on_date or _dt.date.today()
    eligible = [p for p in loaded if _parse_date(p.effective_date) <= on_date]
    if not eligible:
        earliest = min(p.effective_date for p in loaded)
        raise PricingError(f"没有在 {on_date.isoformat()} 之前生效的单价表(最早一版 {earliest})")
    return max(eligible, key=lambda p: _parse_date(p.effective_date))


def _as_float(value: object, where: str, path: Path) -> float:
    """单价字段转 float, 转不了就抛。

    **不兜底成 0.0** —— 一个写成 `"2.0abc"` 的单价兜成 0 之后, 那类 token 就静默免费了,
    而报告会显示一切正常。`bool` 显式拒: 它是 `int` 子类, JSON `true` 会变成单价 1。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"{where} 单价不是数字: {value!r} ({path})")
    return float(value)


def _parse_date(text: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise PricingError(f"effective_date 不是 YYYY-MM-DD: {text!r}") from exc


def _parse_pricing(parsed: object, path: Path) -> Pricing:
    if not isinstance(parsed, dict):
        raise PricingError(f"单价表不是 JSON 对象: {path}")
    # 显式标成 `dict[str, object]`: `json.loads` 回的是 `object`, 不重新绑一个带类型的
    # 名字, 下面每次 `raw[...]` 都会被类型检查报一条, 把真诊断淹在噪音里。
    raw: dict[str, object] = {str(k): v for k, v in parsed.items()}
    for key in ("version", "effective_date", "currency", "unit_tokens", "models"):
        if key not in raw:
            raise PricingError(f"单价表缺必含字段 {key!r}: {path}")
    models_raw = raw["models"]
    if not isinstance(models_raw, dict):
        raise PricingError(f"models 不是 JSON 对象: {path}")

    models: dict[str, ModelPrice] = {}
    for name, raw_item in models_raw.items():
        if not isinstance(raw_item, dict):
            raise PricingError(f"模型 {name!r} 的单价不是 JSON 对象: {path}")
        item: dict[str, object] = {str(k): v for k, v in raw_item.items()}
        # 三类单价必须齐。缺一类就抛 —— 默认成 0 会让那一类 token 静默免费。
        for key in ("prompt", "cached", "completion"):
            if key not in item:
                raise PricingError(f"模型 {name!r} 缺 {key!r} 单价: {path}")
        models[str(name)] = ModelPrice(
            prompt=_as_float(item["prompt"], f"{name}.prompt", path),
            cached=_as_float(item["cached"], f"{name}.cached", path),
            completion=_as_float(item["completion"], f"{name}.completion", path),
            reasoning=_as_float(item.get("reasoning", 0.0), f"{name}.reasoning", path),
            reasoning_in_completion=bool(item.get("reasoning_in_completion", True)),
        )

    unit = int(_as_float(raw["unit_tokens"], "unit_tokens", path))
    if unit <= 0:
        raise PricingError(f"unit_tokens 必须为正: {unit} ({path})")

    return Pricing(
        version=str(raw["version"]),
        effective_date=str(raw["effective_date"]),
        currency=str(raw["currency"]),
        unit_tokens=unit,
        models=models,
        source=str(raw.get("source", "")),
        path=str(path),
    )


# ---------------------------------------------------------------------------
# jsonl 来源: 三个容器各一份
# ---------------------------------------------------------------------------

#: 生产三个容器的 appdata 在**宿主**上的位置(已实测 bind mount, 宿主可直接读)。
#:
#: 只有 gateway 显式设了 `PSI_APPDATA=/workspace/.psi/appdata`; 另两个没设, 落回
#: platformdirs 默认, 而容器内 `/root/.local/share/` 不存在 —— 实际也写到了
#: `/workspace/.psi/appdata`。
#:
#: 三份都读: 生产**一人一容器**, 容器就是成本归因的一个维度。少读一份不是少一点数据,
#: 是少一个人的账。
DEFAULT_SOURCES: tuple[tuple[str, str], ...] = (
    ("gateway", "/srv/haitun/psi-agent/workspace/.psi/appdata"),
    ("luolin", "/srv/haitun/psi-agent/workspace-luolin/.psi/appdata"),
    ("chengxx", "/srv/haitun/psi-agent/workspace-chengxx/.psi/appdata"),
)

#: 采到了。
OK = "ok"
#: **没采到** —— 与「采到 0 行」不是一回事, 必须带 reason。
UNKNOWN = "unknown"


def sources_from_env(env: dict[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """从 `PSI_COST_APPDATA_ROOTS` 取来源, 未配则用 `DEFAULT_SOURCES`。

    格式 `名字=路径` 逗号分隔。**路径可配置而不写死在函数体里**: 宿主路径会随部署拓扑变
    (部署拓扑是过渡态 —— 新加坡节点只为绕备案, 终局搬回境内), 写死等于每次搬家改代码。
    """
    env = dict(os.environ if env is None else env)
    raw = (env.get("PSI_COST_APPDATA_ROOTS", "") or "").strip()
    if not raw:
        return DEFAULT_SOURCES
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, path = item.partition("=")
        if not sep:
            # 只给路径时拿目录名当容器名, 好过整条丢掉。
            out.append((Path(item).name or item, item))
        else:
            out.append((name.strip(), path.strip()))
    return tuple(out) if out else DEFAULT_SOURCES


@dataclass
class SourceRead:
    """一个来源的读取结果。

    `status == UNKNOWN` 时 `rows` 为空, 但**这不等于该来源零花费** —— 调用方必须把它
    渲染成「未测到」而不是 0。整个类存在的理由就是不让这两者塌成同一个空列表。
    """

    name: str
    root: str
    status: str
    rows: list[dict[str, object]] = field(default_factory=list)
    #: 只在 UNKNOWN 时有意义: 为什么没读到。
    reason: str = ""
    #: 读到但解析不出来的行数。坏行不静默丢 —— 它意味着有花费没被算进去。
    malformed: int = 0
    #: 实际读了哪些天文件, 进报告好让人核对覆盖范围。
    files_read: list[str] = field(default_factory=list)


def read_source(
    name: str,
    root: str | os.PathLike[str],
    *,
    days: Sequence[_dt.date] | None = None,
) -> SourceRead:
    """读一个来源的 metrics jsonl。

    三档兜底, 每一档都报 UNKNOWN 而不是当 0:

    1. appdata 根不存在 —— 容器没起过, 或路径配错了;
    2. `metrics/` 子目录不存在 —— **首次部署前它必然不存在**, 这是正常路径。日报依赖
       这里不崩: 崩掉的话第一次部署就触发「日报生成失败」, 而那条通知本该留给真故障;
    3. `metrics/` 在但一个目标天文件都没有 —— 那一天没有回合, 或者轮转把它清掉了。

    第 2、3 档刻意分开: 「目录在但内容不全会让兜底链提前终止」踩过一次, `isdir` 判据
    全绿抓不到, 因为目录确实存在。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return SourceRead(name, str(root_path), UNKNOWN, reason=f"appdata 根不存在: {root_path}")

    metrics_dir = root_path / "metrics"
    if not metrics_dir.is_dir():
        return SourceRead(
            name,
            str(root_path),
            UNKNOWN,
            reason=f"metrics/ 目录不存在(首次部署前正常): {metrics_dir}",
        )

    if days is None:
        paths = sorted(metrics_dir.glob("*.jsonl"))
    else:
        paths = [metrics_dir / f"{d.isoformat()}.jsonl" for d in days]
        paths = [p for p in paths if p.is_file()]

    if not paths:
        span = "全部" if days is None else ", ".join(d.isoformat() for d in days)
        return SourceRead(
            name,
            str(root_path),
            UNKNOWN,
            reason=f"metrics/ 在但没有目标天文件({span}): {metrics_dir}",
        )

    rows: list[dict[str, object]] = []
    malformed = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            malformed += 1
            del exc
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 坏行单独计数而不是静默跳过: 一行坏掉意味着有花费算不进来,
                # 而「少算了钱」与「花得少」在总额里长得一样。
                malformed += 1
                continue
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                malformed += 1

    return SourceRead(
        name,
        str(root_path),
        OK,
        rows=rows,
        malformed=malformed,
        files_read=[p.name for p in paths],
    )


def read_sources(
    sources: Iterable[tuple[str, str]] | None = None,
    *,
    days: Sequence[_dt.date] | None = None,
) -> list[SourceRead]:
    """三份都读。缺哪份就在结果里留一条 UNKNOWN, **不跳过**。

    跳过的后果是那个容器从报告里整个消失, 而消失与「这人今天没用」不可区分。
    """
    sources = tuple(sources) if sources is not None else sources_from_env()
    return [read_source(name, root, days=days) for name, root in sources]


# ---------------------------------------------------------------------------
# 算钱
# ---------------------------------------------------------------------------


def _opt_int(value: object) -> int | None:
    """取一个 token 计数, **缺失返回 None 而不是 0**。

    显式拒 `bool`: 它是 `int` 子类, JSON 里的 `true` 会变成 1 个 token。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


@dataclass(frozen=True)
class RowCost:
    """一行 jsonl 的算钱结果。

    `amount is None` 表示**算不出**(上游没报 usage, 或模型不在单价表里), 与
    `amount == 0.0`(真的没花钱)不是一回事。这个区分是本模块的主轴, 不要在任何
    汇总里把它塌成 0 —— 塌了之后当日花费会在上游最不健康的时候显得最低。
    """

    event: str
    session_id: str
    container: str
    model: str | None
    amount: float | None
    #: 算不出的原因: "" / "no_usage" / "unpriced"
    gap: str = ""
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


def cost_of_row(row: dict[str, object], pricing: Pricing, *, container: str = "") -> RowCost:
    """算一行的钱。

    **四类 token 分开乘**, 单价各不相同:

        未命中 prompt = (prompt_tokens - cached_tokens) * price.prompt
        命中缓存      = cached_tokens                   * price.cached
        completion    = completion_tokens               * price.completion
        reasoning     = reasoning_tokens                * price.reasoning
                        (仅当 reasoning 不算在 completion 里时才这一项)

    把缓存命中混进普通 prompt 一起乘是**错的**: `cached` 单价通常只有 `prompt` 的
    1/4, 混算会把账单高估。注意缓存只省钱、**不省字节** —— 请求体照样整个上传, 首字
    延迟不因此下降(它 ≈ 字节数 ÷ 230KB/s)。

    两种算不出的情况都返回 `amount=None` 并带 `gap`:

    * `usage_reported` 为假 —— 上游没报, 这个回合花了多少**未知**;
    * 模型不在单价表里 —— 单价未知。两者都不许当 0 元。
    """
    event = str(row.get(F_EVENT, "") or "")
    session_id = str(row.get(F_SESSION, "") or "")
    model = row.get(F_MODEL)
    model_name = str(model) if isinstance(model, str) and model else None

    prompt = _opt_int(row.get(F_PROMPT))
    cached = _opt_int(row.get(F_CACHED))
    completion = _opt_int(row.get(F_COMPLETION))
    reasoning = _opt_int(row.get(F_REASONING))

    base = RowCost(
        event=event,
        session_id=session_id,
        container=container,
        model=model_name,
        amount=None,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
    )

    # `usage_reported` 回答的是「上游到底报没报」。缺这个键时按「没报」处理:
    # 老版本埋点写的行不带它, 而把它当成报了会凭 null token 算出 0 元。
    if not bool(row.get(F_USAGE_REPORTED, False)):
        return _with(base, gap="no_usage")

    price = pricing.price_of(model_name)
    if price is None:
        # 表里没有这个模型。**不猜、不拿别的模型的价顶上** —— 猜出来的数字会以
        # 「实测成本」的名义传下去。计入未定价栏, 总额标下限。
        return _with(base, gap="unpriced")

    unit = float(pricing.unit_tokens)
    cached_n = cached or 0
    prompt_n = prompt or 0
    # 未命中的那部分。`max(0, ...)` 防上游把 cached 报得比 prompt 还大(见过 usage
    # 字段互相矛盾的 provider); 负数会变成负金额, 悄悄冲掉别的回合的花费。
    uncached_n = max(0, prompt_n - cached_n)

    amount = (uncached_n * price.prompt + cached_n * price.cached + (completion or 0) * price.completion) / unit
    if not price.reasoning_in_completion:
        amount += (reasoning or 0) * price.reasoning / unit

    return _with(base, amount=amount)


def _with(base: RowCost, *, amount: float | None = None, gap: str = "") -> RowCost:
    return RowCost(
        event=base.event,
        session_id=base.session_id,
        container=base.container,
        model=base.model,
        amount=amount,
        gap=gap,
        prompt_tokens=base.prompt_tokens,
        cached_tokens=base.cached_tokens,
        completion_tokens=base.completion_tokens,
        reasoning_tokens=base.reasoning_tokens,
    )


@dataclass
class Bucket:
    """一个归因维度上的一格。

    `amount` 只累加**算得出**的行; 算不出的那些进 `no_usage` / `unpriced` 计数。
    因此 `amount` 在 `is_lower_bound` 为真时是**下限**, 不是总额。
    """

    key: str
    amount: float = 0.0
    turns: int = 0
    compactions: int = 0
    no_usage: int = 0
    unpriced: int = 0
    #: 真的算出了金额的行数。**`amount == 0.0` 有两种截然不同的成因**: 一分钱没花, 或者
    #: 一行都没算出来。只看 `amount` 分不出这两者 —— 那正是「三个来源全未读到, 总花费
    #: 0.0000, 判成正常」的成因。调用方要判「有没有量到」必须看这个计数, 不是看金额。
    priced_rows: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0

    @property
    def is_lower_bound(self) -> bool:
        return bool(self.no_usage or self.unpriced)

    def add(self, rc: RowCost) -> None:
        if rc.event == EVENT_COMPACTION:
            self.compactions += 1
        elif rc.event == EVENT_TURN:
            self.turns += 1
        if rc.gap == "no_usage":
            self.no_usage += 1
        elif rc.gap == "unpriced":
            self.unpriced += 1
        if rc.amount is not None:
            self.amount += rc.amount
            self.priced_rows += 1
        self.prompt_tokens += rc.prompt_tokens or 0
        self.cached_tokens += rc.cached_tokens or 0
        self.completion_tokens += rc.completion_tokens or 0


#: 用途维度的两个值 —— 正常回合与压缩, 分开归因。
USE_TURN = "正常回合"
USE_COMPACTION = "压缩"


@dataclass
class CostTotals:
    """一次汇总的全部结果。**这是日报卡要调的那个返回类型。**

    下一张卡直接用本类的字段与 `render_cost_section()`, **不重新实现算钱**。

    四个归因维度(`by_container` / `by_session` / `by_model` / `by_use`)是并列的切法,
    各自的 `amount` 加起来都应等于 `total.amount`; 不等就说明有行没被归进任何一格。

    `is_lower_bound` 为真时**所有金额都是下限**。调用方必须照 `lower_bound_note()`
    的措辞打印, 不要把它渲染成等号。
    """

    pricing_version: str
    currency: str
    total: Bucket = field(default_factory=lambda: Bucket("总计"))
    by_container: dict[str, Bucket] = field(default_factory=dict)
    by_session: dict[str, Bucket] = field(default_factory=dict)
    by_model: dict[str, Bucket] = field(default_factory=dict)
    by_use: dict[str, Bucket] = field(default_factory=dict)
    #: 每个来源的读取状态。UNKNOWN 的那些必须进报告 —— 见 `unknown_sources()`。
    sources: list[SourceRead] = field(default_factory=list)
    #: 解析不出来的行总数。
    malformed_rows: int = 0
    #: 单价表里没有的模型名, 排序后。空表示全部定价命中。
    unpriced_models: list[str] = field(default_factory=list)

    @property
    def is_lower_bound(self) -> bool:
        """总额是不是下限。

        三种情况都算: 有回合没 usage、有模型没定价、有来源没读到。第三种也算是因为
        那个容器的花费整个不在总额里 —— 那比缺几个回合更严重。
        """
        return bool(self.total.is_lower_bound or self.unknown_sources())

    def unknown_sources(self) -> list[SourceRead]:
        """没读到的来源。**不是零花费** —— 渲染成「未测到」。"""
        return [s for s in self.sources if s.status == UNKNOWN]

    def lower_bound_note(self) -> str:
        """「总花费是下限」那句话, 带上到底缺了什么。

        措辞刻意把三个缺口分开数: 「N 个回合无 usage」是上游的问题, 「M 个模型未定价」
        是单价表该补, 「K 个来源未测到」是路径或部署的问题 —— 合成一句「数据不全」
        会让读者不知道该去修哪个。
        """
        if not self.is_lower_bound:
            return ""
        bits: list[str] = []
        if self.total.no_usage:
            bits.append(f"{self.total.no_usage} 个回合无 usage")
        if self.total.unpriced:
            models = ", ".join(self.unpriced_models) or "未知模型"
            bits.append(f"{self.total.unpriced} 个回合模型未定价({models})")
        unknown = self.unknown_sources()
        if unknown:
            bits.append(f"{len(unknown)} 个来源未测到({', '.join(s.name for s in unknown)})")
        return "总花费是下限: " + "; ".join(bits)


def _bucket(store: dict[str, Bucket], key: str) -> Bucket:
    if key not in store:
        store[key] = Bucket(key)
    return store[key]


def summarize(
    reads: Sequence[SourceRead],
    pricing: Pricing,
) -> CostTotals:
    """把若干来源的行算成一份汇总。

    归因四个维度: **按容器**(生产一人一容器, 所以容器≈人)、**按会话**、**按模型**、
    **按用途**(正常回合 / 压缩)。

    压缩单独成一维而不是并进触发它的回合: 实测压缩 41.5s x 22 次, 并进去则单回合成本
    看着便宜而月账单对不上。内核已经把它写成独立的 `event="compaction"` 行, 本层照这个
    结构归因即可。
    """
    totals = CostTotals(pricing_version=pricing.version, currency=pricing.currency, sources=list(reads))
    unpriced: set[str] = set()

    for read in reads:
        totals.malformed_rows += read.malformed
        # UNKNOWN 的来源没有行可算, 但它已经进了 `sources`, 会经 `unknown_sources()`
        # 出现在报告里。**不在这里补一格 0**。
        for row in read.rows:
            event = str(row.get(F_EVENT, "") or "")
            if event not in (EVENT_TURN, EVENT_COMPACTION):
                continue
            rc = cost_of_row(row, pricing, container=read.name)
            if rc.gap == "unpriced":
                unpriced.add(rc.model or "(未记录 model)")
            totals.total.add(rc)
            _bucket(totals.by_container, read.name).add(rc)
            _bucket(totals.by_session, rc.session_id or "(无 session_id)").add(rc)
            _bucket(totals.by_model, rc.model or "(未记录 model)").add(rc)
            _bucket(totals.by_use, USE_COMPACTION if event == EVENT_COMPACTION else USE_TURN).add(rc)

    totals.unpriced_models = sorted(unpriced)
    return totals


def collect(
    *,
    sources: Iterable[tuple[str, str]] | None = None,
    days: Sequence[_dt.date] | None = None,
    pricing: Pricing | None = None,
) -> CostTotals:
    """读 + 算, 一步到位。**日报卡的主入口。**

    `pricing` 省略时按 `load_pricing()` 的规则选版。`PricingError` **照旧往外抛**:
    单价表读不出来时报一份全是下限的报告, 会把配置问题伪装成上游问题。
    """
    reads = read_sources(sources, days=days)
    return summarize(reads, pricing or load_pricing())


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _money(amount: float, currency: str) -> str:
    return f"{amount:.4f} {currency}"


def _rows(store: dict[str, Bucket], currency: str, *, limit: int = 0) -> list[str]:
    items = sorted(store.values(), key=lambda b: (-b.amount, b.key))
    if limit:
        items = items[:limit]
    out: list[str] = []
    for b in items:
        line = f"    {b.key}: {_money(b.amount, currency)}"
        detail: list[str] = []
        if b.turns:
            detail.append(f"{b.turns} 回合")
        if b.compactions:
            detail.append(f"{b.compactions} 次压缩")
        if b.no_usage:
            detail.append(f"{b.no_usage} 无 usage")
        if b.unpriced:
            detail.append(f"{b.unpriced} 未定价")
        if detail:
            line += f"  ({', '.join(detail)})"
        if b.is_lower_bound:
            line += " ←下限"
        out.append(line)
    return out


def render_cost_section(totals: CostTotals, *, session_limit: int = 10) -> str:
    """成本一节的纯文本。**日报卡直接用这个, 不自己拼。**

    三条硬要求都在这里兑现:

    1. 抬头无条件带**单价表版本号**与 `NOT_RECONCILED`。少了版本号, 两天之间的金额跳变
       无法区分调价与用量变化; 少了「未与账单对过」, 一个抄错的单价会以「实测成本」的
       名义传下去。本期不做对账已由负责人定, 所以这行字是唯一的诚实性保障;
    2. 「未测到」独立成节, 且总额带下限说明 —— **不静默算 0 元**;
    3. 缓存只在**钱**这一节出现, 并附一句它不影响首字延迟。延迟归 D 卡那侧的字节/带宽
       一节, 两件事分开写, 谁也不解释对方。
    """
    cur = totals.currency
    lines: list[str] = []
    bound = " (下限)" if totals.is_lower_bound else ""
    lines.append(f"■ 成本 · 单价表 {totals.pricing_version} · {NOT_RECONCILED}")
    lines.append(f"  合计{bound}: {_money(totals.total.amount, cur)}")
    lines.append(f"    回合 {totals.total.turns} 个, 压缩 {totals.total.compactions} 次")

    note = totals.lower_bound_note()
    if note:
        # 这一行是判据「无 usage 的回合不得让当日总花费被静默低估」的输出端。
        lines.append(f"  ⚠ {note}")

    unknown = totals.unknown_sources()
    if unknown:
        lines.append("")
        lines.append(f"  未测到 {len(unknown)} 个来源(观测缺口, **不等于零花费**):")
        for s in unknown:
            lines.append(f"    {s.name}: 未测到 —— {s.reason}")

    if totals.malformed_rows:
        lines.append(f"  ⚠ {totals.malformed_rows} 行解析失败, 这些行的花费未计入")

    if totals.by_container:
        lines.append("")
        lines.append("  按容器(一人一容器):")
        lines += _rows(totals.by_container, cur)

    if totals.by_use:
        lines.append("")
        lines.append("  按用途(压缩单独计, 不并进触发它的回合):")
        lines += _rows(totals.by_use, cur)

    if totals.by_model:
        lines.append("")
        lines.append("  按模型:")
        lines += _rows(totals.by_model, cur)

    if totals.by_session:
        lines.append("")
        shown = min(session_limit, len(totals.by_session))
        lines.append(f"  按会话(前 {shown} / 共 {len(totals.by_session)}):")
        lines += _rows(totals.by_session, cur, limit=session_limit)

    cached = totals.total.cached_tokens
    prompt = totals.total.prompt_tokens
    if prompt:
        lines.append("")
        lines.append(f"  缓存命中 {cached}/{prompt} prompt token ({cached * 100.0 / prompt:.1f}%)")
        lines.append(f"    {CACHE_SEMANTICS}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 埋点缺口 —— 只记录, 不在本层偷偷补
# ---------------------------------------------------------------------------

#: 算钱原料的缺口。**本层不改埋点** —— 见模块 docstring 的分层立场。
#:
#: 逐条核过 B 卡实际落的字段后, 算钱必需的原料**齐全**: `model` / `prompt_tokens` /
#: `completion_tokens` / `cached_tokens` / `reasoning_tokens` / `usage_reported` 六项都有,
#: 且 `model` 取自**流**而不是请求体(请求体里是路由前的名字, 定价必须跟实际服务的那个)。
#:
#: 下面两条是**已知但不阻塞**的口径限制, 记在这里供主会话判断要不要回改上游卡:
MISSING_UPSTREAM_INPUTS = (
    "reasoning_tokens 在本部署实测常为 None(上游是否回填未经真实验证)。"
    "当前按 reasoning_in_completion=true 处理, 即不单独计费; 若将来换的上游把 reasoning "
    "排除在 completion 之外, 改单价表里那个布尔即可, 不必动埋点。",
    "无写缓存(cache write)口径。部分上游对「写入缓存」单独计价且高于普通 prompt, "
    "而 usage 只给了 cached_tokens(命中), 没有命中/写入的区分。真要精确到这一层需要上游先报, "
    "不是埋点能补的 —— 因此本层不把它列为埋点缺口。",
)
