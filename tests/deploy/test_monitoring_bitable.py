"""多维表格(趋势表)的判据。

## 这一层唯一要守住的事: 「没量到」在表里是**空格子**

日报的数字全进一张表, 一天一行, 一指标一列。表的读法是**竖着看一列**, 所以一格 0 与一个
真实的低读数不可区分, 折线图还会把它画成一次暴跌。文字版至少还有「未测到」三个字。

所以本文件里大半判据在压同一件事的不同犯法方式: 传 0、传 None、传一个拼错的列名(那一列
从此永远空着, 于是一个拼写错误伪装成一个观测缺口)。

## 不 mock 掉 HTTP 的形状

`_call` 那层的坑是**飞书拿 HTTP 200 回业务错误**, `code` 在 body 里。所以假 opener 回的是
真的字节流和真的状态码, 判据打的是 `_call` 的解析逻辑本身 —— 把 `_call` 整个 mock 掉的话,
那个坑一条判据都碰不到。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

_MON = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "monitoring"


def _load(name: str) -> ModuleType:
    if str(_MON) not in sys.path:
        sys.path.insert(0, str(_MON))
    spec = importlib.util.spec_from_file_location(name, _MON / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


findings = _load("findings")
config = _load("config")
bitable = _load("bitable")


@dataclass
class FakeResp:
    body: bytes

    def read(self) -> bytes:
        return self.body


@dataclass
class FakeOpener:
    """按 URL 后缀回预设 body。记下每次请求的 (method, path, body)。

    默认对任何没预设的路径回 `{"code":0,"data":{}}` —— 这样判据只需声明它关心的那几个响应。
    """

    replies: dict[str, dict] = field(default_factory=dict)
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)
    #: 命中后抛 HTTPError 而不是正常返回 —— 验「HTTP 错误也要解析 body」。
    http_error_on: str = ""

    def open(self, req, timeout=None):
        path = req.full_url.split("/open-apis", 1)[-1]
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.calls.append((req.get_method(), path, body))
        payload: dict = {"code": 0, "data": {}}
        for key, value in self.replies.items():
            if key in path:
                payload = value
                break
        raw = json.dumps(payload).encode("utf-8")
        if self.http_error_on and self.http_error_on in path:
            # hdrs 传真的 `Message` 而不是 `{}` + 一条 ignore: `ty` 的诊断数是本仓闸门之一,
            # 而这里造一个空 Message 只多一行。
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", Message(), io.BytesIO(raw))
        return FakeResp(raw)


def _report(*items) -> object:
    report = findings.Report(tier="日报", timestamp="T")
    for item in items:
        report.add(item)
    return report


# ───────────────── 空格子 ─────────────────


def test_unmeasured_metric_omits_the_key_entirely():
    """没量到 → 那一列的 key **整个不出现**, 不是 None 也不是 0。

    三种写法在飞书那边是三个不同结果: 缺 key 留空格子(对), 传 None 也留空(实测 code 0, 但
    表达的是「我知道这格该有值而我给的是空」), 传 0 则是一个假读数。判据钉最严的那个 ——
    钉「不是 0」的话, 一个传 None 的实现照样全绿, 而 None 和缺 key 的区别正是以后有人「顺手
    补个默认值」时会踩的那一脚。
    """
    row = bitable.build_row(
        _report(findings.unknown("可用内存", reason="free 读不到", baseline=">= 1.5 GiB", direction="越低越糟")),
        day="2026-09-21",
    )
    assert "可用内存 GiB" not in row, f"没量到的列必须缺席, 实收 {row.get('可用内存 GiB')!r}"
    # 反向: 量到了就必须在。少了这一半, 一个「所有数字列都不写」的实现也能让上面那条通过。
    row2 = bitable.build_row(
        _report(
            findings.ok(
                "可用内存",
                value="2.6 GiB",
                baseline=">= 1.5 GiB",
                direction="越低越糟",
                num=2.6,
                num_warn=1.5,
                unit="GiB",
            )
        ),
        day="2026-09-21",
    )
    assert row2["可用内存 GiB"] == 2.6


def test_every_column_name_in_the_map_matches_a_real_metric_name():
    """映射表里的指标名必须**真的是** probes 里那个名字。

    拼错的后果不是报错, 是那一列永远空着 —— 而空着的语义是「没量到」。于是一个拼写错误会
    伪装成一个观测缺口, 每天都很像真的, 而且改了代码也不会有任何东西变红。

    所以这条判据把每个指标名都造一条 Finding 喂进去, 要求对应列真的拿到值。这比「照着
    probes 的源码 grep 一遍」强: grep 只能证明那个字符串在某处出现过。
    """
    for column, (metric, key, digits) in bitable.NUMERIC_COLUMNS.items():
        if metric == bitable._PEAK_MEM:
            continue  # 合成列, 由下一条判据覆盖
        extra = {key: 12.0} if key else {}
        # 单位取列名里最后那个词: 花费那一列有币种核对, 喂个占位单位会被判成「币种不符」而
        # 留空 —— 第一次跑就是这么红的, 那条守卫按设计工作了。
        item = findings.ok(
            metric,
            value="v",
            baseline="b",
            direction="d",
            num=34.0,
            num_warn=99.0,
            unit=column.split()[-1],
            extra_nums=extra,
        )
        row = bitable.build_row(_report(item), day="D")
        assert column in row, f"{column!r} 对应的指标名 {metric!r}/{key!r} 没有命中 —— 列会永远空着"
        assert row[column] == round(12.0 if key else 34.0, digits)


def test_container_memory_column_is_the_peak_not_an_average():
    """三个容器合成一列取**最高**那个。

    取平均会把「三个里有一个撞到 95%」抹平成一个看着没事的数。而这一列要回答的是「有没有
    在恶化」—— 最危险的那个才是答案。
    """
    row = bitable.build_row(
        _report(
            findings.ok("a 内存", value="", baseline="", direction="", num=41.0, num_warn=80.0, unit="%"),
            findings.bad("b 内存", value="", baseline="", direction="", num=95.0, num_warn=80.0, unit="%"),
            findings.ok("c 内存", value="", baseline="", direction="", num=52.0, num_warn=80.0, unit="%"),
        ),
        day="D",
    )
    assert row["容器内存峰值 %"] == 95.0, "取峰值不取平均, 也不取第一个"


def test_container_memory_all_unknown_leaves_the_cell_empty():
    """三个容器全没量到 → 空格子, **不是 0**。

    `max([])` 要么抛要么被兜成 0, 而 0% 内存占用看起来是「极其健康」—— 这是本文件开头那句话
    在合成列上的形态。docker stats 跑不起来时走的就是这条路。
    """
    row = bitable.build_row(
        _report(
            findings.unknown("a 内存", reason="docker stats 跑不起来", baseline="< 80%", direction="接近 limit"),
            findings.unknown("b 内存", reason="docker stats 跑不起来", baseline="< 80%", direction="接近 limit"),
        ),
        day="D",
    )
    assert "容器内存峰值 %" not in row


def test_peak_only_looks_at_primary_numbers():
    """合成峰值只看主数, 不把附属数拉进来比大小。

    附属数是同一指标的另一个百分位。让它参与竞争等于让 p50 和 p95 抢同一个格子, 而
    「哪个容器最高」那个问题的答案会变成一个百分位的名字。
    """
    row = bitable.build_row(
        _report(
            findings.ok(
                "a 内存",
                value="",
                baseline="",
                direction="",
                num=41.0,
                num_warn=80.0,
                unit="%",
                extra_nums={"p50": 99.0},
            )
        ),
        day="D",
    )
    assert row["容器内存峰值 %"] == 41.0


# ───────────────── 计数与越线项 ─────────────────


def test_unknown_count_is_its_own_column_not_folded_into_anomalies():
    """异常数与未测到数是**两列**。

    合成一列「有问题的项数」会让「今天 5 项瞎了」和「今天 5 项越线」在表里长得一样, 而两者的
    修法完全不同(一个是去修探针, 一个是去修生产)。
    """
    row = bitable.build_row(
        _report(
            findings.bad("坏的", value="502", baseline="200", direction="应为 200"),
            findings.unknown("瞎的", reason="探针没跑", baseline="0", direction="-"),
            findings.unknown("也瞎的", reason="探针没跑", baseline="0", direction="-"),
            findings.ok("好的", value="200", baseline="200", direction="应为 200"),
        ),
        day="D",
    )
    assert row["异常数"] == 1
    assert row["未测到数"] == 2


def test_offenders_column_names_anomalies_only_and_never_gaps():
    """越线项**只点异常**, 未测到的一个都不进 —— 它们只贡献 `未测到数` 那个计数。

    这一条是投放当天量出来的: metrics 未落盘让 12 项同时报未测到, 两类都列的那一版把这一格
    撑到 200 多字, 整行读不了。而缺口清单在消息里本来就会完整发一遍(有缺口必发), 抄进格子
    不增加任何信息, 只赔可读性。

    断言写成「整格里一个未测到项的名字都不出现」而不是「不带前缀」: 后者只要把前缀改掉就能
    通过, 而那不是这条要防的事。
    """
    row = bitable.build_row(
        _report(
            findings.bad("磁盘使用率 /", value="91%", baseline="< 85%", direction="越高越糟"),
            findings.unknown("飞书 wss 长连接数", reason="ss 不在", baseline=">= 1", direction="0 = 收不到"),
        ),
        day="D",
    )
    assert row["越线项"] == "磁盘使用率 /"
    assert "wss" not in row["越线项"], "未测到的项不得进越线项 —— 数量看未测到数那一列"
    assert row["未测到数"] == 1, "不进越线项, 但必须还被数着 —— 否则缺口就真的没人看了"


def test_healthy_day_writes_an_empty_offenders_cell_not_a_missing_key():
    """全绿那天越线项是**空串**, 不是缺 key。

    这一列与数字列的规则刻意相反: 数字列缺席表示「没量到」, 而越线项缺席会让人以为那天没
    写进去。空串明确表示「量了, 没有越线的」。
    """
    row = bitable.build_row(_report(findings.ok("好的", value="200", baseline="200", direction="应为 200")), day="D")
    assert row["越线项"] == ""


# ───────────────── 花费 ─────────────────


def test_lower_bound_cost_still_enters_the_table_with_a_flag():
    """下限成立时金额**照样进表**, 由 `花费是否下限` 那列标出来。

    留空会丢掉唯一的花费趋势; 而不标下限的话, 一个偏低的数字看不出是真省了还是有来源没量到
    —— 两者在一列数字里长得一样。
    """
    row = bitable.build_row(
        _report(
            findings.ok(
                "当日总花费",
                value="≥ 3.2100 CNY (下限 —— 2 个来源未测到)",
                baseline="< 50 CNY/天",
                direction="越高越糟",
                num=3.21,
                num_warn=50.0,
                unit="CNY",
            )
        ),
        day="D",
    )
    assert row["当日总花费 CNY"] == 3.21
    assert row["花费是否下限"] == "是"


def test_full_coverage_cost_is_flagged_not_lower_bound():
    """反向: 全量算出来时标「否」。

    少了这一半, 一个「永远写是」的实现也能让上面那条通过, 而那样这一列就没有信息量了。
    """
    row = bitable.build_row(
        _report(
            findings.ok(
                "当日总花费",
                value="3.2100 CNY",
                baseline="< 50 CNY/天",
                direction="越高越糟",
                num=3.21,
                num_warn=50.0,
                unit="CNY",
            )
        ),
        day="D",
    )
    assert row["花费是否下限"] == "否"


def test_currency_mismatch_keeps_the_money_out_of_the_column():
    """单价表换成美元 → 那一格**留空**并记进越线项, 不混进 CNY 那列。

    列名里写死了 CNY 而币种来自单价表。换一版美元单价表后数字会一声不响地进这一列, 历史行
    与新行在同一列里是两种钱 —— 折线图上那是一次七倍下跌。宁可缺一天。
    """
    row = bitable.build_row(
        _report(
            findings.ok(
                "当日总花费",
                value="0.4500 USD",
                baseline="< 50 USD/天",
                direction="越高越糟",
                num=0.45,
                num_warn=50.0,
                unit="USD",
            )
        ),
        day="D",
    )
    assert "当日总花费 CNY" not in row
    assert "币种不符" in row["越线项"] and "USD" in row["越线项"], "留空必须出声, 否则与「没量到」不可区分"


# ───────────────── open API 那一层 ─────────────────


def test_business_error_in_a_http_200_body_is_a_failure():
    """**HTTP 200 + body 里非零 code** 必须当失败。

    这是飞书的形状, 不是假想: 实测写一个不存在的列名回的是 200 带 `code=1254045`。只看状态码
    的实现会把每一次写失败都判成成功, 于是「表停更了」永远不出声 —— 而表停更的表现就是表里
    没有新行, 与「今天没跑」不可区分。
    """
    opener = FakeOpener(replies={"/records": {"code": 1254045, "msg": "FieldNameNotFound"}})
    with pytest.raises(bitable.BitableError, match="1254045"):
        bitable._call("/bitable/v1/apps/a/tables/t/records", "tok", {"fields": {}}, opener=opener)


def test_body_is_parsed_even_when_http_status_is_an_error():
    """HTTP 4xx 时也要**读 body** 再判。

    飞书把可诊断的信息放 body 里(哪个字段不认、哪个 token 过期), 而 urllib 对 4xx 抛
    `HTTPError`。不读 body 的话报错只剩「400 Bad Request」, 查起来要重新抓包。
    """
    opener = FakeOpener(replies={"/records": {"code": 1254018, "msg": "InvalidFilter"}}, http_error_on="/records")
    with pytest.raises(bitable.BitableError, match="InvalidFilter"):
        bitable._call("/bitable/v1/apps/a/tables/t/records", "tok", {"fields": {}}, opener=opener)


def test_same_day_rerun_updates_instead_of_appending():
    """同一天重跑 → **PUT 那一行**, 不 POST 新的一行。

    cron 重跑、手工补跑、有人直接执行 run.py —— 一天两行会在折线图上画出一个垂直段, 而那
    看起来像指标瞬间跳变。判据同时压「用了 PUT」和「没有 POST 到 records」: 只压前者的话,
    一个既 PUT 又 POST 的实现照样全绿。
    """
    opener = FakeOpener(replies={"records/search": {"code": 0, "data": {"items": [{"record_id": "rec1"}]}}})
    action, rid = bitable.write_row("app", "tbl", "tok", {bitable.PRIMARY: "2026-09-21", "异常数": 0}, opener=opener)
    assert (action, rid) == ("updated", "rec1")
    methods = [(m, p) for m, p, _b in opener.calls if "records" in p and "search" not in p]
    assert methods == [("PUT", "/bitable/v1/apps/app/tables/tbl/records/rec1")], f"实收 {methods}"


def test_first_run_of_the_day_creates_a_row():
    """反向: 搜不到今天 → POST 新行。少了这一半, 「永远 PUT」的实现第一天就写不进去。"""
    opener = FakeOpener(
        replies={
            "records/search": {"code": 0, "data": {"items": []}},
            "tables/tbl/records": {"code": 0, "data": {"record": {"record_id": "recNEW"}}},
        }
    )
    action, rid = bitable.write_row("app", "tbl", "tok", {bitable.PRIMARY: "2026-09-21"}, opener=opener)
    assert (action, rid) == ("created", "recNEW")
    assert [m for m, p, _b in opener.calls if p.endswith("/records")] == ["POST"]


def test_search_filters_by_the_primary_field_value():
    """按主字段搜今天那一行, 且 filter 的形状是实测那个。

    形状写错回的是 `code=1254018 InvalidFilter`(实测)。而搜不到的后果不是报错 —— 是每天
    追加一行, 一个月后表里一天好几行。
    """
    opener = FakeOpener(replies={"records/search": {"code": 0, "data": {"items": []}}})
    bitable.write_row("app", "tbl", "tok", {bitable.PRIMARY: "2026-09-21"}, opener=opener)
    body = next(b for _m, p, b in opener.calls if "records/search" in p)
    assert body is not None
    cond = body["filter"]["conditions"][0]
    assert cond["field_name"] == bitable.PRIMARY
    assert cond["value"] == ["2026-09-21"]


def test_ensure_schema_skips_columns_that_already_exist():
    """已存在的列**一个都不碰**: 不改名、不改类型。

    改名会让代码按新名写入时报 FieldNameNotFound, 而老名那列从此不再更新 —— 表面上看是
    「那个指标停止采集了」。所以这个函数只补缺的。
    """
    every = [{"field_name": n} for n in (*bitable.COUNT_COLUMNS, *bitable.NUMERIC_COLUMNS, *bitable.TEXT_COLUMNS)]
    opener = FakeOpener(replies={"/fields": {"code": 0, "data": {"items": every}}})
    created = bitable.ensure_schema("app", "tbl", "tok", opener=opener)
    assert created == []
    assert [m for m, p, _b in opener.calls if p.endswith("/fields")] == ["POST"] * 0 + [], (
        f"列都在时不该有任何建列请求, 实收 {opener.calls}"
    )


def test_ensure_schema_creates_only_the_missing_columns():
    """反向: 缺的补上, 并把补了哪几列返回出来。

    返回值有用处: `push` 拿它打一行 stderr。一张表默默长出新列会让「历史行那一列是空的」
    看起来像一段观测缺口, 而其实是那天还没有这个指标。
    """
    have = [{"field_name": n} for n in (*bitable.COUNT_COLUMNS, *bitable.TEXT_COLUMNS)]
    opener = FakeOpener(replies={"/fields?": {"code": 0, "data": {"items": have}}})
    created = bitable.ensure_schema("app", "tbl", "tok", opener=opener)
    assert created == list(bitable.NUMERIC_COLUMNS)
    types = {b["field_name"]: b["type"] for m, p, b in opener.calls if m == "POST" and b and "field_name" in b}
    assert set(types) == set(bitable.NUMERIC_COLUMNS)
    assert set(types.values()) == {2}, "数字列必须是数字类型(2) —— 文本类型的列画不出折线"


def test_push_reports_and_returns_false_when_config_is_incomplete():
    """配置不全 → **出声并返回 False**, 不抛。

    抛出去会带垮日报消息, 而表写不进去不该让告警也发不出。静默跳过更糟: 「表里一直没有
    新行」查不到原因。
    """
    cfg = config.Config(webhook_url="u", containers=())
    assert bitable.push(_report(), cfg, day="D") is False


def test_push_does_not_raise_when_the_api_rejects_the_write(capsys):
    """写表被拒 → 打 stderr 并返回 False, **不往上抛**。

    同上: 一次飞书 API 抖动不该变成「日报生成失败」。而失败必须进 cron 日志, 否则表停更了
    没有任何地方看得见。
    """
    cfg = config.Config(
        webhook_url="u",
        containers=(),
        feishu_app_id="cli_x",
        feishu_app_secret="sec",
        bitable_app_token="app",
        bitable_table_id="tbl",
    )
    opener = FakeOpener(
        replies={
            "tenant_access_token": {"code": 0, "tenant_access_token": "tok"},
            "/fields": {"code": 1254045, "msg": "FieldNameNotFound"},
        }
    )
    assert bitable.push(_report(), cfg, day="D", opener=opener) is False
    assert "趋势表写入失败" in capsys.readouterr().err
