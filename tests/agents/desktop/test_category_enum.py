"""品类枚举 (总枚举 + 场景子集) 的回归判据。

这张表要回答三个问题, 每个问题各有一组判据:

1. **它是不是一个合法品类?** —— ``resolve``。认不出必须**明确报不认识**, 不许猜最近的那个。
   (猜品类 = 猜档位 = 拿另一套政策参数算钱, 这是这条链路上最贵的一类错。)
2. **这个场景认不认它?** —— ``scenario_ids``。国补 10 个必须与资料卡逐条相等,
   因为资料卡才是参数的唯一数据源。
3. **它在这个场景里算哪一档?** —— ``tier_of``。档位**只在场景子集里**,
   总枚举里一个 ``tier`` 都不许有: 「电脑」在国补归家电、在常识里归数码,
   平表加场景子集才能同时说清, 而树会逼着二选一 (数据文件 decisions.computer_tier)。

另有一组**切换运行时来源**的判据: ``subsidy_calc`` / ``policy_query`` / ``saving_calc`` 过去读
``_guobu_categories`` 里那份硬编码 ``ALIASES`` (10 条), 现在读本表 —— 那份已删, 只剩一份数据。
换来源是内部整理, **不是行为变化**, 所以这里钉住两件事:

1. **切换前录下的逐词结果** (``PRE_SWITCH_MATCHES``): 每个词重新问一遍, 答案必须与切换前逐条相同。
   ``None`` 也是一条结果 —— 「认不出」正是它必须继续做到的事。
2. **改表即改行为**: 换过来源之后, 改数据文件必须能改到 ``match_category`` 的答案。
   只比「切换前后结果相同」是抓不出「其实还在读代码里那份、只是恰好一样」的。

``sys.path`` 由同级 ``conftest.py`` 挂好, 所以下面按裸名 import 工具模块;
ty 不认那个运行时插入, 只能按包路径解析 —— 与 ``test_guobu_fact_card.py`` 同一套写法。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from agents.desktop.tools import _category_enum, _fact_cards, _guobu_categories, subsidy_calc
else:
    import _category_enum
    import _fact_cards
    import _guobu_categories
    import subsidy_calc

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = REPO_ROOT / "agents" / "desktop"
ENUM_PATH = WORKSPACE_ROOT / "sources" / "category-enum.yaml"
CARD_PATH = WORKSPACE_ROOT / "fact-cards" / "guobu-2026.yaml"

#: 这两个名字在数据文件与 ``_guobu_categories`` 里都是**字面量**, 不是话术。
GUBOU = "guobu"
LOCAL_VOUCHER = "local_voucher"

#: 唯一一处「跨 id 的后缀命中」, 而且是**要的**: 「平板电脑」落到平板而不是电脑。
#: 这条允许名单之外再出现一对, 就是别名表打架了 —— 见 test_only_the_documented_suffix_...
DOCUMENTED_CROSS_ID_SUFFIXES = {("电脑", "平板")}


def _enum_file() -> dict[str, Any]:
    data = yaml.safe_load(ENUM_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _card() -> dict[str, Any]:
    data = yaml.safe_load(CARD_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


async def _enum() -> dict[str, Any]:
    return await _category_enum.load_enum()


def _duplicate_keys(node: Any, where: str = "") -> list[str]:
    """在**原始 YAML** 里找重复的键。

    ``yaml.safe_load`` 遇到重复键会**静默取最后一个**, 所以「id 唯一」用
    ``len(set(ids)) == len(ids)`` 是查不出来的 —— 那一行永远为真。
    数据文件里多打一个 ``电脑:`` 就是一条被吞掉的品类, 而吞掉的品类看起来
    和「本来就没写」一模一样。这里直接扫节点树。
    """
    found: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                found.append(f"{where}.{key}")
            seen.add(key)
            found.extend(_duplicate_keys(value_node, f"{where}.{key}"))
    elif isinstance(node, yaml.SequenceNode):
        for index, value_node in enumerate(node.value):
            found.extend(_duplicate_keys(value_node, f"{where}[{index}]"))
    return found


def _guobu_ids() -> tuple[str, ...]:
    return tuple(_card()["categories"])


# --------------------------------------------------------------------------- #
# 数据文件本身
# --------------------------------------------------------------------------- #


def test_file_has_no_duplicate_keys() -> None:
    """整份文件不许有重复键 —— 重复的 id 会被 YAML 静默吞掉, 那是最难发现的一种丢品类。"""
    node = yaml.compose(ENUM_PATH.read_text(encoding="utf-8"))
    assert _duplicate_keys(node) == []


def test_enum_loads_and_declares_its_own_freshness() -> None:
    """数据从哪来、什么时候来的, 必须与值一起走 (与 voucher-sources.yaml 同一套期望)。"""
    enum = _enum_file()
    assert enum.get("version")
    assert enum.get("verified_at")


async def test_canonical_ids_are_unique_and_complete() -> None:
    """总枚举 id 唯一; 每个 id 都得有展示名、合法的类别、非空别名表。"""
    enum = await _enum()
    ids = _category_enum.canonical_ids(enum)
    assert len(ids) == len(set(ids))
    assert ids, "总枚举不能是空的"
    for category_id in ids:
        assert _category_enum.display_of(enum, category_id).strip()
        assert _category_enum.kind_of(enum, category_id) in _category_enum.KINDS
        aliases = _category_enum.aliases_of(enum, category_id)
        assert category_id in aliases, f"{category_id} 的别名表里必须含它自己"


async def test_master_enum_carries_no_tier_at_all() -> None:
    """总枚举里**一个 tier 都不许有**。

    有了就说明「档位」被搬回了平表, 而平表只有一个格子 —— 「电脑」在国补归家电、
    在常识里归数码, 塞哪个都是错的 (decisions.computer_tier)。
    """
    enum = await _enum()
    for category_id in _category_enum.canonical_ids(enum):
        entry = _category_enum.entry_of(enum, category_id)
        assert "tier" not in entry, f"总枚举的 {category_id} 不该带档位"
        assert "tiers" not in entry


# --------------------------------------------------------------------------- #
# 别名 -> 规范 id
# --------------------------------------------------------------------------- #


async def test_an_alias_points_at_exactly_one_id() -> None:
    """**同一个别名不能指向两个 id。** 指向两个时「归一」的结果取决于谁先被遍历到,
    而那种错误不会报错, 只会安静地换一套参数。"""
    enum = await _enum()
    assert _category_enum.alias_conflicts(enum) == {}


async def test_no_alias_shadows_another_id() -> None:
    """别名不得吃掉另一个规范 id 的字面量。

    「空调」可以是「空调」的别名, 但不能是「家电」的别名 —— 否则用户说「空调」,
    同一个词在两张表里各有一个答案。
    """
    enum = await _enum()
    ids = set(_category_enum.canonical_ids(enum))
    for category_id in _category_enum.canonical_ids(enum):
        for alias in _category_enum.aliases_of(enum, category_id):
            if alias in ids:
                assert alias == category_id, f"别名 {alias!r} 指向了 {category_id!r}, 但它本身是另一个 id"


async def test_only_the_documented_cross_id_suffixes_exist() -> None:
    """跨 id 的**后缀**命中只允许一条:「平板电脑」里的「电脑」。

    这是国补既有语义 (平板电脑是平板, 不是电脑), 也是 ``match_category`` 用
    「最长命中」的原因。除它之外再出现一对, 就说明别名表里混进了短词 ——
    那种词会把「说明书」「自行车」这类无关说法也吸进来。
    """
    enum = await _enum()
    pairs: set[tuple[str, str]] = set()
    index = _category_enum.alias_index(enum)
    for alias, owner in index.items():
        for other_alias, other_owner in index.items():
            if other_alias != alias and other_alias.endswith(alias) and other_owner != owner:
                pairs.add((alias, other_owner))
    assert pairs == DOCUMENTED_CROSS_ID_SUFFIXES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 国补 10 个: id 本身与常见说法
        ("电脑", "电脑"),
        ("笔记本", "电脑"),
        ("游戏本", "电脑"),
        ("智能手机", "手机"),
        ("pad", "平板"),
        ("手环", "手表"),
        ("智能眼镜", "眼镜"),
        ("挂机", "空调"),
        ("电冰箱", "冰箱"),
        ("滚筒洗衣机", "洗衣机"),
        ("电视机", "电视"),
        ("燃气热水器", "热水器"),
        # 本体/地方券那批
        ("家用电器", "家电"),
        ("超市", "商超"),
        ("购车", "汽车"),
        ("新能源汽车", "汽车"),
        ("教培", "教育"),
        ("适老化改造", "适老"),
        ("成品油", "加油"),
        ("景区", "文旅"),
        ("电影票", "电影"),
        ("购物中心", "百货"),
        ("民宿", "住宿"),
        ("健身卡", "体育"),
        ("买书", "图书"),
        ("药店", "医药"),
        ("养老服务", "养老"),
        ("婴幼儿照护", "托育"),
        ("月嫂", "家政"),
    ],
)
async def test_known_phrasings_resolve(text: str, expected: str) -> None:
    enum = await _enum()
    assert _category_enum.resolve(enum, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("笔记本电脑", "电脑"),  # 命中「笔记本电脑」而不是「电脑」—— 同一个 id, 但取的是长的那个
        ("台式电脑", "电脑"),
        ("平板电脑", "平板"),  # 跨 id 的那一条: 平板赢
        ("智能手表手环", "手表"),
        ("平板pad", "平板"),
        ("电热水器", "热水器"),
    ],
)
async def test_longest_match_wins_the_way_the_shipped_matcher_does(text: str, expected: str) -> None:
    """最长命中 —— 与已上线的 ``match_category`` 同一套语义, 不另起一套归一规则。"""
    enum = await _enum()
    assert _category_enum.resolve(enum, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "电视柜",
        "空调扇",
        "手机壳",
        "洗衣机罩",
        "说明书",  # 「书」若当别名, 这里就会误判成图书
        "证书",
        "自行车",  # 「车」若当别名, 这里就会误判成汽车
        "玩具车",
        "数据线",
        "耳机",
        "宽带",
        "",
    ],
)
async def test_unknown_phrasings_are_reported_not_guessed(text: str) -> None:
    """**认不出就报不认识**, 不返回最近的那个 id。"""
    enum = await _enum()
    assert _category_enum.resolve(enum, text) is None

    detail = _category_enum.resolve_in_detail(enum, text, GUBOU)
    assert detail["ok"] is False
    assert detail["reason"] in {"unknown_category", "empty_input"}
    # 不许给「你是不是想说 X」那种候选: 猜品类会连带猜档位, 而档位错就是算错钱。
    assert "category" not in detail
    assert not [k for k in detail if "suggest" in k or "candidate" in k or "did_you_mean" in k]


async def test_resolve_details_say_which_alias_matched() -> None:
    enum = await _enum()
    detail = _category_enum.resolve_detail(enum, "电冰箱")
    assert detail["ok"] is True
    assert detail["category"] == "冰箱"
    assert detail["matched"] == "电冰箱"
    assert detail["matched_by"] == "alias"

    literal = _category_enum.resolve_detail(enum, "冰箱")
    assert literal["matched_by"] == "id"


# --------------------------------------------------------------------------- #
# 场景子集: 国补
# --------------------------------------------------------------------------- #


async def test_guobu_subset_is_exactly_the_fact_card_categories() -> None:
    """国补子集的 id 必须与资料卡的 categories **逐条相等** —— 少一个多一个都红。

    这是「品类清单在卡里、口径在枚举里」这条分工的接缝: 任何一边单独加品类都会被
    这条挡住, 而不是等线上映射不出品类才发现。
    """
    enum = await _enum()
    subset = _category_enum.scenario_ids(enum, GUBOU)
    assert set(subset) == set(_guobu_ids())
    assert len(subset) == 10


async def test_guobu_is_six_home_appliances_plus_four_digital() -> None:
    """国补 10 个齐全, 且 6 + 4 的分法是国补那个分法。"""
    enum = await _enum()
    home = {c for c in _category_enum.scenario_ids(enum, GUBOU) if _category_enum.tier_of(enum, GUBOU, c) == "家电"}
    digital = {c for c in _category_enum.scenario_ids(enum, GUBOU) if _category_enum.tier_of(enum, GUBOU, c) == "数码"}
    assert home == {"空调", "冰箱", "洗衣机", "电视", "热水器", "电脑"}
    assert digital == {"手机", "平板", "手表", "眼镜"}
    assert home | digital == set(_category_enum.scenario_ids(enum, GUBOU))
    assert not home & digital


async def test_guobu_tier_per_category_matches_the_fact_card() -> None:
    """档位**只在场景子集里**, 且必须与资料卡一致 —— 不一致就是两处各说各话。"""
    enum = await _enum()
    card = _card()
    for category_id in _category_enum.scenario_ids(enum, GUBOU):
        assert _category_enum.tier_of(enum, GUBOU, category_id) == card["categories"][category_id]["tier"]


def _guobu_aliases() -> tuple[str, ...]:
    """国补 10 个 id 的全部别名(含 id 本身), 取自**枚举** —— 代码里那一份已经删了。"""
    enum = _enum_file()
    return tuple(
        dict.fromkeys(alias for category_id in _guobu_ids() for alias in _category_enum.aliases_of(enum, category_id))
    )


#: **切换运行时来源之前**录下的逐词结果 —— 由已上线的 ``_guobu_categories.match_category``
#: (那份硬编码的 ``ALIASES`` 还在的时候) 在资料卡的 10 个品类上逐个跑出来, 不是照着新实现倒推的。
#: 顺序反了就等于自己给自己判卷, 那样这份冻结毫无意义。
PRE_SWITCH_MATCHES: dict[str, str | None] = {
    "": None,
    "   ": None,
    " 冰箱 ": "冰箱",
    "MacBook Pro": None,
    "iPad": None,
    "iPhone 17": None,
    "ipad": "平板",
    "pad": "平板",
    "一体机": "电脑",
    "冰箱": "冰箱",
    "冰箱 ": "冰箱",
    "医药": None,
    "台式机": "电脑",
    "台式电脑": "电脑",
    "商超": None,
    "图书": None,
    "家政": None,
    "家电": None,
    "宽带": None,
    "平板": "平板",
    "平板pad": "平板",
    "平板电脑": "平板",
    "手机": "手机",
    "手机壳": None,
    "手环": "手表",
    "手表": "手表",
    "挂机": "空调",
    "教育": None,
    "数据线": None,
    "智能手机": "手机",
    "智能手环": "手表",
    "智能手表": "手表",
    "智能手表手环": "手表",
    "智能电视": "电视",
    "智能眼镜": "眼镜",
    "柜机": "空调",
    "汽车": None,
    "洗衣机": "洗衣机",
    "洗衣机罩": None,
    "游戏本": "电脑",
    "滚筒洗衣机": "洗衣机",
    "热水器": "热水器",
    "燃气热水器": "热水器",
    "玩具车": None,
    "电冰箱": "冰箱",
    "电热水器": "热水器",
    "电脑": "电脑",
    "电视": "电视",
    "电视机": "电视",
    "电视柜": None,
    "眼镜": "眼镜",
    "空调": "空调",
    "空调扇": None,
    "空调机": "空调",
    "笔记本": "电脑",
    "笔记本电脑": "电脑",
    "耳机": None,
    "自行车": None,
    "证书": None,
    "说明书": None,
    "适老": None,
    "餐饮": None,
}


@pytest.mark.parametrize(("text", "expected"), sorted(PRE_SWITCH_MATCHES.items()))
def test_the_shipped_matcher_still_answers_exactly_as_before_the_switch(text: str, expected: str | None) -> None:
    """**本次改动的核心判据**: 换取值来源是内部整理, 不是行为变化。

    ``match_category`` 现在一个别名都不持有, 全部经 ``_category_enum.match_among`` 读
    ``sources/category-enum.yaml``。这条把切换前的每个词逐个钉住, 包括认不出的那些 ——
    ``None`` 也是一条答案, 而且是最要紧的那条: 归错品类会连带归错档位。
    """
    assert _guobu_categories.match_category(text, _fact_cards.category_names(_card())) == expected


def test_guobu_categories_holds_no_second_alias_table() -> None:
    """别名表只剩枚举那一份 —— ``_guobu_categories`` 里那份硬编码的**必须不在**。

    留着它就是留一份「运行期不读、判据却在盯着」的数据: 改了它不会影响任何答案, 于是
    两边可以各说各话而没人发现 —— 那正是这次改动要消掉的病。加回来等于把分叉重建一次。
    """
    assert not hasattr(_guobu_categories, "ALIASES")


@pytest.mark.parametrize("alias", _guobu_aliases())
async def test_resolve_agrees_with_the_shipped_matcher_on_every_guobu_alias(alias: str) -> None:
    """同一个词在两套归一里必须得到同一个 id。两套归一规则并存时,
    哪一个对取决于谁先被调用 —— 那是最难查的一类不一致。"""
    enum = await _enum()
    shipped = _guobu_categories.match_category(alias, _fact_cards.category_names(_card()))
    assert shipped is not None
    assert _category_enum.resolve(enum, alias) == shipped


@pytest.mark.parametrize(
    ("text", "candidates", "expected"),
    [
        # 候选集只有「电脑」时, 「平板电脑」以「电脑」结尾 -> 切换前给的是电脑, 现在仍然是。
        # 「先按全表归一再看在不在候选里」会给出平板(最长命中), 而平板不在候选里 -> None。
        # 差别只在候选集不是全表时才显形, 而 saving_calc 的 card 是参数, 会变。
        ("平板电脑", ("电脑",), "电脑"),
        ("平板电脑", ("平板",), "平板"),
        ("平板电脑", ("电脑", "平板"), "平板"),
        ("笔记本电脑", ("电脑", "平板"), "电脑"),
        ("电热水器", ("热水器",), "热水器"),
        ("智能手表手环", ("手表",), "手表"),
        # 候选里出现**枚举没有**的 id 时只按名字本身命中 —— 切换前就是这样 (旧表里没有的品类
        # 也只能靠名字命中), 不是漏了一条别名。
        ("空气炸锅", ("空气炸锅",), "空气炸锅"),
        ("炸锅", ("空气炸锅",), None),
        # 候选里出现**枚举有、旧别名表没有**的 id (国补 10 个之外那 17 个) 时, 现在享用本表的别名。
        # 这是切换唯一会改变结果的一类输入, 方向单一(只会多认), 且今天到不了 ——
        # 仓库里唯一的资料卡登记的正是旧表那 10 个。见 decisions.runtime_switched_to_the_enum。
        ("乘用车", ("汽车",), "汽车"),
        ("书籍", ("图书",), "图书"),
    ],
)
def test_candidates_gate_the_answer_instead_of_filtering_a_global_match(
    text: str, candidates: tuple[str, ...], expected: str | None
) -> None:
    """候选集是归一的**取值范围**, 不是给全表结果做的一道筛子。

    这两件事看着只差一个实现顺序, 结果却不同, 而差别只在候选集不是全表时才显形 ——
    ``saving_calc`` 的 ``card`` 是参数, 候选集本来就会变。所以这条线得钉住, 不能顺手改成
    「全表归一再筛」。
    """
    assert _guobu_categories.match_category(text, candidates) == expected


def test_editing_the_enum_moves_the_shipped_matcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**换过来源的实证判据**: 改数据文件, ``match_category`` 的答案必须跟着改。

    只比「切换前后结果相同」抓不出「其实还在读代码里那份、只是恰好一样」—— 那条只有把表
    改掉才显形 (与 ``test_guobu_fact_card.py`` 里「改卡两个工具一起动」同源)。
    """
    names = ("空气炸锅", *_fact_cards.category_names(_card()))
    assert _guobu_categories.match_category("炸锅", names) is None  # 表里没有这个品类, 认不出

    enum = _enum_file()
    enum["categories"]["空气炸锅"] = {"display": "空气炸锅", "kind": "实物", "aliases": ["空气炸锅", "炸锅"]}
    edited = tmp_path / "sources"
    edited.mkdir()
    (edited / "category-enum.yaml").write_text(yaml.safe_dump(enum, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(_category_enum, "_roots", lambda: [tmp_path])

    assert _guobu_categories.match_category("炸锅", names) == "空气炸锅"
    # 认得出是认得出, 但资料卡没登记 -> 照样取不到值: 候选集说了算, 别名表不越权
    assert _guobu_categories.match_category("炸锅", _fact_cards.category_names(_card())) is None


async def test_editing_the_enum_moves_the_shipped_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一条线走到底: 改数据文件 -> 已上线工具 ``subsidy_calc`` 的答案跟着改。

    上一条证的是 ``match_category`` 读枚举, 这条证的是**整条链**都在这张表上
    (工具 -> match_category -> 枚举) —— 也就是这次改动要办的那件事本身。

    挑的词是「电冰柜」而不是「电冰箱」那类: 后者以品类名「冰箱」结尾, 本来就命中,
    删不删别名都测不出东西 —— 拿这种词当探针, 只会得到一个假的绿灯。
    """
    before = json.loads(await subsidy_calc.subsidy_calc(price=3000, category="电冰柜", energy_level="一级"))
    assert before["ok"] is False
    assert before["suggest_search"] is True  # 改表前: 认不出, 要求去检索官方源

    enum = _enum_file()
    enum["categories"]["冰箱"]["aliases"] = ["冰箱", "电冰箱", "电冰柜"]
    edited = tmp_path / "sources"
    edited.mkdir()
    (edited / "category-enum.yaml").write_text(yaml.safe_dump(enum, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(_category_enum, "_roots", lambda: [tmp_path])

    after = json.loads(await subsidy_calc.subsidy_calc(price=3000, category="电冰柜", energy_level="一级"))
    assert after["ok"] is True
    assert after["kind"] == "冰箱"
    assert after["补贴"] == 450.0  # 走的是冰箱那套参数 (15% / 1500)


async def test_matching_rules_are_a_recorded_ruling_and_hold_in_practice() -> None:
    """匹配规则本身也是一条口径: 后缀 + 最长命中, 大小写不归一。

    最后那句不是「顺手钉一个 bug」, 而是**钉住两边一致**: 今天 ``match_category`` 是大小写敏感的
    (``iPad`` 认不出, 已记在 ``test_guobu_known_failures.py`` 的 xfail 探测器里), 这里就必须
    与它同款 —— 只在一边加 casefold 会让同一个词在两条链路上归一到不同结果。
    将来两边一起修好时, 这条判据**照样成立** (它比的是两者相等, 不是一个写死的值)。
    """
    enum = await _enum()
    ruling = _category_enum.decision(enum, "matching_is_suffix_and_case_sensitive")
    assert "最长命中" in str(ruling["ruling"])

    names = _fact_cards.category_names(_card())
    for probe in ("iPad", "ipad", "iphone", "冰箱 "):
        assert _category_enum.resolve(enum, probe) == _guobu_categories.match_category(probe, names)


# --------------------------------------------------------------------------- #
# 场景子集: 地方消费券
# --------------------------------------------------------------------------- #


async def test_local_voucher_subset_is_exactly_its_evidence() -> None:
    """子集 = 实测计数的那几个 + 另见到的那些, **不许是手抄的第三份名单**。"""
    enum = await _enum()
    block = _enum_file()["scenarios"][LOCAL_VOUCHER]
    subset = set(_category_enum.scenario_ids(enum, LOCAL_VOUCHER))
    assert subset == set(block["observed_counts"]) | set(block["seen_elsewhere"])
    assert subset == {
        "汽车",
        "餐饮",
        "加油",
        "文旅",
        "电影",
        "百货",
        "家电",
        "商超",
        "住宿",
        "体育",
        "图书",
        "医药",
        "养老",
        "托育",
        "家政",
    }


async def test_local_voucher_subset_has_no_tiers() -> None:
    """地方券没有国补那种档位 —— 有档位就等于凭空造了一套不存在的政策参数。"""
    enum = await _enum()
    for category_id in _category_enum.scenario_ids(enum, LOCAL_VOUCHER):
        assert _category_enum.tier_of(enum, LOCAL_VOUCHER, category_id) is None


async def test_every_scenario_member_is_a_master_enum_id() -> None:
    """子集只能从总枚举里挑, 不能自立门户。"""
    enum = await _enum()
    ids = set(_category_enum.canonical_ids(enum))
    for scenario in (GUBOU, LOCAL_VOUCHER):
        assert set(_category_enum.scenario_ids(enum, scenario)) <= ids


# --------------------------------------------------------------------------- #
# 裁决: 电脑归家电
# --------------------------------------------------------------------------- #


async def test_the_computer_ruling_is_recorded_with_its_reason() -> None:
    """裁决必须**连理由一起**写在数据文件里, 而不是只留一个结论。

    只留结论的话, 下一个人看到「国产常识里电脑是数码」就会当成手滑改回去 ——
    而这一改会换掉整档参数 (1500/1 级能效 -> 500/6000 门槛)。
    """
    enum = await _enum()
    ruling = _category_enum.decision(enum, "computer_tier")
    text = " ".join(str(ruling[k]) for k in ("question", "ruling", "because", "consequence"))
    assert "电脑" in text
    assert "家电" in text
    assert "数码" in text  # 反方也要写进来, 否则后来的人会重新提一遍
    # 理由必须落到**参数不同**这个事实上, 不能只是「文件这么写的」
    assert "1500" in text
    assert "500" in text
    assert "6000" in text
    assert ruling.get("evidence")


async def test_computer_tier_in_the_subset_matches_the_ruling() -> None:
    """裁决说的与子集里写的是同一件事 —— 文档与数据不许各说各话。"""
    enum = await _enum()
    assert _category_enum.tier_of(enum, GUBOU, "电脑") == "家电"
    assert _category_enum.decision(enum, "computer_tier")["ruling"].find("归家电") >= 0


async def test_coarse_home_appliance_is_legal_but_not_a_guobu_category() -> None:
    """「家电」是合法品类 (地方券按它发), 但国补不认它 —— 两种「不行」必须分开报。

    折成 6 类里的任意一类, 就是**在没有证据的情况下替用户挑了一个档**:
    拿空调的能效门和上限去算一台洗衣机。
    """
    enum = await _enum()
    assert _category_enum.resolve(enum, "家电") == "家电"
    assert "家电" not in _category_enum.scenario_ids(enum, GUBOU)
    assert "家电" in _category_enum.scenario_ids(enum, LOCAL_VOUCHER)

    detail = _category_enum.resolve_in_detail(enum, "家电", GUBOU)
    assert detail["ok"] is False
    assert detail["reason"] == "not_in_scenario"  # 与「整个不认识」分开
    assert detail["category"] == "家电"  # 但认得出它是什么
    assert _category_enum.resolve_in(enum, "家电", GUBOU) is None


async def test_a_legal_category_with_no_scenario_is_still_legal() -> None:
    """「教育」「适老」: 本体文档列了它们, 本轮实测没见到券 —— 留在总枚举, 不进任何场景子集。

    进了地方券子集就等于宣称它们有券源, 那是没证据的断言 (A2 纪律: 抓不到不等于没有;
    反过来, 没抓到也不能写成有)。**全表就这两个是这种状态** —— 多一个都要有说法。
    """
    enum = await _enum()
    assert _category_enum.resolve(enum, "教育") == "教育"
    assert _category_enum.resolve(enum, "适老") == "适老"
    unclaimed = [c for c in _category_enum.canonical_ids(enum) if not _category_enum.scenarios_of(enum, c)]
    assert set(unclaimed) == {"教育", "适老"}

    # 这两个「没场景认领」的原因也得写在文件里, 否则看起来像漏填了。
    text = " ".join(str(_category_enum.decision(enum, "ontology_only_ids")[k]) for k in ("question", "because"))
    assert "教育" in text
    assert "适老" in text


async def test_decisions_are_unique_and_complete() -> None:
    enum = await _enum()
    items = _category_enum.decisions(enum)
    assert items
    ids = [str(x.get("id") or "") for x in items]
    assert len(ids) == len(set(ids))
    for item in items:
        for field in ("question", "ruling", "because", "consequence"):
            assert item.get(field), f"裁决 {item.get('id')} 缺少 {field}"


# --------------------------------------------------------------------------- #
# 加载器本身
# --------------------------------------------------------------------------- #


def test_enum_root_is_the_pack_that_defines_the_tool() -> None:
    """数据根必须是**定义这个模块的那个包**, 不是 ``sys.path`` 上先出现的那个包。

    两个能力包各有一份 ``tools/_runtime_paths.py``, 靠裸名 import 时谁先谁赢;
    全量跑测试时两个包的 tools 目录同时在场, 用 ``agent_dir()`` 会指到 feishu 包。
    """
    assert _category_enum._roots() == [WORKSPACE_ROOT]
    assert ENUM_PATH.is_file()


async def test_loading_a_missing_enum_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """表丢了要报错, 不能静默回落到「一个品类都不认识」——
    那种失败会变成一句平静的「这个品类不在国补范围内」。"""
    monkeypatch.setattr(_category_enum, "_roots", lambda: [tmp_path])
    with pytest.raises(FileNotFoundError):
        await _category_enum.load_enum()


async def test_editing_the_file_moves_the_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """改数据文件即改行为, 不用重启进程 —— 缓存按 ``(mtime_ns, size)`` 失效。"""
    enum = _enum_file()
    enum["categories"]["空气炸锅"] = {
        "display": "空气炸锅",
        "kind": "实物",
        "aliases": ["空气炸锅", "炸锅"],
    }
    enum["scenarios"][LOCAL_VOUCHER]["categories"]["空气炸锅"] = {}
    edited = tmp_path / "sources"
    edited.mkdir()
    (edited / "category-enum.yaml").write_text(yaml.safe_dump(enum, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(_category_enum, "_roots", lambda: [tmp_path])

    loaded = await _category_enum.load_enum()
    assert _category_enum.resolve(loaded, "炸锅") == "空气炸锅"
    assert "空气炸锅" in _category_enum.scenario_ids(loaded, LOCAL_VOUCHER)
    assert "空气炸锅" not in _category_enum.scenario_ids(loaded, GUBOU)


async def test_editing_the_file_in_place_moves_both_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同步与异步两条入口都按 ``(mtime_ns, size)`` 失效, 且**共用同一份缓存**。

    与上一条互补: 上一条换的是**路径**(tmp 根), 走不到失效逻辑; 这条原地改**同一个文件**,
    考的才是缓存判据本身。多一个 id 会让文件变大, 所以 size 一定变 —— 不靠 mtime 的精度,
    这条判据在 mtime 分辨率粗的文件系统上也是确定的。

    同步入口是运行期真正走的那条(``match_category``), 它要是不过期, 表现就是
    「改了表, 工具还按旧表答」—— 而那种错只有改了表的人看得见。
    """
    sources = tmp_path / "sources"
    sources.mkdir()
    path = sources / "category-enum.yaml"
    path.write_text(yaml.safe_dump(_enum_file(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(_category_enum, "_roots", lambda: [tmp_path])

    names = ("空气炸锅", *_fact_cards.category_names(_card()))
    assert _guobu_categories.match_category("炸锅", names) is None  # 第一版表里没有它
    assert _category_enum.resolve(await _category_enum.load_enum(), "炸锅") is None

    edited = _enum_file()
    edited["categories"]["空气炸锅"] = {"display": "空气炸锅", "kind": "实物", "aliases": ["空气炸锅", "炸锅"]}
    path.write_text(yaml.safe_dump(edited, allow_unicode=True), encoding="utf-8")

    assert _guobu_categories.match_category("炸锅", names) == "空气炸锅"  # 同步入口跟上了
    # 异步入口读的是同一份表, 不是各自缓存的一份副本
    assert _category_enum.resolve(await _category_enum.load_enum(), "炸锅") == "空气炸锅"
