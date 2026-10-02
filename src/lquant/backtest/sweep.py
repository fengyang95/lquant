"""参数扫描（sweep）：事件引擎（真源）与 Polars 向量化快扫（近似）。

设计文档 API 契约里的 `POST /api/backtests/sweep` 是异步队列；本模块是
**纯计算核心**：输入一块行情 DataFrame + 扫描规格，输出 {value: 指标} 网格。

两条路径的输出契约完全一致（`_OUT_COLS`），但**语义权威性不同**：

- `run_sweep`：逐档调用事件引擎 `Engine`（T+1 / 涨跌停 / 手数 / 资金约束齐全），
  是**唯一执行真源（authoritative）**，最终结论必须来自它。
- `run_sweep_vectorized`：一次成型地对全部参数值做 Polars 向量化近似，
  用于万级网格**筛参数**。成本模型与撮合细节是近似的，**不得当作最终结论**。
  正确用法：向量化粗筛 → 取头部候选回事件引擎复核
  （见 `docs/BACKTEST_ENGINES.md` 的 B5）。

引擎只在意输出表的列集稳定（前端画参数-收益/回撤曲面就靠这几列），
扫描什么参数、取哪些值全由调用方决定，这里不做策略语义假设。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import polars as pl

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.rules.loader import default_slippage, load_ruleset
from lquant.server.jobs import JobCanceled

# 固定输出列：前端契约，改列名/加列都要同步更新前端
_OUT_COLS = ["value", "total_return", "annual_return", "sharpe",
             "max_drawdown", "n_trades", "turnover"]

# 年化/夏普口径与事件引擎一致（metrics.perf_from_returns 默认 252 交易日）
TRADING_DAYS = 252

# 端点自动切向量化快扫的档数阈值：档数少时用事件引擎（真源、无近似）；
# 超过该档数说明调用方要的是「大网格粗筛」，用向量化路径。
# 必须 ≤ SweepIn.values 的 max_length（20），否则端点永远选不到向量化。
VECTOR_AUTO_MIN_POINTS = 12


@dataclass
class SweepSpec:
    """扫描公共段：除被扫参数外，其余回测参数固定在这里。"""

    factor: str                                   # 因子列名（需已在 data 里）
    rebalance: str = "daily"
    initial_cash: float = 1_000_000.0
    max_position_weight: float = 1.0


def _metrics_res(metrics: dict, value) -> dict:
    m = metrics
    # turnover 是 {n_trades, total_amount, turnover_per_period} 结构 ——
    # 但这里是「参数-收益/回撤曲面」的固定列契约，必须展平成单标量。
    tov = m.get("turnover") or {}
    return {
        "value": value,
        "total_return": m.get("total_return"),
        "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe"),
        "max_drawdown": m.get("max_drawdown"),
        "n_trades": m.get("n_trades", 0),
        "turnover": tov.get("turnover_per_period") if isinstance(tov, dict) else m.get("turnover"),
    }


def run_sweep(data: pl.DataFrame, param: str, values: list,
              spec: SweepSpec, *, strategy_cls=None,
              strategy_kwargs: dict | None = None,
              cancel_check=None) -> pl.DataFrame:
    """【执行真源】对 `param` 在 `values` 上逐档跑事件引擎，返回网格表。

    `strategy_cls` 构造签名需接受 `{**strategy_kwargs, param: value}`；
    默认是 factor_topn（支持 factor= 与 top_n=）。
    cancel_check：每档开始前轮询，返回 True 时抛 JobCanceled 协作式收尾。

    这是**权威路径**：T+1 可卖、涨跌停/停牌拒单、手数取整、资金不足拒单等
    全都在 `Engine` 里真实撮合。向量的 `run_sweep_vectorized` 只用于筛参数。
    """
    from lquant.backtest.security_meta import load_security_meta
    from lquant.backtest.strategy.factor_topn import FactorTopNStrategy

    cls = strategy_cls or FactorTopNStrategy
    base = dict(strategy_kwargs or {})
    # security 元数据只读一次，复用给每档 —— 否则 N 档就是 N 次全表查询
    meta = load_security_meta()

    rows = []
    for v in values:
        if cancel_check is not None and cancel_check():
            raise JobCanceled(f"参数扫描在第 {len(rows)}/{len(values)} 档被取消")
        kw = {**base, param: v}          # 未知参数会在构造时 KeyError → 快失败
        strat = cls(factor=spec.factor, **kw)
        res = Engine(strat, config=EngineConfig(
            initial_cash=spec.initial_cash, rebalance=spec.rebalance,
            max_position_weight=spec.max_position_weight,
        ), meta=meta, with_db_meta=False).run(data, extra_fields=[spec.factor])
        rows.append(_metrics_res(res.metrics, v))

    # 固定列序（_metrics_res 的键序即 _OUT_COLS）；values 非空时 rows 至少一行
    return pl.DataFrame(rows, orient="row").select(*_OUT_COLS)


# --------------------------------------------------------------------------- #
# 向量化近似快扫（B5）
# --------------------------------------------------------------------------- #

def _empty_grid() -> pl.DataFrame:
    """空网格：列集/类型与 `_OUT_COLS` 一致（前端契约不因空扫而变）。"""
    return pl.DataFrame(schema={
        "value": pl.Int64, "total_return": pl.Float64, "annual_return": pl.Float64,
        "sharpe": pl.Float64, "max_drawdown": pl.Float64, "n_trades": pl.Int64,
        "turnover": pl.Float64,
    }).select(*_OUT_COLS)


def _approx_cost_rates(ruleset, d: date,
                       slippage_rate: float | None = None) -> tuple[float, float]:
    """把 `cn_a_share.yaml` 的成本折成近似单边线性率 (买入率, 卖出率)。

    近似点（全部在 `run_sweep_vectorized` 的文档里声明）：
    - 佣金 / 印花税 / 过户费按**成交额比例**线性化，忽略 per-order 最低佣金
      （5 元/单）：小额单成本被**低估**，但快扫只筛参数，可接受。
    - 印花税取回测**最后一天**所在税率档；跨 2023-08-28 等税率切换区间的长回测
      会略偏，向量化路径不为每档逐日取税。
    - 滑点只吸收 `pct` 模型（规则表默认）；`tick` / `volume_pct` 非线性，
      这里按 0 计（宁可少算，也不假装精确）。
    - `slippage_rate` 显式给出时覆盖规则表滑点（成本敏感性测试用）。
    """
    from lquant.core.types import Board, SecType

    inst = ruleset.for_symbol("600000.SH", SecType.STOCK, Board.MAIN)
    comm = float(inst.commission.rate)
    transfer = float(inst.transfer_fee_rate)
    try:
        stamp = float(inst.tax_rate(d, "sell"))
    except Exception:  # noqa: BLE001 - 税率表缺档时按 0 处理，不让整次快扫失败
        stamp = 0.0
    if slippage_rate is not None:
        slip = float(slippage_rate)
    else:
        mode, params = default_slippage(ruleset)
        slip = float(params.get("rate", 0.0)) if mode == "pct" else 0.0
    return comm + transfer + slip, comm + transfer + stamp + slip


def _rebalance_flags(dates: pl.DataFrame, mode: str) -> pl.DataFrame:
    """给交易日序列打调仓标记（口径对齐 `Engine._should_rebalance`）。

    daily：每个交易日；weekly：ISO 周切换首日；monthly：月切换首日；
    其它（none/未知）：从不调仓。首日恒为调仓日（与引擎 `_last_rebal_key` 初值一致）。
    """
    d = dates.sort("trade_date")
    if mode == "daily":
        return d.with_columns(pl.lit(True).alias("_rebal"))
    if mode == "weekly":
        key = (pl.col("trade_date").dt.iso_year().cast(pl.Utf8) + "-"
               + pl.col("trade_date").dt.week().cast(pl.Utf8))
    elif mode == "monthly":
        key = (pl.col("trade_date").dt.year().cast(pl.Utf8) + "-"
               + pl.col("trade_date").dt.month().cast(pl.Utf8))
    else:
        return d.with_columns(pl.lit(False).alias("_rebal"))
    return (d.with_columns(key.alias("_key"))
            .with_columns((pl.col("_key") != pl.col("_key").shift(1))
                          .fill_null(True).alias("_rebal"))
            .drop("_key"))


def run_sweep_vectorized(data: pl.DataFrame, param: str, values: list,
                         spec: SweepSpec, *,
                         strategy_kwargs: dict | None = None,
                         slippage_rate: float | None = None,
                         cancel_check=None) -> pl.DataFrame:
    """Polars 向量化近似扫参：一次成型地对全部 `values` 出网格。

    输出与 `run_sweep` 同一契约（`_OUT_COLS`），策略语义：

    - 每个调仓日按因子**降序排名**，取前 `top_n` 等权；
    - 防未来函数：T 日收盘定信号，T+1 **开盘**建仓，持有到下一个调仓日开盘；
      区间内是**买入持有**（权重随价格漂移），下一个调仓日恢复等权 ——
      与引擎「每次调仓重设到 nav/top_n」一致；
    - 区间收益用开盘价比值 `open(T+1)/open(建仓日)`，与引擎的
      「T+1 开盘成交、收盘估值」在无隔夜跳空时严格等价。

    **近似成本模型**（详见 `_approx_cost_rates`）：在每个调仓日的建仓价上，
    按换手 `turn = (top_n - 重叠数) / top_n` 线性扣除
    `turn × 买入率 + turn × 卖出率`；首次建仓只扣买入。换手就是
    「新进名单 + 离场名单」的权重占比。

    **未建模（必须在事件引擎复核）**：T+1 可卖约束、涨跌停/停牌/退市拒单、
    100 股手数取整、最低佣金、participation 成交量上限、资金不足拒单、
    除权复权、同因子并列时的稳定排序。因此本函数只用于**筛参数**。

    Parameters
    ----------
    param : 目前仅支持 `"top_n"`；其它参数抛 NotImplementedError。
    values : top_n 候选档位（整数）。
    cancel_check : 协作式取消；向量化只有少数几个阶段，在阶段间轮询。
    """
    if param != "top_n":
        raise NotImplementedError(f"向量化快扫暂只支持 param='top_n'，收到 {param!r}")
    col = spec.factor
    if col not in data.columns:
        raise KeyError(f"因子列不存在: {col}")

    def _check(stage: str) -> None:
        if cancel_check is not None and cancel_check():
            raise JobCanceled(f"参数扫描在向量化快扫（{stage}）被取消")

    _check("启动")
    _ = strategy_kwargs  # 因子/基档位不影响每档取值：被扫参数逐档覆盖

    vals = pl.DataFrame({
        "value": [int(v) for v in values],
        "_ord": list(range(len(values))),
    }).with_columns(pl.col("value").cast(pl.Int64))
    if len(vals) == 0:
        return _empty_grid()

    # ---- 排名：因子非空才进宇宙（与策略 `if fields.get(f) is not None` 一致）----
    ranked = (data.filter(pl.col(col).is_not_null())
              .with_columns(pl.col(col).rank(method="ordinal", descending=True)
                            .over("trade_date").cast(pl.Int64).alias("_rank"))
              .select(pl.col("trade_date").alias("_sig"), "symbol", "_rank"))
    k_by_sig = ranked.group_by("_sig").agg(pl.col("_rank").max().alias("_k"))
    _check("数据准备")

    # ---- 日期轴：日历调仓日 → 有效信号（当日有因子）→ 建仓日（信号次日开盘）----
    dates = (data.select(pl.col("trade_date").unique())
             .sort("trade_date")
             .with_columns(pl.col("trade_date").shift(-1).alias("_next")))
    dates = _rebalance_flags(dates, spec.rebalance)
    eff = (dates.filter(pl.col("_rebal"))
           .join(k_by_sig, left_on="trade_date", right_on="_sig", how="inner")
           .sort("trade_date")
           .select(pl.col("trade_date").alias("_sig"), "_next", "_k")
           .with_columns(pl.col("_next").alias("_exec"),
                         pl.col("_sig").shift(1).alias("_prev_sig")))
    # 每个交易日 → 生效中的信号（严格早于当天的最后一个有效信号）。
    # 无效调仓日（如因子未成熟的月初）不产生信号：引擎当日无目标，保留持仓。
    dd = (dates.with_columns(
              pl.when(pl.col("trade_date").is_in(eff["_sig"].implode()))
                .then(pl.col("trade_date")).otherwise(None).alias("_sg"))
          .with_columns(pl.col("_sg").forward_fill().shift(1).alias("_act_sig")))
    dd = dd.join(eff.select(pl.col("_sig").alias("_act_sig"), "_exec", "_k"),
                 on="_act_sig", how="left")

    # ---- 每 (交易日, 标的)：建仓开盘价、次日开盘价、在生效信号里的排名 ----
    entry = data.select(pl.col("trade_date").alias("_exec"), "symbol",
                        pl.col("open").alias("_eopen"))
    panel = (data.sort(["symbol", "trade_date"])
             .with_columns(pl.col("open").shift(-1).over("symbol").alias("_nopen"))
             .join(dd.select("trade_date", "_act_sig", "_exec", "_k"),
                   on="trade_date", how="left")
             .join(entry, on=["_exec", "symbol"], how="left")
             .join(ranked, left_on=["_act_sig", "symbol"],
                   right_on=["_sig", "symbol"], how="inner")
             .filter(pl.col("_nopen").is_not_null()
                     & pl.col("_eopen").is_not_null()
                     & (pl.col("_eopen") > 0))
             .with_columns((pl.col("_nopen") / pl.col("_eopen")).alias("_ratio")))

    # ---- 排名累积：top_n=N 组合的持有价值 = Σ_{rank≤N} ratio / min(N, k) ----
    # 一次 cum_sum 覆盖**所有档位**（集合嵌套），不对 values 做 Python 循环。
    cum = (panel.sort(["trade_date", "_rank"])
           .with_columns(pl.col("_ratio").cum_sum().over("trade_date").alias("_cum"))
           .select("trade_date", "_rank", "_cum", "_k"))
    v = (cum.join(vals.with_columns(pl.col("value").alias("_rk")),
                  left_on="_rank", right_on="_rk", how="inner")
         .with_columns((pl.col("_cum")
                        / pl.min_horizontal(pl.col("value"), pl.col("_k"))).alias("v"))
         .select("trade_date", "value", "v"))

    # ---- 近似成本：相邻两个有效信号间，标的同在两个 topN 集合 ⟺ max(rank, rank_prev) ≤ N ----
    buy_rate, sell_rate = _approx_cost_rates(
        load_ruleset(), data["trade_date"].max(), slippage_rate)
    pairs = (eff.filter(pl.col("_prev_sig").is_not_null())
             .join(ranked.rename({"_rank": "r_cur"}), on="_sig", how="inner")
             .join(ranked.rename({"_sig": "_prev_sig", "_rank": "r_prev"})
                   .select("_prev_sig", "symbol", "r_prev"),
                   on=["_prev_sig", "symbol"], how="inner")
             .with_columns(pl.max_horizontal("r_cur", "r_prev").alias("_m")))
    counts = (pairs.group_by(["_exec", "_m"]).len()
              .sort(["_exec", "_m"])
              .with_columns(pl.col("len").cum_sum().over("_exec").alias("_cum")))
    turn_grid = (eff.filter(pl.col("_prev_sig").is_not_null())
                 .select(pl.col("_exec").unique())
                 .join(vals.select("value"), how="cross")
                 .sort(["_exec", "value"]))
    # join_asof：对每个 N 取「最大的 _m ≤ N」处的累计重叠数（缺失即 0）
    ov = turn_grid.join_asof(
        counts.select("_exec", "_m", "_cum").sort(["_exec", "_m"]),
        left_on="value", right_on="_m", by="_exec", strategy="backward",
        check_sortedness=False)
    normal = (ov.with_columns(
                  ((pl.col("value") - pl.col("_cum").fill_null(0))
                   / pl.col("value")).alias("_turn"))
              .select("_exec", "value",
                      pl.col("_turn").alias("_tb"), pl.col("_turn").alias("_ts")))
    # 首次建仓：只买不卖
    first = (eff.filter(pl.col("_prev_sig").is_null())
             .select(pl.col("_exec").unique())
             .join(vals.select("value"), how="cross")
             .with_columns(pl.lit(1.0).alias("_tb"), pl.lit(0.0).alias("_ts")))
    cost = (pl.concat([normal, first], how="vertical")
            .with_columns((pl.col("_tb") * buy_rate
                           + pl.col("_ts") * sell_rate).alias("_cost"))
            .select(pl.col("_exec").alias("trade_date"), "value",
                    "_tb", "_ts", "_cost"))

    # ---- 日频网格：每个交易日 × 每个参数值 ----
    grid = (dd.select("trade_date", "_exec")
            .join(vals.select("value"), how="cross")
            .sort(["value", "trade_date"])
            .join(v, on=["trade_date", "value"], how="left")
            .join(cost, on=["trade_date", "value"], how="left")
            .with_columns(pl.col("_tb").fill_null(0.0), pl.col("_ts").fill_null(0.0),
                          pl.col("_cost").fill_null(0.0)))
    # 首个有效信号之前是现金（v 缺失 → 归一为 1.0）；持有期内缺失日前值兜底
    grid = grid.with_columns(
        pl.col("v").forward_fill().over("value").fill_null(1.0))

    # ---- 日收益：换仓日以「建仓日 NAV 基准」重设并扣成本，其余日按持有价值环比 ----
    # 注意建仓日不能用 v/v_prev：每个持有期的 v 都以**该期建仓开盘价**为基准，
    # 上一期的 v（=1+上期收益）已经在 NAV 里复利过一次，再除一次会把历史收益抹掉。
    grid = (grid.with_columns(pl.col("v").shift(1).over("value").alias("_vprev"))
            .with_columns(
                pl.when(pl.col("trade_date") == pl.col("_exec"))
                  .then(pl.col("v") * (1.0 - pl.col("_cost")) - 1.0)
                  .otherwise(pl.col("v") / pl.col("_vprev").fill_null(1.0) - 1.0)
                  .alias("r")))
    grid = (grid.with_columns((1.0 + pl.col("r")).cum_prod().over("value").alias("nav"))
            .with_columns(pl.col("nav").cum_max().over("value").alias("_peak"))
            .with_columns(
                (pl.col("nav") / pl.max_horizontal(pl.col("_peak"), pl.lit(1.0))
                 - 1.0).alias("_dd"))
            .with_columns(
                ((pl.col("_tb") + pl.col("_ts")) * pl.col("nav")).alias("_traded"),
                ((pl.col("_tb") + pl.col("_ts")) * pl.col("value")).alias("_ntr")))

    # ---- 指标：与 metrics.perf_from_returns 同口径（几何年化 / ddof=1 波动 / 初始峰值）----
    agg = (grid.group_by("value").agg(
        pl.col("r").count().alias("_n"),
        (pl.col("r").std(ddof=1) * math.sqrt(TRADING_DAYS)).alias("_vol"),
        (pl.col("nav").sort_by("trade_date").last() - 1.0).alias("total_return"),
        pl.col("_dd").min().alias("max_drawdown"),
        pl.col("_traded").sum().alias("_amount"),
        pl.col("nav").mean().alias("_mean_nav"),
        pl.col("_ntr").sum().alias("_ntr"),
    ))
    agg = agg.with_columns(
        pl.when(pl.col("total_return") <= -1.0).then(None)
          .otherwise((1.0 + pl.col("total_return"))
                     .pow(TRADING_DAYS / pl.col("_n")) - 1.0).alias("annual_return"))
    agg = agg.with_columns(
        pl.when(pl.col("_vol") > 1e-12)
          .then(pl.col("annual_return") / pl.col("_vol"))
          .otherwise(float("nan")).alias("sharpe"),
        pl.when(pl.col("_n") > 0)
          .then(pl.col("_amount") / pl.col("_n") / pl.col("_mean_nav"))
          .otherwise(0.0).alias("turnover"))

    out = agg.select(
        "value", "total_return", "annual_return", "sharpe", "max_drawdown",
        pl.col("_ntr").round().cast(pl.Int64).alias("n_trades"), "turnover")
    # 保持调用方传入的档位顺序（与 run_sweep 的行序一致）
    return (out.join(vals.select("value", "_ord"), on="value", how="left")
            .sort("_ord").select(*_OUT_COLS))


def run_sweep_auto(data: pl.DataFrame, param: str, values: list,
                   spec: SweepSpec, *, engine: str = "auto",
                   strategy_cls=None, strategy_kwargs: dict | None = None,
                   slippage_rate: float | None = None,
                   cancel_check=None) -> pl.DataFrame:
    """按 `engine` 选择路径：`event` / `vector` / `auto`（按档数阈值自动切）。

    `auto` 的阈值是 `VECTOR_AUTO_MIN_POINTS`：小网格走事件引擎（真源、无近似），
    大网格走向量化快扫（近似、只筛参数）。返回 DataFrame 契约相同。
    """
    use_vector = (engine == "vector"
                  or (engine == "auto" and len(values) >= VECTOR_AUTO_MIN_POINTS))
    if use_vector:
        return run_sweep_vectorized(data, param, values, spec,
                                    strategy_kwargs=strategy_kwargs,
                                    slippage_rate=slippage_rate,
                                    cancel_check=cancel_check)
    return run_sweep(data, param, values, spec, strategy_cls=strategy_cls,
                     strategy_kwargs=strategy_kwargs, cancel_check=cancel_check)
