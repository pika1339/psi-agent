"""`saving_read` 形态二的回归判据 —— 页面级的键值行 + 区块状态。

形态一(重复的列表项)的 38 条判据一条都没动, 在 `test_saving_read.py` 里原样跑。本文件只判新加
的第二种形态, 按重要性排:

1. **`markers` 的等价物: 区块锚点**。这是同一个坑的第二层: 同一段行选择器读到 0 行, 可能是这一
   块**真的没有行**, 也可能是**区块结构变了** —— 只有锚点能区分。所以锚点 / 行根一根都没命中 ->
   `ok=false, reason=page_shape_changed`, 并且**整条读定义一起降级**: 半截事实(这块读出了、那块
   没读出)会被下游当成完整事实用。
2. **`ok=false` 一律不带事实字段**。形态二的字段(`rows` / `states` / `screened_out`)同样守这条。
   还有一条同源的:**键的缺席表示"没问", 不是"没有"** —— 只声明列表项的读定义不会回一个空
   `rows: {}`; 只声明区块的读定义不会回 `count: 0`。
3. **绝不抽取收货人 / 支付信息**。结算页**同屏**渲染收货人姓名、手机号、详细地址、银行卡后四位,
   而形态二是通用键值抽取, 顺手就能抓进来。三道闸门各有判据: 结构隔离(行只在锚点元素里找)、
   页面层拦截(JS, 由 node 跑真的代码判)、返回层复核(构造一个含 PII 的页面, 断言抽出来的结果里
   **没有**这些)。
4. **事实与判定分开**: 负值 `-￥22.75` 与币种前缀原样透传, 不做归一、不算减法。

页面那一半(注入的 JS)由 **node 跑真的代码**来判 —— 两道闸门只存在于 JS 里, 只喂一个拼好的 JSON
是判不出来的。node 不存在就跳过; 生产链路上它本来就必需(Playwright MCP 经 npx 起)。

**夹具与出厂定义分开**: 结算页那次真机重新核验(2026-09-18)已经做完, 选择器进了
`platforms/jd.yaml` 的 `reads.checkout`; 出厂那份的判据在 `test_saving_read.py` 里。本文件仍用
**夹具**(形状取自真实结算页, 选择器是自造的), 判的是"区块形状怎么读"这件事本身 —— 这样改页面
配置不会弄红形状判据, 反过来也一样。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

# 运行期靠同级 conftest 把 tools 目录挂上 sys.path(裸名导入); ty 不认那个插入,
# 只能按包路径解析。与 test_saving_read.py 同套写法。
if TYPE_CHECKING:
    from agents.desktop.tools import _browser_eval
    from agents.desktop.tools import saving_login as _login
    from agents.desktop.tools import saving_read as _saving_read
else:
    import _browser_eval
    import saving_login as _login
    import saving_read as _saving_read

_CHECKOUT_URL = "https://trade.jd.com/shopping/order/getOrderInfo.action"

NODE = shutil.which("node")
#: 判 JS 那一半要用真的 JS 引擎。node 是这支链路本来的依赖(Playwright MCP 经 npx 起),
#: 所以跳过只发生在没装 node 的机器上, 而不是"某天悄悄不判了"。
requires_node = pytest.mark.skipif(NODE is None, reason="判注入页面的 JS 需要 node")

#: 夹具平台: 一条只声明形态一, 一条只声明区块, 一条两者都声明(结算页本来就需要)。
_FIXTURE_YAML = """
key: fixture
name: 夹具
gate: "https://order.jd.com/center/list.action"
login_hosts: [passport.jd.com]
reads:
  checkout:
    title: "结算页(形状夹具)"
    url: "https://trade.jd.com/shopping/order/getOrderInfo.action"
    host: "trade.jd.com"
    path_prefix: "/shopping/order/getOrderInfo.action"
    markers: [".checkout-page"]
    blocks:
      - name: payment
        anchor: [".payment-summary", ".payment-summary-item"]
        item: [".payment-summary-item"]
        label: ".payment-summary-item__title"
        value: ".payment-summary-item__price"
      - name: coupon_area
        anchor: [".quan-area-1a2b3c", ".quan-area"]
        states:
          tab_available: {sel: [".tab-available"], presence: true}
          tab_unavailable: {sel: [".tab-unavailable"], presence: true}
          tab_code: {sel: [".tab-code"], presence: true}
          empty: {sel: [".noData-531368"], presence: true, evidence: [".coupon-item"]}
  wallet:
    title: "券包(两形态同页)"
    url: "https://quan.jd.com/user_quan.action"
    host: "quan.jd.com"
    path_prefix: "/user_quan.action"
    markers: [".mod-coupon", ".coupon-items"]
    item: ".coupon-item"
    fields:
      amount: {sel: ".c-price strong"}
      threshold: {sel: [".c-limit", ".c-limit--legacy"]}
    blocks:
      - name: summary
        anchor: [".mod-coupon"]
        item: ".coupon-item"
        label: ".c-price"
        value: ".c-limit"
"""

_PAYMENT_ROWS = [
    {"label": "商品总额", "value": "￥8999.00"},
    {"label": "运费", "value": "￥0.00"},
    {"label": "共减", "value": "-￥22.75"},
]
_COUPON_STATES = {"tab_available": True, "tab_unavailable": True, "tab_code": True, "empty": True}


def _page_text(url: str = _CHECKOUT_URL, **over: Any) -> str:
    """拼一段 `browser_evaluate` 会拿回来的文本(含 MCP 常见的 `### Result` 前缀)。"""
    payload: dict[str, Any] = {
        "url": url,
        "title": "订单结算页",
        "markers": {".checkout-page": 1},
        "anchors": {"payment": 3, "coupon_area": 1},
        "row_hits": {"payment": 3},
        "rows": {"payment": list(_PAYMENT_ROWS)},
        "states": {"coupon_area": dict(_COUPON_STATES)},
        "blocked": 0,
    }
    payload.update(over)
    return "### Result\n" + json.dumps(payload, ensure_ascii=False)


async def _call(**kwargs: Any) -> dict[str, Any]:
    return json.loads(await _saving_read.saving_read(**kwargs))


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把"读页面"换成喂一段固定结果 —— 测试不启动浏览器、不连 MCP。"""

    def _install(result: str | Exception) -> None:
        async def _fake_evaluate(function: str) -> str:
            assert function.strip(), "生成的 JS 不该是空的"
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(_browser_eval, "evaluate", _fake_evaluate)

    return _install


@pytest.fixture
def platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把平台注册表换成夹具平台(见 `_FIXTURE_YAML`)。注册表只有一份, 直接换 saving_login 的目录。"""
    (tmp_path / "fixture.yaml").write_text(_FIXTURE_YAML, encoding="utf-8")
    monkeypatch.setattr(_login, "_platforms_dir", lambda: tmp_path)
    return tmp_path


def _checkout_spec(platforms: Path) -> dict[str, Any]:
    """从夹具 yaml 收出结算页那条读定义的 spec —— JS 判据用的是**真的会送进页面的那段**。"""
    data = yaml.safe_load((platforms / "fixture.yaml").read_text(encoding="utf-8"))
    spec = _saving_read._spec(data["reads"]["checkout"])
    assert spec is not None
    return spec


# --------------------------------------------------------------------------- #
# 1. 页面级的键值行
# --------------------------------------------------------------------------- #


async def test_page_level_rows_come_back_as_facts(page: Any, platforms: Path) -> None:
    """结算页最有价值的那些事实是**页面级的行**, 不是列表 —— 这正是一种新的抽取形态。"""
    page(_page_text())

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is True
    assert payload["rows"] == {"payment": _PAYMENT_ROWS}
    assert payload["source"] == "saving_read:fixture:checkout"


async def test_row_values_pass_through_verbatim(page: Any, platforms: Path) -> None:
    """负值与币种前缀在**同一格文本**里, 一起原样透传 —— 不做归一, 更不算减法。"""
    page(_page_text())

    rows = (await _call(platform="fixture", target="checkout"))["rows"]["payment"]
    assert rows[2]["value"] == "-￥22.75"
    assert rows[0]["value"] == "￥8999.00"
    assert "-" in rows[2]["value"] and "￥" in rows[2]["value"]


async def test_block_states_report_tabs_and_the_empty_notice(page: Any, platforms: Path) -> None:
    """券区的价值一半在"有哪些页签"、一半在"空态提示在不在" —— 都是状态, 不是行。"""
    page(_page_text())

    states = (await _call(platform="fixture", target="checkout"))["states"]["coupon_area"]
    assert states == _COUPON_STATES
    assert states["empty"] is True, "空态提示在 = 这一段事实必须原样交出去"


async def test_a_read_declaring_only_blocks_does_not_invent_the_list_shape(page: Any, platforms: Path) -> None:
    """**键的缺席表示"没问", 不是"没有"**: 只声明区块的读定义不该回一个 `count: 0`。"""
    page(_page_text())

    payload = await _call(platform="fixture", target="checkout")
    for field in ("count", "coupons", "empty"):
        assert field not in payload, f"这条读定义没声明列表项, 不该出现 {field}"


# --------------------------------------------------------------------------- #
# 2. 两种形态可以同页共存
# --------------------------------------------------------------------------- #


async def test_both_shapes_can_be_declared_in_one_read(page: Any, platforms: Path) -> None:
    """结算页本来就需要两种: 券是列表(一页 N 张), 付款详情是页面级的行。"""
    page(
        _page_text(
            url="https://quan.jd.com/user_quan.action",
            markers={".mod-coupon": 1, ".coupon-items": 1},
            anchors={"summary": 3},
            row_hits={"summary": 2},
            count=1,
            coupons=[{"amount": "5", "threshold": "满49可用"}],
            rows={"summary": [{"label": "￥5", "value": "满49可用"}, {"label": "￥10", "value": "满99可用"}]},
            states={},
        )
    )

    payload = await _call(platform="fixture", target="wallet")
    assert payload["ok"] is True
    assert payload["count"] == 1 and payload["coupons"][0]["amount"] == "5"
    assert payload["rows"]["summary"][0] == {"label": "￥5", "value": "满49可用"}
    assert "states" not in payload, "这条读定义没声明区块状态"


# --------------------------------------------------------------------------- #
# 3. 锚点: "读不到" != "没有"(形态二的同一道防线)
# --------------------------------------------------------------------------- #


async def test_a_missing_block_anchor_is_unknown_not_zero_rows(page: Any, platforms: Path) -> None:
    """**最关键的一条**: 区块锚点找不到 -> 未知。绝不能把"读到 0 行"当成"没有这些行"。"""
    page(_page_text(anchors={"coupon_area": 1}, row_hits={}, rows={}))

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is False
    assert payload["reason"] == "page_shape_changed"
    assert payload["basis"] == "missing_anchors"
    assert payload["missing_anchors"] == {"payment": [".payment-summary", ".payment-summary-item"]}
    assert "读不到" in payload["note"]


async def test_a_row_root_that_matches_nothing_is_unknown_too(page: Any, platforms: Path) -> None:
    """锚点在、行根一根都没命中 = "区块在但行读不出" —— 正是本模块最贵的那类错。"""
    page(_page_text(row_hits={"payment": 0}, rows={"payment": []}))

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is False
    assert payload["reason"] == "page_shape_changed"
    assert payload["basis"] == "missing_rows"
    assert payload["missing_rows"] == ["payment"]


async def test_one_broken_block_degrades_the_whole_read(page: Any, platforms: Path) -> None:
    """半截事实会被当成完整事实用: 一块锚点找不到, 另一块也不许交事实。"""
    page(_page_text(anchors={"coupon_area": 1}, row_hits={}, rows={}))

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is False
    for field in ("rows", "states", "screened_out", "count", "coupons", "empty"):
        assert field not in payload, f"ok=false 的返回里不该有 {field}"


async def test_row_hits_survive_the_partial_anchor_map(page: Any, platforms: Path) -> None:
    """锚点全在但行根没命中时, 报的是行根而不是锚点 —— 维护者要改的是哪一个得说清。"""
    page(_page_text(row_hits={}, rows={}))

    payload = await _call(platform="fixture", target="checkout")
    assert payload["reason"] == "page_shape_changed"
    assert "missing_anchors" not in payload
    assert payload["missing_rows"] == ["payment"]


FAILURE_CASES = [
    ("missing_anchor", _page_text(anchors={}, rows={}, row_hits={}), "page_shape_changed"),
    ("missing_row_root", _page_text(row_hits={}, rows={}), "page_shape_changed"),
    ("marker_gone", _page_text(markers={}), "page_shape_changed"),
    ("login", _page_text(url="https://passport.jd.com/new/login.aspx"), "logged_out"),
    ("other_page", _page_text(url="https://www.jd.com/"), "wrong_page"),
    ("unparsable", "这不是 JSON", "unparsable_extract"),
]


@pytest.mark.parametrize(("name", "text", "reason"), FAILURE_CASES, ids=[c[0] for c in FAILURE_CASES])
async def test_no_failure_ever_reports_the_new_fact_fields(
    page: Any, platforms: Path, name: str, text: str, reason: str
) -> None:
    """**不变式**: 形态二的字段也一条都不许跟着 `ok=false` 回来。"""
    page(text)

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is False, name
    assert payload["reason"] == reason, name
    for field in ("rows", "states", "screened_out", "count", "coupons", "empty"):
        assert field not in payload, f"{name} 的返回里不该有 {field}"


def test_a_row_block_without_an_anchor_is_a_config_error() -> None:
    """锚点是承重的: 缺了它就没法区分"这一块没有行"和"这一块读不到", 只能算配置错。"""
    assert (
        _saving_read._spec({"markers": [".m"], "blocks": [{"name": "p", "item": ".i", "label": ".l", "value": ".v"}]})
        is None
    )
    assert (
        _saving_read._spec(
            {"markers": [".m"], "blocks": [{"name": "p", "anchor": [], "item": ".i", "label": ".l", "value": ".v"}]}
        )
        is None
    )


@pytest.mark.parametrize(
    "broken",
    [
        {"markers": [".m"], "blocks": [{"name": "p", "anchor": ".a", "item": ".i", "label": ".l"}]},  # 行块缺 value
        {
            "markers": [".m"],
            "blocks": [{"name": "p", "anchor": ".a", "label": ".l", "value": ".v"}],
        },  # 有 label/value 没 item
        {"markers": [".m"], "blocks": [{"name": "p", "anchor": ".a"}]},  # 既没行也没状态
        {"markers": [".m"], "blocks": []},  # 声明了 blocks 却是空的
        {"markers": [".m"], "blocks": ["垃圾"]},  # 不是 mapping
        {"blocks": [{"name": "p", "anchor": ".a", "item": ".i", "label": ".l", "value": ".v"}]},  # 缺 markers
    ],
)
def test_incomplete_block_definitions_are_a_config_error(broken: dict[str, Any]) -> None:
    """配置错要在**碰页面之前**就失败 —— 拿半截 spec 去读, 读回来还会像成功。"""
    assert _saving_read._spec(broken) is None


def test_a_state_only_block_is_valid() -> None:
    """券区那种"只有页签和空态、没有行"的区块是合法的 —— 状态本身就是事实。"""
    spec = _saving_read._spec(
        {
            "markers": [".m"],
            "blocks": [{"name": "coupon_area", "anchor": ".a", "states": {"empty": {"sel": ".e", "presence": True}}}],
        }
    )
    assert spec is not None
    assert spec["blocks"][0]["states"] == {"empty": {"sel": ".e", "presence": True}}
    assert "item" not in spec["blocks"][0]


# --------------------------------------------------------------------------- #
# 4. 硬约束: 绝不抽取收货人 / 支付信息
# --------------------------------------------------------------------------- #

#: 结算页同屏会渲染的东西。构造一个"选择器太宽、把它们一起捞进来"的页面, 断言一条都没漏出去。
_PII_ROWS = [
    {"label": "收货人", "value": "张三"},
    {"label": "联系电话", "value": "13800138000"},
    {"label": "收货地址", "value": "北京市朝阳区某某路 1 号院 2 单元 301 室"},
    {"label": "支付方式", "value": "银行卡 6222 **** **** 1234"},
]
#: 姓名 / 手机号 / 地址 / 卡号: 这些东西**任何位置**出现都算漏了。
_PII_VALUES = ("张三", "13800138000", "北京市朝阳区", "301", "6222", "1234")
#: 标签本身判的是**事实字段**里没有 —— 工具自己的说明文字里会出现它们("命中隐私闸门(收货人 /
#: 手机号 / 地址 / 支付信息)"), 那是讲清楚挡了什么, 不是把页面上的行交出去。
_PII_LABELS = ("收货人", "联系电话", "收货地址", "银行卡")


async def test_recipient_and_payment_details_never_come_back(page: Any, platforms: Path) -> None:
    """**硬约束的判据**: 收货人 / 手机号 / 地址 / 银行卡后四位一条都不许出现在返回里。

    构造的是最坏情况 —— 行选择器太宽, 把这些行一起命中了; 而且**页面那一半没挡住**(`blocked: 0`,
    等价于 JS 那一半坏了 / 被绕过了)。返回层复核必须把整行挡掉(标签就是"这是谁的手机号"的
    证据), 而不是留一个空串 —— 空串会被下游当成"页面上没有"。
    """
    page(
        _page_text(
            anchors={"payment": 7, "coupon_area": 1},
            row_hits={"payment": 7},
            rows={"payment": [*_PAYMENT_ROWS, *_PII_ROWS]},
            blocked=0,
        )
    )

    text = await _saving_read.saving_read(platform="fixture", target="checkout")
    payload = json.loads(text)

    assert payload["ok"] is True
    assert payload["rows"]["payment"] == _PAYMENT_ROWS, "该留的行一条不能少"
    assert payload["screened_out"] == 4
    facts = json.dumps(payload["rows"], ensure_ascii=False)
    for word in (*_PII_LABELS, *_PII_VALUES):
        assert word not in facts, f"{word!r} 不该出现在事实字段里"
    for value in _PII_VALUES:
        assert value not in text, f"{value!r} 不该出现在返回里的任何位置"
    assert "隐私闸门" in payload["note"], "挡掉了东西就要说, 不然没人会去核选择器"


async def test_a_page_half_that_already_screened_is_not_counted_twice(page: Any, platforms: Path) -> None:
    """两道闸门是接力不是叠加: 页面那一半已经挡掉并计过数的, 返回层不该再数一遍。"""
    page(_page_text(blocked=3))

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is True
    assert payload["screened_out"] == 3
    assert payload["rows"]["payment"] == _PAYMENT_ROWS


async def test_the_gate_also_covers_the_list_shape(page: Any, platforms: Path) -> None:
    """形态一与形态二走的是同一道闸门 —— 漏一条路径就等于没有这道闸门。"""
    page(
        _page_text(
            url="https://quan.jd.com/user_quan.action",
            markers={".mod-coupon": 1, ".coupon-items": 1},
            anchors={"summary": 1},
            row_hits={"summary": 2},
            rows={"summary": [{"label": "￥5", "value": "满49可用"}, {"label": "￥10", "value": "满99可用"}]},
            count=1,
            coupons=[{"amount": "5", "threshold": "满49可用", "holder": "张三 13800138000"}],
            states={},
        )
    )

    text = await _saving_read.saving_read(platform="fixture", target="wallet")
    payload = json.loads(text)

    assert payload["ok"] is True
    assert payload["coupons"] == [{"amount": "5", "threshold": "满49可用"}], "命中的那一个字段不要, 其余照交"
    assert payload["screened_out"] == 1
    for word in ("张三", "13800138000"):
        assert word not in text


async def test_a_block_whose_every_row_is_screened_is_a_config_error(page: Any, platforms: Path) -> None:
    """行根命中了、但每一行都被挡掉: 这不是"这一块没有行", 而是选择器打到了隐私模块。"""
    page(
        _page_text(
            anchors={"payment": 4, "coupon_area": 1},
            row_hits={"payment": 4},
            rows={"payment": list(_PII_ROWS)},
            blocked=4,
        )
    )

    payload = await _call(platform="fixture", target="checkout")
    assert payload["ok"] is False
    assert payload["reason"] == "read_blocked_by_pii_gate"
    assert payload["block"] == "payment"
    assert "rows" not in payload and "screened_out" not in payload
    assert "不要据此对用户下结论" in payload["note"]


async def test_the_raw_echo_is_blanked_when_it_looks_like_privacy(page: Any, platforms: Path) -> None:
    """解析失败会回显原文 —— 而"解析失败"恰恰常发生在页面结构变形的时刻, 回显也要过闸门。"""
    page('### Result\n{"url": "...", "consignee": "张三 13800138000"')

    payload = await _call(platform="fixture", target="checkout")
    assert payload["reason"] == "unparsable_extract"
    assert payload["raw"] == _saving_read._PII_ECHO_BLOCKED
    assert "13800138000" not in json.dumps(payload, ensure_ascii=False)


async def test_a_harmless_raw_echo_is_still_echoed(page: Any, platforms: Path) -> None:
    """闸门不能把"什么都挡掉"当成安全 —— 正常原文必须照旧回显, 否则问题就没法查了。"""
    page("### Result\n这不是 JSON")

    payload = await _call(platform="fixture", target="checkout")
    assert payload["reason"] == "unparsable_extract"
    assert "这不是 JSON" in payload["raw"]


def test_the_page_half_carries_the_same_privacy_rules(platforms: Path) -> None:
    """规则只有一份字面量, JS 侧由 _build_js 注入同一份 —— 不各写一套(写两份就会只改一边)。"""
    js = _saving_read._build_js(_checkout_spec(platforms))

    for label in _saving_read._PII_LABELS:
        assert label in js, f"页面那一半少了标签 {label}"
    for pattern in _saving_read._PII_PATTERNS:
        assert json.dumps(pattern)[1:-1] in js, f"页面那一半少了值形状 {pattern}"


# --------------------------------------------------------------------------- #
# 5. 带构建哈希的类名: 每个选择器槽都收候选列表
# --------------------------------------------------------------------------- #


def test_a_selector_slot_takes_candidates_in_order(platforms: Path) -> None:
    """候选按顺序试, 报出来的键用**首选** —— 维护者要改的就是它。"""
    spec = _checkout_spec(platforms)

    assert spec["blocks"][1]["anchor"] == [".quan-area-1a2b3c", ".quan-area"]
    assert _saving_read._key(spec["blocks"][1]["anchor"]) == ".quan-area-1a2b3c"
    assert spec["blocks"][1]["states"]["tab_available"] == {"sel": [".tab-available"], "presence": True}


def test_selectors_stay_data_in_the_generated_js(platforms: Path) -> None:
    """选择器是数据不是代码: 它们必须**原样**(而不是拼成串)进到送给页面的那段 JS 里。"""
    js = _saving_read._build_js(_checkout_spec(platforms))
    marker = "JSON.stringify(run("
    embedded = json.loads(js[js.index(marker) + len(marker) : js.rindex("))")])

    assert embedded["blocks"][0]["anchor"] == [".payment-summary", ".payment-summary-item"]
    assert embedded["blocks"][0]["item"] == [".payment-summary-item"]
    assert embedded["blocks"][1]["states"]["empty"]["evidence"] == [".coupon-item"]
    assert "note" not in json.dumps(embedded), "yaml 里给人看的 note 不该混进 spec"


async def test_listing_targets_shows_what_each_page_gives(page: Any, platforms: Path) -> None:
    """列表要能说清每条读定义交回哪些事实 —— 两种形态都要列出来。"""
    payload = await _call(platform="fixture")

    row = next(r for r in payload["targets"] if r["target"] == "checkout")
    assert "rows.payment" in row["gives"]
    assert "states.coupon_area" in row["gives"]
    wallet = next(r for r in payload["targets"] if r["target"] == "wallet")
    assert {"amount", "threshold", "rows.summary"} <= set(wallet["gives"])


# --------------------------------------------------------------------------- #
# 6. 页面那一半: 用 node 跑真的代码
# --------------------------------------------------------------------------- #


def _dom_stub(tree: dict[str, Any], *, href: str, title: str) -> str:
    """一段最小的 DOM 替身: 只实现这段 JS 用到的三样(querySelectorAll / textContent / getAttribute)。

    **它不是 CSS 引擎** —— 选择器按字面量去 map 里取, 不做匹配。判据要验的是 JS 的抽取与闸门
    逻辑, 不是浏览器的选择器实现。
    """
    return (
        "const mk = (o) => ({\n"
        "  textContent: o.text || '',\n"
        "  attrs: o.attrs || {},\n"
        "  map: Object.fromEntries(Object.entries(o.map || {}).map(([k, v]) => [k, v.map(mk)])),\n"
        "  querySelectorAll(sel) { return this.map[sel] || []; },\n"
        "  getAttribute(name) { return (name in this.attrs) ? this.attrs[name] : null; },\n"
        "});\n"
        f"globalThis.document = Object.assign(mk({json.dumps(tree, ensure_ascii=False)}), "
        f"{{ title: {json.dumps(title, ensure_ascii=False)} }});\n"
        f"globalThis.location = {{ href: {json.dumps(href, ensure_ascii=False)} }};\n"
    )


def _run_in_node(
    js: str, tree: dict[str, Any], tmp_path: Path, *, href: str = _CHECKOUT_URL, title: str = "订单结算页"
) -> dict[str, Any]:
    """把生成的那段 JS 放进 node 里跑, 拿回它真的读出了什么。"""
    script = _dom_stub(tree, href=href, title=title) + "\nprocess.stdout.write(" + js + ");\n"
    path = tmp_path / "run.mjs"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE or "node", str(path)], capture_output=True, text=True, encoding="utf-8", check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _payment_row(label: str, value: str) -> dict[str, Any]:
    return {
        "text": f"{label} {value}",
        "map": {
            ".payment-summary-item__title": [{"text": label}],
            ".payment-summary-item__price": [{"text": value}],
        },
    }


def _checkout_tree(*, coupons_in_area: int = 0) -> dict[str, Any]:
    """真实结算页的形状: 付款区块在, 券区在, 而收货人模块**同屏但在付款区块外面**。

    券区的锚点首选是带构建哈希的类名(**故意不在树里**), 靠第二个候选命中 —— 这就是"带哈希的
    类名会变"那件事在数据侧的兜法。`coupons_in_area` 用来在同一块里放几张券, 判空态的凭据。
    """
    return {
        "map": {
            ".checkout-page": [{"text": ""}],
            ".payment-summary": [
                {
                    "text": "付款详情",
                    "map": {
                        ".payment-summary-item": [
                            _payment_row("商品总额", "￥8999.00"),
                            _payment_row("共减", "-￥22.75"),
                            # 选择器太宽时真的会命中这些行 —— 闸门必须在**页面层**就挡住,
                            # 让它们根本不跨 MCP 边界。
                            _payment_row("收货人", "张三"),
                            _payment_row("联系电话", "13800138000"),
                        ]
                    },
                }
            ],
            # 同屏、但在付款区块**外面**。结构隔离要保证付款区块的行够不到它。
            ".consignee": [{"text": "收货人 李四", "map": {".payment-summary-item": [{"text": "李四 13900139000"}]}}],
            # 券区: 三个页签 + 一个空态。空态的 evidence 是"券区里有没有券"。
            ".quan-area": [
                {
                    "text": "优惠券",
                    "map": {
                        ".tab-available": [{"text": "可用优惠券 (3)"}],
                        ".tab-unavailable": [{"text": "不可用优惠券"}],
                        ".coupon-item": [{"text": "一张券"}] * coupons_in_area,
                    },
                }
            ],
        }
    }


@requires_node
def test_the_page_half_extracts_rows_and_screens_them_before_they_leave(tmp_path: Path, platforms: Path) -> None:
    """这段 JS 的判据只能靠真的跑它 —— 结构隔离与页面层拦截都只存在于这一半里。"""
    js = _saving_read._build_js(_checkout_spec(platforms))

    data = _run_in_node(js, _checkout_tree(), tmp_path)

    assert data["markers"] == {".checkout-page": 1}
    assert data["anchors"] == {"payment": 1, "coupon_area": 1}, "带哈希的首选没命中, 要回落到第二个候选"
    assert data["row_hits"]["payment"] == 4, "行根在锚点**里面**找"
    assert data["rows"]["payment"] == [
        {"label": "商品总额", "value": "￥8999.00"},
        {"label": "共减", "value": "-￥22.75"},
    ]
    assert data["blocked"] == 2, "收货人 / 联系电话两行在页面层就被挡掉"
    assert "张三" not in json.dumps(data, ensure_ascii=False)
    assert "13800138000" not in json.dumps(data, ensure_ascii=False)
    assert "李四" not in json.dumps(data, ensure_ascii=False), "区块外的收货人模块够不到 —— 这是结构隔离"


@requires_node
def test_the_page_half_needs_evidence_before_it_says_false(tmp_path: Path, platforms: Path) -> None:
    """`presence` 判 false 是一条否定断言: evidence 也看不见时只能是"不知道"(键缺席)。"""
    js = _saving_read._build_js(_checkout_spec(platforms))

    without_evidence = _run_in_node(js, _checkout_tree(), tmp_path)
    states = without_evidence["states"]["coupon_area"]
    assert states["tab_available"] is True
    assert "empty" not in states, "空态选择器没命中、evidence 也没有 -> 只能说不知道, 不能说 false"

    with_evidence = _run_in_node(js, _checkout_tree(coupons_in_area=3), tmp_path)
    states = with_evidence["states"]["coupon_area"]
    assert states["empty"] is False, "看得见券 -> 判 false 有凭据, 这时才交事实"


@requires_node
def test_the_page_half_reports_zero_rows_when_the_block_is_really_gone(tmp_path: Path, platforms: Path) -> None:
    """锚点全不在时, 这一半必须如实报 0 —— 降级成 unknown 是 Python 那一半的判定, 别混在一处。"""
    js = _saving_read._build_js(_checkout_spec(platforms))

    data = _run_in_node(js, {"map": {".checkout-page": [{"text": ""}]}}, tmp_path)

    assert data["anchors"] == {"payment": 0, "coupon_area": 0}
    assert data["rows"] == {}
    assert data["states"] == {}
