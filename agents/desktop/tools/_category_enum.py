"""品类枚举加载器 —— 「总枚举 + 场景子集」的唯一入口。

数据住 ``<agent>/sources/category-enum.yaml``。本模块是它的唯一入口:
**别处不得再留第二份品类清单** —— 那正是这张表要消掉的病 (《省钱场景交接》§7 坑 1
「参数两处重复, 改一处必改另一处」)。

三件事在这里, 别的地方都不做:

1. **别名 -> 规范 id** (``resolve``): 用户说法归一成枚举里的 id。
2. **场景子集** (``scenario_ids`` / ``tier_of``): 「这个场景认哪些 id」「在这个场景里它属于哪一档」。
   档位**不在总枚举里** —— 见数据文件 decisions.computer_tier。
3. **明确报「不认识」** (``resolve_detail``): 认不出时返回带 ``reason`` 的结构,
   **不返回最近的那个 id**。猜一个最像的品类, 在这条链路上等于拿另一套政策参数去算钱。

## 与已上线工具的关系: 运行期已经切到本表

- **``subsidy_calc`` / ``policy_query`` / ``saving_calc`` 经本表取值**。它们仍调
  ``_guobu_categories.match_category``, 但那个函数现在**只做委托** ——
  别名一个都不留在自己身上 (原先那份硬编码的 ``ALIASES`` 已删)。
  见数据文件 decisions.runtime_switched_to_the_enum。
- **切换没有改判定语义**: ``match_among`` 逐字复刻已上线那条规则 (完全相等或以别名结尾,
  取最长命中), 且**候选集仍是调用方给的品类名集合** —— 不是「先按全表归一再看在不在集合里」。
  这两条在受限子集上**不等价**: 资料卡只登记了「电脑」时, ``match_category("平板电脑", ...)``
  的旧行为是给「电脑」(「平板电脑」以「电脑」结尾); 先按全表归一会给「平板」, 而它不在候选里,
  于是变成 ``None``。差别只在候选集不是全表时才显形, 而 ``saving_calc`` 的卡是参数, 会变。
- **有一条同步入口** (``load_enum_sync``): ``match_category`` 是同步函数, 调它的三个工具不会
  await 它。把 ``match_category`` 改成 async 要动三个已上线工具, 而这次改动的目的只是
  「别再有两份别名表」—— 不值得把调用面一起翻掉。同步读文件在本仓库有先例
  (``_mcp._load_cached_schemas`` / ``search._load_env``)。
- **缓存按 ``(mtime_ns, size)`` 失效**, 与 ``_fact_cards`` / ``_voucher_sources`` 同一套期望:
  改完数据文件不用重启进程。这道失效判据必须自己写, 因为数据文件不在 ``tools/`` 下,
  内核不会替我们盯着它。**同步与异步两条入口共用同一个缓存**, 否则同一份表会被读两遍、
  在两边各自失效一次。**刻意没有把那段抽成公共函数** —— 那要动 ``_voucher_sources``
  (已在线上), 而「抽公共缓存」不该混在「定品类口径」这一次改动里。三处各写一遍是代价,
  换来的是这次改动碰不到任何已上线的取值路径。

## 参数顺序

本模块的函数一律 ``(enum, ...)`` —— enum 是表, 永远第一个参数。
``_guobu_categories.match_category(subject, categories)`` 是相反的顺序, 那是有历史原因的,
不要照抄到这里来。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import anyio
import yaml
from loguru import logger

_SOURCES_DIRNAME = "sources"
_FILE_NAME = "category-enum.yaml"

#: 类别标签的合法取值。数据文件里写别的 -> 当场报错, 不静默放行。
KINDS = ("实物", "服务")

# path -> ((mtime_ns, size), enum)
_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _roots() -> list[Path]:
    """能力包根 —— 就是本文件所在的那个包(理由见 ``_fact_cards._roots``)。

    刻意不走 ``_runtime_paths.agent_dir()``: 那个模块名在两个能力包里同名,
    裸名 import 时谁先落在 ``sys.path`` 上谁赢, 全量跑测试时数据文件会跑到别人家里去找。
    """
    return [Path(__file__).resolve().parents[1]]


def _cache_hit(path_str: str, signature: tuple[int, int]) -> dict[str, Any] | None:
    """命中返回那张表, 否则 ``None`` —— 同步与异步入口共用, 免得两边各判一套。"""
    hit = _cache.get(path_str)
    return hit[1] if hit is not None and hit[0] == signature else None


def _remember(path_str: str, signature: tuple[int, int], text: str) -> dict[str, Any]:
    """校验 + 入缓存。同步与异步入口共用 —— 校验只有一处, 两边不会各自放行不同的东西。"""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"品类枚举 {path_str} 顶层必须是 mapping, 实际是 {type(data).__name__}")
    _cache[path_str] = (signature, data)
    logger.debug(f"Category enum loaded: {path_str} ({len(text)} chars)")
    return data


def _not_found(tried: list[str]) -> FileNotFoundError:
    return FileNotFoundError(f"找不到品类枚举 {_FILE_NAME}; 已试: {tried}")


def load_enum_sync() -> dict[str, Any]:
    """读品类枚举的**同步**版本; 找不到抛 ``FileNotFoundError``。

    存在的理由只有一个: ``_guobu_categories.match_category`` 是同步函数, 而调它的
    ``subsidy_calc`` / ``policy_query`` / ``saving_calc`` 不会 await 它。把那条链改成
    async 要动三个已上线工具的签名, 而这次改动的目的只是「别再有两份别名表」。

    同步读文件在这个仓库有先例 (``_mcp._load_cached_schemas`` / ``search._load_env``);
    这里代价更小: 表很小, 且与 :func:`load_enum` 共用同一个 ``(mtime_ns, size)`` 缓存 ——
    首次调用之后每次只多一次 ``stat``, 读文件只在数据文件真的变了时发生。
    """
    tried: list[str] = []
    for root in _roots():
        path = root / _SOURCES_DIRNAME / _FILE_NAME
        path_str = str(path)
        try:
            stat = path.stat()
        except OSError:
            tried.append(path_str)
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        hit = _cache_hit(path_str, signature)
        if hit is not None:
            return hit
        return _remember(path_str, signature, path.read_text(encoding="utf-8"))
    raise _not_found(tried)


async def load_enum() -> dict[str, Any]:
    """读品类枚举(异步入口); 找不到抛 ``FileNotFoundError``。"""
    tried: list[str] = []
    for root in _roots():
        path_str = str(root / _SOURCES_DIRNAME / _FILE_NAME)
        try:
            stat = await anyio.Path(path_str).stat()
        except OSError:
            tried.append(path_str)
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        hit = _cache_hit(path_str, signature)
        if hit is not None:
            return hit
        text = await anyio.Path(path_str).read_text(encoding="utf-8")
        return _remember(path_str, signature, text)
    raise _not_found(tried)


def _categories(enum: dict[str, Any]) -> dict[str, Any]:
    cats = enum.get("categories")
    if not isinstance(cats, dict):
        raise ValueError("品类枚举缺少 categories")
    return cats


def canonical_ids(enum: dict[str, Any]) -> tuple[str, ...]:
    """总枚举的全部规范 id, **按数据文件里的顺序**。"""
    return tuple(_categories(enum))


def entry_of(enum: dict[str, Any], category_id: str) -> dict[str, Any]:
    """取一个规范 id 的条目; 不在总枚举里抛 ``KeyError``(不是返回空字典)。"""
    entry = _categories(enum).get(category_id)
    if not isinstance(entry, dict):
        raise KeyError(category_id)
    return entry


def display_of(enum: dict[str, Any], category_id: str) -> str:
    """展示名(给用户看的那句)。缺省回落成 id —— 缺字段只影响话术, 不影响判定。"""
    return str(entry_of(enum, category_id).get("display") or category_id)


def kind_of(enum: dict[str, Any], category_id: str) -> str:
    """类别(实物/服务)。取值不合法当场抛, 不静默放行 —— 这是数据写错, 不是输入写错。"""
    kind = str(entry_of(enum, category_id).get("kind") or "")
    if kind not in KINDS:
        raise ValueError(f"品类 {category_id!r} 的 kind 必须是 {KINDS} 之一, 实际是 {kind!r}")
    return kind


def aliases_of(enum: dict[str, Any], category_id: str) -> tuple[str, ...]:
    """一个 id 的别名表(含 id 本身)。"""
    raw = entry_of(enum, category_id).get("aliases")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"品类 {category_id!r} 的 aliases 必须是非空列表")
    return tuple(str(x) for x in raw)


def alias_index(enum: dict[str, Any]) -> dict[str, str]:
    """别名 -> 规范 id 的**平表**。别名冲突时保留先出现的那个 —— 冲突本身由判据拦,
    这里不抛, 免得一个数据笔误让整张表在运行期不可用。"""
    index: dict[str, str] = {}
    for category_id in canonical_ids(enum):
        for alias in aliases_of(enum, category_id):
            index.setdefault(alias, category_id)
    return index


def alias_conflicts(enum: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """别名 -> 认领它的全部 id; 只返回**不止一个**的那些(即真正的冲突)。

    判据用得上, 也让「别名不冲突」这条纪律有个可执行的定义:
    **同一个别名不能指向两个 id。**
    """
    owners: dict[str, list[str]] = {}
    for category_id in canonical_ids(enum):
        for alias in aliases_of(enum, category_id):
            owners.setdefault(alias, []).append(category_id)
    return {alias: tuple(ids) for alias, ids in owners.items() if len(ids) > 1}


def _match_all(enum: dict[str, Any], text: str) -> list[tuple[int, str, str]]:
    """返回全部命中 ``(别名长度, 规范 id, 命中的别名)``。

    **命中规则与已上线的 ``match_category`` 逐字相同**: 完全相等, 或以别名结尾。
    刻意不换一套更"聪明"的匹配 —— 两套归一规则并存时, 哪一个对取决于谁先被调用,
    那是最难查的一类不一致。反过来说, 这条规则也会误判 (「说明书」以「书」结尾),
    所以别名表里**不放**这种短词, 宁可报不认识。见数据文件里 汽车/图书 的 note。
    """
    if not text:
        return []
    hits: list[tuple[int, str, str]] = []
    for category_id in canonical_ids(enum):
        for alias in aliases_of(enum, category_id):
            if text == alias or text.endswith(alias):
                hits.append((len(alias), category_id, alias))
    return hits


def _pick(hits: list[tuple[int, str, str]]) -> tuple[str, str] | None:
    """取**最长命中** —— 这样「平板电脑」落到平板而不是电脑(与已上线行为一致)。

    同长时按数据文件顺序取第一个: 顺序确定, 结果就确定, 不会随 dict 迭代漂移。
    """
    if not hits:
        return None
    best = max(hits, key=lambda h: h[0])
    return best[1], best[2]


def resolve(enum: dict[str, Any], text: str) -> str | None:
    """用户说法 -> 规范 id; **认不出返回 ``None``**(不猜最近的那个)。"""
    picked = _pick(_match_all(enum, (text or "").strip()))
    return picked[0] if picked else None


def _candidates(enum: dict[str, Any], category_id: str) -> tuple[str, ...]:
    """一个候选 id 的命中词 = id 本身 + 本表登记的别名。

    两个方向刻意不同, 且都写在这里, 免得读的人以为漏了分支:

    - 候选是本表**没有**的 id -> 只有 id 本身。这是切换前的行为 (旧别名表里没有的品类,
      过去也只能靠名字本身命中), 也刻意不报错: 候选集来自资料卡, 资料卡完全可以先登记一个
      本表还没收录的品类, 那该由两张表之间的一致性判据去拦, 不该让归一当场炸;
    - 候选是本表**有**、但旧别名表里没有的 id (即国补那 10 个之外的 17 个) -> 用上本表的别名。
      这是切换**唯一**会改变结果的一类输入: ``match_category("乘用车", <候选含汽车>)``
      切换前是 ``None``, 现在是「汽车」。方向单一 (只会多认, 不会少认), 且今天到不了 ——
      仓库里唯一的资料卡登记的正是旧别名表那 10 个, 候选集伸不到别处。
      判据 ``test_candidates_gate_the_answer_instead_of_filtering_a_global_match`` 把这条钉住。
    """
    try:
        return (category_id, *aliases_of(enum, category_id))
    except KeyError:
        return (category_id,)


def match_among(enum: dict[str, Any], text: str, candidates: Iterable[str]) -> str | None:
    """在**给定的候选 id 集合**里归一; 命中不了返回 ``None``。

    *candidates* 是调用方认得的 id 集合 —— 工具侧就是 ``_fact_cards.category_names()``
    (资料卡登记了哪些品类, 就只能在哪些品类里取值)。**候选之外的 id 一律不参与**,
    即使它是总枚举里的合法 id: 资料卡没登记的品类查不到参数, 归一过去只会让下一步 KeyError。

    与 :func:`resolve` 的差别**只在候选集**, 匹配规则是同一套 (完全相等或以别名结尾,
    取最长命中) 且由同一个 :func:`_pick` 决定 —— 两套归一规则并存时, 哪个对取决于谁先被调用,
    那是最难查的一类不一致。

    ``candidates`` 的顺序参与判定: 同为最长命中时取先出现的那个, 与切换前的
    ``_guobu_categories.match_category`` 逐字相同。
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    hits: list[tuple[int, str, str]] = []
    for category_id in candidates:
        for alias in _candidates(enum, category_id):
            if stripped == alias or stripped.endswith(alias):
                hits.append((len(alias), category_id, alias))
    picked = _pick(hits)
    return picked[0] if picked else None


def resolve_detail(enum: dict[str, Any], text: str) -> dict[str, Any]:
    """同 :func:`resolve`, 但把「为什么」也说清楚 —— 认不出要能**明确报不认识**。

    上层拿到 ``ok=false`` 时应去追问用户或查官方源, **不得**自己挑一个 id 继续算。
    这里刻意不给「你是不是想说 X」那种候选: 品类猜错会连带档位猜错, 而档位错就是算错钱。
    """
    subject = (text or "").strip()
    if not subject:
        return {"ok": False, "reason": "empty_input", "input": "", "note": "没给品类, 什么都没法归一。"}
    picked = _pick(_match_all(enum, subject))
    if picked is None:
        return {
            "ok": False,
            "reason": "unknown_category",
            "input": subject,
            "known_count": len(canonical_ids(enum)),
            "known_ids": list(canonical_ids(enum)),
            "note": (
                "这个说法不在总枚举里。**不要猜最近的那个 id** —— 品类猜错会连带档位猜错, "
                "档位错就是算错钱。请追问用户, 或去官方源确认它到底属于哪一类。"
            ),
        }
    category_id, alias = picked
    return {
        "ok": True,
        "category": category_id,
        "input": subject,
        "matched": alias,
        "matched_by": "id" if alias == category_id else "alias",
        "display": display_of(enum, category_id),
        "kind": kind_of(enum, category_id),
        "scenarios": list(scenarios_of(enum, category_id)),
    }


def scenario_ids(enum: dict[str, Any], scenario: str) -> tuple[str, ...]:
    """某个场景子集里的全部 id(按数据文件顺序)。场景不存在抛 ``KeyError``。"""
    scenarios = enum.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("品类枚举缺少 scenarios")
    block = scenarios.get(scenario)
    if not isinstance(block, dict):
        raise KeyError(scenario)
    cats = block.get("categories")
    if not isinstance(cats, dict):
        raise ValueError(f"场景 {scenario!r} 缺少 categories")
    return tuple(cats)


def tier_of(enum: dict[str, Any], scenario: str, category_id: str) -> str | None:
    """某个 id **在这个场景里**属于哪一档; 该场景没给它档位(或根本不在子集里)返回 ``None``。

    档位只在这里 —— 总枚举不知道「电脑算什么档」, 因为它在不同场景里可以不同。
    """
    scenarios = enum.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("品类枚举缺少 scenarios")
    block = scenarios.get(scenario)
    if not isinstance(block, dict):
        raise KeyError(scenario)
    cats = block.get("categories")
    if not isinstance(cats, dict):
        raise ValueError(f"场景 {scenario!r} 缺少 categories")
    entry = cats.get(category_id)
    if not isinstance(entry, dict):
        return None
    tier = entry.get("tier")
    return str(tier) if tier else None


def scenarios_of(enum: dict[str, Any], category_id: str) -> tuple[str, ...]:
    """一个 id 被哪些场景子集收着(可能一个都没有 —— 「合法品类, 但还没有场景认领」)。"""
    scenarios = enum.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("品类枚举缺少 scenarios")
    return tuple(name for name in scenarios if category_id in scenario_ids(enum, str(name)))


def resolve_in(enum: dict[str, Any], text: str, scenario: str) -> str | None:
    """在**某个场景里**归一。认不出、或认出来但该场景不认这个 id, 都返回 ``None``。

    这两种「None」必须由 :func:`resolve_in_detail` 分开说 —— 区别在于下一步做什么:
    前者要追问「你说的是哪一类」, 后者要追问「这个具体是哪一件」(如国补侧收到「家电」)。
    """
    detail = resolve_in_detail(enum, text, scenario)
    return str(detail["category"]) if detail.get("ok") else None


def resolve_in_detail(enum: dict[str, Any], text: str, scenario: str) -> dict[str, Any]:
    """同 :func:`resolve_in`, 但把两种失败分开报。

    - ``unknown_category``   这个词整个不认识 -> 追问用户它是什么品类;
    - ``not_in_scenario``    是合法品类, 但这个场景不认 -> 追问更具体的那个(如国补的「家电」)。

    **两种情况都不得由调用方自己折成子集里的某个 id。** 那正是 decisions.coarse_home_appliance
    要拦的事: 把「家电」折成空调, 就是拿空调的能效门和 5000 元上限去算一台洗衣机。
    """
    base = resolve_detail(enum, text)
    if not base.get("ok"):
        return base
    category_id = str(base["category"])
    if category_id in scenario_ids(enum, scenario):
        base["scenario"] = scenario
        base["tier"] = tier_of(enum, scenario, category_id)
        return base
    return {
        "ok": False,
        "reason": "not_in_scenario",
        "input": base["input"],
        "category": category_id,
        "scenario": scenario,
        "scenario_ids": list(scenario_ids(enum, scenario)),
        "note": (
            f"{category_id} 是合法品类, 但 {scenario} 这个场景不认它。"
            "**不要折成这个场景里的某个 id** —— 每个 id 的参数边界不同, 折错就是算错钱。"
            "请追问用户更具体的那个说法。"
        ),
    }


def decisions(enum: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """口径裁决(每条含 id / question / ruling / because / consequence / evidence)。"""
    raw = enum.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("品类枚举缺少 decisions")
    return tuple(x for x in raw if isinstance(x, dict))


def decision(enum: dict[str, Any], decision_id: str) -> dict[str, Any]:
    """按 id 取一条裁决; 没有抛 ``KeyError``。"""
    for item in decisions(enum):
        if item.get("id") == decision_id:
            return item
    raise KeyError(decision_id)
