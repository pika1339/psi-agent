"""出站消息投递 —— 纯标准库, 不经 gateway、不经 psi-agent 任何进程。

## 为什么不用 agent 自己的 schedule

故障史里最贵的一次是 gateway 502 持续 **29 小时无人知晓**。如果日报靠 gateway 自己的
schedule runner 发, 那么 gateway 挂掉时日报也挂掉 —— **恰在最需要告警的时刻静默**。所以
这一整条投递链必须在被监控对象之外: 宿主 cron 起一个 python, 直接 POST 到飞书开放平台。

## 两条通路: 应用机器人(chat_id) 与自定义机器人(webhook)

配了 `feishu_chat_id` 就走应用机器人 `im/v1/messages`, 否则走 `webhook_url`。

**更正一处旧判断。** 这份 docstring 原先写「不用 app token, 因为 app token 走 OAuth,
而 OAuth 回调正是坏过的那条路」—— 这是错的。`tenant_access_token` 是一次**出站** POST,
带 app_id/app_secret 直接换 token, 与本机的 OAuth 回调(入站, 经 oauth-proxy)没有关系。
2026-09-22 在生产上实测: 取 token 加发消息全程只有出站 HTTPS, 不经 gateway 也不经
oauth-proxy。所以两条通路在「在被监控对象之外」这一点上等价。

选应用机器人作首选的实际理由: 应用凭据本来就为多维表格配好了, 用它就不必再建一个自定义
机器人、再多管一份凭据 —— 少一份凭据就少一个会过期而没人发现的东西。

代价是多一次网络往返(先取 token 再发消息), 且 token 这一步失败时要**说清是哪一步失败**:
「取 token 失败」指向凭据, 「发消息失败」指向群权限或 chat_id, 两者该查的地方不同。

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
#: 飞书开放平台根。与 bitable.BASE 同值但**不从那里导入** —— 那会让「发消息」在导入期
#: 就依赖写表模块, 而写表失败不该影响发消息(见 bitable.push 的同一条理由)。
BASE = "https://open.feishu.cn/open-apis"


def post_chat(
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    *,
    sleep=time.sleep,
    opener=None,
) -> bool:
    """走**应用机器人**往群里发纯文本。成功返回 True。

    两步: 先 `tenant_access_token`, 再 `im/v1/messages?receive_id_type=chat_id`。

    失败时必须分清是哪一步 —— 取 token 失败指向凭据(app_id/app_secret 错或应用被停用),
    发消息失败指向群权限或 chat_id(最常见的是机器人没被拉进群)。两者该查的地方不同, 所以
    stderr 里分开写。合成一句「发送失败」会让收信人从零开始查。

    三个入参缺任何一个就返回 False 并打 stdout, 理由同 `post_text` 的空 URL: 配置没投放时
    「什么都没发生」与「一切正常」不可区分。
    """
    if not (app_id and app_secret and chat_id):
        missing = [
            name
            for name, value in (
                ("feishu_app_id", app_id),
                ("feishu_app_secret", app_secret),
                ("feishu_chat_id", chat_id),
            )
            if not value
        ]
        print(
            f"[monitor] 应用机器人通路缺配置({', '.join(missing)}), 消息只打到 stdout:\n" + text,
            file=sys.stdout,
        )
        return False

    # 延迟导入: bitable 那边已经有一份 tenant_token 实现, 复用它而不是再写一份 —— 两份
    # 实现会各自过期, 而「token 怎么取」是将来最可能变的一处。
    #
    # 为什么是延迟而不是放到文件头: 顶层导入会让**发消息**在导入期就依赖写表模块, 于是
    # bitable.py 里任何一个导入期错误都能让告警发不出去 —— 而写表失败不该影响发消息, 这是
    # bitable.push 已经立下的规矩。延迟到真要用时导入, 失败面就只留在这一条通路里。
    from bitable import BitableError, tenant_token  # noqa: PLC0415

    try:
        token = tenant_token(app_id, app_secret, opener=opener)
    except BitableError as exc:
        # 这一步失败指向**凭据**, 不是群。写清楚, 否则会去查群权限而白查。
        print(
            f"[monitor] 应用机器人取 token 失败(该查的是凭据, 不是群): {exc}\n未发出的内容:\n{text}",
            file=sys.stderr,
        )
        return False

    return _post(
        BASE + "/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "text",
            # content 是**字符串化的 JSON**, 不是嵌套对象 —— 飞书 im/v1 与自定义机器人
            # webhook 在这一点上不同。传嵌套对象会拿到 code 非 0 而 HTTP 仍是 200。
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        text,
        sleep=sleep,
        opener=opener,
        headers={"Authorization": f"Bearer {token}"},
        failed_hint="(该查的是群权限或 chat_id: 最常见是机器人没被拉进群)",
    )


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
    return _post(
        webhook_url,
        {"msg_type": "text", "content": {"text": text}},
        text,
        sleep=sleep,
        opener=opener,
        # 两条通路共用 `_post`, 所以「是哪条路失败了」必须由调用方带进来 —— 一句泛泛的
        # 「投递失败」在有两条路以后就不够定位了。
        failed_hint="(webhook 通路)",
    )


def _post(
    webhook_url: str,
    body: dict,
    echo: str,
    *,
    sleep,
    opener,
    headers: dict[str, str] | None = None,
    failed_hint: str = "",
) -> bool:
    """实际的 POST + 重试。`echo` 是放弃时打到 stderr 的内容。

    `headers` 给应用通路带 Authorization。`failed_hint` 是放弃时附在错误后的一句「该查
    哪儿」—— 两条通路的排查方向不同, 共用一句泛泛的「投递失败」等于没说。
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    text = echo
    send = opener.open if opener is not None else urllib.request.urlopen

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # URL 来自宿主配置文件(凭据, 不进仓库), 不是用户输入。
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
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
        f"[monitor] 投递失败, 已重试 {MAX_ATTEMPTS} 次后放弃{failed_hint}: {last_error}\n未发出的内容:\n{text}",
        file=sys.stderr,
    )
    return False


def post(cfg, text: str, *, sleep=time.sleep, opener=None) -> bool:
    """按配置挑通路发一条纯文本 —— **调用方只该用这个**, 不直接挑 post_chat/post_text。

    优先级: 配了 `feishu_chat_id` 走应用机器人, 否则走 `webhook_url`。

    为什么不做「应用失败则回退 webhook」: 两条通路在生产上不会同时配, 回退分支会是一条永不
    执行的死代码 —— 而永不执行的回退分支比没有回退更坏, 它让人以为有兜底。真要双通路得先有
    「两条都配」这个实际场景, 现在没有。

    `cfg` 不标注类型: 标了会让 notify 在导入期依赖 config, 而这一层要能被 cron-wrap 之外的
    最小环境直接用。它只需要有那三个属性。
    """
    chat_id = getattr(cfg, "feishu_chat_id", "")
    if chat_id:
        return post_chat(
            getattr(cfg, "feishu_app_id", ""),
            getattr(cfg, "feishu_app_secret", ""),
            chat_id,
            text,
            sleep=sleep,
            opener=opener,
        )
    return post_text(getattr(cfg, "webhook_url", ""), text, sleep=sleep, opener=opener)
