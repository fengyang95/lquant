"""扩展数据表体系（BYO data → 自动因子/信号）。

把用户自有另类数据（热度、板块归属、舆情、景气度）变成一等公民：
表配置 → parquet 分区 → DuckDB 视图 → 自动因子注册（数值）/ 信号条件通道（字符串）。

三条硬契约（都由框架层兜住，不依赖调用方自觉）：

1. **PIT 边界**（:mod:`~lquant.data.ext.pit`）：timeseries 按 ``(symbol, date)``
   精确对齐，缺分区为 null、绝不前视填充；snapshot 只在单日帧注入，多日历史
   帧跳过。
2. **日期防污染**（:mod:`~lquant.data.ext.ingest`）：HTTP 回补时响应行日期
   必须等于请求日期，不符则拒绝写入该分区（真实存在忽略 ``?date=`` 的接口）。
3. **写入即失效**：任何写入都会清配置缓存与因子/帧缓存（:func:`invalidate_ext_caches`）。
"""

from __future__ import annotations

from lquant.data.ext.duckdb import drop_view, sync_all, sync_view, view_name
from lquant.data.ext.ingest import (
    MAX_BACKFILL_DAYS,
    ExtDateMismatch,
    backfill,
    build_url,
    fetch_rows,
    import_file,
    parse_upload,
    pull_day,
    write_json_rows,
)
from lquant.data.ext.models import (
    FIELD_DTYPES,
    NUMERIC_DTYPES,
    ExtConfig,
    ExtConfigError,
    ExtField,
    PullConfig,
)
from lquant.data.ext.pit import attach_ext_columns, invalidate_frame_cache
from lquant.data.ext.query import query_rows, query_values
from lquant.data.ext.schema import (
    clean_column_names,
    ensure_utf8_csv,
    ext_column_name,
    infer_fields,
    normalize_symbol,
)
from lquant.data.ext.signals import apply_signals, signal_field_catalog, signal_fields
from lquant.data.ext.storage import (
    delete_table_data,
    existing_dates,
    read_table,
    write_frame,
)
from lquant.data.ext.store import (
    ExtConfigStore,
    ext_root,
    invalidate_ext_caches,
    resolve_data_root,
    table_dir,
)

__all__ = [
    "FIELD_DTYPES",
    "MAX_BACKFILL_DAYS",
    "NUMERIC_DTYPES",
    "ExtConfig",
    "ExtConfigError",
    "ExtConfigStore",
    "ExtDateMismatch",
    "ExtField",
    "PullConfig",
    "apply_signals",
    "attach_ext_columns",
    "backfill",
    "build_url",
    "clean_column_names",
    "delete_table_data",
    "drop_view",
    "ensure_utf8_csv",
    "existing_dates",
    "ext_column_name",
    "ext_root",
    "fetch_rows",
    "import_file",
    "infer_fields",
    "invalidate_ext_caches",
    "invalidate_frame_cache",
    "normalize_symbol",
    "parse_upload",
    "pull_day",
    "query_rows",
    "query_values",
    "read_table",
    "resolve_data_root",
    "signal_field_catalog",
    "signal_fields",
    "sync_all",
    "sync_view",
    "table_dir",
    "view_name",
    "write_frame",
    "write_json_rows",
]
