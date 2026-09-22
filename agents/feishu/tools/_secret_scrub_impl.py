"""Scrub exact secret strings from local AppData / workspace memory files.

Never returns the secret values themselves — only counts and relative paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
from _runtime_paths import workspace_dir
from loguru import logger

from psi_agent._appdata import resolve_appdata_root
from psi_agent.session.runtime_context import get_session_id

REDACTION = "[REDACTED_SECRET]"
MIN_SECRET_CHARS = 8
MAX_FILE_BYTES = 8 * 1024 * 1024  # skip huge binaries / dumps
MAX_FILES = 400

# Patterns used to *extract* candidates from pasted / file text — not applied
# as free-form replace across whole disks (too many false positives).
_EXTRACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-]{20,})"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|"
        r"password|passwd|token)\s*[=:]\s*[\"']?([^\s\"']{12,})"
    ),
)

_TEXT_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".log",
        ".txt",
        ".md",
        ".csv",
        ".yml",
        ".yaml",
        ".env",
        ".xml",
        ".html",
        ".htm",
    }
)


def extract_secrets_from_text(text: str) -> list[str]:
    """Pull likely secret literals out of *text* (deduped, longest-first)."""
    found: set[str] = set()
    raw = text or ""
    for pat in _EXTRACT_PATTERNS:
        for m in pat.finditer(raw):
            cand = (m.group(1) or "").strip() if m.lastindex else (m.group(0) or "").strip()
            if len(cand) >= MIN_SECRET_CHARS:
                found.add(cand)
    return sorted(found, key=len, reverse=True)


def _normalize_secrets(secrets: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in secrets:
        s = (item or "").strip()
        if len(s) < MIN_SECRET_CHARS:
            continue
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    # Longest first so overlapping substrings redact fully once.
    cleaned.sort(key=len, reverse=True)
    return cleaned


def _scrub_text(body: str, secrets: list[str]) -> tuple[str, int]:
    hits = 0
    out = body
    for secret in secrets:
        if secret not in out:
            continue
        n = out.count(secret)
        out = out.replace(secret, REDACTION)
        hits += n
    return out, hits


async def _iter_candidate_files(
    *,
    appdata: anyio.Path,
    workspace: anyio.Path,
    session_id: str,
    include_history: bool,
    include_logs: bool,
    include_metrics: bool,
    include_workspace_histories: bool,
    session_only: bool,
) -> list[anyio.Path]:
    paths: list[anyio.Path] = []
    seen: set[str] = set()

    async def add_file(p: anyio.Path) -> None:
        key = str(p).casefold()
        if key in seen:
            return
        seen.add(key)
        paths.append(p)

    async def add_dir_files(root: anyio.Path, *, recursive: bool = True) -> None:
        if not await root.exists():
            return
        if recursive:
            async for p in root.rglob("*"):
                if await p.is_file():
                    await add_file(p)
        else:
            async for p in root.iterdir():
                if await p.is_file():
                    await add_file(p)

    if include_history:
        hist = appdata / "histories"
        if session_only and session_id:
            for name in (f"{session_id}.jsonl",):
                await add_file(hist / name)
        else:
            await add_dir_files(hist, recursive=False)

    if include_workspace_histories:
        wh = workspace / "histories"
        if session_only and session_id:
            await add_file(wh / f"{session_id}.jsonl")
        else:
            await add_dir_files(wh, recursive=False)

    if include_logs:
        await add_dir_files(appdata / "logs", recursive=True)
        # Directed DEBUG sink may land under workspace .psi when PSI_DEBUG_LOG_PATH set.
        await add_dir_files(workspace / ".psi" / "logs", recursive=True)

    if include_metrics:
        await add_dir_files(appdata / "metrics", recursive=False)

    return paths[:MAX_FILES]


def _looks_textish(path: anyio.Path) -> bool:
    suffix = Path(str(path)).suffix.casefold()
    if suffix in _TEXT_SUFFIXES:
        return True
    name = Path(str(path)).name.casefold()
    return name.startswith("psi-debug") or name.endswith(".log")


async def scrub_secrets(
    *,
    secrets: list[str],
    include_history: bool = True,
    include_logs: bool = True,
    include_metrics: bool = True,
    include_workspace_histories: bool = True,
    session_only: bool = False,
) -> dict[str, Any]:
    secrets_n = _normalize_secrets(secrets)
    if not secrets_n:
        return {
            "ok": False,
            "error": "no_usable_secrets",
            "hint": f"pass secrets ≥{MIN_SECRET_CHARS} chars, or text with extractable patterns",
            "files_touched": 0,
            "replacements": 0,
        }

    appdata_root = await resolve_appdata_root()
    appdata = anyio.Path(appdata_root)
    workspace = anyio.Path(workspace_dir())
    sid = (get_session_id() or "").strip()

    candidates = await _iter_candidate_files(
        appdata=appdata,
        workspace=workspace,
        session_id=sid,
        include_history=include_history,
        include_logs=include_logs,
        include_metrics=include_metrics,
        include_workspace_histories=include_workspace_histories,
        session_only=session_only,
    )

    files_touched = 0
    replacements = 0
    touched_rel: list[str] = []
    skipped: list[str] = []

    for path in candidates:
        if not _looks_textish(path):
            continue
        try:
            st = await path.stat()
        except OSError:
            continue
        if st.st_size > MAX_FILE_BYTES:
            skipped.append(f"too_large:{path.name}")
            continue
        try:
            raw = await path.read_bytes()
        except OSError as exc:
            skipped.append(f"read_fail:{path.name}")
            logger.warning("secret_scrub read failed path={} err={}", path.name, type(exc).__name__)
            continue
        # Skip obvious binary
        if b"\x00" in raw[:4096]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
        new_text, hits = _scrub_text(text, secrets_n)
        if hits <= 0:
            continue
        try:
            await path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            skipped.append(f"write_fail:{path.name}")
            logger.warning("secret_scrub write failed path={} err={}", path.name, type(exc).__name__)
            continue
        files_touched += 1
        replacements += hits
        # Relative to appdata or workspace — never echo absolute paths with user home.
        try:
            rel = str(Path(str(path)).relative_to(Path(appdata_root)))
            touched_rel.append(f"appdata:{rel}")
        except ValueError:
            try:
                rel = str(Path(str(path)).relative_to(Path(str(workspace))))
                touched_rel.append(f"workspace:{rel}")
            except ValueError:
                touched_rel.append(path.name)

    return {
        "ok": True,
        "secrets_count": len(secrets_n),
        "files_scanned": len(candidates),
        "files_touched": files_touched,
        "replacements": replacements,
        "redaction_marker": REDACTION,
        "touched": touched_rel[:50],
        "skipped": skipped[:20],
        "session_id_bound": bool(sid),
        "session_only": session_only,
    }


async def run_secret_scrub(
    *,
    secrets_json: str = "",
    text: str = "",
    include_history: bool = True,
    include_logs: bool = True,
    include_metrics: bool = True,
    include_workspace_histories: bool = True,
    session_only: bool = False,
) -> str:
    secrets: list[str] = []
    raw_json = (secrets_json or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": "secrets_json_not_json", "hint": "pass a JSON array of strings"},
                ensure_ascii=False,
            )
        if isinstance(parsed, str):
            secrets.append(parsed)
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str):
                    secrets.append(item)
                elif item is not None:
                    secrets.append(str(item))
        else:
            return json.dumps(
                {"ok": False, "error": "secrets_json_must_be_array_or_string"},
                ensure_ascii=False,
            )

    if (text or "").strip():
        secrets.extend(extract_secrets_from_text(text))

    result = await scrub_secrets(
        secrets=secrets,
        include_history=include_history,
        include_logs=include_logs,
        include_metrics=include_metrics,
        include_workspace_histories=include_workspace_histories,
        session_only=session_only,
    )
    return json.dumps(result, ensure_ascii=False)
