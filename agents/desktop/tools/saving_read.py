"""saving_read v1: 把**页面**确定性地读成事实。

放在链路里的位置(承 `saving_login`):

    查登录态 -> 登录 -> 导航到读定义的 url -> **本工具读成事实** -> 交给本体判定

本工具只负责最后一步。**不导航、不点击、不重试** —— 导航由 agent 用 `browser_*` 完成:
它看得见页面, 也要在登录墙 / 验证码前停下(那时走 `saving_login(action="blocked")`)。

## 一条不能破的线: "读不到" != "没有"

同一段选择器读到 0 张券, 有两种成因, 而它们在计数上完全一样:

- 券包**确实是空的** —— 页面的模块容器还在, 只是里面没有券。这是**事实**。
- 页面**结构变了 / 根本不是那一页** —— 连模块容器都找不到。这是**未知**。

区分它们只能靠 `markers`(页面必须存在的容器选择器)。所以本工具的结果是这样分的:

| 情况 | 返回 |
|---|---|
| 身份对 + markers 在 + 有券 | `ok=true`, 券清单(事实) |
| 身份对 + markers 在 + 0 张 | `ok=true, empty=true`(事实: 券包为空) |
| 身份对 + markers **不在** | `ok=false, reason=page_shape_changed` |
| 区块锚点 / 行根找不到 | `ok=false, reason=page_shape_changed`(附 missing_anchors / missing_rows) |
| 某个区块的行全被隐私闸门挡掉 | `ok=false, reason=read_blocked_by_pii_gate` |
| 落到登录域 | `ok=false, reason=logged_out`(附 gate 与下一步) |
| 是别的地址 | `ok=false, reason=wrong_page`(附该去的 url) |
| 窗口被用户关 | `ok=false, reason=browser_closed`(要停下告知用户) |
| MCP 不通 / JS 抛错 | `ok=false, reason=browser_unavailable` |
| 拿回的文本不是 JSON | `ok=false, reason=unparsable_extract` |

`ok=false` 的返回**不带**任何事实字段(`count` / `coupons` / `empty` / `rows` / `states` /
`screened_out`)。这是刻意的, 与本仓 facts 契约的四态语义同源: `MISSING` 绝不能被下游当成
`false`。少了字段只是少一个信息; 多了个 `count: 0` 就是一个被当真的假事实。

**键的缺席表示"没问", 不是"没有"**: 一条读定义只声明了哪种形态, 返回里就只出现那种形态的
键。只声明列表项的读定义不会回一个空 `rows: {}` —— 那会让下游把"这条读定义不覆盖区块"读成
"这一页没有那些行"。

## 两种抽取形态

页面事实不止一种形状。同一条读定义里可以同时声明两种(结算页本来就需要):

**形态一 · 重复的列表项** —— `item` + `fields`: 一张券一行, 字段名就是交给本体的事实契约。
券包 / 商品列表这种"一页 N 条同类东西"用它。

**形态二 · 页面级的键值行 + 区块状态** —— `blocks`: 京东结算页最有价值的事实是**页面级的行**
(商品总额 / 运费 / 共减, 不是列表)和**券区的状态**(三个页签 + 一个明确的空态「无可用优惠
券」)。这些用 `item` + `fields` 抽不出来 —— 它们没有"一页 N 条"的结构。

```yaml
reads:
  <target>:
    markers: [".page-frame"]          # 页面级: 这一页到底是不是读定义要的那一页
    item: ".coupon-item"              # 形态一(可选)
    fields: {amount: {sel: ".c-price strong"}}
    blocks:                           # 形态二(可选)
      - name: payment
        anchor: [".payment-summary", ".payment-summary-item"]   # 候选锚点, 命中其一即"在"
        item: [".payment-summary-item"]                         # 行根(在锚点**里面**找)
        label: ".payment-summary-item__title"
        value: ".payment-summary-item__price"
      - name: coupon_area
        anchor: [".quan-area"]
        states:
          tab_available: {sel: [".tab-available"], presence: true}
          empty: {sel: [".noData-531368"], presence: true, evidence: [".coupon-item"]}
```

`markers` 是**页面**级的("这一页是不是那一页"), `anchor` 是**区块**级的("这个区块在不在")。
两者都要, 因为页面框架在、而券区模块悄悄换掉是完全可能的 —— 那时只有 anchor 能说话。

三条形态二专属的纪律:

1. **锚点是承重的**: 任何一条声明的锚点在页面上找不到 -> `ok=false` +
   `reason=page_shape_changed` + `missing_anchors`, **整条读定义一起降级**。绝不允许"这一块
   读不出、那一块照常交事实" —— 半截事实会被下游当成完整事实用。
2. **行根一根都没命中也是未知**(`missing_rows`): "区块在、行读不出"正是本模块最贵的那类错,
   所以 `ok=true` 的返回里 `rows.<block>` 永远不是空列表。
3. **抽取范围 = 锚点元素里面**: 行与状态只在该区块内部找。这不只是"证明区块在", 还是下面那条
   硬约束的第一道闸门。

`presence` 判 `false` 是一条**否定断言**, 默认需要 `evidence`(判 false 时页面本该具备的东西);
`evidence` 也没命中就只能算"不知道" —— 那个键**不出现在返回里**, 绝不当成 `false`。`evidence`
是可选的: 没写 = 作者接受"选择器没命中就是 false"这个更强的假设; 写坏了等同于没写(和其他字段
一样, 写坏的单条不炸整条读定义)。

## 一条硬约束: 绝不抽取收货人 / 支付信息

结算页**同屏**渲染收货人姓名、手机号、详细地址、银行卡后四位。形态二是通用键值抽取, 顺手就能
把它们抓进来 —— 而这些内容与"这单省多少钱"毫无关系。三道闸门, 都在代码里, 不靠数据文件自觉:

1. **结构隔离**: 行只在锚点元素**内部**抽取。付款区块的 `item` 够不到同屏的收货人模块。
2. **页面层拦截**(注入的 JS): 命中的行**在页面上就不带走** —— 隐私内容不跨 MCP 边界,
   也就不会落进会话记录。
3. **返回层复核**(本文件): `rows` / `states` / `coupons` 在组装 payload 前**再过一遍**同一份
   规则。这一层是契约边界: 不管页面那一半交回什么, 本工具的输出里都不含 PII 形状的内容。
   `unparsable_extract` 回显的原文同样过闸门。

规则只有一份字面量(`_PII_LABELS` / `_PII_PATTERNS`), Python 与 JS 共用, 不各写一套。命中被
挡掉的条数在 `screened_out` 里如实报出 —— 正常页面应当是 0, 非 0 说明选择器太宽或页面变了。

**这是关键词 + 值形状的闸门, 不是保证**: 一个标着"联系人"以外标签的中文姓名, 规则认不出来。
真正的第一道防线是结构隔离与"区块要声明锚点"这条纪律, 闸门是兜底。所以数据文件里**不要**把
收货人 / 发票 / 支付模块写进 `blocks`。

## 带构建哈希的类名不稳

京东结算页有一批形如 `xx-1a2b3c` 的类名, 会随对方重新构建而变。所以**每个选择器槽都接受两种
写法**: 一个字符串, 或一个按顺序试的**候选列表**(`sel` / `markers` 的每一项 / `anchor` /
`item` / `label` / `value` / `kv` 都是)。首选不稳就补一个不含哈希的锚点, 谁先命中用谁; 报出来
的键用首选(维护者要改的就是它)。这条**不区分形态**, 形态一一样受益。

## 事实与判定分开

本工具只交**页面上写着的东西**(面额 / 门槛 / 有效期 / 商品总额 / 运费 / 共减 / 页签在不在)。
能不能用在这单上、和国补怎么叠加、最终到手多少, 全是**本体**的活 —— 这里一律不推断, 不给
"建议", 也不做算术(负值 `-￥22.75` 与币种前缀都**原样透传**)。返回里的 `note` 会把这句话再
说一遍, 因为越权推断最容易发生在刚拿到原始数据的那一刻。

## 配置在哪

读定义跟着平台走, 放 `<agent>/platforms/<key>.yaml` 的 `reads:` 下(与 `gate` 同源):
出厂内容, 加平台 / 加页面 = 加数据。平台注册表的加载直接复用 `saving_login`, 不另起一份。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _browser_eval
import saving_login as _login

#: 一个选择器槽: 一个字符串, 或按顺序试的候选列表(带构建哈希的类名不稳, 靠候选兜)。
Slot = str | list[str]

# --------------------------------------------------------------------------- #
# 隐私闸门: 绝不抽取收货人 / 支付信息
#
# 结算页同屏渲染收货人姓名、手机号、详细地址、银行卡后四位。规则只有这一份字面量, Python 侧
# 直接用它判定, JS 侧由 _build_js 注入同一份 JSON —— 两半共用一套规则, 不各写一套(写两份的
# 下场是某天只改了一边, 而"只改了一边"在这个场景里就等于漏出一份隐私)。
#
# 标签闸门是主力: 页面把这类内容渲染成**带标签的行**(收货人: / 手机号: / 银行卡:), 而形态二
# 抽的正是带标签的行 —— 标签就是"这是谁的手机号"的证据。值形状闸门是兜底: 标签换了名字, 手机
# 号与卡号的形状还在。
#
# 刻意**不**做地址形状识别(比如"XX省XX路"): 券的适用范围里合法地出现"限北京市"这类文字,
# 按形状挡会把真事实一起挡掉, 而收货地址在页面上本来就是带"地址"标签的一行。
# --------------------------------------------------------------------------- #

_PII_LABELS = (
    "收货人",
    "收货地址",
    "地址",
    "手机号",
    "手机号码",
    "联系电话",
    "电话号码",
    "电话",
    "银行卡",
    "卡号",
    "尾号",
    "身份证",
    "证件号",
    "姓名",
    "联系人",
)

#: 手机号 / 掩码卡号 / 完整卡号 / 身份证。用 `(?:^|[^0-9])` 这类边界而不是前后瞻断言:
#: 前后瞻在老一点的 WebKit 上会直接让整段 JS 抛语法错, 那等于整条读定义全废。
_PII_PATTERNS = (
    r"(?:^|[^0-9])1[3-9][0-9](?:[ -]?[0-9]{4}){2}(?:[^0-9]|$)",
    r"[0-9]{4}[ *]{2,}[0-9]{4}",
    r"(?:^|[^0-9])[0-9]{16,19}(?:[^0-9]|$)",
    r"(?:^|[^0-9])[0-9]{17}[0-9Xx](?:[^0-9]|$)",
)

_PII_RULES = {"labels": list(_PII_LABELS), "patterns": list(_PII_PATTERNS)}
_PII_RES = tuple(re.compile(p) for p in _PII_PATTERNS)

#: 原文回显被整体屏蔽时的占位。**整体**屏蔽而不是逐段替换: 逐段替换要先把"标签 + 内容"切准,
#: 而那正是最容易切漏的地方, 漏一次就是一份隐私进了会话记录。
_PII_ECHO_BLOCKED = "[原始返回命中隐私闸门, 已整体屏蔽]"

# 页面上按声明抽字段的固定运行时。`__SPEC__` / `__PII__` 由 _build_js 换成 JSON 字面量
# (JSON 是 JS 的子集, 所以选择器里的引号由 json.dumps 负责转义, 不用手写拼串)。
_JS_TEMPLATE = """(() => {
  const PII = __PII__;
  const PII_RES = PII.patterns.map((p) => new RegExp(p));
  const cands = (slot) => (Array.isArray(slot) ? slot : [slot]);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  // 一个选择器槽 = 一个字符串, 或按顺序试的候选列表。带构建哈希的类名会随对方重新构建而变,
  // 候选列表就是为它准备的: 首选 miss 就用下一个, 谁先命中用谁。
  const hits = (scope, slot) => {
    for (const s of cands(slot)) {
      const found = scope.querySelectorAll(s);
      if (found.length) return Array.from(found);
    }
    return [];
  };
  const first = (scope, slot) => {
    const found = hits(scope, slot);
    return found.length ? found[0] : null;
  };
  const textOf = (node) => norm(node ? node.textContent : '');
  const pii = (text) => {
    const s = norm(text);
    if (!s) return false;
    for (const w of PII.labels) if (s.indexOf(w) >= 0) return true;
    for (const re of PII_RES) if (re.test(s)) return true;
    return false;
  };
  // 一行键值是一个整体: 标签或值任一命中就整行不要 —— 标签正是"这是谁的手机号"的证据,
  // 留下值等于把隐私带出页面。
  const rowPii = (row) => pii(row.label) || pii(row.value);
  const readFields = (scope, fields, blocked) => {
    const out = {};
    for (const name of Object.keys(fields)) {
      const f = fields[name];
      if (f.presence) {
        const seen = hits(scope, f.sel).length > 0;
        // 判 false 是一条否定断言: 得先看得见"它本该在的那个世界"(evidence)。看不见就只能
        // 算"不知道" —— 不出键, 绝不当成 false。
        if (!seen && f.evidence && !hits(scope, f.evidence).length) continue;
        out[name] = seen;
      } else if (f.many) {
        const kept = [];
        let matched = 0;
        for (const node of hits(scope, f.sel)) {
          matched += 1;
          if (f.kv) {
            const pair = { label: textOf(first(node, f.kv[0])), value: textOf(first(node, f.kv[1])) };
            if (rowPii(pair)) { blocked.n += 1; continue; }
            kept.push(pair);
          } else {
            const text = norm(node.textContent);
            if (pii(text)) { blocked.n += 1; continue; }
            kept.push(text);
          }
        }
        // 命中过但全被挡掉 -> 不出键(缺席 = 不知道), 免得下游把被抹掉的值当成"页面上没有"。
        if (kept.length || !matched) out[name] = kept;
      } else if (f.attr) {
        const node = first(scope, f.sel);
        const value = node ? (node.getAttribute(f.attr) || '') : '';
        if (value && pii(value)) { blocked.n += 1; } else { out[name] = value; }
      } else {
        const value = textOf(first(scope, f.sel));
        if (value && pii(value)) { blocked.n += 1; } else { out[name] = value; }
      }
    }
    return out;
  };
  const run = (spec) => {
    const blocked = { n: 0 };
    const markers = {};
    for (const slot of spec.markers || []) markers[cands(slot)[0]] = hits(document, slot).length;
    const out = { url: location.href, title: document.title, markers: markers };
    if (spec.item) {
      const coupons = [];
      for (const el of hits(document, spec.item)) coupons.push(readFields(el, spec.fields || {}, blocked));
      out.count = coupons.length;
      out.coupons = coupons;
    }
    if (spec.blocks) {
      out.anchors = {};
      out.rows = {};
      out.row_hits = {};
      out.states = {};
      for (const b of spec.blocks) {
        out.anchors[b.name] = hits(document, b.anchor).length;
        // 抽取范围 = 锚点元素**里面**。这不只是"证明区块在", 还是隐私闸门的第一道:
        // 付款区块的行永远够不到同屏的收货人 / 发票模块。
        const root = first(document, b.anchor);
        if (!root) continue;
        if (b.item) {
          const rows = [];
          let matched = 0;
          for (const el of hits(root, b.item)) {
            matched += 1;
            const row = { label: textOf(first(el, b.label)), value: textOf(first(el, b.value)) };
            if (rowPii(row)) { blocked.n += 1; continue; }
            rows.push(row);
          }
          out.rows[b.name] = rows;
          out.row_hits[b.name] = matched;
        }
        if (b.states) out.states[b.name] = readFields(root, b.states, blocked);
      }
    }
    out.blocked = blocked.n;
    return out;
  };
  return JSON.stringify(run(__SPEC__));
})()"""


def _fail(reason: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "reason": reason}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _platform_ref(key: str, name: str) -> dict[str, str]:
    return {"key": key, "name": name}


# --------------------------------------------------------------------------- #
# 隐私闸门
# --------------------------------------------------------------------------- #


def _pii_hit(text: Any) -> bool:
    """这段文本命中隐私闸门吗(标签关键词, 或值形状像手机号 / 卡号)。"""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return False
    if any(label in normalized for label in _PII_LABELS):
        return True
    return any(pattern.search(normalized) for pattern in _PII_RES)


def _screen_row(label: Any, value: Any) -> bool:
    """一行键值要不要整行挡掉 —— 标签 + 值是一体, 任一命中就整行不要。"""
    return _pii_hit(label) or _pii_hit(value)


def _screen_fields(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """把一条事实条目过一遍隐私闸门 -> ``(干净的条目, 挡掉的条数)``。

    标量命中: **整条键不要**(不是留个空串)。留个 `""` 既丢了隐私也造了个假事实 ——
    下游会把"被抹掉"读成"页面上没有"。列表里的键值对按行判定(见 `_screen_row`)。
    """
    out: dict[str, Any] = {}
    blocked = 0
    for name, value in entry.items():
        if isinstance(value, str):
            if _pii_hit(value):
                blocked += 1
                continue
            out[name] = value
        elif isinstance(value, list):
            kept: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    if _pii_hit(item):
                        blocked += 1
                        continue
                    kept.append(item)
                elif isinstance(item, dict):
                    if _screen_row(item.get("label"), item.get("value")):
                        blocked += 1
                        continue
                    kept.append(item)
                else:
                    kept.append(item)
            out[name] = kept
        else:
            out[name] = value
    return out, blocked


def _safe_echo(text: Any) -> str:
    """回显页面原文 / 驱动错误时的兜底: 命中闸门就整体屏蔽。

    这条路径容易被忘掉 —— `unparsable_extract` 会回显 500 字原文, 而"解析失败"恰恰常发生在
    页面结构变形的时刻。整段屏蔽而不是逐段替换, 因为逐段替换要先把"标签 + 内容"切准, 切漏一次
    就是一份隐私进了会话记录。
    """
    raw = str(text or "")
    return _PII_ECHO_BLOCKED if _pii_hit(raw) else raw


# --------------------------------------------------------------------------- #
# 读定义 -> JS
# --------------------------------------------------------------------------- #


def _slot(raw: Any) -> Slot | None:
    """收一个选择器槽: 字符串, 或非空的候选列表。空 / 类型不对 -> None。"""
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, list):
        candidates = [str(item).strip() for item in raw if str(item).strip()]
        return candidates or None
    return None


def _key(slot: Any) -> str:
    """槽位在 `markers` 计数表里的键: 字符串槽位就是它自己, 候选列表用首选。

    用首选而不是整串, 是为了 `missing_markers` 报出来的东西正好是维护者要去改的那一个。
    """
    return slot if isinstance(slot, str) else str(slot[0])


def _fields(raw: Any) -> dict[str, dict[str, Any]]:
    """收一组字段定义(形态一的 `fields` 与区块的 `states` 共用)。写坏了就丢这一条, 不炸整条读定义。"""
    fields: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return fields
    for raw_name, raw_field in raw.items():
        if not isinstance(raw_field, dict):
            continue
        if _slot(raw_field.get("sel")) is None:
            continue
        field = {k: v for k, v in raw_field.items() if k != "note"}
        evidence = _slot(raw_field.get("evidence"))
        if evidence is not None:
            field["evidence"] = evidence
        else:
            field.pop("evidence", None)
        fields[str(raw_name)] = field
    return fields


def _block_spec(raw: Any) -> dict[str, Any] | None:
    """收一个区块定义。**写坏一个区块 = 整条读定义作废**(返回 None)。

    刻意不"丢掉坏的那块、其余照读": 那会交回一条看起来成功、实则少了一整组事实的结果,
    而少掉的那组会被下游当成"页面上没有"。配置错就该在碰页面之前失败。
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    anchor = _slot(raw.get("anchor"))
    if not name or anchor is None:
        return None
    item = _slot(raw.get("item"))
    label = _slot(raw.get("label"))
    value = _slot(raw.get("value"))
    states = _fields(raw.get("states"))
    if item is None:
        # 没有行根的区块 = 纯状态块; 只写了 label / value 就是半截配置。
        if label is not None or value is not None:
            return None
    elif label is None or value is None:
        return None
    if item is None and not states:
        return None
    block: dict[str, Any] = {"name": name, "anchor": anchor}
    if item is not None:
        block.update({"item": item, "label": label, "value": value})
    if states:
        block["states"] = states
    return block


def _blocks(raw: Any) -> list[dict[str, Any]] | None:
    """收 `blocks`。没写 -> None; 写了但有一块收不成 -> None(配置错)。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    blocks = []
    for raw_block in raw:
        block = _block_spec(raw_block)
        if block is None:
            return None
        blocks.append(block)
    return blocks or None


def _spec(read: dict[str, Any]) -> dict[str, Any] | None:
    """把 yaml 里的读定义收成一个干净的 spec; 不完整就返回 None(配置错, 不是页面错)。

    合法 = `markers` 非空, 且**至少声明一种形态**: 形态一要给全(`item` + `fields`), 形态二
    至少一个收得成的区块。半截的形态一(只给 `item` 不给 `fields`, 或反过来)一律算配置错 ——
    拿半截 spec 去读页面, 读回来还会像成功。
    """
    raw_markers = read.get("markers")
    markers = [slot for slot in (_slot(m) for m in (raw_markers if isinstance(raw_markers, list) else [])) if slot]
    item = _slot(read.get("item"))
    fields = _fields(read.get("fields"))
    blocks = _blocks(read.get("blocks"))
    if not markers:
        return None
    if (item is None) != (not fields):
        return None
    if item is None and not blocks:
        return None
    spec: dict[str, Any] = {"markers": markers}
    if item is not None:
        spec["item"] = item
        spec["fields"] = fields
    if blocks:
        spec["blocks"] = blocks
    return spec


def _build_js(spec: dict[str, Any]) -> str:
    return _JS_TEMPLATE.replace("__PII__", json.dumps(_PII_RULES, ensure_ascii=False)).replace(
        "__SPEC__", json.dumps(spec, ensure_ascii=False)
    )


def _balanced_object(text: str, start: int) -> str | None:
    """从 `text[start]` 那个 `{` 起, 按括号配平切出**第一个完整**的 JSON 对象。

    字符串里的括号不算数(引号与反斜杠转义要跳过), 否则 `{"a": "}"}` 会被切坏。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _envelope_body(text: str) -> str:
    """切出 `### Result` 与 `### Ran Playwright code` 之间的那段, 没有这两者就用全文。

    这两段是 Playwright MCP 的回显外壳。**必须先把外壳切掉**: 尾部代码块里全是括号,
    在里面找 JSON 会捞到 `const PII = {...}` 之类的**看起来像结果的错误对象**。
    """
    body = text
    head = body.find("### Result")
    if head >= 0:
        body = body[head + len("### Result") :]
    tail = body.find("### Ran Playwright code")
    if tail >= 0:
        body = body[:tail]
    body = body.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        fence = body.rfind("```")
        if fence >= 0:
            body = body[:fence]
    return body.strip()


def _parse_payload(text: str) -> dict[str, Any] | None:
    """从 MCP 返回的文本里取出那段 JSON。

    结果可能被整体转义成 JSON 字符串再塞进信封(`### Result` 后面是 `"{\\"a\\":1}"`),
    也可能直接就是对象, 所以两种都试。宽松点无害, 但**不能乱猜**: 只在信封体里找,
    且只认括号配平出来的那个第一个对象。
    """
    body = _envelope_body(text)
    candidates = [body]
    if body.startswith('"'):
        try:
            inner = json.loads(body)
        except ValueError:
            pass
        else:
            if isinstance(inner, str):
                candidates.append(inner)

    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        chunk = _balanced_object(candidate, start)
        if chunk is None:
            continue
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# --------------------------------------------------------------------------- #
# 页面身份判定
# --------------------------------------------------------------------------- #


def _verdict(actual_url: Any, definition: dict[str, Any], read: dict[str, Any]) -> tuple[str, str]:
    """判定"现在这一页是不是读定义要的那一页" -> ``(verdict, basis)``。

    verdict ∈ ``on_page`` / ``logged_out`` / ``wrong_page`` / ``no_page``。
    判不准就算 `wrong_page`(保守): 宁可让 agent 再导航一次, 也不要从别的页面上读事实。
    """
    text = str(actual_url or "").strip()
    if not text:
        return "no_page", "empty_url"
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return "no_page", "unparsable_url"
    host = (parsed.hostname or "").lower()
    if not host:
        return "no_page", "no_host"

    login_hosts = [str(h).strip().lower() for h in definition.get("login_hosts") or [] if str(h).strip()]
    if any(host == h or host.endswith("." + h) for h in login_hosts):
        return "logged_out", f"landed_on_login_host:{host}"

    want_host = str(read.get("host") or "").strip().lower()
    if want_host and not (host == want_host or host.endswith("." + want_host)):
        return "wrong_page", f"host:{host}!={want_host}"
    prefix = str(read.get("path_prefix") or "").strip()
    if prefix and not (parsed.path or "/").startswith(prefix):
        return "wrong_page", f"path:{parsed.path}!~{prefix}"
    return "on_page", "matched"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


async def saving_read(platform: str = "", target: str = "", return_json: bool = True) -> str:
    """把浏览器**当前页面**按平台读定义读成结构化事实(京东: 已领券清单 / 结算页)。

    platform: 平台, 可给 key(jd) 或名称(京东)。
    target:   读哪一类页面, 见读定义。留空 = 列出这个平台有哪些可读页面(含该去的 url)。
    return_json: 默认返回 JSON 文本。

    用法: 先用 `browser_navigate` 打开列表里给的 url(需要登录时先走 `saving_login`),
    再调本工具。**本工具不导航** —— 导航要在你能看见登录墙/验证码的前提下做。

    返回的 `ok=true` 才是事实: `empty=true` 表示券包确实为空(模块容器在、里面没券),
    这与"读不到"是两回事。结算页那种页面级的行与区块状态在 `rows` / `states` 里, 同样只在
    `ok=true` 时出现 —— 区块锚点找不到时整条读定义降级成 unknown, **不要**据此告诉用户
    "没有这些行"。

    `ok=false` 一律**不带** facts 字段, 按 reason 处理: 先看那个页面的实际地址, 再决定是去
    登录、去导航, 还是停下告诉用户。

    **不抽收货人 / 支付信息**: 结算页同屏有收货人姓名、手机号、地址、银行卡后四位, 这些一律
    不进返回(`screened_out` 会如实报出被挡掉的条数, 正常页面应当是 0)。

    **不要在这里推断**: 券能不能用在这单上、和国补怎么叠加、到手多少, 都是本体的活。
    本工具只交页面上写着的东西, 负值与币种前缀原样透传。
    """
    # 平台注册表只有一份: 直接复用 saving_login 的加载与归一, 不在这里另起一套 ——
    # 两份平台定义迟早会对不上, 而对不上的表现就是"登录说登了, 读却说不是那一页"。
    registry = await _login._load_platforms()
    if not registry:
        return _fail(f"没有平台定义; 往 {_login._platforms_dir()} 放 <key>.yaml 即可加平台")

    key = _login._resolve_platform(registry, platform)
    if key is None:
        known = sorted(f"{k}({v.get('name') or k})" for k, v in registry.items())
        return _fail(f"未知平台: {platform!r}", known=known)
    definition = registry[key]
    name = str(definition.get("name") or key)

    raw_reads = definition.get("reads")
    reads: dict[str, Any] = raw_reads if isinstance(raw_reads, dict) else {}
    if not reads:
        return _fail(
            f"{name} 还没有可读页面定义(reads:)",
            platform=_platform_ref(key, name),
            note=f"往 {_login._platforms_dir() / (key + '.yaml')} 的 reads: 下加一条即可, 不用改代码。",
        )

    # -- 不指定 target: 列出可读页面 ------------------------------------------
    target_key = (target or "").strip()
    if not target_key:
        targets: list[dict[str, Any]] = []
        for raw_target, raw_def in sorted(reads.items()):
            read_def = raw_def if isinstance(raw_def, dict) else {}
            spec = _spec(read_def)
            gives: list[str] = []
            if spec is None:
                gives = sorted(str(k) for k in (read_def.get("fields") or {}))
            else:
                gives = sorted(spec.get("fields") or {})
                for block in spec.get("blocks") or []:
                    if block.get("item"):
                        gives.append(f"rows.{block['name']}")
                    if block.get("states"):
                        gives.append(f"states.{block['name']}")
                gives.sort()
            targets.append(
                {
                    "target": str(raw_target),
                    "title": str(read_def.get("title") or raw_target),
                    "url": str(read_def.get("url") or ""),
                    "needs_login": bool(definition.get("gate")),
                    "verified_at": str(read_def.get("verified_at") or ""),
                    "gives": gives,
                }
            )
        payload = {
            "ok": True,
            "action": "list",
            "platform": _platform_ref(key, name),
            "targets": targets,
            "gate": str(definition.get("gate") or definition.get("home") or ""),
            "note": (
                "先 browser_navigate 到某个 target 的 url(要登录就先按 gate 走 saving_login), "
                "再带上 target 调一次本工具读事实。"
            ),
        }
        return json.dumps(payload, ensure_ascii=False) if return_json else str(payload)

    raw_read = reads.get(target_key)
    if not isinstance(raw_read, dict):
        known_targets = sorted(str(t) for t in reads)
        return _fail(
            f"{name} 没有名为 {target_key!r} 的读定义",
            platform=_platform_ref(key, name),
            known_targets=known_targets,
        )
    read: dict[str, Any] = raw_read

    spec = _spec(read)
    if spec is None:
        return _fail(
            f"读定义 {key}.{target_key} 不完整(需要 markers + item/fields, 或 markers + blocks)",
            platform=_platform_ref(key, name),
            note="这是配置问题, 不是页面问题: 改平台 yaml, 别去重试页面。",
        )

    blocks: list[dict[str, Any]] = spec.get("blocks") or []
    expected = {
        "url": str(read.get("url") or ""),
        "host": str(read.get("host") or ""),
        "markers": spec["markers"],
        "item": spec.get("item") or "",
        "blocks": [block["name"] for block in blocks],
    }
    verified_at = str(read.get("verified_at") or "")
    source = f"saving_read:{key}:{target_key}"

    # -- 读页面 ---------------------------------------------------------------
    try:
        text = await _browser_eval.evaluate(_build_js(spec))
    except _browser_eval.BrowserGoneError as exc:
        return _fail(
            "browser_closed",
            platform=_platform_ref(key, name),
            target=target_key,
            message=str(exc),
            note=("**停下, 把窗口被关这件事告诉用户, 不要擅自重开。** 用户回复继续之后再重新导航、重新读。"),
        )
    except _browser_eval.BrowserEvalError as exc:
        return _fail(
            "browser_unavailable",
            platform=_platform_ref(key, name),
            target=target_key,
            detail=_safe_echo(exc),
            note='浏览器/驱动这一层没读成。这不是"页面上没有", 别据此下结论; 稍后重试或让用户看窗口状态。',
        )

    data = _parse_payload(text)
    if data is None:
        return _fail(
            "unparsable_extract",
            platform=_platform_ref(key, name),
            target=target_key,
            raw=_safe_echo(text[:500]),
            note="页面上没拿回可解析的结果(可能页面在读取途中跳走了)。重新导航后再读一次。",
        )

    page = {"url": str(data.get("url") or ""), "title": str(data.get("title") or "")}
    verdict, basis = _verdict(page["url"], definition, read)

    if verdict == "logged_out":
        gate = str(definition.get("gate") or definition.get("home") or "")
        return _fail(
            "logged_out",
            platform=_platform_ref(key, name),
            target=target_key,
            basis=basis,
            page=page,
            gate=gate,
            note=(
                "页面被跳到登录域了, 现在读不到券。先按 saving_login 打开 gate 让用户登录, "
                "登录后再导航回 " + expected["url"] + " 重读。"
            ),
        )
    if verdict != "on_page":
        return _fail(
            "wrong_page",
            platform=_platform_ref(key, name),
            target=target_key,
            basis=basis,
            page=page,
            expected=expected,
            note="当前地址不是读定义里的那一页。先 browser_navigate 到 expected.url, 再调一次本工具。",
        )

    # 身份对了。markers 决定"读到 0 条"能不能当事实。
    raw_markers = data.get("markers")
    marker_counts: dict[str, Any] = raw_markers if isinstance(raw_markers, dict) else {}
    missing_markers = [slot for slot in spec["markers"] if not marker_counts.get(_key(slot))]
    if missing_markers:
        return _fail(
            "page_shape_changed",
            platform=_platform_ref(key, name),
            target=target_key,
            basis="missing_markers",
            page=page,
            expected=expected,
            missing_markers=missing_markers,
            note=(
                '页面在了, 但**应当存在的模块容器找不到** —— 这是"我读不到", 不是"没有券"。'
                "不要据此告诉用户券包是空的。请把这一页截图给维护者核(页面结构可能改了), "
                "或先让用户自己看一眼券包。"
            ),
        )

    # 区块锚点与行根: 任何一条声明的锚点找不到, 整条读定义一起降级 —— 半截事实会被当成完整事实。
    raw_anchors = data.get("anchors")
    anchor_counts: dict[str, Any] = raw_anchors if isinstance(raw_anchors, dict) else {}
    raw_hits = data.get("row_hits")
    row_hits: dict[str, Any] = raw_hits if isinstance(raw_hits, dict) else {}
    missing_anchors = {block["name"]: block["anchor"] for block in blocks if not anchor_counts.get(block["name"])}
    missing_rows = [block["name"] for block in blocks if block.get("item") and not row_hits.get(block["name"])]
    if missing_anchors or missing_rows:
        extra: dict[str, Any] = {}
        if missing_anchors:
            extra["missing_anchors"] = missing_anchors
        if missing_rows:
            extra["missing_rows"] = missing_rows
        return _fail(
            "page_shape_changed",
            platform=_platform_ref(key, name),
            target=target_key,
            basis="missing_anchors" if missing_anchors else "missing_rows",
            page=page,
            expected=expected,
            note=(
                '页面在了, 但**某个区块本该存在的锚点 / 行根找不到** —— 这是"我读不到", 不是'
                '"没有这些行"。**不要**据此告诉用户这个区块是空的。区块锚点同时也是抽取范围, '
                "锚点找不到时读出来的行会来自别处 —— 所以整条读定义一起降级, 不给半截事实。"
                "请把这一页截图给维护者核(页面结构可能改了, 或带构建哈希的类名变了)。"
            ),
            **extra,
        )

    raw_blocked = data.get("blocked")
    screened_out = raw_blocked if isinstance(raw_blocked, int) and raw_blocked > 0 else 0

    payload: dict[str, Any] = {
        "ok": True,
        "target": target_key,
        "title": str(read.get("title") or target_key),
        "platform": _platform_ref(key, name),
        "page": page,
        "read_at": _now_iso(),
        "source": source,
        "verified_at": verified_at,
    }

    # -- 形态一: 列表项 -------------------------------------------------------
    count = 0
    if spec.get("item") is not None:
        raw_coupons = data.get("coupons")
        coupons: list[dict[str, Any]] = []
        for raw_coupon in raw_coupons if isinstance(raw_coupons, list) else []:
            if not isinstance(raw_coupon, dict):
                continue
            clean, blocked = _screen_fields(raw_coupon)
            screened_out += blocked
            coupons.append(clean)
        count = len(coupons)
        payload["empty"] = count == 0
        payload["count"] = count
        payload["coupons"] = coupons

    # -- 形态二: 页面级键值行 + 区块状态 --------------------------------------
    if blocks:
        raw_rows_by_block = data.get("rows")
        raw_states_by_block = data.get("states")
        rows_by_block: dict[str, list[dict[str, str]]] = {}
        states_by_block: dict[str, dict[str, Any]] = {}
        for block in blocks:
            block_name = block["name"]
            if block.get("item"):
                raw_rows = raw_rows_by_block.get(block_name) if isinstance(raw_rows_by_block, dict) else None
                rows: list[dict[str, str]] = []
                for raw_row in raw_rows if isinstance(raw_rows, list) else []:
                    if not isinstance(raw_row, dict):
                        continue
                    label, value = str(raw_row.get("label") or ""), str(raw_row.get("value") or "")
                    if _screen_row(label, value):
                        screened_out += 1
                        continue
                    rows.append({"label": label, "value": value})
                if row_hits.get(block_name) and not rows:
                    # 行根命中了, 但每一行都没过隐私闸门 —— 说明声明的选择器打到了隐私模块,
                    # 这是配置错。留个 `rows: []` 会被下游当成"这个区块没有行"。
                    return _fail(
                        "read_blocked_by_pii_gate",
                        platform=_platform_ref(key, name),
                        target=target_key,
                        block=block_name,
                        basis="every_row_screened",
                        page=page,
                        note=(
                            f"区块 {block_name} 的行根命中了, 但每一行都命中隐私闸门(收货人 / 手机号 / "
                            '地址 / 支付信息)被挡掉了。这不是"页面上没有这些行", 而是这条读定义'
                            "声明的选择器太宽或打到了别的模块 —— 请让维护者改读定义, 不要据此对用户下结论。"
                        ),
                    )
                rows_by_block[block_name] = rows
            if block.get("states"):
                raw_states = raw_states_by_block.get(block_name) if isinstance(raw_states_by_block, dict) else None
                states, blocked = _screen_fields(raw_states if isinstance(raw_states, dict) else {})
                screened_out += blocked
                states_by_block[block_name] = states
        if any(block.get("item") for block in blocks):
            payload["rows"] = rows_by_block
        if any(block.get("states") for block in blocks):
            payload["states"] = states_by_block

    payload["screened_out"] = screened_out
    if spec.get("item") is not None and count == 0:
        note = "券包确实是空的(模块容器在, 里面没有券)。这是一条事实, 不是读取失败。"
    else:
        note = "以上是页面上的原始事实。能不能用在这单上、和国补怎么叠加、到手多少, 都交给本体判定 —— 不要在这里推断。"
    if screened_out:
        note += (
            f" 另有 {screened_out} 条命中隐私闸门(收货人 / 手机号 / 地址 / 支付信息), 已在页面层 / "
            "返回层被挡掉, 没有带回来 —— 正常页面应当是 0, 请让维护者核选择器。"
        )
    payload["note"] = note
    return json.dumps(payload, ensure_ascii=False) if return_json else str(payload)
