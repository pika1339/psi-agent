"""`deploy/haitun/monitoring/` 的投递链路判据 —— **真起 HTTP server, 不 mock 传输层**。

## 判据为什么长这样

卡里的三条原则:

1. **判据必须落在它声称的那一层。** 说测公网就真发 HTTP 请求。曾有 docstring 说测 AI 层
   却调 Session 层函数, 连变异复核都照不出来。所以本文件里凡是名字带「投递」「webhook」
   的判据, 都往一个 `http.server` 收集器真发 POST, 断言的是**服务端收到了什么**, 不是
   「调了哪个函数」;
2. **兜底链判据用仓库里不存在的资源名。** 用真名会让「目录在但内容不全」这类提前终止
   全绿通过。本文件用 `example.invalid` 这类 RFC 2606 保留名与随机端口;
3. **「未测到」与「零」不混。** 每个探针的阴性用例都断言 `status == UNKNOWN` 而不是
   「值为 0」。

## 两条验收门槛判据在哪

* 门槛 1「故意让脚本非零退出 → 群里收到日报生成失败」:
  `test_nonzero_exit_sends_failure_notification_to_real_collector`。它跑的是真的
  `cron-wrap.sh`(bash), 内层换成一个 `exit 3` 的假 run.py, 收集器是真 HTTP server。
* 门槛 2「故意断一个 vhost → 即时档一个周期内告警」:
  `test_fast_tier_alerts_on_broken_vhost_within_one_cycle`。vhost 用不可达的假 host
  构造, 断言告警真的到了收集器, 且整轮耗时远小于一个 5 分钟周期。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

_MON = Path(__file__).resolve().parents[2] / "deploy" / "haitun" / "monitoring"


def _load(name: str) -> Any:
    """按路径加载 —— 这些脚本住在 `deploy/` 下, 不属任何包。

    与 `tests/deploy/test_oauth_proxy.py` 同款做法。`sys.path` 要插 monitoring 目录:
    模块之间用裸 import 互相引用(宿主上是 `python3 run.py`, 那时 cwd 就在该目录)。

    返回标成 `Any` 而不是 `ModuleType`: 判据要替换 `run.build_fast` / `run.Config.load`
    这类模块属性, 而 `ModuleType` 上没有这些名字 —— 标成 `ModuleType` 会让 `ty` 每处都报
    一条 unresolved-attribute, 把真诊断淹在噪音里。
    """
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
notify = _load("notify")
render = _load("render")
probes_public = _load("probes_public")
probes_resources = _load("probes_resources")
run = _load("run")


# ───────────────────────── 真 HTTP 收集器 ─────────────────────────


@dataclass
class Collector:
    """收到的 POST 都记下来。`posts` 为空就是「一条都没发出去」的判据。"""

    posts: list[dict] = field(default_factory=list)
    #: 下发的状态码。判据用它构造「webhook 回 5xx」的情形。
    status: int = 200
    #: >0 则前 N 次请求回 500, 之后回 200 —— 用来验重试真的重试了。
    fail_first: int = 0
    seen: int = 0
    #: True 则回 **HTTP 200 + body 带非零 code**。
    #:
    #: 这是飞书的实测行为: 不认的消息它不回 4xx, 而是 200 带 `{"code":9499,...}`。只看 HTTP
    #: 状态码的实现会把「被拒」判成发送成功, 于是重试和 stderr 留痕都不触发 —— 而表现是群里
    #: 什么都没有, 与「一切正常」不可区分。
    reject_text: bool = False
    #: 每次 POST 的路径, 与 `posts` 同序。应用通路是**两步**(取 token、发消息), 不记路径
    #: 就分不清「token 拿到了但消息没发」与「两步都成了」—— 而前者的表现也是群里安静。
    paths: list[str] = field(default_factory=list)
    #: 每次 POST 的 Authorization 头(没有则空串), 与 `posts` 同序。
    auths: list[str] = field(default_factory=list)
    #: 非空则取 token 那一步回这个错误 code, 用来构造「凭据错」而非「群权限错」。
    token_error: int = 0


def _make_handler(col: Collector) -> type[BaseHTTPRequestHandler]:
    """按 collector 造一个 handler 类。

    用闭包而不是往 `server` 上挂属性: `HTTPServer` 没有 `collector` 这个字段, 挂上去要么
    加 `type: ignore` 要么让类型检查报错 —— 而 `ty` 的诊断数是本仓的闸门之一。
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            col.seen += 1
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError, UnicodeDecodeError:
                body = {"_raw": raw.decode("utf-8", "replace")}
            col.posts.append(body)
            col.paths.append(self.path)
            col.auths.append(self.headers.get("Authorization") or "")

            # 取 token 那一步单独应答 —— 它的响应体形状与发消息不同(tenant_access_token
            # 在顶层), 共用一个 `{"code":0}` 会让应用通路永远拿不到 token。
            if "tenant_access_token" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if col.token_error:
                    self.wfile.write(json.dumps({"code": col.token_error, "msg": "app not found"}).encode())
                else:
                    self.wfile.write(b'{"code":0,"tenant_access_token":"t-judge-token","expire":7200}')
                return

            status = 500 if col.seen <= col.fail_first else col.status
            reject = col.reject_text
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # 拒收时回 200 + 非零 code, 见 Collector.reject_text 的注释。
            self.wfile.write(b'{"code":9499,"msg":"invalid card"}' if reject else b'{"code":0}')

        # 形参名 `format` 是基类签名, 不能改 —— 改了 `ty` 就报 invalid-method-override。
        def log_message(self, format: str, *args: object) -> None:
            """噤声 —— 否则每条请求往 stderr 打一行, 把判据输出淹掉。"""

    return _Handler


@pytest.fixture
def collector():
    """起一个真的 HTTP server 在随机端口上。yield (collector, url)。"""
    col = Collector()
    server = HTTPServer(("127.0.0.1", 0), _make_handler(col))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield col, f"http://127.0.0.1:{server.server_port}/hook"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ───────────────────────── 投递本身 ─────────────────────────


def test_post_text_reaches_real_http_server(collector):
    """最基本的一层: 真发一次 POST, 服务端真收到, 且格式是飞书自定义机器人的。

    断言的是**服务端收到的 body**, 不是「post_text 返回了 True」—— 后者在 URL 拼错、
    payload 编码坏掉时照样能绿(比如把请求发给了别的地址而那边也回 200)。
    """
    col, url = collector
    assert notify.post_text(url, "判据文本 ✓") is True
    assert len(col.posts) == 1
    assert col.posts[0]["msg_type"] == "text"
    assert col.posts[0]["content"]["text"] == "判据文本 ✓"


def test_post_chat_takes_two_steps_and_carries_the_token(collector, monkeypatch):
    """应用通路: 先取 token, 再带 `Bearer` 发到 `im/v1/messages`。

    断言的是**服务端收到的两次请求**(路径、顺序、Authorization 头), 不是「返回了 True」——
    后者在「token 没带上」时照样绿, 而那时线上会拿到 401 而群里安静。

    `content` 必须是**字符串化的 JSON**: im/v1 与自定义机器人在这点上不同, 传嵌套对象会
    拿到 HTTP 200 + 非零 code。这条判据把它钉住, 因为那种失败的表现也是群里安静。
    """
    col, url = collector
    base = url.rsplit("/", 1)[0]
    monkeypatch.setattr(notify, "BASE", base)
    # bitable 那份 tenant_token 也要指到收集器上 —— post_chat 复用的是它。
    monkeypatch.setattr(_load("bitable"), "BASE", base)

    assert notify.post_chat("cli_judge", "secret_judge", "oc_judge", "应用通路 ✓") is True

    assert len(col.posts) == 2, f"必须是两步(取 token + 发消息), 实收 {col.paths}"
    assert "tenant_access_token" in col.paths[0], f"第一步该取 token, 实为 {col.paths[0]}"
    assert col.posts[0]["app_id"] == "cli_judge"
    assert "/im/v1/messages" in col.paths[1], f"第二步该发消息, 实为 {col.paths[1]}"
    assert "receive_id_type=chat_id" in col.paths[1], "要声明 receive_id_type, 否则飞书按 open_id 解析"
    assert col.auths[1] == "Bearer t-judge-token", f"发消息必须带上第一步拿到的 token, 实为 {col.auths[1]!r}"
    assert col.posts[1]["receive_id"] == "oc_judge"
    assert col.posts[1]["content"] == '{"text": "应用通路 ✓"}', (
        "content 必须是字符串化的 JSON —— 传嵌套对象会拿到 200 + 非零 code, 表现同样是群里安静"
    )


def test_post_chat_token_failure_blames_credentials_not_the_group(collector, monkeypatch, capsys):
    """取 token 失败 → 只发生一步, 且 stderr 指向**凭据**而不是群权限。

    两步失败该查的地方不同: token 失败是 app_id/app_secret 错或应用被停用, 发消息失败是
    机器人没进群或 chat_id 错。合成一句「发送失败」会让人从零开始查 —— 这正是码 2 那条通知
    早先犯的错, 只是更细一层。
    """
    col, url = collector
    base = url.rsplit("/", 1)[0]
    col.token_error = 10003
    monkeypatch.setattr(notify, "BASE", base)
    monkeypatch.setattr(_load("bitable"), "BASE", base)

    assert notify.post_chat("cli_bad", "secret_bad", "oc_judge", "不该发出去") is False

    assert len(col.posts) == 1, "token 都没拿到就不该发第二步 —— 那一步必然 401, 只是白打一次"
    err = capsys.readouterr().err
    assert "凭据" in err, f"要指向凭据; 实际 stderr={err!r}"
    assert "群" not in err.replace("不是群", ""), "不该把人指向群权限 —— 那是另一步的成因"
    assert "不该发出去" in err, "未发出的正文要留痕, 否则内容就真丢了"


def test_post_dispatches_to_chat_when_chat_id_is_set(collector, monkeypatch):
    """派发口: 配了 chat_id 走应用通路, 没配才走 webhook。

    这条判据存在的理由是**优先级**本身会被改错: 两个分支的实现都能「发出去」, 只有断言
    「走的是哪条路」才能区分。用 `paths` 而不是返回值 —— 返回值两条路都是 True。
    """
    col, url = collector
    base = url.rsplit("/", 1)[0]
    monkeypatch.setattr(notify, "BASE", base)
    monkeypatch.setattr(_load("bitable"), "BASE", base)

    # 两条都配上, 断言 chat_id 胜出 —— 只配 chat_id 的话, 一个「总是走应用」的实现也绿。
    cfg = config.Config(
        webhook_url=url,
        feishu_app_id="cli_judge",
        feishu_app_secret="secret_judge",
        feishu_chat_id="oc_judge",
    )
    assert notify.post(cfg, "派发 ✓") is True
    assert any("/im/v1/messages" in p for p in col.paths), f"配了 chat_id 就该走应用通路, 实为 {col.paths}"
    assert not any(p.endswith("/hook") for p in col.paths), "配了 chat_id 就不该再打 webhook"

    # 对偶: chat_id 留空 → 落回 webhook。没有这一半, 「永远走应用」也能让上半绿。
    col.paths.clear()
    col.posts.clear()
    assert notify.post(config.Config(webhook_url=url), "回退 ✓") is True
    assert col.paths == ["/hook"], f"没配 chat_id 就该走 webhook, 实为 {col.paths}"


def test_post_text_retries_then_succeeds(collector):
    """前两次 5xx、第三次 200 → 最终成功, 且服务端确实收到 3 次。

    `seen == 3` 是「真重试了」的判据。只断返回 True 的话, 一个「首次就成功」的实现也绿。
    """
    col, url = collector
    col.fail_first = 2
    slept: list[float] = []
    assert notify.post_text(url, "重试", sleep=slept.append) is True
    assert col.seen == 3
    # 退避真的退了 —— 不然重试会在毫秒内打完三次, 对上游限流毫无意义。
    assert slept == [notify.BACKOFF_BASE * 1, notify.BACKOFF_BASE * 2]


def test_unreachable_webhook_gives_up_and_reports(capsys):
    """阴性: webhook 不可达 → 有限重试后**放弃**, 返回 False, 并把未发出的内容写 stderr。

    行为是刻意选的「放弃而非无限重试」: 即时档 5 分钟一轮, 一轮卡住会和下一轮叠起来。
    而 stderr 那条是「webhook 本身不可达」时唯一还活着的通路(cron 下进 MAILTO/日志)。

    端口用 `127.0.0.1:1`(特权端口且没人听)构造不可达, 不用真域名 —— 真域名可能被
    DNS 劫持或代理拦下, 那样测的就不是「不可达」。
    """
    slept: list[float] = []
    assert notify.post_text("http://127.0.0.1:1/hook", "发不出去", sleep=slept.append) is False
    err = capsys.readouterr().err
    assert "投递失败" in err
    # 有两条通路以后, 留痕必须说清是**哪条**失败了 —— 否则排查得先去猜当前配的是哪一条。
    assert "webhook 通路" in err, f"要点名是 webhook 通路; 实际={err!r}"
    assert "发不出去" in err, "未发出的内容必须留在 stderr, 否则这条消息彻底消失"
    assert len(slept) == notify.MAX_ATTEMPTS - 1


def test_empty_webhook_url_is_not_silent(capsys):
    """阴性: URL 没配 → 打到 stdout 且返回 False, **不静默丢**。

    空 URL 最可能的成因是宿主配置文件没投放。那种情况下「什么都没发生」与「一切正常」
    不可区分, 所以必须留下痕迹。
    """
    assert notify.post_text("", "没有 URL") is False
    assert "未配置 webhook URL" in capsys.readouterr().out


def test_text_is_the_only_carrier(collector):
    """发出去的是**纯文本**, 一条, 没有 interactive。

    卡片那条路删了(曾有 `post_card` / `post_report`): 图上一根共用告警线在各项告警线不同时
    会画出假越线 —— 实测磁盘 81% 的柱子越过「告警 80%」那根线, 而它自己的线是 85%。趋势改由
    多维表格承担。

    判据压的是「不再发 interactive」而不只是「发了 text」: 只压后者的话, 一个同时发卡片和
    文本的实现照样全绿, 而那是两条消息。
    """
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.bad("坏的", value="502", baseline="200", direction="应为 200"))
    col, url = collector

    assert notify.post_text(url, render.render(report), sleep=lambda _s: None) is True
    assert [p["msg_type"] for p in col.posts] == ["text"], f"只该有一条纯文本, 实收 {col.posts}"
    assert "坏的" in col.posts[0]["content"]["text"]


def test_text_rejected_with_http_200_is_not_counted_as_success(collector):
    """**HTTP 200 + body 非零 code** 不算成功。

    飞书对不认的消息回的是 200 带非零 `code`, 不是 4xx。只看 HTTP 状态码的实现会判成发送
    成功, 于是重试和 stderr 留痕都不触发 —— 表现是群里什么都没有, 与一切正常不可区分。

    卡片删掉后这条判据更要留: 原先它由「卡片被拒要回退」那条顺带压着, 现在纯文本是唯一
    载体, 没别的判据会碰这段 body 检查了。
    """
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.bad("坏的", value="502", baseline="200", direction="应为 200"))
    col, url = collector
    col.reject_text = True

    slept: list[float] = []
    assert notify.post_text(url, render.render(report), sleep=slept.append) is False
    assert len(col.posts) == notify.MAX_ATTEMPTS, "被拒要重试到上限, 而不是发一次就算完"
    assert len(slept) == notify.MAX_ATTEMPTS - 1


# ───────── 门槛判据 1: 非零退出 → 群里收到「日报生成失败」 ─────────


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash 跑 cron-wrap.sh")
def test_nonzero_exit_sends_failure_notification_to_real_collector(collector, tmp_path):
    """**验收门槛 1。** 内层脚本非零退出 → 真的 `cron-wrap.sh` 往真收集器发「生成失败」。

    这条判据跑的是真 bash + 真 HTTP, 中间没有 mock:

    * 把 `cron-wrap.sh` 与一个**故意 `exit 3`** 的假 `run.py` 拷进 tmp_path;
    * 用 `HAITUN_MONITOR_WEBHOOK` 指向收集器;
    * 断言收集器收到了一条, 内容含「生成失败」与内层的 stderr 尾部, 且包装层原样透传 3。

    为什么假 run.py 而不是让真 run.py 失败: 要测的是**包装层**这一环, 而让真 run.py 失败
    需要构造环境损坏, 那会把判据变成「环境损坏的某一种」的测试。

    ⚠️ 这条判据在 Windows 上跑的是 Git Bash。`cron-wrap.sh` 用到 `date -d`/`mktemp`/`awk`,
    Git Bash 都有。若在没有 bash 的环境上则 skip —— skip 不是绿, 见文件末尾的说明。
    """
    col, url = collector
    shutil.copy(_MON / "cron-wrap.sh", tmp_path / "cron-wrap.sh")
    (tmp_path / "run.py").write_text(
        "import sys\nprint('内层的错误尾部 MARKER-STDERR-42', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HAITUN_MONITOR_WEBHOOK"] = url
    env["HAITUN_MONITOR_PYTHON"] = sys.executable
    env["HAITUN_MONITOR_STATE"] = str(tmp_path / "state")
    # 配置文件指向一个**不存在的路径** —— 兜底链判据不能用真名, 否则宿主上真有
    # /etc/haitun/monitoring.conf 时会静默走到那份配置上。
    env["HAITUN_MONITOR_CONF"] = str(tmp_path / "no-such-conf-do-not-create")

    proc = subprocess.run(
        ["bash", str(tmp_path / "cron-wrap.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 3, f"包装层必须原样透传内层退出码; stderr={proc.stderr[-500:]}"
    assert len(col.posts) == 1, f"非零退出必须发出一条失败通知, 实收 {len(col.posts)} 条; stderr={proc.stderr[-800:]}"
    text = col.posts[0]["content"]["text"]
    assert "生成失败" in text
    assert "码 3" in text, "要带上退出码 —— 只说失败会让人从零开始查"
    assert "MARKER-STDERR-42" in text, "要带 stderr 尾部"
    assert "不是「生产正常」" in text, "必须写明「不知道状态」≠「正常」"
    assert "投递失败" not in text, "码 3 不是投递失败 —— 若两种码给同一段话, 下一条判据(码 2)就只是在重复这一条"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash 跑 cron-wrap.sh")
def test_exit_code_2_is_reported_as_delivery_failure_not_collection_failure(collector, tmp_path):
    """码 2(投递失败)与其它非零码必须给**不同的**归因。

    实测 2026-09-22: webhook 未配时心跳档稳定退 2 且 stderr 全空, 于是群里收到的是
    「本轮指标全部未采集」+ 一个空的错误尾部 —— 指标其实全采到了, 只是发不出去。一条把
    投递失败说成采集失败的通知比没有通知更坏: 它会让人去查生产, 而该改的是一行配置。

    判据必须**同时**要求说对和不说错: 只断言「含投递失败」的话, 一个把两段话都拼上去的
    实现也能绿, 而那样归因依旧是模糊的。
    """
    col, url = collector
    shutil.copy(_MON / "cron-wrap.sh", tmp_path / "cron-wrap.sh")
    # stderr 留空 —— 这正是线上那次的形态: 码 2 时内层没话说, 通知里除了归因没别的信息。
    (tmp_path / "run.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    env = dict(os.environ)
    env["HAITUN_MONITOR_WEBHOOK"] = url
    env["HAITUN_MONITOR_PYTHON"] = sys.executable
    env["HAITUN_MONITOR_STATE"] = str(tmp_path / "state")
    env["HAITUN_MONITOR_CONF"] = str(tmp_path / "no-such-conf-do-not-create")

    proc = subprocess.run(
        ["bash", str(tmp_path / "cron-wrap.sh"), "heartbeat"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 2, f"包装层必须原样透传 2; stderr={proc.stderr[-500:]}"
    assert len(col.posts) == 1, f"码 2 照样要发通知, 实收 {len(col.posts)} 条"
    text = col.posts[0]["content"]["text"]
    assert "投递失败" in text, "码 2 要说清是发不出去"
    assert "webhook_url" in text, "要点名该去看哪一项配置, 否则收信人只能从零开始查"
    assert "全部未采集" not in text, "码 2 时指标是采到了的 —— 说成未采集会把人指向生产, 而该改的是配置"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash 跑 cron-wrap.sh")
def test_zero_exit_sends_no_failure_notification(collector, tmp_path):
    """门槛 1 的对偶: 内层退出 0 → 包装层**不发**失败通知。

    没有这条, 一个「无条件发失败通知」的实现也能让上一条绿, 而那样每天都会收到一条假报,
    继而整条通道被静音 —— 与没有通知等价。
    """
    col, url = collector
    shutil.copy(_MON / "cron-wrap.sh", tmp_path / "cron-wrap.sh")
    (tmp_path / "run.py").write_text("print('ok')\n", encoding="utf-8")
    env = dict(os.environ)
    env["HAITUN_MONITOR_WEBHOOK"] = url
    env["HAITUN_MONITOR_PYTHON"] = sys.executable
    env["HAITUN_MONITOR_STATE"] = str(tmp_path / "state")
    env["HAITUN_MONITOR_CONF"] = str(tmp_path / "no-such-conf-do-not-create")
    proc = subprocess.run(
        ["bash", str(tmp_path / "cron-wrap.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0
    assert col.posts == [], "内层成功时不该有失败通知"


# ───────── 门槛判据 2: 断一个 vhost → 即时档一个周期内告警 ─────────


class _Proc:
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


class _FakeRunner:
    """假 subprocess.run。按命令行里的关键字挑响应, 并记下每次真实参数。

    它替换的是**进程边界**, 不是 curl 的行为 —— 命令行原样传进来, 所以
    `test_vhost_probe_uses_resolve_not_host_header` 能断言参数本身。

    写成类而不是「函数 + 挂 `.calls`」: 后者在类型检查里是 unresolved-attribute。
    """

    def __init__(self, script: dict[str, tuple[int, str, str]]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs) -> _Proc:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, (rc, out, err) in self.script.items():
            if key in joined:
                return _Proc(rc, out, err)
        return _Proc(0, "", "")


def _fake_runner(script: dict[str, tuple[int, str, str]]) -> _FakeRunner:
    return _FakeRunner(script)


def test_fast_tier_alerts_on_broken_vhost_within_one_cycle(collector):
    """**验收门槛 2。** 一个 vhost 断了 → 即时档在一个周期内把告警真发到收集器。

    「断」用 curl 回 502 构造(真实的 502 形态), host 用 RFC 2606 保留域名
    `broken.invalid` —— 保留名永远解析不到真机器, 判据不会因为环境里恰好有同名主机而变绿。

    「一个周期内」用**墙钟**断言: 整轮耗时必须远小于 5 分钟(取 60 秒余量)。这不是性能
    测试, 是门槛本身 —— 一轮跑超过一个周期意味着两轮会叠起来, 告警就不再是「一个周期内」。
    """
    col, url = collector
    cfg = config.Config(
        webhook_url=url,
        vhosts=[("broken.invalid", 200)],
        public_ip="203.0.113.1",  # RFC 5737 文档用地址, 不是真机器
        containers=(),
        timeout_seconds=1,
    )
    runner = _fake_runner(
        {
            "https://broken.invalid/": (0, "HTTPCODE:502", ""),
            "openssl": (0, "notAfter=Dec 31 23:59:59 2099 GMT", ""),
            "8090": (0, "HTTPCODE:404", ""),
            "dmesg": (0, "[    0.000000] Linux version 5.10\n", ""),
        }
    )
    started = time.monotonic()
    report = run.build_fast(cfg, runner=runner)
    assert notify.post_text(url, render.render(report)) is True
    elapsed = time.monotonic() - started

    assert elapsed < 60, f"一轮 {elapsed:.1f}s, 接近或超过 5 分钟周期则告警不再是「一个周期内」"
    assert len(col.posts) == 1
    text = col.posts[0]["content"]["text"]
    assert text.startswith("【异常】"), f"异常必须在标题上就看得见, 实际: {text[:40]!r}"
    assert "broken.invalid" in text
    assert "502" in text
    # 异常要排在正常之前 —— 位置关系是硬要求, 不是排版偏好。
    assert "■ 异常" in text
    if "□ 正常" in text:
        assert text.index("■ 异常") < text.index("□ 正常")


def test_fast_tier_stays_silent_when_all_green(collector):
    """门槛 2 的对偶: 全绿时即时档**不出声**。

    理由: 5 分钟一条会被无视继而被静音, 而静音之后连真告警也收不到。存活由 heartbeat
    那一档负责, 不靠「每轮都说一句我还活着」。

    这条也挡住一个作弊实现: 「无条件发告警」能让上一条绿。
    """
    col, url = collector
    # containers 必须给 —— 空配置会让 wss 探针报 UNKNOWN(「缺配置不是健康」, 见
    # probes_public.collect)。第一次写这条判据时我留了空元组, 它红了, 而红得对:
    # 全绿的前提是所有探针都真的量到了。
    cfg = config.Config(
        webhook_url=url,
        vhosts=[("good.invalid", 200)],
        public_ip="203.0.113.1",
        containers=("psi-agent-gateway",),
        timeout_seconds=1,
    )
    runner = _fake_runner(
        {
            "https://good.invalid/": (0, "HTTPCODE:200", ""),
            "openssl": (0, "notAfter=Dec 31 23:59:59 2099 GMT", ""),
            "oauth/callback": (0, "HTTPCODE:302", ""),
            "8090": (0, "HTTPCODE:404", ""),
            "dmesg": (0, "[    0.000000] Linux version 5.10\n", ""),
            # wss 走宿主 nsenter 而不是 `docker exec` —— 容器里没有 `ss`。两条都得给:
            # 拿不到 netns PID 时探针报 UNKNOWN, 而 UNKNOWN 不算「全绿」。
            "inspect": (0, "3088458\n", ""),
            "nsenter": (
                0,
                "Recv-Q Send-Q Local Address:Port Peer Address:Port\n0 0 172.21.0.2:40404 61.170.82.99:443\n",
                "",
            ),
        }
    )
    report = run.build_fast(cfg, runner=runner)
    assert not report.has_anomaly, [
        f"{f.name}={f.status}:{f.value}|{f.reason}" for f in report.findings if f.status != findings.OK
    ]
    # 断言的是**投递这一层**真的没发生, 不是「has_anomaly 为 False」—— 后者在 main 里
    # 那个 if 写反时照样绿。走真 main, 只把 Config.load 换成上面这份 cfg(配置来源不是
    # 本条判据要测的东西), 收集器 posts 为空才是「没出声」的证据。
    original_load = run.Config.load
    original_build = run.build_fast
    run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
    # build_fast 换成「返回上面这份已建好的绿报告」: 本条判据测的是 main 里
    # 「无异常就不投递」那个分支, 探针层由别的判据守。不换掉它的话 main 会真跑 curl
    # 打 good.invalid, 于是必然 UNKNOWN、必然投递, 测的就不是这个分支了。
    run.build_fast = lambda c, **kw: report  # type: ignore[assignment]
    try:
        assert run.main(["fast"]) == 0
    finally:
        run.Config.load = original_load  # type: ignore[assignment]
        run.build_fast = original_build  # type: ignore[assignment]
    assert col.posts == [], f"全绿时即时档必须沉默, 实收 {len(col.posts)} 条"


def test_broken_vhost_alerts_then_next_round_goes_back_to_silent(collector):
    """**告警一次之后要能停。** 断 → 告警 → 恢复 → 下一轮沉默, 两轮连着跑。

    为什么不是上面两条判据的重复: 它们一条测「断了会响」、一条测「一直绿不响」, 两条都
    绿的实现里仍然存在一种 —— **响过一次之后就再也停不下来**(例如把越线状态记在模块级
    变量里、或把 vhost 探针的失败缓存住)。那种实现的表现是从此天天报, 而告警天天来的结局
    与从不告警等价: 收信人学会跳过整条消息。

    所以这条判据的形状是**一个序列**: 同一份 cfg、同一个进程, 只换 runner 的响应, 第一轮
    必须发且只发一条, 第二轮必须一条都不发。断言 `len(col.posts)` 的**增量**而不是总数,
    否则第二轮多发一条时总数从 1 变 2 也能被写成「还是有消息」而看不出是哪一轮发的。

    2026-09-22 在生产宿主上实测过同一序列(用 `HAITUN_MONITOR_VHOSTS` 注入一个不存在的
    域名, 不改生产 caddy): 注入轮真发到群里(飞书 `code=0`, message_id
    `om_x100b64135c9ae4a8b16240f579ebf0a`), 去掉注入后的下一轮打印「即时档全绿, 不发
    消息。8 项已采集」。本判据是那次实测的回归形态。
    """
    col, url = collector
    cfg = config.Config(
        webhook_url=url,
        vhosts=[("flaky.invalid", 200)],
        public_ip="203.0.113.1",
        containers=("psi-agent-gateway",),
        wss_containers=("psi-agent-gateway",),
        timeout_seconds=1,
    )
    # 两轮只差 vhost 那一条响应。其余响应保持健康, 好让「第二轮沉默」的成因唯一 ——
    # 否则某个别的探针顺带转绿也能让第二轮沉默, 而那不是本判据要证明的事。
    healthy = {
        "openssl": (0, "notAfter=Dec 31 23:59:59 2099 GMT", ""),
        "oauth/callback": (0, "HTTPCODE:302", ""),
        "8090": (0, "HTTPCODE:404", ""),
        "dmesg": (0, "[    0.000000] Linux version 5.10\n", ""),
        "inspect": (0, "3088458\n", ""),
        "nsenter": (
            0,
            "Recv-Q Send-Q Local Address:Port Peer Address:Port\n0 0 172.21.0.2:40404 61.170.82.99:443\n",
            "",
        ),
    }

    def one_round(vhost_response: tuple[int, str, str]) -> int:
        """跑一轮真 `main`, 返回**这一轮**发出的消息条数。

        `main` 内部是 `build_fast(cfg)` —— **不带 runner**, 所以只换 `Config.load` 的话
        两轮都会真去 curl `flaky.invalid`。第一次写这条判据时就是这样, 结果断掉那一轮
        「绿」得毫无意义: 它响是因为真 curl 连不上保留域名, 与我脚本里那个 502 无关, 而
        恢复那一轮同样连不上、于是照样响。所以这里把 `build_fast` 换成「带上我的 runner
        调真的 build_fast」, 探针逻辑一行不绕过, 只把进程边界换掉。
        """
        before = len(col.posts)
        runner = _fake_runner({"https://flaky.invalid/": vhost_response, **healthy})
        original_load = run.Config.load
        original_build = run.build_fast
        run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
        run.build_fast = lambda c, **kw: original_build(c, runner=runner)  # type: ignore[assignment]
        try:
            # 走真 main 而不是只看 report: 「响过就停不下来」最可能的落点是 main 里那个
            # 投递分支, 只断言 report 的话那种实现照样绿。
            assert run.main(["fast"]) == 0
        finally:
            run.Config.load = original_load  # type: ignore[assignment]
            run.build_fast = original_build  # type: ignore[assignment]
        return len(col.posts) - before

    broken_round = one_round((0, "HTTPCODE:502", ""))
    assert broken_round == 1, f"断掉那一轮必须发且只发一条, 实发 {broken_round} 条"
    assert "flaky.invalid" in col.posts[-1]["content"]["text"]

    recovered_round = one_round((0, "HTTPCODE:200", ""))
    assert recovered_round == 0, (
        f"恢复后的下一轮必须回到沉默, 实发 {recovered_round} 条 —— 告警停不下来等于天天报, 而天天报与从不报等价"
    )


def test_fast_tier_posts_when_anomaly_present(collector):
    """上一条的对偶, 守住同一个 `if` 的另一侧: 有异常时 main **必须**投递。

    两条一起才把那个分支钉住 —— 只有沉默那条时, 一个「永远不投递」的实现也全绿, 而那
    正是最坏的结果(即时档彻底哑掉)。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="即时档", timestamp="T")
    report.add(findings.bad("假指标", value="502", baseline="200", direction="应为 200"))
    original_load = run.Config.load
    original_build = run.build_fast
    run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
    run.build_fast = lambda c, **kw: report  # type: ignore[assignment]
    try:
        assert run.main(["fast"]) == 0
    finally:
        run.Config.load = original_load  # type: ignore[assignment]
        run.build_fast = original_build  # type: ignore[assignment]
    assert len(col.posts) == 1
    # 判据钉在「指标名真的进了发出去的那条消息」上, 而不是钉在载体形状上 —— 用整条 JSON
    # 序列化后搜, 换载体不用改判据, 而「投递了但内容是空的」照样抓得到。
    post = col.posts[0]
    assert post["msg_type"] == "text"
    assert "假指标" in json.dumps(post, ensure_ascii=False)


# ==========================================================================
# 发群的门槛是「紧急」而不是「有异常」(2026-09-22 负责人定)
# ==========================================================================


def _daily_run(cfg, report, *, wrote: bool = True) -> int:
    """走真 `run.main(["daily"])`, 只把配置、报告与写表结果换成给定的。

    判据必须走 main —— 那个 `if not urgent` 分支就住在 main 里。只调 `urgent_anomalies()`
    测不到它, 而「门槛判反了」正是这里最贵的失败模式(要么天天发, 要么彻底哑掉)。
    """
    bitable = _load("bitable")
    original = (run.Config.load, run.build_daily, bitable.push, run.bitable.push)
    run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
    run.build_daily = lambda c, **kw: report  # type: ignore[assignment]
    run.bitable.push = lambda *a, **kw: wrote  # type: ignore[assignment]
    try:
        return run.main(["daily"])
    finally:
        run.Config.load, run.build_daily, bitable.push, run.bitable.push = original  # type: ignore[assignment]


def test_daily_stays_silent_when_anomaly_is_not_urgent(collector):
    """**不紧急的异常只进表, 不发群。**

    这是负责人 2026-09-22 的原话「有大异常再群通知」的判据。触发它的实测背景: 连着几份日报
    的异常都是同样那三条(请求体字节数、压缩耗时、压缩占比), 都是真问题但都是已知常态 ——
    天天发一遍的结果是收信人学会跳过整条消息, 连真该看的一起跳过。

    断言落在**投递这一层**(收集器一条都没收到), 不落在 `urgent_anomalies()` 的返回值上:
    后者在 main 里那个 `if` 写反时照样绿。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="日报", timestamp="T")
    report.add(
        findings.bad("请求体字节数 p50/p95", value="p95 1226 KiB", baseline="p95 < 1500 KiB", direction="越高越糟")
    )
    report.add(findings.unknown("成本来源 luolin", reason="metrics/ 不存在", baseline="应能读到", direction="≠ 零花费"))

    assert _daily_run(cfg, report) == 0
    assert col.posts == [], f"不紧急的异常不该发群, 实收 {len(col.posts)} 条"
    # 但它**仍然是异常** —— 只是载体变成了表。两件事分开断言: 把 urgent 实现成「顺手把
    # status 改成 ok」也能让上面那条绿, 而那样表里也不红了。
    assert report.has_anomaly, "不发群不等于不算异常 —— 表里那一行仍要标出来"


def test_daily_posts_when_something_urgent_breaks(collector):
    """上一条的对偶: **紧急项坏掉时必须发群**, 且抬头点名是哪几项。

    只有沉默那条判据时, 一个「日报永不发群」的实现全绿 —— 而那是最坏的结果, 502 会像
    2026-09 那次一样安静地过去 29 小时。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.bad("压缩占总花费比例", value="74.0%", baseline="< 85%", direction="越高越糟"))
    report.add(findings.bad("vhost x.cn HTTPS", value="502", baseline="200", direction="应为 200", urgent=True))

    assert _daily_run(cfg, report) == 0
    assert len(col.posts) == 1, "紧急项坏掉必须发群"
    text = json.dumps(col.posts[0], ensure_ascii=False)
    assert "vhost x.cn HTTPS" in text
    assert "紧急 1 项" in text, "抬头要点名紧急项 —— 正文里它和不紧急的异常混在一节里"
    # 不紧急的那条仍在正文里(它是今天的实测结果), 只是它自己不构成发群的理由。
    assert "压缩占总花费比例" in text


def test_urgent_ok_findings_do_not_trigger_a_message(collector):
    """`urgent=True` 而状态是 OK 时**不发群**。

    `urgent` 与 `status` 是两个维度: 前者说「如果它坏了就要喊人」, 坏没坏由后者说。混成一个
    的后果是 vhost 一切正常也天天发一条 —— 比原来的噪音更糟。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.ok("vhost x.cn HTTPS", value="200", baseline="200", direction="应为 200", urgent=True))

    assert _daily_run(cfg, report) == 0
    assert col.posts == [], "紧急项**正常**时不该出声"


def test_failed_table_write_is_itself_urgent(collector):
    """**写表失败要发群**, 即使没有任何别的紧急项。

    这是「日报只进表」新开的缺口: 表成了唯一载体, 那么表没写进去就等于今天的监控没留痕 ——
    而这件事原先只打在 stderr 上进 cron 日志, 没人看 cron 日志。

    退出码仍是 0: 退出码 2 会让 cron 包装层发「日报生成失败」, 那条留给采集失败。发群与退出
    码是两条不同的通路, 判据同时钉住两者。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="日报", timestamp="T")
    report.add(findings.ok("正常项", value="200", baseline="200", direction="应为 200"))

    assert _daily_run(cfg, report, wrote=False) == 0, "写表失败不改退出码"
    assert len(col.posts) == 1, "写表失败本身就该发群"
    text = json.dumps(col.posts[0], ensure_ascii=False)
    assert "趋势表写入" in text
    # 正文里要找得到这一项, 不能只在抬头点名 —— 那会让读者去正文里找一个不存在的条目。
    assert text.count("趋势表写入") >= 2, f"抬头与正文都该有, 实际只出现在一处: {text[:200]}"


def test_fast_tier_keeps_the_old_any_anomaly_rule(collector):
    """**即时档不受紧急门槛管** —— 有异常就出声, 照旧。

    理由: 即时档不写表, 消息是它唯一的载体。给它也上门槛的话, 一条不紧急的异常(例: 证书
    剩 7 天到期)就彻底没有出口了 —— 那不是收短, 是把它删掉。

    这条与 `test_daily_stays_silent_when_anomaly_is_not_urgent` 造的是**同一份报告内容**,
    只差 tier。两条一起才能证明门槛是按档分的, 而不是按内容碰巧分的。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())
    report = findings.Report(tier="即时档", timestamp="T")
    report.add(findings.bad("证书剩余天数", value="7 天", baseline="> 14 天", direction="越低越糟"))

    original = (run.Config.load, run.build_fast)
    run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
    run.build_fast = lambda c, **kw: report  # type: ignore[assignment]
    try:
        assert run.main(["fast"]) == 0
    finally:
        run.Config.load, run.build_fast = original  # type: ignore[assignment]

    assert len(col.posts) == 1, "即时档有异常就该出声, 不看紧急与否"
    assert "证书剩余天数" in json.dumps(col.posts[0], ensure_ascii=False)


def test_silent_exit_says_why_it_was_silent_per_tier(collector, capsys):
    """沉默那行的**理由按档不同**: 日报「无紧急项」, 即时档「全绿」。

    2026-09-22 生产上即时档打出「即时档无紧急项, 不发消息」—— 字面没错但会误导: 即时档根本
    没上紧急门槛, 它沉默只可能是一条异常都没有。那句话读起来像它也在按紧急过滤, 于是排查的人
    会去找「被紧急门槛滤掉的不紧急异常」, 而那个东西不存在。

    cron 日志是这行字唯一的读者, 也是即时档全绿时唯一的痕迹, 所以它必须说对。
    """
    col, url = collector
    cfg = config.Config(webhook_url=url, containers=())

    daily = findings.Report(tier="日报", timestamp="T")
    daily.add(findings.bad("压缩占比", value="74.0%", baseline="< 85%", direction="越高越糟"))
    assert _daily_run(cfg, daily) == 0
    out = capsys.readouterr().out
    assert "无紧急项" in out, out
    assert "均不紧急, 只进表" in out, "有异常却沉默时要在 stdout 交代清楚"

    fast = findings.Report(tier="即时档", timestamp="T")
    fast.add(findings.ok("vhost", value="200", baseline="200", direction="应为 200"))
    original = (run.Config.load, run.build_fast)
    run.Config.load = classmethod(lambda cls, env=None: cfg)  # type: ignore[assignment]
    run.build_fast = lambda c, **kw: fast  # type: ignore[assignment]
    try:
        assert run.main(["fast"]) == 0
    finally:
        run.Config.load, run.build_fast = original  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "全绿" in out, out
    assert "无紧急项" not in out, f"即时档不该说「无紧急项」—— 它不按紧急过滤: {out}"
    assert col.posts == []
