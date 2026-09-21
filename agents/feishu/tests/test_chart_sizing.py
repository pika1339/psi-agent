"""Content-aware canvas sizing for row-labelled charts.

Why this file exists: the base canvas was a fixed 8x4.5in = 1600x900px. Measured on the
2026-09-21 全员成长 chart, 38 people in that canvas left **15.8px of row pitch** while one
13pt CJK line box needs **52px** — about 11 rows could be labelled. The renderer's response
was ``_thin_ticks``, i.e. *drop* the labels, so a 38-person chart silently showed a dozen
names and nobody was told the other 26 existed.

So the canvas now grows with the data. Two properties have to hold together, and the second
is easy to break while fixing the first:

* **It must grow** — every row label present, at full type size.
* **It must stay predictable** — the crop-to-content failure this repo already paid for
  produced *26 distinct sizes across 54 charts*, and the narrow ones rendered as thumbnails
  in the Feishu doc, because a Feishu image block is shown at the PNG's own pixel size.

Hence a pure function of the data shape, not of the rendered ink.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # before pyplot is imported: these tests render off-screen
import matplotlib.pyplot as plt
from PIL import Image

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_cr: Any = importlib.import_module("_chart_render")

NAMES_3 = ["赵胜迪", "潘逸轩", "郑淳"]
NAMES_38 = [f"员工{i:02d}" for i in range(38)]
#: One 13pt CJK line box, in pixels — the amount of vertical room a row label needs.
_LINE_BOX_PX = 13.0 * 1.45 * _cr._DPI / 72.0


def _size(rows: int, longest: str, *, horizontal: bool) -> tuple[float, float]:
    return _cr._row_aware_size(rows, longest, horizontal=horizontal)


async def _render_size(names: list[str], *, horizontal: bool) -> tuple[int, int]:
    """Render a stacked bar chart offline and return the PNG's pixel size."""
    values = list(range(1, len(names) + 1))
    draw = _cr.draw_bar(
        names,
        [("正面", values), ("负面", [1] * len(names))],
        title="全员有效成长",
        stacked=True,
        horizontal=horizontal,
    )
    out = Path(tempfile.mkdtemp(prefix="psi-size-")) / "c.png"
    try:
        await _cr.render_to_png(draw, str(out))
        with Image.open(out) as im:
            return im.size
    finally:
        plt.close("all")


def test_single_row_charts_keep_the_base_canvas() -> None:
    """Non-row charts and single-series charts must keep the size delivery was tuned for."""
    assert _size(1, "", horizontal=True) == (_cr._FIG_W, _cr._FIG_H)
    assert _size(0, "", horizontal=True) == (_cr._FIG_W, _cr._FIG_H)
    # the one-row case is the base canvas; multi-row vertical charts grow in height only
    assert _size(1, "赵胜迪", horizontal=False) == (_cr._FIG_W, _cr._FIG_H)


def test_height_grows_with_the_row_count() -> None:
    """More rows must mean more canvas, monotonically, until the cap."""
    heights = [_size(n, "赵胜迪", horizontal=True)[1] for n in (1, 3, 12, 24, 38)]

    assert heights == sorted(heights), f"height must not shrink as rows grow: {heights}"
    assert heights[-1] > _cr._FIG_H * 2, f"38 rows must be far taller than the base canvas: {heights[-1]}"


def test_height_is_enough_for_a_full_line_box_per_row() -> None:
    """The point of growing: each row gets room for a 13pt CJK line box.

    The measured 2026-09-21 failure was 15.8px of pitch against a 52px line box. This pins
    the arithmetic that produced it, so a later tweak to the pitch constants has to face the
    number it is changing.
    """
    height_in = _size(38, "赵胜迪", horizontal=True)[1]
    band_in = height_in - _cr._TITLE_LEGEND_IN - _cr._AXIS_IN  # canvas minus fixed furniture
    pitch_px = band_in * _cr._DPI / 38

    assert pitch_px >= _LINE_BOX_PX, f"38 rows get {pitch_px:.1f}px of pitch, need {_LINE_BOX_PX:.1f}px"


def test_width_grows_only_for_the_label_gutter() -> None:
    """A horizontal chart earns width for its longest label; a vertical one does not.

    Width is the expensive dimension — a wider PNG is scaled down harder on the page, so
    every glyph shrinks. Only the gutter is allowed to grow.
    """
    narrow = _size(38, "张三", horizontal=True)[0]
    wide = _size(38, "一个非常非常长的部门名称", horizontal=True)[0]

    assert wide > narrow, "a longer label must earn a wider gutter"
    assert _size(38, "赵胜迪", horizontal=False)[0] == _cr._FIG_W, "vertical charts keep the base width"


def test_growth_is_capped() -> None:
    """An extreme input must not produce an unbounded canvas."""
    w, h = _size(10_000, "超" * 200, horizontal=True)

    assert w <= _cr._MAX_W + 1e-9, f"width {w} exceeds the cap"
    assert h <= _cr._MAX_H + 1e-9, f"height {h} exceeds the cap"


def test_same_shape_always_gives_the_same_size() -> None:
    """Determinism is what separates this from the crop-to-content thumbnail failure.

    26 distinct sizes came from measuring *ink*; this measures the *data shape*, so the same
    shape lands on the same canvas every time — including at the cap.
    """
    assert _size(38, "赵胜迪", horizontal=True) == _size(38, "赵胜迪", horizontal=True)
    assert _size(300, "赵胜迪", horizontal=True) == _size(300, "赵胜迪", horizontal=True)


async def test_many_rows_render_taller_end_to_end() -> None:
    """The size function's answer must actually reach the PNG."""
    small = await _render_size(NAMES_3, horizontal=True)
    large = await _render_size(NAMES_38, horizontal=True)

    assert large[1] > small[1], f"38 rows must render taller than 3 ({large[1]} vs {small[1]})"
    assert large[0] == round(_size(38, max(NAMES_38, key=len), horizontal=True)[0] * _cr._DPI), (
        f"rendered width {large[0]} does not match the computed size"
    )


async def test_vertical_columns_are_not_made_taller_by_category_count() -> None:
    """A vertical chart's categories sit on the x axis; growing height there would be wrong."""
    _w3, h3 = await _render_size(NAMES_3, horizontal=False)
    _w38, h38 = await _render_size(NAMES_38, horizontal=False)

    assert h3 == h38, f"vertical charts must keep one height regardless of category count ({h3}, {h38})"
