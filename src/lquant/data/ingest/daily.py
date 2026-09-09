"""日线回填。

设计要点：
- 断点续传：每批写 checkpoint（data/cache/checkpoints/daily.json），挂了从断点继续
- 看门狗：BaoStock 静默挂起，子进程超时就杀；整批超时自动缩批重试
- 血缘：source / ingested_at / data_version 必填
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.config import get_settings
from lquant.core.errors import DataQualityError
from lquant.core.types import now_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.parquet import write_daily

_CP_NAME = "daily"


def backfill_daily(
    full: bool = False,
    start: str = "2016-01-01",
    end: str | None = None,
    concurrency: int = 4,
) -> int:
    from loguru import logger

    from lquant.data.providers import get_provider
    from lquant.data.store.catalog import SecurityRepo

    s = get_settings()
    end_d = date.fromisoformat(end) if end else date.today()
    start_d = date.fromisoformat(start)

    symbols = SecurityRepo().active_symbols()
    if not full:
        # 哨兵池：大中小盘 + ETF，快速验证链路
        symbols = symbols[:200]
    if not symbols:
        raise RuntimeError(
            "security 表为空 —— 先跑 `lq data reference` 建标的清单（没有日历与标的，日线无从谈起）"
        )
    logger.info(f"回填 {len(symbols)} 只标的 {start_d} ~ {end_d}")

    cp = Checkpoint(_CP_NAME)
    todo = cp.remaining(symbols)
    if not todo:
        logger.info("全部标的已完成（checkpoint 命中），删除 data/cache/checkpoints/daily.json 可强制重跑")
        return 0

    provider = get_provider()
    target = provider.providers[0] if hasattr(provider, "providers") else provider
    batch = s.ingest_concurrency or concurrency
    done = 0
    for i in range(0, len(todo), 200):
        chunk = todo[i : i + 200]
        failed: list[str] = []
        try:
            df = target.daily_bars(chunk, start_d, end_d)
        except TimeoutError as e:
            # 整批挂起 → 缩到 20 只再试，把挂住的损失压到最小
            logger.warning(f"批次 {i} 超时，缩批重试: {e}")
            df = pl.DataFrame()
            for j in range(0, len(chunk), 20):
                sub = chunk[j : j + 20]
                try:
                    sub_df = target.daily_bars(sub, start_d, end_d)
                except (TimeoutError, RuntimeError) as e2:
                    logger.warning(f"  缩批 {j} 仍失败，跳过 {len(sub)} 只: {e2}")
                    failed.extend(sub)
                    continue
                if len(sub_df):
                    try:
                        write_daily(_stamp(sub_df))
                    except DataQualityError as e3:
                        # 质量门禁 fatal 只拦这一小批，不能让整个回填挂掉（H2）
                        logger.error(f"  缩批 {j} 质量门禁拦截（fatal，不入湖）: {e3}")
                        failed.extend(sub)
        except RuntimeError as e:
            logger.warning(f"批次 {i} 失败，跳过（下次重跑会重试）: {e}")
            continue
        except DataQualityError as e:
            # 质量门禁 fatal：留证据（issue 已落库），批次不入湖。
            # 只捕获这一类 —— Polars/IO 等程序性 bug 不该被伪装成数据问题（H7）
            logger.error(f"批次 {i} 质量门禁拦截（fatal，批次不入湖）: {e}")
            failed.extend(chunk)
            continue
        if len(df):
            write_daily(_stamp(df))
        # 只有"确认失败"的标的不写 checkpoint，下次重跑自动重试
        cp.mark([s for s in chunk if s not in set(failed)])
        done += len(chunk) - len(failed)
        logger.info(f"  进度 {done}/{len(todo)}（checkpoint 已写，本批失败 {len(failed)}）")
    return done


def _stamp(df: pl.DataFrame) -> pl.DataFrame:
    """补血缘字段 + 质量门禁（记录级断言，fatal 阻断入湖）。

    data_version 用 lineage.new_version()（YYYYMMDD.n）并登记到
    data_version 表 —— 湖里的 data_version 必须能对上血缘登记，
    否则因子缓存的失效锚点是死的（§3.6）。
    fatal 抛 DataQualityError 会让整批不入湖 —— 这是设计行为
    （§3.8.6：fatal 阻断下游，回滚到上一 data_version）。
    warn 只打 quality_flags 标，批次照常落地。
    """
    from lquant.data import lineage
    from lquant.data.quality.pipeline import gate_daily

    version = lineage.new_version()
    lineage.register(version, "daily_bar", row_count=len(df))
    stamped = df.with_columns(
        source=pl.lit("baostock"),
        ingested_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit(version),
    )
    out, _issues = gate_daily(stamped, data_version=version)
    return out
