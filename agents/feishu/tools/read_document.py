"""Read local Office documents into grounded plain text."""

from __future__ import annotations

from pathlib import Path

import _runtime_paths as _paths
from anyio import to_thread
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def _escape_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", r"\|")


def _render_table(table: Table, budget: int) -> tuple[str, str]:
    """Render a table as Markdown, trimming whole rows when *budget* runs out.

    Returns ``(text, note)`` -- *note* is empty when nothing was omitted.  Rows are
    dropped from the bottom and the header always survives, so a trimmed table is
    still a valid table whose first N rows are exactly the document's first N rows.
    Cutting on a character boundary instead lands mid-cell, which reads as a
    corrupted table rather than an incomplete one (the failure this replaces).
    """
    rows = [[_escape_cell(cell.text) for cell in row.cells] for row in table.rows]
    if not rows:
        return "", ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = [
        "| " + " | ".join(normalized[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    body: list[str] = []
    used = sum(len(line) + 1 for line in header)
    for row in normalized[1:]:
        line = "| " + " | ".join(row) + " |"
        if used + len(line) + 1 > budget and body:
            break
        body.append(line)
        used += len(line) + 1
    omitted = len(normalized) - 1 - len(body)
    note = (
        f"\u8868\u683c\u5171 {len(normalized) - 1} \u884c\u6570\u636e\uff08\u4e0d\u542b\u8868\u5934\uff09\uff0c"
        f"\u4e0a\u9762\u5217\u51fa\u524d {len(body)} \u884c\uff0c"
        f"\u5176\u4f59 {omitted} \u884c\u56e0\u957f\u5ea6\u4e0a\u9650\u672a\u5217\u51fa"
        if omitted > 0
        else ""
    )
    return "\n".join(header + body), note


def _read_docx(path: Path, budget: int = 0) -> tuple[str, list[str]]:
    """Flatten the document; when *budget* > 0, trim at structure boundaries.

    Returns ``(text, notes)``.  *notes* is empty for a document that fits -- the
    reader has to be able to tell a complete document from a trimmed one, which
    the old "[Truncated at N characters]" suffix could not do inside a table.
    """
    document = Document(str(path))
    blocks: list[tuple[str, str]] = []
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if text:
                blocks.append((text, ""))
        elif isinstance(item, Table):
            text, note = _render_table(item, budget if budget > 0 else 10**9)
            if text:
                blocks.append((text, note))
        else:  # pragma: no cover
            continue
    if budget <= 0:
        return "\n\n".join(text for text, _ in blocks), []
    kept: list[str] = []
    notes: list[str] = []
    used = 0
    for index, (text, note) in enumerate(blocks, 1):
        cost = len(text) + 2
        if used + cost > budget:
            notes.append(
                f"\u6587\u6863\u8f83\u957f\uff0c\u4e0a\u9762\u5217\u5230\u7b2c {index - 1} \u4e2a\u5757\uff0c"
                f"\u5176\u4f59 {len(blocks) - index + 1} \u4e2a\u5757\u672a\u5217\u51fa"
            )
            break
        if note:
            notes.append(note)
        kept.append(text)
        used += cost
    return "\n\n".join(kept), notes


async def read_document(file_path: str, max_chars: int = 50000) -> str:
    """Read a local Word ``.docx`` as plain text, preserving table order.

    Use this instead of ``read`` for local ``.docx`` files. The generic
    ``read`` tool treats Office containers as UTF-8 text and returns ZIP binary
    noise. This tool extracts paragraphs and tables without modifying the
    source document.

    Args:
        file_path: Absolute path, or a path relative to the Session workspace.
        max_chars: Maximum returned characters (1,000 to 200,000).

    Returns:
        Extracted text prefixed with the exact resolved source path. Errors are
        explicit and never return guessed document content.
    """
    path = _paths.resolve_user_path(file_path)
    if not await path.exists():
        return f"[Error] File not found: {path}"
    if not await path.is_file():
        return f"[Error] Not a file: {path}"
    if path.suffix.lower() != ".docx":
        return f"[Error] Unsupported document type {path.suffix!r}; read_document currently supports .docx"

    limit = max(1000, min(int(max_chars), 200000))
    try:
        text, omitted = await to_thread.run_sync(_read_docx, Path(str(path)), limit)
    except Exception as exc:
        return f"[Error] Could not parse DOCX {path}: {type(exc).__name__}: {exc}"

    if not text.strip():
        return f"[Error] No extractable text found in DOCX: {path}"
    note = (
        "[Extraction: paragraphs and tables in document order; embedded images, "
        "drawings, and text boxes are not interpreted]"
    )
    # Omissions are stated per structure -- how many table rows, how many blocks --
    # never as a bare character count: "Truncated at 50000 characters" does not tell
    # a reader whether the table in front of it is complete.
    omitted_lines = "".join(f"\n[\u672a\u5217\u51fa] {line}" for line in omitted)
    return f"[Source: {path}]\n{note}{omitted_lines}\n\n{text}"
