"""A2 的**跨层**判据: 线索 -> 规则 -> 钱, 那两段接缝对得上。

为什么单独一个文件: `voucher_clues` / `voucher_rules` / `saving_calc` / `_offer_engine` **各自**
都有判据, 合起来 200 条上下, 但**没有一条判"接得起来"**。分层判据全绿而接缝是错的, 是完全可能的
—— 2026-09-21 就是这么发现 `voucher_rules` 在 SKILL 里一次都没出现、生产代码零调用的: 每一层自己
都好, 没人验中间那段。

所以这里不重复各层内部的事, 只钉三件**只有跨层才看得见**的事:

1. **形状对得上**: `voucher_rules` 出来的 `rules` 直接 `json.dumps` 就能当 `saving_calc` 的
   `rules_json`。它俩一个返回 `{ok, rules, missing, ...}`、一个要**裸数组**, 是最容易接错的地方。
2. **闸门跨层有效**: 有效期读不到的券进不了 `rules`, 因此也就进不了 `可用` —— 不是"被标成不可用",
   而是**根本没到引擎里**。这条要在最下游看见才算数。
3. **接错会响, 不会静默**: 把整个 payload 当 `rules_json` 传必须 `ok=false`。一个"传错也照算"的
   接口会让模型少算几张券还一路 green, 那正是本场景最贵的一类错。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

# 运行期靠同级 conftest 把 tools 目录挂上 sys.path(裸名导入); ty 不认那个插入,
# 只能按包路径解析。与 test_voucher_clues.py 同套写法。
if TYPE_CHECKING:
    from agents.desktop.tools import saving_calc as _saving_calc
    from agents.desktop.tools import voucher_rules as _voucher_rules
else:
    import saving_calc as _saving_calc
    import voucher_rules as _voucher_rules

#: 照页面抄下来的草稿。形状取自 `voucher_rules` 的文档, `原文` 是页面上的写法。
_DRAFTS: list[dict[str, Any]] = [
    {
        "id": "wh-canyin-30",
        "原文": "满30减7.8元",
        "来源": "https://m.wh.bendibao.com/news/123/2026/9/a.html",
        "核验于": "2026-09-21",
        "有效期": {"起": "2026-09-01", "止": "2026-12-31"},
        "适用品类": ["餐饮"],
        "适用城市": ["武汉"],
    },
    {
        "id": "wh-jiadian-1000",
        "原文": "满1000减100元",
        "来源": "https://m.wh.bendibao.com/news/123/2026/9/b.html",
        "核验于": "2026-09-21",
        "有效期": {"起": "2026-09-01", "止": "2026-12-31"},
        "适用品类": ["家电"],
        "适用城市": ["武汉"],
    },
    {
        "id": "wh-no-window",
        "原文": "满50减20元",
        "来源": "https://m.wh.bendibao.com/news/123/2026/9/c.html",
        "核验于": "2026-09-21",
        # 有效期读不到 —— 三道硬闸门之一。引擎里"没有窗口"等于"一直有效", 所以这张
        # **不能**进 rules, 否则过期券会被当成能领。
        "有效期": {},
    },
]

#: 一单餐饮。两张券的门槛都够(1200 > 30 / 1200 > 1000), 所以被挡下来的只能是因为品类。
_ORDER = {"结算价": 1200, "品类": "餐饮", "城市": "武汉", "日期": "2026-09-21"}


async def _rules_payload() -> dict[str, Any]:
    return json.loads(await _voucher_rules.voucher_rules(json.dumps(_DRAFTS, ensure_ascii=False)))


async def _calc(rules_json: str) -> dict[str, Any]:
    return json.loads(await _saving_calc.saving_calc(json.dumps(_ORDER, ensure_ascii=False), rules_json))


async def test_drafts_become_rules_the_engine_accepts() -> None:
    """跨层形状: `voucher_rules` 的 `rules` 直接喂 `saving_calc`, 算得出来。

    这一条是"线串起来了"的最小证据 —— 前面每一层各自的判据都给不出它。
    """
    payload = await _rules_payload()
    assert payload["ok"] is True
    assert payload["rules"], "夹子该至少产出一条规则"

    calc = await _calc(json.dumps(payload["rules"], ensure_ascii=False))

    assert calc["ok"] is True, calc
    assert calc["engine"] == "local-rules"
    # 门槛与面额是 `voucher_rules` 从 `原文` 里抠的, 一路带到了最终结果里
    available = {r["id"]: r for r in calc["可用"]}
    assert available["wh-canyin-30"]["减"] == 7.8
    assert calc["最优"]["到手价"] == 1192.2


async def test_terms_are_parsed_from_the_page_wording() -> None:
    """面额/门槛来自**页面原文**, 不是模型填的 —— 跨层看得到的证据在后半段。"""
    payload = await _rules_payload()
    by_id = {r["id"]: r for r in payload["rules"]}

    assert by_id["wh-canyin-30"]["门槛"] == 30.0
    assert by_id["wh-canyin-30"]["面额"] == 7.8
    assert by_id["wh-jiadian-1000"]["门槛"] == 1000.0
    assert by_id["wh-jiadian-1000"]["面额"] == 100


async def test_a_voucher_without_a_window_never_reaches_the_engine() -> None:
    """硬闸门跨层: 有效期读不到的券**不进 rules**, 因此也不进 `可用`。

    注意判的是"根本不在下游出现", 而不是"下游把它标成不可用" —— 后者说明它已经带着一个
    **编出来的有效期**进了引擎, 那才是要防的。
    """
    payload = await _rules_payload()

    assert "wh-no-window" not in {r["id"] for r in payload["rules"]}
    missing = {m["id"]: m for m in payload["missing"]}
    assert "wh-no-window" in missing, "被闸门挡下的券必须**带着原因**出现, 不能悄悄消失"
    assert "有效期" in missing["wh-no-window"]["why"]

    calc = await _calc(json.dumps(payload["rules"], ensure_ascii=False))
    everywhere = {r["id"] for r in calc["可用"]} | {r["id"] for r in calc["不可用"]}
    assert "wh-no-window" not in everywhere


async def test_the_engine_still_gates_on_category_across_the_seam() -> None:
    """两层的闸门**叠加**: 券能过 `voucher_rules` 的三道闸门, 仍可能被引擎的品类闸门挡下。

    这单是餐饮, 家电那张门槛也够 —— 被挡下来只能是因为品类。这条同时说明两件事: 闸门
    没有被绕过, 以及"过了规则层"不等于"能用"。
    """
    payload = await _rules_payload()
    calc = await _calc(json.dumps(payload["rules"], ensure_ascii=False))

    unavailable = {r["id"]: r for r in calc["不可用"]}
    assert "wh-jiadian-1000" in unavailable
    assert "品类" in unavailable["wh-jiadian-1000"]["原因"]


async def test_wiring_the_whole_payload_instead_of_rules_fails_loudly() -> None:
    """接错的形态必须**响**: `rules_json` 要的是裸数组, 不是 `voucher_rules` 的整个返回。

    这是这条链上最容易犯的错(两边的形状只差一层), 而一个"传错也照算"的接口会让模型少算
    几张券却一路 green —— 所以这里钉的是"错要错出声"。
    """
    payload = await _rules_payload()
    calc = await _calc(json.dumps(payload, ensure_ascii=False))

    assert calc["ok"] is False
    assert "数组" in calc["reason"]


async def test_missing_rules_say_how_to_fix_instead_of_guessing() -> None:
    """`missing` 的说明要给出**出路**(回页面补), 并说清**为什么不能糊一个值过去**。

    只是"这张券不行"是不够的 —— 模型手上就有一堆看起来很合理的默认值(比如把文章日期当成
    有效期), 说明里必须把那个诱人的错答堵掉, 否则它照样会自己填一个。
    """
    payload = await _rules_payload()
    why = payload["missing"][0]["why"]

    assert "页面" in why, f"没给出路: {why}"
    # 引擎里"没有窗口"等于"一直有效" —— 这正是不许填默认值的理由, 得写在说明里
    assert "一直有效" in why, f"没说清为什么要挡: {why}"
