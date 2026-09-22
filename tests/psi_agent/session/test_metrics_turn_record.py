"""`event="turn"` 一行的判据,全部落在 Session 层:跑一个真回合,读 metrics 收到什么。

判据的形状由「这个点位过去怎么骗过我们」决定,不是由「哪些字段好断言」决定:

- **N 与 M 两个值**(W#3):生产上 `tools_exposed=232 of 232` 读起来像收窄已启用,
  而真相是没有任何一层有 manifest、全量在暴露,请求体每回合扛 289774 字符的 schema
  数周。只记一个数看不出来 —— 所以这里有一条 **N ≠ M** 的用例,且它必须能打红「只
  记 N」的实现。
- **恰好一行**:tool_calls 分支 `break` 出 stream 循环,留在循环之后的代码「在最花
  上下文的回合上永远不会跑」(`agent.py` 那段注释写的就是这个)。所以既有「走
  tool_calls 的回合仍恰好一行」,也有「普通回合不写两行」。
- **首 token 不得是响应头时刻**:`AI response status: 200` 只有 50-60ms,量的是第一
  跳响应头。把它当 TTFB 曾产出一条已撤回的「上游 0.18s」结论。这里用一个**先回响应
  头、迟迟不回 token** 的 mock 上游钉死这条。
- **结束原因四值**:`AgentStopCause` 的四个枚举值并**不**等于 正常/超时/取消/异常
  —— 取消与超时是从 `run()` 里以异常形态穿出去的(`agent.py` 的 `except BaseException`
  分支),不是 stop cause。所以映射由本卡新增,判据要覆盖正常与取消两侧。

metrics 用 `set_sink` 收进内存,不落盘:判据要的是「这一行有没有、字段全不全」,
落盘那一层是 `tests/psi_agent/test_metrics.py` 的事(信息只归属一层)。
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
from psi_agent.session.protocol import AgentError
from psi_agent.session.tool_exposure import EXPOSURE_TIER_ENV, MANIFEST_NAME
from psi_agent.session.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _capture_metrics(monkeypatch: pytest.MonkeyPatch):
    """把 metrics 收进内存。返回 payload 列表。

    `set_sink` 是模块级状态,用完必须复位,否则跨用例串味。
    """
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


def _sse(**delta: Any) -> bytes:
    chunk = {
        "id": "mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model-1",
        "choices": [{"index": 0, "delta": delta, "finish_reason": delta.pop("_finish", None)}],
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _sse_raw(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


_STOP_CHUNK = {
    "id": "mock",
    "model": "mock-model-1",
    "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": "stop"}],
}
_USAGE_CHUNK = {
    "id": "mock-usage",
    "model": "mock-model-1",
    "choices": [],
    "usage": {
        "prompt_tokens": 800,
        "completion_tokens": 120,
        "total_tokens": 920,
        "prompt_tokens_details": {"cached_tokens": 512},
        "completion_tokens_details": {"reasoning_tokens": 64},
    },
}


class _MockAI:
    """真 HTTP socket 上的 mock 上游,按脚本回 SSE。"""

    def __init__(self, scripts: list[list[bytes]], *, header_delay: float = 0.0) -> None:
        self._scripts = scripts
        self._header_delay = header_delay
        self.requests: list[dict] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def handler(request: web.Request) -> web.StreamResponse:
            self.requests.append(await request.json())
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            # 响应头先走,token 后走 —— 两者之间的这段延迟是本文件那条「首 token 不
            # 得是响应头时刻」判据的全部依据。
            await resp.prepare(request)
            if self._header_delay:
                await anyio.sleep(self._header_delay)
            idx = min(len(self.requests) - 1, len(self._scripts) - 1)
            for line in self._scripts[idx]:
                await resp.write(line)
            await resp.write(b"data: [DONE]\n\n")
            return resp

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()
        return f"http://127.0.0.1:{port}"

    async def cleanup(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


def _turns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [r for r in rows if r.get("event") == "turn"]


def _write_tools(tools_dir: Path, *names: str) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (tools_dir / f"{name}.py").write_text(
            f'async def {name}() -> str:\n    """Tool {name}."""\n    return "{name}"\n',
            encoding="utf-8",
        )


async def _run_turn(
    tmp_path: Path,
    server: _MockAI,
    *,
    tool_registry: ToolRegistry | None = None,
) -> None:
    socket = await server.start()
    try:
        agent = SessionAgent(
            ai_client=AiClient(socket),
            conversation=Conversation(path=tmp_path / "session.jsonl"),
            tool_registry=tool_registry,
        )
        _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
    finally:
        await server.cleanup()


@pytest.mark.anyio
async def test_one_turn_writes_exactly_one_turn_row_with_every_field(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """一个普通回合 → 恰好一行 turn,且方案 W#1/W#2 的字段一个不缺。"""
    server = _MockAI([[_sse_raw(_STOP_CHUNK), _sse_raw(_USAGE_CHUNK)]])
    await _run_turn(tmp_path, server)

    rows = _turns(_capture_metrics)
    assert len(rows) == 1, f"一个回合必须恰好一行 turn, 实得 {len(rows)}: {_capture_metrics}"
    row = rows[0]

    # 身份与序号
    assert row["session_id"]
    assert row["turn_index"] == 1
    # 请求体字节数(注释里说明它比真实上线字节少几十字节)
    assert isinstance(row["req_bytes"], int) and row["req_bytes"] > 0
    # 耗时:首 token 与整回合各一个
    assert isinstance(row["ttft_s"], float)
    assert isinstance(row["duration_s"], float)
    assert row["duration_s"] >= row["ttft_s"]
    # 工具调用次数、压缩标记
    assert row["tool_calls"] == 0
    assert row["compaction_triggered"] is False
    # 结束原因
    assert row["outcome"] == "normal"
    assert row["stop_cause"] == "model_completed"
    # 成本原料 + 上游是否返回 usage 的标记
    assert row["usage_reported"] is True
    assert row["model"] == "mock-model-1"
    assert row["prompt_tokens"] == 800
    assert row["completion_tokens"] == 120
    assert row["cached_tokens"] == 512
    assert row["reasoning_tokens"] == 64


@pytest.mark.anyio
async def test_turn_row_carries_both_exposed_and_total_tool_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _capture_metrics: list[dict[str, object]]
) -> None:
    """**N ≠ M**:收窄生效时两个数必须都在,且**不相等**。

    这条是 W#3 的正脸,也是「只记 N」那个变异的死因:三个工具里 manifest 只声明一个
    (外加发现工具 `tool_search`),于是 N=2、M=3。只记 N 的实现在这里没有 M 可断言;
    把两个字段都填成 N 的实现会在 `!=` 上红。
    """
    monkeypatch.delenv(EXPOSURE_TIER_ENV, raising=False)
    tools_dir = tmp_path / "tools"
    _write_tools(tools_dir, "declared_tool", "hidden_tool", "tool_search")
    (tools_dir / MANIFEST_NAME).write_text("declared_tool\n", encoding="utf-8")
    registry = await ToolRegistry.load(tools_dir)

    server = _MockAI([[_sse_raw(_STOP_CHUNK)]])
    await _run_turn(tmp_path, server, tool_registry=registry)

    row = _turns(_capture_metrics)[0]
    assert row["tools_exposed"] == 2, "N 应是 manifest 声明的 1 个 + 发现工具 1 个"
    assert row["tools_total"] == 3, "M 应是注册表里的全部工具"
    assert row["tools_exposed"] != row["tools_total"], "两个值相等就回到了 `232 of 232` 那种读起来像已启用的状态"
    # 与请求体实际携带的 tools 数组对齐 —— N 是「这一回合真送出去多少个」,不是
    # 另算一遍的数字。
    assert len(server.requests[0]["tools"]) == row["tools_exposed"]


@pytest.mark.anyio
async def test_ttft_is_not_the_response_header_moment(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """上游先回响应头、隔一段才回首个 token → ttft 必须量到后者。

    `AI response status: 200` 那行只有 50-60ms(litellm 立刻回的 SSE 响应头),把它
    当首字曾产出一条已撤回的「上游 TTFB 0.18s」结论。这里响应头几乎立刻回,token
    延后 0.3s:记响应头时刻的实现量出来接近 0,这条判据于是红。
    """
    delay = 0.3
    server = _MockAI([[_sse_raw(_STOP_CHUNK)]], header_delay=delay)
    await _run_turn(tmp_path, server)

    row = _turns(_capture_metrics)[0]
    ttft = row["ttft_s"]
    assert isinstance(ttft, float)
    assert ttft >= delay, f"ttft={ttft} 小于上游延迟 {delay}s, 量的是响应头不是首个 token"


@pytest.mark.anyio
async def test_tool_calls_turn_still_writes_exactly_one_row(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """走 tool_calls 分支的回合:仍然**恰好一行** turn,且工具调用次数记上了。

    tool_calls 分支 `break` 出 stream 循环,所以「留到循环之后」的采集点在最花上下
    文的回合上永远不跑 —— 那种实现会让这条用例得到 0 行。而两个模型回合(先要工具、
    再收尾)只能产出一行:一行一回合,不是一行一次模型调用。
    """
    tools_dir = tmp_path / "tools"
    _write_tools(tools_dir, "declared_tool")
    registry = await ToolRegistry.load(tools_dir)

    tool_call_chunk = {
        "id": "mock-tc",
        "model": "mock-model-1",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "declared_tool", "arguments": "{}"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    server = _MockAI([[_sse_raw(tool_call_chunk)], [_sse_raw(_STOP_CHUNK), _sse_raw(_USAGE_CHUNK)]])
    await _run_turn(tmp_path, server, tool_registry=registry)

    rows = _turns(_capture_metrics)
    assert len(rows) == 1, f"tool_calls 回合必须恰好一行, 实得 {len(rows)}: {_capture_metrics}"
    assert rows[0]["tool_calls"] == 1
    assert rows[0]["model_rounds"] == 2, "两次模型调用应记在同一行里"
    assert rows[0]["outcome"] == "normal"


@pytest.mark.anyio
async def test_turn_without_upstream_usage_records_null_not_zero(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """上游不返回 usage → turn 行的 token 字段是 `null` 且带「未测到」标记。

    记 0 的后果不是少一个数,而是成本汇总把这个回合算成不花钱 —— 观测缺口伪装成
    健康,且上游越不稳当日越便宜。
    """
    server = _MockAI([[_sse_raw(_STOP_CHUNK)]])
    await _run_turn(tmp_path, server)

    row = _turns(_capture_metrics)[0]
    assert row["usage_reported"] is False, "没报 usage 却标成报了"
    for field in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens"):
        assert row[field] is None, f"{field} 记成了 {row[field]!r},未测到被当成了零"
    # 序列化成 jsonl 后仍然是 null —— 报告层读到的是这个,不是内存里的 None。
    assert '"prompt_tokens": null' in json.dumps(row, ensure_ascii=False, indent=None).replace(
        '"prompt_tokens":null', '"prompt_tokens": null'
    )


@pytest.mark.anyio
async def test_cancelled_turn_is_recorded_as_cancelled(
    tmp_path: Path, _capture_metrics: list[dict[str, object]]
) -> None:
    """取消的回合:仍然落一行,`outcome` 是 `cancelled` 而不是 `normal`。

    取消不走 stop cause —— 它以异常形态从 `run()` 里穿出去(`except BaseException`
    那个分支)。结束原因的四值因此不能直接等于 `AgentStopCause`,映射是本卡新增的。
    """
    # 上游回完响应头后长时间不回 token,给取消留出窗口。
    server = _MockAI([[_sse_raw(_STOP_CHUNK)]], header_delay=5.0)
    socket = await server.start()
    try:
        agent = SessionAgent(
            ai_client=AiClient(socket),
            conversation=Conversation(path=tmp_path / "session.jsonl"),
        )
        with anyio.move_on_after(0.4):
            async for _ in agent.run({"role": "user", "content": "hi"}):
                pass
    finally:
        await server.cleanup()

    rows = _turns(_capture_metrics)
    assert len(rows) == 1, f"取消的回合也必须留下一行, 实得 {len(rows)}"
    assert rows[0]["outcome"] == "cancelled"
    # 取消时上游没给 usage,这里同样不许记 0。
    assert rows[0]["usage_reported"] is False
    assert rows[0]["prompt_tokens"] is None


@pytest.mark.anyio
async def test_failed_turn_is_recorded_as_error(tmp_path: Path, _capture_metrics: list[dict[str, object]]) -> None:
    """异常结束的回合:`outcome` 是 `error`,且 `AgentError` **照旧往外抛**。

    埋点不得改变控制流 —— 这一条同时盯住「记了」和「没吞」。历史上有一次变异复核
    四条全绿,暴露的正是「异常照旧往外传」没人盯。

    上游非 200 时 `AiClient` 回一个 `finish_reason=error` 的 delta,Session 随即抛
    `AgentError`(见 `agent.py` 的 `FINISH_REASON_ERROR` 分支)—— 回合是**抛着**结束
    的,不是返回一串 chunk。所以这里既要看见异常穿出来,也要看见行已经落下。
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        raise web.HTTPInternalServerError(text="boom")

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        agent = SessionAgent(
            ai_client=AiClient(f"http://127.0.0.1:{port}"),
            conversation=Conversation(path=tmp_path / "session.jsonl"),
        )
        with pytest.raises(AgentError):
            _ = [c async for c in agent.run({"role": "user", "content": "hi"})]
    finally:
        await runner.cleanup()

    rows = _turns(_capture_metrics)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
