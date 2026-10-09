"""日线回填。

设计要点：
- 断点续传：每批写 checkpoint（data/cache/checkpoints/daily.json），挂了从断点继续
- 看门狗：BaoStock 静默停，子进程超时就杀；整批超时自动缩批重试
- 血缘：source / ingested_at / data_version 必填

backfill_pool 是核心逐批回填循环（T3 重构）：
- pool 元素 (symbol, end_date)：每只自己的 end（退市股被上游截断）
- 同批内 end 不同 → 按 end 分组拉取（daily_bars 只接受单一 end）
- 批内再按标的类别分流：ETF/LOF 走 etf_daily 能力的源 + etf_daily_bars
  （见 resolve_ingest_source —— 盲取链头会让整个基金段零行）
- TimeoutError → 缩到 SUB_BATCH 再试；质量门禁 fatal → 拦整组不入湖
- 连续 EARLY_STOP_BATCHES 批全失败 → 早停，避免对挂掉的数据源空转
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from lquant.core.config import get_settings
from lquant.core.errors import DataQualityError
from lquant.core.types import now_cn, parse_symbol, today_cn
from lquant.data.capability import Capability
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.parquet import write_daily

_CP_NAME = "daily"
BATCH = 200
SUB_BATCH = 20
EARLY_STOP_BATCHES = 10

# 取数方法名（provider 侧）：基金段优先 etf_daily_bars，指数段优先
# index_daily_bars，其余走 daily_bars
_DAILY_METHOD = "daily_bars"
_FUND_METHOD = "etf_daily_bars"
_INDEX_METHOD = "index_daily_bars"
_FUND_TYPES = ("etf", "lof")

ProgressFn = Callable[[dict], None]


def _is_fund(symbol: str) -> bool:
    """ETF/LOF 判定（代码段规则，与 security 表口径一致，不查库）。"""
    try:
        return parse_symbol(symbol).sec_type.value in _FUND_TYPES
    except Exception:  # noqa: BLE001 - 解析不了的代码按非基金处理
        return False


def _is_index(symbol: str) -> bool:
    """指数判定（代码段规则，与 ``Symbol.sec_type`` 同一真源，不查库）。

    沪市 ``000xxx`` 全段 + 深市 ``399xxx`` 是指数（``000001.SH`` 上证指数与
    ``000001.SZ`` 平安银行同名不同指 —— 必须带交易所后缀判定）。
    """
    try:
        return parse_symbol(symbol).sec_type.value == "index"
    except Exception:  # noqa: BLE001 - 解析不了的代码按非指数处理
        return False


def resolve_ingest_source(*, fund: bool, index: bool = False,
                          provider=None) -> tuple[object, str]:
    """解析该标的类别的**实际取数源**与取数方法（不盲取链头）。

    为什么不能一律用 ``chain.providers[0]``：各源对「日线」的覆盖面不同 ——
    tushare 的 pro.daily 只有股票（ETF 在 fund_daily、指数在 index_daily），
    盲取链头时整个 ETF/LOF 段会逐日返回零行，被记成 empty_response（实测
    2026-09-17 任务：1582 只 ETF + 85 只 LOF 全段零行，ETF 湖停更 6 天）。
    指数同理：指数走 ``Capability.INDEX_DAILY`` 选源，方法优先
    ``index_daily_bars``（tushare 的 pro.index_daily）—— 用 ``daily_bars``
    取指数在 tushare 上必然零行。基金段因此按 etf_daily 能力选源；源没有
    etf_daily_bars 时回落 daily_bars（baostock 没有 etf_daily_bars 但
    daily_bars 能取 ETF —— 注意它只覆盖近端：实测 510300.SH / 159915.SZ
    在 2026-03 有行、2024-06 零行）。

    provider 显式注入（测试/单源场景）时原样返回，不做能力路由。
    """
    if provider is not None:
        if index:
            return provider, _INDEX_METHOD if hasattr(provider, _INDEX_METHOD) \
                else _DAILY_METHOD
        return provider, _FUND_METHOD if fund else _DAILY_METHOD

    from lquant.data.providers import get_provider

    chain = get_provider()
    if not hasattr(chain, "providers"):
        if index:
            return chain, (_INDEX_METHOD if hasattr(chain, _INDEX_METHOD)
                           else _DAILY_METHOD)
        return chain, _DAILY_METHOD
    want = (Capability.INDEX_DAILY if index
            else Capability.ETF_DAILY if fund
            else Capability.DAILY)
    for p in chain.providers:
        if not p.has(want):
            continue
        if index:
            method = _INDEX_METHOD if hasattr(p, _INDEX_METHOD) else _DAILY_METHOD
        else:
            method = _FUND_METHOD if fund and hasattr(p, _FUND_METHOD) else _DAILY_METHOD
        return p, method
    return chain.providers[0], _DAILY_METHOD


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
        provider: 注入 provider（测试/单源场景）。注入后整组走它、不做类别
            路由；缺省按标的类别从链中解析实际取数源（见 resolve_ingest_source）
        batch_size: 每批标的数
        cp_name: checkpoint 名（默认 "daily" 哨兵池；任务执行器传 f"daily:{task_id}"
            隔离记账，避免旧 daily cp 被任务跑满导致每日增量空转）
        cancel_check: 协作式取消探针 —— 每批开始前轮询，返回 True 提前收尾。
            None = 不取消（默认，旧调用方零改动）。

    Returns:
        {"done": int, "failed": [{"symbol","reason"}...], "rows": int,
         "early_stopped": bool, "canceled": bool, "unprocessed": [symbol...]}
        `unprocessed` = 因取消/提前停止**从未尝试**的标的。调用方必须用它把
        这些标的排除在 `cp.mark()` 之外 —— 否则「没跑」被记成「跑完了」，
        后续 retry 会因为断点命中而报 ok，留下永久静默空洞（审计 P0-2）。
    """
    from loguru import logger

    cp = Checkpoint(cp_name)

    todo = [(s, e) for s, e in pool if s not in cp.done]
    total = len(todo)
    if not todo:
        return {"done": 0, "failed": [], "rows": 0, "early_stopped": False,
                "canceled": False, "unprocessed": []}
    # 空跑不覆盖 meta（end=None 时避免抹掉上次记录）
    cp.set_meta(start=str(start), end=str(end) if end else None)

    explicit_provider = provider is not None

    done = 0
    rows = 0
    failed: list[dict] = []
    consecutive_full_failures = 0
    stopped = False
    canceled = False
    attempted: set[str] = set()
    #: 每个交易日实际写入的标的数（跨批次累加）—— 日级完整性门禁的分母是
    #: 「本次应写标的数」total，见 quality/coverage.py::day_completeness
    per_day: dict[date, int] = {}

    for i in range(0, total, batch_size):
        if cancel_check is not None and cancel_check():
            canceled = True
            break
        chunk = todo[i : i + batch_size]
        # 进入本批即算「尝试过」：无论成功失败，后续都由 failed/ok 记账决定
        # 是否标 done；只有从未进入的尾部才是 unprocessed。
        attempted.update(s for s, _ in chunk)
        batch_failed: dict[str, str] = {}
        batch_rows = 0
        for end_d, group in _by_end(chunk):
            if end_d < start:
                # 退市截断后窗口倒挂（delist < 窗口 start）：该股在窗口内无
                # 交易日，视为完成（0 行），不算 empty_response 失败
                continue
            # 显式注入 provider（测试/单源）时整组走它；否则按标的类别分流，
            # 基金段路由到支持 etf_daily 的源、指数段路由到 index_daily 能力的源
            # （盲取链头会让基金/指数段全零行）
            buckets = ([("all", group)] if explicit_provider
                       else _by_class(group))
            for cls, syms in buckets:
                src, method = resolve_ingest_source(
                    fund=(cls == "fund"), index=(cls == "index"), provider=provider)
                df, grp_failed = _pull_group(src, syms, start, end_d, method)
                batch_rows += len(df)
                batch_failed.update(grp_failed)
                # 源站静默丢标的（无异常但零行）此前被当成功标 done —— 整段
                # 历史缺失且不可发现。显式标 empty_response 并带上**实际服务
                # 源名**：源不具备该标的类别时（如 tushare 的 pro.daily 不含
                # ETF）一眼可辨，不必再逐层排查。
                got = set(df["symbol"].to_list()) if len(df) else set()
                for sym in syms:
                    if sym not in got and sym not in batch_failed:
                        batch_failed[sym] = (
                            f"empty_response: 源({_provider_source(src)})零行返回")
                if len(df):
                    try:
                        if cls == "index":
                            # int(... or 0)：_write_index_bars 可能被替换/吞异常
                            # 返回 None —— 校验必须对「假成功」稳健
                            n_written = int(_write_index_bars(df) or 0)
                            # market.persist 会吞掉 DuckDB 写库异常（返回 0）：
                            # 不检查返回值的话，指数数据没落库却被标 done，
                            # 断点/coverage 都不会再补 —— 基准序列静默停更。
                            if n_written < len(df):
                                raise RuntimeError(
                                    f"index_daily 写入不完整（{n_written}/{len(df)} 行，"
                                    "写库异常被 persist 吞掉）")
                        else:
                            write_daily(_stamp(df, _provider_source(src)))
                            # 日级完整性记账：按**本次应写标的数**当分母，
                            # 不看 security 表（那张表本身可能不齐）。见
                            # quality/coverage.py::day_completeness。
                            # 列名防御：注入的 provider 可能给的是源列名
                            # （映射在更上层），没有 trade_date 就跳过记账。
                            if "trade_date" in df.columns:
                                for r in (df.group_by("trade_date").len()
                                          .iter_rows(named=True)):
                                    d0 = r["trade_date"]
                                    per_day[d0] = per_day.get(d0, 0) + int(r["len"])
                    except DataQualityError as e:
                        # 质量门禁 fatal 拦批：不入湖，标失败留待重试（H2）
                        logger.error(f"质量门禁拦截（fatal，不入湖）: {e}")
                        for sym in syms:
                            batch_failed.setdefault(sym, f"quality: {e}")
                    except RuntimeError as e:
                        logger.error(str(e))
                        for sym in syms:
                            batch_failed.setdefault(sym, f"index_write: {e}")
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
    # ---- 日级完整性门禁 ----
    # 分级：整日缺失/大面积缺口 → error；最新交易日的缺口 → tip（最典型的形态是
    # `end=today` 只写了一半，而「任务报 ok」让人以为这天已经齐了）。
    # 注意这里**不修改** checkpoint（标的级覆盖语义仍然正确），只是把
    # 「这一天到底齐没齐」显式暴露出来，供调用方决定要不要推进消费水位。
    day_gaps: list[dict] = []
    day_status = "ok"
    if per_day:
        from lquant.data.quality.coverage import day_completeness, day_gap_issues
        try:
            gaps = day_completeness(per_day, total)
            day_gaps = [{"trade_date": str(g.trade_date), "symbols": g.symbols,
                         "expected": g.expected, "ratio": round(g.ratio, 4),
                         "kind": g.kind} for g in gaps]
            if gaps:
                day_status = "incomplete"
                logger.warning(
                    "日级完整性缺口 %d 天（应写 %d 只）：%s",
                    len(gaps), total, "；".join(g.detail() for g in gaps[:5]))
                try:
                    from lquant.data.quality.issues import save_issues

                    save_issues(day_gap_issues(gaps))
                except Exception as e:  # noqa: BLE001 - 落库失败不影响回填结果
                    logger.warning(f"完整性缺口落库失败: {e}")
        except Exception as e:  # noqa: BLE001 - 门禁自身异常不阻断回填
            logger.warning(f"日级完整性门禁执行失败: {e}")

    return {
        "done": done,
        "failed": failed,
        "rows": rows,
        "early_stopped": stopped,
        "canceled": canceled,
        # 从未尝试的尾部：调用方绝不能把它们标 done（审计 P0-2）
        "unprocessed": [s for s, _ in todo if s not in attempted],
        # 日级完整性（"ok" / "incomplete"）与逐日缺口明细
        "day_status": day_status,
        "day_gaps": day_gaps,
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


def _by_class(chunk: list[str]) -> list[tuple[str, list[str]]]:
    """同组内按标的类别分流（基金 / 指数 / 其余），保持各自首次出现顺序。

    只分三类即可：基金（ETF/LOF，可能需要 etf_daily 通道）、指数
    （index_daily 通道，写入 DuckDB ``index_daily`` 表而非日线湖）、
    其余（股票，走 daily 通道）。

    指数**不入日线 parquet 湖**：点位超价格护栏、量纲断言不成立
    （详见 ``SecurityRepo.active_symbols``）；但混入的指数必须被识别出来，
    否则会走 ``daily_bars`` 拉零行、或被质量门禁拦成整批失败。
    """
    fund: list[str] = []
    index: list[str] = []
    other: list[str] = []
    for sym in chunk:
        if _is_index(sym):
            index.append(sym)
        elif _is_fund(sym):
            fund.append(sym)
        else:
            other.append(sym)
    out: list[tuple[str, list[str]]] = []
    if other:
        out.append(("other", other))
    if fund:
        out.append(("fund", fund))
    if index:
        out.append(("index", index))
    return out


def _write_index_bars(df: pl.DataFrame) -> int:
    """指数序列写入 DuckDB ``index_daily``（回测基准 / 大盘看板的数据源）。

    与日线湖的差别是有意的：指数是**点位**不是价格，不进 parquet 湖、不走
    ``gate_daily``（价格护栏对点位无意义）。列对齐 ``TABLE_COLUMNS["index_daily"]``，
    缺列由 market.persist 的 upsert 处理。
    """
    from loguru import logger

    if not len(df):
        return 0
    from lquant.market.collectors.index_daily import INDEX_POOL
    from lquant.market.scheduler import persist

    keep_cols = ["trade_date", "symbol", "open", "high", "low", "close",
                 "pre_close", "volume", "amount"]
    cols = [c for c in keep_cols if c in df.columns]
    out = df.select(cols).with_columns(
        pl.col("symbol").replace_strict(INDEX_POOL, default=None).alias("name"),
        collected_at=pl.lit(now_cn(), dtype=pl.Datetime("us")),
    )
    counts = persist({"index_daily": out})
    n = int(counts.get("index_daily", 0))
    logger.info(f"指数日线入库 index_daily: {n} 行")
    return n


def _pull_group(
    provider, syms: list[str], start: date, end_d: date | None,
    method: str = _DAILY_METHOD,
) -> tuple[pl.DataFrame, dict[str, str]]:
    """拉一组（同 end）：RuntimeError → 整组失败；TimeoutError → 缩批重试。

    method：取数方法名（daily_bars / etf_daily_bars）。源未实现该方法时回落
    daily_bars —— 能力声明与实际方法不必一一对应（baostock 声明 etf_daily
    但 ETF 走 daily_bars 即可）。
    """
    from loguru import logger

    fn = getattr(provider, method, None) or provider.daily_bars

    try:
        df = fn(syms, start, end_d)
    except TimeoutError as e:
        # 整批挂起 → 缩到 SUB_BATCH 只再试，把挂住的损失压到最小
        logger.warning(f"批次超时，缩批重试（{len(syms)} 只 → {SUB_BATCH} 只）: {e}")
        frames: list[pl.DataFrame] = []
        failed: dict[str, str] = {}
        for j in range(0, len(syms), SUB_BATCH):
            sub = syms[j : j + SUB_BATCH]
            try:
                frames.append(fn(sub, start, end_d))
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


def _window_cp_name(start_d: date, end_d: date, full: bool) -> str:
    """断点名纳入请求窗口与池型——不同窗口不共享断点(防静默 done 0)。"""
    return f"daily:{start_d.isoformat()}:{end_d.isoformat()}:full={full}"


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
    result = backfill_pool(pool, start_d, end=end_d, batch_size=batch,
                           cp_name=_window_cp_name(start_d, end_d, full))
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

    快照哨兵契约（与 ``quality/integrity.py::partition_is_snapshot`` 对齐）：
    ``_stamp`` 是**盘后权威批量行**的唯一盖章入口，所以在这里**显式**写
    ``quote_ts = NULL``。为什么必须显式而不是依赖「列缺失自然为 null」：
    integrity 判定「整分区缺列」为 **UNKNOWN** 而不是 **AUTHORITATIVE** ——
    缺列 = 没有证据，会被当成可疑分区永远进不了复用/修复流程；而显式 NULL
    才是「这批行是权威批量拉取的」的正面证据。反过来说，谁也**不能**在这里
    给批量行编造采集时刻：那等于把盘后权威行谎称成盘中快照，哨兵语义反了。
    """
    from lquant.data import lineage
    from lquant.data.quality.pipeline import gate_daily

    version = lineage.new_version()
    lineage.register(version, "daily_bar", row_count=len(df))
    stamped = df.with_columns(
        source=pl.lit(source),
        ingested_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit(version),
        # 盘后批量权威行 = 显式 NULL（哨兵契约：非空才是采集时刻）
        quote_ts=pl.lit(None, dtype=pl.Int64),
    )
    out, _issues = gate_daily(stamped, data_version=version)
    return out
