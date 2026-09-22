"""`event="compaction"` 独立一行的判据(W#7)。

为什么必须独立:实测压缩 41.5s x 22 次。并进触发它的那个回合,单回合成本看着便宜
而月账单对不上 —— 压缩是**尾部工作**,由 `turn_lock` 在锁释放后跑,它的 40s 既不属
于刚结束的那个回合,也不该悄悄记在下一个回合头上。

时序:`_request_compaction()` 只是**记录**,回复此时已流完并提交;真正的 LLM 调用在
`drain_pending_compaction()` 里。所以 compaction 行的时间戳晚于触发它的 turn 行,
这是对的,不是要修的偏差。
"""

from __future__ import annotations

import json
import socket as _s
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import web

import psi_agent.metrics as metrics
from psi_agent.session.agent import SessionAgent
from psi_agent.session.ai_client import AiClient
from psi_agent.session.conversation import Conversation
from psi_agent.session.system_prompt import SystemPrompt

_STOP_SSE = (
    b'data: {"id":"test","model":"mock-model-1","choices":[{"index":0,'
    b'"delta":{"content":"Hi"},"finish_reason":"stop"}]}\n\n'
)
_COMPACTION_SSE = (
    b'data: {"id":"compaction","model":"mock-model-1","choices":[{"index":0,"delta":{},'
    b'"finish_reason":"compaction_needed"}],'
    b'"psi_compaction":{"needed":true,"prompt_tokens":50000,"threshold":10000}}\n\n'
)
# 压缩自己那次 LLM 调用的回包。它带自己的 usage —— 这几个 token 是压缩的成本,
# 必须落在 compaction 行上,不得出现在 turn 行里。
_SUMMARY_SSE = (
    b'data: {"id":"sum","model":"summarizer-9","choices":[{"index":0,'
    b'"delta":{"content":"summary text"},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":31337,"completion_tokens":222,"total_tokens":31559}}\n\n'
)


@pytest.fixture(autouse=True)
def _capture_metrics(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, object]] = []

    async def _sink(payload: dict[str, object]) -> None:
        rows.append(payload)

    default = metrics._jsonl_sink
    metrics.set_sink(_sink)
    monkeypatch.delenv("PSI_METRICS", raising=False)
    try:
        yield rows
    finally:
        metrics.set_sink(default)


async def _serve(scripts: list[list[bytes]]) -> tuple[str, web.AppRunner, list[dict]]:
    seen: list[dict] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(await request.json())
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        idx = min(len(seen) - 1, len(scripts) - 1)
        for line in scripts[idx]:
            await resp.write(line)
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    return f"http://127.0.0.1:{port}", runner, seen


def _rows(rows: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [r for r in rows if r.get("event") == event]


@pytest.mark.anyio
async def test_compaction_turn_writes_two_rows_and_keeps_costs_apart(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """压缩触发的回合 → turn 行 + compaction 行**两行**,且成本不串。

    这条同时钉三件事:两行都在、compaction 的 token 不出现在 turn 行里、turn 行标记
    了「触发过压缩」。并成一行的实现会在第一个断言上红;把压缩 token 记进 turn 行的
    实现会在 `31337` 那条上红。
    """
    summary_calls: list[list[dict[str, Any]]] = []

    async def compact_history_mock(history, complete_fn):
        summary_calls.append(list(history))
        # 真的走一次 LLM 调用,压缩自己的 usage 才有来源。
        return await complete_fn([{"role": "user", "content": "summarize"}])

    sp = SystemPrompt(builder=lambda: "You are helpful.", compaction_fn=compact_history_mock)
    socket, runner, _seen = await _serve([[_STOP_SSE, _COMPACTION_SSE], [_SUMMARY_SSE]])
    try:
        conv = Conversation(
            path=tmp_path / "session.jsonl",
            messages=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "older"}],
        )
        agent = SessionAgent(ai_client=AiClient(socket), conversation=conv, system_prompt=sp)
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
        # 生产走 `turn_lock` 的 finally;这里直接驱动 run(),显式 drain。
        await agent.drain_pending_compaction()
        await anyio.sleep(0.02)
    finally:
        await runner.cleanup()

    assert summary_calls, "压缩没被真正执行, 这条判据测不到东西"
    turns = _rows(_capture_metrics, "turn")
    compactions = _rows(_capture_metrics, "compaction")
    assert len(turns) == 1, f"turn 应恰好一行, 实得 {len(turns)}"
    assert len(compactions) == 1, f"compaction 应独立一行, 实得 {len(compactions)}: {_capture_metrics}"

    turn, comp = turns[0], compactions[0]
    assert turn["compaction_triggered"] is True, "turn 行要标记这个回合触发了压缩"
    # 压缩的 token 归 compaction 行。
    assert comp["prompt_tokens"] == 31337
    assert comp["completion_tokens"] == 222
    assert comp["usage_reported"] is True
    # 且**不**串到 turn 行里 —— 这是「单回合成本看着便宜」的那个失败模式。
    assert turn["prompt_tokens"] != 31337
    # 压缩自身耗时单独记,不加进回合耗时。
    assert isinstance(comp["duration_s"], float)
    assert comp["outcome"] == "normal"
    assert comp["session_id"] == turn["session_id"]


@pytest.mark.anyio
async def test_compaction_row_timestamp_is_after_the_turn_row(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """compaction 行的时间戳**晚于** turn 行,这是对的。

    压缩是尾部工作,`_request_compaction()` 只记录不执行。把两行的时间戳「修」成同
    一时刻等于把 40s 的尾部成本重新记回回合里。
    """

    async def compact_history_mock(history, complete_fn):
        return await complete_fn([{"role": "user", "content": "summarize"}])

    sp = SystemPrompt(builder=lambda: "You are helpful.", compaction_fn=compact_history_mock)
    socket, runner, _seen = await _serve([[_STOP_SSE, _COMPACTION_SSE], [_SUMMARY_SSE]])
    try:
        conv = Conversation(
            path=tmp_path / "session.jsonl",
            messages=[{"role": "user", "content": "old"}],
        )
        agent = SessionAgent(ai_client=AiClient(socket), conversation=conv, system_prompt=sp)
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
        await agent.drain_pending_compaction()
        await anyio.sleep(0.02)
    finally:
        await runner.cleanup()

    turn = _rows(_capture_metrics, "turn")[0]
    comp = _rows(_capture_metrics, "compaction")[0]
    assert str(comp["ts"]) >= str(turn["ts"])
    # 两行的顺序也要是先 turn 后 compaction(报告层按行序读)。
    assert _capture_metrics.index(comp) > _capture_metrics.index(turn)


@pytest.mark.anyio
async def test_skipped_compaction_writes_no_compaction_row(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """压缩没真跑(没有 `compact_history`)→ 不许凭空落一行 compaction。

    反方向的判据:没有它,「每次 drain 都无条件写一行」也能让上面两条全绿,而报告层
    会看到一堆零成本的压缩记录,把「压缩根本没在跑」读成「压缩很便宜」。
    """
    sp = SystemPrompt(builder=lambda: "You are helpful.", compaction_fn=None)
    socket, runner, _seen = await _serve([[_STOP_SSE, _COMPACTION_SSE]])
    try:
        conv = Conversation(path=tmp_path / "session.jsonl")
        agent = SessionAgent(ai_client=AiClient(socket), conversation=conv, system_prompt=sp)
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
        await agent.drain_pending_compaction()
        await anyio.sleep(0.02)
    finally:
        await runner.cleanup()

    assert _rows(_capture_metrics, "compaction") == [], "压缩没跑却落了一行"
    assert len(_rows(_capture_metrics, "turn")) == 1


@pytest.mark.anyio
async def test_failed_compaction_is_recorded_as_error(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """压缩自己失败:也要留一行,`outcome` 是 `error`。

    压缩失败当前只落一行 `logger.error` 就算了,历史上没有任何计数。失败的压缩仍然
    花了上游的 token,安静地不记等于把这笔钱从账里抹掉。
    """

    async def compact_history_mock(history, complete_fn):
        raise RuntimeError("summarizer exploded")

    sp = SystemPrompt(builder=lambda: "You are helpful.", compaction_fn=compact_history_mock)
    socket, runner, _seen = await _serve([[_STOP_SSE, _COMPACTION_SSE]])
    try:
        conv = Conversation(
            path=tmp_path / "session.jsonl",
            messages=[{"role": "user", "content": "old"}],
        )
        agent = SessionAgent(ai_client=AiClient(socket), conversation=conv, system_prompt=sp)
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
        await agent.drain_pending_compaction()
        await anyio.sleep(0.02)
    finally:
        await runner.cleanup()

    comps = _rows(_capture_metrics, "compaction")
    assert len(comps) == 1, f"失败的压缩也要留一行, 实得 {len(comps)}"
    assert comps[0]["outcome"] == "error"
    # 失败时上游没回 usage,同样不许记 0。
    assert comps[0]["usage_reported"] is False
    assert comps[0]["prompt_tokens"] is None


@pytest.mark.anyio
async def test_compaction_metrics_are_json_serialisable(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """两种行都得能过 `json.dumps` —— 落盘 sink 序列化失败只会降级成一行 WARNING,
    不会让用例红,所以这里显式过一遍。
    """

    async def compact_history_mock(history, complete_fn):
        return await complete_fn([{"role": "user", "content": "summarize"}])

    sp = SystemPrompt(builder=lambda: "You are helpful.", compaction_fn=compact_history_mock)
    socket, runner, _seen = await _serve([[_STOP_SSE, _COMPACTION_SSE], [_SUMMARY_SSE]])
    try:
        conv = Conversation(
            path=tmp_path / "session.jsonl",
            messages=[{"role": "user", "content": "old"}],
        )
        agent = SessionAgent(ai_client=AiClient(socket), conversation=conv, system_prompt=sp)
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
        await agent.drain_pending_compaction()
        await anyio.sleep(0.02)
    finally:
        await runner.cleanup()

    assert _capture_metrics
    for row in _capture_metrics:
        json.dumps(row, ensure_ascii=False)
