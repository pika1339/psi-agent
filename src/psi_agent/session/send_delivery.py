"""Fill missing ``[SEND:]`` markers after a tool wrote a deliverable (刻意为之).

Prompt rules already require the model to emit ``[SEND:<abs-path>]`` in the
final reply whenever a turn wrote a user-facing file.  Models still often
write the file then paste the body inline and say "已写好: …" with no marker —
Channel never uploads, spa-v2's treasure chest stays empty, and
``/history`` has no ``sends``.

This module is a **Session-only** safety net on the existing wire: it does
not invent a new finish_reason, REST field, or chunk type.  On
``finish_reason=stop`` the agent loop asks for a suffix of ordinary
``[SEND:]`` lines, appends them to the reply text, and yields them as a
normal ``AgentChunk(content=…)`` so Channel's existing marker scanner
fires.  History then carries the same markers Gateway already projects.

Two gates, either one is enough (刻意为之: a later tool must not depend on
someone remembering to extend the name list, and the original name list
must not go dark if a path has no suffix):

- **Named create / export tools** (``write`` / Office writers / ``generate_image`` /
  ``text_to_speech`` / Feishu chart+export+download, …): a successful
  ``[OK] … to <path>`` **or** JSON ``ok: true`` with a top-level output path
  is a deliverable even when the path has no known suffix.
- **Known suffixes** (``.md`` / ``.pptx`` / ``.jpg`` / ``.png`` / ``.json`` /
  office / images / audio / …) on any tool: the same ``[OK] … to <path>``
  line, or the same JSON shape.  Export, download, chart PNG, and TTS use
  that JSON shape.

A non-empty ``text`` field on that JSON means the path is an input being
described or transcribed (vision / speech-to-text), so it is not sent.
``edit`` uses ``in <path>`` rather than ``to <path>``, so it stays out.
Paths already present in the reply (via ``extract_send_paths``) are not
duplicated.  Internal capability-package paths are skipped, matching the
prompt's "do not auto-send tools/skills/…" rule.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from psi_agent.session.history_display import extract_send_paths

# Named create / export gate.  A successful ``[OK] … to <path>`` **or** JSON
# ``ok: true`` + output-path key from one of these is a deliverable even with
# no suffix.  ``edit`` / ``bash`` / ``python_run`` stay off this list — their
# results are not "I created a user file" by name alone (suffix gate still
# covers ``.pptx`` / ``.md`` / … when the stdout shape matches).
FILE_CREATE_TOOLS: frozenset[str] = frozenset(
    {
        # First-class workspace writers
        "write",
        "write_excel",
        "write_word",
        "write_word_from_markdown",
        # Image / audio file tools (JSON ``path``)
        "generate_image",
        "text_to_speech",
        # Feishu → local file (JSON ``image_path`` / ``save_path`` / ``path``)
        "feishu_chart",
        "feishu_chart_figure",
        "feishu_doc_export",
        "feishu_file_download",
    }
)

# Suffix gate, independent of tool name.  ``.jpg`` / ``.jpeg`` / ``.png`` /
# ``.md`` / ``.ppt`` / ``.pptx`` / ``.json`` and the other office, image, and
# audio types the chest is supposed to light for.
DELIVERABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".html",
        ".htm",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".ppt",
        ".pptx",
        ".pptm",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".mov",
        ".webm",
        ".zip",
        ".excalidraw",
    }
)

# Top-level JSON keys that name a file the tool just wrote.  Nested objects
# are ignored so a search hit list cannot spray paths into the reply.
_OUTPUT_PATH_KEYS: tuple[str, ...] = (
    "path",
    "save_path",
    "image_path",
    "output_path",
    "file_path",
)

# Match desktop/haitun tool success strings, e.g.
# ``[OK] Written 12 bytes to C:\ws\a.md`` / ``[OK] Wrote 3 row(s) to reports/a.xlsx``.
# No ``\b`` after ``]`` — ``]`` and the following space are both non-word, so ``\b`` never fires.
_OK_TO_PATH = re.compile(r"^\[OK\].*\sto\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)


# Path segments that mean "agent package / private runtime", not a user deliverable.
_BLOCKED_SEGMENTS: frozenset[str] = frozenset(
    {
        "tools",
        "schedules",
        "skills",
        "systems",
        "histories",
        "channel_events",
        "triggers",
    }
)


def path_from_ok_tool_result(content: str) -> str | None:
    """Pull the path from a successful file-tool result, or ``None``."""
    text = content.strip()
    if not text.startswith("[OK]"):
        return None
    match = _OK_TO_PATH.match(text)
    if match is None:
        return None
    path = match.group(1).strip().strip("\"'")
    return path or None


def is_deliverable_path(path: str) -> bool:
    """True when *path* ends with a known user-facing file suffix."""
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    # ``notes`` and ``.gitignore`` have no deliverable suffix. ``a.md`` and ``a.tar.md`` do.
    if dot <= 0:
        return False
    return name[dot:].casefold() in DELIVERABLE_SUFFIXES


def is_blocked_auto_send_path(path: str) -> bool:
    """True when *path* looks like capability-package / history internals."""
    parts = {p.casefold() for p in path.replace("\\", "/").split("/") if p and p != "."}
    return bool(parts & _BLOCKED_SEGMENTS)


def _accept_path(path: str, found: list[str], seen: set[str], *, allow_any_suffix: bool) -> None:
    if is_blocked_auto_send_path(path):
        return
    if not allow_any_suffix and not is_deliverable_path(path):
        return
    key = path.casefold()
    if key in seen:
        return
    seen.add(key)
    found.append(path)


def paths_from_tool_content(content: str, *, tool_name: str = "") -> list[str]:
    """Paths a single tool result claims it wrote, in report order.

    Named create / export tools accept any ``[OK] … to <path>`` and any JSON
    output-path key.  Every other result still has to end in
    ``DELIVERABLE_SUFFIXES``.
    """
    found: list[str] = []
    seen: set[str] = set()
    named = tool_name in FILE_CREATE_TOOLS
    ok_path = path_from_ok_tool_result(content)
    if ok_path is not None:
        _accept_path(ok_path, found, seen, allow_any_suffix=named)
        return found
    text = content.strip()
    if not text.startswith("{"):
        return found
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return found
    if not isinstance(data, dict) or data.get("ok") is not True:
        return found
    # Non-empty ``text`` is a transcript or a description of an existing file
    # (speech-to-text, vision).  The path on that object is an input.
    described = data.get("text")
    if isinstance(described, str) and described.strip():
        return found
    for key in _OUTPUT_PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            _accept_path(
                value.strip().strip("\"'"),
                found,
                seen,
                allow_any_suffix=named,
            )
    return found


def created_file_paths_from_turn(messages: Sequence[dict[str, Any]]) -> list[str]:
    """Collect successful deliverable paths from this turn's tool results.

    Order follows tool-result order; duplicates keep the first occurrence.
    A path is kept when the tool is a named create tool or the path suffix
    is a known deliverable — either gate is enough.
    """
    found: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        name = msg.get("name")
        tool_name = name if isinstance(name, str) else ""
        for path in paths_from_tool_content(content, tool_name=tool_name):
            key = path.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


def missing_send_paths(messages: Sequence[dict[str, Any]], reply_content: str) -> list[str]:
    """Paths written this turn that the final reply has not marked with ``[SEND:]``."""
    already = {p.casefold() for p in extract_send_paths(reply_content)}
    return [p for p in created_file_paths_from_turn(messages) if p.casefold() not in already]


def send_marker_suffix(paths: Sequence[str]) -> str:
    """Format paths as reply-tail ``[SEND:]`` lines (leading newline when non-empty)."""
    lines = [f"[SEND:{p}]" for p in paths if p.strip()]
    if not lines:
        return ""
    return "\n" + "\n".join(lines)
