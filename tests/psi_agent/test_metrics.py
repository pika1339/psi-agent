"""内核埋点机制 `psi_agent.metrics`,判据全部落在 metrics 这一层。

刻意**不**出现任何业务字段名(`ttft` / `open_id` / 模型 id 之类):内核不认识内容,
若哪天有人往 `metrics.py` 里塞产品概念,这里的字段名一律是编造的(`喵数` /
`bogus_field`),不会跟着一起"恰好对上"。

三条历史教训直接决定了下面几条判据的形状:

1. `caplog` 收不到 loguru 的日志 —— 阴性用例会假绿。所以自己挂 `logger.add` sink,
   并**同时记 level**,否则"落了一行 INFO"也能冒充 WARNING 蒙混过关。
2. `suppress(CancelledError)` 这个形状曾导致 anyio 活锁 100% 忙转、零日志。所以
   "只吞 `Exception`、`CancelledError` 照旧往外传"必须有人盯 —— 没有这条,把
   `except Exception` 改成 `except BaseException` 的变异是全绿的。
3. 兜底/清理类判据**用仓库里不存在的资源名**构造。保留期那条用相对今天算出来的
   合成日期名,外加一个根本不是日期的文件名,验证清理不误伤、也不被怪名字噎死。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

import psi_agent.metrics as metrics
from psi_agent.metrics import RETENTION_DAYS, record, set_sink


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """每条用例一个干净的 appdata 根 + 干净的模块级状态。

    `set_sink` 与清理日标记都是模块级的,不复位会跨用例串味。
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.delenv("PSI_METRICS", raising=False)
    default_sink = metrics._jsonl_sink
    metrics._purged_day = None
    yield
    set_sink(default_sink)
    metrics._purged_day = None


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    """挂一个自带 sink 的 logger,**level 与 message 一起记**。

    只记 message 的话,"落了一行 DEBUG"和"落了一行 WARNING"无法区分。
    """
    seen: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    return seen, handler_id


def _today_file(root: Path) -> Path:
    return root / "metrics" / f"{date.today().isoformat()}.jsonl"


async def test_record_writes_exactly_one_parseable_line_with_fields_verbatim(tmp_path: Path) -> None:
    """一次 record → 恰好一行,字段原样在。"""
    await record("turn", 喵数=3, bogus_field="abc", 缺失=None)

    path = _today_file(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "turn"
    assert row["喵数"] == 3
    assert row["bogus_field"] == "abc"
    # None 必须原样落成 null —— "未测到"与"零"不混,内核不得替调用方填 0。
    assert row["缺失"] is None
    assert isinstance(row["ts"], str) and row["ts"]


async def test_two_records_append_instead_of_overwrite(tmp_path: Path) -> None:
    await record("turn", seq=1)
    await record("compaction", seq=2)

    lines = _today_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
    assert [json.loads(line)["event"] for line in lines] == ["turn", "compaction"]


async def test_chinese_field_value_stays_readable(tmp_path: Path) -> None:
    """`ensure_ascii=False`:中文落盘后肉眼可读,不是 `\\uXXXX`。"""
    await record("turn", 结束原因="取消")

    raw = _today_file(tmp_path).read_text(encoding="utf-8")
    assert "取消" in raw
    assert "\\u" not in raw


async def test_line_terminator_is_written_explicitly(tmp_path: Path) -> None:
    """换行必须是显式 `\\n`,不得被平台改写成 `\\r\\n`。

    报告层在宿主(Linux)上按行读容器写的文件,`\\r` 会跟进最后一个字段的值里。
    """
    await record("turn", seq=1)
    await record("turn", seq=2)

    raw = _today_file(tmp_path).read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw


async def test_disabled_is_a_cheap_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PSI_METRICS=0`:不建目录、不写字节,且**不先序列化再丢掉**。

    那个不可序列化的字段就是"廉价"的判据:若实现先 `json.dumps` 再判开关,
    会抛 TypeError → 被吞 → 落一行 WARNING。所以 `seen` 必须是空的。
    """
    monkeypatch.setenv("PSI_METRICS", "0")
    seen, handler_id = _capture_logs()
    try:
        await record("turn", unserializable=object())
    finally:
        logger.remove(handler_id)

    assert not (tmp_path / "metrics").exists()
    assert seen == []


async def test_sink_failure_never_reaches_the_caller_and_logs_one_warning() -> None:
    """埋点写坏了不能拖垮业务回合:异常吞掉,降级成一行 WARNING。"""

    async def _broken_sink(payload: dict[str, Any]) -> None:
        raise OSError("no space left on device")

    set_sink(_broken_sink)
    seen, handler_id = _capture_logs()
    try:
        await record("turn", seq=1)  # 不得抛
    finally:
        logger.remove(handler_id)

    warnings = [(lvl, msg) for lvl, msg in seen if lvl == "WARNING"]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {seen}"
    assert "no space left on device" in warnings[0][1]


async def test_cancelled_error_is_not_swallowed() -> None:
    """`CancelledError` 必须照旧往外传。

    这条盯的是"只吞 `Exception`"。把 `except Exception` 改成 `except BaseException`
    时本条转红 —— 否则取消信号被埋点吞掉,调用方的 cancel scope 等不到交付,
    就是 anyio 活锁那个形状(100% 忙转、零日志)。
    """

    async def _cancelling_sink(payload: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    set_sink(_cancelling_sink)
    seen, handler_id = _capture_logs()
    try:
        with pytest.raises(asyncio.CancelledError):
            await record("turn", seq=1)
    finally:
        logger.remove(handler_id)

    # 取消不是"埋点失败",不该降级成 WARNING 混进告警面。
    assert [lvl for lvl, _ in seen if lvl == "WARNING"] == []


async def test_retention_purges_expired_days_and_leaves_the_rest(tmp_path: Path) -> None:
    """保留期到期的旧天文件被清、未到期的不动、非日期名的不误伤。

    全部用相对今天算出来的**合成**日期名,不用仓库里任何真实文件名。
    """
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    today = date.today()
    expired = metrics_dir / f"{(today - timedelta(days=RETENTION_DAYS + 5)).isoformat()}.jsonl"
    fresh = metrics_dir / f"{(today - timedelta(days=1)).isoformat()}.jsonl"
    not_a_date = metrics_dir / "喵喵喵.jsonl"
    for f in (expired, fresh, not_a_date):
        f.write_text('{"event":"turn"}\n', encoding="utf-8")

    await record("turn", seq=1)

    assert not expired.exists(), "到期的天文件必须被清掉"
    assert fresh.exists(), "未到期的天文件不得被动"
    assert not_a_date.exists(), "名字不是日期的文件不得被清理误伤"
    assert _today_file(tmp_path).exists()


async def test_sink_holds_no_file_handle_between_records(tmp_path: Path) -> None:
    """sink 每次 append 开关一次文件,**不把句柄挂在模块级单例上**。

    判据形状:写一行 → 把文件删掉 → 再写一行 → 必须重新出现且恰好一行。
    句柄被持有时两个平台各自转红 —— Windows 上 unlink 直接 PermissionError,
    POSIX 上第二行写进已 unlink 的旧 inode,新路径根本不出现。

    背景:psi-agent 每会话把整个 workspace 重编一份(实测每会话 114 文件、
    104 万字节,模块名带 session_id 挡住了 file_hash 复用),模块级单例持句柄
    可能变成每会话一个句柄同时追加同一文件。不持句柄就不需要先去确认这个行为。
    """
    await record("turn", seq=1)
    path = _today_file(tmp_path)
    path.unlink()

    await record("turn", seq=2)

    assert path.exists(), "句柄被持有时这里为假"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 2
