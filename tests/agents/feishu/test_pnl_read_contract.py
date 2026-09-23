"""台账的分页契约与文档读取的结构化截断。

2026-09-22 实测的两个形状(都来自正负面清单报告这条链):

1. **分页契约断在工具里**。一页 100 条在公开投影里是 **21.8k 字符**, 而框架对单条
   工具结果有 **20k 硬上限**(`history_display.MAX_TOOL_RESULT_CHARS`) —— 于是模型
   拿到的那一页**尾巴被砍掉**, 而工具还 (a) 写死 `page_size=100`、(b) 自己的
   docstring 明说 "never returns a pagination cursor to the chat model"。
   结果是模型既读不完、也没法续读, 只能**从头再读一遍**(实测一轮里
   `positive_negative_case_read` 连调 5 次)。
2. **文档截断按字符切**。`read_document` 把超限正文砍在任意字符位置并附一句
   `[Truncated at 50000 characters]` —— 落在表格中间时, 读者无法区分"表坏了"与
   "我没读全"。云文档 `feishu_doc_read` 更弱: 只给 `truncated: true`。

这里钉住修好之后的形状: 页码可控、游标回传、截断发生在**行边界**且**说清丢了多少**。
用例不碰网络: 台账那条喂一个假的 list_records 客户端, 文档那条自己造 docx。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from docx import Document

TOOLS_DIR = Path(__file__).resolve().parents[3] / "agents" / "feishu" / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_reader = importlib.import_module("_positive_negative_list.reader")
_case_read = importlib.import_module("positive_negative_case_read")
_read_document = importlib.import_module("read_document")


class _FakeClient:
    """`list_records` 的最小替身: 每次回 `page_size` 条, 用 1..N 当游标。"""

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[tuple[int, str]] = []

    async def list_records(self, query, user_key):
        start = int(query.page_token or 0)
        size = int(query.page_size)
        self.calls.append((size, query.page_token))
        end = min(start + size, self.total)
        rows = [
            {
                "记录编号": f"rec-{i}",
                "报告人": "张三",
                "涉事人": "李四",
                "发生时间": "2026-09-02",
                "行为性质": "负面清单",
                "行为事实": "事情描述" * 6,
                "证据状态": "未填写",
                "复盘状态": "待复盘",
                "记录链接": "",
            }
            for i in range(start, end)
        ]
        has_more = end < self.total
        return {"ok": True, "records": rows, "has_more": has_more, "page_token": str(end) if has_more else ""}


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, total: int) -> _FakeClient:
    client = _FakeClient(total)

    class _Adapter:
        def __init__(self) -> None:
            self._client = client

    monkeypatch.setattr(_case_read.runtime, "configured_read_table_adapter", lambda: _Adapter())
    monkeypatch.setattr(_case_read.runtime, "configured_read_view_id", lambda: "")
    monkeypatch.setattr(_case_read.runtime, "read_target_coordinates", lambda: ("app", "tbl"))
    monkeypatch.setattr(
        _case_read.runtime,
        "configured_column_aliases",
        lambda: {},
    )
    monkeypatch.setattr(_reader, "list_table_field_names", lambda *a, **k: _ok_names())
    return client


async def _ok_names():
    return None


async def test_the_page_size_is_the_callers_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型可以要小页: 一页塞不下时它必须能自己收窄, 而不是拿一页被砍掉的 JSON。"""
    _install_fake_client(monkeypatch, total=200)

    raw = await _case_read.positive_negative_case_read(query_json=json.dumps({"page_size": 25}), user_key="")
    result = json.loads(raw)

    assert len(result["记录"]) == 25
    assert result["还有更多"] is True
    assert result["下一页游标"]


async def test_the_page_size_cannot_exceed_the_result_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """页大小有硬顶: 一条记录约 222 字符, 60 条以上就会撞框架的 20k 上限。"""
    client = _install_fake_client(monkeypatch, total=500)

    await _case_read.positive_negative_case_read(query_json=json.dumps({"page_size": 500}), user_key="")

    assert client.calls, "没有发起读取"
    assert client.calls[0][0] == _case_read.MAX_PAGE_SIZE
    assert _case_read.MAX_PAGE_SIZE * 222 < 20_000


async def test_pages_walk_the_whole_ledger_without_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    """游标必须能一路走完: 这是"截断"变成"分页"的关键。"""
    _install_fake_client(monkeypatch, total=98)

    seen: list[str] = []
    token = ""
    for _ in range(10):
        raw = await _case_read.positive_negative_case_read(
            query_json=json.dumps({"page_size": 40, "page_token": token}), user_key=""
        )
        result = json.loads(raw)
        seen.extend(rec["记录编号"] for rec in result["记录"])
        if not result["还有更多"]:
            break
        token = result["下一页游标"]
        assert token, "还有更多 却没有游标 —— 模型无法续读"

    assert len(seen) == 98
    assert len(set(seen)) == 98


async def test_the_cursor_is_absent_exactly_when_the_read_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """读完就必须明确说"读完了" —— 否则模型会怀疑还有下一页。"""
    _install_fake_client(monkeypatch, total=10)

    raw = await _case_read.positive_negative_case_read(query_json="{}", user_key="")
    result = json.loads(raw)

    assert result["还有更多"] is False
    assert result["下一页游标"] == ""
    assert "读完" in result["读取状态"]


def _write_big_table_docx(path: Path, rows: int) -> None:
    document = Document()
    document.add_paragraph("表格保真度测试")
    table = document.add_table(rows=rows + 1, cols=4)
    header = ["姓名", "正向(条次)", "负向(条次)", "说明"]
    for index, name in enumerate(header):
        table.cell(0, index).text = name
    for r in range(rows):
        table.cell(r + 1, 0).text = f"员工{r:03d}"
        table.cell(r + 1, 1).text = str(r % 7)
        table.cell(r + 1, 2).text = str(r % 3)
        table.cell(r + 1, 3).text = "说明文字" * 3
    document.add_paragraph("表后段落: 确认表没有吞掉后面的内容。")
    document.save(str(path))


async def test_a_document_that_fits_is_not_annotated(tmp_path: Path) -> None:
    target = tmp_path / "small.docx"
    _write_big_table_docx(target, rows=3)

    out = await _read_document.read_document(file_path=str(target))

    assert "未列出" not in out, out
    assert "Truncated" not in out, out
    assert "员工002" in out


async def test_a_trimmed_table_ends_on_a_row_boundary(tmp_path: Path) -> None:
    """截断必须切在行边界: 表头留存、列数不变, 而不是把某一行切成半句。"""
    target = tmp_path / "big.docx"
    _write_big_table_docx(target, rows=300)

    out = await _read_document.read_document(file_path=str(target), max_chars=4000)
    table_lines = [line for line in out.splitlines() if line.startswith("|")]

    assert "未列出" in out, "截断了却没说 —— 读者分不清表是坏的还是没读全"
    assert table_lines, "一行表格都没返回"
    assert table_lines[0].startswith("| 姓名")
    assert table_lines[-1].endswith("|"), "最后一行不是完整的一行"
    assert table_lines[-1].count("|") == table_lines[0].count("|"), "最后一行少列了"


async def test_the_omission_note_counts_rows_and_blocks(tmp_path: Path) -> None:
    target = tmp_path / "big2.docx"
    _write_big_table_docx(target, rows=300)

    out = await _read_document.read_document(file_path=str(target), max_chars=4000)
    notes = [line for line in out.splitlines() if line.startswith("[未列出]")]

    assert notes, out[:200]
    joined = " ".join(notes)
    assert ("行" in joined) or ("块" in joined), joined
    assert any(ch.isdigit() for ch in joined), joined
