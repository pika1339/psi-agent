"""第 1 类: 公网可达性 —— 权重最高的一类。

## 为什么这一类要从外面打

502 那 29 小时里, **容器内自检全绿**。`docker ps` 显示 Up、容器内 `curl 127.0.0.1:8080`
通、飞书定时卡片照发 —— 三个信号同时说「健康」, 而公网上的用户看到的是 502。根因是
gateway 重建后 oauth-proxy 挂在了死掉的 netns 上(它是 `network_mode: "service:gateway"`)。

从这次得到的判据原则: **只有从公网打才算数**, 容器内的任何自检都不构成入站可达的证据。

## 四个各自独立的假阴性

| 探针 | 它上次怎么骗过我们 |
| --- | --- |
| vhost HTTPS 状态码 | 见上: 容器内全绿而公网 502 |
| OAuth 回调 | 定时任务与发卡片走**出站**, 回调走**入站**。出站全绿不证明入站活着 |
| TLS 剩余天数 | `caddy validate` 绿而 reload 失败 —— 服务仍 active 跑**旧配置**, 新 vhost 静默没生效 |
| 8090 | netns 死了 8090 照样 `LISTEN`。`Up` 与 `LISTEN` 是两个并列的假阴性, 只有**真打一次请求**算数 |

## 硬要求: `curl --resolve`, 不是 `-H Host`

要验「这个域名在这台机器上能不能打通」, 直觉写法是 `curl -H "Host: x.com" https://<ip>/`。
**它是错的**: `-H` 只改 HTTP 请求头, TLS 握手的 SNI 仍然是那个 IP, 于是服务端在 TLS 层就
`alert`, curl 报 `HTTP=000`。而 `000` 与真的服务挂了**不可区分** —— 会假报一次服务宕机。

`--resolve host:port:ip` 改的是 curl 的 DNS 解析结果, SNI 与 Host 头都是正确的域名, 走的是
和真实用户完全一样的路径。`_curl_args` 里钉住了这一点, 判据
`test_vhost_probe_uses_resolve_not_host_header` 断言命令行里有 `--resolve`、没有 `-H Host`。
"""

from __future__ import annotations

import calendar
import re
import subprocess
import time
from dataclasses import dataclass

from findings import Finding, bad, ok, unknown

#: TLS 证书剩余天数的告警线。低于此值进 BAD。
#: 30 天: Let's Encrypt 是 90 天有效期、60 天自动续, 落到 30 天说明自动续已经失败一轮。
TLS_WARN_DAYS = 30

#: curl 输出里状态码那一行的前缀 —— 我们用 `-w` 自己定格式, 不解析 HTTP 头。
_CODE_MARK = "HTTPCODE:"


@dataclass
class CurlResult:
    """一次 curl 的结果。`code == "000"` 表示连不上或 TLS 失败, **不是** HTTP 状态码。"""

    code: str
    stderr: str
    ok: bool


def _curl_args(url: str, *, resolve: str = "", timeout: int = 10, method: str = "GET") -> list[str]:
    """拼 curl 命令行。**判据直接断言这个函数的返回值**, 因为 `--resolve` vs `-H Host`
    的区别只在命令行上看得见 —— 跑起来之后两者都只是「连不上」。

    `-o /dev/null` + `-w` 自定义格式: 只要状态码, 不要响应体(响应体可能很大, 也可能含
    用户内容)。`--max-time` 而非 `--connect-timeout`: 要挡的是「连上了但一直不回」,
    502 那次 caddy 就是连得上的。
    """
    args = [
        "curl",
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--write-out",
        f"{_CODE_MARK}%{{http_code}}",
        "--max-time",
        str(timeout),
    ]
    if method != "GET":
        args += ["--request", method]
    if resolve:
        # 这一条是硬要求, 见模块 docstring。不要换成 -H Host。
        args += ["--resolve", resolve]
    args.append(url)
    return args


def _run_curl(args: list[str], *, runner=subprocess.run) -> CurlResult:
    try:
        proc = runner(args, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        # curl 不存在或跑不起来 —— 这是**探针自己坏了**, 必须报 UNKNOWN 而不是 BAD。
        # 报成 BAD 会让人去查服务, 而问题在监控机上。
        return CurlResult(code="", stderr=f"{type(exc).__name__}: {exc}", ok=False)
    out = proc.stdout or ""
    match = re.search(rf"{_CODE_MARK}(\d+)", out)
    code = match.group(1) if match else ""
    return CurlResult(code=code, stderr=(proc.stderr or "").strip(), ok=bool(code))


def probe_vhost(host: str, expect: int, public_ip: str, *, timeout: int = 10, runner=subprocess.run) -> Finding:
    """从公网打一个 vhost 的 HTTPS 根路径。

    `public_ip` 为空时**不退化成直接解析 DNS**, 而是报 UNKNOWN: 走系统 DNS 意味着可能打到
    别的机器上(境内 A / 境外 B 两台都跑过同一套栈), 那种情况下「绿」证明不了这台机器活着。
    这正是「兜底链提前终止」那类坑 —— 少一个输入就悄悄换一条语义不同的路径。
    """
    if not public_ip:
        return unknown(
            f"vhost {host} HTTPS",
            reason="未配置 public_ip, 无法用 --resolve 定向到目标机; 走系统 DNS 可能打到另一台机器",
            baseline=str(expect),
            direction=f"应为 {expect}",
        )
    result = _run_curl(
        _curl_args(f"https://{host}/", resolve=f"{host}:443:{public_ip}", timeout=timeout),
        runner=runner,
    )
    if not result.ok:
        return unknown(
            f"vhost {host} HTTPS",
            reason=f"curl 没能给出状态码: {result.stderr or '无 stderr'}",
            baseline=str(expect),
            direction=f"应为 {expect}",
        )
    if result.code == "000":
        return bad(
            f"vhost {host} HTTPS",
            value="000 (连不上 / TLS 握手失败)",
            baseline=str(expect),
            direction=f"应为 {expect}",
            semantics="000 不是 HTTP 状态码。已用 --resolve 故排除了 SNI 打裸 IP 那种假报",
        )
    if result.code != str(expect):
        return bad(
            f"vhost {host} HTTPS",
            value=result.code,
            baseline=str(expect),
            direction=f"应为 {expect}",
            semantics="从公网打的, 容器内自检全绿不构成反证 —— 502 那 29 小时就是这样过去的",
        )
    return ok(f"vhost {host} HTTPS", value=result.code, baseline=str(expect), direction=f"应为 {expect}")


def probe_oauth_callback(
    host: str, public_ip: str, path: str = "/oauth/callback", *, timeout: int = 10, runner=subprocess.run
) -> Finding:
    """实打一次 OAuth 回调路径 —— 这是**入站**那条路。

    为什么它要单独一条, 而不是跟在 vhost 根路径后面: 定时任务与发飞书卡片走的是**出站**
    HTTP(只起 gateway 也会真发卡片), 那些全绿时入站可以是死的。而 OAuth 回调是唯一一条
    「外部请求打进来」的必经路径, 它不通则登录整个不可用。

    判据是**不为 000 且不为 404**: 不带 code 参数打这条路径, 后端会回 4xx/302/HTML 报错页
    —— 那都说明这一跳活着。`404` 是白名单这一层没放行(oauth-proxy 白名单外一律 404),
    `000` 是压根没连上。
    """
    if not public_ip:
        return unknown(
            f"OAuth 回调 {path}",
            reason="未配置 public_ip, 无法用 --resolve 定向到目标机",
            baseline="非 000 / 非 404",
            direction="000=入站断; 404=白名单未放行",
        )
    result = _run_curl(
        _curl_args(f"https://{host}{path}", resolve=f"{host}:443:{public_ip}", timeout=timeout),
        runner=runner,
    )
    baseline, direction = "非 000 / 非 404", "000=入站断; 404=白名单未放行"
    semantics = "走的是入站路径。出站(定时任务/发卡片)全绿不证明这条活着"
    if not result.ok:
        return unknown(
            f"OAuth 回调 {path}",
            reason=f"curl 没能给出状态码: {result.stderr or '无 stderr'}",
            baseline=baseline,
            direction=direction,
        )
    if result.code in ("000", "404"):
        return bad(f"OAuth 回调 {path}", value=result.code, baseline=baseline, direction=direction, semantics=semantics)
    return ok(f"OAuth 回调 {path}", value=result.code, baseline=baseline, direction=direction, semantics=semantics)


def probe_oauth_proxy_port(port: int = 8090, *, timeout: int = 10, runner=subprocess.run) -> Finding:
    """真打一次 8090。**不看 `LISTEN`、不看 `docker ps` 的 Up。**

    两个并列的假阴性: netns 死掉之后 `docker-proxy` 仍在宿主上监听 8090(于是 `ss` 显示
    LISTEN), 而容器状态仍是 Up(它的进程没死, 死的是网络命名空间)。两者都绿, 公网 502。
    只有真发一次请求才能穿过这两层。

    **8090 是 oauth-proxy, 不是 gateway。** gateway 在容器内听 8080 且不对外暴露。把 8090
    当成 gateway 直连会得出反向的错误根因(会以为 gateway 挂了, 而实际是代理这一跳断了)。

    期望值是 404: oauth-proxy 对白名单外的路径一律 404, 而 `/` 不在白名单里 —— **404 恰好
    是「代理活着并且白名单在生效」的证据**。000 才是断了。
    """
    result = _run_curl(_curl_args(f"http://127.0.0.1:{port}/", timeout=timeout), runner=runner)
    name = f"oauth-proxy {port} 实打"
    baseline, direction = "404", "000=netns 或进程断; 非 404 = 白名单可能失效"
    semantics = "这一跳是 oauth-proxy 不是 gateway(gateway 在容器内听 8080)。LISTEN 与 Up 都不是判据"
    if not result.ok:
        return unknown(
            name, reason=f"curl 没能给出状态码: {result.stderr or '无 stderr'}", baseline=baseline, direction=direction
        )
    if result.code == "000":
        return bad(
            name,
            value="000 (连不上 —— netns 可能已失效)",
            baseline=baseline,
            direction=direction,
            semantics=semantics,
        )
    return ok(name, value=result.code, baseline=baseline, direction=direction, semantics=semantics)


def probe_tls_days(host: str, public_ip: str, *, timeout: int = 10, runner=subprocess.run) -> Finding:
    """证书剩余天数 —— 从**真实握手**拿, 不读磁盘上的证书文件。

    读文件的写法会被这个坑到: `caddy validate` 语法绿、reload 却失败, 服务仍 active 跑着
    **旧配置** —— 磁盘上是新证书而线上发的是旧的。只有握手拿到的那份才是用户看到的那份。

    `--resolve` 同样必须, 理由见模块 docstring。
    """
    name = f"TLS 证书 {host} 剩余天数"
    baseline, direction = f">= {TLS_WARN_DAYS} 天", "越少越糟; 低于阈值说明自动续期已失败一轮"
    if not public_ip:
        return unknown(
            name, reason="未配置 public_ip, 无法用 --resolve 定向到目标机", baseline=baseline, direction=direction
        )
    # curl --cert-status 不给天数, 所以走 openssl。-servername 是 SNI, 与 --resolve 同理:
    # 没有它就是拿 IP 当 SNI, 会取到默认 vhost 的证书甚至直接握手失败。
    args = [
        "openssl",
        "s_client",
        "-connect",
        f"{public_ip}:443",
        "-servername",
        host,
        "-verify_return_error",
    ]
    try:
        proc = runner(args, capture_output=True, text=True, timeout=timeout + 5, check=False, input="")
    except (OSError, subprocess.SubprocessError) as exc:
        return unknown(
            name, reason=f"openssl 跑不起来: {type(exc).__name__}: {exc}", baseline=baseline, direction=direction
        )
    match = re.search(r"NotAfter\s*:\s*(.+)|notAfter=(.+)", proc.stdout or "")
    if not match:
        return unknown(
            name,
            reason=f"openssl 输出里没有 notAfter: {(proc.stderr or '').strip()[:200] or '无 stderr'}",
            baseline=baseline,
            direction=direction,
        )
    raw = (match.group(1) or match.group(2) or "").strip()
    days = _days_until(raw)
    if days is None:
        return unknown(name, reason=f"notAfter 解析不出日期: {raw!r}", baseline=baseline, direction=direction)
    value = f"{days} 天 (至 {raw})"
    semantics = "从真实握手取, 不读磁盘 —— caddy reload 失败时磁盘是新的而线上是旧的"
    if days < TLS_WARN_DAYS:
        return bad(name, value=value, baseline=baseline, direction=direction, semantics=semantics)
    return ok(name, value=value, baseline=baseline, direction=direction, semantics=semantics)


def _days_until(raw: str) -> int | None:
    """解析 openssl 的 `notAfter` 时刻, 返回距今天数。解析不出返回 None(→ UNKNOWN)。"""
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            parsed = time.strptime(raw.replace("  ", " "), fmt)
        except ValueError:
            continue
        # 证书时刻是 UTC, 故用 timegm 而不是 mktime(后者按本地时区解释, 会偏 8 小时)。
        return int((calendar.timegm(parsed) - time.time()) // 86400)
    return None


def _netns_pid(container: str, *, runner=subprocess.run) -> tuple[int | None, str]:
    """容器的 netns 宿主 PID。失败时返回 `(None, 原因)`。

    `.State.Pid` 对 gateway 给的可能是 launcher 而不是 python —— 但这里要的正是 netns,
    而 launcher 与它的子进程同一个 netns, 所以这个 PID 恰好够用。**不要**据此去 `/proc`
    找 python 自己的东西。
    """
    try:
        proc = runner(
            ["docker", "inspect", "-f", "{{.State.Pid}}", container],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"docker inspect 跑不起来: {type(exc).__name__}: {exc}"
    raw = (proc.stdout or "").strip()
    if not raw.isdigit() or raw == "0":
        # PID 为 0 意味着容器没在跑。这跟「跑着但没连接」是两件事, 不能都落成 0 条连接。
        return None, f"拿不到 netns PID(容器可能没在跑): {raw[:60]!r}"
    return int(raw), ""


def probe_feishu_wss(containers: tuple[str, ...], *, runner=subprocess.run) -> Finding:
    """飞书长连接数。

    **wss=0 只证明收不到, 不证明发不出。** 这个语义必须跟着数字一起进报告: 飞书是出站
    wss 长连接(不是入站 webhook), 而 schedule runner 发卡片走的是**出站 HTTP**, 完全不
    需要这条长连接 —— 实测只起 gateway、wss=0 的情况下卡片照样真发出去了。

    所以读者看到 wss=0 时该得的结论是「用户发消息进不来」, 不是「机器人整个死了」;
    反过来看到 wss>0 也不能推出「一切正常」。不写这句话, 报告就会把人引向反方向。

    ## 为什么从宿主 nsenter 而不是 docker exec

    2026-09-21 实测: **容器里没有 `ss`**(`command -v ss` 为空)。原先那版走
    `docker exec ... sh -c "ss ... 2>/dev/null | grep -c ':443'"`, 于是「命令不存在」的
    stderr 被 `2>/dev/null` 吞掉, 空输出喂给 `grep -c` 得到一个**合法的 `0`** ——
    `isdigit()` 通过, 探针报「0 条连接」并升级为异常。

    那一刻的真相是近 30 分钟有 9 个不同 open_id 正在跑。也就是说这条判据恒定报
    「用户进不来」, 而真的进不来时它报的是**同一句话**, 两者不可区分。

    改从宿主进容器 netns 数(`nsenter -t <pid> -n ss`), 宿主有 `ss`。关键是**不再把
    stderr 重定向掉**: 命令缺失/权限不足一律走 UNKNOWN, 绝不退化成 0。
    """
    name = "飞书 wss 长连接数"
    baseline, direction = ">= 1 每建连容器", "0 = 收不到用户消息"
    semantics = "wss=0 只证明**收不到**, 不证明发不出 —— 发卡片走出站 HTTP, 不用这条连接"
    if not containers:
        return unknown(
            name,
            reason="未配置建连容器名(wss_containers 为空), 整项未采集",
            baseline=baseline,
            direction=direction,
            semantics=semantics,
        )
    counts: list[str] = []
    nums: list[int] = []
    for container in containers:
        pid, why = _netns_pid(container, runner=runner)
        if pid is None:
            return unknown(
                name,
                reason=f"{container}: {why}",
                baseline=baseline,
                direction=direction,
                semantics=semantics,
            )
        try:
            # 刻意不加 `2>/dev/null`, 也刻意不在 shell 里 `grep -c` —— 计数在 Python 这侧
            # 做。那两样合起来正是上一版把「没这个命令」变成「0 条连接」的成因。
            proc = runner(
                ["nsenter", "-t", str(pid), "-n", "ss", "-tn", "state", "established"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return unknown(
                name,
                reason=f"{container}: nsenter 跑不起来({type(exc).__name__}: {exc}) —— 本探针须在宿主以 root 跑",
                baseline=baseline,
                direction=direction,
                semantics=semantics,
            )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
            return unknown(
                name,
                reason=f"{container}: nsenter/ss 失败({err[:100]}) —— 宿主缺 ss 或非 root",
                baseline=baseline,
                direction=direction,
                semantics=semantics,
            )
        # `ss` 的首行是表头(`Recv-Q Send-Q ...`), 不含 `:443`, 自然不会被数进来。
        n = sum(1 for line in (proc.stdout or "").splitlines() if ":443" in line)
        counts.append(f"{container}={n}")
        nums.append(n)
    value = " ".join(counts)
    # 进表的数取**最小**那个容器, 不是总和。和会掩盖「三个容器里有一个掉到 0」——
    # 那一个容器的用户消息已经没人收了, 而总和看起来仍然很健康。
    low = float(min(nums)) if nums else None
    if any(c.endswith("=0") for c in counts):
        return bad(
            name,
            value=value,
            baseline=baseline,
            direction=direction,
            semantics=semantics,
            num=low,
            num_warn=1.0,
            unit="条",
        )
    return ok(
        name, value=value, baseline=baseline, direction=direction, semantics=semantics, num=low, num_warn=1.0, unit="条"
    )


def collect(cfg, *, runner=subprocess.run) -> list[Finding]:
    """第 1 类全部探针。任何一个探针抛出都不该带垮其余的 —— 见 run.py 的 `_guard`。"""
    out: list[Finding] = []
    if not cfg.vhosts:
        out.append(
            unknown(
                "vhost HTTPS 状态码",
                reason="配置里没有 vhosts, 整类未采集(这本身是配置缺失, 不是'全部健康')",
                baseline="每个 vhost 一条",
                direction="缺配置 = 这一类没人在看",
            )
        )
    for host, expect in cfg.vhosts:
        out.append(probe_vhost(host, expect, cfg.public_ip, timeout=cfg.timeout_seconds, runner=runner))
        out.append(probe_tls_days(host, cfg.public_ip, timeout=cfg.timeout_seconds, runner=runner))
    if cfg.vhosts:
        primary = cfg.vhosts[0][0]
        out.append(
            probe_oauth_callback(
                primary, cfg.public_ip, cfg.oauth_callback_path, timeout=cfg.timeout_seconds, runner=runner
            )
        )
    out.append(probe_oauth_proxy_port(cfg.oauth_proxy_port, timeout=cfg.timeout_seconds, runner=runner))
    # `wss_containers` 而不是 `containers`: 私有容器设计上不建长连接, 见 config 那侧的
    # DEFAULT_WSS_CONTAINERS。传 `containers` 会为两个健康容器天天报异常。
    out.append(probe_feishu_wss(cfg.wss_containers, runner=runner))
    return out
