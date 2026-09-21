"""`saving_read` 从 MCP 回显里取那一段 JSON 的回归判据。

**这条判据是补出来的, 起因是一次真实调用**: 2026-09-20 对着活的结算页跑
`saving_read(platform="jd", target="checkout")`, 页面上的 JS 明明跑通了并回了完整对象
(`markers` 全是 0, 因为那一页是别处的空白页), 但工具回的是
`ok=false, reason=unparsable_extract`。

根因是取 JSON 的方式: 当时取的是**第一个 `{` 到最后一个 `}`**。而 Playwright MCP 的返回是

    ### Result
    "<结果, 被整体转义成 JSON 字符串>"
    ### Ran Playwright code
    ```js
    await page.evaluate('() => ((() => { ... })())')
    ```

尾部那段代码回显里全是括号, 于是切出来的串从结果一直吞到 JS 末尾, `json.loads` 必然失败。
**这个错会伪装成"页面读不出来"**, 而"读不出来"在这个场景里是最容易被当成事实用掉的一类结果 ——
所以它比一般解析 bug 更贵。

两条判据:
1. **真实外形必须读成**。用上面那个外形逐个试: 字符串转义的结果、裸对象、`### Result` 前缀、
   代码围栏。
2. **绝不能从代码回显里捞到一个"看起来像结果的错对象"**。注入的 JS 里就有一个字面量对象
   (`const PII = {...}`), 早期"全串找括号"的写法会把它当成结果返回 —— 那会是一条**安静的错误事实**,
   比失败更糟。所以外壳必须先切掉, 且只认信封体里第一个括号配平的对象。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

# 运行期靠同级 conftest 把 tools 目录挂上 sys.path(裸名导入); ty 不认那个插入,
# 只能按包路径解析。与 test_saving_read.py 同套写法。
if TYPE_CHECKING:
    from agents.desktop.tools import saving_read as _saving_read
else:
    import saving_read as _saving_read

_PAYLOAD = {
    "url": "https://trade.jd.com/shopping/order/getOrderInfo.action",
    "title": "订单结算",
    "markers": {".payment-summary": 1, ".production-item": 1},
    "anchors": {"price_summary": 1, "coupon_area": 1},
    "rows": {"price_summary": [{"label": "商品总额", "value": "￥23.80"}]},
    "row_hits": {"price_summary": 3},
    "states": {"coupon_area.empty": True},
    "blocked": 0,
}
_JSON_TEXT = json.dumps(_PAYLOAD, ensure_ascii=False)

#: 代码回显里那个**很像结果的错对象**。真实 JS 里注入的就是这一份 PII 配置。
_CODE_ECHO = (
    "### Ran Playwright code\n```js\nawait page.evaluate('() => ((() => {\n"
    '  const PII = {"labels":["收货人","手机号"],"rules":[]};\n'
    "  return null;\n})())')\n```\n"
)


def _envelope(result_line: str) -> str:
    """拼出真实的 MCP 回显外形: `### Result` + 结果 + 代码回显。"""
    return f"### Result\n{result_line}\n{_CODE_ECHO}"


def test_real_envelope_is_read() -> None:
    """真实外形(结果被转义成 JSON 字符串 + 尾部代码回显)必须读成, 且是**那一个**对象。"""
    got = _saving_read._parse_payload(_envelope(json.dumps(_JSON_TEXT, ensure_ascii=False)))
    assert got == _PAYLOAD


def test_naive_slice_would_fail() -> None:
    """把"第一个 `{` 到最后一个 `}`"这个旧写法钉死成不合格, 免得哪天被改回去。

    判的是**形状**: 真实外形下那段切片本来就不是合法 JSON。旧实现因此永远返回 None,
    表现成"所有页面都读不出来"。
    """
    text = _envelope(json.dumps(_JSON_TEXT, ensure_ascii=False))
    naive = text[text.find("{") : text.rfind("}") + 1]
    try:
        json.loads(naive)
    except ValueError:
        return
    raise AssertionError("真实外形下朴素切片居然能解析 —— 本判据的前提变了, 要重新想")


def test_never_picks_object_out_of_code_echo() -> None:
    """代码回显里的字面量对象绝不能被当成结果 —— 那是一条安静的错误事实。"""
    got = _saving_read._parse_payload(_envelope(json.dumps(_JSON_TEXT, ensure_ascii=False)))
    assert got is not None
    assert "labels" not in got
    assert got["markers"] == {".payment-summary": 1, ".production-item": 1}


def test_no_usable_result_yields_none() -> None:
    """结果那行不是 JSON 时, 不许为了"总得读出点什么"退而返回代码块里的对象。

    只剩代码回显 -> 没有可用结果 -> None。宁可是 `unparsable_extract`, 也不要错对象。
    """
    assert _saving_read._parse_payload(_CODE_ECHO) is None
    assert _saving_read._parse_payload(_envelope("到这里为止什么都没返回")) is None


def test_shapes_that_must_keep_working() -> None:
    """共存的外形一起判, 免得修好一种弄坏另一种。"""
    cases = {
        "裸对象": _JSON_TEXT,
        "Result前缀+裸对象": f"### Result\n{_JSON_TEXT}",
        "代码围栏": f"### Result\n```json\n{_JSON_TEXT}\n```\n{_CODE_ECHO}",
        "结果里有大括号字符": '### Result\n{"a": "}", "b": "{"}\n' + _CODE_ECHO,
    }
    for name, text in cases.items():
        got = _saving_read._parse_payload(text)
        assert isinstance(got, dict), f"{name} 没读成"
    assert _saving_read._parse_payload(cases["结果里有大括号字符"]) == {"a": "}", "b": "{"}


def test_bracket_balance_respects_strings_and_escapes() -> None:
    """配平不能把字符串里的括号算进去, 转义引号也不能提前结束字符串。"""
    text = '### Result\n{"s": "a\\"}\\"b", "t": "{{{"}\n'
    assert _saving_read._parse_payload(text) == {"s": 'a"}"b', "t": "{{{"}


def test_non_json_is_none() -> None:
    """非 JSON 一律 None, 不许把散文认成 JSON。"""
    for text in ("", "no json here", "### Result\nnull\n", "### Result\n[1, 2]\n"):
        assert _saving_read._parse_payload(text) is None, text


def test_empty_object_is_an_object() -> None:
    """`{}` 是合法对象, 照实返回 —— 有没有内容由页面身份判定那一步去说, 解析器不替它下结论。"""
    assert _saving_read._parse_payload("### Result\n{}\n") == {}


def test_envelope_body_is_stripped() -> None:
    """外壳切分本身单独判: 代码回显必须在信封体之外。"""
    body = _saving_read._envelope_body(_envelope(_JSON_TEXT))
    assert body == _JSON_TEXT
    assert "const PII" not in body
