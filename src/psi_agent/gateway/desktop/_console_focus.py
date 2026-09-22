"""Bring an existing Web Console window to the foreground.

刻意为之: ``haitun.exe`` second click must *activate* the live console, not
``webbrowser.open`` another tab onto the same Gateway.  Named mutex + AppData
lock already keep a single Gateway; this module covers the browser side.

Matching is by window title containing ``app_name`` (SPA ``<title>`` is that
same string).  When no matching window exists (user closed the tab), callers
may open the URL once — that is "open", not "stack".
"""

from __future__ import annotations

import ctypes
import sys
import webbrowser
from collections.abc import Callable
from typing import Any, Protocol

from loguru import logger

# Win32 ShowWindow — restore a minimized window before SetForegroundWindow.
_SW_RESTORE = 9


class _Showable(Protocol):
    def show(self) -> None: ...

    def request_attention(self) -> None: ...


class _Attention(Protocol):
    def request_attention(self) -> None: ...


def title_matches_console(title: str, app_name: str) -> bool:
    """True when a top-level window title looks like our Web Console tab."""
    needle = app_name.strip()
    if not needle or not title.strip():
        return False
    return needle.casefold() in title.casefold()


def _user32() -> Any:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise RuntimeError("ctypes.windll is only available on Windows")
    return windll.user32


def _kernel32() -> Any:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise RuntimeError("ctypes.windll is only available on Windows")
    return windll.kernel32


def list_console_hwnds(app_name: str) -> list[int]:
    """Top-level visible HWNDs whose title contains *app_name* (Z-order)."""
    if sys.platform != "win32":
        return []
    needle = app_name.strip()
    if not needle:
        return []
    user32 = _user32()
    found: list[int] = []
    # EnumWindows callback: BOOL CALLBACK(HWND, LPARAM) — keep a strong ref.
    wnd_enum = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @wnd_enum
    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_matches_console(buf.value, needle):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_callback, 0)
    return found


def _force_foreground(hwnd: int) -> bool:
    """Restore + bring *hwnd* to the front (best-effort under Win32 focus rules)."""
    user32 = _user32()
    kernel32 = _kernel32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    foreground = int(user32.GetForegroundWindow() or 0)
    if foreground == hwnd:
        return True
    current_tid = int(kernel32.GetCurrentThreadId())
    foreground_tid = 0
    if foreground:
        foreground_tid = int(user32.GetWindowThreadProcessId(foreground, None))
    attached = False
    if foreground_tid and foreground_tid != current_tid:
        attached = bool(user32.AttachThreadInput(current_tid, foreground_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, _SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, foreground_tid, False)


def activate_existing_console(app_name: str) -> bool:
    """Activate the first matching console window.  False when none / non-Windows."""
    if sys.platform != "win32":
        return False
    try:
        hwnds = list_console_hwnds(app_name)
    except Exception as e:
        logger.debug(f"Console window enumerate failed: {e!r}")
        return False
    if not hwnds:
        return False
    hwnd = hwnds[0]
    try:
        ok = _force_foreground(hwnd)
    except Exception as e:
        logger.debug(f"Console window activate failed hwnd={hwnd}: {e!r}")
        return False
    if ok:
        logger.info(f"Activated existing console window (hwnd={hwnd})")
    else:
        # Focus steal blocked — window was still restored / brought up; treat as
        # success so callers do not stack a second browser tab.
        logger.info(f"Restored existing console window (hwnd={hwnd}, focus deferred)")
    return True


def reopen_gateway_console(
    *,
    url: str,
    app_name: str,
    webview: _Showable | None = None,
    tray: _Attention | None = None,
    activate_fn: Callable[[str], bool] | None = None,
    open_browser_fn: Callable[[str], object] | None = None,
) -> str:
    """Second-click / tray reopen: activate first, open only when nothing exists.

    Returns which branch ran: ``webview`` / ``activate`` / ``open``.
    """
    activate = activate_fn if activate_fn is not None else activate_existing_console
    open_browser = open_browser_fn if open_browser_fn is not None else webbrowser.open

    if webview is not None:
        webview.show()
        webview.request_attention()
        if tray is not None:
            tray.request_attention()
        return "webview"

    if activate(app_name):
        if tray is not None:
            tray.request_attention()
        return "activate"

    # No live console tab — open once.  Never reach here when a matching window
    # exists (that would reintroduce the stacked-tab bug C14 caught).
    open_browser(url)
    if tray is not None:
        tray.request_attention()
    return "open"
