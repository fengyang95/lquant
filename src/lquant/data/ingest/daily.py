"""日线回填。

设计要点：
- 断点续传：每批写 checkpoint（data/cache/checkpoints/daily.json），挂了从断点继续
- 看门狗：BaoStock 静默停，子进程超时就杀；整批超时自动缩批重试
- 血缘：source / ingested_at / data_version 必填

backfill_pool 是核心逐批回填循环（T3 重构）：
- pool 元素 (symbol, end_date)：每只自己的 end（退市股被上游截断）
- 同批内 end 不同 → 按 end 分组拉取（daily_bars 只接受单一 end）
- TimeoutError → 缩到 SUB_BATCH 再试；质量门禁 fatal → 拦整组不入湖
- 连续 EARLY_STOP_BATCHES 批全失败 → 早停，避免对挂掉的数据源空转
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from lquant.core.config import get_settings
from lquant.core.errors import DataQualityError
from lquant.core.types import now_cn, today_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.parquet import write_daily

_CP_NAME = "daily"
BATCH = 200
SUB_BATCH = 20
EARLY_STOP_BATCHES = 10

ProgressFn = Callable[[dict], None]


def backfill_pool(
    pool: list[tuple[str, date]],
    start: date,
    end: date | None = None,
    on_progress: ProgressFn | None = None,
    *,
    provider=None,
    batch_size: int = BATCH,
    cp_name: str = _CP_NAME,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """逐批流式回填日线池。

    Args:
        pool: [(symbol, end_date), ...]，每只自己的 end（退市股被上游截断）
        start: 起始日
        end: 兼容参数，仅记入 checkpoint meta（pool 已带逐只 end）
        on_progress: 每批回调
            on_progress({"done","total","failed","rows","early_stopped"})，
            回调异常不中断回填
        provider: 注入 provider（测试用）；默认 get_provider() 并解包 FallbackProvider
        batch_size: 每批标的数
        cp_name: checkpoint 名（默认 "daily" 哨兵池；任务执行器传 f"daily:{task_id}"
            隔离记账，避免旧 daily cp 被任务跑满导致每日增量空转）
        cancel_check: 协作式取消探针 —— 每批开始前轮询，返回 True 提前收尾。
            None = 不取消（默认，旧调用方零改动）。

    Returns:
        {"done": int, "failed": [{"symbol","reason"}...], "rows": int,
         "early_stopped": bool}
    """
    from loguru import logger

    cp = Checkpoint(cp_name)

    todo = [(s, e) for s, e in pool if s not in cp.done]
    total = len(todo)
    if not todo:
        return {"done": 0, "failed": [], "rows": 0, "early_stopped": False,
                "canceled": False}
    # 空跑不覆盖 meta（end=None 时避免抹掉上次记录）
    cp.set_meta(start=str(start), end=str(end) if end else None)

    if provider is None:
        from lquant.data.providers import get_provider

        provider = get_provider()
        provider = provider.providers[0] if hasattr(provider, "providers") else provider

    done = 0
    rows = 0
    failed: list[dict] = []
    consecutive_full_failures = 0
    stopped = False
    canceled = False

    for i in range(0, total, batch_size):
        if cancel_check is not None and cancel_check():
            canceled = True
            break
        chunk = todo[i : i + batch_size]
        batch_failed: dict[str, str] = {}
        batch_rows = 0
        for end_d, syms in _by_end(chunk):
            df, grp_failed = _pull_group(provider, syms, start, end_d)
            batch_rows += len(df)
            batch_failed.update(grp_failed)
            if len(df):
                try:
                    write_daily(_stamp(df, _provider_source(provider)))
                except DataQualityError as e:
                    # 质量门禁 fatal 拦批：不入湖，标失败留待重试（H2）
                    logger.error(f"质量门禁拦截（fatal，不入湖）: {e}")
                    for sym in syms:
                        batch_failed.setdefault(sym, f"quality: {e}")
        ok = [s for s, _ in chunk if s not in batch_failed]
        cp.mark(ok)
        done += len(ok)
        rows += batch_rows
        failed.extend({"symbol": s, "reason": batch_failed[s]} for s in batch_failed)
        logger.info(f"  进度 {done}/{total}（本批失败 {len(batch_failed)}）")
        _notify(
            on_progress,
            {
                "done": done,
                "total": total,
                "failed": list(failed),  # 快照：消费方存帧不被后续批次追溯改写
                "rows": rows,
                "early_stopped": False,
            },
        )
        if batch_failed and not ok:
            consecutive_full_failures += 1
            if consecutive_full_failures >= EARLY_STOP_BATCHES:
                logger.error(
                    f"连续 {EARLY_STOP_BATCHES} 批全失败，提前停止"
                    f"（已失败 {len(failed)} 只，可用 retry 重试）"
                )
                stopped = True
                break
        else:
            consecutive_full_failures = 0

    if stopped:
        _notify(
            on_progress,
            {
                "done": done,
                "total": total,
                "failed": list(failed),  # 快照
                "rows": rows,
                "early_stopped": True,
            },
        )
    return {
        "done": done,
        "failed": failed,
        "rows": rows,
        "early_stopped": stopped,
        "canceled": canceled,
    }


def _by_end(chunk: list[tuple[str, date]]) -> list[tuple[date, list[str]]]:
    """同批内按 end_date 分组（保持首次出现顺序）。"""
    order: list[date] = []
    groups: dict[date, list[str]] = {}
    for sym, end_d in chunk:
        if end_d not in groups:
            order.append(end_d)
            groups[end_d] = []
        groups[end_d].append(sym)
    return [(d, groups[d]) for d in order]


def _pull_group(
    provider, syms: list[str], start: date, end_d: date | None
) -> tuple[pl.DataFrame, dict[str, str]]:
    """拉一组（同 end）：RuntimeError → 整组失败；TimeoutError → 缩批重试。"""
    from loguru import logger

    try:
        df = provider.daily_bars(syms, start, end_d)
    except TimeoutError as e:
        # 整批挂起 → 缩到 SUB_BATCH 只再试，把挂住的损失压到最小
        logger.warning(f"批次超时，缩批重试（{len(syms)} 只 → {SUB_BATCH} 只）: {e}")
        frames: list[pl.DataFrame] = []
        failed: dict[str, str] = {}
        for j in range(0, len(syms), SUB_BATCH):
            sub = syms[j : j + SUB_BATCH]
            try:
                frames.append(provider.daily_bars(sub, start, end_d))
            except (TimeoutError, RuntimeError) as e2:
                logger.warning(f"  缩批 {j} 仍失败，跳过 {len(sub)} 只: {e2}")
                for sym in sub:
                    failed[sym] = f"{type(e2).__name__}: {e2}"
        df = pl.concat(frames) if frames else pl.DataFrame()
        return df, failed
    except RuntimeError as e:
        logger.warning(f"批次失败，跳过（重跑会重试）: {e}")
        return pl.DataFrame(), {s: str(e) for s in syms}
    return df, {}


def _notify(cb: ProgressFn | None, frame: dict) -> None:
    """调用进度回调，异常吞掉不中断回填。"""
    if cb is None:
        return
    try:
        cb(frame)
    except Exception as e:  # noqa: BLE001
        from loguru import logger

        logger.warning(f"on_progress 回调异常（忽略）: {e}")


def backfill_daily(
    full: bool = False,
    start: str = "2016-01-01",
    end: str | None = None,
    concurrency: int = 4,
) -> int:
    """兼容签名：转调 backfill_pool（哨兵池 full=False 取前 200 只）。"""
    from loguru import logger

    from lquant.data.store.catalog import SecurityRepo

    s = get_settings()
    end_d = date.fromisoformat(end) if end else today_cn()
    start_d = date.fromisoformat(start)

    # 指数不入日线湖：点位超价格护栏、量纲断言不成立（详见 active_symbols docstring）
    symbols = SecurityRepo().active_symbols(exclude_index=True)
    if not full:
        # 哨兵池：大中小盘 + ETF，快速验证链路
        symbols = symbols[:200]
    if not symbols:
        raise RuntimeError(
            "security 表为空 —— 先跑 `lq data reference` 建标的清单（没有日历与标的，日线无从谈起）"
        )
    logger.info(f"回填 {len(symbols)} 只标的 {start_d} ~ {end_d}")

    batch = s.ingest_concurrency or concurrency
    pool = [(sym, end_d) for sym in symbols]
    result = backfill_pool(pool, start_d, end=end_d, batch_size=batch)
    return result["done"]


def _provider_source(provider) -> str:
    """实际使用的源名（血缘 source 字段）：优先 source key，回落 name。"""
    from lquant.data.base import source_name

    return source_name(provider)


def _stamp(df: pl.DataFrame, source: str = "baostock") -> pl.DataFrame:
    """补血缘字段 + 质量门禁（记录级断言，fatal 阻断入湖）。

    source 用实际使用的 Provider 标注（provider.source 回落 provider.name），
    fallback 到其他源时血缘不能标错 —— 湖里的 source 是跨源对拍的锚点。

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
        source=pl.lit(source),
        ingested_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit(version),
    )
    out, _issues = gate_daily(stamped, data_version=version)
    return out
