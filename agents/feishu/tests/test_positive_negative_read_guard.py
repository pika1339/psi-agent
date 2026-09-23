"""台账原始读法只有专用工具一条路 —— 绕过它就没有认知口径。

2026-09-22 那次正负面清单报告: 模型用 `feishu_api` 把台账拉出来, 自己写 `python_run` 聚合,
再自己写 Word。数字全对, 而 `positive_negative_rules` / `positive_negative_case_analyze`
一次都没调 —— 于是整份认知口径丢失, 报告写成「只红不绿」「负面反超」的对抗叙事, 44 条负面
一条改正路径都没有。这正是 08-22 全员会明令不许的读法。

所以这条读法**在代码里被封**: 碰台账坐标的记录读一律拒绝并点名该用哪个工具。提示词里写一句
"先调 analyze" 只对愿意照做的回合有效(软约束), 而这里的拒绝是确定性的。

这些用例钉住四件事:
1. 走 `feishu_api` 读台账记录 → 拒绝, 且**根本没发出请求**;
2. 走专用 bitable 工具读同一张表 → 同样拒绝(它不经过 `feishu_api`, 是第二个门);
3. 字段元数据 / 别的表 / 写入 → 不受影响(拒绝必须窄, 否则是拿新缺陷换旧缺陷);
4. 专用读工具的返回里必须带认知口径 —— 否则"改走专用工具"只是换个地方丢口径。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_impl: Any = importlib.import_module("_feishu_impl")
_api: Any = importlib.import_module("_feishu_api_impl")
_bitable: Any = importlib.import_module("feishu_bitable")
_runtime: Any = importlib.import_module("_positive_negative_list.runtime")

LEDGER_APP, LEDGER_TABLE = _runtime.read_target_coordinates()
RECORDS_URI = "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records"
FIELDS_URI = "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields"


class _NeverCalled:
    """``_invoke`` stand-in: any network attempt is a test failure, not a slow test."""

    async def __call__(self, request: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("请求被发出去了 —— 这道闸门必须在网络之前拦住")


@pytest.mark.asyncio
async def test_reading_the_ledger_through_feishu_api_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_impl, "_invoke", _NeverCalled())

    res = await _api.call_api_impl(
        method="GET",
        uri=RECORDS_URI,
        paths_json=json.dumps({"app_token": LEDGER_APP, "table_id": LEDGER_TABLE}),
    )

    assert res["ok"] is False
    assert res["code"] == "use_dedicated_tool"
    assert "positive_negative_case_read" in res["message"]
    assert "positive_negative_case_analyze" in res["message"]


@pytest.mark.asyncio
async def test_field_metadata_is_still_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """写前预检要读列名 —— 那是允许的, 且技能里写明允许。"""
    calls: list[Any] = []

    class _Captured:
        async def __call__(self, request: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(request)
            return {"ok": True, "data": {}}

    monkeypatch.setattr(_impl, "_invoke", _Captured())
    res = await _api.call_api_impl(
        method="GET",
        uri=FIELDS_URI,
        paths_json=json.dumps({"app_token": LEDGER_APP, "table_id": LEDGER_TABLE}),
    )

    assert res.get("ok") is True, res
    assert calls, "字段元数据没有被放行"


@pytest.mark.asyncio
async def test_another_tables_records_are_not_affected(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝只针对台账坐标。同库的其它表(如战争版)与任何别的表照常可读。"""
    calls: list[Any] = []

    class _Captured:
        async def __call__(self, request: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(request)
            return {"ok": True, "data": {}}

    monkeypatch.setattr(_impl, "_invoke", _Captured())
    res = await _api.call_api_impl(
        method="GET",
        uri=RECORDS_URI,
        paths_json=json.dumps({"app_token": LEDGER_APP, "table_id": "tblbF6ZVQbNTNxxn"}),
    )

    assert res.get("ok") is True, res
    assert calls, "非台账表的读被误拦"


@pytest.mark.asyncio
async def test_dedicated_bitable_search_is_refused_too() -> None:
    """第二个门: 专用搜索工具不经过 `feishu_api`, 同一张表必须同样拒绝。"""
    raw = await _bitable.feishu_bitable_search_records(app_token=LEDGER_APP, table_id=LEDGER_TABLE)
    res = json.loads(raw)

    assert res["ok"] is False
    assert res["code"] == "use_dedicated_tool"


def test_the_aggregation_tool_is_where_the_cognition_rides() -> None:
    """被指到的那个工具必须真的带认知口径 —— 否则"改走专用工具"只是换个地方丢口径。

    这条读的是源码契约而不是运行时返回: 台账读要凭据, 而口径必须在场的判据跟凭据无关。
    原始读(case_read)**故意**不带口径(它就是原始明细), 所以"汇总必须有口径"这条只对
    analyze 成立 —— 这正是报告该走 analyze、而不是自己聚合 case_read 的理由。
    """
    analyze_src = (TOOLS_DIR / "positive_negative_case_analyze.py").read_text(encoding="utf-8")
    assert "load_cognition_pack().report_view()" in analyze_src, "汇总工具不再附带认知口径"


def test_the_guard_names_the_tool_that_replaces_it() -> None:
    refusal = _api.ledger_guard_refusal(endpoint=RECORDS_URI)

    assert refusal["code"] == "use_dedicated_tool"
    assert refusal["tool"].startswith("positive_negative_case_read")
    assert "字段元数据" in refusal["message"]
