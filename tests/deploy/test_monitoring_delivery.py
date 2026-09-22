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
    assert "webhook 投递失败" in err
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
