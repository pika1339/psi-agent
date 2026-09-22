"""Unit tests for Session auto-fill of missing ``[SEND:]`` markers."""

from __future__ import annotations

import json
import socket as _s

import pytest
from aiohttp import web

from psi_agent.session.agent import SessionAgent
from psi_agent.session.ai_client import AiClient
from psi_agent.session.send_delivery import (
    FILE_CREATE_TOOLS,
    created_file_paths_from_turn,
    is_blocked_auto_send_path,
    is_deliverable_path,
    missing_send_paths,
    path_from_ok_tool_result,
    send_marker_suffix,
)
from psi_agent.session.tool_registry import FileEntry, ToolFunction, ToolRegistry


def test_path_from_ok_matches_write_shapes() -> None:
    assert path_from_ok_tool_result("[OK] Written 12 bytes to C:\\ws\\a.md") == r"C:\ws\a.md"
    assert path_from_ok_tool_result("[OK] Wrote 3 row(s) to reports/out.xlsx") == "reports/out.xlsx"
    assert path_from_ok_tool_result("[OK] Wrote 8 block(s) to /tmp/doc.docx") == "/tmp/doc.docx"


def test_path_from_ok_rejects_errors_and_noise() -> None:
    assert path_from_ok_tool_result("[Error] disk full") is None
    assert path_from_ok_tool_result("Wrote something to nowhere") is None
    assert path_from_ok_tool_result("") is None


def test_blocked_paths_skip_capability_package() -> None:
    assert is_blocked_auto_send_path("skills/foo/SKILL.md") is True
    assert is_blocked_auto_send_path("/ws/tools/write.py") is True
    assert is_blocked_auto_send_path(r"C:\proj\histories\x.jsonl") is True
    assert is_blocked_auto_send_path("deliverables/report.md") is False
    assert is_blocked_auto_send_path(r"C:\Users\me\Desktop\haitun交付\方案.docx") is False


def test_deliverable_suffix_rejects_bare_words() -> None:
    assert is_deliverable_path("out/a.md") is True
    assert is_deliverable_path(r"C:\ws\deck.PPTX") is True
    assert is_deliverable_path("shot.JPG") is True
    assert is_deliverable_path("shot.png") is True
    assert is_deliverable_path("slides.ppt") is True
    assert is_deliverable_path("data.json") is True
    assert is_deliverable_path("board.excalidraw") is True
    assert is_deliverable_path("notes") is False
    assert is_deliverable_path(".gitignore") is False
    assert is_deliverable_path("to Alice") is False


def test_named_create_tools_cover_writers_and_exports() -> None:
    assert {
        "write",
        "write_excel",
        "write_word",
        "write_word_from_markdown",
        "generate_image",
        "text_to_speech",
        "feishu_chart",
        "feishu_chart_figure",
        "feishu_doc_export",
        "feishu_file_download",
    } == FILE_CREATE_TOOLS


def test_named_create_tool_sends_a_path_with_no_suffix() -> None:
    messages = [
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 4 bytes to out/notes",
            "tool_call_id": "1",
        },
        {
            "role": "tool",
            "name": "write_ppt",
            "content": "[OK] Wrote 1 slide(s) to out/deck",
            "tool_call_id": "2",
        },
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 1 bytes to skills/secret.md",
            "tool_call_id": "3",
        },
        {
            "role": "tool",
            "name": "generate_image",
            "content": '{"ok": true, "path": "generated/images/no-ext"}',
            "tool_call_id": "4",
        },
        {
            "role": "tool",
            "name": "feishu_doc_export",
            "content": '{"ok": true, "save_path": "exports/board"}',
            "tool_call_id": "5",
        },
    ]
    assert created_file_paths_from_turn(messages) == [
        "out/notes",
        "generated/images/no-ext",
        "exports/board",
    ]


def test_created_paths_follow_result_shape_not_tool_name() -> None:
    messages = [
        {"role": "assistant", "content": "writing"},
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 4 bytes to out/a.md",
            "tool_call_id": "1",
        },
        {
            "role": "tool",
            "name": "edit",
            "content": "[OK] Replaced 1 occurrence in out/a.md",
            "tool_call_id": "2",
        },
        {
            "role": "tool",
            "name": "bash",
            "content": "ls\nout/secret.md\n",
            "tool_call_id": "3",
        },
        {
            "role": "tool",
            "name": "python_run",
            "content": "[OK] Wrote 12 slide(s) to out/deck.pptx",
            "tool_call_id": "4",
        },
        {
            "role": "tool",
            "name": "write_excel",
            "content": "[OK] Wrote 2 row(s) to out/b.xlsx",
            "tool_call_id": "5",
        },
        {
            "role": "tool",
            "name": "text_to_speech",
            "content": '{"ok": true, "path": "generated/audio/tts-1.mp3", "text": "", "message": "ok"}',
            "tool_call_id": "6",
        },
        {
            "role": "tool",
            "name": "speech_to_text",
            "content": '{"ok": true, "path": "in/voice.mp3", "text": "你好"}',
            "tool_call_id": "7",
        },
        {
            "role": "tool",
            "name": "save_image",
            "content": "[OK] Wrote image to out/shot.jpg",
            "tool_call_id": "10",
        },
        {
            "role": "tool",
            "name": "feishu_chart",
            "content": '{"ok": true, "chart_type": "pie", "image_path": "charts/a.png"}',
            "tool_call_id": "8",
        },
        {
            "role": "tool",
            "name": "describe_image",
            "content": '{"ok": true, "text": "a cat", "image_path": "in/cat.png"}',
            "tool_call_id": "9",
        },
        {
            "role": "tool",
            "name": "generate_image",
            "content": '{"ok": true, "path": "generated/images/gen-1.png"}',
            "tool_call_id": "11",
        },
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 20 bytes to out/data.json",
            "tool_call_id": "12",
        },
    ]
    assert created_file_paths_from_turn(messages) == [
        "out/a.md",
        "out/deck.pptx",
        "out/b.xlsx",
        "generated/audio/tts-1.mp3",
        "out/shot.jpg",
        "charts/a.png",
        "generated/images/gen-1.png",
        "out/data.json",
    ]


def test_missing_send_skips_already_marked() -> None:
    messages = [
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 1 bytes to /ws/a.md",
            "tool_call_id": "1",
        },
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 1 bytes to /ws/b.md",
            "tool_call_id": "2",
        },
    ]
    reply = "done\n[SEND:/ws/a.md]\n"
    assert missing_send_paths(messages, reply) == ["/ws/b.md"]


def test_send_marker_suffix_format() -> None:
    assert send_marker_suffix([]) == ""
    assert send_marker_suffix(["/ws/a.md", "/ws/b.docx"]) == "\n[SEND:/ws/a.md]\n[SEND:/ws/b.docx]"


def test_dedupe_same_path_case_insensitive() -> None:
    messages = [
        {
            "role": "tool",
            "name": "write",
            "content": "[OK] Written 1 bytes to C:\\WS\\A.md",
            "tool_call_id": "1",
        },
        {
            "role": "tool",
            "name": "write_word",
            "content": "[OK] Wrote 1 block(s) to c:\\ws\\a.md",
            "tool_call_id": "2",
        },
    ]
    assert created_file_paths_from_turn(messages) == [r"C:\WS\A.md"]


# --- agent stop path: auto-append is ordinary content, not a new protocol ---


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_agent_stop_auto_appends_missing_send_after_write() -> None:
    """write succeeded + reply forgot [SEND:] → suffix yielded and committed."""
    path = r"C:\ws\deliverables\report.md"
    req_count = 0

    async def write_fn(file_path: str = "", content: str = "") -> str:
        return f"[OK] Written {len(content)} bytes to {path}"

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal req_count
        req_count += 1
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        if req_count == 1:
            chunk = {
                "id": "mock",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "write",
                                        "arguments": json.dumps({"file_path": path, "content": "hi"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        else:
            # Model forgot the marker — the bug this gate fixes.
            chunk = {
                "id": "mock",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"已写好: {path}"},
                        "finish_reason": "stop",
                    }
                ],
            }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        tf = ToolFunction(
            name="write",
            description="Write a file.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        )
        agent = SessionAgent(
            ai_client=AiClient(f"http://127.0.0.1:{port}"),
            tool_registry=ToolRegistry(
                files={
                    "__test__": FileEntry(
                        file_hash="",
                        tools={"write": tf},
                        funcs={"write": write_fn},
                    )
                }
            ),
        )
        chunks = [c async for c in agent.run({"role": "user", "content": "写个报告"})]
        content = "".join(c.content or "" for c in chunks)
        assert f"[SEND:{path}]" in content
        assistants = [m for m in agent._conversation.messages if m.get("role") == "assistant"]
        assert any(f"[SEND:{path}]" in (m.get("content") or "") for m in assistants)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_agent_stop_does_not_duplicate_existing_send() -> None:
    path = "/ws/a.md"
    req_count = 0

    async def write_fn(file_path: str = "", content: str = "") -> str:
        return f"[OK] Written {len(content)} bytes to {path}"

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal req_count
        req_count += 1
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        if req_count == 1:
            chunk = {
                "id": "mock",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "write",
                                        "arguments": json.dumps({"file_path": path, "content": "x"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        else:
            chunk = {
                "id": "mock",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"ok\n[SEND:{path}]\n"},
                        "finish_reason": "stop",
                    }
                ],
            }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        tf = ToolFunction(
            name="write",
            description="Write a file.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        )
        agent = SessionAgent(
            ai_client=AiClient(f"http://127.0.0.1:{port}"),
            tool_registry=ToolRegistry(
                files={
                    "__test__": FileEntry(
                        file_hash="",
                        tools={"write": tf},
                        funcs={"write": write_fn},
                    )
                }
            ),
        )
        chunks = [c async for c in agent.run({"role": "user", "content": "写"})]
        content = "".join(c.content or "" for c in chunks)
        assert content.count(f"[SEND:{path}]") == 1
    finally:
        await runner.cleanup()
