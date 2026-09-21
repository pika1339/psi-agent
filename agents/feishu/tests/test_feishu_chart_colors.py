"""`colors` on the feishu_chart tool — reaching the draw, and the two ways it can be wrong.

Why this file exists: on 2026-09-21 the agent needed 正面绿 / 负面红 on a bar chart, found
that `feishu_chart` exposed no colour parameter, and therefore wrote a raw matplotlib script
instead. That script drew the chart but lost every layout guard the renderer carries
(``constrained_layout``, tick thinning, label tilting, row-label sizing) — the delivered
image had labels wedged against the plot border, and catching that cost a round trip.

So the assertion that matters is not "the parameter is accepted" but **the colour reaches
matplotlib through the real dispatcher**. A parameter that parses and is then dropped looks
identical from the outside, which is exactly the failure this test is here to prevent.

Tools are reached through ``importlib`` (not plain imports) because they live in the agent
package, not on the import path — same pattern as ``test_feishu_chart.py`` next door, and it
keeps ``ty check`` from resolving modules it cannot see.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # before pyplot is imported: these tests render off-screen
import matplotlib.pyplot as plt
from matplotlib.colors import to_hex

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_cr: Any = importlib.import_module("_chart_render")
_place: Any = importlib.import_module("_chart_place")
_chart: Any = importlib.import_module("feishu_chart")

#: The two colours the 2026-09-21 request needed. The renderer's own palette also defines
#: these, but they are spelled out here so a palette edit cannot silently change the test.
GREEN, RED = "#34C724", "#F5222D"

#: (chart_type, data, expected series count)
SERIES_CASES = [
    ("line", {"labels_json": ["a", "b"], "series_json": {"正面": [1, 2], "负面": [2, 1]}}, 2),
    ("area", {"labels_json": ["a", "b"], "series_json": {"正面": [1, 2], "负面": [2, 1]}}, 2),
    ("stacked_area", {"labels_json": ["a", "b"], "series_json": {"正面": [1, 2], "负面": [2, 1]}}, 2),
    ("grouped_column", {"labels_json": ["a", "b"], "series_json": {"正面": [1, 2], "负面": [2, 1]}}, 2),
    ("stacked_column", {"labels_json": ["a", "b"], "series_json": {"正面": [1, 2], "负面": [2, 1]}}, 2),
    ("column", {"labels_json": ["a", "b"], "values_json": [1, 2]}, 1),
    ("bar", {"labels_json": ["a", "b"], "values_json": [1, 2]}, 1),
]


def _hex(colour: Any) -> str:
    """matplotlib passes RGBA tuples as well as strings; normalise on lowercase hex.

    ``get_facecolor()`` is typed as possibly a *sequence* of colours (the shape
    ``fill_between`` produces), so the first row is taken when it is one.
    """
    if isinstance(colour, (list, tuple)) and colour and isinstance(colour[0], (list, tuple)):
        colour = colour[0]
    return str(to_hex(colour)).lower()


class _CaptureDraw:
    """Run the tool with `_place.place` stubbed, so nothing talks to Feishu."""

    def __init__(self) -> None:
        self.draw: Any = None
        self.original: Any = _place.place

    def __enter__(self) -> _CaptureDraw:
        async def fake_place(draw: Any, **_kw: Any) -> str:
            self.draw = draw
            return "PLACED"

        _place.place = fake_place
        return self

    def __exit__(self, *_exc: object) -> None:
        _place.place = self.original

    def colours(self) -> list[str]:
        """The drawn series colours, read back from a real (throwaway) render.

        Which artist carries the colour depends on the chart family, so all three are
        tried: lines (line/area), collections (stacked_area fills via ``fill_between``),
        and patches (the bar family). Reading only one of them silently reports an empty
        list for the others — which is how `stacked_area` first "failed" here.
        """
        _cr._apply_style()
        fig, ax = plt.subplots()
        try:
            self.draw(fig, ax)
            lines = [_hex(ln.get_color()) for ln in ax.get_lines()]
            if lines:
                return lines
            fills = [_hex(c.get_facecolor()) for c in ax.collections if len(c.get_facecolor())]
            if fills:
                return fills
            # the bar family draws patches; a single series makes them all one colour
            return sorted({_hex(p.get_facecolor()) for p in ax.patches})
        finally:
            plt.close(fig)


async def _run(chart_type: str, data: dict[str, Any], options: dict[str, Any] | None) -> tuple[str, _CaptureDraw]:
    with _CaptureDraw() as cap:
        out: str = await _chart.feishu_chart(
            chart_type=chart_type,
            data_json=json.dumps(data, ensure_ascii=False),
            options_json=json.dumps(options, ensure_ascii=False) if options else "",
        )
    return out, cap


async def test_colours_reach_the_draw_for_every_colour_capable_kind() -> None:
    """Every kind advertising `colors` must actually apply it.

    The table's coverage is checked against `_COLOR_KINDS` by the last test in this file,
    so adding a kind to the renderer without exercising it here fails loudly.
    """
    for chart_type, data, n_series in SERIES_CASES:
        wanted = [GREEN, RED][:n_series]
        out, cap = await _run(chart_type, data, {"colors": wanted})
        assert out == "PLACED", f"{chart_type}: dispatch failed — {out}"
        expected = wanted if n_series > 1 else sorted(set(wanted))
        assert cap.colours() == [c.lower() for c in expected], (
            f"{chart_type}: colours did not reach the draw ({cap.colours()})"
        )


async def test_the_case_that_forced_this_green_positive_red_negative() -> None:
    """正面绿 / 负面红 in series order — the exact need that produced the hand-rolled script."""
    data = {"labels_json": ["6月", "7月"], "series_json": {"正面": [3, 5], "负面": [4, 3]}}
    out, cap = await _run("line", data, {"colors": [GREEN, RED]})

    assert out == "PLACED"
    assert cap.colours() == [GREEN.lower(), RED.lower()], "the colours must follow series order"


async def test_omitting_colours_keeps_the_house_palette() -> None:
    """No `colors` must behave exactly as before this change — the default path is untouched."""
    data = {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}}
    out, cap = await _run("line", data, None)

    assert out == "PLACED"
    assert cap.colours() == [c.lower() for c in _cr._colors(2)]


async def test_wrong_colour_count_is_reported_not_padded() -> None:
    """A short list must fail loudly; padding would leave the tail on palette colours."""
    data = {"labels_json": ["a", "b"], "series_json": {"A": [1, 2], "B": [2, 1]}}
    out, _ = await _run("line", data, {"colors": [GREEN]})

    assert "expected 2 colors, got 1" in out, out


async def test_colours_on_a_kind_that_does_not_support_them_is_refused() -> None:
    """pie/donut colour their *slices*, not their series — accepting `colors` would lie."""
    out, _ = await _run("pie", {"labels_json": ["a", "b"], "values_json": [1, 2]}, {"colors": [GREEN, RED]})

    assert "does not take" in out, out


async def test_spec_coverage_matches_the_renderer() -> None:
    """The tool surface and the renderer must agree on which kinds take colours.

    `_CHART_SPECS[<kind>]["opts"]` is what the dispatcher validates against, so a kind in
    `_COLOR_KINDS` but missing from `opts` is unreachable, while one in `opts` without
    renderer support would be accepted and then ignored.
    """
    declared = {kind for kind, spec in _chart._CHART_SPECS.items() if "colors" in spec["opts"]}
    covered = {kind for kind, _data, _n in SERIES_CASES}

    assert declared == set(_cr._COLOR_KINDS), (
        "spec opts and _COLOR_KINDS disagree — "
        f"only in specs: {sorted(declared - set(_cr._COLOR_KINDS))}, "
        f"only in renderer: {sorted(set(_cr._COLOR_KINDS) - declared)}"
    )
    assert covered == declared, f"this file does not exercise every colour-capable kind: {sorted(declared - covered)}"
