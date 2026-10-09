"""看板另类数据因子化（factors/sources/board.py · EP-9）。

三张看板表（money_flow / dragon_tiger / limit_up_pool）采集已久、因子层零
消费 —— 本模块把它们做成 CovariateProvider，让 G0-G3 门禁能评价情绪/资金面
因子。这里守三条命门：

1. 防前视（生命线）：看板数据 T 日定型（龙虎榜晚间公布、资金流/涨停池盘中
   会漂），所有特征按 T 日算好 → 组内 shift(1)，T+1 行的因子值只能看到
   ≤T 日的看板数据。宁可晚一天，不可用未来。
2. 语义分界：未上榜/当日无流数据 = 0（业务事实不是缺失）；表未同步 =
   CovariateUnavailable（coverage=0 显式上报，绝不填 0 冒充）。
3. G0 白名单随面板走：submit 引用 cov_ 列时，allowed = 日线字段 ∪ 实际挂载
   的 cov 列 —— 面板里有什么字段，校验就该认什么。

隔离姿势沿用 test_factor_monitor：LQ_ROOT + chdir + cache_clear + DDL 全量
建表 + ensure_market_tables（看板三张表的 DDL 由 market/schema.py 拥有，
不在 DDL_STATEMENTS 里）。symbol 用真实湖口径（600000.SH，demo 与
parse_symbol 同源），并混入脏格式覆盖 _norm_sym 防御。
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import polars as pl
import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")

DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(8)]  # 06-01..06-08
# 6 只票是底线：ic_series 的 min_obs=5 过滤每日 <5 只票的截面（3 只票算
# 相关性没有意义）—— eval 端到端要产出逐日 IC，面板每天必须 ≥5 行。
SYMS = (
    "600000.SH",
    "000001.SZ",
    "300750.SZ",
    "600036.SH",
    "000333.SZ",
    "601318.SH",
)
D1, D2, D3, D4, D5, D6, D7, D8 = DAYS


def _daily_df(days: list[date] | None = None, *, offset: int = 0) -> pl.DataFrame:
    """每股固定漂移的指数增长价（截面单调 → IC 可预期），全字段列。

    ``days`` 缺省用模块级 ``DAYS``（既有测试口径不变）；``offset`` 用于在
    既有面板之后**续造**交易日时保持价格序列连续（``i+1`` 的幂次接着涨）。
    """
    days = DAYS if days is None else days
    rows = []
    for i, d in enumerate(days, start=offset):
        for j, s in enumerate(SYMS):
            c = 10.0 * (1 + 0.001 * (j + 1)) ** (i + 1)
            rows.append(
                {
                    "trade_date": d,
                    "symbol": s,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "pre_close": c,
                    "volume": 1e5,
                    "amount": 1e5 * c,
                    "turnover_rate": 1.0 + j,
                    "adj_factor": 1.0,
                    "float_mv": 1e8 * (j + 1),
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def _seed_board_tables(con) -> None:
    """确定性场景：600000 主角（资金流 4 天 / 龙虎榜 1 天 / 三连板），000001 配角。"""
    mf = [
        (D1, "600000.SH", 0.1),
        (D2, "600000.SH", 0.2),
        (D3, "600000.SH", 0.3),
        (D4, "600000.SH", 0.4),
        # 脏格式（sz. 前缀）故意混入 —— _norm_sym 防御生效才能 join 上
        (D2, "sz.000001", 0.5),
    ]
    for d, s, r in mf:
        con.execute(
            "INSERT INTO money_flow (trade_date, symbol, main_net_ratio, collected_at) "
            "VALUES (?, ?, ?, now())",
            [d, s, r],
        )
    lhb = [
        # 采集端同票多上榜原因已合并取第一条 —— 同票同日恒一行
        (D3, "600000.SH", 150.0, "日涨幅偏离值达7%的证券"),
        (D1, "000001.SZ", -20.0, "日涨幅偏离值达7%的证券"),
    ]
    for d, s, nb, reason in lhb:
        con.execute(
            "INSERT INTO dragon_tiger (trade_date, symbol, net_buy, reason, collected_at) "
            "VALUES (?, ?, ?, ?, now())",
            [d, s, nb, reason],
        )
    zt = [
        # (date, symbol, open_count, limit_up_type) —— 真涨停行才有 limit_up_type
        (D2, "600000.SH", 1, "换手板"),
        (D3, "600000.SH", 0, "换手板"),
        (D4, "600000.SH", 2, "T字板"),
        (D6, "000001.SZ", 3, "一字板"),
        # 炸板池（曾涨停未封住）也落 limit_up_pool，且没有 limit_up_type：
        # market/collectors/__init__.py 把 broken_pool 指到同一张表，
        # fetch_broken_pool 不产出该列 → 入库 NULL。300750 连续三天在池里
        # 但一次都没封住，连板数必须恒 0（不当成三连板）。
        (D2, "300750.SZ", 1, None),
        (D3, "300750.SZ", 1, None),
        (D4, "300750.SZ", 1, None),
    ]
    for d, s, oc, ltype in zt:
        con.execute(
            "INSERT INTO limit_up_pool "
            "(trade_date, symbol, open_count, limit_up_type, collected_at) "
            "VALUES (?, ?, ?, ?, now())",
            [d, s, oc, ltype],
        )


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb

    from lquant.data.store.ddl import DDL_STATEMENTS
    from lquant.market.schema import ensure_market_tables

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    ensure_market_tables(con)
    _seed_board_tables(con)
    con.close()

    from lquant.data.store.parquet import write_daily

    write_daily(_daily_df())
    yield
    get_settings.cache_clear()


def _cov_frame(board_env, names) -> tuple[pl.DataFrame, list[dict]]:
    from lquant.data.store.parquet import read_daily
    from lquant.factors.covariates import build_covariates

    panel = read_daily().collect()
    return build_covariates(panel, names)


def _sym_col(df, cov: str) -> dict:
    """600000.SH 按 (trade_date → cov 值) 的映射。"""
    sub = df.filter(pl.col("symbol") == "600000.SH")
    return dict(zip(sub["trade_date"].to_list(), sub[cov].to_list(), strict=True))


# ---------------- 防前视：shift(1) 语义 ----------------


def test_mf_main_ratio_t_plus_1(board_env):
    """T 日资金流值出现在 T+1 行；未上榜/当日无流数据按业务语义取 0。

    旧断言 ``got[D6] is None``（T 日缺行 = 缺失留 null）锁的正是缺陷行为：
    ``money_flow`` 采集按主力净流入降序只取前 200 只，留 null 会让 OLS 中性化的
    valid_mask 把这些行整行剔除 —— 评价样本被协变量本身截断成「当日前 200 只」，
    而输出里看不到任何样本量字段。模块 docstring 与 README 写的都是取 0，
    实现必须跟文档一致。
    """
    df, _ = _cov_frame(board_env, ["mf_main_ratio"])
    got = _sym_col(df, "cov_mf_main_ratio")
    assert got[D1] is None  # 窗口首行：shift 无前值（防前视的代价，文档已声明）
    assert got[D2] == pytest.approx(0.1)  # D2 行只看得到 D1 的 0.1
    assert got[D5] == pytest.approx(0.4)  # D5 行 = D4 的 0.4
    assert got[D6] == pytest.approx(0.0)  # D5 起无资金流数据 → 业务语义取 0
    assert got[D8] == pytest.approx(0.0)


def test_mf_main_ratio_3d_rolling_then_shift(board_env):
    """3 日均值先在 T 日窗口内算好，再整体 shift —— T+1 行看 ≤T 的 3 日均值。

    无流日按 0 参与滚动（与 mf_main_ratio 同口径），不再被 rolling 跳过。
    """
    df, _ = _cov_frame(board_env, ["mf_main_ratio_3d"])
    got = _sym_col(df, "cov_mf_main_ratio_3d")
    assert got[D2] == pytest.approx(0.1)  # D1 单日
    assert got[D4] == pytest.approx((0.1 + 0.2 + 0.3) / 3)  # D3 行 = D1..D3 均值
    assert got[D5] == pytest.approx((0.2 + 0.3 + 0.4) / 3)
    # D5 起 money_flow 无行：填 0 后 D5 行窗口 D3..D5 = (0.3+0.4+0)/3，
    # D6 行窗口 D4..D6 = (0.4+0+0)/3，D7 起全 0
    assert got[D6] == pytest.approx((0.3 + 0.4 + 0.0) / 3)
    assert got[D7] == pytest.approx((0.4 + 0.0 + 0.0) / 3)
    assert got[D8] == pytest.approx(0.0)


def test_mf_zero_fill_keeps_neutralization_sample(board_env):
    """mf 缺流日填 0 后，中性化只剔除 shift 窗口首行，不再截断评价样本。

    修复前 mf 缺流日留 null，OLS 的 ``valid_mask`` 会把缺值行整行剔除 ——
    样本被协变量自己截断成「当日前 200 只」，而 submit/CLI 的 JSON 里没有任何
    样本量字段能看到这次截断。
    """
    from lquant.factors.mining.submit import _panel_with_covs, prepare_segment

    df, cov_cols = _panel_with_covs(covs=["mf_main_ratio"])
    assert cov_cols == ["cov_mf_main_ratio"]
    dates = sorted(df["trade_date"].unique().to_list())
    with_cov = prepare_segment(df, cov_cols, "$close", dates)
    without_cov = prepare_segment(df, [], "$close", dates)
    # 6 票 × 8 日：D8 无前瞻收益（6 行），其余 42 行参与；mf 只多剔窗口首行 6 行
    assert len(without_cov) == 42
    assert len(with_cov) == len(without_cov) - 6
    # 剩余 D2..D7（D1 是 mf 的 shift 窗口首行，D8 无前瞻收益）
    assert with_cov["trade_date"].n_unique() == 6


def test_lhb_on_board_seen_next_day(board_env):
    """上榜事实 T+1 才可见；未上榜日填 0（业务事实），窗口首行保留 null。"""
    df, _ = _cov_frame(board_env, ["lhb_on_board"])
    got = _sym_col(df, "cov_lhb_on_board")
    assert got[D1] is None
    assert got[D2] == 0 and got[D3] == 0
    assert got[D4] == 1  # D3 上榜 → D4 行才为 1
    assert got[D5] == 0 and got[D8] == 0


def test_lhb_net_buy_5d_window_and_zero_fill(board_env):
    """5 日上榜净买滚动：未上榜日计 0，窗口滑出后归 0。"""
    df, _ = _cov_frame(board_env, ["lhb_net_buy_5d"])
    got = _sym_col(df, "cov_lhb_net_buy_5d")
    assert got[D1] is None
    assert got[D2] == pytest.approx(0.0)
    assert got[D4] == pytest.approx(150.0)  # D3 的 150 在 D4 行可见
    assert got[D8] == pytest.approx(150.0)  # D3 仍在 D4..D8 的 5 日窗口内
    df2 = df.filter(pl.col("symbol") == "000001.SZ")
    m = dict(zip(df2["trade_date"].to_list(), df2["cov_lhb_net_buy_5d"].to_list(), strict=True))
    assert m[D2] == pytest.approx(-20.0)  # D1 上榜净卖 20
    assert m[D7] == pytest.approx(0.0)  # D7 行窗口 D2..D6，D1 已滑出
    assert m[D8] == pytest.approx(0.0)


def test_zt_streak_counts_consecutive_days(board_env):
    """连板计数：rle_id 连续段内累计，断板归零，T+1 可见。"""
    df, _ = _cov_frame(board_env, ["zt_streak"])
    got = _sym_col(df, "cov_zt_streak")
    assert got[D1] is None
    assert got[D2] == 0  # D1 未涨停
    assert got[D3] == 1  # D2 首板
    assert got[D4] == 2  # D3 二板
    assert got[D5] == 3  # D4 三板（D2..D4 连续）
    assert got[D6] == 0 and got[D8] == 0  # D5 起断板
    df2 = df.filter(pl.col("symbol") == "000001.SZ")
    m = dict(zip(df2["trade_date"].to_list(), df2["cov_zt_streak"].to_list(), strict=True))
    assert m[D7] == 1  # D6 涨停 → D7 行为 1
    assert m[D6] == 0


def test_zt_streak_excludes_broken_pool_rows(board_env):
    """炸板池行（``limit_up_type IS NULL``）不算涨停日。

    炸板池与涨停池共用 ``limit_up_pool`` 表；不区分的话「盘中涨停、收盘没封住」
    会被计成涨停，连板数虚高（300750 连续三天在炸板池 → 会被算成三连板）。
    """
    df, _ = _cov_frame(board_env, ["zt_streak"])
    sub = df.filter(pl.col("symbol") == "300750.SZ")
    m = dict(zip(sub["trade_date"].to_list(), sub["cov_zt_streak"].to_list(), strict=True))
    assert m[D5] == 0, "炸板被当成涨停：D2-D4 全在池里 → 伪三连板"
    assert m[D4] == 0 and m[D3] == 0
    # 主角（真涨停）不受影响
    got = _sym_col(df, "cov_zt_streak")
    assert got[D5] == 3


def test_zt_streak_unavailable_without_limit_up_type_column(board_env):
    """旧库缺 ``limit_up_type`` 列 → 显式不可用，不静默拿炸板行凑连板。"""
    import duckdb

    from lquant.core.config import get_settings

    con = duckdb.connect(str(get_settings().duckdb_path))
    con.execute("ALTER TABLE limit_up_pool DROP COLUMN limit_up_type")
    con.close()
    _, report = _cov_frame(board_env, ["zt_streak"])
    r = {x["covariate"]: x for x in report}
    assert r["zt_streak"]["coverage"] == 0.0
    assert "limit_up_type" in r["zt_streak"]["note"]


def test_zt_open_count_20d_rolling_sum(board_env):
    """炸板次数 20 日合计：未涨停日 open_count 计 0。"""
    df, _ = _cov_frame(board_env, ["zt_open_count_20d"])
    got = _sym_col(df, "cov_zt_open_count_20d")
    assert got[D1] is None
    assert got[D2] == pytest.approx(0.0)
    assert got[D3] == pytest.approx(1.0)  # D2 炸板 1 次
    assert got[D5] == pytest.approx(3.0)  # D4 行 = D1..D4 窗口 1+0+2
    assert got[D8] == pytest.approx(3.0)


def test_board_providers_missing_columns_report_unavailable(board_env):
    """老库/手改库缺 provider 字段 → 与其他看板 provider 一致的 coverage=0 上报。

    修复前只有 ``zt_streak`` 走 ``_limit_up_only`` 的显式检查，``main_net_ratio`` /
    ``net_buy`` / ``open_count`` 缺列时抛裸 polars ``ColumnNotFoundError`` 穿透
    ``build_covariates``（CLI 裸 traceback / API 500），而不是模块承诺的显式上报。
    """
    import duckdb

    from lquant.core.config import get_settings

    cases = (
        ("money_flow", "main_net_ratio", "mf_main_ratio"),
        ("dragon_tiger", "net_buy", "lhb_net_buy_5d"),
        ("limit_up_pool", "open_count", "zt_open_count_20d"),
    )
    for table, column, cov in cases:
        path = get_settings().duckdb_path
        con = duckdb.connect(str(path))
        con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        con.close()
        df, report = _cov_frame(board_env, [cov])
        r = {x["covariate"]: x for x in report}
        assert r[cov]["coverage"] == 0.0, f"{cov} 缺列未显式上报"
        assert column in r[cov]["note"], r[cov]["note"]
        assert f"cov_{cov}" not in df.columns
        con = duckdb.connect(str(path))
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} DOUBLE")
        con.close()


# ---------------- 语义分界：缺失 vs 业务 0 ----------------


def test_missing_table_reports_unavailable(board_env):
    """表未同步 → CovariateUnavailable → coverage=0 + note 可见，绝不填 0 冒充。"""
    import duckdb

    from lquant.core.config import get_settings

    con = duckdb.connect(str(get_settings().duckdb_path))
    con.execute("DROP TABLE money_flow")
    con.close()
    df, report = _cov_frame(board_env, ["mf_main_ratio", "lhb_on_board"])
    r = {x["covariate"]: x for x in report}
    assert r["mf_main_ratio"]["coverage"] == 0.0
    assert "未建表" in r["mf_main_ratio"]["note"]
    # 数据可用的 covariate 不受牵连
    assert r["lhb_on_board"]["coverage"] > 0
    assert "cov_mf_main_ratio" not in df.columns


def test_empty_window_reports_unavailable(board_env):
    """评价窗口内无数据 → 显式 unavailable，note 指向采集动作。"""
    from lquant.data.store.parquet import read_daily
    from lquant.factors.covariates import build_covariates

    panel = read_daily(start="2099-01-01").collect()  # 空面板
    assert not len(panel)
    _, report = build_covariates(panel, ["mf_main_ratio"])
    assert report[0]["coverage"] == 0.0
    assert "无数据" in report[0]["note"]


def test_coverage_reports_zero_fill_vs_missing(board_env):
    """覆盖率是语义分界的量化体现：缺数据日填 0，只剩 shift 窗口首行是 null。

    旧断言 ``mf_main_ratio coverage < 0.5`` 锁的是缺陷行为（资金流缺行留 null）。
    资金流表只采了 5 行，若留 null 覆盖会被压到 0.13 —— 那不是「数据可算」，
    而是评价样本被协变量自己截断（中性化剔除 null 行）的隐蔽来源。
    """
    from lquant.factors.sources.board import BOARD_COVARIATES

    df, report = _cov_frame(board_env, list(BOARD_COVARIATES))
    r = {x["covariate"]: x for x in report}
    assert set(r) == set(BOARD_COVARIATES)
    # 48 行面板（6 票 × 8 日）：只有每股 shift 首行 null → 42/48 = 0.875
    assert r["lhb_on_board"]["coverage"] == pytest.approx(0.875)
    assert r["zt_streak"]["coverage"] == pytest.approx(0.875)
    # 资金流同样只在窗口首行 null：缺流日按业务语义取 0，不再触发中性化截断
    assert r["mf_main_ratio"]["coverage"] == pytest.approx(0.875)
    assert r["mf_main_ratio_3d"]["coverage"] == pytest.approx(0.875)


# ---------------- symbol 口径防御 ----------------


def test_norm_sym_aligns_exchange_formats():
    from lquant.factors.sources.board import _norm_sym

    assert _norm_sym("sh.600000") == "600000.SH"
    assert _norm_sym("600000") == "600000.SH"
    # 解析不了的原样返回（宁可 join 不上 coverage=0，也不静默错位）
    assert _norm_sym("garbage") == "garbage"


# ---------------- _panel_with_covs：covs 参数与缺省口径 ----------------


def test_panel_with_covs_accepts_board_covs(board_env):
    from lquant.factors.mining.submit import _panel_with_covs

    df, cov_cols = _panel_with_covs(covs=["lhb_on_board", "mf_main_ratio"])
    assert cov_cols == ["cov_lhb_on_board", "cov_mf_main_ratio"]
    assert "cov_lhb_on_board" in df.columns


def test_panel_with_covs_default_unchanged(board_env):
    """缺省仍是市值/行业/换手统一口径，所有既有调用点零漂移。"""
    from lquant.factors.mining.submit import DEFAULT_COVS, _panel_with_covs

    assert DEFAULT_COVS == ("market_cap", "industry_sw1", "turnover_1m")
    df, cov_cols = _panel_with_covs()
    # industry_classify 无数据 → coverage=0 → 不进 present（诚实上报，不硬凑）
    assert "cov_market_cap" in cov_cols
    assert "cov_lhb_on_board" not in cov_cols
    assert "cov_market_cap" in df.columns


# ---------------- submit：G0 白名单随面板走 ----------------


def test_submit_g0_accepts_mounted_cov_fields(board_env):
    """引用 cov_ 列的表达式过 G0（allowed = 日线字段 ∪ 挂载的 cov 列）。

    f≡cov_lhb_on_board 会被中性化对自身消掉 → 流程应死在 RECOMPUTE/统计门
    （OOS_FAIL / LOW_TSTAT），而不是 G0 的 STATIC_FAIL —— 走到哪一步就是
    哪一步的问题，原因码必须指对地方。
    """
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register(
        {
            "expr": "cov_lhb_on_board",
            "rationale": "上榜次日情绪动量（EP-9 验证 G0 白名单并集）",
            "covs": ["lhb_on_board"],
        }
    )
    assert ok is False
    assert payload.get("reason_code") in ("OOS_FAIL", "LOW_TSTAT")


def test_submit_g0_rejects_unmounted_cov_field(board_env):
    """引用没挂载的 cov 列 → G0 STATIC_FAIL，近似候选提示可操作。"""
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register(
        {
            "expr": "$ghost_cov + $close",
            "rationale": "引用未挂载字段",
            "covs": ["lhb_on_board"],
        }
    )
    assert ok is False
    assert payload["stage"] == "G0"
    assert payload["reason_code"] == "STATIC_FAIL"
    assert "ghost_cov" in payload["hint"]


# ---------------- provider 注册触发 ----------------


def test_board_providers_registered_via_factors_import():
    """import lquant.factors 即注册全部看板 provider（factors/__init__ 触发）。"""
    from lquant.factors.covariates import PROVIDERS
    from lquant.factors.sources.board import BOARD_COVARIATES

    for name in BOARD_COVARIATES:
        meta = PROVIDERS.meta(name)
        assert "label" in meta and "T+1 可用" in meta["label"]


# ---------------- demo 数据契约：必须能复现真实采集的涨停/炸板分界 ----------------

def test_demo_pool_matches_real_collector_contract():
    """demo 炸板池不得带 ``limit_up_type``，且与涨停池 symbol 无交集。

    board.py 的 ``zt_streak`` 按 ``limit_up_type IS NOT NULL`` 区分涨停与炸板。
    修复前 ``_demo_pool("broken")`` 也生成该列（15/15 非空），且 up/broken 共用
    同一套 symbol 生成式（交集 15/15）—— 在 demo 上验证「炸板不算涨停」会得到
    与真实实盘相反的假结论。real ``fetch_broken_pool`` 契约里本就没有该列。
    """
    from lquant.market.collectors.limit_up import fetch_broken_pool, fetch_limit_up_pool

    up = fetch_limit_up_pool("2024-01-02", demo=True)
    broken = fetch_broken_pool("2024-01-02", demo=True)
    assert "limit_up_type" in up.columns
    assert "limit_up_type" not in broken.columns
    assert set(up["symbol"]).isdisjoint(set(broken["symbol"]))


# ---------------- CLI：eval --cov 端到端 ----------------


def test_eval_cli_with_cov_end_to_end(board_env):
    """lq factor eval --cov 挂看板 covariate：表达式引用 cov_ 列全链路可算。"""
    import json

    import duckdb
    from click.testing import CliRunner

    from lquant.cli.commands.factor import factor
    from lquant.core.config import get_settings

    # eval 现在带切分隔离带（purge=1/embargo=1，见 cli/commands/factor.py）：
    # 训练段两端各被剪掉 1 天 —— covariate 的 shift(1) 让首日 cov 为 null，
    # 标签前瞻让末日没有 fwd_ret。8 天面板切完训练段只剩 2 天 IC，断言
    # ``n_days >= 3`` 表达的是「端到端确实算出了多天」，那就要把面板补够，
    # 而不是把断言放宽成 >=2（等于承认只算出了一天）。
    # 这里续造 2 个交易日并写进日线湖（同 key 覆盖合并）。
    extra_days = [DAYS[-1] + timedelta(days=1), DAYS[-1] + timedelta(days=2)]
    from lquant.data.store.parquet import write_daily

    write_daily(_daily_df(extra_days, offset=len(DAYS)))

    # 零星小场景足够测 shift 语义，但撑不起每日截面 IC —— 端到端前把看板
    # 表补密到「每票每日都有」的真实形态（含与已有行的主键共存，验证收敛），
    # 并把续造的交易日一起补齐：cov 若在某天恒 0，表达式就是常数、IC 无从算起。
    con = duckdb.connect(str(get_settings().duckdb_path))
    for j, s in enumerate(SYMS):
        for i, d in enumerate([*DAYS[:7], *extra_days]):
            con.execute(
                "INSERT OR REPLACE INTO money_flow (trade_date, symbol, main_net_ratio,"
                " collected_at) VALUES (?, ?, ?, now())",
                [d, s, 0.01 * (i + 1) * (j + 1) - 0.1],
            )
            con.execute(
                "INSERT OR REPLACE INTO dragon_tiger (trade_date, symbol, net_buy,"
                " reason, collected_at) VALUES (?, ?, ?, 'x', now())",
                [d, s, 1e4 * (i + 1) * ((-1) ** j)],
            )
    con.close()

    r = CliRunner().invoke(
        factor,
        [
            "eval",
            "cov_lhb_on_board * cov_mf_main_ratio",
            "--cov",
            "mf_main_ratio,lhb_on_board",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert set(data["covariates"]) == {"cov_lhb_on_board", "cov_mf_main_ratio"}
    assert data["neutralized"] is True
    assert data["n_days"] >= 3
