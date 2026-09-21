"""Versioned, deterministic positive-negative rule and cognition packs."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from psi_agent.session.content_roots import content_roots_from_env as _content_roots_from_env

VALID_DIRECTIONS = frozenset({"positive", "negative", "red_line"})
DEFAULT_VERSION = "6.0-shadow"
# Rules are part of the positive-negative-list skill package, not deployment
# configuration.  Keeping them beside the skill avoids requiring a second
# process configuration file just to classify a private-chat report.
_SKILL_NAME = "positive-negative-list"

#: 单根世界里的老落点。分层未声明时 ``_config_dirs`` 只产出这一个, 与改动前逐字节相同。
_LEGACY_CONFIG_DIR = Path(__file__).resolve().parents[2] / "skills" / _SKILL_NAME

#: 认知口径文件名。刻意与规则包同目录、同一套层解析: 两者都是技能内容, 落在一起才不会
#: 出现"规则读到了就近那层、认知还停在老层"的偏斜 —— 那种偏斜正是认知标准悄悄失守的样子。
_COGNITION_FILE = "cognition.yaml"

#: 认知必须覆盖的三个方向。少一个方向就是口径不完整, 加载即失败关闭。
COGNITION_DIRECTIONS = ("positive", "negative", "red_line")


def _config_dirs() -> list[Path]:
    """规则包可能所在的目录, **就近者在前**。

    内容分层把技能挪出了 ``<workspace>/skills``, 于是原来写死的
    ``parents[2]/"skills"/...`` 断链, 规则包读不到 —— 这个函数按 ``PSI_CONTENT_ROOTS``
    逐层给出候选。

    刻意用同步的 ``content_roots_from_env()`` 而不是工具侧的 ``_content_layers``:
    后者返回 ``anyio.Path``, 会把 ``load_rule_pack`` 这个同步 API 连带染成 async,
    波及它的调用方。层的顺序两处同源(都是声明序反转 + agent 根最近), 这里只是不需要
    异步 IO。

    每次调用时算, 不在 import 期定死: 层来自按进程的环境变量, 而 Gateway 一个进程跑
    很多 Session。
    """
    # 声明序里越靠后越近, 所以反转 —— 与读侧 ``layers_for`` 同一口径。
    dirs = [Path(str(root.path)) / "skills" / _SKILL_NAME for root in reversed(_content_roots_from_env())]
    dirs.append(_LEGACY_CONFIG_DIR)
    return dirs


@dataclass(frozen=True)
class RuleEntry:
    id: str
    direction: str
    category: str
    title: str
    text: str
    mirror_rule_id: str | None
    conditions: tuple[str, ...]
    exceptions: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    source_locator: str
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    red_line_id: str | None = None
    trigger_conditions: tuple[str, ...] = ()
    escalation_conditions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "direction": self.direction,
            "category": self.category,
            "title": self.title,
            "text": self.text,
            "mirror_rule_id": self.mirror_rule_id,
            "conditions": list(self.conditions),
            "exceptions": list(self.exceptions),
            "evidence_requirements": list(self.evidence_requirements),
            "source_locator": self.source_locator,
        }
        if self.red_line_id:
            result["red_line_id"] = self.red_line_id
            result["trigger_conditions"] = list(self.trigger_conditions)
            result["escalation_conditions"] = list(self.escalation_conditions)
        return result


@dataclass(frozen=True)
class RulePack:
    version: str
    entries: tuple[RuleEntry, ...]
    status: str = "shadow"
    source: str = ""

    def query(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or not isinstance(limit, int) or limit <= 0:
            return []
        normalized = _normalize(query)
        cjk = re.findall(r"[\u4e00-\u9fff]", normalized)
        terms = tuple(
            dict.fromkeys((normalized, *normalized.split(), *("".join(cjk[i : i + 2]) for i in range(len(cjk) - 1))))
        )
        scored: list[tuple[int, int, RuleEntry]] = []
        for index, entry in enumerate(self.entries):
            # Red-line escalation is a human process, never an agent-side
            # judgement: red-line entries stay out of the model query surface.
            if entry.direction == "red_line":
                continue
            haystack = _normalize(
                " ".join((entry.id, entry.title, entry.text, entry.category, *entry.keywords, *entry.aliases))
            )
            score = sum(2 if term and term in _normalize(entry.title) else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, index, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [entry.as_dict() for _, _, entry in scored[:limit]]


@dataclass(frozen=True)
class CognitionPack:
    """正负面清单的关系认知: 清单是什么、正负面怎么读、报告怎么写。

    这份口径**不是**代码里的常量。它与规则包同目录、同层解析, 由 ``positive_negative_rules``
    与 ``positive_negative_case_analyze`` 读取后返回。这样做有两个直接后果: 改口径只改
    ``cognition.yaml`` 一处, 不碰代码; "本轮读到的口径是哪一份字节"可以用
    ``cognition_source()`` 的指纹证明, 而不必靠谁记得改过哪一行。

    认知标准的关键一条是**正负面都算同一个人的成长**: 正向是履行创业者身份、值得被看见,
    负向是信任基础被破坏、需要被纠正, 纠偏拿到的边界与改正路径同样是成长值。把清单讲成
    "表扬 vs 批评"或"加分 vs 扣分"就是这个包要防的那个缺陷, 所以它必须可版本化、可追溯。
    """

    version: str
    premise: str
    order: str
    source_document: dict[str, str]
    relationship: dict[str, Any]
    reading: dict[str, dict[str, str]]
    stance: tuple[dict[str, str], ...]
    ledger_discipline: tuple[str, ...]
    report_rules: tuple[str, ...]
    disclosure: str
    notice: dict[str, str]

    def notice_line(self, direction: str) -> str:
        """员工侧通知卡的一句说辞, 按方向取; 缺失即 ``ValueError``。

        卡片由代码确定性渲染, 拿不到 ``positive_negative_rules`` 的返回, 所以这一句是它唯一
        的口径来源。刻意**不给兜底文案** —— 兜底就等于在代码里偷偷留下第二份口径, 而"负面卡
        只列整改三条、没有成长框架"正是这次要修的那个读法。
        """
        line = self.notice.get(direction, "")
        if not line:
            raise ValueError(f"cognition pack notice missing: {direction}")
        return line

    def as_dict(self) -> dict[str, Any]:
        """完整口径, 含版本与出处。给需要判断依据的调用方。"""
        return {
            "version": self.version,
            "premise": self.premise,
            "order": self.order,
            "source_document": dict(self.source_document),
            "relationship": self.relationship,
            "reading": {direction: dict(entry) for direction, entry in self.reading.items()},
            "stance": [dict(item) for item in self.stance],
            "ledger_discipline": list(self.ledger_discipline),
            "report_rules": list(self.report_rules),
            "disclosure": self.disclosure,
            "notice": dict(self.notice),
        }

    def report_view(self) -> dict[str, Any]:
        """面向用户的投影: 只有中文业务表述, 不带版本号、内部字段名或文件路径。

        汇总结果会直接进对话, 所以这里刻意**不复用** ``as_dict()`` —— 那条路会把 ``version``
        之类的内部标识带进用户可见的返回。也刻意**不在代码里拼句子**: 每一条的正文原样来自
        ``cognition.yaml``, 代码只换一层中文键名。拼句子意味着标点与措辞住进代码, 而口径
        恰恰是最常被改的那部分。
        """
        return {
            "认知口径": {
                "前提": self.premise,
                "关系": self.relationship,
                "正向清单": dict(self.reading.get("positive", {})),
                "负面清单": dict(self.reading.get("negative", {})),
                "红线": dict(self.reading.get("red_line", {})),
                "台账纪律": list(self.ledger_discipline),
                "报告怎么写": list(self.report_rules),
            },
            "说明": self.disclosure,
        }


def _layer_name_of(path: Path) -> str:
    """``path`` 命中的内容层名; 落在老落点上返回 ``"legacy"``。

    按 ``_config_dirs()`` 同一份候选倒查, 而不是去解析路径字符串 —— 层的根可以是任意目录,
    从路径反推层名迟早会猜错。
    """
    for root in reversed(_content_roots_from_env()):
        if path.parent == Path(str(root.path)) / "skills" / _SKILL_NAME:
            return root.name
    return "legacy"


def _skill_file(filename: str) -> Path | None:
    """技能目录下的一个文件, 按内容层就近解析; 全层未命中返回 ``None``。"""
    return next((d / filename for d in _config_dirs() if (d / filename).is_file()), None)


def _rule_pack_path(version: str) -> Path:
    """规则包文件, 按内容层就近解析; 全层未命中抛 ``ValueError``。

    ``load_rule_pack`` 与 ``rule_pack_source`` **必须共用这一个解析口**: 后者返回的是
    "我确实读了这份文件"的指纹, 一旦两处各自解析、落到不同层的同名文件上, 指纹就成了伪证 ——
    它会为一份没被读过的文件作保。共用之后这种偏斜在结构上不可能出现。
    """
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:-[a-z0-9-]+)?", version):
        raise ValueError("invalid rule pack version")
    path = _skill_file(f"{version}.yaml")
    if path is None:
        raise ValueError(f"unknown rule pack version: {version}")
    return path


def _cognition_path() -> Path:
    """认知口径文件, 与规则包共用同一个解析口。

    缺失时**失败关闭**, 不给兜底: 没有这份口径, "正负面都是成长"就没有判据, 让工具带着一份
    代码里自造的认知继续跑, 正是这次要修的那个缺陷。错误文案刻意不带路径 —— 这个模块的错误
    会原样进 agent 的返回, 服务器目录结构不该跟着出去。
    """
    path = _skill_file(_COGNITION_FILE)
    if path is None:
        raise ValueError("cognition pack unavailable")
    return path


def load_rule_pack(version: str = DEFAULT_VERSION) -> RulePack:
    raw = yaml.safe_load(_rule_pack_path(version).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != version:
        raise ValueError("rule pack version metadata mismatch")
    entries: list[RuleEntry] = []
    for item in raw.get("entries", []):
        if not isinstance(item, dict):
            raise ValueError("rule entry must be an object")
        entries.append(_entry_from_mapping(item))
    pack = RulePack(
        version=version, entries=tuple(entries), status=str(raw.get("status", "")), source=str(raw.get("source", ""))
    )
    validate_rule_pack(pack)
    return pack


def query_rules(query: str, version: str = DEFAULT_VERSION, limit: int = 8) -> list[dict[str, Any]]:
    return load_rule_pack(version).query(query, limit)


def _skill_fingerprint(path: Path, relative: str) -> dict[str, Any]:
    """技能文件的来源指纹: ``{file, layer, sha256, bytes}``。"""
    raw = path.read_bytes()
    return {
        "file": relative,
        "layer": _layer_name_of(path),
        "sha256": hashlib.sha256(raw).hexdigest()[:12],
        "bytes": len(raw),
    }


def rule_pack_source(version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """规则包来源指纹: ``{file, sha256, bytes}``。

    为什么要有它: SKILL 要求 agent 在"本轮确实重读了、内容未变"时给出证据, 而此前
    **没有任何工具返回规则文件指纹** —— agent 只能自己 ``md5sum
    skills/positive-negative-list/<version>.yaml``, 那正是"用 bash 去补工具缺口"的
    典型来源(实测 P25)。把指纹放回工具返回里, 这条路就不需要了。

    ``file`` 保持"相对技能根"的短路径, 便于在原句里引用, 也不把服务器目录结构泄进 agent 的
    输出。但分层之后**光有它不足以定位文件**: 同名 ``<version>.yaml`` 在每层都可能存在, 只报
    这一个字段会让 ``file`` 读着像 agent 包那份、``sha256`` 其实来自 ``official`` 层 —— 自相
    矛盾, 且恰好骗过"报了路径就算有据"的读法。所以另给 ``layer``: 命中层的名字(未声明分层时是
    ``legacy``, 与改动前的单根世界对应)。

    ``sha256`` 只取前 12 位 —— 够区分且好念。刻意**不**返回全文: 这个字段是给"比对"用的,
    不是给"读"用的。
    """
    return _skill_fingerprint(_rule_pack_path(version), f"skills/{_SKILL_NAME}/{version}.yaml")


def cognition_source() -> dict[str, Any]:
    """认知口径来源指纹, 形状与 ``rule_pack_source`` 相同。

    认知也要指纹, 理由和规则包一样: SKILL 要求 agent 在"本轮确实重读了、口径未变"时给出
    证据, 而口径文本随时可能被改。只报文件名证明不了读的是哪一层的哪一份字节, 所以
    ``layer`` 与 ``sha256`` 一起给。
    """
    return _skill_fingerprint(_cognition_path(), f"skills/{_SKILL_NAME}/{_COGNITION_FILE}")


def validate_rule_pack(pack: RulePack) -> None:
    if not isinstance(pack, RulePack) or not pack.version:
        raise ValueError("invalid rule pack")
    if any(entry.direction not in VALID_DIRECTIONS for entry in pack.entries):
        raise ValueError("direction must be positive, negative, or red_line")
    ids = [entry.id for entry in pack.entries]
    if len(ids) != len(set(ids)):
        raise ValueError("rule IDs must be unique")
    by_id = {entry.id: entry for entry in pack.entries}
    for entry in pack.entries:
        if not all((getattr(entry, field) or ()) for field in ("conditions", "exceptions", "evidence_requirements")):
            raise ValueError(f"rule fields missing: {entry.id}")
        if entry.direction == "red_line":
            if (
                entry.mirror_rule_id
                or not entry.red_line_id
                or not entry.trigger_conditions
                or not entry.escalation_conditions
            ):
                raise ValueError(f"red line entry invalid: {entry.id}")
        else:
            if entry.red_line_id or entry.trigger_conditions or entry.escalation_conditions:
                raise ValueError(f"ordinary entry carries red-line metadata: {entry.id}")
            mirror = by_id.get(entry.mirror_rule_id or "")
            if mirror is None or mirror.mirror_rule_id != entry.id or mirror.direction == entry.direction:
                raise ValueError(f"mirror invalid: {entry.id}")


def _entry_from_mapping(item: dict[str, Any]) -> RuleEntry:
    def seq(name: str) -> tuple[str, ...]:
        value = item.get(name, ())
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"{name} must be a non-empty string list")
        return tuple(value)

    required = ("id", "direction", "category", "title", "text", "source_locator")
    if any(not isinstance(item.get(name), str) or not item[name] for name in required):
        raise ValueError("rule entry required fields missing")
    return RuleEntry(
        id=item["id"],
        direction=item["direction"],
        category=item["category"],
        title=item["title"],
        text=item["text"],
        mirror_rule_id=item.get("mirror_rule_id"),
        conditions=seq("conditions"),
        exceptions=seq("exceptions"),
        evidence_requirements=seq("evidence_requirements"),
        source_locator=item["source_locator"],
        keywords=seq("keywords") if item.get("keywords") else (),
        aliases=seq("aliases") if item.get("aliases") else (),
        red_line_id=item.get("red_line_id"),
        trigger_conditions=seq("trigger_conditions") if item.get("trigger_conditions") else (),
        escalation_conditions=seq("escalation_conditions") if item.get("escalation_conditions") else (),
    )


def load_cognition_pack() -> CognitionPack:
    """读取认知口径。文件缺失即失败关闭(见 ``_cognition_path``)。"""
    raw = yaml.safe_load(_cognition_path().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("cognition pack must be a mapping")
    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("cognition pack version missing")
    reading = raw.get("reading")
    if not isinstance(reading, dict):
        raise ValueError("cognition pack reading missing")
    pack = CognitionPack(
        version=version,
        premise=str(raw.get("premise") or ""),
        order=str(raw.get("order") or ""),
        source_document=_string_mapping("source_document", raw.get("source_document")),
        relationship=_object_mapping("relationship", raw.get("relationship")),
        reading=_reading_mapping(reading),
        stance=_object_sequence("stance", raw.get("stance")),
        ledger_discipline=_text_sequence("ledger_discipline", raw.get("ledger_discipline")),
        report_rules=_text_sequence("report_rules", raw.get("report_rules")),
        disclosure=str(raw.get("disclosure") or ""),
        notice=_string_mapping("notice", raw.get("notice")),
    )
    validate_cognition_pack(pack)
    return pack


def validate_cognition_pack(pack: CognitionPack) -> None:
    """口径完整性: 少一条就失败关闭, 不让残缺的口径被当成完整口径用。

    刻意**不**给缺项兜底默认值。缺一角的认知 (例如只写了正向怎么写、没写负向同样算成长)
    恰恰会复现这次要修的那个缺陷, 而一个补了默认值的加载器会让它静默通过。
    """
    if not isinstance(pack, CognitionPack):
        raise ValueError("invalid cognition pack")
    missing = [name for name in ("version", "premise", "order", "disclosure") if not getattr(pack, name)]
    if missing:
        raise ValueError(f"cognition pack fields missing: {', '.join(missing)}")
    if not pack.relationship or not pack.ledger_discipline or not pack.report_rules:
        raise ValueError("cognition pack relationship, ledger discipline, or report rules missing")
    for direction in COGNITION_DIRECTIONS:
        entry = pack.reading.get(direction)
        if not entry:
            raise ValueError(f"cognition pack reading missing: {direction}")
        for field in ("label", "translates", "organizational_response", "growth"):
            if not entry.get(field):
                raise ValueError(f"cognition pack reading incomplete: {direction}/{field}")
    # 通知卡两个方向各要一句: 只有正向那句时, 负面卡会退回"只列整改三条"。
    for direction in ("positive", "negative"):
        if not pack.notice.get(direction, ""):
            raise ValueError(f"cognition pack notice missing: {direction}")


def _string_mapping(name: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise ValueError(f"{name} must map strings to non-empty strings")
        result[key] = item
    return result


def _object_mapping(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    return value


def _object_sequence(name: str, value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or not item:
            raise ValueError(f"{name} entries must be non-empty mappings")
        entry: dict[str, str] = {}
        for key, text in item.items():
            if not isinstance(key, str) or not isinstance(text, str) or not text:
                raise ValueError(f"{name} entries must map strings to non-empty strings")
            entry[key] = text
        items.append(entry)
    return tuple(items)


def _text_sequence(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a non-empty string list")
    return tuple(value)


def _reading_mapping(value: dict[str, Any]) -> dict[str, dict[str, str]]:
    reading: dict[str, dict[str, str]] = {}
    for direction, entry in value.items():
        if not isinstance(direction, str) or not isinstance(entry, dict) or not entry:
            raise ValueError("cognition reading entries must be non-empty mappings")
        fields: dict[str, str] = {}
        for key, text in entry.items():
            if not isinstance(key, str) or not isinstance(text, str) or not text:
                raise ValueError(f"cognition reading must map strings to non-empty strings: {direction}")
            fields[key] = text
        reading[direction] = fields
    return reading


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
