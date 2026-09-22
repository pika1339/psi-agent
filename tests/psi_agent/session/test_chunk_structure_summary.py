"""W#9 结构摘要的判据 —— 落在 AI 客户端这一层, 用真的 loguru sink 量级别。

## `caplog` 在这里收不到任何东西

pytest 的 `caplog` 挂的是标准库 `logging`, 而本项目用 loguru —— 两者没有默认桥接。拿
`caplog` 写的判据会**一条日志都收不到**, 于是所有阴性用例假绿。所以本文件自己
`logger.add(sink)`, 并且 **sink 同时记 level**: 「是 INFO 不是 DEBUG」这条要求, 只有把
级别一起记下来才验得到。仅断言「消息出现了」的判据在级别被降到 DEBUG 后照样绿。

## 判据落在它声称的那一层

`summarize_chunk_structure()` 是 `session/ai_client.py` 的模块级函数, 下面直接调它 ——
不绕道 `SessionAgent`。曾有 docstring 说测 AI 层却调 Session 层函数, 连变异复核都照不出来。
另外有一条判据真的跑 `AiClient.stream()`(喂一个假 SSE 响应), 覆盖「这行摘要真的被打出来」
这半 —— 纯函数绿不证明它接上了线。

## 为什么不用正则判 tool_calls

thinking 泄漏那次查不到根因, 一部分原因是判据用正则去匹配文本。摘要给的是**结构**:
几个 tool_call、各自在哪个 index、有没有 function name。下面的判据也照这个结构断言。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from loguru import logger

import psi_agent.session.ai_client as ai_client_mod
from psi_agent.session.ai_client import AiClient, summarize_chunk_structure

#: 长得像密钥的东西。判据要证明它**一个字节都不进摘要**。
FAKE_SECRET = "sk-nonexistent1234567890ABCDEFGHIJKLMNOPQRSTUV"


@pytest.fixture
def captured() -> Any:
    """自挂 loguru sink, **同时记 level 与消息**。

    `caplog` 收不到 loguru 的日志(见模块 docstring), 所以这个 fixture 是本文件所有日志
    判据的唯一入口。记的是 `record["level"].name` 而不是格式化后的字符串: 格式串里未必
    带级别, 靠文本判级别会在改格式时静默失效。
    """
    rows: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: rows.append((m.record["level"].name, m.record["message"])),
        level=0,  # 收全部级别 —— 否则「降成 DEBUG」这条变异会因为收不到而看起来像没打
        format="{message}",
    )
    yield rows
    logger.remove(sink_id)


def _chunk(**delta: object) -> dict:
    return {"choices": [{"index": 0, "delta": delta}]}


# --------------------------------------------------------------------------
# 结构本身
# --------------------------------------------------------------------------


def test_reports_reasoning_presence_without_the_text() -> None:
    """记「有没有 reasoning 字段」, 不记它的内容。"""
    s = summarize_chunk_structure(_chunk(reasoning="模型的内心独白, 不该进日志"))
    assert "reasoning=reasoning" in s
    assert "内心独白" not in s


def test_reasoning_absent_is_stated_explicitly() -> None:
    """没有 reasoning 要明说 `absent` —— thinking 被关掉时正是这个形态。

    不明说的话「没有这个字段」与「摘要没记这一项」不可区分。
    """
    s = summarize_chunk_structure(_chunk(content="hi"))
    assert "reasoning=absent" in s


def test_mirrored_reasoning_pair_is_not_flagged_as_anomaly() -> None:
    """**`reasoning_content` 与 `reasoning` 逐字节相同不是 bug。**

    AI 层为兼容把同一份内容挂在两个拼法下。把它记成异常曾让一次排查走错方向, 所以摘要
    里它是一句平铺直叙的 `mirrored`, 且**不含** `DIFFER` 这类告警词。
    """
    same = "一模一样的思考内容"
    s = summarize_chunk_structure(_chunk(reasoning=same, reasoning_content=same))
    assert "mirrored" in s
    assert "DIFFER" not in s


def test_differing_reasoning_pair_is_flagged() -> None:
    """反过来, 两个拼法**内容不同**才值得注意 —— 与上一条成对, 证明那个判断真的在比较
    内容, 而不是恒定输出 `mirrored`。"""
    s = summarize_chunk_structure(_chunk(reasoning="甲", reasoning_content="乙"))
    assert "reasoning_pair=DIFFER" in s
    assert "mirrored" not in s
    # 两边的内容都不许漏出来。
    assert "甲" not in s and "乙" not in s


def test_tool_calls_recorded_as_structure_not_matched_by_regex() -> None:
    """tool_calls 记**结构**: 几个、在哪个 index、有没有 function name。

    这条是「判据该用 tool_calls 结构而非正则」的落点。断言的是 index 与 name/nameless
    这些结构事实, 不是对文本做模式匹配。
    """
    s = summarize_chunk_structure(
        _chunk(
            tool_calls=[
                {"index": 0, "function": {"name": "read", "arguments": '{"path":"a"}'}},
                {"index": 1, "function": {"arguments": '{"more":1}'}},
            ]
        )
    )
    assert "tool_calls=2(" in s
    assert "[0]name" in s
    assert "[1]nameless" in s
    # 参数只记长度, 不记内容 —— 参数里会有用户数据。
    assert '{"path":"a"}' not in s


def test_absent_tool_calls_distinguished_from_empty_list() -> None:
    """「没有 tool_calls 字段」与「有但是空列表」要分开。

    模型自述要调工具却没真调时, 前者才是那个形态; 混起来看不出来。
    """
    assert "tool_calls=absent" in summarize_chunk_structure(_chunk(content="x"))
    assert "tool_calls=0(" in summarize_chunk_structure(_chunk(content="x", tool_calls=[]))


def test_malformed_tool_calls_reported_not_crashing() -> None:
    """tool_calls 不是列表时报 MALFORMED, 不抛异常。

    摘要是探针, 探针崩掉会把一次上游异常变成一次回合失败。
    """
    s = summarize_chunk_structure(_chunk(content="x", tool_calls={"not": "a list"}))
    assert "tool_calls=MALFORMED(dict)" in s


def test_content_fingerprint_carries_no_characters_of_the_content() -> None:
    """指纹是**派生值**, 不含 content 的任何字符。

    最初这里写的是字面前缀, 被本文件的密钥判据当场抓住漏了 API key。前缀这个形状救不
    回来 —— 截短到 32 字符仍是一个可识别的密钥。所以改成「摘要哈希 + 字符类骨架」。
    """
    text = "甲乙丙丁戊己庚辛"
    s = summarize_chunk_structure(_chunk(content=text))
    assert "content=8c" in s  # 长度照旧记真实长度
    for ch in text:
        assert ch not in s, f"指纹漏出了原文字符 {ch}"
    assert "h=" in s and "shape=" in s


def test_fingerprint_is_stable_and_distinguishes_different_openings() -> None:
    """同样的开头指纹相同, 不同的开头指纹不同 —— 「两个回合是不是同一个开场」这个问题
    要答得出来, 否则指纹没有用途。

    不带盐、不带进程种子: 每进程变一次的指纹没法跨两行日志比较, 而那是它唯一的用途。
    """
    a = summarize_chunk_structure(_chunk(content="我先想一下这个问题"))
    b = summarize_chunk_structure(_chunk(content="我先想一下这个问题"))
    c = summarize_chunk_structure(_chunk(content="好的, 结果如下"))
    assert a == b
    assert a != c


def test_fingerprint_shape_tells_prose_from_token_blob() -> None:
    """字符类骨架要能区分中文散文与 token 样的乱串 —— 这是「模型在自述」与「正常回答」
    的可判读差别。骨架只暴露形状, 不暴露值, 而形状不是秘密。"""
    prose = summarize_chunk_structure(_chunk(content="这是一段中文回答内容"))
    blob = summarize_chunk_structure(_chunk(content="aGVsbG8gd29ybGQ5OTk5MTIzNA"))
    assert "H" in prose.split("shape=")[1]
    assert "H" not in blob.split("shape=")[1]


def test_summary_is_always_a_single_line() -> None:
    """摘要恒为一行 —— 多行会把一个 chunk 拆成多条日志, 之后就对不回去了。"""
    for content in ("第一行\n第二行\n第三行", "带\r\n回车", "tab\t分隔"):
        assert "\n" not in summarize_chunk_structure(_chunk(content=content))
        assert "\r" not in summarize_chunk_structure(_chunk(content=content))


def test_empty_summary_for_substanceless_chunk() -> None:
    """没内容可描述的 chunk 返回空串。

    首个 chunk 常常只有 `role`, 而它对每条健康的流都长一样 —— 拿它占掉「每个响应一行」
    的额度, 等于把探针要记的形态挤掉。
    """
    assert summarize_chunk_structure({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}) == ""
    assert summarize_chunk_structure({"choices": []}) == ""
    assert summarize_chunk_structure({}) == ""
    # content 是空串也算没内容 —— 但 content 为 "0" 这种假值字符串要算有。
    assert summarize_chunk_structure(_chunk(content="")) == ""
    assert summarize_chunk_structure(_chunk(content="0")) != ""


def test_finish_reason_alone_is_substance() -> None:
    """只带 finish_reason 的收尾 chunk 要记 —— 「这轮什么都没产出就结束了」本身是结论。"""
    s = summarize_chunk_structure({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    assert s and "finish=stop" in s


# --------------------------------------------------------------------------
# 不落原文: 密钥
# --------------------------------------------------------------------------


def test_secret_looking_content_never_appears_in_summary() -> None:
    """喂一段含密钥样子的 SSE → 摘要里**不得出现密钥原文**。

    原始请求体含用户内容与密钥, 落原文是新开一个泄露面, 而日志文件比它排查的那次故障
    活得久。这里把密钥放进四个可能漏出来的位置各试一次。
    """
    places = [
        _chunk(content=f"这是我的 key: {FAKE_SECRET}"),
        _chunk(reasoning=f"我应该用 {FAKE_SECRET}"),
        _chunk(reasoning="a", reasoning_content=FAKE_SECRET),
        _chunk(
            content="x",
            tool_calls=[
                {"index": 0, "function": {"name": "http", "arguments": json.dumps({"token": FAKE_SECRET})}},
            ],
        ),
    ]
    for chunk in places:
        s = summarize_chunk_structure(chunk)
        assert FAKE_SECRET not in s, f"密钥泄漏进摘要: {s}"
        # 连片段也不许有 —— 前缀指纹可能截出密钥的头几个字符。
        assert "sk-nonexistent" not in s


def test_secret_at_start_of_content_is_not_leaked_by_fingerprint() -> None:
    """密钥恰好在 content 开头时, 前缀指纹会截到它 —— 这条判据钉住这个最坏情况。

    指纹的取值范围有意很短(32 字符), 但 32 个字符仍足以露出一个密钥的可识别前缀。所以
    这条断言的是: content 以密钥开头时, 摘要里不出现那个可识别前缀。
    """
    s = summarize_chunk_structure(_chunk(content=FAKE_SECRET + " 剩下的话"))
    assert "sk-nonexistent" not in s, f"前缀指纹漏出了密钥头部: {s}"


# --------------------------------------------------------------------------
# 是 INFO 不是 DEBUG, 且真的接上了线
# --------------------------------------------------------------------------


class _FakeContent:
    """假的 `resp.content` —— 按行异步吐 SSE。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line

        return gen()


class _FakeResp:
    def __init__(self, lines: list[bytes]) -> None:
        self.status = 200
        self.content = _FakeContent(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def post(self, *a, **kw):
        return _FakeResp(self._lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


async def _drain(monkeypatch: pytest.MonkeyPatch, chunks: list[dict]) -> None:
    """真的跑一遍 `AiClient.stream()`, 喂给它一串 SSE 行。"""
    mod = ai_client_mod
    lines = [b"data: " + json.dumps(c).encode() + b"\n" for c in chunks] + [b"data: [DONE]\n"]
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(lines))
    monkeypatch.setattr(mod, "resolve_connector_and_endpoint", lambda socket: (None, "http://nonexistent.invalid/v1"))
    client = AiClient("nonexistent.invalid:1")
    async for _ in client.stream({"messages": [{"role": "user", "content": "hi"}]}):
        pass


@pytest.mark.anyio
async def test_structure_summary_is_emitted_at_info_level(
    monkeypatch: pytest.MonkeyPatch, captured: list[tuple[str, str]]
) -> None:
    """**摘要真的被打出来, 且级别是 INFO。**

    为什么必须是 INFO: 生产走批量模式, `_run.py` 把级别钉死在 INFO, **生产没有任何路径能
    开出全局 DEBUG** —— DEBUG 的探针在生产等于什么都不输出。thinking 泄漏查不到根因就是
    因为能记原始 SSE 的三处全在 DEBUG。

    sink 收全部级别(`level=0`), 所以把这行降成 DEBUG 时**消息照样收得到**, 只有级别变 ——
    这条判据因此靠 `level == "INFO"` 转红, 而不是靠消息消失。
    """
    await _drain(
        monkeypatch,
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            _chunk(content="你好", tool_calls=[{"index": 0, "function": {"name": "read", "arguments": "{}"}}]),
        ],
    )

    hits = [(lvl, msg) for lvl, msg in captured if "AI chunk structure" in msg]
    assert hits, f"结构摘要一行都没打出来; 收到的是 {captured}"
    level, message = hits[0]
    assert level == "INFO", f"结构摘要必须是 INFO, 实际 {level}"
    # 内容上确实是结构摘要, 不是别的碰巧同名的行。
    assert "tool_calls=1(" in message and "[0]name" in message


@pytest.mark.anyio
async def test_summary_logged_once_per_response_not_per_chunk(
    monkeypatch: pytest.MonkeyPatch, captured: list[tuple[str, str]]
) -> None:
    """每个响应只打一行, 不是每个 chunk 一行。

    一个回合几百个 delta; 每 chunk 一行 INFO 会把日志的其余部分埋掉 —— 体量本身就成了
    新的盲区。
    """
    await _drain(monkeypatch, [_chunk(content=f"第{i}段") for i in range(40)])
    hits = [msg for _, msg in captured if "AI chunk structure" in msg]
    assert len(hits) == 1, f"打了 {len(hits)} 行"


@pytest.mark.anyio
async def test_role_only_opener_does_not_consume_the_one_line_budget(
    monkeypatch: pytest.MonkeyPatch, captured: list[tuple[str, str]]
) -> None:
    """只有 role 的首 chunk 不占额度 —— 那一行摘要要落在真正带内容的 chunk 上。

    否则每条流的摘要都是 `reasoning=absent content=absent tool_calls=absent`, 对每条健康
    的流都成立, 于是探针记下的恰好是最没有信息的那一个 chunk。
    """
    await _drain(
        monkeypatch,
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            _chunk(reasoning="在想事情"),
        ],
    )
    hits = [msg for _, msg in captured if "AI chunk structure" in msg]
    assert len(hits) == 1
    assert "reasoning=reasoning" in hits[0], f"摘要落在了空的首 chunk 上: {hits[0]}"


@pytest.mark.anyio
async def test_raw_sse_body_is_never_logged(monkeypatch: pytest.MonkeyPatch, captured: list[tuple[str, str]]) -> None:
    """整条链路跑完, **原始正文一个字节都不进日志**(任何级别)。

    断言范围是 sink 收到的全部行, 不只是摘要那一行: 别处不小心打了原文, 这条也要红。
    """
    user_text = "我的密钥是 " + FAKE_SECRET + ", 请不要记到日志里"
    await _drain(monkeypatch, [_chunk(content=user_text)])
    blob = "\n".join(msg for _, msg in captured)
    assert FAKE_SECRET not in blob
    assert "sk-nonexistent" not in blob
