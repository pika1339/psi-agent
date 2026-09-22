"""Second-click console activate (no stacked webbrowser.open)."""

from __future__ import annotations

from typing import Any

import psi_agent.gateway.desktop._console_focus as console_focus
from psi_agent.gateway.desktop._console_focus import (
    reopen_gateway_console,
    title_matches_console,
)


def test_title_matches_console_substring_casefold() -> None:
    assert title_matches_console("Haitun Agent - Edge", "Haitun Agent")
    assert title_matches_console("haitun agent", "Haitun Agent")
    assert not title_matches_console("Unrelated Tab", "Haitun Agent")
    assert not title_matches_console("Haitun Agent", "")
    assert not title_matches_console("", "Haitun Agent")


def test_reopen_prefers_webview_over_activate_and_open() -> None:
    opened: list[str] = []
    activated: list[str] = []
    shown: list[str] = []
    attention: list[str] = []

    class FakeWebview:
        def show(self) -> None:
            shown.append("show")

        def request_attention(self) -> None:
            attention.append("wv")

    class FakeTray:
        def request_attention(self) -> None:
            attention.append("tray")

    branch = reopen_gateway_console(
        url="http://127.0.0.1:1/",
        app_name="Haitun Agent",
        webview=FakeWebview(),
        tray=FakeTray(),
        activate_fn=lambda name: activated.append(name) or True,
        open_browser_fn=opened.append,
    )
    assert branch == "webview"
    assert shown == ["show"]
    assert attention == ["wv", "tray"]
    assert activated == []
    assert opened == []


def test_reopen_activates_when_no_webview() -> None:
    opened: list[str] = []
    activated: list[str] = []

    branch = reopen_gateway_console(
        url="http://127.0.0.1:1/",
        app_name="Haitun Agent",
        webview=None,
        tray=None,
        activate_fn=lambda name: activated.append(name) or True,
        open_browser_fn=opened.append,
    )
    assert branch == "activate"
    assert activated == ["Haitun Agent"]
    assert opened == []


def test_reopen_opens_only_when_activate_misses() -> None:
    """刻意为之: open only when no matching window — never stack on a hit."""
    opened: list[str] = []

    branch = reopen_gateway_console(
        url="http://127.0.0.1:8765/",
        app_name="Haitun Agent",
        webview=None,
        tray=None,
        activate_fn=lambda _name: False,
        open_browser_fn=opened.append,
    )
    assert branch == "open"
    assert opened == ["http://127.0.0.1:8765/"]


def test_reopen_tray_attention_on_activate_and_open() -> None:
    hits: list[str] = []

    class FakeTray:
        def request_attention(self) -> None:
            hits.append("tray")

    reopen_gateway_console(
        url="http://x/",
        app_name="A",
        activate_fn=lambda _: True,
        open_browser_fn=lambda _: None,
        tray=FakeTray(),
    )
    assert hits == ["tray"]

    hits.clear()
    reopen_gateway_console(
        url="http://x/",
        app_name="A",
        activate_fn=lambda _: False,
        open_browser_fn=lambda _: None,
        tray=FakeTray(),
    )
    assert hits == ["tray"]


def test_activate_existing_console_false_off_windows(monkeypatch: Any) -> None:
    monkeypatch.setattr(console_focus.sys, "platform", "linux")
    assert console_focus.activate_existing_console("Haitun Agent") is False
