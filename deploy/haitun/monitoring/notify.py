"""出站 webhook 投递 —— 纯标准库, 不经 gateway、不经 psi-agent 任何进程。

## 为什么不用 agent 的 schedule, 也不用飞书 app token

故障史里最贵的一次是 gateway 502 持续 **29 小时无人知晓**。如果日报靠 gateway 自己的
schedule runner 发, 那么 gateway 挂掉时日报也挂掉 —— **恰在最需要告警的时刻静默**。所以
这一整条投递链必须在被监控对象之外: 宿主 cron 起一个 python, 直接 POST 到飞书自定义机器人
的 webhook URL。

不用飞书 app token 的理由同类: app token 走 OAuth, 而 OAuth 回调**正是坏过的那条路**。
自定义机器人的 webhook 是一次裸 HTTP POST, 不依赖本机任何服务。

## 为什么用 urllib 而不是 requests

宿主不装包。`urllib.request` 在任何 CPython 3 上都在。

## 重试策略: 有限重试后放弃, 但**必须留下痕迹**

网络抖动值得重试(飞书 open API 偶发 5xx), 但重试不能无限 —— 即时档 5 分钟一轮, 一轮卡住
会和下一轮叠起来。所以 3 次、指数退避、上限几秒。

**放弃时写 stderr 并返回 False**, 不抛异常: 抛出去会让调用方的 `finally` 分支也发不出通知,
而 stderr 在 cron 下会进 MAILTO 或 cron 日志 —— 那是「webhook 本身不可达」时唯一还活着的
通路。判据 `test_unreachable_webhook_gives_up_and_reports` 钉住这个行为。
"""

from __future__ import annotations

# ruff: noqa: T201  这是命令行脚本, stdout/stderr 就是它的输出通道 —— 且 stderr 是
# 「webhook 不可达」时唯一还活着的通路, 见下方 post_text 的注释。
import json
import sys
import time
import urllib.error
import urllib.request

#: 重试次数(含首次)。见模块 docstring: 有限, 因为即时档周期是 5 分钟。
MAX_ATTEMPTS = 3
#: 每次尝试的超时秒数。
ATTEMPT_TIMEOUT = 10
#: 退避基数秒。第 n 次失败后等 BACKOFF_BASE * n。
BACKOFF_BASE = 2


def post_text(webhook_url: str, text: str, *, sleep=time.sleep, opener=None) -> bool:
    """往飞书自定义机器人发一条纯文本消息。成功返回 True。

    `sleep` 与 `opener` 是为判据留的注入点 —— 判据要打**真的 HTTP server**(见
    `tests/deploy/test_monitoring_delivery.py`), 注入的只是「别真等 2 秒」和「换掉全局
    opener」, 不是把 HTTP 本身 mock 掉。说测公网就真发请求, 否则判据测的是字符串拼接。

    webhook URL 为空时**不静默丢**: 打到 stdout 并返回 False。空 URL 最可能的成因是宿主
    配置文件没投放, 而那种情况下「什么都没发生」与「一切正常」不可区分。
    """
    if not webhook_url:
        print("[monitor] 未配置 webhook URL, 消息只打到 stdout:\n" + text, file=sys.stdout)
        return False

    # 纯文本是这里**唯一**的载体。卡片那条路删了(曾有 post_card / post_report): 图上一根
    # 共用告警线在各项告警线不同时会画出假越线 —— 实测磁盘 81% 的柱子越过「告警 80%」那根
    # 线, 而它自己的线是 85%。读图的人先看柱子与线的相对位置, 再看标题, 所以一行说明修不了。
    #
    # 趋势现在由多维表格承担(`bitable.py`), 消息只负责「今天有事」。顺带去掉的失败面:
    # 卡片 schema 变更、chart 组件要求客户端 7.1+、整卡 30KB 上限 —— 这三种失败的表现都是
    # 群里安静, 与一切正常不可区分。
    return _post(webhook_url, {"msg_type": "text", "content": {"text": text}}, text, sleep=sleep, opener=opener)


def _post(webhook_url: str, body: dict, echo: str, *, sleep, opener) -> bool:
    """实际的 POST + 重试。`echo` 是放弃时打到 stderr 的内容。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    text = echo
    send = opener.open if opener is not None else urllib.request.urlopen

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # URL 来自宿主配置文件(凭据, 不进仓库), 不是用户输入。
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with send(request, timeout=ATTEMPT_TIMEOUT) as resp:
                # 变量名不叫 body —— 那是入参(请求体)的名字, 重名会让第二次重试把响应
                # 内容当请求体发出去。这个坑在加卡片支持时就差一行踩到。
                resp_body = resp.read(512).decode("utf-8", "replace")
                if 200 <= resp.status < 300:
                    # 飞书 webhook 对 schema 错误也回 200, 错误在 body 里的 code 字段。
                    # 只看 HTTP 状态码会把「卡片被拒」判成发送成功, 于是回退永不触发。
                    if '"code"' in resp_body and '"code":0' not in resp_body.replace(" ", ""):
                        last_error = f"HTTP 200 但 body 报错: {resp_body}"
                    else:
                        return True
                else:
                    last_error = f"HTTP {resp.status}: {resp_body}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # ValueError 收的是 URL 拼错(urllib 对 "not-a-url" 抛的就是它) —— 配错和
            # 网络不通在这里同等对待: 都要退避重试, 都要在放弃时留痕。
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_ATTEMPTS:
            sleep(BACKOFF_BASE * attempt)

    # 放弃。stderr 是「webhook 不可达」时唯一还活着的通路, 见模块 docstring。
    print(
        f"[monitor] webhook 投递失败, 已重试 {MAX_ATTEMPTS} 次后放弃: {last_error}\n未发出的内容:\n{text}",
        file=sys.stderr,
    )
    return False
