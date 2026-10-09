"""盘后官方日线的「值级覆盖检测 + 自动修复」（FEATURE-IDEAS.md B1）。

**要防的事故不是「缺行」，而是「行数对、值错了」。** 参考实现注释里记录了
一次真实事故：某实时端点收盘后长期返回旧价，3392/5554 只股票的当日收盘价
与官方日线不符 —— 但**按行数校验完全识别不到**（行数是对的，值才是错的）。
这种分区一旦留在日线湖里，因子与回测会产生**看似合理**的静默错误：没有
任何一处会报错，结果只是系统性地偏一点。

因此本模块按四条互补的口径检查最近若干交易日的**当日分区**：

1. **快照哨兵**（:func:`partition_is_snapshot`）——写分区时随行带一个
   ``quote_ts``（报价时刻，epoch 毫秒，上海墙钟），用来区分「盘中实时落盘
   的分区」与「盘后权威历史」。哨兵**缺失时返回「未知」而不是「权威」** ——
   这正是事故里「停机后的盘中覆写分区被当成完整历史、永远不进修复」的成因。
2. **值级比对**（:func:`check_partition_values`）——当日 ``close`` 与**权威
   来源**逐 symbol 比较，差值**超过半个最小报价单位**（0.005 元）即判不一致。
   权威来源必须由调用方显式注入；**没有权威来源就如实说无法比对**，绝不拿
   湖里的分区跟它自己比（那样恒一致，是最坏的假阴性）。
3. **覆盖集比对**（:func:`check_partition_coverage`）——按**停牌过滤后的
   symbol 覆盖集**比对，而不是行数。正常剔除停牌会让分区少行，按行数比对
   会把健康分区反复误报（参考实现里这正是「每次管道都删除重算」的原因）。
4. **修复计划 + 执行**（:func:`plan_repair` / :func:`apply_repair`）——参考
   实现的做法是「删分区让增量重算把它们当新日期」。删除是**破坏性**动作，
   所以 :func:`apply_repair` 默认 ``dry_run=True`` 只报不改；真要执行必须显式
   传参，并在返回值里逐条报告实际删了什么。

口径来源：``/tmp/tick-stock-panel``（**MIT，可移植**）的
``backend/app/jobs/daily_pipeline.py:31-108``（覆盖集修剪 + 值级修剪）、
``services/kline_sync.py:477-479``（``quote_ts`` 快照标记的写入侧）、
``services/data_integrity.py:64-171,228-330``（哨兵判定 + 尾部缺口扫描 +
删分区重算）。本文件的实现与注释按 lquant 口径重写，未复制其代码结构。

与 lquant 既有模块的分工：``crosscheck.py`` 做**跨源**对拍（主源 vs 同行
provider，只标记降级）；``coverage.py`` 做**日粒度**覆盖缺口（整日/标的级
稀疏）并落 issue；``store/integrity.py`` 做数据根自检与**年分区**连续性。
本模块补的是**值级**这一层：行数相同也能发现「当日分区被盘中快照污染」。

**哨兵需要谁来生产（关键存疑点，必须由写入方落地）**：lquant 现在没有任何
写入方产出 ``quote_ts``，所以对现有湖调用 :func:`partition_is_snapshot` 一律
返回「未知」——这是**有意的 fail-loudly**，不是 bug。要让它生效：

- 盘后权威批量路径：``lquant/data/ingest/daily.py``（``sync_daily`` 里
  ``write_daily(_stamp(df, ...))`` 那几处）写 ``quote_ts=NULL``（缺失即权威）；
- 盘中实时落盘路径（lquant 目前**还没有**这条路径，需新建）：把
  ``providers/tencent.py::parse_quotes`` 的 ``ts`` 列（源站字段 ~30，
  ``yyyymmddHHMMSS`` 上海墙钟）换算成 epoch 毫秒写进 ``quote_ts``；
- 落库必经的 ``lquant/data/store/parquet.py::write_daily`` 的 ``_overlay``
  是 key 覆盖合并，**新列会随 diagonal concat 保留**，无需改写入层，但需要
  在 ``data/schema.py`` 的 ``DAILY_BAR`` 里加一列（本任务范围外）。

旁挂 meta 是备用形态（写入方不方便加列时用）：分区目录下放
``_snapshot_meta.json``，形如 ``{"quote_ts_ms": 1757400000000}`` 或
``{"days": {"2026-09-10": {"quote_ts_ms": ...}}}``。它只有整分区一个时刻，
无法识别逐行残留，精度低于列形态。注意：lquant 日线是**按年单文件**
（``daily/year=2026/part-0.parquet``），同目录的 meta 只能表达「该目录内
某交易日」；按日分目录（``date=YYYY-MM-DD/``）时才能表达整分区。

修复动作的 lquant 口径适配：分区不是按日一个文件，直接删「年文件」会把整年
数据一起删掉（灾难）。所以 :func:`apply_repair` 对 ``date=`` 形态的日目录走
``rmtree``（与参考实现一致），对年文件走**逐行 purge**（只删该交易日的行，
其余日原样保留），并复用 ``store/parquet.py`` 的原子写与双层文件锁。
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from lquant.core.logging import get_logger
from lquant.core.sessions import CLOSE_TIME
from lquant.core.types import TZ

log = get_logger(__name__)

__all__ = [
    "AUTHORITATIVE",
    "HALF_TICK",
    "META_FILE",
    "SENTINEL_COL",
    "SNAPSHOT",
    "UNKNOWN",
    "CoverageCheckResult",
    "IntegrityReport",
    "RepairAction",
    "RepairItem",
    "RepairPlan",
    "RepairReport",
    "SnapshotVerdict",
    "ValueCheckResult",
    "ValueMismatch",
    "apply_repair",
    "check_partition_coverage",
    "check_partition_values",
    "integrity_report",
    "partition_is_snapshot",
    "plan_repair",
    "read_partition_frame",
    "resolve_day_files",
]

#: 半个最小报价单位。A 股最小报价单位 0.01 元，浮点误差允许半个单位：
#: ``|diff| > 0.005`` 才算不一致。取「半个单位」而不是「一个单位」是因为
#: 合法来源之间也可能有四舍五入到分位的末位差（0.01），一个单位会把这类
#: 可接受的舍入差误报成污染；半个单位则能抓住真正的价格错误（参考实现同口径）。
HALF_TICK = 0.005

#: 阈值比较的浮点护栏。``10.005 - 10.00`` 在 IEEE754 下算出
#: ``0.005000000000000782``，裸 ``> 0.005`` 会把「正好半个单位」判成不一致；
#: 加一个远小于真实错误量级（真实价格错至少 0.01）的 epsilon，只吸收表示
#: 误差，不掩盖任何真实差异。
_TOL_EPS = 1e-9

#: 快照哨兵列名（epoch 毫秒，上海墙钟口径）。与参考实现同名，便于对读。
SENTINEL_COL = "quote_ts"

#: 旁挂 meta 文件名（列形态不可用时的备用哨兵）。
META_FILE = "_snapshot_meta.json"

#: 快照判定的三态。**缺失哨兵只能是 UNKNOWN，绝不是 AUTHORITATIVE** ——
#: 把「没有证据」当成「证据表明干净」，正是事故分区永远进不了修复的原因。
SNAPSHOT = "snapshot"
AUTHORITATIVE = "authoritative"
UNKNOWN = "unknown"

#: 值级比对的结果状态。
VALUE_OK = "ok"
VALUE_MISMATCH = "mismatch"
VALUE_NO_AUTHORITY = "no_authority"
VALUE_EMPTY_PARTITION = "empty_partition"
VALUE_NO_VALUE_COLUMN = "no_value_column"
VALUE_NO_OVERLAP = "no_overlap"
VALUE_SELF_REFERENCE = "self_reference"
VALUE_EMPTY_AUTHORITY = "empty_authority"

#: 覆盖集比对的结果状态。
COVERAGE_OK = "ok"
COVERAGE_GAP = "gap"
COVERAGE_NO_AUTHORITY = "no_authority"
COVERAGE_NO_HALT_FILTER = "no_halt_filter"
COVERAGE_EMPTY_PARTITION = "empty_partition"

#: 修复原因（写进 RepairItem.reasons）。
REASON_SNAPSHOT = "snapshot"
REASON_VALUE_MISMATCH = "value_mismatch"
REASON_COVERAGE_GAP = "coverage_gap"

#: 权威来源的类型：DataFrame / LazyFrame / 「某交易日 → 帧」映射 /
#: 「某交易日 → 帧」回调 / parquet 路径。参数注入是刻意的 —— 权威来源
#: 有多种形态（官方日线表、已确认的权威快照、Golden 冻结值），模块不该替
#: 调用方决定，更不该默认回落到湖里的分区自己。
AuthoritySource = (
    pl.DataFrame
    | pl.LazyFrame
    | Mapping[date, pl.DataFrame]
    | Callable[[date], pl.DataFrame | None]
    | str
    | Path
)


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------
def _iter_parquet(partition: Path) -> list[Path]:
    """分区下的全部 parquet 文件（单文件原样返回；目录递归）；排序保证确定性。"""
    if partition.is_file():
        return [partition]
    if not partition.is_dir():
        return []
    return sorted(partition.glob("**/*.parquet"))


def _to_date_expr(dtype: pl.DataType, expr: pl.Expr) -> pl.Expr:
    """把 trade_date 归一到 Date。

    湖内 trade_date 是 Date，但跨版本/跨写入方可能出现 Datetime 或字符串；
    dtype 不一致时 ``== day`` 的比较要么抛错要么**静默全 False**（那就成了
    「每天都缺」的假象），所以统一在这里归一。
    """
    if dtype == pl.Date:
        return expr
    if isinstance(dtype, pl.Datetime):
        return expr.dt.date()
    return expr.cast(pl.String, strict=False).str.slice(0, 10).str.to_date(strict=False)


def _scan(files: Sequence[Path]) -> pl.LazyFrame:
    """多文件 scan，容忍跨文件 schema 漂移。

    显式 list 传入 ``pl.scan_parquet`` 时 ``missing_columns="insert"`` 不生效
    （该参数只在 glob 形态下做 schema 归一），所以这里改用 ``diagonal_relaxed``
    拼接：某文件缺哨兵列时补 null，某文件多列时保留 —— 这正是「同日分区由
    盘中快照文件 + 盘后批量文件共同构成」的常见形态，哨兵列只可能在部分文件
    里出现。若不做归一，多文件分区会被误判成「哨兵列不存在」而恒为未知。
    """
    return pl.concat(
        [pl.scan_parquet(str(f)) for f in files], how="diagonal_relaxed"
    )


def read_partition_frame(
    partition: Path | str,
    day: date | None = None,
    *,
    columns: Sequence[str] | None = None,
) -> pl.DataFrame:
    """读「某交易日分区」的行。

    ``partition`` 可以是单文件、``date=YYYY-MM-DD/`` 日目录，或 ``daily/``
    整棵表目录 —— ``day`` 给了就按 ``trade_date == day`` 过滤（对 lquant 的
    ``year=YYYY/part-0.parquet`` 年文件布局同样成立：年文件里筛出一天）。
    这样同一个入口既能跑参考实现的按日分区布局，也能跑 lquant 的年文件布局。

    读不出来（文件损坏）时**抛异常**，不做静默空结果 —— 检查工具把「读不动」
    当成「没有数据」会让污染分区看起来干净。
    """
    files = _iter_parquet(Path(partition))
    if not files:
        return pl.DataFrame()
    lf = _scan(files)
    if day is not None:
        schema = lf.collect_schema()
        if "trade_date" not in schema.names():
            return pl.DataFrame()
        lf = lf.with_columns(
            _to_date_expr(schema["trade_date"], pl.col("trade_date")).alias("trade_date")
        ).filter(pl.col("trade_date") == day)
    if columns is not None:
        lf = lf.select(list(columns))
    return lf.collect()


def resolve_day_files(table_dir: Path | str, day: date) -> list[Path]:
    """该交易日在湖里涉及的 parquet 文件。

    有 ``date=<iso>/`` 日目录时只返回它（参考实现的按日分区布局，最精确）；
    否则返回整棵 ``table_dir`` 下的 parquet（lquant 年文件布局，调用方按
    ``trade_date`` 过滤）。``plan_repair`` 只扫近端窗口，不会遍历全历史湖。
    """
    root = Path(table_dir)
    day_dir = root / f"date={day.isoformat()}"
    if day_dir.is_dir():
        return _iter_parquet(day_dir)
    return _iter_parquet(root)


def _table_root(partition: Path) -> Path:
    """分区所在的「表根」——用于识别「拿湖自己当权威来源」的自比陷阱。"""
    root = partition if partition.is_dir() else partition.parent
    if root.name.startswith("date="):
        root = root.parent
    return root


# ---------------------------------------------------------------------------
# 哨兵
# ---------------------------------------------------------------------------
def _sentinel_ms(frame: pl.DataFrame) -> pl.Series | None:
    """把哨兵列归一成 Int64 epoch 毫秒；列不存在返回 None。

    Datetime 形态也接受：带时区的直接用，**naive 的按上海墙钟理解**
    （``providers.tencent.parse_quotes`` 的 ``ts`` 就是 naive 上海墙钟）。
    不这么约定的话，naive 14:30 会被当成 UTC 14:30 = 上海 22:30，收盘线
    判定整体错 8 小时，盘中快照会被误判成权威历史。
    """
    if SENTINEL_COL not in frame.columns:
        return None
    series = frame[SENTINEL_COL]
    if isinstance(series.dtype, pl.Datetime):
        if series.dtype.time_zone is None:
            series = series.dt.replace_time_zone("Asia/Shanghai")
        return series.dt.epoch("ms").cast(pl.Int64, strict=False)
    return series.cast(pl.Int64, strict=False)


def _to_ms(value: Any) -> int | None:
    """任意时刻表示 → epoch 毫秒；无法识别返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=TZ)
        return int(dt.timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            return _to_ms(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def _day_bounds_ms(day: date) -> tuple[int, int, int]:
    """该交易日的 (00:00, 15:00, 次日 00:00) 上海墙钟 → epoch 毫秒。

    「当日盘后」= ``15:00 <= ts < 次日 00:00``。边界用半开区间，向量化判定
    不依赖逐行 Python 函数（``map_elements`` 在几十万行上会拖慢整轮检查）。
    """
    start = datetime.combine(day, time.min, tzinfo=TZ)
    cutoff = datetime.combine(day, CLOSE_TIME, tzinfo=TZ)
    start_ms = int(start.timestamp() * 1000)
    return start_ms, int(cutoff.timestamp() * 1000), start_ms + 86_400_000


def _post_close(day: date, ts_ms: int) -> bool:
    """哨兵时刻是否「当日盘后」（>= 15:00 上海墙钟）。

    不同日的哨兵（陈旧报价）一律不算盘后权威 —— 它对该交易日没有意义。
    """
    _start, cutoff, end = _day_bounds_ms(day)
    return cutoff <= ts_ms < end


def _meta_candidates(part: Path, day: date) -> list[Path]:
    """旁挂 meta 的候选路径。

    调用方可能传日目录（``daily/date=X/``）也可能传表根（``daily/``）——
    两种都要能找到 meta：表根形态下 meta 通常放在 ``date=X/`` 子目录里。
    按「越具体越优先」排序。
    """
    if part.is_file():
        return [part.parent / META_FILE]
    return [
        part / f"date={day.isoformat()}" / META_FILE,
        part / META_FILE,
    ]


def _sidecar_quote_ts(part: Path, day: date) -> tuple[int | None, str]:
    """读旁挂 meta 的哨兵；返回 (epoch_ms | None, 说明)。"""
    meta_path = next((p for p in _meta_candidates(part, day) if p.is_file()), None)
    if meta_path is None:
        return None, "无旁挂 meta"
    try:
        payload = json.loads(meta_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # 坏 meta 不能当成「没有快照」——那样会让污染分区看起来干净。
        log.warning(f"旁挂 meta 解析失败，按未知处理 {meta_path}: {e}")
        return None, f"旁挂 meta 解析失败: {e}"
    if not isinstance(payload, dict):
        return None, "旁挂 meta 结构非法（顶层不是对象）"
    if "quote_ts_ms" in payload:
        ts = _to_ms(payload.get("quote_ts_ms"))
        return ts, f"旁挂 meta(扁平) {meta_path}"
    days = payload.get("days")
    if isinstance(days, dict):
        entry = days.get(day.isoformat())
        if isinstance(entry, dict):
            ts = _to_ms(entry.get("quote_ts_ms"))
            return ts, f"旁挂 meta(days[{day.isoformat()}]) {meta_path}"
    return None, "旁挂 meta 无该交易日条目"


def _has_activity(frame: pl.DataFrame) -> bool | None:
    """帧中是否存在成交（volume/amount 任一 > 0）；无相关列返回 None。"""
    cols = [c for c in ("volume", "amount") if c in frame.columns]
    if not cols:
        return None
    expr = pl.any_horizontal(
        [pl.col(c).cast(pl.Float64, strict=False).fill_null(0) > 0 for c in cols]
    )
    return bool(frame.select(expr.any()).item())


@dataclass(frozen=True)
class SnapshotVerdict:
    """分区是否为盘中快照的判定结论（三态）。

    ``state`` 取值 :data:`SNAPSHOT` / :data:`AUTHORITATIVE` / :data:`UNKNOWN`。
    调用方**必须**显式处理 UNKNOWN：它表示哨兵缺失或不可信，既不能当成
    「已收盘的权威历史」复用，也不该被自动删分区（没有证据的删除是破坏）。
    """

    state: str
    reason: str
    quote_ts_max_ms: int | None = None
    rows: int = 0
    suspicious_rows: int = 0
    batch_rows: int = 0

    @property
    def is_snapshot(self) -> bool:
        return self.state == SNAPSHOT

    @property
    def is_unknown(self) -> bool:
        return self.state == UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "quote_ts_max_ms": self.quote_ts_max_ms,
            "rows": self.rows,
            "suspicious_rows": self.suspicious_rows,
            "batch_rows": self.batch_rows,
        }


def partition_is_snapshot(partition: Path | str, day: date) -> SnapshotVerdict:
    """判定某交易日分区是否仍是「盘中实时落盘」的快照。

    判定口径（对参考实现 ``_partition_is_snapshot`` 的移植 + 一处收紧）：

    - 哨兵列存在：``quote_ts`` 为空的行是**盘后权威批量行**（写入侧约定：
      批量行不写报价时刻）；非空且**不满足「当日 >= 15:00」**的行是可疑的
      盘中快照行（含当日收盘前、以及哨兵落在别的日期的陈旧报价 —— 后者是
      参考实现没有覆盖的一类，一行对某交易日而言带着别日的报价时刻必然是
      错的，所以本实现把它一并算作可疑）。
    - 全为空 → 权威；
    - 有可疑行且**有成交** → 快照；
    - 有可疑行但全部**零成交**且批量行更多 → 不是快照（停牌股票的实时轮询
      会留下 09:15/零成交的孤立行，batch 侧过滤停牌日不会覆盖它们；把这种
      残留当快照会让分区反复进入修复，参考实现踩过）；
    - 整个分区只有零成交实时行、没有批量行 → 仍是快照（盘前写入也在此列）。

    **哨兵缺失（既无列也无可用 meta）→ UNKNOWN，绝不返回 AUTHORITATIVE。**
    这就是事故里「盘中覆写的分区在停机后被当作完整历史，永远不进修复」的
    根因：没有证据时必须承认「不知道」，由调用方决定（值级比对仍可独立发现
    价格错，见 :func:`check_partition_values`）。
    """
    part = Path(partition)
    if not part.exists():
        return SnapshotVerdict(UNKNOWN, f"分区不存在: {part}")
    frame = read_partition_frame(part, day)
    if frame.height == 0:
        return SnapshotVerdict(UNKNOWN, "该交易日无行（属覆盖缺口，不是快照判定的对象）")

    ms = _sentinel_ms(frame)
    if ms is None:
        side_ts, side_reason = _sidecar_quote_ts(part, day)
        if side_ts is None:
            return SnapshotVerdict(
                UNKNOWN,
                f"缺少 {SENTINEL_COL} 哨兵列且无可用旁挂 meta（{side_reason}）"
                " —— 不能假定为权威历史",
                rows=frame.height,
            )
        if _post_close(day, side_ts):
            return SnapshotVerdict(
                AUTHORITATIVE,
                f"{side_reason} 的 quote_ts 不早于当日收盘线",
                quote_ts_max_ms=side_ts,
                rows=frame.height,
            )
        # 旁挂 meta 只有整分区一个时刻，无法识别「批量行 + 停牌实时残留」的混合。
        # 时刻不是当日盘后 → 按快照处理（保守方向：宁可进修复，不可假干净）。
        return SnapshotVerdict(
            SNAPSHOT,
            f"{side_reason} 的 quote_ts 早于当日收盘线（或不在当日）",
            quote_ts_max_ms=side_ts,
            rows=frame.height,
            suspicious_rows=frame.height,
        )

    null_rows = int(ms.null_count())
    _start, cutoff, end = _day_bounds_ms(day)
    with_ms = frame.with_columns(ms.alias("_quote_ms"))
    # 可疑 = 非空且**不**落在 [15:00, 次日 00:00)：含当日收盘前与任何异日哨兵。
    suspicious = with_ms.filter(
        pl.col("_quote_ms").is_not_null()
        & ~pl.col("_quote_ms").is_between(cutoff, end, closed="left")
    )
    valid = ms.drop_nulls()
    ts_max = int(valid.max()) if valid.len() else None

    if suspicious.height == 0:
        reason = (
            "全部行无 quote_ts（盘后批量行）"
            if null_rows == frame.height
            else "非空 quote_ts 均为当日盘后时刻"
        )
        return SnapshotVerdict(
            AUTHORITATIVE, reason, quote_ts_max_ms=ts_max,
            rows=frame.height, batch_rows=null_rows,
        )

    activity = _has_activity(suspicious)
    if activity is None or activity:
        detail = "无法判定成交" if activity is None else "存在成交"
        return SnapshotVerdict(
            SNAPSHOT,
            f"{suspicious.height}/{frame.height} 行是收盘前快照（{detail}）",
            quote_ts_max_ms=ts_max,
            rows=frame.height,
            suspicious_rows=suspicious.height,
            batch_rows=null_rows,
        )

    # 可疑行全部零成交：停牌实时残留。批量权威行更多时不算快照。
    if null_rows > suspicious.height:
        return SnapshotVerdict(
            AUTHORITATIVE,
            f"收盘前实时行 {suspicious.height} 行全部零成交（停牌残留），"
            f"垃圾行不多于批量权威行 {null_rows} 行",
            quote_ts_max_ms=ts_max,
            rows=frame.height,
            suspicious_rows=suspicious.height,
            batch_rows=null_rows,
        )
    return SnapshotVerdict(
        SNAPSHOT,
        f"整分区 {suspicious.height} 行均为收盘前零成交实时行，无批量权威行",
        quote_ts_max_ms=ts_max,
        rows=frame.height,
        suspicious_rows=suspicious.height,
        batch_rows=null_rows,
    )


# ---------------------------------------------------------------------------
# 权威来源
# ---------------------------------------------------------------------------
def _load_authority_frame(authority: Any, day: date, partition: Path) -> tuple[pl.DataFrame | None, str]:
    """把任意形态的权威来源取成「该交易日」的 DataFrame。

    返回 ``(frame | None, status)``：status 为空串表示成功；否则是
    :data:`VALUE_NO_AUTHORITY` 等具体原因，调用方据此如实回报「无法比对」。
    """
    if authority is None:
        return None, VALUE_NO_AUTHORITY
    if isinstance(authority, Mapping):
        frame = authority.get(day)
        return (None, VALUE_NO_AUTHORITY) if frame is None else (frame, "")
    if callable(authority):
        frame = authority(day)
        return (None, VALUE_NO_AUTHORITY) if frame is None else (frame, "")
    if isinstance(authority, (str, Path)):
        path = Path(authority)
        # 自比陷阱：权威路径指回被检查的表根时，比对必然恒一致，是最坏的
        # 假阴性。宁可直接拒绝，也不产出一个「看着干净」的结论。
        root = _table_root(partition).resolve()
        target = path.resolve()
        if target == root or root in target.parents:
            return None, VALUE_SELF_REFERENCE
        if not path.exists():
            return None, VALUE_NO_AUTHORITY
        frame = pl.read_parquet(str(path)) if path.is_file() else read_partition_frame(path, day)
    elif isinstance(authority, pl.LazyFrame):
        frame = authority.collect()
    elif isinstance(authority, pl.DataFrame):
        frame = authority
    else:
        return None, VALUE_NO_AUTHORITY

    if frame is None or frame.height == 0:
        return None, VALUE_EMPTY_AUTHORITY
    schema = frame.schema
    if "trade_date" in schema:
        expr = _to_date_expr(schema["trade_date"], pl.col("trade_date"))
        frame = frame.with_columns(expr.alias("trade_date")).filter(pl.col("trade_date") == day)
        if frame.height == 0:
            return None, VALUE_EMPTY_AUTHORITY
    return frame, ""


def _dedup_symbols(frame: pl.DataFrame, value_col: str) -> tuple[pl.DataFrame, int]:
    """按 symbol 去重（保留最后一行），返回 (帧, 被丢弃的行数)。

    同日同标的重复行本身就是数据问题；这里不让它把 join 炸成笛卡尔积，
    但把丢弃行数回报给调用方（不静默）。
    """
    if "symbol" not in frame.columns:
        return frame, 0
    before = frame.height
    cleaned = frame.unique(subset=["symbol"], keep="last") if before else frame
    return cleaned, before - cleaned.height


# ---------------------------------------------------------------------------
# 值级比对
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValueMismatch:
    """一只标的的收盘价与权威来源的差异。"""

    symbol: str
    value: float
    authority_value: float
    diff: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "value": self.value,
            "authority_value": self.authority_value,
            "diff": self.diff,
        }


@dataclass(frozen=True)
class ValueCheckResult:
    """某交易日分区的值级比对结论。"""

    day: date
    status: str
    checked: int
    tolerance: float
    mismatches: tuple[ValueMismatch, ...] = ()
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == VALUE_OK

    @property
    def comparable(self) -> bool:
        """是否真的比过（区别于「没权威来源所以没法比」）。"""
        return self.status in (VALUE_OK, VALUE_MISMATCH, VALUE_NO_OVERLAP)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "status": self.status,
            "checked": self.checked,
            "tolerance": self.tolerance,
            "n_mismatch": len(self.mismatches),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "note": self.note,
        }


def check_partition_values(
    partition: Path | str,
    day: date,
    authority: AuthoritySource | None = None,
    *,
    value_col: str = "close",
    authority_value_col: str | None = None,
    tolerance: float = HALF_TICK,
    max_samples: int = 20,
) -> ValueCheckResult:
    """逐 symbol 比较当日分区 ``close`` 与**权威来源**，差值超阈值即不一致。

    为什么按值而不是按行：真实事故里 3392/5554 只股票收盘价与官方日线不符，
    但**每只股票都有行**，行数校验完全无感。价格错会顺着因子/回测放大成
    「看似合理」的静默错误，只有逐值比对能发现。

    为什么必须注入权威来源：同一份湖里的 ``close`` 跟它自己比恒一致，
    不构成任何证据。``authority=None`` 时返回 ``status='no_authority'`` 并
    在 note 里说清「无法比对」，绝不给出「通过」。权威来源可以是官方日线表
    （重新拉取的 provider 日线）、已人工确认的权威快照、或 Golden 冻结值。

    Args:
        partition: 单文件 / ``date=`` 日目录 / 表目录。
        day: 交易日。
        authority: 权威来源（Frame / Mapping[date, Frame] / 回调 / parquet 路径）。
        value_col: 分区里的价格列名（默认 ``close``）。
        authority_value_col: 权威来源里的价格列名（默认同 ``value_col``）。
        tolerance: 判不一致的阈值，默认 :data:`HALF_TICK`（半个最小报价单位）。
    """
    part = Path(partition)
    auth_col = authority_value_col or value_col
    frame = read_partition_frame(part, day)
    if frame.height == 0:
        return ValueCheckResult(day, VALUE_EMPTY_PARTITION, 0, tolerance,
                                note="该交易日分区无行，交由覆盖检查处理")
    if value_col not in frame.columns:
        return ValueCheckResult(
            day, VALUE_NO_VALUE_COLUMN, 0, tolerance,
            note=f"分区缺少价格列 {value_col!r}（现有列：{sorted(frame.columns)}）",
        )

    authority_frame, status = _load_authority_frame(authority, day, part)
    if authority_frame is None:
        note = {
            VALUE_NO_AUTHORITY: "未提供权威来源 —— 无法比对（不拿分区自己跟自己比）",
            VALUE_SELF_REFERENCE: "权威来源指向被检查的表根 —— 拒绝自比（必然恒一致）",
            VALUE_EMPTY_AUTHORITY: "权威来源在该交易日没有数据 —— 无法比对",
        }.get(status, f"权威来源不可用: {status}")
        return ValueCheckResult(day, status, 0, tolerance, note=note)
    if auth_col not in authority_frame.columns:
        return ValueCheckResult(
            day, VALUE_NO_VALUE_COLUMN, 0, tolerance,
            note=f"权威来源缺少价格列 {auth_col!r}",
        )

    left, dropped_l = _dedup_symbols(frame.select(["symbol", value_col]), value_col)
    right, dropped_r = _dedup_symbols(
        authority_frame.select(["symbol", auth_col]), auth_col
    )
    left = left.rename({value_col: "value"}).drop_nulls(["symbol", "value"])
    right = right.rename({auth_col: "authority_value"}).drop_nulls(
        ["symbol", "authority_value"]
    )
    joined = left.join(right, on="symbol", how="inner")
    if joined.height == 0:
        return ValueCheckResult(
            day, VALUE_NO_OVERLAP, 0, tolerance,
            note="分区与权威来源没有共同标的 —— 无法比对（不视为通过）",
        )

    bad = (
        joined.with_columns(
            (pl.col("value") - pl.col("authority_value")).abs().alias("diff")
        )
        .filter(pl.col("diff") > tolerance + _TOL_EPS)
        .sort("diff", descending=True)
    )
    mismatches = tuple(
        ValueMismatch(
            symbol=row["symbol"],
            value=float(row["value"]),
            authority_value=float(row["authority_value"]),
            diff=float(row["diff"]),
        )
        for row in bad.head(max_samples).iter_rows(named=True)
    )
    notes = []
    if dropped_l or dropped_r:
        notes.append(f"重复行去重：分区 {dropped_l} 行 / 权威 {dropped_r} 行")
    if bad.height > len(mismatches):
        notes.append(f"不一致 {bad.height} 只，仅列前 {len(mismatches)} 只")
    return ValueCheckResult(
        day=day,
        status=VALUE_MISMATCH if bad.height else VALUE_OK,
        checked=joined.height,
        tolerance=tolerance,
        mismatches=mismatches,
        note="; ".join(notes),
    )


# ---------------------------------------------------------------------------
# 覆盖集比对
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageCheckResult:
    """某交易日分区的 symbol 覆盖集比对结论。"""

    day: date
    status: str
    expected: int
    actual: int
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    n_missing: int = 0
    n_extra: int = 0
    rows_partition: int = 0
    rows_authority: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == COVERAGE_OK

    @property
    def row_count_differs(self) -> bool:
        """行数是否不同 —— 用来提醒「行数校验在这里会误报」。"""
        return self.rows_partition != self.rows_authority

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "n_missing": self.n_missing,
            "n_extra": self.n_extra,
            "missing": list(self.missing),
            "extra": list(self.extra),
            "rows_partition": self.rows_partition,
            "rows_authority": self.rows_authority,
            "row_count_differs": self.row_count_differs,
            "note": self.note,
        }


def _tradable_mask(frame: pl.DataFrame) -> pl.Expr | None:
    """停牌过滤掩码；帧里没有可判定停牌的列时返回 None。

    口径来源：lquant ``quality/tradability.py`` —— ``SUSPENDED`` 位 = 当日
    ``volume == 0``；日线 schema 另有显式 ``is_suspended`` 布尔列。两者取并集：
    ``is_suspended == True`` 或 ``volume == 0`` 视为不可交易。

    ``volume`` 为 null 不算停牌（null 是「没这个字段的值」，不是「零成交」）；
    但要能判定就必须至少有一个证据列，否则返回 None 让调用方 fail-loudly。
    """
    exprs: list[pl.Expr] = []
    if "is_suspended" in frame.columns:
        # 显式 cast Boolean：列若是 Int8/Utf8，``~`` 会按位取反而不是逻辑取反。
        exprs.append(
            ~pl.col("is_suspended").cast(pl.Boolean, strict=False).fill_null(False)
        )
    if "volume" in frame.columns:
        exprs.append(pl.col("volume").cast(pl.Float64, strict=False) != 0)
    if not exprs:
        return None
    mask = exprs[0]
    for expr in exprs[1:]:
        mask = mask & expr
    return mask


def _tradable_symbols(frame: pl.DataFrame) -> set[str] | None:
    """停牌过滤后的 symbol 集合；无法过滤返回 None。"""
    mask = _tradable_mask(frame)
    if mask is None or "symbol" not in frame.columns:
        return None
    rows = frame.filter(mask & pl.col("symbol").is_not_null()).select("symbol")
    return set(rows["symbol"].to_list())


def check_partition_coverage(
    partition: Path | str,
    day: date,
    authority: AuthoritySource | None = None,
    *,
    max_samples: int = 50,
) -> CoverageCheckResult:
    """按**停牌过滤后的 symbol 覆盖集**比对当日分区与权威来源。

    为什么不比行数：正常剔除停牌本来就会让分区比权威来源少行（权威来源若
    含停牌行，或反向剔除以外的口径差异），按行数比对会把健康分区判成不完整
    —— 参考实现里这正是「每次管道都删除重算」的成因。集合比对同时给出
    ``row_count_differs`` 供报告说明「行数在这里会误报」。

    ``missing`` 是期望有、分区没有的标的（真正需要重算的），``extra`` 是
    分区有、权威来源没有的（可能是新上市/退市/口径差，仅提示不自动修）。

    无法做停牌过滤（权威来源既无 ``is_suspended`` 也无 ``volume``）时返回
    ``status='no_halt_filter'``：此时算出的 missing 不可信，宁可说「判不了」。
    """
    part = Path(partition)
    frame = read_partition_frame(part, day)
    if frame.height == 0:
        return CoverageCheckResult(day, COVERAGE_EMPTY_PARTITION, 0, 0,
                                   note="该交易日分区无行（整日缺口）")

    authority_frame, status = _load_authority_frame(authority, day, part)
    if authority_frame is None:
        note = {
            VALUE_NO_AUTHORITY: "未提供权威来源 —— 无法比对覆盖集",
            VALUE_SELF_REFERENCE: "权威来源指向被检查的表根 —— 拒绝自比",
            VALUE_EMPTY_AUTHORITY: "权威来源在该交易日没有数据 —— 无法比对",
        }.get(status, f"权威来源不可用: {status}")
        return CoverageCheckResult(day, COVERAGE_NO_AUTHORITY, 0, 0,
                                   rows_partition=frame.height, note=note)

    expected = _tradable_symbols(authority_frame)
    if expected is None:
        return CoverageCheckResult(
            day, COVERAGE_NO_HALT_FILTER, 0, 0,
            rows_partition=frame.height, rows_authority=authority_frame.height,
            note="权威来源缺少 is_suspended/volume，无法按停牌口径构建期望覆盖集",
        )
    actual = _tradable_symbols(frame)
    notes = []
    if actual is None:
        # 分区侧没有停牌证据：missing 仍然可靠（期望集是权威侧算的），
        # 但 extra 可能混入停牌残留行 —— 明说，不静默。
        actual = set(frame["symbol"].drop_nulls().to_list()) if "symbol" in frame.columns else set()
        notes.append("分区缺少 is_suspended/volume，未做停牌过滤（extra 可能含停牌行）")

    missing = tuple(sorted(expected - actual))
    extra = tuple(sorted(actual - expected))
    # 采样上限只影响「列出来几只」，n_missing/n_extra 始终是全量计数，
    # 报告不会因为截断而少报缺口规模。
    if len(missing) > max_samples:
        notes.append(f"缺失 {len(missing)} 只，仅列前 {max_samples} 只")
    if len(extra) > max_samples:
        notes.append(f"多出 {len(extra)} 只，仅列前 {max_samples} 只")
    return CoverageCheckResult(
        day=day,
        status=COVERAGE_GAP if missing else COVERAGE_OK,
        expected=len(expected),
        actual=len(actual),
        missing=missing[:max_samples],
        extra=extra[:max_samples],
        n_missing=len(missing),
        n_extra=len(extra),
        rows_partition=frame.height,
        rows_authority=authority_frame.height,
        note="; ".join(notes),
    )


# ---------------------------------------------------------------------------
# 修复计划与执行
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RepairItem:
    """一个需要重算的交易日分区及其原因。"""

    day: date
    reasons: tuple[str, ...]
    detail: dict[str, Any]
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "reasons": list(self.reasons),
            "detail": self.detail,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class RepairPlan:
    """修复计划（只描述，不执行）。

    参考实现「删分区让增量重算把它们当新日期」依赖增量只算湖里不存在的
    日期：已被污染的分区**存在**，不删就永远不重算。计划就是列出这些日期。
    """

    table_dir: str
    days: tuple[date, ...]
    items: tuple[RepairItem, ...]
    unknown_sentinel: tuple[date, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def repair_days(self) -> tuple[date, ...]:
        return tuple(item.day for item in self.items)

    @property
    def empty(self) -> bool:
        return not self.items

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_dir": self.table_dir,
            "days_scanned": [d.isoformat() for d in self.days],
            "repair_days": [d.isoformat() for d in self.repair_days],
            "items": [i.to_dict() for i in self.items],
            "unknown_sentinel": [d.isoformat() for d in self.unknown_sentinel],
            "notes": list(self.notes),
        }


def plan_repair(
    table_dir: Path | str,
    days: Iterable[date],
    *,
    authority: AuthoritySource | None = None,
    value_col: str = "close",
    tolerance: float = HALF_TICK,
) -> RepairPlan:
    """列出需要重算的交易日分区 + 原因（**不落盘**）。

    三类原因：``snapshot``（哨兵判定为盘中快照）、``value_mismatch``（值级
    比对不一致）、``coverage_gap``（停牌过滤后覆盖集缺标的）。一个日期可以
    同时命中多个原因。

    哨兵「未知」的日期**不进修复清单**，只记在 ``unknown_sentinel`` 里：没有
    证据的删除是破坏性动作。但注意值级比对是独立证据 —— 哨兵未知不妨碍
    它把价格错的分区送进修复（若提供了权威来源）。

    读取失败的日期记入 ``notes`` 而不是抛异常：单个坏文件不该让整轮检查
    停摆，但必须在结果里可见。
    """
    root = Path(table_dir)
    scanned = tuple(sorted(set(days)))
    items: list[RepairItem] = []
    unknown: list[date] = []
    notes: list[str] = []

    for day in scanned:
        try:
            frame = read_partition_frame(root, day)
        except Exception as e:  # noqa: BLE001 - 分区损坏要回报，不是让整轮崩
            notes.append(f"{day} 读取失败：{type(e).__name__}: {e}")
            continue
        if frame.height == 0:
            notes.append(f"{day} 无行 —— 属尾部缺口，交给 integrity_report/覆盖检查")
            continue

        reasons: list[str] = []
        detail: dict[str, Any] = {"rows": frame.height}

        try:
            verdict = partition_is_snapshot(root, day)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{day} 哨兵判定失败：{type(e).__name__}: {e}")
            verdict = None
        if verdict is not None:
            detail["sentinel"] = verdict.state
            detail["sentinel_reason"] = verdict.reason
            if verdict.is_snapshot:
                reasons.append(REASON_SNAPSHOT)
            elif verdict.is_unknown:
                unknown.append(day)

        value = check_partition_values(root, day, authority,
                                       value_col=value_col, tolerance=tolerance)
        if value.status == VALUE_MISMATCH:
            reasons.append(REASON_VALUE_MISMATCH)
            detail["n_mismatch"] = len(value.mismatches)
            detail["max_diff"] = max(m.diff for m in value.mismatches)
            detail["sample_symbols"] = [m.symbol for m in value.mismatches[:5]]
        elif value.status not in (VALUE_OK, VALUE_NO_OVERLAP):
            detail["value_status"] = value.status

        coverage = check_partition_coverage(root, day, authority)
        if coverage.status == COVERAGE_GAP:
            reasons.append(REASON_COVERAGE_GAP)
            detail["n_missing"] = len(coverage.missing)
            detail["missing_sample"] = list(coverage.missing[:5])
        elif coverage.status != COVERAGE_OK:
            detail["coverage_status"] = coverage.status

        if reasons:
            items.append(RepairItem(
                day=day,
                reasons=tuple(reasons),
                detail=detail,
                paths=tuple(str(p) for p in resolve_day_files(root, day)),
            ))

    return RepairPlan(
        table_dir=str(root),
        days=scanned,
        items=tuple(items),
        unknown_sentinel=tuple(sorted(set(unknown))),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class RepairAction:
    """一条实际（或 dry-run 下计划）的删除动作。"""

    day: date
    path: str
    action: str          # "rmtree" | "rewrite" | "unlink"
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "path": self.path,
            "action": self.action,
            "rows": self.rows,
        }


@dataclass(frozen=True)
class RepairReport:
    """修复执行结果（dry-run 时是「将要做什么」）。"""

    dry_run: bool
    actions: tuple[RepairAction, ...] = ()
    deleted_rows: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "actions": [a.to_dict() for a in self.actions],
            "deleted_rows": self.deleted_rows,
            "errors": list(self.errors),
        }


def _count_day_rows(path: Path, day: date) -> tuple[int, int]:
    """只读统计 (该交易日行数, 文件总行数)（dry-run 用，绝不落盘）。

    返回总行数是为了让 dry-run 也能报告准确动作：某日行数 == 总行数时，
    真实执行会 ``unlink`` 整文件而不是 ``rewrite``。
    """
    df = pl.read_parquet(path, columns=["trade_date"])
    expr = _to_date_expr(df.schema["trade_date"], pl.col("trade_date"))
    hit = int(df.select((expr == day).sum()).item() or 0)
    return hit, df.height


def _purge_day_from_file(path: Path, day: date) -> tuple[str, int] | None:
    """从年文件里删掉该交易日的行；返回 (动作, 行数)；无该日行返回 None。

    复用 ``store/parquet.py`` 的 ``_file_lock``（进程内 + 跨进程 flock 双层）
    与 ``_atomic_write_parquet``（临时文件 + os.replace）：本模块不得另造一条
    写入路径，否则与同步链路并发时会出现半截文件 / 丢更新。
    """
    from lquant.data.store.parquet import _atomic_write_parquet, _file_lock

    with _file_lock(path):
        df = pl.read_parquet(path)
        if "trade_date" not in df.columns:
            return None
        expr = _to_date_expr(df.schema["trade_date"], pl.col("trade_date"))
        df = df.with_columns(expr.alias("trade_date"))
        hit = int(df.select((pl.col("trade_date") == day).sum()).item() or 0)
        if not hit:
            return None
        remaining = df.filter(pl.col("trade_date") != day)
        if remaining.height == 0:
            path.unlink()
            return "unlink", hit
        remaining = remaining.sort(
            [c for c in ("symbol", "trade_date") if c in remaining.columns]
        )
        _atomic_write_parquet(remaining, path)
        return "rewrite", hit


def apply_repair(plan: RepairPlan, *, dry_run: bool = True) -> RepairReport:
    """执行修复计划：删掉需要重算的分区/行，让增量重算把它们当「新日期」。

    **默认 dry_run=True，只报不改。** 删除是不可逆的破坏性动作：删错一个
    分区，湖里就少一段历史，而下游只会看到「数据变少了」。参考实现把它放在
    自动管道里是因为它面对的是可重拉的 enriched 派生表；lquant 的 daily 是
    基础观测，更该由人确认后再执行，所以这里默认值必须是安全的那个。
    真要执行要显式传 ``dry_run=False``，并在返回的 :class:`RepairReport`
    里拿到逐条的 ``actions``（删了哪个路径、什么动作、多少行）。

    两种落点：
    - ``date=YYYY-MM-DD/`` 日目录 → 整目录 ``rmtree``（参考实现口径）；
    - lquant 的 ``year=YYYY/part-0.parquet`` 年文件 → **逐行 purge**，只删该
      交易日的行，其余日原样保留。直接删年文件会把整年历史一起删掉，那是
      灾难，不是修复。

    单个分区失败不中断其余分区，错误逐条记进 ``errors``。
    """
    root = Path(plan.table_dir)
    actions: list[RepairAction] = []
    errors: list[str] = []
    deleted_rows = 0

    for day in plan.repair_days:
        day_dir = root / f"date={day.isoformat()}"
        try:
            if day_dir.is_dir():
                rows = read_partition_frame(day_dir).height
                if not dry_run:
                    shutil.rmtree(day_dir)
                actions.append(RepairAction(day, str(day_dir), "rmtree", rows))
                deleted_rows += rows
                continue
            for path in resolve_day_files(root, day):
                if not path.is_file():
                    continue
                if dry_run:
                    rows, total = _count_day_rows(path, day)
                    if not rows:
                        continue
                    action = "unlink" if rows == total else "rewrite"
                    actions.append(RepairAction(day, str(path), action, rows))
                    deleted_rows += rows
                    continue
                outcome = _purge_day_from_file(path, day)
                if outcome is None:
                    continue
                action, rows = outcome
                actions.append(RepairAction(day, str(path), action, rows))
                deleted_rows += rows
        except Exception as e:  # noqa: BLE001 - 逐分区隔离，失败要可见
            errors.append(f"{day} {type(e).__name__}: {e}")
    return RepairReport(dry_run=dry_run, actions=tuple(actions),
                        deleted_rows=deleted_rows, errors=tuple(errors))


# ---------------------------------------------------------------------------
# 汇总报告
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IntegrityReport:
    """一轮完整性检查的汇总。

    三类结论刻意分开（它们的处置动作不同）：
    - ``tail_gaps``：**尾部**整日缺口 —— 需要补数据（同步失败/没跑）；
    - ``value_mismatch`` / ``coverage_gap``：已有分区但内容错/不完整 ——
      需要重算（``plan_repair`` 的输入）；
    - ``snapshot_polluted`` / ``unknown_sentinel``：分区来源可疑 —— 前者进
      修复，后者必须由人确认真实性（见 :func:`partition_is_snapshot`）。

    只报尾部缺口是刻意的：历史内部空洞（中间某天没数据）是另一类问题，且
    在长历史里极多，混进本报告会让真正的近端事故被噪声淹没（既有
    ``store/integrity.check_partition_continuity`` 与 ``coverage.scan_coverage``
    覆盖那类问题）。这里盯的是「最近几天的当日分区有没有被盘中快照污染」。
    """

    table_dir: str
    window: tuple[date, date] | None
    tail_gaps: tuple[date, ...]
    value_mismatch: tuple[dict[str, Any], ...]
    coverage_gap: tuple[dict[str, Any], ...]
    snapshot_polluted: tuple[date, ...]
    unknown_sentinel: tuple[date, ...]
    no_authority: tuple[date, ...]
    latest_present: date | None
    calendar_checked: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_dir": self.table_dir,
            "window": [d.isoformat() for d in self.window] if self.window else None,
            "tail_gaps": [d.isoformat() for d in self.tail_gaps],
            "value_mismatch": list(self.value_mismatch),
            "coverage_gap": list(self.coverage_gap),
            "snapshot_polluted": [d.isoformat() for d in self.snapshot_polluted],
            "unknown_sentinel": [d.isoformat() for d in self.unknown_sentinel],
            "no_authority": [d.isoformat() for d in self.no_authority],
            "latest_present": self.latest_present.isoformat() if self.latest_present else None,
            "calendar_checked": self.calendar_checked,
            "notes": list(self.notes),
        }


def _present_days(table_dir: Path) -> set[date]:
    """湖里出现过数据的交易日集合（只读 trade_date 一列去重）。"""
    files = _iter_parquet(table_dir)
    if not files:
        return set()
    lf = _scan(files)
    schema = lf.collect_schema()
    if "trade_date" not in schema.names():
        return set()
    expr = _to_date_expr(schema["trade_date"], pl.col("trade_date"))
    df = lf.select(expr.alias("trade_date")).drop_nulls().unique().collect()
    return set(df["trade_date"].to_list())


def integrity_report(
    table_dir: Path | str,
    *,
    authority: AuthoritySource | None = None,
    calendar_days: Iterable[date] | None = None,
    days: Iterable[date] | None = None,
    today: date | None = None,
    lookback_days: int = 10,
    value_col: str = "close",
    tolerance: float = HALF_TICK,
) -> IntegrityReport:
    """汇总最近窗口的「尾部缺口 / 值级不一致 / 快照污染」。

    Args:
        table_dir: 日线表根（如 ``<lake>/daily``）。
        authority: 权威来源（缺省则值级/覆盖比对如实报 ``no_authority``）。
        calendar_days: 交易日列表。给了才能报尾部缺口（没有日历就无法判定
            「哪天本该有数据」）；不给则只做已有分区的质量检查。
        days: 显式限定要检查的交易日（优先级最高）。
        today: 「今天」；缺省用上海当日。
        lookback_days: 日历给定时，候选窗口 = ``[today-lookback, today]``。

    尾部缺口的口径：候选窗口内、湖里没有、且**晚于湖内最新日期**的交易日。
    内部空洞（早于最新日期的缺失日）**刻意不报** —— 那是历史遗留问题，
    数量多、处置路径不同，混进来会让近端事故淹没在噪声里（尾部才是
    「同步最近停了/写坏了」的信号）。
    """
    from lquant.core.types import today_cn

    root = Path(table_dir)
    today = today or today_cn()
    window_start = today - timedelta(days=lookback_days)
    present = _present_days(root)
    latest = max(present) if present else None

    cal: list[date] | None = None
    if calendar_days is not None:
        cal = sorted(set(calendar_days))
    calendar_checked = cal is not None

    notes: list[str] = []
    if days is not None:
        candidates = sorted(set(days))
        cal_window: list[date] | None = (
            [d for d in cal if d in set(candidates)] if cal is not None else None
        )
    elif cal is not None:
        cal_window = [d for d in cal if window_start <= d <= today]
        candidates = [d for d in cal_window if d in present]
    else:
        cal_window = None
        candidates = sorted(d for d in present if window_start <= d <= today)

    tail_gaps: list[date] = []
    if cal_window is not None:
        if latest is None:
            # 湖里一段数据都没有：可能是从未同步（覆盖策略），也可能是被清空。
            # 不判定尾部缺口 —— 无法区分这两种语义，瞎报会把「首次启动」变告警。
            notes.append("窗口内湖里无任何数据 —— 不判定尾部缺口（可能是从未同步）")
        else:
            tail_gaps = [d for d in cal_window if d not in present and d > latest]
            skipped = [d for d in cal_window if d not in present and d <= latest]
            if skipped:
                notes.append(
                    f"另有 {len(skipped)} 个内部空洞日早于湖内最新日期 {latest}"
                    "，按口径不在此报告"
                )
    else:
        notes.append("未提供交易日历 —— 无法判定尾部缺口，仅做已有分区的质量检查")

    if latest is not None and latest > today:
        notes.append(f"湖内出现未来日期 {latest}（晚于今天 {today}），时间口径可疑")

    value_mismatch: list[dict[str, Any]] = []
    coverage_gap: list[dict[str, Any]] = []
    snapshot_polluted: list[date] = []
    unknown_sentinel: list[date] = []
    no_authority: list[date] = []
    authority_seen = False

    for day in candidates:
        try:
            verdict = partition_is_snapshot(root, day)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{day} 哨兵判定失败：{type(e).__name__}: {e}")
            verdict = None
        if verdict is not None:
            if verdict.is_snapshot:
                snapshot_polluted.append(day)
            elif verdict.is_unknown:
                unknown_sentinel.append(day)

        try:
            value = check_partition_values(
                root, day, authority, value_col=value_col, tolerance=tolerance
            )
        except Exception as e:  # noqa: BLE001
            notes.append(f"{day} 值级比对失败：{type(e).__name__}: {e}")
            continue
        if value.status == VALUE_MISMATCH:
            value_mismatch.append(value.to_dict())
        elif value.status in (VALUE_NO_AUTHORITY, VALUE_SELF_REFERENCE,
                              VALUE_EMPTY_AUTHORITY):
            no_authority.append(day)
        elif value.status == VALUE_OK:
            authority_seen = True

        try:
            coverage = check_partition_coverage(root, day, authority)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{day} 覆盖比对失败：{type(e).__name__}: {e}")
            continue
        if coverage.status == COVERAGE_GAP:
            coverage_gap.append(coverage.to_dict())

    if no_authority and not authority_seen:
        notes.append(
            f"{len(no_authority)} 个交易日因缺少权威来源无法做值级比对 —— "
            "未提供 authority 时这是预期结果，不是「数据干净」"
        )

    window = (min(candidates), max(candidates)) if candidates else None
    return IntegrityReport(
        table_dir=str(root),
        window=window,
        tail_gaps=tuple(tail_gaps),
        value_mismatch=tuple(value_mismatch),
        coverage_gap=tuple(coverage_gap),
        snapshot_polluted=tuple(snapshot_polluted),
        unknown_sentinel=tuple(unknown_sentinel),
        no_authority=tuple(no_authority),
        latest_present=latest,
        calendar_checked=calendar_checked,
        notes=tuple(notes),
    )
