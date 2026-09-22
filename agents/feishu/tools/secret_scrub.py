"""Scrub sensitive strings from local session history / debug logs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_impl = importlib.import_module("_secret_scrub_impl")


async def secret_scrub(
    secrets_json: str = "",
    text: str = "",
    include_history: bool = True,
    include_logs: bool = True,
    include_metrics: bool = True,
    include_workspace_histories: bool = True,
    session_only: bool = False,
) -> str:
    """Scrub exact secret strings from local AppData / workspace memory files.

    Call this when the user pastes an API key / token, or after ``read`` returns
    a file that contains one (e.g. ``敏感串.txt``). Pass the literal values via
    ``secrets_json`` (JSON array) and/or paste the surrounding text into
    ``text`` so patterns like ``sk-…`` / JWT / ``api_key=…`` can be extracted.

    The tool replaces each exact match with ``[REDACTED_SECRET]`` in:
    AppData ``histories/``, ``logs/``, ``metrics/``, plus legacy workspace
    ``histories/``. It **never** returns the secret values — only counts and
    relative paths.

    After calling, follow ``skills/sensitive-secret-response/SKILL.md``: force a
    risk notice and key-rotation advice unless the user explicitly insists there
    is no security risk.

    Args:
        secrets_json: JSON array of exact strings to scrub, e.g.
            ``["sk-abc…"]``. Each string must be ≥8 characters.
        text: Optional blob to scan for common secret patterns; extracted
            candidates are merged with ``secrets_json``.
        include_history: Scrub AppData ``histories/*.jsonl`` (default true).
        include_logs: Scrub AppData / workspace ``logs/`` (default true).
        include_metrics: Scrub AppData ``metrics/*.jsonl`` (default true).
        include_workspace_histories: Also scrub legacy
            ``{workspace}/histories/`` (default true).
        session_only: If true, only the current Session's history file
            (when a session id is bound).

    Returns:
        JSON with ``ok``, ``files_touched``, ``replacements``, ``touched``
        (relative paths only). Never includes the secret plaintext.
    """
    return await _impl.run_secret_scrub(
        secrets_json=secrets_json,
        text=text,
        include_history=include_history,
        include_logs=include_logs,
        include_metrics=include_metrics,
        include_workspace_histories=include_workspace_histories,
        session_only=session_only,
    )
