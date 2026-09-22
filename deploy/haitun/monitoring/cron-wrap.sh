#!/usr/bin/env bash
# cron 包装层 —— **脚本非零退出即发「日报生成失败」**。
#
# 这一层为什么必须存在:
#
# 没有它, 监控脚本挂掉的表现是「群里安静」, 而安静与「一切正常」**不可区分**。故障史里
# 最贵的一次是 gateway 502 持续 29 小时无人知晓 —— 那次恰恰是「没有任何消息」被读成了
# 「没有任何问题」。如果日报脚本自己也能静默死掉, 我们就是把同一个坑挖了第二遍。
#
# 所以: 内层非零退出 → 无条件往 webhook 发一条「生成失败」, 带 stderr 尾部。
#
# ## 时区: 宿主时区, 不是容器时区
#
# 本脚本跑在**宿主 cron** 上, 用的是宿主时区。容器里那套时区只由 compose 的
# `TZ=Asia/Shanghai` 撑着, 容器内 `/etc/localtime` **恒为 UTC** —— 谁去核它都会得到
# 「UTC」这个假阴性, 然后据此把 cron 时刻调偏 8 小时。装 crontab 前先在宿主上 `date`
# 一次, 确认宿主时区就是你以为的那个。crontab.example 里的 8:57 是宿主本地时间。
#
# ## 用法
#
#     cron-wrap.sh daily
#     cron-wrap.sh fast
#     cron-wrap.sh heartbeat
#
# 不 `set -e`: 内层失败是本脚本要**处理**的正常分支, 不是要退出的错误。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TIER="${1:-daily}"

# 宿主上装的 python3。刻意不用容器里那个 —— 依赖容器等于把监控绑在被监控对象上。
PY="${HAITUN_MONITOR_PYTHON:-python3}"

# 即时档轮数计数: heartbeat 那档要报「过去 24 小时跑了多少轮」, 而轮数少于预期才是
# 「cron 漏跑」的唯一线索。用一个追加的 stamp 文件数, 不引 sqlite。
STAMP_DIR="${HAITUN_MONITOR_STATE:-/var/lib/haitun-monitor}"
STAMP="$STAMP_DIR/fast.stamp"

ERR_FILE="$(mktemp)"
trap 'rm -f "$ERR_FILE"' EXIT

# stderr 先落文件, 跑完再原样转出去。
#
# **刻意不用 `2> >(tee "$ERR_FILE" >&2)`**: 进程替换里的 tee 是异步的, 父进程拿到 `$?`
# 时它可能还没把最后一块写完 —— 于是失败通知里的「错误尾部」时有时无。那种缺陷只在
# 内层输出较多时显形, 平时看着好好的。先落盘再 cat 是确定性的, 代价只是 stderr 晚几毫秒
# 到 cron 日志。
if [ "$TIER" = "heartbeat" ]; then
  # 数过去 24 小时的轮数, 然后把 stamp 截掉。`find -newermt` 不适用(是同一个文件),
  # 所以按行存时间戳、按时间过滤行。
  CYCLES=0
  if [ -f "$STAMP" ]; then
    CUTOFF=$(date -d '24 hours ago' +%s 2>/dev/null || echo 0)
    CYCLES=$(awk -v c="$CUTOFF" '$1 >= c' "$STAMP" | wc -l | tr -d ' ')
    # 保留窗口内的行, 不让文件无限长。
    awk -v c="$CUTOFF" '$1 >= c' "$STAMP" > "$STAMP.tmp" 2>/dev/null && mv "$STAMP.tmp" "$STAMP"
  fi
  "$PY" "$HERE/run.py" heartbeat "$CYCLES" 2> "$ERR_FILE"
  RC=$?
else
  "$PY" "$HERE/run.py" "$TIER" 2> "$ERR_FILE"
  RC=$?
  if [ "$TIER" = "fast" ]; then
    mkdir -p "$STAMP_DIR" 2>/dev/null
    date +%s >> "$STAMP" 2>/dev/null
  fi
fi
cat "$ERR_FILE" >&2

if [ "$RC" -ne 0 ]; then
  # 失败通知走**同一个 webhook, 但不经 run.py** —— run.py 已经证明自己跑不起来了,
  # 再调它一次发通知是在赌「它只坏了一半」。这里直接 curl, 少一层依赖。
  #
  # webhook URL 的读取与 config.py 同顺序: 环境变量优先, 否则宿主配置文件。它是凭据,
  # 不进仓库。
  CONF="${HAITUN_MONITOR_CONF:-/etc/haitun/monitoring.conf}"
  URL="${HAITUN_MONITOR_WEBHOOK:-}"
  if [ -z "$URL" ] && [ -f "$CONF" ]; then
    URL="$(sed -n 's/^[[:space:]]*webhook_url[[:space:]]*=[[:space:]]*//p' "$CONF" | tail -1 | tr -d "\"'")"
  fi

  TAIL="$(tail -c 1500 "$ERR_FILE" 2>/dev/null)"
  TEXT="【生成失败】HaiTun ${TIER} · $(date '+%Y-%m-%d %H:%M:%S %Z')

■ 监控脚本非零退出(码 ${RC}) —— 本轮指标全部未采集。
  这条消息的含义是「不知道生产是什么状态」, 不是「生产正常」。

错误尾部:
${TAIL:-(无 stderr 输出)}"

  if [ -n "$URL" ]; then
    # python 来做 JSON 转义: 错误正文里有引号/换行/反斜杠, 手拼 JSON 会让飞书整条拒收
    # —— 而被拒收的表现又是「群里安静」, 正是本脚本要消灭的那件事。
    "$PY" -c 'import json,sys; sys.stdout.write(json.dumps({"msg_type":"text","content":{"text":sys.stdin.read()}},ensure_ascii=False))' \
      <<< "$TEXT" > "$ERR_FILE.json"
    curl --silent --show-error --max-time 15 \
      --header 'Content-Type: application/json; charset=utf-8' \
      --data @"$ERR_FILE.json" "$URL" >/dev/null
    rm -f "$ERR_FILE.json"
  else
    echo "[monitor] 无 webhook URL, 失败通知只能进 cron 日志:" >&2
    echo "$TEXT" >&2
  fi
fi

exit "$RC"
