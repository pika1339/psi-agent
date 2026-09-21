"""Write-Word tool - create real .docx reports with consistent Chinese fonts.

The recurring "字体不齐" (uneven font) bug comes from writing a .docx whose runs
only set the Latin font (``w:ascii``/``w:hAnsi``) and never the East-Asian font
(``w:eastAsia``); Word then renders CJK glyphs in its default East-Asian face, so
some Chinese characters fall back to a different typeface. This tool builds the
document from structured content and sets ``w:eastAsia`` on every base style once,
so all text — paragraphs, headings, and table cells — is consistent by default.

``chart`` and ``image`` blocks render through the same matplotlib pipeline the
Feishu chart tools use (``_chart_render``), so a Word report can carry the same
visuals a Feishu doc would. Chart PNGs are rendering scaffolding: they live in a
temp dir and are removed once the document is saved, since python-docx embeds the
image bytes into the .docx.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import _chart_render as _cr
import _runtime_paths as _paths
import anyio
import feishu_chart as _fc
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches

#: Rendered chart width in inches. The renderers emit a fixed 1600x900-ish canvas, which
#: at 96 dpi is wider than a portrait A4 text column (~6.3in), so an unconstrained
#: ``add_picture`` would overflow the margin. 6.0 keeps it inside and is fixed rather
#: than computed so every chart in a document lines up.
_CHART_WIDTH_IN = 6.0

#: Each chart's data/option contract. Read from ``feishu_chart`` rather than copied:
#: that module owns the tool surface and adding a chart type there must not leave this
#: tool advertising a contract it no longer matches. Importing it is cheap — matplotlib
#: itself is imported lazily inside ``_cr.render_to_png``, so a text-only document
#: still never loads it.
_CHART_SPECS: dict[str, Any] = _fc._CHART_SPECS


def _spec_text(kind: str) -> str:
    """Render the data/option contract for ``kind`` straight off the shared registry."""
    spec = _CHART_SPECS.get(kind)
    if spec is None:
        return f"unknown chart kind {kind!r}. Use one of: {', '.join(sorted(_CHART_SPECS))}"
    data = ", ".join(spec["data"])
    opts = ", ".join(spec.get("opts") or ()) or "(none)"
    return f"{kind}: data keys = {data} | option keys = {opts}"


# ── chart kind → draw ──────────────────────────────────────────────────────────
#
# ``_CHART_SPECS`` declares what a caller writes in ``data``, but its ``fn`` is the
# **Feishu-document** wrapper: it renders *and* appends a block, so it is not a draw.
# The draws live in ``_chart_render``, and their argument shapes differ by kind —
# most take ``labels + series``, several take ``labels + values``, and scatter/box/bubble
# take point groups the wrapper normally pre-parses. So the mapping below is spelled out
# rather than inferred from parameter names: ``points_json`` happens to be the wrapper's
# input name, and passing it through unchanged would hand the draw a JSON string where it
# expects a list of groups.
#
# Each adapter parses the caller's JSON strings into the draw's positional arguments
# using the renderer's own parsers, so per-chart validation (lengths, sign, emptiness)
# stays in one place.


def _draw_args_series(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"labels": _cr.parse_labels(d["labels_json"]), "series": _cr.parse_series(d["series_json"])}


def _draw_args_values(d: dict[str, Any], _o: dict[str, Any]) -> dict[str, Any]:
    return {"labels": _cr.parse_labels(d["labels_json"]), "values": _cr.parse_values(d["values_json"])}


def _draw_args_single_series(d: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """``labels + values`` for the bar family, which takes ``labels + series``.

    One series named after the caller's ``y_label`` (or 数值) — the same shape the Feishu
    column/bar tools build for a single series, so the chart and its legend read the same
    in both places.
    """
    return {
        "labels": _cr.parse_labels(d["labels_json"]),
        "series": [(str(options.get("y_label") or "") or "数值", _cr.parse_values(d["values_json"]))],
    }


def _draw_args_points(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"groups": _cr.parse_point_groups(d["points_json"])}


def _draw_args_heatmap(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    rows = _cr.parse_labels(d["row_labels_json"], "row_labels")
    cols = _cr.parse_labels(d["col_labels_json"], "col_labels")
    return {"row_labels": rows, "col_labels": cols, "matrix": _cr.parse_matrix(d["values_json"], len(rows), len(cols))}


def _draw_args_combo(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {
        "labels": _cr.parse_labels(d["labels_json"]),
        "bar_series": _cr.parse_series(d["bar_series_json"], "bar_series"),
        "line_series": _cr.parse_series(d["line_series_json"], "line_series"),
    }


def _draw_args_gantt(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    tasks, tick_labels, today = _cr.parse_gantt_tasks(d["tasks_json"])
    return {"tasks": tasks, "tick_labels": tick_labels, "today": today}


def _draw_args_funnel(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"stages": _cr.parse_labels(d["stages_json"], "stages"), "values": _cr.parse_values(d["values_json"])}


def _draw_args_waterfall(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"labels": _cr.parse_labels(d["labels_json"]), "deltas": _cr.parse_values(d["deltas_json"])}


def _draw_args_radar(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {
        "axes_labels": _cr.parse_labels(d["axes_json"], "axes"),
        "series": _cr.parse_series(d["series_json"]),
    }


def _draw_args_scalar(what: str) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """A draw whose only input is one numbers array (histogram)."""

    def build(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
        return {what: _cr.parse_values(d["values_json"])}

    return build


def _draw_args_bubble(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"points": _cr.parse_points(d["points_json"], dims=3)}


def _draw_args_box(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"groups": _cr.parse_series(d["groups_json"], "groups")}


def _draw_args_progress(d: dict[str, Any], _options: dict[str, Any]) -> dict[str, Any]:
    return {"items": _cr.parse_pairs(d["items_json"], "items")}


#: display kind → (draw function name on ``_cr``, adapter, extra fixed kwargs).
#: ``extra`` carries the draw's own variant flags — ``area`` is ``draw_line`` with a
#: filled band, ``donut`` is ``draw_pie`` with a hole — which is how the Feishu tools
#: express them too, so the two surfaces stay one implementation.
_CHART_DRAWS: dict[str, tuple[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]], dict[str, Any]]] = {
    "line": ("draw_line", _draw_args_series, {}),
    "area": ("draw_line", _draw_args_series, {"smooth_area": True}),
    "stacked_area": ("draw_stacked_area", _draw_args_series, {}),
    "column": ("draw_bar", _draw_args_single_series, {}),
    "bar": ("draw_bar", _draw_args_single_series, {"horizontal": True}),
    "grouped_column": ("draw_bar", _draw_args_series, {}),
    "stacked_column": ("draw_bar", _draw_args_series, {"stacked": True}),
    "pareto": ("draw_pareto", _draw_args_values, {}),
    "pie": ("draw_pie", _draw_args_values, {}),
    "donut": ("draw_pie", _draw_args_values, {"donut": True}),
    "funnel": ("draw_funnel", _draw_args_funnel, {}),
    "waterfall": ("draw_waterfall", _draw_args_waterfall, {}),
    "radar": ("draw_radar", _draw_args_radar, {}),
    "scatter": ("draw_scatter", _draw_args_points, {}),
    "bubble": ("draw_bubble", _draw_args_bubble, {}),
    "box": ("draw_box", _draw_args_box, {}),
    "histogram": ("draw_histogram", _draw_args_scalar("values"), {}),
    "heatmap": ("draw_heatmap", _draw_args_heatmap, {}),
    "combo": ("draw_combo", _draw_args_combo, {}),
    "gantt": ("draw_gantt", _draw_args_gantt, {}),
    "progress": ("draw_progress", _draw_args_progress, {}),
}


def _as_object(raw: Any, *, kind: str, what: str) -> dict[str, Any]:
    """Accept an object, its JSON string, or nothing; reject everything else by name.

    ``None`` and a blank string both mean "not given" — every key in ``options`` is
    optional, so an absent ``options`` must not be an error.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"chart {kind!r}: {what} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"chart {kind!r}: {what} must be an object — {_spec_text(kind)}")
    return raw


def _unwrap_draw(built: Any, kind: str) -> Callable[[Any, Any], None]:
    """Normalize a renderer's return value to the ``draw(fig, ax)`` callable.

    ``draw_pie`` is the one that returns ``(draw, folded)`` — the count of slices it
    merged into 其他 — because the Feishu tool reports that number back to the model.
    Every other draw returns the callable directly. Unwrapping here rather than inside
    each call site keeps ``_CHART_DRAWS`` free of a per-kind quirk; the ``callable``
    check is what makes it safe, since a bare draw is a function and so never matches
    the tuple form.
    """
    if isinstance(built, tuple) and len(built) == 2 and callable(built[0]):
        return built[0]
    if not callable(built):
        raise ValueError(f"chart {kind!r}: the renderer did not return a draw callable")
    return built


def _build_chart_draw(block: dict[str, Any]) -> Callable[[Any, Any], None]:
    """Build the matplotlib ``draw`` closure for one ``chart`` block.

    Both spellings reach the same place: the natural one
    (``{"type":"chart","kind":"line","data":{...}}``) and the stringified one the Feishu
    chart tools take (``{"data_json":"{...}"}``). The renderers parse numbers out of JSON
    strings and validate per chart while doing so, so each value is re-serialized here.
    """
    kind = str(block.get("kind") or "").strip()
    spec = _CHART_SPECS.get(kind)
    draw_spec = _CHART_DRAWS.get(kind)
    if spec is None or draw_spec is None:
        raise ValueError(_spec_text(kind))
    draw_name, adapt, fixed = draw_spec

    raw_data = block.get("data", block.get("data_json"))
    data_obj = _as_object(raw_data, kind=kind, what="`data`")
    data: dict[str, Any] = {}
    for key in spec["data"]:
        if key not in data_obj:
            raise ValueError(f"chart {kind!r}: missing data key {key!r} — {_spec_text(kind)}")
        value = data_obj[key]
        # the adapters call the renderer's parsers, which take JSON *strings*
        data[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    raw_opts = block.get("options", block.get("options_json"))
    options = _as_object(raw_opts, kind=kind, what="`options`")
    unknown = sorted(set(options) - set(spec.get("opts") or ()))
    if unknown:
        raise ValueError(f"chart {kind!r}: unknown option(s) {unknown} — {_spec_text(kind)}")

    colors = block.get("colors")
    if colors is not None:
        if kind not in _cr._COLOR_KINDS:
            raise ValueError(f"chart {kind!r} does not take `colors` (only {sorted(_cr._COLOR_KINDS)} do).")
        if not isinstance(colors, list) or not all(isinstance(c, str) for c in colors):
            raise ValueError(f"chart {kind!r}: `colors` must be an array of colour strings.")
        options["colors"] = colors

    try:
        # the renderer's parsers own the per-chart validation (lengths, sign, totals),
        # and they raise ChartDataError — surfaced below as a tool-level error naming
        # the kind, so a bad block in a long document says which one it was
        args = adapt(data, options)
        draw = getattr(_cr, draw_name)
        built = draw(**args, title=str(block.get("title") or ""), **fixed, **options)
        return _unwrap_draw(built, kind)
    except _cr.ChartDataError as exc:
        raise ValueError(f"chart {kind!r}: {exc}") from exc


async def _render_chart_png(block: dict[str, Any], workdir: Path) -> Path:
    """Render one chart block to a PNG under ``workdir`` and return its path."""
    draw = _build_chart_draw(block)
    digest = abs(hash(json.dumps(block, sort_keys=True, default=str))) % 10**10
    return Path(await _cr.render_to_png(draw, str(workdir / f"chart-{digest}.png")))


def _set_cjk_font(doc: Any, cjk: str, latin: str) -> None:
    """Set East-Asian + Latin fonts on every base style so all text is consistent.

    Uses ``rPr.get_or_add_rFonts()`` (not a raw append) so the ``w:rFonts``
    element lands as the first child of ``w:rPr`` — the order the OOXML schema
    requires. Iterating ``doc.styles`` covers Normal, every Heading, Title, and
    the table styles in one pass.
    """
    for style in doc.styles:
        element = style.element
        if not hasattr(element, "get_or_add_rPr"):
            continue  # e.g. numbering styles have no run properties
        rpr = element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn("w:ascii"), latin)
        rfonts.set(qn("w:hAnsi"), latin)
        rfonts.set(qn("w:eastAsia"), cjk)  # the attribute that fixes 字体不齐


def _add_block(doc: Any, block: dict[str, Any]) -> None:
    """Render one content block into the document."""
    kind = block.get("type")
    if kind == "heading":
        level = int(block.get("level", 1))
        doc.add_heading(str(block.get("text", "")), level=max(0, min(level, 9)))
    elif kind == "paragraph":
        doc.add_paragraph(str(block.get("text", "")))
    elif kind == "table":
        rows = block.get("rows") or []
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        table = doc.add_table(rows=0, cols=ncols)
        table.style = block.get("style", "Light Grid Accent 1")
        for row in rows:
            cells = table.add_row().cells
            for idx in range(ncols):
                cells[idx].text = "" if idx >= len(row) else str(row[idx])
    elif kind == "image":
        _add_image(doc, block)
    elif kind == "chart":
        raise ValueError("chart blocks are rendered to an image first; call `_render` before `_add_block`")
    elif kind == "page_break":
        doc.add_page_break()
    else:
        raise ValueError(f"unknown block type: {kind!r}")


def _add_image(doc: Any, block: dict[str, Any]) -> None:
    """Insert a local PNG/JPEG and an optional caption paragraph below it.

    The path arrives already resolved (see ``_resolve_image_block``) because resolution
    is async and reads the workspace ContextVar, which is not available on the worker
    thread this runs on.
    """
    image_path = str(block.get("path") or "").strip()
    if not image_path:
        raise ValueError("image block needs a `path`")
    if not os.path.isfile(image_path):
        raise ValueError(f"image block path does not exist: {image_path}")
    width = block.get("width_inches")
    doc.add_picture(
        image_path,
        width=Inches(float(width)) if width else Inches(_CHART_WIDTH_IN),
    )
    caption = str(block.get("caption") or "").strip()
    if caption:
        doc.add_paragraph(caption)


async def _resolve_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Resolve an ``image`` block's ``path`` against the session workspace, upfront.

    Both resolutions happen here rather than inside the worker thread so a bad path is
    reported before any chart has been rendered and before the document is opened.
    """
    raw = str(block.get("path") or "").strip()
    if not raw:
        raise ValueError("image block needs a `path`")
    path = _paths.resolve_user_path(raw)
    if not await path.is_file():
        raise ValueError(f"image block path does not exist: {raw}")
    return {**block, "path": str(path)}


def _build_document(
    file_path: str,
    title: str,
    blocks: list[dict[str, Any]],
    cjk_font: str,
    latin_font: str,
) -> int:
    """Build and save a .docx synchronously. Returns the number of blocks written.

    ``chart`` blocks must already have been replaced by ``image`` blocks (see
    ``_resolve_blocks``): rendering is async and may run matplotlib, neither of which
    belongs on this worker thread.
    """
    doc = Document()
    _set_cjk_font(doc, cjk_font, latin_font)  # right after creating the document
    if title:
        doc.add_heading(title, level=0)
    for block in blocks:
        _add_block(doc, block)
    doc.save(file_path)
    return len(blocks)


async def _resolve_blocks(blocks: list[dict[str, Any]], workdir: Path) -> list[dict[str, Any]]:
    """Resolve ``image`` paths and expand ``chart`` blocks into rendered images.

    Kept out of the worker thread: chart rendering is async and matplotlib is not
    thread-safe with itself, so both belong on the event loop. A failure here means the
    document is never opened, so a bad chart cannot leave a half-written .docx behind.
    """
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "image":
            out.append(await _resolve_image_block(block))
        elif kind == "chart":
            png = await _render_chart_png(block, workdir)
            entry: dict[str, Any] = {"type": "image", "path": str(png)}
            caption = str(block.get("caption") or "").strip()
            if caption:
                entry["caption"] = caption
            out.append(entry)
        else:
            out.append(block)
    return out


async def write_word(
    file_path: str,
    blocks_json: list[dict[str, Any]],
    title: str = "",
    cjk_font: str = "微软雅黑",
    latin_font: str = "Calibri",
) -> str:
    """Create a real Word (.docx) report from structured content.

    Use this instead of hand-writing a python-docx script when the user asks for a
    Word document. It sets the East-Asian font (``w:eastAsia``) on every base style,
    so Chinese text renders in one consistent typeface — this is the fix for the
    "字体不齐" (uneven font) bug that appears when only ``run.font.name`` is set.

    Charts are rendered here (matplotlib, the same renderer the Feishu chart tools
    use) and embedded as pictures, so a report can carry real visuals rather than a
    table of numbers.

    Args:
        file_path: Output path for the .docx file (e.g. "report.docx").
            Relative paths (including bare filenames) resolve under the
            **session workspace** — never under the agent package directory.
            Absolute paths are used as-is, except writes into the agent
            package itself, which are refused.
        blocks_json: Array of content blocks, in order. Each block
            is an object with a ``type``:
              - ``{"type": "heading", "level": 1, "text": "概述"}`` — level 0 is
                the title style, 1-3 feed the table of contents.
              - ``{"type": "paragraph", "text": "本季度……"}``
              - ``{"type": "table", "rows": [["月份", "收入"], ["1月", "100"]],
                "style": "Light Grid Accent 1"}`` — ``style`` is optional.
              - ``{"type": "chart", "kind": "line", "title": "正负面累积趋势",
                "data": {"labels_json": ["6月","7月"], "series_json": {"正面":[3,5],"负面":[1,2]}},
                "colors": ["#34C724","#F5222D"], "caption": "图 1 正负面累积趋势"}``
                — ``kind`` is one of the chart types the Feishu chart tools accept
                (line / area / stacked_area / column / bar / grouped_column /
                stacked_column / pie / donut / funnel / waterfall / scatter /
                bubble / histogram / box / heatmap / radar / pareto / combo /
                gantt / progress). ``data`` keys are that kind's own; ask a chart
                tool or pass a wrong key and the error names the contract.
                ``colors`` is optional and applies only to the series-based kinds;
                give one colour per series **in series order**.
              - ``{"type": "image", "path": "chart.png", "caption": "…"}`` —
                insert an existing local image (path relative to the workspace).
              - ``{"type": "page_break"}``
            Example: '[{"type":"heading","level":1,"text":"概述"},
                       {"type":"paragraph","text":"正文"}]'.
        title: Optional document title rendered with the Title style at the top.
        cjk_font: East-Asian font for Chinese text (default 微软雅黑). Safe
            alternatives: 宋体, 黑体.
        latin_font: Latin font for ASCII text (default Calibri).

    Returns:
        Success message with the block count and the **resolved** output path
        (the path a ``[SEND:…]`` marker should point at), or an error message.
    """
    if not file_path.lower().endswith(".docx"):
        file_path = f"{file_path}.docx"

    blocks: Any = blocks_json
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except json.JSONDecodeError as e:
            return f"[Error] blocks_json is not valid JSON: {e}"

    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        return '[Error] blocks_json must be an array of objects, e.g. [{"type":"paragraph","text":"hi"}]'
    if not blocks and not title:
        return "[Error] provide a title or at least one block"

    # Deliverables belong in the session workspace: bare/relative names resolve
    # there (the framework binds get_workspace() per turn), never against the
    # process cwd — which is the agent package dir. Writing into the package
    # silently breaks [SEND:] delivery (markers point at the workspace) and
    # pollutes the version-controlled tree, so it is refused outright.
    path = _paths.resolve_user_path(file_path)
    refusal = _paths.refuse_agent_write(str(path))
    if refusal is not None:
        return refusal
    parent = path.parent
    if not await parent.exists():
        await parent.mkdir(parents=True, exist_ok=True)

    # Chart PNGs are scaffolding, not deliverables: python-docx embeds image *bytes*
    # into the .docx, so the files can be removed once the document is saved. A system
    # temp dir rather than the workspace — a report's charts should not litter the
    # user's folder, and `.psi-scratch/` is not safe here when the session workspace
    # happens to be the agent package (which ``refuse_agent_write`` guards for outputs).
    scratch = Path(tempfile.mkdtemp(prefix="psi-word-charts-"))
    try:
        resolved = await _resolve_blocks(blocks, scratch / "charts")
        count = await anyio.to_thread.run_sync(  # ty: ignore
            _build_document, str(path), title, resolved, cjk_font, latin_font
        )
    except Exception as e:  # python-docx raises assorted errors on bad content
        return f"[Error] Failed to write Word file: {e!r}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return f"[OK] Wrote {count} block(s) to {path}"


async def write_word_from_markdown(
    markdown_path: str,
    file_path: str,
    cjk_font: str = "微软雅黑",
    latin_font: str = "Calibri",
) -> str:
    """Convert an existing Markdown artifact into a Word document.

    Prefer this for long contracts, SOPs, and reports: draft the substantial
    content to Markdown first, then call this tool with two short paths. This
    avoids oversized structured tool arguments and runtime package installs.

    Args:
        markdown_path: Existing UTF-8 Markdown source file. Relative paths
            resolve under the session workspace.
        file_path: Output .docx path (see ``write_word`` for resolution rules).
        cjk_font: East-Asian font for Chinese text.
        latin_font: Latin font for ASCII text.

    Returns:
        Success message with the block count, or an error message.
    """
    source = _paths.resolve_user_path(markdown_path)
    if not await source.exists():
        return f"[Error] Markdown source does not exist: {markdown_path}"
    try:
        markdown = await source.read_text(encoding="utf-8")
    except OSError as e:
        return f"[Error] Failed to read Markdown source: {e}"

    blocks: list[dict[str, Any]] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            blocks.append({"type": "paragraph", "text": "\n".join(paragraph_lines)})
            paragraph_lines.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            marker, separator, heading = stripped.partition(" ")
            if separator and set(marker) == {"#"}:
                flush_paragraph()
                blocks.append({"type": "heading", "level": min(len(marker), 9), "text": heading})
                continue
        if not stripped:
            flush_paragraph()
        else:
            paragraph_lines.append(line)
    flush_paragraph()
    if not blocks:
        return "[Error] Markdown source is empty"
    return await write_word(file_path, blocks, cjk_font=cjk_font, latin_font=latin_font)
