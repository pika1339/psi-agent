"""趋势表 —— 每天往飞书多维表格追加一行。

## 为什么是表而不是图

卡片里画过一张条形图, 结论是不要: 一天一张的图只能看**当天**, 而「磁盘从 78 涨到 87」这种
话图上看不出来 —— 它需要历史, 而这套监控本身不存历史。多维表格既是存储也是视图: 一行一天,
列是指标, 飞书自带的图表视图能直接在任一列上出折线。

所以这里只负责**把数字写进去**, 不负责画。画由看表的人在飞书里挑列。

## 「没量到」在表里的形态是空格子, 不是 0

这是这一层最容易犯、也最贵的错。一列数字里 0 与一个真实的低读数完全不可区分, 而折线图会
把 0 画成一次暴跌 —— 读者看到磁盘从 81 掉到 0, 合理推断是清过盘, 实际是探针瞎了。

所以 `Finding.num` 为 None 的项**整个 key 不进 payload**(不是传 `None`, 更不是传 0)。
飞书对缺失的 key 留空格子, 那正是我们要的形态。`findings.Finding.__post_init__` 已经把
「UNKNOWN 带 num」做成构造期报错, 这里再不读 UNKNOWN 的 num, 两道一起挡。

空格子有一个已知的二义性: 「当天没量到」与「那天还没有这个指标」(后加的列在老行是空的)长得
一样。区分办法是看同一行 `越线项` 之外的 `未测到数` —— 不完美, 但表结构解决不了它, 记在
这里比假装没有好。

## 判重: 同一天只有一行

cron 重跑、手工补跑、`run.py` 被人手动执行 —— 一天写两行会让折线图上出现一个垂直段。所以
写之前先按日期 search, 命中就 PUT 更新那一行, 不命中才 POST 新建。

## 为什么用 app token 而这套监控的消息用 webhook

`notify.py` 的模块 docstring 说了不用 app token 的理由(OAuth 回调正是坏过的那条路)。这里
不得不用: 多维表格没有 webhook 写入方式。风险是真的, 缓解是**写表失败不影响发消息** ——
`run.py` 里两件事互不依赖, 表写失败只在 stderr 出声并让退出码非零, 异常消息照发。
"""

from __future__ import annotations

# ruff: noqa: T201  命令行脚本, stdout/stderr 就是它的输出通道。
import json
import sys
import urllib.error
import urllib.request

from findings import BAD, UNKNOWN, Report

BASE = "https://open.feishu.cn/open-apis"
#: 单次请求超时秒数。与 notify 一致 —— 即时档 5 分钟一轮, 不能卡住。
TIMEOUT = 15

#: 主字段名。判重按它, 所以它必须是**文本**而不是日期类型: 日期类型存的是毫秒时间戳,
#: search 时的时区归一化会让「今天」在边界上对不上。
PRIMARY = "日期"

#: 趋势表的数值列: 列名 -> (指标名, 单位, 小数位)。
#:
#: **列名一旦写进表就不好改** —— 改名后历史行的数据还在, 但代码按新名写入会报
#: FieldNameNotFound(实测 code 1254045), 而老名那一列从此不再更新, 表面上看是「那个指标
#: 停止采集了」。所以这张映射表是契约, 改它等于改表。
#:
#: 列数刻意少(负责人定的: 一版 36 列被否, 「指标太多了」)。取舍原则:
#:
#: * 同类**取最严的一个**而不是每个占一列: 三个容器内存合成一列峰值, 是哪个容器写进
#:   `越线项` 那列。趋势要回答的是「有没有在恶化」;
#: * 砍掉的项**不是不采**, 日报正文照旧有全部 29 项, 只是不进趋势表。
#: 容器内存峰值的合成指标名。**故意取一个 probes 里不可能出现的名字**: 若哪天这个名字
#: 真撞上一条 Finding, 合成值会被那条覆盖而不报错, 于是「三容器最高」静默变成「某一个」。
_PEAK_MEM = "\x00容器内存峰值"

#: 列名 -> (指标名, 附属数的 key, 小数位)。
#:
#: 第二项是 `Finding.extra_nums` 里的 key: 空串表示取主数 `num`。`请求体 p50` 这种
#: **不是独立指标** —— 它和 p95 是同一条 Finding 的两个数, 因为 p50 没有自己的告警线
#: (基线写在 p95 上), 硬拆成两条 Finding 就得给 p50 编一条线出来。见 `Finding.extra_nums`。
#:
#: 指标名**抄自 probes 里的字面量, 不照列名猜**: 拼错的后果是那一列永远空着, 而空着的
#: 语义是「没量到」—— 一个拼写错误会伪装成一个观测缺口, 而且每天都很像真的。
NUMERIC_COLUMNS: dict[str, tuple[str, str, int]] = {
    "当日总花费 CNY": ("当日总花费", "", 4),
    "磁盘使用率 %": ("磁盘使用率 /", "", 1),
    "容器内存峰值 %": (_PEAK_MEM, "", 1),
    "可用内存 GiB": ("可用内存", "", 2),
    "wss 长连接数": ("飞书 wss 长连接数", "", 0),
    "OOM kill 次数": ("内核 OOM kill 次数", "", 0),
    "请求体 p50 KiB": ("请求体字节数 p50/p95", "p50", 1),
    "请求体 p95 KiB": ("请求体字节数 p50/p95", "", 1),
    "首字延迟 p50 秒": ("首字延迟 p50/p95", "p50", 2),
    "首字延迟 p95 秒": ("首字延迟 p50/p95", "", 2),
    "压缩次数": ("压缩次数", "", 0),
}

#: 文本列。`越线项` 与 `花费是否下限` 各自回答一个数字列回答不了的问题。
TEXT_COLUMNS = ("越线项", "花费是否下限")
#: 计数列。
COUNT_COLUMNS = ("异常数", "未测到数")

#: 字段描述: 写进飞书字段的 description, 让看表的人不必回来翻代码。
#:
#: wss 那条尤其要写: 它只证明**收得到**, 不证明发得出 —— 只起 gateway 也会真发飞书卡片。
#: 掉到 0 意味着「用户发的消息没人收」, 而不是「机器人死了」。反方向排查会浪费一整轮。
FIELD_NOTES: dict[str, str] = {
    PRIMARY: "宿主本地日期(YYYY-MM-DD)。一天一行, 重跑会更新而不是追加。",
    "异常数": "当日 BAD 项数。0 不代表健康 —— 还要看未测到数。",
    "未测到数": "当日观测缺口项数。缺口与故障同级: 都意味着这一轮没人在看。",
    "越线项": "具体哪几项越线/没量到。同类指标在数字列里取了最严的一个, 是哪个容器/哪个域名看这里。",
    "当日总花费 CNY": "报告层按带版本号的单价表算出, 未与上游账单对过。内核只记 token 数与模型 id。",
    "花费是否下限": "「是」表示有回合缺 usage 或有来源没读到, 真实花费只会更高。",
    "磁盘使用率 %": "告警线 85%。",
    "容器内存峰值 %": "三个容器里**最高**的那个, 不是平均。告警线 80%; 撞 3g 上限会 memcg OOM。",
    "可用内存 GiB": "告警线 >= 1.5 GiB。实测 1.6 GiB 那次伴随全局 OOM。",
    "wss 长连接数": "只证明收得到消息, **不证明发得出** —— 只起 gateway 也会真发卡片。0 = 用户消息没人收。",
    "OOM kill 次数": "读 dmesg。容器内子进程被杀时 docker 的 OOMKilled 仍是 false, 只有这条看得见。",
    "请求体 p50 KiB": "首字延迟 ≈ 字节数 ÷ 230KB/s, 所以这一列是延迟的成因。缓存省钱不省字节。",
    "请求体 p95 KiB": "同上。",
    "首字延迟 p50 秒": "取 turn 行的 ttft_s(第一个内容 delta)。",
    "首字延迟 p95 秒": "告警线 6s。",
    "压缩次数": "告警线 10 次/天。每次都是一整轮模型调用的钱和时间。",
}


class BitableError(Exception):
    """写表失败。**不往上抛到让日报整体失败** —— 见 run.py 里的调用点。"""


def _call(path: str, token: str, body: dict | None = None, *, method: str | None = None, opener=None) -> dict:
    """一次 open API 调用。返回解析后的 body, HTTP 错误也解析 —— 飞书把业务错误放在
    body 的 `code` 里, 而 HTTP 状态码可能是 200。只看状态码会把失败判成成功。
    """
    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method or ("POST" if data is not None else "GET")
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        raw = open_fn(req, timeout=TIMEOUT).read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except (OSError, urllib.error.URLError) as exc:
        raise BitableError(f"{path}: 连不上 ({type(exc).__name__}: {exc})") from exc
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BitableError(f"{path}: 返回不是 JSON: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict) or parsed.get("code") != 0:
        raise BitableError(f"{path}: code={parsed.get('code')} msg={parsed.get('msg')}")
    return parsed.get("data") or {}


def tenant_token(app_id: str, app_secret: str, *, opener=None) -> str:
    """换 tenant access token。**不缓存**: 日报一天一次, 缓存的收益为零而过期的坑是真的。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        BASE + "/auth/v3/tenant_access_token/internal/", data=body, headers=headers, method="POST"
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        raw = open_fn(req, timeout=TIMEOUT).read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except (OSError, urllib.error.URLError) as exc:
        raise BitableError(f"取 token 连不上 ({type(exc).__name__}: {exc})") from exc
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BitableError(f"取 token 返回不是 JSON: {raw[:200]!r}") from exc
    token = parsed.get("tenant_access_token") if isinstance(parsed, dict) else None
    if not token:
        raise BitableError(f"取 token 失败: code={parsed.get('code')} msg={parsed.get('msg')}")
    return str(token)


def _measured(report: Report) -> dict[tuple[str, str], float]:
    """(指标名, 附属 key) -> 数值。**只收真量到的**, 附属数走 `("名", "p50")` 这种键。

    UNKNOWN 的项一律不进来: 它们的 `num` / `extra_nums` 必然是空(构造期已挡), 这里再显式
    过滤一遍 —— 两道挡同一件事, 因为这件事的代价是一条假的趋势线。
    """
    out: dict[tuple[str, str], float] = {}
    for f in report.findings:
        if f.status == UNKNOWN:
            continue
        if f.num is not None:
            out[(f.name, "")] = f.num
        for key, value in f.extra_nums.items():
            out[(f.name, key)] = value
    return out


def _peak(measured: dict[tuple[str, str], float], suffix: str) -> float | None:
    """同类指标取**最大**的那个(容器内存那种)。一个都没量到时返回 None, 不返回 0。

    只看主数(`key == ""`): 附属数是同一指标的另一个百分位, 把它也拉进来比大小等于让
    p50 和 p95 竞争同一个格子。
    """
    hits = [v for (name, key), v in measured.items() if key == "" and name.endswith(suffix)]
    return max(hits) if hits else None


def build_row(report: Report, *, day: str) -> dict[str, object]:
    """把一份 `Report` 变成一行。**没量到的列整个 key 不出现。**

    不是传 `None`, 也不是传 0: 飞书对缺失的 key 留空格子, 而空格子是「没量到」在表里唯一
    不会被误读成低读数的形态。传 0 会让折线图画出一次暴跌。
    """
    measured = _measured(report)
    anomalies = report.of(BAD)
    unknowns = report.of(UNKNOWN)

    row: dict[str, object] = {
        PRIMARY: day,
        "异常数": len(anomalies),
        "未测到数": len(unknowns),
    }

    # 越线项**只列异常**, 不列未测到。
    #
    # 曾经两类都列, 各带前缀, 理由是「看表的人最需要知道哪一项瞎了」。那个判断是错的: 有观测
    # 缺口时消息**一定会发**(见 run.py 的触发条件), 缺口清单在消息里已经完整给过一遍, 抄进
    # 格子换不来任何信息。而代价是首次投放那天这一格 200 多字 —— metrics 未落盘让 12 项同时
    # 报未测到 —— 整行被压成一堵读不了的墙, 正是这次改表要逃开的那件事。
    #
    # 缺口的**数量**由 `未测到数` 那一列承担, 趋势(缺口有没有在收敛)看那一列就够。
    parts = [f.name for f in anomalies]
    row["越线项"] = ", ".join(parts) if parts else ""

    # 容器内存是一容器一条 Finding(`psi-agent-gateway 内存`), 合成成一列峰值再进表。
    # 先塞进 measured 是为了让下面那个循环只有一条取数路径 —— 两条路径意味着「没量到就留空」
    # 这个规则要实现两遍。
    peak = _peak(measured, "内存")
    if peak is not None:
        measured[(_PEAK_MEM, "")] = peak

    for column, (metric, key, digits) in NUMERIC_COLUMNS.items():
        hit = measured.get((metric, key))
        if hit is None:
            continue  # 空格子 —— 见 docstring
        row[column] = round(hit, digits)

    money = next((f for f in report.findings if f.name == "当日总花费"), None)
    if money is not None:
        # 币种核对: 列名里写死了 CNY, 而金额的币种来自**单价表**(`pricing.currency`)。换一版
        # 美元单价表后, 数字会一声不响地进这一列, 历史行与新行在同一列里是两种钱 —— 折线图上
        # 那是一次七倍下跌。对不上就留空并记进越线项, 宁可缺一天也不要一列混币。
        if "当日总花费 CNY" in row and money.unit != "CNY":
            del row["当日总花费 CNY"]
            parts.append(f"币种不符:单价表是 {money.unit} 而趋势表列名是 CNY, 本日花费未入表")
            row["越线项"] = ", ".join(parts)
        # 花费是否下限: 「是」时那个金额只是下限, 真实花费只会更高。没有这一列的话, 一个偏低
        # 的数字看不出是真省了还是有来源没量到 —— 两者在一列数字里长得一样。
        row["花费是否下限"] = "是" if money.status == UNKNOWN or "下限" in money.value else "否"

    return row


def ensure_schema(app_token: str, table_id: str, token: str, *, opener=None) -> list[str]:
    """把缺的列补齐, 返回新建的列名。已存在的列**不改类型、不改名**。

    幂等: 每次运行都调一遍, 这样「加了一个新指标」不需要有人去飞书里手点。不动已有列是
    因为改名会让代码按新名写入时报 FieldNameNotFound, 而老名那列从此不再更新 —— 表面上
    看是「那个指标停止采集了」。
    """
    base = f"/bitable/v1/apps/{app_token}/tables/{table_id}"
    existing = {f["field_name"] for f in _call(f"{base}/fields?page_size=200", token, opener=opener).get("items", [])}
    created: list[str] = []

    def add(name: str, field_type: int, prop: dict | None = None) -> None:
        if name in existing:
            return
        body: dict[str, object] = {"field_name": name, "type": field_type}
        if prop:
            body["property"] = prop
        if name in FIELD_NOTES:
            # 描述写进字段本身, 让看表的人不必回来翻代码。注意告警线也写在这里, 而它是代码
            # 里的常量 —— 改了常量, 历史行的描述会跟着变成新值, 看老数据时对不上。日报正文
            # 的 `基线` 一栏仍是当天那个值, 那才是可追溯的那份。
            body["description"] = {"text": FIELD_NOTES[name]}
        _call(f"{base}/fields", token, body, opener=opener)
        created.append(name)

    for name in COUNT_COLUMNS:
        add(name, 2, {"formatter": "0"})
    for column, (_metric, _unit, digits) in NUMERIC_COLUMNS.items():
        add(column, 2, {"formatter": "0" if digits == 0 else "0." + "0" * digits})
    for name in TEXT_COLUMNS:
        add(name, 1)
    return created


def write_row(app_token: str, table_id: str, token: str, row: dict[str, object], *, opener=None) -> tuple[str, str]:
    """写一行。返回 ("created"|"updated", record_id)。

    **同一天只有一行。** 先按主字段 search, 命中就更新那一行。cron 重跑、手工补跑、有人
    直接执行 run.py —— 一天两行会在折线图上画出一个垂直段, 而那看起来像指标瞬间跳变。
    """
    base = f"/bitable/v1/apps/{app_token}/tables/{table_id}"
    day = row[PRIMARY]
    found = _call(
        f"{base}/records/search?page_size=2",
        token,
        {"filter": {"conjunction": "and", "conditions": [{"field_name": PRIMARY, "operator": "is", "value": [day]}]}},
        opener=opener,
    )
    items = found.get("items") or []
    if items:
        rid = items[0]["record_id"]
        _call(f"{base}/records/{rid}", token, {"fields": row}, method="PUT", opener=opener)
        return "updated", rid
    data = _call(f"{base}/records", token, {"fields": row}, opener=opener)
    return "created", (data.get("record") or {}).get("record_id", "")


def push(report: Report, cfg, *, day: str, opener=None) -> bool:
    """建列 + 写行。失败时**出声并返回 False, 不抛** —— 表写不进去不该带垮日报消息。

    配置不全时同样返回 False 并说明缺什么: 静默跳过会让「表里一直没有新行」查不到原因。
    """
    missing = [
        n
        for n, v in (
            ("app_id", cfg.feishu_app_id),
            ("app_secret", cfg.feishu_app_secret),
            ("bitable_app_token", cfg.bitable_app_token),
            ("bitable_table_id", cfg.bitable_table_id),
        )
        if not v
    ]
    if missing:
        print(f"[monitor] 趋势表未配置({', '.join(missing)}), 本轮不写表", file=sys.stderr)
        return False
    try:
        token = tenant_token(cfg.feishu_app_id, cfg.feishu_app_secret, opener=opener)
        created = ensure_schema(cfg.bitable_app_token, cfg.bitable_table_id, token, opener=opener)
        if created:
            print(f"[monitor] 趋势表新建了 {len(created)} 列: {', '.join(created)}", file=sys.stderr)
        action, rid = write_row(
            cfg.bitable_app_token, cfg.bitable_table_id, token, build_row(report, day=day), opener=opener
        )
        print(f"[monitor] 趋势表 {action} {rid}", file=sys.stderr)
        return True
    except BitableError as exc:
        print(f"[monitor] 趋势表写入失败: {exc}", file=sys.stderr)
        return False
