"""第 2 类: 资源与 OOM。

## 为什么 OOM 只能看 dmesg

2026-09 gateway 反复 502, 根因是 memcg OOM 撞 3g 上限。当时查的三处判据**全是假阴性**:

1. `docker inspect` 的 `.State.OOMKilled` —— **`false`**。被杀的是 python 的子进程,
   而 OOMKilled 只反映容器主进程(PID 1)。容器状态一直是 `Up`;
2. 容器 `Up` + 端口 `LISTEN` —— 两者都绿;
3. 应用日志 —— 报错长得像上游返回异常, 一个字都没提内存。

于是这类故障**零告警**。唯一靠得住的是 `dmesg` 里内核那行 `Out of memory: Killed process`
/ `oom-kill:`。所以本模块**不使用 `docker inspect` 的 OOMKilled** 判 OOM, 判据
`test_oom_probe_does_not_rely_on_docker_inspect` 断言命令行里压根没有 `inspect`。

## 为什么 swappiness 要看「声明值」不是运行时值

阿里云的镜像在 `/etc/sysctl.d/` 里**显式声明** `vm.swappiness=0`。有人用 `sysctl -w` 把它
改成 10 之后, 运行时值是 10、看着修好了 —— 但重启必回滚成 0, OOM 随之回来。

所以判据要同时给两个数: 运行时值, 与 `sysctl.d` 里的声明。两者不一致本身就是 BAD(意味着
当前状态活不过下一次重启)。看加载顺序的理由: `sysctl --system` 按文件名排序依次加载, 后
加载的覆盖先加载的, 所以**最后一个声明**才是重启后的实际值。

## `docker logs --since` 的坑(本模块不用它, 但写下来防复现)

`--since` 写 `HH:MM` **静默失效**: 它只吐一行错误消息就退出, 于是 `grep` 完看起来像「没
触发」。必须写完整时间戳(`2026-09-20T08:57:00`)。我因此误报过一次定时任务没跑。
"""

from __future__ import annotations

import glob
import re
import subprocess
import time
from pathlib import Path

from findings import Finding, bad, ok, unknown

#: 可用内存告警线 GiB。A 机搬来 ToC 之后实测可用仅 1.6G 且 swappiness=0 —— 那之后
#: 全局 OOM 开始出现。1.5 是「已经在出事的水位」, 不是舒适线。
MEM_WARN_GIB = 1.5
#: 磁盘使用率告警线 %。
DISK_WARN_PCT = 85
#: inode 使用率告警线 %。
INODE_WARN_PCT = 85


def _run(args: list[str], runner, timeout: int = 20) -> tuple[str, str, bool]:
    """跑一个命令, 返回 (stdout, 错误说明, 是否跑成功)。

    「跑不起来」与「跑起来了但结果是 0」必须分开 —— 前者是 UNKNOWN(探针瞎了), 后者是
    数据。这是整个报告的第一原则, 见 findings.py 的 docstring。
    """
    try:
        proc = runner(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{type(exc).__name__}: {exc}", False
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return "", f"退出码 {proc.returncode}: {(proc.stderr or '').strip()[:200]}", False
    return proc.stdout or "", "", True


#: OOM 计入异常的时间窗(小时)。见 `probe_oom_kills` 的 docstring: 不设窗的话缓冲区里的
#: 陈旧条目会天天报成今天的异常。
OOM_WINDOW_HOURS = 24

#: `dmesg -T` 的时间戳形如 `[Sat Sep 12 08:44:22 2026]`。
_DMESG_TS = re.compile(r"^\[([A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d \d{4})\]")


def _dmesg_timestamp(line: str) -> float | None:
    """`dmesg -T` 行首时间戳 → epoch 秒。解析不出返回 None。

    `-T` 打的是**本地时间**, 所以用 `mktime` 而不是 `timegm` —— 这跟证书那侧刻意相反
    (证书时刻是 UTC)。两处用同一个函数会偏 8 小时。
    """
    m = _DMESG_TS.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def probe_oom_kills(*, runner=subprocess.run, now: float | None = None) -> Finding:
    """内核 OOM kill 计数。**只看 dmesg**, 见模块 docstring。

    ## 为什么必须按时间窗过滤

    2026-09-21 实测: 缓冲区里有 3 条 OOM, 时间戳全是 **9-12** —— 9 天前。宿主
    `up 41 days`, 环形缓冲没被冲掉, 所以不设窗的话这 3 条会**天天**出现在日报的「异常」
    里, 直到缓冲区自己滚掉。连续几天之后这一项就会被读者无视, 而那时真的新增一条也没人
    看得出来 —— 一条恒定为真的告警等于没有告警。

    原先那版只在 semantics 里写了「计的是缓冲区现存条数, 不是累计」, 但**没把时间戳打
    出来**。读者看到「内核 OOM kill 次数: 3」的第一反应必然是今天出事了。

    所以: 窗内(默认 24h)的条数才升级为异常, 窗外的仍然报出来但只作背景。两者都带**时间
    戳**, 让「什么时候的事」不用去猜。解析不出时间戳的行按窗内算 —— 宁可多报一次, 不能
    把一条真的 OOM 因为格式没对上而静默丢掉。
    """
    name = "内核 OOM kill 次数"
    baseline = f"最近 {OOM_WINDOW_HOURS}h 内 0"
    direction = "> 0 = 有进程被内核杀掉"
    semantics = "只看 dmesg。docker inspect 的 OOMKilled 在子进程被杀时仍是 false, 容器还显示 Up —— 那是零告警的成因"
    out, err, alive = _run(["dmesg", "-T"], runner, timeout=30)
    if not alive:
        return unknown(
            name,
            reason=f"dmesg 读不到({err}) —— 容器内跑或非 root 时会这样, 本探针必须在宿主以 root 跑",
            baseline=baseline,
            direction=direction,
            semantics=semantics,
        )
    hits = [line for line in out.splitlines() if "oom-kill:" in line or "Out of memory: Killed process" in line]
    cutoff = (time.time() if now is None else now) - OOM_WINDOW_HOURS * 3600
    recent, stale = [], []
    for line in hits:
        stamp = _dmesg_timestamp(line)
        (recent if stamp is None or stamp >= cutoff else stale).append(line)
    semantics += f"; 只有最近 {OOM_WINDOW_HOURS}h 内的计入异常, 更早的条目仍在缓冲区里但只作背景"

    def _fmt(lines: list[str]) -> str:
        # 时间戳在行首而细节在行尾, 两头都要 —— 只截尾部就是原先那版丢掉时间的原因。
        out_parts = []
        for line in lines[-3:]:
            m = _DMESG_TS.match(line)
            when = m.group(1) if m else "时间戳解析不出"
            out_parts.append(f"[{when}] …{line.strip()[-100:]}")
        return " | ".join(out_parts)

    if recent:
        value = f"{len(recent)} (最近 {OOM_WINDOW_HOURS}h: {_fmt(recent)})"
        if stale:
            value += f"; 窗外另有 {len(stale)} 条"
        # 进表的数是**窗内**条数, 不含窗外。窗外那些是陈旧记录, 计进去会让表上每天都显示
        # 同一个非零值 —— 那正是「陈旧 OOM 天天报成今天」那个假阳性在趋势表上的形态。
        # `urgent=True`: 窗内有 OOM 意味着刚刚有进程被内核杀掉, 服务此刻可能是残的(容器
        # 仍显示 Up, 那是零告警的成因)。同 wss, **只给这条 BAD 不给上面那条 UNKNOWN** ——
        # 「dmesg 读不到」是环境问题, 一成立就每轮成立, 会变成天天亮着的灯。
        return bad(
            name,
            value=value,
            baseline=baseline,
            direction=direction,
            semantics=semantics,
            num=float(len(recent)),
            num_warn=1.0,
            unit="次",
            urgent=True,
        )
    if stale:
        # 窗内为 0 才是 OK。窗外那些照旧打出来: 它们是「这台机器有 OOM 史」的证据, 排查
        # 内存问题时是线索, 但不该每天当新事故报。
        return ok(
            name,
            value=f"0 (窗外另有 {len(stale)} 条, 最早的仍在缓冲区: {_fmt(stale)})",
            baseline=baseline,
            direction=direction,
            semantics=semantics,
            num=0.0,
            num_warn=1.0,
            unit="次",
        )
    return ok(
        name, value="0", baseline=baseline, direction=direction, semantics=semantics, num=0.0, num_warn=1.0, unit="次"
    )


def probe_memory(*, runner=subprocess.run) -> list[Finding]:
    """可用内存与 swap 总量。

    两台机器的 swap 不同是已知事实: **B 零 swap, A 有 4G。** 所以「swap=0」在 B 上不是
    回归而是常态 —— 报告给数字与方向, 不擅自判定, 因为脚本不知道自己跑在哪台上。
    """
    out, err, alive = _run(["free", "-m"], runner)
    base_mem, dir_mem = f">= {MEM_WARN_GIB} GiB", "越低越糟; 1.6 GiB 那次伴随全局 OOM"
    if not alive:
        return [
            unknown("可用内存", reason=f"free 读不到: {err}", baseline=base_mem, direction=dir_mem),
            unknown(
                "swap 总量", reason=f"free 读不到: {err}", baseline="A 机 4G / B 机 0", direction="0 = 内存压力无缓冲"
            ),
        ]
    available_mib: int | None = None
    swap_total_mib: int | None = None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("Mem:") and len(parts) >= 7:
            available_mib = int(parts[6])
        elif parts and parts[0].startswith("Swap:") and len(parts) >= 2:
            swap_total_mib = int(parts[1])
    findings: list[Finding] = []
    if available_mib is None:
        findings.append(
            unknown("可用内存", reason="free -m 输出里没解析出 available 列", baseline=base_mem, direction=dir_mem)
        )
    else:
        gib = available_mib / 1024
        value = f"{gib:.2f} GiB"
        maker = bad if gib < MEM_WARN_GIB else ok
        # 进表用 GiB 而不是 MiB: 告警线 1.5 GiB 是拿 GiB 说的, 两个单位混着会让「表上的数
        # 和告警线对不上」。`num_warn` 与 `MEM_WARN_GIB` 同源, 不另写一个字面量。
        findings.append(
            maker(
                "可用内存",
                value=value,
                baseline=base_mem,
                direction=dir_mem,
                num=round(gib, 2),
                num_warn=MEM_WARN_GIB,
                unit="GiB",
            )
        )
    if swap_total_mib is None:
        findings.append(
            unknown(
                "swap 总量",
                reason="free -m 输出里没有 Swap 行",
                baseline="A 机 4G / B 机 0",
                direction="0 = 内存压力无缓冲",
            )
        )
    else:
        findings.append(
            ok(
                "swap 总量",
                value=f"{swap_total_mib / 1024:.2f} GiB",
                baseline="A 机 4G / B 机 0",
                direction="0 = 内存压力无缓冲",
                semantics="两台机器本来就不同(B 零 swap、A 有 4G), 0 在 B 上是常态不是回归",
            )
        )
    return findings


def probe_swappiness(*, runner=subprocess.run, read_text=None) -> Finding:
    """swappiness 的**运行时值**与 **sysctl.d 声明值**, 两个都要。

    只看运行时值会被骗: `sysctl -w` 改完看着好了, 但重启必回滚到 `sysctl.d` 里的声明值。
    阿里云镜像**显式声明了 0**, 所以这不是理论风险。

    声明值取的是**最后一个**匹配 —— `sysctl --system` 按文件名排序依次加载, 后者覆盖前者,
    于是重启后生效的是最后那个。只 grep 出「有人声明过」是不够的。
    """
    name = "vm.swappiness"
    baseline = "运行时与声明值一致且 > 0"
    direction = "声明为 0 = 重启后 OOM 必回来"
    out, err, alive = _run(["sysctl", "-n", "vm.swappiness"], runner)
    runtime = (out or "").strip()
    if not alive or not runtime.isdigit():
        return unknown(name, reason=f"读不到运行时值: {err or runtime!r}", baseline=baseline, direction=direction)

    reader = read_text or (lambda p: Path(p).read_text(encoding="utf-8", errors="replace"))
    declared: str | None = None
    declared_in = ""
    # 加载顺序: /etc/sysctl.d/*.conf 与 /usr/lib/sysctl.d/*.conf 按文件名排序, 最后是
    # /etc/sysctl.conf。这里按同样顺序走, 取最后一个命中。
    candidates = sorted(glob.glob("/etc/sysctl.d/*.conf")) + sorted(glob.glob("/usr/lib/sysctl.d/*.conf"))
    candidates.append("/etc/sysctl.conf")
    for path in candidates:
        try:
            text = reader(path)
        except OSError:
            continue
        for line in text.splitlines():
            m = re.match(r"\s*vm\.swappiness\s*=\s*(\d+)", line)
            if m:
                declared, declared_in = m.group(1), path
    if declared is None:
        return ok(
            name,
            value=f"运行时 {runtime}, sysctl.d 无声明",
            baseline=baseline,
            direction=direction,
            semantics="无声明 = 重启后回内核默认 60, 不会被拉回 0",
        )
    value = f"运行时 {runtime}, 声明 {declared} ({declared_in})"
    semantics = "声明值才是重启后的实际值; sysctl -w 只改运行时。取的是加载顺序里最后一个声明"
    if declared != runtime or declared == "0":
        return bad(name, value=value, baseline=baseline, direction=direction, semantics=semantics)
    return ok(name, value=value, baseline=baseline, direction=direction, semantics=semantics)


def probe_container_memory(containers: tuple[str, ...], *, runner=subprocess.run) -> list[Finding]:
    """各容器当前内存用量与上限。

    **不读 `docker inspect` 的 OOMKilled**(它在子进程被杀时是 false)。这里给的是
    `docker stats` 的瞬时值与 limit —— 瞬时值抓不到峰值, 所以 semantics 里写明这一点:
    要判「有没有撞过顶」只能靠 dmesg 那条, 本条只回答「现在离顶有多远」。

    memcg 的历史峰值在 cgroup v1 有 `memory.max_usage_in_bytes`, v2 有 `memory.peak`
    (5.19+)。两者路径不同且宿主内核版本未实测, 所以峰值这一项**不猜**: 读得到就报,
    读不到报 UNKNOWN 带路径, 不拿瞬时值顶替。
    """
    if not containers:
        return [unknown("容器内存", reason="未配置容器名", baseline="每容器一条", direction="缺配置 = 这一类没人在看")]
    out, err, alive = _run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}", *containers],
        runner,
        timeout=40,
    )
    baseline, direction = "< 80% of limit", "接近 limit = memcg OOM 风险"
    semantics = "docker stats 是瞬时值, 抓不到峰值。「撞过顶没有」只能看 dmesg 那条"
    if not alive:
        return [
            unknown(
                "容器内存",
                reason=f"docker stats 跑不起来: {err}",
                baseline=baseline,
                direction=direction,
                semantics=semantics,
            )
        ]
    seen: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            seen[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    findings: list[Finding] = []
    for container in containers:
        if container not in seen:
            # 容器不存在 / 没起来。**UNKNOWN 不是 OK** —— 「容器没了」比「内存高」更糟,
            # 而 docker stats 对不存在的名字只是不输出那一行, 不报错。
            findings.append(
                unknown(
                    f"{container} 内存",
                    reason="docker stats 没有这一行 —— 容器可能没起或名字不对(容器名不是 compose service 名)",
                    baseline=baseline,
                    direction=direction,
                    semantics=semantics,
                )
            )
            continue
        usage, perc = seen[container]
        pct = float(perc.rstrip("%")) if perc.rstrip("%").replace(".", "", 1).isdigit() else None
        value = f"{usage} ({perc})"
        maker = bad if pct is not None and pct >= 80 else ok
        findings.append(
            maker(
                f"{container} 内存",
                value=value,
                baseline=baseline,
                direction=direction,
                semantics=semantics,
                # `pct is None` 时不进表(解析不出百分比), 而不是当 0 —— 见 findings.num 注释。
                num=pct,
                num_warn=80.0 if pct is not None else None,
                unit="%" if pct is not None else "",
            )
        )
    findings.append(_probe_memcg_peak(containers, runner=runner))
    return findings


def _probe_memcg_peak(containers: tuple[str, ...], *, runner=subprocess.run) -> Finding:
    """memcg 历史峰值。读不到就报 UNKNOWN 带路径, **不拿瞬时值顶替**。

    cgroup v1 与 v2 的文件名不同, 且 v2 的 `memory.peak` 要 5.19+ 内核。宿主内核版本本轮
    没实测, 所以这里两条都试、都读不到就如实报「未测到」。用瞬时值冒充峰值会让「撞过顶」
    这件事永远看不见 —— 正是 502 那次的失败模式。
    """
    name = "容器 memcg 历史峰值"
    baseline, direction = "< limit", "等于 limit = 撞过顶"
    parts: list[str] = []
    missing: list[str] = []
    for container in containers:
        got = ""
        for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
            out, _err, alive = _run(["docker", "exec", container, "cat", path], runner)
            digits = (out or "").strip()
            if alive and digits.isdigit():
                got = f"{container}={int(digits) / 1024 / 1024:.0f} MiB"
                break
        if got:
            parts.append(got)
        else:
            missing.append(container)
    if not parts:
        return unknown(
            name,
            reason=(
                "memory.peak 与 memory.max_usage_in_bytes 都读不到(cgroup v2 的 peak 要 5.19+ 内核; 宿主内核版本未实测)"
            ),
            baseline=baseline,
            direction=direction,
            semantics="不拿 docker stats 的瞬时值顶替 —— 那会让「撞过顶」永远看不见",
        )
    value = " ".join(parts) + (f"; 未测到: {','.join(missing)}" if missing else "")
    return ok(name, value=value, baseline=baseline, direction=direction, semantics="峰值是累计的, 重启容器才清零")


def probe_disk(*, runner=subprocess.run) -> list[Finding]:
    """磁盘与 inode 使用率。两者分开 —— inode 耗尽时磁盘空间可能还很空, 报错却是
    `No space left on device`, 只看空间会得出「空间够啊」的反向结论。
    """
    findings: list[Finding] = []
    for args, label, warn in (
        (["df", "-P", "/"], "磁盘使用率 /", DISK_WARN_PCT),
        (["df", "-iP", "/"], "inode 使用率 /", INODE_WARN_PCT),
    ):
        out, err, alive = _run(args, runner)
        baseline, direction = f"< {warn}%", "越高越糟"
        if not alive:
            findings.append(unknown(label, reason=f"df 跑不起来: {err}", baseline=baseline, direction=direction))
            continue
        pct = None
        for line in out.splitlines()[1:]:
            m = re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                break
        if pct is None:
            findings.append(
                unknown(
                    label, reason=f"df 输出里没有百分比: {out.strip()[:120]!r}", baseline=baseline, direction=direction
                )
            )
            continue
        maker = bad if pct >= warn else ok
        semantics = (
            "inode 与空间是两件事: inode 耗尽时空间可能还很空, 而报错同样是 No space left" if "inode" in label else ""
        )
        findings.append(
            maker(
                label,
                value=f"{pct}%",
                baseline=baseline,
                direction=direction,
                semantics=semantics,
                # 进趋势表。`pct` 在这里已经是解析好的整数, 不是从 value 字符串里再抠一遍
                # —— 抠不出来时最省事的写法是当 0, 而 0 与真实的低读数在一列数字里一样。
                num=float(pct),
                num_warn=float(warn),
                unit="%",
            )
        )
    return findings


def collect(cfg, *, runner=subprocess.run) -> list[Finding]:
    out: list[Finding] = [probe_oom_kills(runner=runner)]
    out.extend(probe_memory(runner=runner))
    out.append(probe_swappiness(runner=runner))
    out.extend(probe_container_memory(cfg.containers, runner=runner))
    out.extend(probe_disk(runner=runner))
    return out


def collect_fast(cfg, *, runner=subprocess.run) -> list[Finding]:
    """即时档只取 OOM 一项 —— 5 分钟一轮, `docker stats` 与 `docker exec` 太慢且有副作用。

    选 OOM 而不是内存水位: 水位高不等于出事, 而 dmesg 里出现 OOM kill 是**已经出事了**。
    """
    return [probe_oom_kills(runner=runner)]
