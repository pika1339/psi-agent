"""成本报告的命令行入口 —— 在宿主上手跑, 或由日报卡调用。

宿主上用法(纯标准库, `python3 monthly_cost.py`, 不需要装任何包):

    python3 monthly_cost.py                    # 今天
    python3 monthly_cost.py --days 7           # 近 7 天
    python3 monthly_cost.py --date 2026-09-18  # 指定某天
    python3 monthly_cost.py --all              # jsonl 里有多少天算多少天
    python3 monthly_cost.py --pricing v1-2026-09-01   # 指名单价表版本, 按旧价重算

**日报卡不该调本文件的 `main()`**, 而是调 `cost.collect()` 拿 `CostTotals`, 再调
`cost.render_cost_section()` 拿文本 —— 那两个是本层对外的接口。本文件只是同一对函数的
一层命令行包装, 存在的意义是「负责人能在宿主上手跑一次核对」, 以及让
「换一版单价表→金额与版本号都变」这条判据有个端到端的落点。

退出码: 0 正常, 2 单价表读不出来(`PricingError`)。**非零退出是刻意的** —— cron 那层
据此发「日报生成失败」, 而一份全是下限的报告会把配置问题伪装成上游问题。
"""

# ruff: noqa: T201  这是命令行脚本, stdout/stderr 就是它的输出通道(与 run.py 同)。

from __future__ import annotations

import argparse
import datetime as _dt
import sys

from cost import PricingError, collect, load_pricing, render_cost_section


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HaiTun 成本报告 (读 metrics jsonl, 按带版本号的单价表算钱)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=1, help="往回算几天, 含今天 (默认 1)")
    group.add_argument("--date", help="只算这一天, YYYY-MM-DD")
    group.add_argument("--all", action="store_true", help="算 jsonl 里存在的全部天")
    parser.add_argument("--pricing", help="指名单价表版本, 例 v1-2026-09-01; 省略则按生效日期选最新一版")
    parser.add_argument("--session-limit", type=int, default=10, help="按会话一节最多列几行 (默认 10)")
    return parser.parse_args(argv)


def _days_of(args: argparse.Namespace) -> list[_dt.date] | None:
    """要算哪些天。`None` 意味着不限天(读到多少算多少)。"""
    if args.all:
        return None
    if args.date:
        return [_dt.date.fromisoformat(args.date)]
    today = _dt.date.today()
    return [today - _dt.timedelta(days=i) for i in range(max(1, args.days))]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        pricing = load_pricing(version=args.pricing)
    except PricingError as exc:
        # 往 stderr 且非零退出: cron-wrap 会把 stderr 尾部塞进「生成失败」通知。
        print(f"单价表读不出来: {exc}", file=sys.stderr)
        return 2

    totals = collect(days=_days_of(args), pricing=pricing)
    print(render_cost_section(totals, session_limit=args.session_limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
