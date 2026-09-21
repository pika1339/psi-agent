"""`deploy/haitun/audit-workspace-drift.sh` 的参数与分类判据。

## 为什么这些判据存在

这个脚本是**投放生产的唯一判据** —— 它说「同/落后/领先/缺失」, 人就照它决定覆盖哪些文件。
所以它给出**错误结论**比直接报错更糟: 报错会让人停下来, 而一个假「缺失」会让人去覆盖本来
不该动的文件。

2026-09-18 就踩在这个位置上。生产的会议纪要 pipeline 整场失败在
`RuntimeError: 会议 SOP 配置缺失`, 而 `config/meeting-sop.yaml` 在 `origin/main` 里一直
存在 —— 根因不是文件没了, 而是 `config/` 这个子树**从来没进过投放清单**: 脚本写死了
`$PROD_ROOT/$W/tools`, 只认 `tools/`。

修的时候几乎又犯第二次同类错: 第一版把「subtree 参数」判成「长得像路径」(含斜杠), 于是
`audit ... workspace config` 这种最自然的写法会被当成一个 workspace 名, 脚本转而去审
`$PROD_ROOT/config/tools` 并报出一整片假「缺失」—— 一个审计工具给出错误结论, 且不报错。
所以这里的判据钉的是**参数解析**(值对得上白名单), 而不是它的实现方式。

## 为什么不跑 Windows

脚本依赖 `md5sum` / `stat -c` / bash 数组, 是给 Linux 目标机用的; 本仓 CI 也是
`ubuntu-latest`。非 POSIX 平台直接 skip, 不假装能测。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not Path("/bin/bash").exists(),
    reason="audit-workspace-drift.sh 依赖 bash/md5sum/stat, 只在 POSIX 上可测",
)

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "audit-workspace-drift.sh"

#: 两棵树各三个文件。用极小的内容 —— 这个测试要真跑 `git archive`, 不该拖成慢用例。
_FILES = ("alpha.yaml", "beta.yaml", "gamma.yaml")
_SUBTREE = "config"


def _repo_root() -> Path | None:
    """从脚本位置或 cwd 往上找一个 git 检出。"""
    for start in (_SCRIPT.parent, Path.cwd()):
        for cand in (start, *start.parents):
            if (cand / ".git").exists() and _SCRIPT.is_relative_to(cand):
                return cand
    return None


def _lf_copy() -> Path:
    """取脚本的 LF 版本。

    Windows 开发机上工作树那份可能带 CRLF, 而 bash 会把行尾的 `\\r` 当成 token 的一部分
    (`set -euo pipefail` 直接报 `invalid option name`), 表现成与脚本内容无关的失败。
    仓库已用 `.gitattributes` 的 `*.sh text eol=lf` 钉住提交内容是 LF; 这里优先从 git 里
    取一份, 既是取到提交面的真身, 也顺带让本用例在 CRLF 工作树上照样跑得动。

    写成「try 里只有一条语句」而不是 `except (OSError, X):`: 后者会被 ruff format 改写成
    PEP 758 的 `except OSError, X:`(无括号), 那个写法**只有 3.14+ 能解析** —— 本仓要求
    3.14, 但没必要让一个测试文件只在 3.14 上读得动。
    """
    out = Path(tempfile.gettempdir()) / f"audit-lf-{os.getpid()}.sh"
    root = _repo_root()
    if root is not None:
        rel = _SCRIPT.relative_to(root).as_posix()
        try:
            blob = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-p", f"HEAD:{rel}"],
                capture_output=True,
                timeout=30,
                check=False,
            ).stdout
        except Exception:
            # git 不在 / 超时 / 不是检出 —— 一律退回工作树那份。这里是探针代码,
            # 兜住所有失败比按异常类型枚举更符合意图(漏一类就是一个假的 skip)。
            blob = b""
        if blob.startswith(b"#!"):
            out.write_bytes(blob)
            return out
    # 不是 git 检出(或 git 不可用): 直接用工作树那份, 只保证行尾是 LF
    out.write_bytes(_SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    return out


_LF_COPY = _lf_copy()


def _run(*args: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_LF_COPY), *args],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), **env_extra},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """造一个真仓库 + 一个假生产根; 返回 (repo, prod_root)。

    **两棵子树都要在仓库里**: 默认 subtree 是 `tools`, 而 `git archive <sha> <subtree>`
    对不存在的路径直接 `fatal: pathspec ... did not match any files`(退出 2)。只造
    `config/` 的话, 那些走默认 tools 的用例会因为仓库里没有 `tools/` 而失败 —— 症状是
    「脚本什么都没输出就退出 2」, 与参数解析毫无关系。
    """
    repo = tmp_path / "repo"
    for sub in (_SUBTREE, "tools"):
        (repo / "agents" / "feishu" / sub).mkdir(parents=True)
    for name in _FILES:
        (repo / "agents" / "feishu" / _SUBTREE / name).write_text(f"# {name}\n", encoding="utf-8")
    (repo / "agents" / "feishu" / "tools" / "probe.py").write_text("# probe\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo, tmp_path / "prod"


def _deploy(repo: Path, prod: Path, workspace: str, names: tuple[str, ...]) -> Path:
    target = prod / workspace / _SUBTREE
    target.mkdir(parents=True)
    for name in names:
        shutil.copyfile(repo / "agents" / "feishu" / _SUBTREE / name, target / name)
    return target


def _summary(out: str) -> str:
    """取出 `    同=N    落后=N    领先=N    缺失=N    生产独有=N` 行。"""
    for line in out.splitlines():
        if "同=" in line and "缺失=" in line:
            return " ".join(line.split())
    raise AssertionError(f"输出里没有分类行:\n{out}")


def test_all_identical_reports_no_drift(tree: tuple[Path, Path]) -> None:
    repo, prod = tree
    _deploy(repo, prod, "ws", _FILES)

    r = _run("main", "ws", _SUBTREE, "*.yaml", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert r.returncode == 0, r.stdout + r.stderr
    assert _summary(r.stdout) == "同=3 落后=0 领先=0 缺失=0 生产独有=0"
    # 审的必须是 config, 不是 tools —— 写死 tools 的那版会在这里报出一片假缺失
    assert f"范围: agents/feishu/{_SUBTREE}" in r.stdout


def test_missing_file_is_reported(tree: tuple[Path, Path]) -> None:
    repo, prod = tree
    _deploy(repo, prod, "ws", _FILES[:1])

    r = _run("main", "ws", _SUBTREE, "*.yaml", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert _summary(r.stdout) == "同=1 落后=0 领先=0 缺失=2 生产独有=0"
    assert "beta.yaml" in r.stdout and "gamma.yaml" in r.stdout


def test_ahead_file_blocks_with_exit_1(tree: tuple[Path, Path]) -> None:
    """就地写入(内容不在任何 revision 里)必须判「领先」并以 1 退出, 而不是被覆盖。"""
    repo, prod = tree
    target = _deploy(repo, prod, "ws", _FILES)
    (target / "alpha.yaml").write_text("# 就地改的, 从未提交\n", encoding="utf-8")

    r = _run("main", "ws", _SUBTREE, "*.yaml", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert _summary(r.stdout) == "同=2 落后=0 领先=1 缺失=0 生产独有=0"
    assert r.returncode == 1, "有『领先』文件时必须退出 1, 否则调用方会继续覆盖"


def test_stale_file_is_safe_to_overwrite(tree: tuple[Path, Path]) -> None:
    """回退到上一版的文件必须判「落后」(可安全覆盖), 而不是「领先」。"""
    repo, prod = tree
    alpha = repo / "agents" / "feishu" / _SUBTREE / "alpha.yaml"
    old = "# 第一版\n"
    alpha.write_text(old, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    alpha.write_text("# 第二版\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")
    target = _deploy(repo, prod, "ws", _FILES)
    (target / "alpha.yaml").write_text(old, encoding="utf-8")

    r = _run("main", "ws", _SUBTREE, "*.yaml", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert _summary(r.stdout) == "同=2 落后=1 领先=0 缺失=0 生产独有=0"
    assert r.returncode == 0, "『落后』是可安全覆盖的, 不该让调用方停下"


def test_absent_subtree_dir_is_skipped_not_faked(tree: tuple[Path, Path]) -> None:
    repo, prod = tree
    prod.mkdir(exist_ok=True)

    r = _run("main", "ws", _SUBTREE, "*.yaml", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert r.returncode == 0
    assert "跳过 ws" in r.stdout
    assert "缺失=" not in r.stdout, "目录不存在时不该伪造出一片『缺失』"


def test_subtree_match_is_exact_not_substring(tree: tuple[Path, Path]) -> None:
    """subtree 名必须**逐字**匹配, 不能被任意包含它的 workspace 名冒充。

    哨兵名 `ok` 被刻意选成 `tools` 的子串: 子串匹配(第一版写法)会把它吃掉、判成
    `subtree=ok`, 于是去审一个不存在的子树; WORKSPACES 变空又回落成默认三份, 症状是
    「只传了一份却报三份的差异」—— 与传入的名字毫无关系, 极难从输出反推。
    """
    repo, prod = tree
    target = prod / "ok" / "tools"
    target.mkdir(parents=True)
    shutil.copyfile(repo / "agents" / "feishu" / "tools" / "probe.py", target / "probe.py")

    r = _run("main", "ok", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    # 默认子树仍是 tools, workspace `ok` 被原样审到(它的 probe.py 与仓库一致)
    assert "范围: agents/feishu/tools" in r.stdout, r.stdout
    assert "=== ok" in r.stdout, f"workspace `ok` 没被当成 workspace:\n{r.stdout}"
    assert _summary(r.stdout) == "同=1 落后=0 领先=0 缺失=0 生产独有=0", r.stdout
    assert "workspace-luolin" not in r.stdout, "不该回落成默认三份 workspace"


def test_default_subtree_is_still_tools(tree: tuple[Path, Path]) -> None:
    """不传 subtree 时行为与改动前一致 —— 既有调用方(文档 / 跑道)一字不用改。"""
    repo, prod = tree
    (prod / "ws" / "tools").mkdir(parents=True)

    r = _run("main", "ws", cwd=repo, env_extra={"PROD_ROOT": str(prod)})

    assert "范围: agents/feishu/tools" in r.stdout
    assert "(*.py)" in r.stdout


def test_committed_script_is_lf_only() -> None:
    """提交面必须是纯 LF。

    这个脚本是 `cp` 上生产机跑的; CRLF 的 shell 脚本在 Linux 上会
    `bad interpreter: no such file or directory`, 而在 Windows 上编辑一次就可能被
    `core.autocrlf=true` 转成 CRLF —— 提交不报错, 上机才死。`.gitattributes` 的
    `*.sh text eol=lf` 已经钉了转换, 这里钉的是**结果**。
    """
    in_repo = _SCRIPT.parents[2]
    rel = _SCRIPT.relative_to(in_repo).as_posix()
    proc = subprocess.run(
        ["git", "-C", str(in_repo), "cat-file", "-p", f"HEAD:{rel}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("不是 git 检出, 无法取提交面内容")
    assert b"\r" not in proc.stdout, "提交的 audit-workspace-drift.sh 含 CR, 上 Linux 会直接死"
