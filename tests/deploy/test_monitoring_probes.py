"""`deploy/haitun/monitoring/` 的探针与渲染判据。

## 这些判据守的是「上次它怎么骗过我们」那一列

每条判据对应一个**实测过的假阴性**, 注释里写明是哪一次。不写来源的话, 后人会把这些看似
多余的断言删掉 —— 它们看起来都像在测显而易见的事。

* `--resolve` 而非 `-H Host`: 后者 SNI 仍是裸 IP, TLS 层就 alert, `HTTP=000` 与真 502
  不可区分, 会**假报一次服务宕机**;
* OOM 不看 `docker inspect`: 子进程被杀时 `OOMKilled` 仍是 `false`, 容器还显示 Up;
* swappiness 要看声明值: `sysctl -w` 只改运行时, 重启必回滚, OOM 就回来;
* 「未测到」与「零」分栏: 量 `live render` 得 0 行不是没渲染, 是那行为 DEBUG 而生产跑 INFO。

## 兜底链判据用不存在的资源名

用真名会让「目录在但内容不全导致兜底链提前终止」全绿通过。所以这里一律用 RFC 2606 保留
域名(`*.invalid`)、RFC 5737 文档地址(`203.0.113.0/24`)与仓库里不存在的路径名。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

_MON = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "monitoring"


def _load(name: str) -> ModuleType:
    if str(_MON) not in sys.path:
        sys.path.insert(0, str(_MON))
    spec = importlib.util.spec_from_file_location(name, _MON / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


findings = _load("findings")
config = _load("config")
render = _load("render")
probes_public = _load("probes_public")
probes_resources = _load("probes_resources")


class _Proc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


class _Runner:
    """假的 `subprocess.run`, 按子串脚本化返回, 并记下每次真实参数。

    写成类而不是「函数 + 挂 `.calls`」: 后者要么加 `type: ignore`, 要么让 `ty` 报
    unresolved-attribute —— 而判据里 `runner.calls` 是主力断言, 不该靠忽略标记撑着。
    """

    def __init__(self, script: dict[str, _Proc], *, default: _Proc | None = None) -> None:
        self.script = script
        self.default = default or _Proc()
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs) -> _Proc:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, proc in self.script.items():
            if key in joined:
                return proc
        return self.default


def _runner(script: dict[str, _Proc], *, default: _Proc | None = None) -> _Runner:
    return _Runner(script, default=default)


#: `ss -tn state established` 的首行表头。它**不含** `:443`, 所以不该被数进来 —— 但如果
#: 哪天改成按行数计, 这一行就会让每个容器凭空多一条连接。留着它当靶子。
_SS_HEADER = "Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
#: 一条真实形状的连接行(2026-09-21 从 gateway netns 里抄的对端 IP)。
_SS_443 = "0      0         172.21.0.2:40404   61.170.82.99:443 \n"

#: 2026-09-21 从生产 B 机 `dmesg -T` 原样抄下的三条 OOM 行, **一个字符没删**。
#:
#: 长度是判据的一部分: 真实行 417 字符, 那串 64 位 cgroup id 出现三次。摘要写法(`...,`)
#: 只有 90 字符, 于是「截尾部 100 字符」时时间戳仍在窗内 —— 一条本该抓到「value 丢了
#: 时间戳」的变异会因此假绿。用真实长度让截断真的发生。
_REAL_OOM_LINES = (
    "[Sat Sep 12 08:44:22 2026] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-"
    "9c36374e8d75e9428a6006bb7a5dc4cb698e0f87cf7d4a1794d411d381ab1cc0.scope,mems_allowed=0,oom_memcg=/system.slice/"
    "docker-9c36374e8d75e9428a6006bb7a5dc4cb698e0f87cf7d4a1794d411d381ab1cc0.scope,task_memcg=/system.slice/docker-"
    "9c36374e8d75e9428a6006bb7a5dc4cb698e0f87cf7d4a1794d411d381ab1cc0.scope,task=psi-agent,pid=2439793,uid=0\n"
    "[Sat Sep 12 17:38:00 2026] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,"
    "mems_allowed=0,global_oom,task_memcg=/system.slice/docker-"
    "9c36374e8d75e9428a6006bb7a5dc4cb698e0f87cf7d4a1794d411d381ab1cc0.scope,task=psi-agent,pid=3020917,uid=0\n"
    "[Sat Sep 12 17:38:00 2026] Out of memory: Killed process 3020917 (psi-agent) total-vm:5112132kB, "
    "anon-rss:3498384kB, file-rss:8kB, shmem-rss:8kB, UID:0 pgtables:7564kB oom_score_adj:0\n"
)


# ───────────────── 硬要求: --resolve 不是 -H Host ─────────────────


def test_vhost_probe_uses_resolve_not_host_header():
    """**变异复核靶点 2 就在这。** 命令行必须有 `--resolve`, 且不能出现 `Host:` 头。

    为什么断言的是命令行而不是结果: 两种写法在**跑起来之后长得一样** —— `-H Host` 打裸
    IP 时 TLS 层 alert, curl 回 `HTTP=000`; 而真的 502 那次也可能回 000。两者不可区分,
    所以这个缺陷只在命令行上看得见, 判据也只能落在那一层。

    实测来源: 2026-09 用 `-H Host` 探境内机器, 得 `HTTP=000`, 据此报告「服务挂了」——
    而服务一直好着, 挂的是我的探法。
    """
    args = probes_public._curl_args("https://x.invalid/", resolve="x.invalid:443:203.0.113.1")
    assert "--resolve" in args
    assert args[args.index("--resolve") + 1] == "x.invalid:443:203.0.113.1"
    joined = " ".join(args)
    assert "Host:" not in joined, "不能用 -H Host 打裸 IP —— SNI 仍是 IP, 000 与真 502 不可区分"
    assert "-H" not in args and "--header" not in args


def test_vhost_probe_actually_passes_resolve_to_subprocess():
    """上一条只看拼参数的函数; 这一条确认**真调进 subprocess 的那次**也带着它。

    分两条的理由: `_curl_args` 对了而 `probe_vhost` 自己另拼一份命令行, 是一个 git 不会
    报冲突、测试也不会红的漂移方式。判据必须落在它声称的那一层。
    """
    runner = _runner({"curl": _Proc(0, "HTTPCODE:200")})
    probes_public.probe_vhost("x.invalid", 200, "203.0.113.1", runner=runner)
    assert runner.calls, "一次 curl 都没发出去"
    joined = " ".join(runner.calls[0])
    assert "--resolve x.invalid:443:203.0.113.1" in joined
    assert "Host:" not in joined


def test_vhost_502_is_anomaly_and_000_is_distinguished():
    """502 与 000 都是 BAD, 但 value 里要分得开 —— 排查方向完全不同。"""
    r502 = probes_public.probe_vhost(
        "x.invalid", 200, "203.0.113.1", runner=_runner({"curl": _Proc(0, "HTTPCODE:502")})
    )
    assert r502.status == findings.BAD
    assert r502.value == "502"

    r000 = probes_public.probe_vhost(
        "x.invalid", 200, "203.0.113.1", runner=_runner({"curl": _Proc(0, "HTTPCODE:000")})
    )
    assert r000.status == findings.BAD
    assert "连不上" in r000.value
    assert "--resolve" in r000.semantics, "000 必须声明已排除 SNI 那种假报, 否则读者无法判断这个 000 可不可信"


def test_curl_missing_is_unknown_not_bad():
    """阴性: curl 跑不起来 → UNKNOWN, **不是** BAD。

    报成 BAD 会让人去查生产服务, 而问题在监控机上 —— 方向完全反了。
    """

    def broken(args, **kwargs):
        raise OSError("curl: not found")

    result = probes_public.probe_vhost("x.invalid", 200, "203.0.113.1", runner=broken)
    assert result.status == findings.UNKNOWN
    assert result.reason, "UNKNOWN 必须带「为什么没量到」"


def test_missing_public_ip_is_unknown_not_dns_fallback():
    """兜底链判据: 没配 `public_ip` → UNKNOWN, **不退化成走系统 DNS**。

    用真名测这条会全绿(DNS 真能解析), 所以用一个仓库/环境里都不存在的保留名。走 DNS 的
    危害: 境内 A / 境外 B 两台跑过同一套栈, DNS 可能把探针导到另一台, 那种「绿」证明不了
    这台机器活着。
    """
    calls: list[list[str]] = []

    def spy(args, **kwargs):
        calls.append(list(args))
        return _Proc(0, "HTTPCODE:200")

    result = probes_public.probe_vhost("never-registered-host.invalid", 200, "", runner=spy)
    assert result.status == findings.UNKNOWN
    assert "public_ip" in result.reason
    assert calls == [], "没有 IP 时一次请求都不该发 —— 发了就是在走 DNS"


def test_oauth_callback_404_is_anomaly():
    """回调路径回 404 = 白名单这一层没放行, 是异常。

    它与 vhost 根路径分两条探针的理由: 定时任务与发飞书卡片走**出站**, 回调走**入站**。
    出站全绿不证明入站活着 —— 2026-09 那次 502 持续 2h45m 就是这么漏检的。
    """
    r404 = probes_public.probe_oauth_callback(
        "x.invalid", "203.0.113.1", runner=_runner({"curl": _Proc(0, "HTTPCODE:404")})
    )
    assert r404.status == findings.BAD
    # 302 / 4xx(非 404) 都说明这一跳活着 —— 不带 code 参数打回调, 后端本来就会拒。
    r302 = probes_public.probe_oauth_callback(
        "x.invalid", "203.0.113.1", runner=_runner({"curl": _Proc(0, "HTTPCODE:302")})
    )
    assert r302.status == findings.OK
    assert "入站" in r302.semantics


def test_oauth_proxy_probe_really_requests_and_expects_404():
    """8090 探针必须**真发请求**, 且不看 `LISTEN` / `docker ps`。

    netns 死掉后 `docker-proxy` 仍在宿主监听 8090(`ss` 显示 LISTEN), 容器状态仍是 Up ——
    两个并列的假阴性。只有真打一次算数。

    期望 404: oauth-proxy 白名单外一律 404, 而 `/` 不在白名单 —— **404 恰好证明代理活着
    且白名单在生效**。
    """
    runner = _runner({"curl": _Proc(0, "HTTPCODE:404")})
    result = probes_public.probe_oauth_proxy_port(8090, runner=runner)
    assert result.status == findings.OK
    assert result.value == "404"
    joined = " ".join(runner.calls[0])
    assert "curl" in joined and "8090" in joined
    for forbidden in ("ss ", "netstat", "docker ps", "inspect"):
        assert forbidden not in joined, f"不能用 {forbidden} 当判据 —— 它与 Up 是两个并列的假阴性"
    assert "oauth-proxy" in result.semantics and "8080" in result.semantics, (
        "必须写明 8090 是 oauth-proxy 而 gateway 在 8080 —— 混了会得出反向的错误根因"
    )


def test_oauth_proxy_000_is_anomaly_mentioning_netns():
    """000 = 连不上, 要在 value 里点出 netns —— 那是这条链路最常见的死法。"""
    result = probes_public.probe_oauth_proxy_port(8090, runner=_runner({"curl": _Proc(0, "HTTPCODE:000")}))
    assert result.status == findings.BAD
    assert "netns" in result.value


def test_wss_zero_is_anomaly_but_semantics_says_outbound_still_works():
    """wss=0 是异常, 但**语义必须写明「只证明收不到, 不证明发不出」**。

    实测: 只起 gateway、wss=0 的情况下, schedule runner 照样真发出了飞书卡片 —— 它走的是
    出站 HTTP, 不需要这条长连接。不写这句话, 读者会从「机器人整个死了」这个错方向开始查。
    """
    runner = _runner({"inspect": _Proc(0, "3088458\n"), "nsenter": _Proc(0, _SS_HEADER)})
    result = probes_public.probe_feishu_wss(("psi-agent-gateway",), runner=runner)
    assert result.status == findings.BAD
    assert "收不到" in result.semantics and "发" in result.semantics


def test_wss_counts_from_host_netns_not_docker_exec():
    """**数法必须是宿主 nsenter, 不是 `docker exec`。**

    2026-09-21 实测容器里没有 `ss`。原先那版走 `docker exec ... "ss ... 2>/dev/null |
    grep -c ':443'"`, 于是「命令不存在」的 stderr 被吞掉, 空输出喂给 `grep -c` 得到一个
    **合法的 `0`**, 探针报「0 条连接」并升级为异常 —— 而同一时刻有 9 个 open_id 正在跑。

    这个缺陷只在命令行上看得见: 「恒定报 0」与「真的收不到消息」打出的是同一句话。所以
    判据落在命令行, 并且钉住 `2>/dev/null` 不许回来 —— 它是把观测缺口变成数据的那一步。
    """
    runner = _runner({"inspect": _Proc(0, "3088458\n"), "nsenter": _Proc(0, _SS_HEADER + _SS_443 * 14)})
    result = probes_public.probe_feishu_wss(("psi-agent-gateway",), runner=runner)
    assert result.status == findings.OK
    assert result.value == "psi-agent-gateway=14", "表头不该被数进来"
    joined = " ".join(" ".join(c) for c in runner.calls)
    assert "nsenter" in joined
    assert "docker exec" not in joined
    assert "2>/dev/null" not in joined, "重定向 stderr 正是「没这个命令」退化成「0 条连接」的成因"
    assert "grep" not in joined, "计数要在 Python 这侧做 —— shell 里 grep -c 对空输入给 0"


def test_wss_tool_missing_is_unknown_not_zero():
    """阴性: 宿主缺 `ss` / 非 root → UNKNOWN, **不是 0**。

    这正是「量 live render 得 0 行」那类坑: 工具不在与连接数为 0 在输出里长得一样, 混成
    一栏之后观测缺口就伪装成了数据。
    """
    result = probes_public.probe_feishu_wss(
        ("psi-agent-gateway",),
        runner=_runner({"inspect": _Proc(0, "3088458\n"), "nsenter": _Proc(127, "", "nsenter: ss: not found")}),
    )
    assert result.status == findings.UNKNOWN
    assert result.value == "未测到"
    assert "ss" in result.reason


def test_wss_container_not_running_is_unknown_not_zero():
    """阴性: 容器没在跑(`.State.Pid` 为 0)→ UNKNOWN。

    「容器死了」与「容器活着但没连接」是两件事。都落成 0 条连接, 报告就会把一个死掉的
    容器描述成一个连不上飞书的容器, 把人引去查网络。
    """
    result = probes_public.probe_feishu_wss(("psi-agent-gateway",), runner=_runner({"inspect": _Proc(0, "0\n")}))
    assert result.status == findings.UNKNOWN
    assert "没在跑" in result.reason


def test_wss_only_counts_containers_that_build_the_connection():
    """**私有容器不该被算进来 —— 基线本身错了, 不是读数不准。**

    实测: luolin/chengxx 跑 `psi-agent run` + `channel_socket`, 它们的 `config.yml` 自己
    写着「消息由主容器转发进来」, netns 里 established 总数是 0(一条都没有)。设计上就不连
    飞书。拿三容器清单去数, `>= 1 每容器` 永远不可能满足 —— 两个健康容器天天被报异常。

    所以默认建连清单只有 gateway 一个, 且**不继承** `containers`。
    """
    assert config.DEFAULT_WSS_CONTAINERS == ("psi-agent-gateway",)
    assert len(config.DEFAULT_CONTAINERS) == 3, "三容器清单仍用于内存等按容器采集的项"

    # 只配 `containers=` 不该把私有容器带进建连清单。
    cfg = config.Config.load(env={"HAITUN_MONITOR_CONTAINERS": "a,b,c", "HAITUN_MONITOR_CONF": "/nonexistent"})
    assert cfg.containers == ("a", "b", "c")
    assert cfg.wss_containers == ("psi-agent-gateway",)

    # 显式配空 → 整项 UNKNOWN, 而不是「没有容器所以全都正常」。
    empty = config.Config.load(env={"HAITUN_MONITOR_WSS_CONTAINERS": "", "HAITUN_MONITOR_CONF": "/nonexistent"})
    assert empty.wss_containers == ()
    blind = probes_public.probe_feishu_wss(empty.wss_containers, runner=_runner({}))
    assert blind.status == findings.UNKNOWN


# ───────────────── 第 2 类 ─────────────────


def test_oom_probe_does_not_rely_on_docker_inspect():
    """**OOM 只看 dmesg。** 命令行里不许出现 `inspect`。

    实测: gateway memcg OOM 撞 3g 时, `docker inspect` 的 `.State.OOMKilled` 是 **false**
    (被杀的是子进程, 不是 PID 1), 容器状态一直 Up, 应用日志报错像上游问题。三处判据全是
    假阴性, 只有 dmesg 里那行 `oom-kill:` 靠得住 —— 所以这类故障当时零告警。
    """
    runner = _runner({"dmesg": _Proc(0, "[12345.6] oom-kill:constraint=CONSTRAINT_MEMCG,...,task=python3\n")})
    result = probes_resources.probe_oom_kills(runner=runner)
    assert result.status == findings.BAD
    assert result.value.startswith("1")
    joined = " ".join(" ".join(c) for c in runner.calls)
    assert "dmesg" in joined
    assert "inspect" not in joined, "OOMKilled 在子进程被杀时是 false, 不能拿它当判据"
    assert "OOMKilled" not in joined


def test_oom_stale_entries_do_not_report_as_today_anomaly():
    """**窗外的陈旧 OOM 不许报成今天的异常。**

    2026-09-21 实测: 缓冲区里 3 条 OOM 的时间戳全是 9-12 —— 9 天前。宿主 `up 41 days`,
    环形缓冲没滚掉, 所以不设窗的话这 3 条会天天进「异常」栏, 直到缓冲自己滚掉。一条恒定
    为真的告警等于没有告警: 连续几天之后读者会无视这一项, 而那时真的新增一条也看不出来。

    窗外的条目仍要**打出来**(是「这台机器有 OOM 史」的线索), 但状态是 OK。
    """
    # 2026-09-21 17:20 本地时间, 与实测那次对齐。
    now = time.mktime(time.strptime("Mon Sep 21 17:20:00 2026", "%a %b %d %H:%M:%S %Y"))
    result = probes_resources.probe_oom_kills(runner=_runner({"dmesg": _Proc(0, _REAL_OOM_LINES)}), now=now)
    assert result.status == findings.OK, "9 天前的 OOM 不是今天的异常"
    assert result.value.startswith("0")
    assert "窗外" in result.value and "3" in result.value, "窗外条目要报出来, 不能静默丢"
    assert "Sep 12" in result.value, "必须带时间戳 —— 只写「3 次」读者必然以为是今天"


def test_oom_within_window_is_anomaly_and_carries_timestamp():
    """正向: 窗内的 OOM → BAD, 且 value 里带时间戳。

    跟上一条配对。只测阴性(陈旧的不报)会让「窗口过滤」退化成「永远不报」—— 那是把一个
    假阳性换成了一个假阴性, 更糟。
    """
    now = time.mktime(time.strptime("Mon Sep 21 17:20:00 2026", "%a %b %d %H:%M:%S %Y"))
    mixed = (
        "[Sat Sep 12 08:44:22 2026] oom-kill:constraint=CONSTRAINT_MEMCG,...,task=psi-agent\n"
        "[Mon Sep 21 09:05:00 2026] oom-kill:constraint=CONSTRAINT_MEMCG,...,task=psi-agent,pid=9\n"
    )
    result = probes_resources.probe_oom_kills(runner=_runner({"dmesg": _Proc(0, mixed)}), now=now)
    assert result.status == findings.BAD
    assert result.value.startswith("1"), "只该计窗内那一条"
    assert "Sep 21" in result.value
    assert "窗外另有 1 条" in result.value


def test_oom_unparseable_timestamp_counts_as_recent():
    """时间戳解析不出时按**窗内**算 —— 宁可多报一次, 不能静默丢掉一条真的 OOM。

    这是刻意选的偏向。`dmesg -T` 的格式随发行版有差异, 而「格式没对上」不该让告警消失:
    一个多报的 OOM 会被人看一眼排除掉, 一个漏报的不会。
    """
    result = probes_resources.probe_oom_kills(
        runner=_runner({"dmesg": _Proc(0, "[12345.6] oom-kill:constraint=CONSTRAINT_MEMCG,...,task=python3\n")})
    )
    assert result.status == findings.BAD
    assert result.value.startswith("1")


def test_oom_probe_zero_hits_is_ok_but_unreadable_dmesg_is_unknown():
    """dmesg 读到了但没有 OOM 行 → OK(值为 0); dmesg 读不到 → UNKNOWN。两者不混。"""
    clean = probes_resources.probe_oom_kills(runner=_runner({"dmesg": _Proc(0, "[0.0] Linux version 5.10\n")}))
    assert clean.status == findings.OK
    assert clean.value == "0"

    blind = probes_resources.probe_oom_kills(
        runner=_runner({"dmesg": _Proc(1, "", "dmesg: read kernel buffer failed: Operation not permitted")})
    )
    assert blind.status == findings.UNKNOWN
    assert "dmesg" in blind.reason


def test_swappiness_declared_zero_is_anomaly_even_when_runtime_is_fine():
    """**运行时 10 而声明 0 → BAD。** 这条是整个第 2 类里最反直觉的一个。

    实测: 阿里云镜像在 `/etc/sysctl.d/` 里显式声明 `vm.swappiness=0`。有人 `sysctl -w`
    改成 10, 运行时看着修好了 —— 但重启必回滚, OOM 随之回来。只看运行时值的判据会说
    「已修复」。
    """
    result = probes_resources.probe_swappiness(
        runner=_runner({"sysctl": _Proc(0, "10\n")}),
        read_text=lambda p: "vm.swappiness = 0\n" if "sysctl" in p else "",
    )
    assert result.status == findings.BAD
    assert "运行时 10" in result.value and "声明 0" in result.value


def test_swappiness_takes_last_declaration_in_load_order():
    """多处声明时取**最后加载**的那个 —— `sysctl --system` 按文件名排序, 后者覆盖前者。

    只 grep「有人声明过」是不够的: 一个早加载的 `=0` 会被后加载的 `=10` 覆盖, 报成 BAD
    就是假报。这里让所有文件都读到内容, 最后一个是 10 且运行时也是 10 → OK。
    """
    result = probes_resources.probe_swappiness(
        runner=_runner({"sysctl": _Proc(0, "10\n")}),
        read_text=lambda p: "vm.swappiness=0\nvm.swappiness=10\n",
    )
    assert result.status == findings.OK, f"取最后一个声明应为 10, 实得 {result.value}"


def test_container_memory_missing_row_is_unknown_not_ok():
    """阴性: `docker stats` 里没有某容器那一行 → UNKNOWN, 不是 OK。

    「容器没了」比「内存高」更糟, 而 `docker stats` 对不存在的名字只是不输出那一行、不报错
    —— 又一个静默的健康假象。容器名用仓库里不存在的名字构造。
    """
    out = "psi-agent-gateway\t500MiB / 3GiB\t16.3%\n"
    result = probes_resources.probe_container_memory(
        ("psi-agent-gateway", "psi-agent-never-existed"), runner=_runner({"docker stats": _Proc(0, out)})
    )
    by_name = {f.name: f for f in result}
    assert by_name["psi-agent-gateway 内存"].status == findings.OK
    missing = by_name["psi-agent-never-existed 内存"]
    assert missing.status == findings.UNKNOWN
    assert "没有这一行" in missing.reason


def test_memcg_peak_unreadable_is_unknown_not_substituted_by_instant_value():
    """阴性: 峰值文件都读不到 → UNKNOWN, **不拿瞬时值顶替**。

    用瞬时值冒充峰值会让「撞过顶」永远看不见 —— 正是 502 那次的失败模式。两个路径都用
    真路径名探测, 但 runner 让它们都失败(模拟内核版本不支持 `memory.peak`)。
    """
    result = probes_resources._probe_memcg_peak(
        ("psi-agent-gateway",), runner=_runner({"cat": _Proc(1, "", "No such file")})
    )
    assert result.status == findings.UNKNOWN
    assert "peak" in result.reason


def test_disk_and_inode_are_separate_findings():
    """磁盘与 inode 分两条。inode 耗尽时空间可能还很空, 而报错同样是
    `No space left on device` —— 只看空间会得出「空间够啊」的反向结论。
    """
    result = probes_resources.probe_disk(
        runner=_runner(
            {
                "df -iP": _Proc(0, "Filesystem Inodes IUsed IFree IUse%\n/dev/vda1 100 95 5 95%\n"),
                "df -P": _Proc(0, "Filesystem 1K-blocks Used Avail Use%\n/dev/vda1 100 10 90 10%\n"),
            }
        )
    )
    by_name = {f.name: f for f in result}
    assert by_name["磁盘使用率 /"].status == findings.OK
    assert by_name["inode 使用率 /"].status == findings.BAD, "inode 95% 必须独立告警"


# ───────────────── 渲染的三条硬要求 ─────────────────


def test_anomalies_come_first_and_healthy_is_one_line():
    """异常排最前, 正常压一行。日报最大的失败模式不是数字错, 是没人读。"""
    report = findings.Report(tier="日报", timestamp="2026-09-20 08:57:00 CST(UTC+08:00)")
    for i in range(5):
        report.add(findings.ok(f"正常项{i}", value="200", baseline="200", direction="应为 200"))
    report.add(findings.bad("坏掉的 vhost", value="502", baseline="200", direction="应为 200"))
    text = render.render(report)

    assert text.startswith("【异常】")
    assert text.index("坏掉的 vhost") < text.index("正常项0")
    # 正常项压成一行 —— 5 个名字在同一行里。
    healthy_line = [line for line in text.splitlines() if line.startswith("□ 正常")]
    assert len(healthy_line) == 1
    assert all(f"正常项{i}" in healthy_line[0] for i in range(5))


def test_unknown_is_its_own_section_not_merged_into_ok_or_bad():
    """「未测到」独立一栏, 且带原因。

    并进 OK 是**观测缺口伪装成健康**(量 live render 得 0 行那次); 并进 BAD 则让人去查
    服务而问题在探针。所以必须是第三栏。
    """
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.ok("好的", value="200", baseline="200", direction="应为 200"))
    report.add(findings.unknown("瞎了的", reason="dmesg 权限不足", baseline="0", direction="> 0 = 有进程被杀"))
    text = render.render(report)

    assert "【有观测缺口】" in text
    assert "未测到 1 项(观测缺口, 不等于零)" in text
    assert "dmesg 权限不足" in text
    # UNKNOWN 不能出现在「正常」那一行里。
    healthy_line = next(line for line in text.splitlines() if line.startswith("□ 正常"))
    assert "瞎了的" not in healthy_line


def test_every_finding_carries_baseline_and_direction():
    """每个指标带基线与方向。`232 of 232` 单看无异常, 只有知道应是 66 才叫异常。"""
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.bad("工具收窄", value="232 of 232", baseline="66 of 232", direction="N==M 表示收窄未生效"))
    text = render.render(report)
    assert "基线 66 of 232" in text
    assert "N==M 表示收窄未生效" in text


def test_header_carries_three_counts_so_scale_is_visible_without_reading_on():
    """抬头下面给出三档各自的数量。

    2026-09-21 第一份真实日报的抬头只有 `【有观测缺口】`, 而「1 项缺口」与「13 项缺口」是两件
    完全不同的事 —— 要把整篇读完才知道是哪种。这一行的作用是让人**先决定要不要往下读**。
    """
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.ok("好的", value="200", baseline="200", direction="应为 200"))
    report.add(findings.ok("也好的", value="200", baseline="200", direction="应为 200"))
    report.add(findings.unknown("瞎了的", reason="来源读不到", baseline="0", direction="> 0 = 有"))
    report.add(findings.bad("坏的", value="502", baseline="200", direction="应为 200"))
    text = render.render(report)

    summary = text.splitlines()[1]
    assert "异常 1" in summary and "未测到 1" in summary and "正常 2" in summary
    # 这一行必须紧贴抬头 —— 排到异常之后就失去了「先决定读不读」的作用。
    assert text.splitlines()[0].startswith("【异常】")


def test_empty_report_has_no_all_zero_summary_line():
    """一个指标都没采到时不出那行 `异常 0 · 未测到 0 · 正常 0`。

    阴性配对。三个零连读最像「什么问题都没有」, 而它的真实含义正相反 —— 采集整体没跑起来。
    那句话由专门的一行说, 不能被一行零数字抢在前面。
    """
    text = render.render(findings.Report(tier="日报", timestamp="T"))
    assert "异常 0 · 未测到 0" not in text
    assert "不是「一切正常」" in text


def test_anomaly_name_is_on_its_own_line_not_buried_in_parens():
    """异常条目的名称单独占行, 实测/基线/方向在下一行。

    原格式把四样东西塞进一行, 实测到的长度是 200 多字符, 名称和实测值淹在括号堆里。判据钉在
    「名称所在那行不含基线」上 —— 只断言存在会被原格式一并满足。
    """
    report = findings.Report(tier="日报", timestamp="T")
    report.add(
        findings.bad(
            "工具收窄",
            value="232 of 232",
            baseline="66 of 232",
            direction="N==M 表示收窄未生效",
            semantics="EXPOSED.txt 缺失时收窄整体不生效",
        )
    )
    text = render.render(report)
    lines = text.splitlines()

    name_line = next(line for line in lines if line.strip().endswith("工具收窄"))
    assert "基线" not in name_line, "名称那行不该再挤基线 —— 挤进去就回到原格式了"
    assert "232 of 232" not in name_line

    detail = next(line for line in lines if "232 of 232" in line)
    assert "基线 66 of 232" in detail, "实测与基线要在同一行才比得起来"
    assert "N==M 表示收窄未生效" in detail
    assert any("EXPOSED.txt" in line for line in lines), "异常的读数语义仍要展开"


def test_unknowns_sharing_a_reason_print_that_reason_once():
    """多项共用同一原因时, 原因只出现一次, 组内逐项给基线。

    实测第一份日报里同一句原因(某个来源读不到)波及好几个指标, 每条抄一遍之后整节被重复文本
    占满, 而真正不同的信息(瞎了哪几项)埋在中间。

    判据用 `count` 而不是 `in`: 断言「原因在文本里」对归组前后都成立, 量不出任何东西。
    """
    reason = "metrics 目录读不到: 三个来源全未落盘"
    report = findings.Report(tier="日报", timestamp="T")
    for name in ("首字延迟 p95", "请求体字节 p95", "每回合成本 p95"):
        report.add(findings.unknown(name, reason=reason, baseline="< 1", direction="越高越糟"))
    report.add(findings.unknown("OOM 次数", reason="dmesg 权限不足", baseline="0", direction="> 0 = 有进程被杀"))
    text = render.render(report)

    assert text.count(reason) == 1, "共用的原因只该出现一次"
    assert text.count("dmesg 权限不足") == 1, "不同原因各自成组, 不被合并掉"
    assert "未测到 4 项" in text, "归组后计数仍按指标数, 不按组数 —— 报 2 项会低估缺口"
    for name in ("首字延迟 p95", "请求体字节 p95", "每回合成本 p95", "OOM 次数"):
        assert name in text
    # 归组省掉的是重复原因, 不是每项的刻度。
    assert text.count("基线 < 1") == 3


# ───────────────── 进趋势表的数字 ─────────────────


def test_unknown_must_not_carry_a_number():
    """UNKNOWN 带 `num` → 构造期就抛。

    这是本节最重要的一条。表比文字更容易让人一眼相信: 把「未测到」当 0 写进趋势表, 看表的人
    看到的是一列数字里的一个 0, 那与一个真实的、很低的读数**不可区分**, 折线图还会把它画成
    一次暴跌。文字版至少还有「未测到」三个字, 一格数字没有这个形态。表里「没量到」的正确形态
    是**空格子**。
    """
    with pytest.raises(ValueError, match="不得带 num"):
        findings.Finding(
            name="x", status=findings.UNKNOWN, value="未测到", baseline="0", direction="-", reason="读不到", num=0.0
        )
    # 报错顺序也是判据: 同时缺 num_warn 时**仍要报这一条**。先报「缺告警线」会把调用方引向
    # 「补个 num_warn」, 补完就静默通过, 缺口进了表 —— 报错顺序决定修法方向。
    with pytest.raises(ValueError, match="不得带 num"):
        findings.Finding(
            name="x",
            status=findings.UNKNOWN,
            value="未测到",
            baseline="0",
            direction="-",
            reason="读不到",
            num=0.0,
            num_warn=None,
        )


def test_unknown_must_not_carry_extra_nums_either():
    """UNKNOWN 带 `extra_nums` → 也要抛。

    附属数不受 `num` 那条管(`num` 可以是 None 而 `extra_nums` 有货), 而
    `{"p50": 0.0}` 进表和 `num=0.0` 进表是同一个坏结果 —— 少拦这一条, 「请求体 p50」那一列
    会在没量到的日子里出现 0.0。
    """
    with pytest.raises(ValueError, match="不得带 extra_nums"):
        findings.Finding(
            name="x",
            status=findings.UNKNOWN,
            value="未测到",
            baseline="0",
            direction="-",
            reason="读不到",
            extra_nums={"p50": 0.0},
        )


def test_number_without_a_warn_line_is_rejected():
    """填了 `num` 不填 `num_warn` → 构造期就抛。

    与 `baseline` 必填同一个道理: 一格 41 单看无异常, 只有和告警线比才知道 41 是宽裕还是快
    撞顶。字段描述里少了那条线, 这一列就没有刻度。
    """
    with pytest.raises(ValueError, match="必须填 num_warn"):
        findings.ok("磁盘", value="41%", baseline="< 85%", direction="越高越糟", num=41.0)


def test_extra_nums_need_no_warn_line():
    """附属数**不要求**告警线 —— 这是刻意的, 不是漏了。

    p50 没有自己的告警线(这一对指标的基线写在 p95 上: "p95 < 600 KiB")。硬要求的话, 唯一
    的满足办法是给 p50 编一条线出来, 那是给表加一个假刻度 —— 比不进表坏。
    """
    f = findings.ok(
        "请求体字节数 p50/p95",
        value="p50 170 KiB / p95 500 KiB",
        baseline="p95 < 600 KiB",
        direction="越高越糟",
        num=500.0,
        num_warn=600.0,
        unit="KiB",
        extra_nums={"p50": 170.0},
    )
    assert f.extra_nums == {"p50": 170.0}


def test_unknown_without_reason_is_rejected_at_construction():
    """UNKNOWN 不给理由 → 构造期就抛。

    不写原因的「未测到」等于又一个「群里安静」: 知道没量到却不知道为什么, 没人会去修。
    在类型层拦住比留给 review 可靠。
    """
    with pytest.raises(ValueError, match="必须带 reason"):
        findings.Finding(name="x", status=findings.UNKNOWN, value="未测到", baseline="0", direction="-")


def test_empty_report_is_not_reported_as_healthy():
    """一个指标都没采到 → 必须说明这不是「一切正常」。

    否则第一次部署(配置还没齐)会发出一条看着全绿的日报, 而实际什么都没测。
    """
    text = render.render(findings.Report(tier="日报", timestamp="T"))
    assert "不是「一切正常」" in text


def test_failure_notification_says_unknown_state_not_normal():
    """「生成失败」正文必须写明含义是「不知道生产是什么状态」。

    只说「失败了」会被当成脚本小毛病略过, 而它的真实含义是本轮无人在看。
    """
    text = render.render_failure("日报", "T", "Traceback...\nValueError: boom")
    assert "生成失败" in text
    assert "不是「生产正常」" in text
    assert "ValueError: boom" in text


def test_heartbeat_reports_cycle_count_not_just_alive():
    """心跳报**轮数**, 不只说「存活」。

    轮数少于预期是 cron 漏跑的唯一线索 —— 而一句「我还活着」在漏跑一半的情况下同样会发出。
    """
    text = render.render_heartbeat("T", cycles=12, window_hours=24)
    assert "12 轮" in text
    assert "288" in text, "要给出预期轮数做基线, 否则 12 这个数字不触发任何警觉"
    assert "不能当作「无异常」" in text


def test_timestamp_carries_timezone_and_offset():
    """时间戳带时区名与 UTC 偏移。

    「cron 按哪个时区跑」是排查定时任务没触发时的第一个问题, 而只写 `08:57` 回答不了它。
    容器内 `/etc/localtime` 恒为 UTC 这个假阴性已经让 cron 静默偏过 8 小时。
    """
    stamp = config.local_timestamp(0)
    assert "UTC" in stamp
    assert "+" in stamp or "-" in stamp


def test_config_reads_webhook_from_conf_file_not_repo():
    """webhook URL 从宿主配置文件读。仓库里只有 `.example`。"""
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "monitoring.conf"
        conf.write_text(
            "# 注释\nwebhook_url=https://example.invalid/hook\nvhosts=a.invalid:200,b.invalid\npublic_ip=203.0.113.9\n",
            encoding="utf-8",
        )
        cfg = config.Config.load({"HAITUN_MONITOR_CONF": str(conf)})
    assert cfg.webhook_url == "https://example.invalid/hook"
    assert cfg.vhosts == [("a.invalid", 200), ("b.invalid", 200)]
    assert cfg.public_ip == "203.0.113.9"


def test_missing_conf_file_does_not_crash():
    """兜底链判据: 配置文件**不存在**时不崩, 只是各项报「未测到」。

    路径用仓库里不存在的名字。崩掉的后果是第一次部署就触发「日报生成失败」—— 那条通知
    本该留给真的故障, 第一天就变成噪音的话它以后不会再被认真看。
    """
    cfg = config.Config.load({"HAITUN_MONITOR_CONF": "/nonexistent/never-created-haitun.conf"})
    assert cfg.webhook_url == ""
    assert cfg.vhosts == []


def test_no_webhook_secret_in_repo():
    """仓库里不许出现真的飞书 webhook URL。

    样例里那串必须是明显的占位符。凭据一旦进 git 就要走轮换流程, 而 `.example` 这种文件
    最容易被当成「反正是样例」填上真值。
    """
    for path in _MON.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "open.feishu.cn/open-apis/bot" in line:
                assert "REPLACE-ME" in line or "hook/" not in line.split("bot/")[-1], (
                    f"{path.name} 疑似含真 webhook: {line[:80]}"
                )
