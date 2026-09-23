"""Read tool - read file contents."""

from __future__ import annotations

import _runtime_paths as _paths


async def read(file_path: str, offset: int = 0, limit: int = 0) -> str:
    """Read file contents, optionally with line offset and limit.

    Relative paths resolve under the current Session workspace
    (``get_workspace()``). Absolute paths are used as-is. **Exception:** a
    relative path under ``skills/`` that the workspace does not contain is read
    from the agent package instead — skills are part of the capability package,
    and the prompt points at them as ``skills/<name>/SKILL.md`` everywhere.

    Args:
        file_path: Path to the file to read.
        offset: Line number to start reading from (0-indexed, 0 = beginning).
        limit: Maximum number of lines to read (0 = no limit).

    Returns:
        File contents as a string, or an error message if the file cannot be read.
    """
    path = _paths.resolve_user_path(file_path)
    if _paths.is_skill_ref(file_path) and not await path.exists():
        path = _paths.skill_ref_fallback(file_path)
    if not await path.exists():
        return f"[Error] File not found: {path}"
    if not await path.is_file():
        return f"[Error] Not a file: {path}"

    content = await path.read_text(encoding="utf-8", errors="replace")

    if offset == 0 and limit == 0:
        return content

    lines = content.splitlines(keepends=True)
    selected = lines[offset:] if limit == 0 else lines[offset : offset + limit]
    return "".join(selected)
