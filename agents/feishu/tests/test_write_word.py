"""Tests for the Haitun workspace ``write_word`` tool.

The tool's whole reason to exist is the "字体不齐" font bug: a .docx must set the
East-Asian font (``w:eastAsia``), not just the Latin font, or Word renders CJK
glyphs in a fallback typeface. These tests build real .docx files (python-docx
is a hard dependency of the tool) and inspect the OOXML to prove ``w:eastAsia``
is set on the base styles and that ``w:rFonts`` is ordered first per the schema.
"""

from __future__ import annotations

import importlib
import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

tool: Any = importlib.import_module("write_word")


def _styles_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/styles.xml").decode("utf-8")


def _document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


def _style_rpr(styles_xml: str, style_id: str) -> str | None:
    """Return the ``<w:rPr>…</w:rPr>`` block for a given styleId, or None."""
    m = re.search(rf'w:styleId="{style_id}".*?</w:style>', styles_xml, re.S)
    if not m:
        return None
    rp = re.search(r"<w:rPr>.*?</w:rPr>", m.group(0), re.S)
    return rp.group(0) if rp else None


async def test_sets_eastasia_font_on_base_styles(tmp_path: Path) -> None:
    out = tmp_path / "report.docx"
    blocks = [
        {"type": "heading", "level": 1, "text": "概述"},
        {"type": "paragraph", "text": "PyTorch 将在未来五年继续稳坐深度学习框架的王座"},
    ]
    result = await tool.write_word(str(out), json.dumps(blocks), title="2026 报告", cjk_font="微软雅黑")

    assert result.startswith("[OK]")
    assert out.exists()

    styles = _styles_xml(out)
    # Normal, Heading 1, and Title must all carry the East-Asian font.
    for style_id in ("Normal", "Heading1", "Title"):
        rpr = _style_rpr(styles, style_id)
        assert rpr is not None, f"missing rPr for {style_id}"
        assert 'w:eastAsia="微软雅黑"' in rpr, f"{style_id} missing w:eastAsia (would cause 字体不齐)"
        # w:rFonts must be the first child of w:rPr (OOXML schema order).
        first = re.search(r"<w:rPr>\s*<([\w:]+)", rpr)
        assert first is not None and first.group(1) == "w:rFonts", f"{style_id} rFonts not first"


async def test_renders_blocks_and_table(tmp_path: Path) -> None:
    out = tmp_path / "r.docx"
    blocks = [
        {"type": "heading", "level": 2, "text": "生态系统"},
        {"type": "paragraph", "text": "护城河"},
        {"type": "table", "rows": [["月份", "收入"], ["1月", "100"]]},
        {"type": "page_break"},
    ]
    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result == f"[OK] Wrote 4 block(s) to {out}"
    doc_xml = _document_xml(out)
    assert "生态系统" in doc_xml
    assert "护城河" in doc_xml
    assert "月份" in doc_xml and "收入" in doc_xml


async def test_accepts_structured_blocks_without_json_escaping(tmp_path: Path) -> None:
    out = tmp_path / "structured.docx"
    blocks = [{"type": "paragraph", "text": "含有“引号”和换行\n的合同条款"}]

    result = await tool.write_word(str(out), blocks)

    assert result.startswith("[OK]")
    assert out.exists()


async def test_appends_docx_extension(tmp_path: Path) -> None:
    out = tmp_path / "noext"
    result = await tool.write_word(str(out), json.dumps([{"type": "paragraph", "text": "hi"}]))
    assert result.startswith("[OK]")
    assert (tmp_path / "noext.docx").exists()


async def test_rejects_bad_json(tmp_path: Path) -> None:
    result = await tool.write_word(str(tmp_path / "x.docx"), "{not json")
    assert result.startswith("[Error]")
    assert "valid JSON" in result


async def test_rejects_non_array_json(tmp_path: Path) -> None:
    result = await tool.write_word(str(tmp_path / "x.docx"), '{"type": "paragraph"}')
    assert result.startswith("[Error]")


async def test_rejects_empty_without_title(tmp_path: Path) -> None:
    result = await tool.write_word(str(tmp_path / "x.docx"), "[]")
    assert result.startswith("[Error]")


async def test_unknown_block_type_is_reported(tmp_path: Path) -> None:
    result = await tool.write_word(str(tmp_path / "x.docx"), json.dumps([{"type": "bogus"}]))
    assert result.startswith("[Error]")
    assert "bogus" in result


async def test_write_word_from_markdown_converts_long_file(tmp_path: Path) -> None:
    source = tmp_path / "contract.md"
    source.write_text("# 第一章\n\n## 第一条 定义\n\n这是合同正文。", encoding="utf-8")
    out = tmp_path / "contract.docx"

    result = await tool.write_word_from_markdown(str(source), str(out))

    assert result.startswith("[OK]")
    assert out.exists()
    assert "第一章" in _document_xml(out)
    assert "这是合同正文" in _document_xml(out)


# ── chart / image blocks ───────────────────────────────────────────────────────
#
# Why these exist: every Word report used to come out as nothing but a table of
# numbers, because the tool could only express heading/paragraph/table/page_break and
# its own docstring told the model to drop to a raw python-docx script for images —
# which the system prompt forbids. So the "no visuals" behaviour was structural, not
# the model being lazy. These tests pin the way out: a chart is rendered here and the
# picture lands in the document.


def _media_pngs(path: Path) -> list[bytes]:
    """Every PNG embedded in the .docx, as bytes."""
    with zipfile.ZipFile(path) as z:
        return [z.read(n) for n in z.namelist() if n.startswith("word/media/") and n.endswith(".png")]


_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # colour types: grey, RGB, grey+alpha, RGBA


def _decode_pixels(png: bytes) -> tuple[int, int, int, bytes]:
    """Decode an 8-bit PNG to raw samples. Returns (width, height, channels, bytes).

    A naive "decompress the IDAT and grep for three bytes" does not work: PNG applies a
    per-row filter (Sub / Up / Average / Paeth) that leaves the samples transformed, so an
    ordinary colour is simply not present verbatim. Every filter is undone here — the
    alternative is a test that passes or fails depending on which filter the encoder
    happened to pick. matplotlib writes RGBA (colour type 6), hence the channel handling.
    """
    meta: dict[str, int] = {}
    pos, idat = 8, b""
    while pos < len(png):
        length = int.from_bytes(png[pos : pos + 4], "big")
        ctype = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        if ctype == b"IHDR":
            meta = {
                "width": int.from_bytes(data[0:4], "big"),
                "height": int.from_bytes(data[4:8], "big"),
                "depth": data[8],
                "colour": data[9],
                "interlace": data[12],
            }
        elif ctype == b"IDAT":
            idat += data
        pos += 12 + length

    assert meta["depth"] == 8 and meta["colour"] in _PNG_CHANNELS, f"unsupported PNG: {meta}"
    assert meta["interlace"] == 0, "interlaced PNGs are not handled by this decoder"
    channels = _PNG_CHANNELS[meta["colour"]]
    stride = meta["width"] * channels

    raw = zlib.decompress(idat)
    prev = bytearray(stride)
    out = bytearray()
    offset = 0
    for _ in range(meta["height"]):
        ftype = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            upleft = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + left) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + up) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif ftype == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else upleft)
                line[i] = (line[i] + pred) & 0xFF
        out += line
        prev = line
    return meta["width"], meta["height"], channels, bytes(out)


def _has_colour(png: bytes, hex_colour: str) -> bool:
    """Does any opaque pixel equal ``hex_colour`` exactly?"""
    _, _, channels, pixels = _decode_pixels(png)
    rgb = bytes.fromhex(hex_colour.lstrip("#"))
    return any(
        pixels[i : i + 3] == rgb and (channels != 4 or pixels[i + 3] == 0xFF)
        for i in range(0, len(pixels) - channels + 1, channels)
    )


async def test_chart_block_embeds_a_rendered_picture(tmp_path: Path) -> None:
    """A ``chart`` block becomes a real embedded image, not skipped or inlined as text."""
    out = tmp_path / "growth.docx"
    blocks = [
        {"type": "heading", "level": 1, "text": "正负面成长"},
        {
            "type": "chart",
            "kind": "line",
            "title": "正负面累积趋势",
            "data": {"labels_json": ["6月", "7月", "8月"], "series_json": {"正面": [3, 5, 12], "负面": [1, 2, 3]}},
            "caption": "图 1 正负面累积趋势",
        },
    ]

    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result.startswith("[OK]"), result
    doc_xml = _document_xml(out)
    assert "正负面累积趋势" in doc_xml, "the caption must reach the document body"
    assert "<w:drawing>" in doc_xml or "<pic:pic" in doc_xml, "no picture landed in the document"
    assert _media_pngs(out), "chart PNG was not embedded as media"


async def test_chart_colours_apply_in_series_order(tmp_path: Path) -> None:
    """``colors`` maps to series by order — 正面绿 / 负面红, the case that forced it.

    Checked against the rendered pixels rather than the call arguments: the palette
    lookup happens inside the renderer, so only the picture proves the override reached
    matplotlib instead of being accepted and ignored.
    """
    out = tmp_path / "colours.docx"
    blocks = [
        {
            "type": "chart",
            "kind": "line",
            "title": "正负面累积趋势",
            "data": {"labels_json": ["6月", "7月", "8月"], "series_json": {"正面": [3, 5, 12], "负面": [1, 2, 3]}},
            "colors": ["#34C724", "#F5222D"],
        }
    ]

    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result.startswith("[OK]"), result
    png = _media_pngs(out)[0]
    assert _has_colour(png, "#34C724"), "正面 series is not green"
    assert _has_colour(png, "#F5222D"), "负面 series is not red"


async def test_chart_defaults_to_house_palette_without_colours(tmp_path: Path) -> None:
    """No ``colors`` → the same first-two palette slots the Feishu chart tools use."""
    out = tmp_path / "palette.docx"
    blocks = [
        {
            "type": "chart",
            "kind": "line",
            "data": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}},
        }
    ]

    assert (await tool.write_word(str(out), json.dumps(blocks))).startswith("[OK]")
    png = _media_pngs(out)[0]
    assert _has_colour(png, "#3370FF"), "first series should take the Feishu blue"
    assert _has_colour(png, "#FF8800"), "second series should take the amber"


async def test_chart_accepts_the_stringified_spelling(tmp_path: Path) -> None:
    """``data_json`` / ``options_json`` work too — the spelling the chart tools take."""
    out = tmp_path / "stringified.docx"
    blocks = [
        {
            "type": "chart",
            "kind": "column",
            "data_json": json.dumps({"labels_json": ["正面", "负面"], "values_json": [12, 3]}),
            "options_json": json.dumps({"unit": "条"}),
        }
    ]

    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result.startswith("[OK]"), result
    assert _media_pngs(out)


async def test_chart_bad_input_is_reported_with_the_contract(tmp_path: Path) -> None:
    """Bad chart input must fail the write with the contract, not raise a bare error."""
    cases: list[tuple[dict[str, Any], str]] = [
        ({"type": "chart", "kind": "nope"}, "unknown chart kind"),
        ({"type": "chart", "kind": "line", "data": {"labels_json": ["a"]}}, "missing data key"),
        (
            {
                "type": "chart",
                "kind": "line",
                "data": {"labels_json": ["a"], "series_json": {"A": [1]}},
                "options": {"nope": 1},
            },
            "unknown option",
        ),
        (
            {
                "type": "chart",
                "kind": "line",
                "data": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}},
                "colors": ["#34C724"],
            },
            "expected 2 colors",
        ),
        (
            {"type": "chart", "kind": "pie", "data": {"labels_json": ["a"], "values_json": [1]}, "colors": ["#34C724"]},
            "does not take",
        ),
    ]
    for block, needle in cases:
        out = tmp_path / f"bad-{abs(hash(json.dumps(block))) % 9999}.docx"
        result = await tool.write_word(str(out), json.dumps([block]))
        assert result.startswith("[Error]"), f"{block} -> {result}"
        assert needle in result, f"{block} -> {result}"
        assert not out.exists(), "a rejected block must not leave a half-written .docx"


async def test_image_block_inserts_a_local_file(tmp_path: Path) -> None:
    """``image`` block: an existing local picture lands in the document with its caption."""
    png = tmp_path / "shot.png"
    png.write_bytes(_render_png_bytes())

    out = tmp_path / "with-image.docx"
    blocks = [
        {"type": "image", "path": str(png), "caption": "图 1 截图"},
    ]

    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result.startswith("[OK]"), result
    assert "图 1 截图" in _document_xml(out)
    assert _media_pngs(out)


async def test_image_block_missing_path_fails_before_opening_the_document(tmp_path: Path) -> None:
    out = tmp_path / "missing.docx"
    result = await tool.write_word(str(out), json.dumps([{"type": "image", "path": str(tmp_path / "nope.png")}]))

    assert result.startswith("[Error]"), result
    assert "does not exist" in result
    assert not out.exists()


#: Minimal *valid* data per chart kind — one entry per kind in ``_CHART_SPECS``.
#:
#: This table exists because the first version of the chart feature had a defect that no
#: per-shape test caught: ``draw_pie`` is the one renderer that returns ``(draw, folded)``
#: instead of the draw itself (the Feishu tool reports the folded-slice count), so ``pie``
#: and ``donut`` failed with ``TypeError: 'tuple' object is not callable``. The tests
#: above all used a line chart, so 19 of 21 kinds were unexercised and the two broken ones
#: went unnoticed until a real report was built. A table that covers every kind is what
#: makes "I added a chart type" fail loudly instead of silently.
_CHART_SAMPLE_DATA: dict[str, dict[str, Any]] = {
    "line": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2]}},
    "area": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2]}},
    "stacked_area": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}},
    "column": {"labels_json": ["a", "b"], "values_json": [1, 2]},
    "bar": {"labels_json": ["a", "b"], "values_json": [1, 2]},
    "grouped_column": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}},
    "stacked_column": {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}},
    "pareto": {"labels_json": ["a", "b"], "values_json": [5, 3]},
    "pie": {"labels_json": ["a", "b"], "values_json": [3, 2]},
    "donut": {"labels_json": ["a", "b"], "values_json": [3, 2]},
    "funnel": {"stages_json": ["a", "b"], "values_json": [10, 4]},
    "waterfall": {"labels_json": ["a", "b"], "deltas_json": [5, -2]},
    "radar": {"axes_json": ["x", "y", "z"], "series_json": {"A": [1, 2, 3]}},
    # scatter takes *named groups* of points; bubble takes a **plain** array of
    # (x, y, size) rows. Two different parsers behind two similar-looking keys.
    "scatter": {"points_json": {"A": [[1, 2], [2, 3]]}},
    "bubble": {"points_json": [[1, 2, 30], [2, 3, 50]]},
    "box": {"groups_json": {"A": [1, 2, 3, 4]}},
    "histogram": {"values_json": [1, 2, 2, 3, 3, 3, 4]},
    "heatmap": {
        "row_labels_json": ["r1", "r2"],
        "col_labels_json": ["c1", "c2"],
        "values_json": [[1, 2], [3, 4]],
    },
    "combo": {"labels_json": ["a", "b"], "bar_series_json": {"A": [1, 2]}, "line_series_json": {"B": [2, 1]}},
    "gantt": {"tasks_json": [{"name": "T1", "start": "2026-09-01", "end": "2026-09-05"}]},
    "progress": {"items_json": {"A": 118, "B": 92}},
}


def test_every_chart_kind_builds_a_draw() -> None:
    """Every kind in the shared registry must produce a callable draw.

    Catches the two classes of defect a per-shape test cannot: a kind whose renderer
    returns something other than the draw (``pie``/``donut`` return a tuple), and a kind
    whose data shape the adapter table got wrong.
    """
    assert set(_CHART_SAMPLE_DATA) == set(tool._CHART_SPECS), (
        "the sample table and the shared registry disagree — add the new kind's sample data"
    )
    for kind, data in sorted(_CHART_SAMPLE_DATA.items()):
        draw = tool._build_chart_draw({"type": "chart", "kind": kind, "data": data})
        assert callable(draw), f"{kind}: renderer did not return a callable draw"


async def test_every_chart_kind_renders_into_a_document(tmp_path: Path) -> None:
    """And each one actually renders + embeds — building a draw is not the same as drawing.

    One document, not 21: the point is that the whole set survives a real render pass
    through matplotlib, which is where a wrong data shape or a tuple-returning renderer
    finally blows up.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "chart", "kind": kind, "data": data, "title": kind}
        for kind, data in sorted(_CHART_SAMPLE_DATA.items())
    ]
    out = tmp_path / "all-kinds.docx"

    result = await tool.write_word(str(out), json.dumps(blocks))

    assert result.startswith("[OK]"), result
    assert len(_media_pngs(out)) == len(_CHART_SAMPLE_DATA), "every chart must embed its own picture"


def _render_png_bytes() -> bytes:
    """A minimal valid 2x2 PNG, so the image test needs no renderer or fixture file."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))  # two red rows
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
