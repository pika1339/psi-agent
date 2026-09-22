"""配置读取 —— webhook URL 是凭据, 绝不进仓库。

## 时区: 宿主时区, 不是容器时区

**这一整个目录的脚本跑在宿主 cron 上, 用的是宿主时区。** 容器里那套时区只由 compose 的
`TZ=Asia/Shanghai` 撑着, 容器内 `/etc/localtime` 恒为 UTC —— 谁去核 `/etc/localtime` 都会
得到「UTC」这个假阴性, 然后据此把 cron 的时刻改偏 8 小时。2026-09 已经因为这个漏检过一次
定时任务静默偏移。

所以:

* 本模块的 `local_timestamp()` 用宿主的 `time.localtime()`, 不做任何时区换算;
* 报告里同时打**宿主时区名与 UTC 偏移**, 让读者一眼看出 cron 是按哪个时区在跑;
* crontab 片段里的 `8:57` 是**宿主本地时间**, 装的时候先 `date` 一次核对宿主时区。

## webhook URL 的读取顺序

它是凭据(拿到就能往运维群发任意消息), 所以只从运行环境取, 仓库里只有 `.example`:

1. `HAITUN_MONITOR_WEBHOOK` 环境变量;
2. `HAITUN_MONITOR_CONF`(默认 `/etc/haitun/monitoring.conf`)里的 `webhook_url=`。

cron 的环境极简(没有登录 shell 的 profile), 所以生产实际走的是第 2 条 —— 第 1 条是本地
调试与测试用的。配置文件格式刻意只支持 `key=value`, 不用 ini/json: 少一个解析分支, 也少
一次「宿主上少装一个模块」的可能。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 宿主上的配置文件默认位置。用 `HAITUN_MONITOR_CONF` 覆盖。
DEFAULT_CONF_PATH = "/etc/haitun/monitoring.conf"

#: 生产三个容器名。gateway 是公网入口那份, 另两个是私有 workspace(一人一容器)。
#: 容器名不是 compose 的 service 名 —— service 是 `gateway`/`private-luolin`/
#: `private-chengxx`, 容器是 `psi-agent-*`。`docker stats` 收容器名, `docker compose`
#: 收 service 名, 传错不报错只是什么都不做。
DEFAULT_CONTAINERS = ("psi-agent-gateway", "psi-agent-luolin", "psi-agent-chengxx")

#: 会自己建飞书长连接的容器 —— **只有 gateway 一个**, 不是上面那三个。
#:
#: 私有容器跑的是 `psi-agent run` + `channel_socket`, 设计上压根不连飞书: 它们的
#: `config.yml` 自己写着「消息由主容器转发进来, 回复顺原路出去」, gateway 靠
#: `PSI_FEISHU_EXTERNAL_SESSIONS` 把某个 open_id 指到私有容器的 TCP 地址。所以私有容器
#: 的长连接数**本该是 0**, 2026-09-21 实测它们 netns 里 established 总数就是 0(不是只缺
#: 443, 是一条都没有), 而 gateway 那份是 14 条。
#:
#: 拿 DEFAULT_CONTAINERS 去数长连接会为两个健康容器天天报异常 —— 基线本身是错的, 而不是
#: 探针读数不准。用 `wss_containers=` 覆盖(逗号分隔); 留空则整项报 UNKNOWN 而不是 OK。
DEFAULT_WSS_CONTAINERS = ("psi-agent-gateway",)


def _parse_conf(text: str) -> dict[str, str]:
    """`key=value` 逐行解析。`#` 起头与空行跳过, value 里的 `=` 保留。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip().strip("'\"")
    return out


@dataclass
class Config:
    """一次运行需要的全部外部输入。

    字段都有默认值, 且默认值**不指向生产** —— 除了容器名与路径这类无害的。webhook 没配
    时 `webhook_url` 为空串, 由 `notify` 那层决定怎么响(见它的 docstring: 空 URL 走
    stdout, 不静默丢)。
    """

    webhook_url: str = ""
    #: 要探的 vhost: (域名, 期望状态码)。空列表意味着第 1 类整类报 UNKNOWN 而不是 OK。
    vhosts: list[tuple[str, int]] = field(default_factory=list)
    #: `curl --resolve` 要绑的 IP。见 probes_public 的 docstring: 必须用 --resolve,
    #: 不能用 -H Host。
    public_ip: str = ""
    containers: tuple[str, ...] = DEFAULT_CONTAINERS
    #: 只有这些容器该有飞书长连接。与 `containers` 刻意分开 —— 见 DEFAULT_WSS_CONTAINERS。
    wss_containers: tuple[str, ...] = DEFAULT_WSS_CONTAINERS
    #: OAuth 回调路径 —— 入站那条路。定时任务与发卡片走出站, 内部全绿不证明入站活着。
    oauth_callback_path: str = "/oauth/callback"
    #: oauth-proxy 在宿主上的监听端口。**它不是 gateway** —— gateway 在容器内听 8080,
    #: 不对外暴露。把 8090 当成 gateway 直连会得出反向的错误根因。
    oauth_proxy_port: int = 8090
    #: 单次 curl 的超时秒数。即时档 5-10 分钟一轮, 总耗时要远小于一个周期。
    timeout_seconds: int = 10
    #: 趋势表(多维表格)写入用的飞书应用凭据与表坐标。四个缺任何一个就不写表, 只发消息。
    #:
    #: **app_secret 是凭据, 只能来自 mode 600 的配置文件或环境变量, 不入库。** 这也是
    #: `monitoring.conf` 整个文件不进 git 的理由之一。
    #:
    #: 为什么这里用 app token 而消息用 webhook: 多维表格没有 webhook 写入方式, 不得不用。
    #: 风险(OAuth 那条路坏过)由「写表失败不影响发消息」缓解, 见 bitable.push。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    bitable_app_token: str = ""
    bitable_table_id: str = ""
    #: 表的可点链接, 只用于在异常消息末尾附一行「明细见表」。空则不附。
    bitable_url: str = ""

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        conf_path = env.get("HAITUN_MONITOR_CONF", DEFAULT_CONF_PATH)
        conf: dict[str, str] = {}
        try:
            conf = _parse_conf(Path(conf_path).read_text(encoding="utf-8"))
        except OSError:
            # 配置文件不存在不是致命错 —— 环境变量也能给全。真正缺什么由各探针自己
            # 报 UNKNOWN(带 reason), 而不是整份脚本崩掉: 崩掉会让第一次部署直接触发
            # 「日报生成失败」, 而那条通知本该留给真的故障。
            conf = {}

        def pick(name: str, default: str = "") -> str:
            return env.get(f"HAITUN_MONITOR_{name.upper()}") or conf.get(name) or default

        vhosts: list[tuple[str, int]] = []
        for item in (s.strip() for s in pick("vhosts").split(",")):
            if not item:
                continue
            host, _, code = item.partition(":")
            vhosts.append((host.strip(), int(code) if code.strip().isdigit() else 200))

        containers_raw = pick("containers")
        containers = (
            tuple(s.strip() for s in containers_raw.split(",") if s.strip()) if containers_raw else DEFAULT_CONTAINERS
        )

        # 显式配了 `wss_containers=` 就照配的来, 包括配成空(那时整项报 UNKNOWN)。没配则用
        # 默认的 gateway 一个 —— **不继承 `containers`**, 否则私有容器又会被算进去。
        wss_raw = pick("wss_containers")
        if "wss_containers" in conf or "HAITUN_MONITOR_WSS_CONTAINERS" in env:
            wss_containers = tuple(s.strip() for s in wss_raw.split(",") if s.strip())
        else:
            wss_containers = DEFAULT_WSS_CONTAINERS

        timeout_raw = pick("timeout_seconds", "10")
        port_raw = pick("oauth_proxy_port", "8090")
        return cls(
            webhook_url=pick("webhook", "") or pick("webhook_url", ""),
            vhosts=vhosts,
            public_ip=pick("public_ip"),
            containers=containers,
            wss_containers=wss_containers,
            oauth_callback_path=pick("oauth_callback_path", "/oauth/callback"),
            oauth_proxy_port=int(port_raw) if port_raw.isdigit() else 8090,
            timeout_seconds=int(timeout_raw) if timeout_raw.isdigit() else 10,
            feishu_app_id=pick("feishu_app_id"),
            feishu_app_secret=pick("feishu_app_secret"),
            bitable_app_token=pick("bitable_app_token"),
            bitable_table_id=pick("bitable_table_id"),
            bitable_url=pick("bitable_url"),
        )


def local_timestamp(when: float | None = None) -> str:
    """宿主本地时间 + 时区名 + UTC 偏移。

    偏移一起打出来的理由见模块 docstring: 「cron 按哪个时区跑」是排查定时任务没触发时
    的第一个问题, 而报告里只写 `08:57` 回答不了它。
    """
    t = time.localtime(when)
    offset_seconds = -(time.altzone if t.tm_isdst and time.daylight else time.timezone)
    sign = "+" if offset_seconds >= 0 else "-"
    offset = f"UTC{sign}{abs(offset_seconds) // 3600:02d}:{abs(offset_seconds) % 3600 // 60:02d}"
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', t)} {time.tzname[0]}({offset})"
