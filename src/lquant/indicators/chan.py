"""缠论（缠中说禅）日线结构 —— czsc 适配层。

**为什么单独一个模块、不进 ``INDICATORS`` 注册表**：注册表契约是「每行一个标量列」，
而缠论产出是**分段结构**（分型 / 笔 / 中枢 / 背驰 / 买卖点）。这正是
:mod:`lquant.indicators.registry` 模块注释里写的「中枢、浪型这类分段结构用
``Ts_*`` 算子表达不出来」。所以缠论走独立引擎 + 独立 API + 独立前端面板。

**为什么用 czsc 而不是自研**：缠论算法细节多、流派多、易错。czsc 是 Apache-2.0、
专为 A 股、活跃维护，且与 lquant 同为「Rust 核 + pyo3 + polars」架构。两者共存
没有问题 —— 边界是 ``format_standard_kline(pandas_df) -> list[RawBar]``，
**不跨界传 polars**（czsc 的 Rust 核编译期锁 polars 0.52 只存在于它自己的 ``.so`` 内）。

**口径的三条硬约束**（每条都有实测支撑，见 ``docs/superpowers/specs/
2026-10-10-chan-theory-daily-design.md`` §3）：

1. **只用 ``bi_list[:-1]``，不用 ``finished_bis``**。czsc 自带的「已确认」是启发式
   （``bars_ubi.len() < 5`` 时丢末笔），实测**不满足前缀不变性**（26 个检查点 3 处违例）——
   它会把一个仍可能被推翻的笔当成已确认输出，后续数据到来时该笔消失，正是未来函数。
   无条件丢末笔才真正满足「已确认的笔永不改变」（实测 0 违例）。
2. **中枢自建**：``get_zs_seq(bi_list[:-1])``，不用 ``c.zs_list``（它基于 ``finished_bis``，
   继承了上一条的违例）。再过滤 ``len(z.bis) >= 3``：``get_zs_seq`` 用单笔起头再延伸，
   末位中枢常只有 1-2 笔，此时 ``zg``/``zd`` 是退化值。
3. **中枢的「延伸」不是违例**：同一中枢的 ``edt``/``n_bis`` 会随时点增长，
   前缀不变性只约束 ``sdt``/``zg``/``zd``（实测三者完全稳定）。

**MACD 口径差异**：czsc 的柱是 ``DIF − DEA``（**未 ×2**，TA-Lib 口径），本仓
:func:`lquant.indicators.trend.add_macd` 是国内的 ``2 × (DIF − DEA)``。背驰判据比的是
**面积比值**，同倍缩放不改变比值，故结论不受影响 —— 但该差异会在 ``notes`` 里如实透出。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import polars as pl

__all__ = [
    "DEFAULT_SIGNAL_KINDS",
    "SIGNAL_INIT_N",
    "ChanBi",
    "ChanFx",
    "ChanResult",
    "ChanSignalEvent",
    "ChanUnavailable",
    "ChanZs",
    "compute_chan",
    "tail_result",
    "warmup",
]

#: 日线周期名（czsc 的 ``_FREQ_MAP`` 键）。
_FREQ = "日线"

#: 构成一笔所需的最少**合并后** K 线根数（czsc 默认 6）。显式传入，
#: 不依赖 czsc 的 ``CZSC_MIN_BI_LEN`` 环境变量 —— 环境变量会让行为不可复现。
DEFAULT_MIN_BI_LEN = 6

#: 保留的最大笔数。czsc 默认 50 会从头部裁剪笔、并连带丢弃更早的原始 K 线，
#: 导致中枢序列被截断。取 200 保证历史结构完整。
DEFAULT_MAX_BI_NUM = 200

#: 最少需要的日线根数（不足则无法构成分型/笔）。
MIN_BARS = 30

#: 中枢至少要有几笔才有意义（``zg``/``zd`` 由前 3 笔定义）。
MIN_ZS_BIS = 3

#: czsc ``generate_czsc_signals`` 的 ``init_n``：**跳过的前 N 根**（不是「预热根数」）。
#: 取 0 = 全窗口都出信号。实测 ``init_n`` **不改变信号取值**（300 个共同交易日 0 差异），
#: 只决定输出从哪根开始，且各取值都满足前缀不变性。
SIGNAL_INIT_N = 0

DEFAULT_SIGNAL_KINDS: tuple[str, ...] = (
    "beichi", "buy1", "sell1", "buy2", "sell2", "buy3", "sell3",
)


@dataclass(frozen=True)
class _KindSpec:
    """一个信号种类的定位方式。

    ``tag`` 是 czsc 信号版本号（如 ``V230422``）。用它**子串匹配**输出列名，
    而不是硬编码 ``日线_D1T50MACD面积背驰_BS辅助V230422`` 这种完整列名 ——
    后者会随 freq / 参数变化而失效。版本号在 czsc 内部唯一且稳定。
    """

    kind: str
    label: str
    tag: str
    values: tuple[str, ...]


#: kind → 信号种类。``values`` 是该种类下**算数**的 ``v1`` 取值（其余为「其他」）。
_KIND_SPECS: tuple[_KindSpec, ...] = (
    _KindSpec("beichi", "背驰", "V230422", ("上涨", "下跌")),
    _KindSpec("buy1", "一买", "V221126", ("一买",)),
    _KindSpec("sell1", "一卖", "V221126", ("一卖",)),
    _KindSpec("buy2", "二买", "V230320", ("二买",)),
    _KindSpec("sell2", "二卖", "V230320", ("二卖",)),
    _KindSpec("buy3", "三买", "V230318", ("三买",)),
    _KindSpec("sell3", "三卖", "V230318", ("三卖",)),
)

#: czsc ``signals_config``：去重后的信号函数清单（一个函数可能产出买卖两个方向）。
_SIGNALS_CONFIG: tuple[dict[str, str], ...] = (
    {"name": "zdy_macd_bc_V230422", "freq": _FREQ},
    {"name": "cxt_first_buy_V221126", "freq": _FREQ},
    {"name": "cxt_first_sell_V221126", "freq": _FREQ},
    {"name": "cxt_second_bs_V230320", "freq": _FREQ},
    {"name": "cxt_third_bs_V230318", "freq": _FREQ},
)

_NOTES: tuple[str, ...] = (
    "线段未实现：czsc 1.0.1 无线段（XD）实现，本页为 笔 → 中枢 直连。",
    "MACD 柱口径：背驰信号用 DIF−DEA（未×2，TA-Lib 口径），"
    "与本页 MACD 图的国内口径 2×(DIF−DEA) 不同；背驰比的是面积比值，结论不受影响。",
    "仅展示已确认结构：最新一笔（未完成笔）不参与，结构不会随后续数据改变。",
)


class ChanUnavailable(RuntimeError):
    """缠论不可用（未安装 czsc / 数据不足）。

    ``reason`` 是**面向用户**的原因文本，API 层直接透出，不静默补默认值。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ChanFx:
    """分型。``value`` 顶分型取中间 K 线高点、底分型取低点。"""

    dt: date
    mark: Literal["G", "D"]
    value: float


@dataclass(frozen=True)
class ChanBi:
    """笔。``n_bars`` 是合并后 K 线根数（czsc 的 ``length``）。"""

    sdt: date
    edt: date
    direction: Literal["up", "down"]
    high: float
    low: float
    n_bars: int


@dataclass(frozen=True)
class ChanZs:
    """中枢。``zg``/``zd`` 由前 3 笔冻结，``gg``/``dd`` 随延伸扩张。"""

    sdt: date
    edt: date
    zg: float
    zd: float
    gg: float
    dd: float
    n_bis: int


@dataclass(frozen=True)
class ChanSignalEvent:
    """信号**事件**（沿变点），不是状态。

    实测同一信号可连续多日保持同值（三卖曾持续 57 天），因此只在
    「上一行 ≠ 该值 且 本行 = 该值」时记一条，避免图上被状态点淹没。
    """

    dt: date
    kind: str
    label: str
    detail: str


@dataclass(frozen=True)
class ChanResult:
    """缠论日线结构的完整快照。"""

    symbol: str
    asof: date | None
    n_bars: int
    fx: tuple[ChanFx, ...]
    bi: tuple[ChanBi, ...]
    zs: tuple[ChanZs, ...]
    latest: dict[str, str | None]
    events: tuple[ChanSignalEvent, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready（``date`` → ``"YYYY-MM-DD"``）。"""
        return {
            "symbol": self.symbol,
            "freq": _FREQ,
            "asof": self.asof.isoformat() if self.asof else None,
            "available": True,
            "n_bars": self.n_bars,
            "fx": [{"dt": f.dt.isoformat(), "mark": f.mark, "value": f.value}
                   for f in self.fx],
            "bi": [{"sdt": b.sdt.isoformat(), "edt": b.edt.isoformat(),
                    "direction": b.direction, "high": b.high, "low": b.low,
                    "n_bars": b.n_bars} for b in self.bi],
            "zs": [{"sdt": z.sdt.isoformat(), "edt": z.edt.isoformat(),
                    "zg": z.zg, "zd": z.zd, "gg": z.gg, "dd": z.dd,
                    "n_bis": z.n_bis} for z in self.zs],
            "signals": {
                "latest": dict(self.latest),
                "events": [{"dt": e.dt.isoformat(), "kind": e.kind,
                            "label": e.label, "detail": e.detail}
                           for e in self.events],
            },
            "notes": list(self.notes),
        }


def _import_czsc() -> Any:
    """惰性导入 czsc（未安装时给出可执行的补救提示）。

    函数级 import 是有意的：czsc 是 optional extra，未装时平台其余部分零影响，
    且服务启动不必付它的导入开销（本仓惯例，见 ``pyproject.toml`` 的 PLC0415 注释）。
    """
    try:
        import czsc  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 由测试 monkeypatch 覆盖
        raise ChanUnavailable(
            "未安装 czsc 依赖，请执行 `uv sync --extra chan` 后重启服务"
        ) from exc
    return czsc


def _to_pandas(bars: pl.DataFrame, symbol: str = "") -> Any:
    """polars 日线 → czsc 要求的 pandas 8 列布局。

    czsc 的 ``format_standard_kline`` 强制要求 ``dt/symbol/open/close/high/low/vol/amount``
    八列，缺一即 ``ValueError``。本仓日线用 ``trade_date``/``volume`` 命名，且不一定有
    ``amount``，故在此统一映射；``amount`` 缺失时用 ``close × volume`` 兜底。
    """
    need = {"trade_date", "open", "high", "low", "close", "volume"}
    missing = need - set(bars.columns)
    if missing:
        raise ChanUnavailable(f"日线缺列 {sorted(missing)}，无法计算缠论")

    df = bars.select([
        pl.col("trade_date").cast(pl.Datetime).alias("dt"),
        pl.col("open").cast(pl.Float64).alias("open"),
        pl.col("close").cast(pl.Float64).alias("close"),
        pl.col("high").cast(pl.Float64).alias("high"),
        pl.col("low").cast(pl.Float64).alias("low"),
        pl.col("volume").cast(pl.Float64).alias("vol"),
        (pl.col("amount").cast(pl.Float64) if "amount" in bars.columns
         else (pl.col("close") * pl.col("volume")).cast(pl.Float64)).alias("amount"),
    ])
    pdf = df.to_pandas()
    pdf["symbol"] = symbol or "SYMBOL"
    return pdf[["dt", "symbol", "open", "close", "high", "low", "vol", "amount"]]


def _as_date(v: Any) -> date:
    """czsc 的 ``dt`` 在结构对象上是 tz-aware datetime、在信号行里是 ISO 字符串。

    两条路径都归一成 ``date``：只取日期部分，避开时区（实测 czsc 返回 ``+00:00``）。
    """
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    return v.date() if hasattr(v, "date") else v


def _extract_bi(c: Any) -> tuple[tuple[ChanBi, ...], tuple[ChanFx, ...]]:
    """取已确认的笔与分型。

    **无条件丢弃末笔**（``bi_list[:-1]``）—— 见模块 docstring 第 1 条硬约束。
    """
    bis = list(c.bi_list)[:-1]
    bi_out: list[ChanBi] = []
    fx_seen: dict[date, ChanFx] = {}
    for b in bis:
        bi_out.append(ChanBi(
            sdt=_as_date(b.sdt), edt=_as_date(b.edt),
            direction="up" if b.direction.name == "Up" else "down",
            high=float(b.high), low=float(b.low), n_bars=int(b.length),
        ))
        for f in (b.fx_a, b.fx_b):
            fx_seen.setdefault(_as_date(f.dt), ChanFx(
                dt=_as_date(f.dt),
                mark="G" if f.mark.name == "G" else "D",
                value=float(f.fx),
            ))
    fx_out = tuple(fx_seen[k] for k in sorted(fx_seen))
    return tuple(bi_out), fx_out


def _extract_zs(czsc: Any, c: Any) -> tuple[ChanZs, ...]:
    """自建中枢序列（``get_zs_seq(bi_list[:-1])``），过滤退化中枢。

    见模块 docstring 第 2 条硬约束：不用 ``c.zs_list``（它基于 ``finished_bis``）。
    """
    seq = czsc.get_zs_seq(list(c.bi_list)[:-1])
    out = []
    for z in seq:
        if len(z.bis) < MIN_ZS_BIS:
            continue
        out.append(ChanZs(
            sdt=_as_date(z.sdt), edt=_as_date(z.edt),
            zg=float(z.zg), zd=float(z.zd), gg=float(z.gg), dd=float(z.dd),
            n_bis=len(z.bis),
        ))
    return tuple(out)


def _transition_idx(seq: Sequence[str], values: tuple[str, ...]) -> list[int]:
    """沿变点下标：本行取值落在 ``values`` 内、且与**上一行**不同。

    信号是**状态**而非事件 —— 实测同一取值可连续多日保持（三卖曾持续 57 天）。
    只记沿变点，图上才不会被状态点淹没。抽成纯函数是为了不依赖 czsc 就能单测。
    """
    out: list[int] = []
    prev: str | None = None
    for i, v in enumerate(seq):
        if v in values and prev != v:
            out.append(i)
        prev = v
    return out


def _v1(row: dict, cols: Sequence[str]) -> str:
    """从一行里解析出信号取值（信号串的 ``v1`` 段），无有效值时为「其他」。"""
    for col in cols:
        val = str(row.get(col, "")).split("_")[0]
        if val and val != "其他":
            return val
    return "其他"


def _detail(row: dict, cols: Sequence[str], value: str) -> str:
    """取 ``v2`` 段作为明细（如三买的「6笔」），无意义时为空串。"""
    for col in cols:
        parts = str(row.get(col, "")).split("_")
        if parts[0] == value and len(parts) > 1 and parts[1] != "任意":
            return parts[1]
    return ""


def _signals(czsc: Any, bars: Any, kinds: Sequence[str],
             init_n: int = SIGNAL_INIT_N) -> tuple[dict, tuple]:
    """跑 czsc 信号，解析 ``v1`` 段并做**沿变检测**。

    Returns:
        ``(latest, events)`` —— ``latest`` 是 ``{kind: v1 或 None}``，
        ``events`` 是沿变点序列。
    """
    specs = [s for s in _KIND_SPECS if s.kind in kinds]
    if not specs:
        return {k: None for k in kinds}, ()

    sdt = str(bars[0].dt)[:10].replace("-", "")
    rows = czsc.generate_czsc_signals(
        bars, signals_config=list(_SIGNALS_CONFIG), sdt=sdt,
        init_n=init_n, df=False)
    if not rows:
        return {k: None for k in kinds}, ()

    # 列名按版本号子串定位（不硬编码完整列名，见 _KindSpec 的说明）
    cols = {s.kind: [c for c in rows[0] if s.tag in c] for s in specs}

    events: list[ChanSignalEvent] = []
    latest: dict[str, str | None] = {}
    for s in specs:
        seq = [_v1(r, cols[s.kind]) for r in rows]
        for i in _transition_idx(seq, s.values):
            events.append(ChanSignalEvent(
                dt=_as_date(rows[i]["dt"]), kind=s.kind, label=s.label,
                detail=_detail(rows[i], cols[s.kind], seq[i])))
        latest[s.kind] = seq[-1] if seq[-1] in s.values else None

    events.sort(key=lambda e: (e.dt, e.kind))
    return latest, tuple(events)


def compute_chan(
    bars: pl.DataFrame,
    *,
    symbol: str = "",
    min_bi_len: int = DEFAULT_MIN_BI_LEN,
    max_bi_num: int = DEFAULT_MAX_BI_NUM,
    signal_kinds: Sequence[str] = DEFAULT_SIGNAL_KINDS,
    signal_tail: int | None = None,
) -> ChanResult:
    """计算日线缠论结构。

    Args:
        bars: 日线，需含 ``trade_date/open/high/low/close/volume``（``amount`` 可选），
            按 ``trade_date`` 升序。
        symbol: 标的代码（仅回填进结果，czsc 内部不用）。
        min_bi_len: 成笔所需的最少合并 K 线根数。
        max_bi_num: 保留的最大笔数。
        signal_kinds: 要计算的信号种类，取值见 :data:`DEFAULT_SIGNAL_KINDS`。
        signal_tail: 只计算最后这么多根的信号。**纯性能开关，不改变取值** ——
            实测 ``init_n`` 不影响信号值（300 个共同交易日 0 差异），只决定从哪根开始算。
            全量算 7000 根信号约 1.7s，只算尾部 250 根约 78ms（22×）。
            结构（笔/中枢）仍用全量历史，故不受影响。

    Raises:
        ChanUnavailable: 未安装 czsc，或数据不足（``reason`` 面向用户）。
        ValueError: ``signal_kinds`` 含未注册的种类。
    """
    unknown = [k for k in signal_kinds if k not in {s.kind for s in _KIND_SPECS}]
    if unknown:
        raise ValueError(f"未知信号种类 {unknown}，可选: {DEFAULT_SIGNAL_KINDS}")

    czsc = _import_czsc()
    if bars.is_empty() or bars.height < MIN_BARS:
        raise ChanUnavailable(f"日线不足 {MIN_BARS} 根，无法构成笔")

    pdf = _to_pandas(bars.sort("trade_date"), symbol)
    raw = czsc.format_standard_kline(pdf, freq=_FREQ)
    c = czsc.CZSC(raw, min_bi_len=min_bi_len, max_bi_num=max_bi_num)

    bi, fx = _extract_bi(c)
    if not bi:
        raise ChanUnavailable("已确认的笔为空，日线历史太短")

    zs = _extract_zs(czsc, c)
    init_n = 0 if signal_tail is None else max(0, len(raw) - signal_tail)
    latest, events = _signals(czsc, raw, signal_kinds, init_n)

    asof = _as_date(raw[-1].dt)
    return ChanResult(
        symbol=symbol, asof=asof, n_bars=len(raw),
        fx=fx, bi=bi, zs=zs, latest=latest, events=events, notes=_NOTES,
    )


def warmup() -> bool:
    """预热缠论引擎：导入 czsc（实测约 2.3 s，**进程内只付一次**）。

    由 ``server/main.py`` 的启动钩子在 **daemon 线程**里调用 —— 把导入成本挪到
    服务启动之后，首屏请求就不必等它。不违背「重依赖惰性 import 提速启动」的约定：
    启动本身不等这个线程，且未装 czsc 时只是静默跳过。

    未安装 czsc 时返回 ``False``（不抛异常 —— optional extra 没装不是错误）。

    **只导入、不做缓存**：实测 import 之后的首次 ``compute_chan`` 与后续耗时相同
    （~140 ms），所以导入即预热完毕，不必再跑一次空计算。
    """
    try:
        _import_czsc()
    except ChanUnavailable:
        return False
    return True


def tail_result(result: ChanResult, since: date) -> ChanResult:
    """截取 ``since`` 起的结构，供前端展示窗口使用。

    过滤口径：**笔/中枢按结束日**（``edt >= since``，跨进窗口的段要保留），
    分型/事件按当日。``latest``/``notes``/``asof``/``n_bars`` 原样保留。

    注意这会顺带滤掉「历史截断边界」上可能出现的最老一个中枢 —— 那类中枢的
    ``zg``/``zd`` 依赖被截掉的前文，且远在展示窗口之外，滤掉正是想要的行为。
    """
    return ChanResult(
        symbol=result.symbol, asof=result.asof, n_bars=result.n_bars,
        fx=tuple(f for f in result.fx if f.dt >= since),
        bi=tuple(b for b in result.bi if b.edt >= since),
        zs=tuple(z for z in result.zs if z.edt >= since),
        latest=result.latest,
        events=tuple(e for e in result.events if e.dt >= since),
        notes=result.notes,
    )
