"""`psi-agent run` 配的 appdata 根, metrics jsonl 必须真落进去。

## 判据为什么落在「落盘位置」而不是「环境变量设上了」

这一条对着的缺陷是: 生产两个私有容器的 `config.yml` 已经写了
`appdata: /workspace/.psi/appdata`, 且 histories **确实**落在那里, 但
`{appdata}/metrics/` 一个文件都没出现, 日报里「成本来源 luolin/chengxx」每天报
「未测到」, 当日总花费永远只是个下限。

根因是 `metrics.record()` 走 `resolve_appdata_root()` **不带参数**(metrics 是跨组件
机制、不是组件, 它手上没有 Session 句柄可问), 于是忽略 `config.yml` 的 `appdata:`
回落 platformdirs —— 落到容器里并不存在的 `/root/.local/share/Haitun`。gateway 容器
没中这一枪, 只因为 `Gateway._run_with_appdata` 早就做了同样的导出, 而私有容器的
`psi-agent run` 根本不经过 Gateway。

所以**断言必须落在「那一行 JSON 出现在配置的目录下」**。只断言
`os.environ["PSI_APPDATA"]` 被设成了配置值是不够的 —— 那是修法的形状, 不是用户遇到的
故障; 把 `metrics.py` 改回自己拼 platformdirs 这条判据照样全绿。同理也不断言 histories,
那一条本来就是对的, 拿它当判据等于测一个从没坏过的东西。

## 为什么不真起一个 Session

`Session.run()` 会连 AI socket、起 serve_session 并永不返回; 测完整启动需要一整套替身,
而要钉的那一段只有「解析 appdata 根 → 让进程内的机制看得见它」。所以这里只跑到该段
之后就取消, 再直接调一次真的 `metrics.record()` —— 落盘走的是 `metrics.py` 真的
`_jsonl_sink`, 不是替身。
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import anyio
import pytest

import psi_agent.metrics as metrics
from psi_agent.session import Session


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_metrics_module_state(monkeypatch: pytest.MonkeyPatch) -> object:
    """复位 metrics 的模块级状态与 `PSI_APPDATA`。

    `_purged_day` 与 `_sink` 都是模块级的, 不复位会跨用例串味。`PSI_APPDATA` 用
    `monkeypatch.delenv` 删掉而不是设个值 —— 本用例要证的正是「即便环境里没有它,
    `config.yml` 的 `appdata:` 也管用」, 预先设上会让判据从另一条路假绿。
    """
    monkeypatch.delenv("PSI_APPDATA", raising=False)
    monkeypatch.delenv("PSI_METRICS", raising=False)
    default_sink = metrics._jsonl_sink
    metrics._purged_day = None
    yield
    metrics.set_sink(default_sink)
    metrics._purged_day = None


async def _start_session_until_appdata_resolved(session: Session) -> None:
    """跑 `Session.run()` 到 appdata 解析完, 然后取消掉。

    取消是必须的: `run()` 尾部 `serve_session` 永不返回。用 `move_on_after` 而不是
    `start_soon` + sleep, 是为了让这条用例在机器慢时只是变慢、不会变成偶发红。
    """
    # 连不上 AI socket / 建不了 channel socket 都无所谓: 那些都在 appdata 解析**之后**。
    # 吞掉它们, 否则判据会变成在测 "socket 能不能连", 而那不是这条用例的事。
    #
    # 只吞 `Exception`, **不用 `contextlib.suppress`**: `suppress` 连
    # `CancelledError` 一起吞(它是 `BaseException` 而非 `Exception` 的子类, 但
    # `suppress(Exception)` 这个形状在这条代码路径上极易被后人顺手放宽成
    # `suppress(BaseException)`), 而上面那个 `move_on_after` 正是靠取消信号收尾的 ——
    # 吞掉它 anyio 会无限重试交付取消, 表现是 100% 忙转、零日志。
    with anyio.move_on_after(5):
        try:
            await session.run()
        except Exception as exc:
            del exc


@pytest.mark.anyio
async def test_metrics_jsonl_lands_under_the_configured_appdata_root(tmp_path: Path) -> None:
    """`appdata:` 配了哪, 当天的 metrics jsonl 就必须出现在哪。

    这是用户遇到的那个故障的直接反面: 修之前这个文件会出现在 platformdirs 兜底根下,
    而配置的目录里只有 `histories/`。
    """
    workspace = tmp_path / "ws"
    appdata = tmp_path / "configured-appdata"
    await anyio.Path(workspace).mkdir()
    await anyio.Path(appdata).mkdir()

    session = Session(
        workspace=str(workspace),
        appdata=str(appdata),
        channel_socket=str(tmp_path / "c.sock"),
        ai_socket=str(tmp_path / "a.sock"),
    )
    await _start_session_until_appdata_resolved(session)

    # 真的 `record()`, 真的 `_jsonl_sink` —— 落点由 metrics 自己决定, 本用例不插手。
    await metrics.record("turn", 喵数=3)

    landed = appdata / "metrics" / f"{date.today().isoformat()}.jsonl"
    assert landed.is_file(), (
        f"metrics jsonl 没落进配置的 appdata 根。实际 PSI_APPDATA={os.environ.get('PSI_APPDATA')!r}, "
        f"配置值={str(appdata)!r} —— 这正是生产上两个私有容器每天报「未测到」的形状"
    )
    rows = [json.loads(line) for line in landed.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["喵数"] == 3


@pytest.mark.anyio
async def test_platformdirs_fallback_root_stays_empty_when_appdata_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 `appdata:` 就不许再往 platformdirs 兜底根写。

    与上一条不是重复: 上一条只证"配置的目录里有", 若实现改成**两个根都写**它照样绿,
    而那等于生产上那份数据仍旧一半落在容器里不存在的路径上。这条钉住兜底根没被碰过。

    兜底根用 `_APPDATA_APPNAME` 真名在一个临时 HOME 下构造 —— 不写死
    `/root/.local/share/Haitun` 那个 Linux 字面量, 否则这条判据在 Windows 上恒真。
    """
    workspace = tmp_path / "ws"
    appdata = tmp_path / "configured-appdata"
    fallback_home = tmp_path / "fallback-home"
    await anyio.Path(workspace).mkdir()
    await anyio.Path(appdata).mkdir()
    await anyio.Path(fallback_home).mkdir()

    # 把 platformdirs 的用户数据根指到一个空目录上: 之后只要那里冒出任何东西,
    # 就说明有写操作绕过了配置的根。三个平台各看一个变量, 全设上最省事。
    # 用 monkeypatch 而非直接写 os.environ: HOME / APPDATA 这类变量泄漏出去会让**后面
    # 别的用例**去一个临时目录里找用户数据, 而那种串味的症状离现场很远。
    for var in ("XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(fallback_home))

    session = Session(
        workspace=str(workspace),
        appdata=str(appdata),
        channel_socket=str(tmp_path / "c.sock"),
        ai_socket=str(tmp_path / "a.sock"),
    )
    await _start_session_until_appdata_resolved(session)
    await metrics.record("turn", 喵数=1)

    strays = list(fallback_home.rglob("*.jsonl"))
    assert strays == [], f"有 metrics 落到了 platformdirs 兜底根: {strays}"
    assert (appdata / "metrics" / f"{date.today().isoformat()}.jsonl").is_file()
