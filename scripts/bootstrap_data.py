#!/usr/bin/env python3
"""全市场日线回填。

关键：BaoStock 批量连续请求会静默挂起（实测 12 分钟无返回，
socket.setdefaulttimeout 也救不回来），所以必须走子进程看门狗 + 断点续传。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lquant.core.logging import setup_logging  # noqa: E402


def main() -> int:
    import click

    @click.command()
    @click.option("--full", is_flag=True, help="全市场；不加则只跑哨兵池")
    @click.option("--start", default="2016-01-01")
    @click.option("--end", default=None)
    @click.option("--concurrency", default=4)
    def run(full: bool, start: str, end: str | None, concurrency: int) -> None:
        setup_logging()
        from lquant.data.ingest.daily import backfill_daily

        n = backfill_daily(full=full, start=start, end=end, concurrency=concurrency)
        click.echo(f"done: {n} symbols")

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
