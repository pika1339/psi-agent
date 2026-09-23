"""台账的日期区间读取必须真的能读到记录。

2026-09-22 的实机发现(本地海豚二号跑"出一份 9 月上半月汇总报告"): 台账专用读工具
**带任何条件都失败**, 于是报告类请求拿不到一条记录。

两个缺陷叠在一起, 各自都不报错:

1. **列名对不上**: 工具认的是 `occurred_from` / `occurred_to`, 而模型几乎必然按直觉写
   `date_from` / `date_to`; `parse_query` 直接抛 `unknown query fields`, 表现为
   「查询条件无法解析」—— 用户/模型都无从知道正确的字段名。
2. **区间算子对日期字段无效**: `occurred_from` 走的是 `isGreaterEqual`, 而飞书**日期字段
   不接受** `isGreaterEqual` / `isLessEqual` —— 它接受请求然后**一行都不匹配**,
   于是区间查询安静地返回"一条都没有"。日期的区间只能用 `isGreater` / `isLess` 配
   `["ExactDate", "<毫秒>"]` 操作数。

第三条同源于第一处的**组合**故障: 默认 `view_id` 与任何 filter 一起发出去时, 专用搜索
实现会直接拒绝(`view_id cannot be combined with filter_json`) —— 即"带条件的读取必然失败"。
所以带条件时读工具不再带视图, 读的是全表(与汇总工具的范围一致)。

这些用例钉住三件事: 直觉字段名可用、区间用日期字段真接受的算子、带条件时不带视图。
判据不碰网络 —— 只查生成的 filter JSON 与解析结果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from _positive_negative_list import reader  # noqa: E402  # ty: ignore[unresolved-import]

#: 飞书日期字段**不接受**的两个算子 —— 用了它们请求会成功、匹配为空。
_DATE_UNSUPPORTED_OPERATORS = ("isGreaterEqual", "isLessEqual")


def _conditions(query_json: str) -> list[dict]:
    query = reader.parse_query(query_json)
    built = reader.build_filter(query)
    assert built, "条件齐全时不该生成空 filter"
    return json.loads(built)["conditions"]


def test_the_intuitive_date_field_names_are_accepted() -> None:
    """模型写 `date_from`/`date_to` 是常态 —— 直接可用, 不要求它先读源码。"""
    query = reader.parse_query('{"date_from": "2026-09-01", "date_to": "2026-09-15"}')

    assert query.occurred_from == "2026-09-01"
    assert query.occurred_to == "2026-09-15"


def test_an_unknown_field_name_is_still_rejected() -> None:
    """别名只补这一组; 真拼错的字段仍要报错, 不能悄悄忽略。"""
    with pytest.raises(ValueError, match="unknown query fields"):
        reader.parse_query('{"date_fromm": "2026-09-01"}')


def test_the_date_range_uses_operators_the_date_field_accepts() -> None:
    conditions = _conditions('{"occurred_from": "2026-09-01", "occurred_to": "2026-09-15"}')
    date_conditions = [c for c in conditions if c["field_name"] == reader._FIELD_NAMES["occurred_at"]]

    assert len(date_conditions) == 2, date_conditions
    for condition in date_conditions:
        assert condition["operator"] not in _DATE_UNSUPPORTED_OPERATORS, condition
        assert condition["value"][0] == "ExactDate", condition
        assert str(condition["value"][1]).isdigit(), condition


def test_equal_bounds_cover_exactly_that_one_day() -> None:
    """闭区间两端相等 = 只那一天: 下界退一天、上界进一天, 两侧都是严格不等。"""
    conditions = _conditions('{"occurred_from": "2026-09-07", "occurred_to": "2026-09-07"}')
    lower = next(c for c in conditions if c["operator"] == "isGreater")
    upper = next(c for c in conditions if c["operator"] == "isLess")

    assert int(upper["value"][1]) - int(lower["value"][1]) == 2 * 86_400_000


def test_a_bad_date_operand_says_what_it_wants() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _conditions('{"occurred_from": "上周"}')
