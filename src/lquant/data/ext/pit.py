"""PIT（Point-In-Time）对齐：把扩展表字段注入日线面板，并封死未来函数风险。

这是整个扩展数据体系**最需要防错**的一层。两条模式的口径：

``timeseries``
    按 ``(symbol, date)`` **精确等值**对齐。某一天没有扩展分区 → 该列是 null，
    **绝不前视填充**（ffill 会让「未来才知道的热度」出现在过去，是回测里最
    隐蔽的收益来源）。缺失就是缺失，由消费方显式决定丢样本还是填中性值。

``snapshot``
    只代表「最新值」。仅当面板是**当日/单日帧**时才注入；多日历史帧一律跳过。
    否则回看历史时每一行都会拿到「今天的最新值」——IC 会凭空变好，且没有任何
    报错。这是必须由框架层兜住的契约，不能指望每个调用方都记得。

市场级表（``market_level``，无 symbol 列）按日期 join，不按标的 —— 它是择时/
情绪序列，一行代表全市场当日状态。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
from loguru import logger

from lquant.data.ext.models import ExtConfig
from lquant.data.ext.schema import ext_column_name
from lquant.data.ext.storage import existing_dates, partition_path, snapshot_path
from lquant.data.ext.store import ExtConfigStore

#: 帧缓存：(数据根, 表 id, 模式) -> (签名, 帧)。
#: 扩展表在评价/挖掘里被反复 join，每次重读 parquet 纯属浪费；签名由分区
#: 文件的 (mtime, size) 组成，写入后必然变化 → invalidate 之外还有一道自愈。
_FRAME_CACHE: dict[tuple[str, str, str], tuple[tuple, pl.DataFrame]] = {}


def invalidate_frame_cache(data_root: str | Path | None = None) -> None:
    """清帧缓存（写入/配置变更后由 store.invalidate_ext_caches 调用）。"""
    if data_root is None:
        _FRAME_CACHE.clear()
        return
    root = str(Path(data_root))
    for key in [k for k in _FRAME_CACHE if k[0] == root]:
        _FRAME_CACHE.pop(key, None)


def _signature(config: ExtConfig, data_root: str | Path | None) -> tuple:
    from lquant.data.ext.store import table_dir

    if config.mode == "snapshot":
        p = snapshot_path(config, data_root)
        if not p.exists():
            return ()
        st = p.stat()
        return (("snapshot", st.st_mtime_ns, st.st_size),)
    sig = []
    for d in existing_dates(config, data_root):
        p = table_dir(config.id, data_root) / f"date={d}" / "part.parquet"
        st = p.stat()
        sig.append((d, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _ext_frame(config: ExtConfig, data_root: str | Path | None) -> pl.DataFrame:
    """整张扩展表的「信号列」投影：ext_{id}_{field} + 标的 + 日期。

    数值列统一 Float64、字符串列 Utf8 —— 与因子/信号两条通道各自的消费口径
    一致（见 factors/ext_bridge）。
    """
    sig = _signature(config, data_root)
    if not sig:
        return pl.DataFrame()
    cache_key = (str(Path(data_root) if data_root is not None else ""), config.id, config.mode)
    cached = _FRAME_CACHE.get(cache_key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    fields = [f for f in config.fields if f.dtype in ("int", "float", "string")]
    exprs: list[pl.Expr] = []
    if config.symbol_field:
        exprs.append(pl.col(config.symbol_field).cast(pl.Utf8).alias("__ext_symbol"))
    if config.mode == "timeseries":
        exprs.append(pl.col(config.date_field).cast(pl.Date, strict=False).alias("__ext_date"))
    for f in fields:
        target = pl.Float64 if f.numeric else pl.Utf8
        exprs.append(pl.col(f.name).cast(target, strict=False).alias(ext_column_name(config.id, f.name)))

    parts: list[pl.DataFrame] = []
    if config.mode == "snapshot":
        raw = pl.read_parquet(snapshot_path(config, data_root))
        parts.append(raw.select(exprs).with_columns(pl.lit(None, dtype=pl.Date).alias("__ext_date")))
    else:
        for d in existing_dates(config, data_root):
            raw = pl.read_parquet(partition_path(config, _as_date(d), data_root))
            if config.date_field not in raw.columns:
                raw = raw.with_columns(pl.lit(d).str.to_date().alias(config.date_field))
            parts.append(raw.select(exprs))
    if not parts:
        return pl.DataFrame()
    frame = pl.concat(parts, how="diagonal_relaxed")
    keys = (["__ext_symbol"] if config.symbol_field else []) + ["__ext_date"]
    frame = frame.unique(subset=keys, keep="last")
    _FRAME_CACHE[cache_key] = (sig, frame)
    return frame


def _as_date(iso: str):
    from datetime import date

    return date.fromisoformat(iso)


def _panel_date_column(panel: pl.DataFrame, date_column: str | None) -> str | None:
    if date_column:
        return date_column if date_column in panel.columns else None
    for cand in ("trade_date", "date"):
        if cand in panel.columns:
            return cand
    return None


def attach_ext_columns(
    panel: pl.DataFrame,
    *,
    data_root: str | Path | None = None,
    include_snapshot: bool | None = None,
    date_column: str | None = None,
) -> pl.DataFrame:
    """把扩展字段 join 到面板上（无配置/无匹配时原样返回）。

    ``include_snapshot``:
      - ``None``（默认）= 自动判定：面板日期列只有 1 个取值（单日/当日帧）
        才注入快照。历史多日帧自动跳过。
      - ``True`` = 调用方明确声明这是当日帧（如盘中实时计算），强制注入。
      - ``False`` = 强制跳过（如回测里即便是单日也不许用快照）。
    """
    if panel.is_empty():
        return panel
    root = data_root
    configs = ExtConfigStore(root).load_all()
    if not configs:
        return panel

    pdate = _panel_date_column(panel, date_column)
    single_day = False
    if pdate is not None:
        n = panel.select(pl.col(pdate).n_unique()).item()
        single_day = n == 1
    else:
        logger.warning("扩展数据注入：面板没有日期列，timeseries 表一律跳过（无法 PIT 对齐）")

    out = panel
    for cfg in configs:
        try:
            if cfg.mode == "snapshot":
                if include_snapshot is False or (include_snapshot is None and not single_day):
                    continue
                if not cfg.symbol_field:
                    continue  # 市场级快照无法按标的 join，跳过（避免笛卡尔积）
                out = _attach_snapshot(out, cfg, root)
            else:
                if pdate is None:
                    continue
                out = _attach_timeseries(out, cfg, root, pdate)
        except Exception as e:  # noqa: BLE001 - 单表失败只跳过该表，不炸整帧
            logger.warning(f"扩展表 {cfg.id} 注入失败，跳过该表: {e}")
    return out


def _attach_timeseries(
    panel: pl.DataFrame, cfg: ExtConfig, root: str | Path | None, pdate: str
) -> pl.DataFrame:
    ext = _ext_frame(cfg, root)
    if ext.is_empty():
        return panel
    value_cols = [c for c in ext.columns if c.startswith(f"ext_{cfg.id}_")
                  and c not in panel.columns]
    if not value_cols:
        return panel
    join_cols = ["__ext_date"]
    left = panel.with_columns(pl.col(pdate).cast(pl.Date, strict=False).alias("__ext_date"))
    if cfg.symbol_field:
        if cfg.symbol_field not in panel.columns:
            return panel
        left = left.with_columns(
            pl.col(cfg.symbol_field).cast(pl.Utf8).alias("__ext_symbol")
        )
        join_cols = ["__ext_symbol", "__ext_date"]
    joined = left.join(
        ext.select([*join_cols, *value_cols]), on=join_cols, how="left"
    )
    # 左连接缺匹配 → null：**不做任何填充**。缺失就是缺失。
    return joined.drop(join_cols)


def _attach_snapshot(panel: pl.DataFrame, cfg: ExtConfig, root: str | Path | None) -> pl.DataFrame:
    ext = _ext_frame(cfg, root)
    if ext.is_empty():
        return panel
    if "__ext_symbol" not in ext.columns:
        return panel
    value_cols = [c for c in ext.columns if c.startswith(f"ext_{cfg.id}_")
                  and c not in panel.columns]
    if not value_cols:
        return panel
    return panel.join(
        ext.select(["__ext_symbol", *value_cols]),
        left_on=cfg.symbol_field, right_on="__ext_symbol", how="left",
    )
