"""ETF 专属因子：标的池 / 面板 / 因子清单 / 评价。

为什么必须单独一套，不能复用个股链路：

1. 日线湖 ``read_daily()`` **不按 ``sec_type`` 过滤** —— ETF 与个股混在同一张表里。
   直接拿去做因子评价，横截面里一半是股票，IC 没有任何意义。
2. ETF 没有流通市值（``float_mv`` 全 NULL），个股默认协变量
   ``market_cap`` / ``industry_sw1`` 对它覆盖率恒为 0。沿用默认口径**不会报错**，
   只会静默变成「不中性化」—— 正是本仓库反复在防的那类退化。
3. ETF 横截面很薄（全市场数百只、单基金类型几十只），评级阈值不能照搬个股。
4. ETF 的基金类型（equity/qdii/bond/commodity/money）是**截面分组**的一等维度：
   同类型内部比较才公平（跨境 / 债券 / 商品 ETF 的收益结构与股票 ETF 完全不同）。

因此本模块自带：ETF 标的池解析（可继续按基金类型细分）、ETF 面板构建
（强制 ETF 过滤 + join ``etf_meta``）、ETF 因子清单（``config/factors/etf.yaml``）、
以及 ETF 口径的评价入口 ``evaluate_etf``。

回测侧**无需改规则** —— ``backtest.rules.model.RuleSet.for_symbol`` 已按
``parse_symbol().sec_type`` 自动选 ``etf:`` 段（免印花税/过户费、T+N by fund_type、
涨跌停 by_track_index），ETF 标的进去就自动用对规则。

数据边界（明确说明，避免被当成实现缺陷）：只用日线湖 + ``etf_meta``。
日线湖**没有** IOPV / 折溢价 / 份额的日频序列，所以「折溢价套利」类因子暂不可得；
要做需要先补数据管道（``market_snapshot`` 只有实时快照，不落历史）。
"""
from __future__ import annotations

import math
from pathlib import Path

import polars as pl

from lquant.core.types import SecType, parse_symbol
from lquant.factors.fields import daily_fields

# ─────────────────────────── ETF 标的池 ───────────────────────────
#
# 池名用 ``etf_`` 前缀，与个股指数池（hs300/zz500/…）在同一张选项表里共存，
# 但语义完全独立：个股池用「指数成分」，ETF 池用「基金类型」。
#
# (别名, fund_type, 展示名) —— fund_type=None 表示不筛（全 ETF）。
# fund_type 取值对齐 ``etf_meta.fund_type`` 与 ``rules/cn_a_share.yaml`` 的
# ``t_plus`` 分档（qdii/commodity/bond/money/equity）。
ETF_UNIVERSE_OPTIONS: tuple[tuple[str, str | None, str], ...] = (
    ("etf_all", None, "全部ETF"),
    ("etf_equity", "equity", "股票型ETF"),
    ("etf_qdii", "qdii", "跨境ETF"),
    ("etf_bond", "bond", "债券ETF"),
    ("etf_commodity", "commodity", "商品ETF"),
    ("etf_money", "money", "货币ETF"),
)

_ETF_BY_KEY: dict[str, str | None] = {k: ft for k, ft, _ in ETF_UNIVERSE_OPTIONS}
_ETF_LABEL_BY_KEY: dict[str, str] = {k: label for k, _, label in ETF_UNIVERSE_OPTIONS}

#: ``etf_meta`` 里对因子/分析有意义、需要挂到面板上的列。
#: 数值列进字段白名单供 DSL 引用（规模/份额/费率）；``fund_type`` 是类别量，
#: 只做分组键（``by_group`` / ``cat_col``），不做 ``$字段``。
ETF_EXTRA_FIELDS: tuple[str, ...] = (
    "fund_size",
    "share_outstanding",
    "management_fee",
    "custody_fee",
)

#: ``etf_meta`` 挂载到面板时的列与 dtype（空表也要补列，否则下游字段解析会飘）。
_ETF_META_FIELDS: tuple[str, ...] = (
    "fund_type",
    "track_index",
    "fund_size",
    "share_outstanding",
    "management_fee",
    "custody_fee",
    "sellable_after_days",
)
_META_DTYPE: dict[str, pl.DataType] = {
    "fund_type": pl.Utf8,
    "track_index": pl.Utf8,
    "fund_size": pl.Float64,
    "share_outstanding": pl.Float64,
    "management_fee": pl.Float64,
    "custody_fee": pl.Float64,
    "sellable_after_days": pl.Int64,
}

#: ETF 默认中性化协变量。**刻意不含** ``market_cap`` / ``industry_sw1``：
#: ETF 无流通市值、无申万行业分类，挂上去只会得到覆盖率 0 的「假中性化」。
#: 规模用 ETF 自己的 ``fund_size``（log1p），再叠换手与动量两个风格维度。
ETF_DEFAULT_COVS: tuple[str, ...] = ("etf_fund_size", "turnover_1m", "momentum_1m")

#: ETF 因子定义与评级阈值（相对 ``config/``）。
ETF_FACTORS_YAML = "factors/etf.yaml"
ETF_RATING_YAML = "factors/rating_etf.yaml"


def is_etf_universe(universe: str | None) -> bool:
    """池名是否是 ETF 池（``etf_`` 前缀 + 在册）。"""
    if not universe:
        return False
    return universe.strip().lower() in _ETF_BY_KEY


def etf_universe_options() -> list[tuple[str, str | None, str]]:
    """ETF 池选项 ``[(别名, None, 展示名)]``，供 API/前端枚举（无指数代码）。"""
    return [(k, None, label) for k, _, label in ETF_UNIVERSE_OPTIONS]


def etf_universe_label(universe: str) -> str:
    """池名 → 中文展示名；未知池名原样返回。"""
    key = (universe or "").strip().lower()
    return _ETF_LABEL_BY_KEY.get(key, (universe or "").strip())


def is_etf_symbol(symbol: str) -> bool:
    """代码是否 ETF/LOF —— 与回测规则同一套判定（``parse_symbol().sec_type``）。

    绝不用「代码段硬编码」另起一套：``core.types.Symbol.sec_type`` 已是全平台
    唯一口径（沪 50/51/56/58、深 15/16/159），另写一份必然漂移。
    """
    try:
        return parse_symbol(symbol).sec_type in (SecType.ETF, SecType.LOF)
    except ValueError:
        return False


def _dedup_sorted(symbols) -> list[str]:
    return sorted({str(s) for s in symbols if s})


def _read_etf_meta() -> pl.DataFrame:
    """读 ``etf_meta``；表缺失/未 init_db → 空表（带 schema），不抛。"""
    from lquant.data.schema import ETF_META

    try:
        from lquant.data.store.catalog import EtfMetaRepo

        df = EtfMetaRepo().all()
    except Exception:  # noqa: BLE001 - 表不存在/未初始化：按「未同步」处理
        return pl.DataFrame(schema=ETF_META)
    return df if df is not None else pl.DataFrame(schema=ETF_META)


def _read_security_etf_symbols() -> list[str]:
    try:
        from lquant.data.store.catalog import SecurityRepo

        return list(SecurityRepo().etf_symbols())
    except Exception:  # noqa: BLE001
        return []


def _read_daily_etf_symbols() -> list[str]:
    """最后一级兜底：扫日线湖，按代码段挑 ETF。"""
    try:
        from lquant.data.store.parquet import read_daily

        lf = read_daily()
        symbols = lf.select("symbol").unique().collect()["symbol"].to_list()
    except Exception:  # noqa: BLE001
        return []
    return [s for s in symbols if is_etf_symbol(s)]


def resolve_etf_universe(
    universe: str,
    *,
    etf_meta: pl.DataFrame | None = None,
    security_symbols: list[str] | None = None,
    daily_symbols: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """ETF 池 → (标的清单, 降级披露)；无池名非法即抛。

    三级数据来源，逐级降级且**每一级都显式披露**（绝不静默冒充）：

    1. ``etf_meta``（首选，支持按 fund_type 细分）；
    2. ``security`` 表（``sec_type IN ('etf','lof')``）—— etf_meta 未同步时；
    3. 日线湖代码段 —— 前两者都空时。

    唯一「有池但取不到子集」的情形（请求 fund_type 但 ``etf_meta.fund_type``
    全 NULL，或该类型一只都没有）退化为全 ETF 口径，并在第二个返回值里写明
    原因 —— 调用方必须把它并入报告口径标签。

    ``etf_meta`` / ``security_symbols`` / ``daily_symbols`` 可注入，便于离线测试。
    """
    key = (universe or "").strip().lower()
    if key not in _ETF_BY_KEY:
        known = "/".join(k for k, _, _ in ETF_UNIVERSE_OPTIONS)
        raise ValueError(f"未知 ETF 池 {universe!r}（可用: {known}）")
    fund_type = _ETF_BY_KEY[key]

    meta = _read_etf_meta() if etf_meta is None else etf_meta
    if meta is not None and len(meta) and "symbol" in meta.columns:
        symbols = meta["symbol"].to_list()
        if fund_type is None:
            return _dedup_sorted(symbols), None
        if "fund_type" not in meta.columns:
            return _dedup_sorted(symbols), "etf_meta 无 fund_type 列，已按全 ETF 口径"
        if meta["fund_type"].null_count() == len(meta):
            return _dedup_sorted(symbols), "etf_meta.fund_type 未同步，已按全 ETF 口径"
        picked = meta.filter(pl.col("fund_type") == fund_type)["symbol"].to_list()
        if picked:
            return _dedup_sorted(picked), None
        return _dedup_sorted(symbols), (
            f"etf_meta 中无 fund_type={fund_type} 的 ETF，已按全 ETF 口径"
        )

    sec = security_symbols if security_symbols is not None else _read_security_etf_symbols()
    sec = [s for s in (sec or []) if is_etf_symbol(s)]
    if sec:
        return _dedup_sorted(sec), "etf_meta 未同步，已按 security 表 ETF/LOF 兜底"
    daily = daily_symbols if daily_symbols is not None else _read_daily_etf_symbols()
    # 兜底分支也必须过一遍 ``is_etf_symbol``：日线湖里个股与 ETF 混在一起，
    # 「ETF 池里绝不出现个股」这条不变量不能因为数据来源不同而失效。
    daily = [s for s in (daily or []) if is_etf_symbol(s)]
    if daily:
        return _dedup_sorted(daily), (
            "etf_meta/security 均未同步，已按日线湖 ETF 代码段兜底"
        )
    raise ValueError(
        "ETF 池为空：etf_meta / security / 日线湖都没有 ETF 标的 —— "
        "先同步 ETF 日线与元数据（lq data bootstrap / lq data etf）"
    )


# ─────────────────────────── ETF 面板 ───────────────────────────


def etf_allowed_fields() -> set[str]:
    """ETF 上下文可用的 DSL 字段白名单 = 日线字段 ∪ ETF 元数据字段。

    **刻意不改** ``factors/fields.py`` 的全局白名单：那里是股票画布的字段源，
    把 ETF 字段塞进去会让股票因子编辑页出现「选了也永远是 null」的列。
    ETF 路径显式传本函数结果给 ``check(path, allowed_fields=…)`` 即可。
    """
    return daily_fields() | set(ETF_EXTRA_FIELDS)


def _ensure_meta_cols(df: pl.DataFrame) -> pl.DataFrame:
    """补齐缺失的 ETF 元数据列（空值），保证面板列集合稳定。"""
    missing = {c: _META_DTYPE[c] for c in _ETF_META_FIELDS if c not in df.columns}
    if not missing:
        return df
    return df.with_columns([pl.lit(None, dtype=dt).alias(c) for c, dt in missing.items()])


def attach_etf_meta(df: pl.DataFrame, meta: pl.DataFrame | None = None) -> pl.DataFrame:
    """把 ``etf_meta`` 的字段按 symbol 广播到面板（left join，不改行数）。

    注意：这是**当下快照**（``etf_meta.as_of``），不是 PIT 序列 —— 基金规模/份额
    只有最新值。用它做中性化/规模因子时，结论只对「近期」成立；报告口径里要写明。
    """
    if df is None or not len(df):
        return df
    if meta is None:
        meta = _read_etf_meta()
    cols = [c for c in _ETF_META_FIELDS if meta is not None and c in meta.columns]
    if not cols or "symbol" not in meta.columns:
        return _ensure_meta_cols(df)
    # etf_meta.symbol 是主键 → 每个 symbol 一行；unique 仅作防御（此处 join 不依赖行序）。
    slim = meta.select(["symbol", *cols]).unique(subset=["symbol"], keep="first")
    return _ensure_meta_cols(df.join(slim, on="symbol", how="left"))


def _restrict_to_etf(df: pl.DataFrame, symbols: list[str] | None = None) -> pl.DataFrame:
    """只保留 ETF/LOF 行 —— 防住「read_daily 不过滤 sec_type」这个坑。"""
    if df is None or not len(df):
        return df
    present = set(df["symbol"].unique().to_list())
    keep = {s for s in present if is_etf_symbol(s)}
    if symbols is not None:
        keep &= set(symbols)
    return df.filter(pl.col("symbol").is_in(sorted(keep)))


def build_etf_panel(
    *,
    start=None,
    end=None,
    symbols: list[str] | None = None,
    universe: str | None = None,
    covs: list[str] | None = None,
    etf_meta: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, list[str], dict]:
    """构建 ETF 分析面板：ETF 过滤 + etf_meta 挂载 + 协变量。

    返回 ``(面板, 已挂载的 cov_* 列名, info)``；``info`` 含池口径、降级披露、
    标的数、以及**每个协变量的覆盖率**（覆盖率是结论的一部分，必须显式回传）。

    ``symbols`` 与 ``universe`` 二选一：给了 symbols 就用它（不再解析池），
    否则按 ``universe`` 解析。两者都不给 → 全湖 ETF。
    """
    from lquant.data.store.parquet import read_daily
    from lquant.factors.covariates import build_covariates

    # etf_meta 只读一次：池解析与面板挂载共用同一份快照，避免两次 DB 读出现
    # 「池按 A 快照筛、列按 B 快照填」的错配。
    meta = etf_meta if etf_meta is not None else _read_etf_meta()

    note = None
    if symbols is None and universe is not None:
        symbols, note = resolve_etf_universe(universe, etf_meta=meta)

    df = read_daily(symbols=symbols, start=start, end=end).collect()
    df = _restrict_to_etf(df, symbols)
    df = attach_etf_meta(df, meta)

    covs = list(covs) if covs is not None else list(ETF_DEFAULT_COVS)
    report: list[dict] = []
    if len(df):
        df, report = build_covariates(df, covs)
    present = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    info = {
        "universe": universe,
        "note": note,
        "n_symbols": int(df["symbol"].n_unique()) if len(df) else 0,
        "n_rows": len(df),
        "covariates": {r["covariate"]: r["coverage"] for r in report},
    }
    return df, present, info


# ─────────────────────────── 因子清单 ───────────────────────────


def _load_etf_raw(path: str | Path | None = None) -> dict:
    import yaml

    if path is None:
        from lquant.core.config import load_yaml

        return load_yaml(ETF_FACTORS_YAML) or {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def load_etf_factors(path: str | Path | None = None) -> list[dict]:
    """读 ``config/factors/etf.yaml`` 的 ETF 因子清单，逐条过 DSL 校验。

    校验用 ``etf_allowed_fields()``（含 ``fund_size`` 等 ETF 元数据字段）——
    用个股的 ``_daily_fields()`` 会把合法 ETF 因子误判成「未知字段」。
    """
    from lquant.factors.dsl.analyzer import check
    from lquant.factors.dsl.parser import parse

    raw = _load_etf_raw(path)
    allowed = etf_allowed_fields()
    out = []
    for it in raw.get("factors") or []:
        ast = parse(it["expr"], it["name"])
        check(ast, allowed_fields=allowed)
        out.append(
            {
                "name": it["name"],
                "expression": it["expr"],
                "description": it.get("description", it.get("category", "")),
                "source": "etf_yaml",
                "source_ref": str(path or ETF_FACTORS_YAML),
                "factor_id": None,
                "category": it.get("category", ""),
                "min_window": ast.min_window,
            }
        )
    return out


def etf_preprocess_default(path: str | Path | None = None) -> list[dict] | None:
    """ETF 默认预处理配方（报告口径披露用）。"""
    raw = _load_etf_raw(path)
    steps = raw.get("preprocess_default")
    return list(steps) if steps else None


# ─────────────────────────── 因子评价 ───────────────────────────


def _jsonable(obj):
    """把评价结果里的非 JSON 原生对象转成可序列化形式。

    ``evaluate()`` 的返回值里混着 polars ``DataFrame``（衰减 profile / 暴露矩阵）、
    可能还有 numpy 标量与裸 NaN —— CLI 的 ``json.dumps`` 与 FastAPI 的响应编码
    都会直接炸。这一层在出口统一收口，CLI 与 API 就不必各自 ``default=str``
    （那会把 DataFrame 印成不可读的 repr）。
    """
    import datetime as _dt

    if isinstance(obj, pl.DataFrame):
        return [_jsonable(r) for r in obj.to_dicts()]
    if isinstance(obj, pl.Series):
        return [_jsonable(v) for v in obj.to_list()]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    try:
        f = float(obj)          # numpy 标量 / Decimal 等
    except (TypeError, ValueError):
        return str(obj)
    return f if math.isfinite(f) else None


def _etf_rating_thresholds(rating_path: str | Path | None = None):
    """ETF 评级阈值。

    ``load_thresholds(path=None)`` 读的是**个股** ``factors/rating.yaml``，
    不是我们要的 ETF 口径；所以要显式把相对配置名解析到 ``config/`` 目录
    （``load_thresholds`` 对非 None 的 path 是当普通文件路径直接读的）。
    文件缺失时 ``load_thresholds`` 内部已回退到代码默认值，不抛。
    """
    from lquant.factors.evaluate.rating import load_thresholds

    if rating_path is not None:
        return load_thresholds(str(rating_path))
    from lquant.core.config import get_settings

    return load_thresholds(str(get_settings().config_dir / ETF_RATING_YAML))


def evaluate_etf(
    expr: str,
    *,
    universe: str = "etf_all",
    start=None,
    end=None,
    n_groups: int | None = None,
    with_report: bool = False,
    with_robustness: bool = False,
    out: str | Path | None = None,
    rating_path: str | Path | None = None,
) -> dict:
    """ETF 口径的因子评价：IC/分层/衰减/归因/评级（+ 可选 HTML 报告）。

    与个股唯一的口径差异，全部集中在这里：
    - **面板**用 ``build_etf_panel``（ETF 过滤 + etf_meta + ETF 协变量）；
    - **评级阈值**走 ``config/factors/rating_etf.yaml``（ETF 横截面薄）；
    - **行业维度换成 ``fund_type``**（ETF 无申万分类）。
    """
    from lquant.factors.evaluate import evaluate as _evaluate
    from lquant.factors.evaluate.defaults import DEFAULT_DECAY_HORIZONS, DEFAULT_N_GROUPS
    from lquant.factors.mining.runner import split_dates
    from lquant.factors.mining.submit import prepare_segment

    df, cov_cols, info = build_etf_panel(start=start, end=end, universe=universe)
    if not len(df):
        raise ValueError(
            f"ETF 面板为空（池={universe}）—— 先同步 ETF 日线与元数据，"
            "或改用 lq data demo 造演示数据"
        )
    dates = sorted(df["trade_date"].unique().to_list())
    # 标签是前瞻 1 日收益：purge=1 + embargo=1 剪掉边界，否则「样本外」是假的。
    train_d, _val_d, _test_d = split_dates(dates, purge_bars=1, embargo_bars=1)
    train = prepare_segment(
        df, cov_cols, expr, train_d, horizons=list(DEFAULT_DECAY_HORIZONS)
    )
    if not len(train):
        raise ValueError("ETF train 段为空 —— 数据太短或表达式全 null")

    label = etf_universe_label(universe)
    caliber = label if not info.get("note") else f"{label}（{info['note']}）"
    th = _etf_rating_thresholds(rating_path)

    res = _evaluate(
        train,
        "f",
        "fwd_ret_1",
        n_groups=n_groups if n_groups is not None else DEFAULT_N_GROUPS,
        with_report=with_report,
        with_robustness=with_robustness,
        # ETF 无行业：归因与分组 IC 都改用基金类型，避免传 industry_sw1 后整段静默消失
        cat_col="fund_type",
        group_col="fund_type",
        expr=expr,
        covs=cov_cols,
        universe=caliber,
        data_start=start,
        n_samples=len(train),
        covariates=info.get("covariates"),
        rating_thresholds=th,
    )
    res["etf"] = {
        "universe": universe,
        "label": label,
        "note": info.get("note"),
        "n_symbols": info.get("n_symbols"),
        "covariates": info.get("covariates"),
        "preprocess": etf_preprocess_default(),
    }
    if with_report and out:
        from lquant.factors.evaluate import save_report

        p = save_report(res["report"], Path(out))
        res["report_path"] = str(p)
    # HTML 报告是长字符串，保留原样；其余 polars/numpy/NaN 一律归一成 JSON 原生。
    report_html = res.pop("report", None)
    out_res = _jsonable(res)
    if report_html is not None:
        out_res["report"] = report_html
    return out_res


__all__ = [
    "ETF_UNIVERSE_OPTIONS",
    "ETF_EXTRA_FIELDS",
    "ETF_DEFAULT_COVS",
    "ETF_FACTORS_YAML",
    "ETF_RATING_YAML",
    "attach_etf_meta",
    "build_etf_panel",
    "etf_allowed_fields",
    "etf_preprocess_default",
    "etf_universe_label",
    "etf_universe_options",
    "evaluate_etf",
    "is_etf_symbol",
    "is_etf_universe",
    "load_etf_factors",
    "resolve_etf_universe",
]
