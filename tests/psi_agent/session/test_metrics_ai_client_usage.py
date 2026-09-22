"""`ai_client` 层的 usage 采集判据:全套 token + 「未测到」与「零」不混。

**这一层测的就是这一层。** 判据全部构造 SSE 流喂 `AiClient.stream()`,断言落在它
产出的 `AiDelta.usage` 上 —— 不调 Session 层函数然后声称测了 AI 层(这个错误在本仓
库出过:docstring 说测 AI 层却调 Session 层函数,变异复核也照不出来)。

核心那条是 W#6:**上游不返回 usage 时记 `null` 并标记「未测到」,绝不记 0**。现有的
`_as_int()` 缺失时返回 0,那个 0 的语义是给预算校准用的(偏低=欠装=安全);成本汇总
里同一个 0 会让当日花费被静默低估 —— 观测缺口伪装成健康。所以新取法必须能区分
「字段不在」与「字段真的是 0」,而判据必须**同时**钉住这两侧,否则把 `None` 改回 0
的变异只会让一条断言变红、另一条照旧。
"""

from __future__ import annotations

import json
import socket as _s
from collections.abc import Sequence

import pytest
from aiohttp import web

from psi_agent.session.ai_client import AiClient
from psi_agent.session.protocol import AiDelta


async def _deltas_for(chunks: Sequence[dict], request_body: dict | None = None) -> list[AiDelta]:
    """把一串 chunk 当 SSE 发出去,收集 `AiClient.stream()` 产出的 delta。

    走真 HTTP socket(与本目录既有 ai_client 判据同一形状),不 mock `stream()`:
    usage 的解析在 SSE 边界上,mock 掉就把被测代码一起 mock 了。
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in chunks:
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        client = AiClient(ai_socket=f"http://127.0.0.1:{port}")
        body = request_body if request_body is not None else {"messages": [], "stream": True}
        return [d async for d in client.stream(body)]
    finally:
        await runner.cleanup()


def _last_usage(deltas: Sequence[AiDelta]):
    """最后一个带 usage 的 delta 上的 usage。没有任何一个则 `None`。"""
    carried = [d.usage for d in deltas if d.usage is not None]
    return carried[-1] if carried else None


@pytest.mark.anyio
async def test_usage_chunk_yields_every_cost_input() -> None:
    """成本原料一次取齐:prompt / completion / cached / reasoning 四项都要到位。

    只取 `prompt_tokens`(改造前的状态)会让这条红:cached 与 reasoning 是 OpenAI
    放在 `*_tokens_details` 子字典里的,不显式下钻就永远是「未测到」,而这两项恰好
    是缓存命中率与思维开销的唯一来源。
    """
    deltas = await _deltas_for(
        [
            {"id": "a", "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
            {
                "id": "b",
                "choices": [],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "total_tokens": 1540,
                    "prompt_tokens_details": {"cached_tokens": 1024},
                    "completion_tokens_details": {"reasoning_tokens": 256},
                },
            },
        ]
    )

    usage = _last_usage(deltas)
    assert usage is not None, "带 usage 的 chunk 没有把 usage 交出来"
    assert usage.reported is True
    assert usage.prompt_tokens == 1200
    assert usage.completion_tokens == 340
    assert usage.cached_tokens == 1024
    assert usage.reasoning_tokens == 256


@pytest.mark.anyio
async def test_absent_usage_is_null_and_flagged_not_reported() -> None:
    """上游一个 usage 都不回:token 字段是 `None` 且带「未测到」标记,**不是 0**。

    这是 W#6 的正脸。0 在成本汇总里是一句「这个回合不花钱」的断言,而真相是
    「不知道花了多少」—— 两者混淆的后果是当日花费被静默低估,且越是上游异常的
    日子低估越多。
    """
    deltas = await _deltas_for([{"id": "a", "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}])

    assert deltas, "流里一个 delta 都没有"
    assert _last_usage(deltas) is None, "上游没报 usage 却凭空造出一份"
    # 没报就必须每一处都判得出来,不能只有「最后那个 delta 没带」这一种表现。
    assert all(d.usage is None for d in deltas)


@pytest.mark.anyio
async def test_zero_tokens_reported_is_distinct_from_absent() -> None:
    """上游**明确**回了 0:记 0 且 `reported` 为真 —— 与「未测到」是两种状态。

    反方向的判据。只有这条在,「缺失记 null」才不能靠「把所有数都记成 null」蒙过去。
    """
    deltas = await _deltas_for(
        [
            {
                "id": "z",
                "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        ]
    )

    usage = _last_usage(deltas)
    assert usage is not None
    assert usage.reported is True
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    # 子字典整个不在 ≠ 值是 0。上游没给 details 就是没测到 cached/reasoning。
    assert usage.cached_tokens is None
    assert usage.reasoning_tokens is None


@pytest.mark.anyio
async def test_json_true_is_not_counted_as_one_token() -> None:
    """`usage` 里是 JSON `true`:不得变成 1 token。

    `bool` 是 `int` 的子类,直接 `isinstance(x, int)` 会让 `true` 静默变成 1 —— 既有
    `_as_int()` 显式拒了这一条,新取法也要守住。记 `None`(未测到)而不是 0:一个
    布尔值出现在 token 位置意味着上游给的东西没法读,不是「用了零个 token」。
    """
    deltas = await _deltas_for(
        [
            {
                "id": "t",
                "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": True,
                    "completion_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": False},
                },
            }
        ]
    )

    usage = _last_usage(deltas)
    assert usage is not None
    assert usage.prompt_tokens != 1, "JSON true 被当成了 1 token"
    assert usage.prompt_tokens is None
    assert usage.cached_tokens != 0, "JSON false 被当成了 0 token"
    assert usage.cached_tokens is None
    # 同一份 usage 里能读的那一项照旧要读出来,坏字段不连坐。
    assert usage.completion_tokens == 7


@pytest.mark.anyio
async def test_model_id_comes_off_the_stream() -> None:
    """模型 id 取自流本身,不取自请求体。

    请求体里写的是路由前的名字,上游实际服务的模型可能不是它;算钱要按后者。
    """
    deltas = await _deltas_for(
        [
            {
                "id": "m",
                "model": "deepseek-v4-0711",
                "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
        ],
        request_body={"messages": [], "stream": True, "model": "whatever-was-asked-for"},
    )

    usage = _last_usage(deltas)
    assert usage is not None
    assert usage.model == "deepseek-v4-0711"


@pytest.mark.anyio
async def test_prompt_token_calibration_still_sees_the_number() -> None:
    """既有的预算校准通路不能被这次扩字段改坏。

    `usage_prompt_tokens` 是 `RequestAssembler.calibrate` 的输入,且 OpenAI 把最终
    usage chunk 的 `choices` 发成**空数组** —— 那个 chunk 必须照旧作为一个 delta 冒
    出来,否则校准拿不到任何数字(这是它当初被单独放行的原因)。
    """
    deltas = await _deltas_for(
        [
            {"id": "a", "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
            {"id": "b", "choices": [], "usage": {"prompt_tokens": 999, "completion_tokens": 3}},
        ]
    )

    assert any(d.usage_prompt_tokens == 999 for d in deltas), "空 choices 的 usage chunk 被吞了"
