"""跨组件的可观测性埋点机制:一个 ``record()`` 加一个可替换 sink。

**只提供能力,不决定内容。** 字段全部由调用方传入,本模块不认识任何业务字段名,
也不含任何产品概念。**不含金额、不算钱** —— 钱由报告层用带版本号的单价表算,
理由与落点见 ``docs/plans/2026-09-18-可观测性埋点-metrics-log-成本.md``。

``event`` 不做白名单校验(内核不决定内容)。当前调用方只用两个值:``"turn"``
(一行一回合)与 ``"compaction"``(压缩独立一行,不并进触发它的那个回合)。

落盘 ``{appdata}/metrics/{YYYY-MM-DD}.jsonl``,**按天分文件即轮转**,外加保留期。
刻意**不**复用 loguru 的轮转:日志轮转会吃掉历史,而成本汇总需要跨天可读。

为什么是扁平模块而不是包:``ai`` / ``session`` / ``gateway`` 这些包是**组件**,
有自己的进程或生命周期;跨组件共享的**机制**一律是顶层扁平模块(``_appdata.py`` /
``_logging.py`` / ``_sockets.py`` / ``_workspace_paths.py``)。metrics 是第四个这一类。
**不带下划线前缀**:调用方将来包括容器内 ``workspace/tools/`` 那一层(跨层 import),
而下划线前缀在跨层契约上的意思是「不该被外面 import」。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections.abc import Awaitable, Callable

import anyio
from loguru import logger

from psi_agent._appdata import resolve_appdata_root

_METRICS_ENV = "PSI_METRICS"

# 保留 90 天。理由:成本汇总的最粗粒度是「按月同比」,跨两个自然月才有得比,
# 因此下限是 62 天;取 90 天留出一个季度。上限由体量定:一行回合记录约 300 字节,
# 单机每天的回合数是三位数,90 天不到 10 MB —— 而生产那台机器内存只剩 2.6G、
# 磁盘也不宽裕,再长的保留期换不到新的结论。
RETENTION_DAYS = 90

# sink 签名:拿一个已成形的 payload,自己负责落地。换 sink 的口子只有这一个。
MetricsSink = Callable[[dict[str, object]], Awaitable[None]]

# 当天已清理过的标记。清理是「每天第一次写时顺手做一次」,不起后台任务:
# 本模块没有生命周期可挂(不是组件),而按天分文件让清理天然是幂等的。
_purged_day: str | None = None


def _enabled() -> bool:
    """默认开,``PSI_METRICS=0`` 关。"""
    return (os.environ.get(_METRICS_ENV, "") or "").strip() != "0"


async def _purge_expired(metrics_dir: anyio.Path, today: _dt.date) -> None:
    """删掉超过 ``RETENTION_DAYS`` 的天文件。

    只认 ``YYYY-MM-DD.jsonl``;名字解析不出日期的文件一律跳过(不是本模块写的,
    不归本模块清)。
    """
    cutoff = today - _dt.timedelta(days=RETENTION_DAYS)
    async for entry in metrics_dir.glob("*.jsonl"):
        try:
            day = _dt.date.fromisoformat(entry.stem)
        except ValueError:
            continue
        if day < cutoff:
            await entry.unlink(missing_ok=True)


def _day_of(payload: dict[str, object]) -> _dt.date:
    """从 payload 的 ``ts`` 取天名;取不出来就退回今天。

    退路存在的意义是自定义 sink 或将来改字段时不至于整条埋点崩掉 —— 埋点的失败
    模式必须是「记得糙一点」,不是「抛异常」。
    """
    try:
        return _dt.datetime.fromisoformat(str(payload["ts"])).date()
    except KeyError, ValueError:
        return _dt.date.today()


async def _jsonl_sink(payload: dict[str, object]) -> None:
    """按天 append 一行 JSON。

    **每次开关一次文件,不持句柄。** psi-agent 每会话把整个 workspace 重编一份
    (实测每会话 114 文件、104 万字节,模块名带 session_id 挡住了 ``file_hash``
    复用),模块级单例若持有句柄,可能变成每会话一个句柄同时追加同一文件。
    不持句柄就不需要先去确认这个行为,直接规避。

    以二进制 append 写,换行是显式的 ``b"\\n"``:文本模式在 Windows 上会把 ``\\n``
    翻成 ``\\r\\n``,而报告层在宿主(Linux)上按行读,``\\r`` 会跟进最后一个字段的值里。
    """
    global _purged_day
    root = await resolve_appdata_root()
    metrics_dir = anyio.Path(root) / "metrics"
    await metrics_dir.mkdir(parents=True, exist_ok=True)

    # 天名取自这一行自己的 ``ts``,不另外调一次 ``today()`` —— 两次取时刻会在
    # 跨午夜的那一行上落进与 ``ts`` 不一致的天文件。
    day = _day_of(payload)
    if _purged_day != day.isoformat():
        _purged_day = day.isoformat()
        await _purge_expired(metrics_dir, day)

    line = json.dumps(payload, ensure_ascii=False)
    async with await (metrics_dir / f"{day.isoformat()}.jsonl").open("ab") as f:
        await f.write(line.encode("utf-8") + b"\n")


_sink: MetricsSink = _jsonl_sink


def set_sink(sink: MetricsSink) -> None:
    """换掉落地实现。留这个口子给第二种 sink(sqlite / 网络上报)与测试用。"""
    global _sink
    _sink = sink


async def record(event: str, **fields: object) -> None:
    """记一个点位。``event`` 与字段的含义全由调用方决定。

    **async 而非 sync**:落点路径要走 ``_appdata.resolve_appdata_root()``,那是个
    async 函数(``anyio.Path``),而本模块不该自己拼 platformdirs 绕开它 —— 绕开
    就等于第二处 appname 字面量。调用方(``session/ai_client.py`` 与
    ``session/agent.py``)本来就在 async 路径上,``await`` 一下没有额外代价;
    反过来若做成 sync,要么在事件循环里跑阻塞 IO,要么得起个后台任务队列,
    后者是本模块撑不起的生命周期(见模块 docstring:机制不是组件)。

    埋点失败**绝不能让业务回合失败**:异常吞掉并降级成一行 WARNING。只吞
    ``Exception`` —— ``CancelledError`` / ``BaseException`` **照旧往外传**。
    ``suppress(CancelledError)`` 这个形状曾导致 anyio 活锁 100% 忙转且零日志,
    取消信号被吞掉后调用方的 cancel scope 永远等不到交付。
    """
    if not _enabled():
        # 关掉时必须是廉价空操作:在这里返回,payload 一个字节都不序列化。
        return
    now = _dt.datetime.now().astimezone()
    payload: dict[str, object] = {
        "ts": now.isoformat(),
        "event": event,
        # ``ts`` 带时区偏移:容器 TZ 配错时偏移看得见,不会变成一批安静挪了
        # 8 小时的记录(生产容器的时区只由 compose 的 ``TZ`` 撑着)。天名由 sink
        # 从这个 ``ts`` 取,不另外调一次 ``today()``。
        **fields,
    }
    try:
        await _sink(payload)
    except Exception as exc:
        logger.warning(f"metrics record dropped: event={event!r} err={exc!r}")
