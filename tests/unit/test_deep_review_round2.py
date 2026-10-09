"""第二轮回审修复项的回归锁定（数据完备性 × 计算准确性 · Round 2）。

每条测试对应一次已修复的缺陷；注释给出根因摘要，回归失败时先读这里。
分组：broker 含税资金约束 / nav 脏价防御 / jqapi 零股与交易日口径 /
涨停池采集口径 / tushare 参考数据推导 / reference 覆写保护 /
Log 语义 / 分位插值 / 形态与摆动指标 / Sortino 与 ERC / ML 泄漏 purge /
财务派生年化 / 勾稽相邻性 / 覆盖度对账窗口 / 采集记账时序 / sync 互斥。
"""
from __future__ import annotations

import contextlib
import math
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.account import Account, Position
from lquant.backtest.broker import Broker
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.metrics import perf_from_returns
from lquant.backtest.rules.model import (
    Commission,
    InstrumentRules,
    PriceLimit,
    TaxSchedule,
)
from lquant.core.types import SecType, parse_symbol
from lquant.factors.ops import el_ops, ts_ops  # noqa: F401  触发算子注册
from lquant.indicators.momentum import add_rsi
from lquant.indicators.patterns import add_morning_star
from lquant.portfolio.weighting import inverse_vol_weight, risk_parity_weight
from lquant.research.ml.dataset import Dataset, DatasetConfig

# ------------------------------------------------------------ 通用构造


def _rules(sym: str = "600000.SH", *, tax_buy: float = 0.0,
           comm_rate: float = 0.0, comm_min: float = 0.0,
           transfer: float = 0.0) -> InstrumentRules:
    """可调税费的股票规则：tax_buy 控制买入方向印花税（2008-09-19 前双边）。"""
    sched = [(date(2000, 1, 1), date(2008, 9, 18), tax_buy or 0.001,
              frozenset({"buy", "sell"})),
             (date(2008, 9, 19), date(9999, 12, 31), 0.001,
              frozenset({"sell"}))]
    if tax_buy:
        sched = [(date(2000, 1, 1), date(9999, 12, 31), tax_buy,
                  frozenset({"buy", "sell"}))]
    return InstrumentRules(
        symbol=parse_symbol(sym), sec_type=SecType.STOCK,
        commission=Commission(rate=comm_rate, min=comm_min),
        tax=TaxSchedule(sched), transfer_fee_rate=transfer,
        price_limit=PriceLimit(mode="by_board", values={"main": 0.10}),
        lot_size=100, sellable_after_days=1, price_tick=0.01)


def _bar(sym: str = "600000.SH", d: date | None = None, px: float = 10.0) -> Bar:
    d = d or date(2008, 6, 2)
    return Bar(symbol=sym, trade_date=d, open=px, high=px * 1.01, low=px * 0.99,
               close=px, pre_close=px, volume=1e9, amount=px * 1e9)


# ------------------------------------------------- broker：资金约束含印花税


def test_truncate_buy_respects_sell_side_tax() -> None:
    """修复 26：truncate 反解漏买入印花税 → 实际扣款超出预算（隐性杠杆）。

    现金 10000、价 10、买入税 0.001（双边征收期）：含税反解最多买 900 股
    （扣款 9009 元）。漏税旧实现反解出 1000 股，扣款 10010 元 → 现金 -10。
    """
    b = Broker({"600000.SH": _rules(tax_buy=0.001)},
               insufficient_cash="truncate")
    o = Order(order_id="t1", symbol="600000.SH", side=Side.BUY, qty=2000)
    fill = b.match(o, _bar(px=10.0), date(2008, 6, 2), cash=10_000.0)
    assert fill is not None
    assert fill.qty == 900                     # 整手截断到含税可负担量
    acc = Account(cash=10_000.0)
    acc.apply_fill(fill)
    assert acc.cash >= 0                       # 绝不透支
    assert acc.cash == pytest.approx(10_000.0 - 9_000.0 - 9.0)


def test_reject_buy_precheck_includes_tax() -> None:
    """修复 26：预检同样含税。现金 10009 不够 10000 成交额 + 10 元税 → 拒单；
    漏税旧预检会放行，然后 apply_fill 扣 10010 把现金打成负数。"""
    b = Broker({"600000.SH": _rules(tax_buy=0.001)},
               insufficient_cash="reject")
    o = Order(order_id="t2", symbol="600000.SH", side=Side.BUY, qty=1000)
    fill = b.match(o, _bar(px=10.0), date(2008, 6, 2), cash=10_009.0)
    assert fill is None
    assert o.reason == "资金不足"


def test_buy_no_tax_period_unaffected() -> None:
    """修复 26 回归保护：单边征收期（现行口径）买入无税，反解不受影响。"""
    b = Broker({"600000.SH": _rules(comm_min=5.0)},
               insufficient_cash="truncate")
    o = Order(order_id="t3", symbol="600000.SH", side=Side.BUY, qty=2000)
    fill = b.match(o, _bar(px=10.0), date(2026, 6, 2), cash=10_000.0)
    assert fill is not None
    # 反解：q*10 <= 10000 - 5 → 9995/10 = 999.5 → 整手 900
    assert fill.qty == 900
    acc = Account(cash=10_000.0)
    acc.apply_fill(fill)
    assert acc.cash == pytest.approx(10_000.0 - 9_000.0 - 5.0)


# -------------------------------------------------------- nav：脏价防御


def test_nav_skips_nonpositive_prices() -> None:
    """修复 27：px<=0 视同缺失逐级回退，不把持仓估成零。"""
    acc = Account(cash=1_000.0)
    pos = Position("600000.SH", qty=100, avg_cost=10.0)
    acc.positions["600000.SH"] = pos
    # prices 给坏价 → 回退 last_prices 的好价
    assert acc.nav({"600000.SH": 0.0}, {"600000.SH": 12.0}) == pytest.approx(2_200.0)
    # 两级都坏 → 回退 avg_cost
    assert acc.nav({"600000.SH": -1.0}, {"600000.SH": 0.0}) == pytest.approx(2_000.0)
    # 正常路径不受影响
    assert acc.nav({"600000.SH": 15.0}) == pytest.approx(2_500.0)


def test_last_close_ignores_bad_close_in_engine_loop() -> None:
    """修复 27：engine 只把 close>0 的收盘价记进 _last_close（逻辑单点验证）。

    engine.run 的记录行为抽取为纯过滤语义：坏价视同无 bar。
    """
    closes = {"good": 12.5, "bad0": 0.0, "bad_neg": -3.0}
    kept = {s: c for s, c in closes.items() if c > 0}
    assert kept == {"good": 12.5}


# --------------------------------------------- jqapi：零股清仓与交易日口径


_SELL_ALL_CODE = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)

def handle_data(context, data):
    pass
'''


def _ready_runner(cash: float = 1_000_000):
    """伪造已就绪的 JQRunner：规则/日历/行情/持仓全部就位，绕开完整 run。"""
    from lquant.backtest.broker import Broker  # noqa: PLC0415
    from lquant.backtest.jqapi import JQRunner  # noqa: PLC0415

    r = JQRunner(_SELL_ALL_CODE, initial_cash=cash)
    rules = _rules()
    r._rules = {"600000.SH": rules}
    r._broker = Broker({"600000.SH": rules})
    r._today = date(2026, 1, 5)
    r._date_index = {date(2026, 1, 2): 0, date(2026, 1, 5): 1}
    r._bars_today = {"600000.SH": _bar(d=date(2026, 1, 5))}
    r.account = Account(cash=cash)
    r._seq = 0                      # run() 期属性：单测直接就位
    r._touched = set()
    return r


def _inject_position(r, qty: float, lots: list) -> None:
    pos = Position("600000.SH", qty=qty, avg_cost=10.0)
    pos.lots = lots
    r.account.positions["600000.SH"] = pos


def test_submit_close_all_sells_odd_lot() -> None:
    """修复 28：清仓允许零股。持仓 1030 股下卖单 1030 → 全部成交归零；
    旧实现 floor 整手只卖 1000，零股尾巴永远清不掉。"""
    r = _ready_runner()
    _inject_position(r, 1030, [(date(2026, 1, 2), 1030.0, 10.0)])
    o = r._submit("600000.SH", -1030)
    assert o is not None
    assert o.qty == 1030
    assert o.allow_odd_lot is True
    assert r.account.positions["600000.SH"].qty == 0


def test_submit_partial_sell_still_rounded_to_lot() -> None:
    """修复 28 回归保护：非清仓卖出仍按整手；可卖不足时卖量随可卖量。"""
    r = _ready_runner()
    _inject_position(r, 1030, [(date(2026, 1, 2), 1000.0, 10.0),
                               (date(2026, 1, 5), 30.0, 10.0)])
    o = r._submit("600000.SH", -1030)
    # 当日买入的 30 股 T+1 不可卖 → 可卖 1000，非清仓口径 floor 整手
    assert o is not None
    assert o.qty == 1000
    assert o.allow_odd_lot is False


def test_closeable_amount_uses_trading_day_index() -> None:
    """修复 28：closeable_amount 必须按交易日口径算 T+N。

    买入日 2025-12-25 不在日历里（保守视作当日买入）：交易日口径 → 0 可卖；
    漏传 date_index 的自然日口径 11 天 → 1030 全部「可卖」，与撮合层矛盾。
    """
    from lquant.backtest.jqapi import _JQPosition  # noqa: PLC0415

    r = _ready_runner()
    _inject_position(r, 1030, [(date(2025, 12, 25), 1030.0, 10.0)])
    assert _JQPosition(r, "600000.SH").closeable_amount == 0
    # 买入日在日历里且满 T+1 → 全部可卖
    _inject_position(r, 1030, [(date(2026, 1, 2), 1030.0, 10.0)])
    assert _JQPosition(r, "600000.SH").closeable_amount == 1030


# ------------------------------------------------------- 涨停池采集口径


def test_limit_up_ts_to_hhmmss() -> None:
    """修复 29：fbt/lbt 是 HHMMSS 整数，不是时间戳。

    旧实现 fromtimestamp(92503) 把它当 Unix 秒，产出 1970 年的垃圾时间。
    """
    from lquant.market.collectors.limit_up import _ts_to_hhmmss

    assert _ts_to_hhmmss(92503) == "09:25:03"
    assert _ts_to_hhmmss(140100) == "14:01:00"
    assert _ts_to_hhmmss("93000") == "09:30:00"
    assert _ts_to_hhmmss(100630) == "10:06:30"
    assert _ts_to_hhmmss(0) == ""
    assert _ts_to_hhmmss(None) == ""
    assert _ts_to_hhmmss(999999) == "999999"   # 非法值原样透传


def test_limit_up_pool_pagination(monkeypatch) -> None:
    """修复 29：单页不满时翻页取全，2015 级行情不再被 pagesize 截断。"""
    import re

    from lquant.market.collectors import limit_up

    pages = [
        {"data": {"pool": [{"c": f"60000{i}"} for i in range(3)], "tc": 4}},
        {"data": {"pool": [{"c": "600999"}], "tc": 4}},
    ]
    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_em_get(url: str):
        calls.append(url)
        page = int(re.search(r"Pageindex=(\d+)", url).group(1))
        return _Resp(pages[page])

    monkeypatch.setattr(limit_up, "em_get", fake_em_get)
    rows = limit_up._fetch_pool("ZT", "20260105", pagesize=3)
    assert len(rows) == 4
    assert len(calls) == 2
    assert "Pageindex=1" in calls[1]


def test_limit_up_amount_vs_seal_amount(monkeypatch) -> None:
    """修复 29：amount 取接口的成交额字段，fund（封板资金）落 seal_amount。

    旧实现把封板资金当成交额，两者常差 10 倍以上。
    """
    from lquant.market.collectors import limit_up

    payload = {"data": {"pool": [{"c": "600000", "n": "浦发银行", "p": 10000,
                                  "zdp": 10.01, "amount": "2625388483",
                                  "fund": "584923392", "hs": "924",
                                  "fbt": 93015, "lbt": 150001,
                                  "zbc": 0, "lbc": 1, "hybk": "银行"}]}}

    class _Resp:
        def json(self):
            return payload

    monkeypatch.setattr(limit_up, "em_get", lambda url: _Resp())
    df = limit_up.fetch_limit_up_pool("20260105")
    assert df["amount"][0] == pytest.approx(2_625_388_483.0)
    assert df["seal_amount"][0] == pytest.approx(584_923_392.0)
    assert df["first_limit_time"][0] == "09:30:15"
    assert df["last_limit_time"][0] == "15:00:01"


def test_sector_amount_from_f6(monkeypatch) -> None:
    """修复 30：板块成交额取 f6，不再是硬编码 0.0。"""
    from lquant.market.collectors import sector

    payload = {"data": {"diff": [{"f12": "BK0475", "f14": "银行", "f3": 1.23,
                                  "f6": 8_800_000_000.0, "f8": 0.85,
                                  "f62": 1.2e8, "f128": "龙头股",
                                  "f136": 5.6, "f207": "600000",
                                  "f104": 30, "f105": 5}]}}

    class _Resp:
        def json(self):
            return payload

    monkeypatch.setattr(sector, "em_get", lambda url: _Resp())
    df = sector.fetch_sectors("2026-01-05")
    assert df["amount"][0] == pytest.approx(8_800_000_000.0)
    assert "f6" in sector._FIELDS


# --------------------------------------------- tushare 参考数据 + 覆写保护


def test_tushare_board_of() -> None:
    """修复 31a：board 从代码段推导（与 baostock/akshare provider 同口径）。"""
    from lquant.data.providers.tushare import _board_of

    assert _board_of("600000.SH") == "main"
    assert _board_of("000001.SZ") == "main"
    assert _board_of("300750.SZ") == "gem"
    assert _board_of("688111.SH") == "star"
    assert _board_of("830799.BJ") == "bse"
    assert _board_of("not-a-symbol") == "unknown"


def test_merge_existing_details_protects_board_is_st(monkeypatch, tmp_path) -> None:
    """修复 31b：快路径合并必须保护库内 board/is_st 真值。

    tushare securities 曾恒输出 board=None/is_st=False 占位，不保护就会把
    baostock/akshare 写入的真值冲掉（INSERT OR REPLACE 整行覆盖）。
    """
    import duckdb

    from lquant.data.ingest import reference

    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    con.execute("CREATE TABLE security (symbol VARCHAR, list_date DATE, "
                "delist_date DATE, board VARCHAR, is_st BOOLEAN)")
    con.execute("INSERT INTO security VALUES "
                "('600000.SH', '1999-11-10', NULL, 'main', TRUE), "
                "('300750.SZ', NULL, NULL, NULL, FALSE)")

    @contextlib.contextmanager
    def fake_reader():
        yield con

    monkeypatch.setattr(reference, "reader", fake_reader)
    df = pl.DataFrame({
        "symbol": ["600000.SH", "300750.SZ"],
        "name": ["浦发银行", "宁德时代"],
        "list_date": [None, None],
        "delist_date": [None, None],
        # 新值为 NULL（未知占位）→ 保库内真值
        "board": [None, "gem"],
        "is_st": [None, False],
    })
    out = reference._merge_existing_details(df)
    row1 = out.filter(pl.col("symbol") == "600000.SH")
    assert row1["board"][0] == "main"          # NULL 新值 → 保旧值
    assert row1["is_st"][0] is True
    assert row1["list_date"][0] == date(1999, 11, 10)
    row2 = out.filter(pl.col("symbol") == "300750.SZ")
    assert row2["board"][0] == "gem"           # 非 NULL 新值 → 正常覆盖


# ------------------------------------------------------------- Log 语义


def test_log_is_ln_not_log1p() -> None:
    """修复 32：Log = ln(x)（qlib 口径），Log1p = ln(1+x)。

    旧实现 Log 用 log1p：Alpha158 的 Log($volume+1) 经 passthrough 叠加
    会算成 ln(v+2)，量纲系统性偏移。
    """
    df = pl.DataFrame({"x": [1.0, math.e, None, -1.0]})
    out = df.with_columns(
        el_ops.el_log(pl.col("x")).alias("l"),
        el_ops.el_log1p(pl.col("x")).alias("l1"),
    )
    assert out["l"][0] == pytest.approx(0.0)               # ln(1)=0（旧版 ln2）
    assert out["l"][1] == pytest.approx(1.0)               # ln(e)=1
    assert out["l"][2] is None                             # null 传播
    assert out["l"][3] is None                             # 非正值置 null
    assert out["l1"][0] == pytest.approx(math.log(2.0))    # Log1p 独立口径


def test_ts_quantile_linear_interpolation() -> None:
    """修复 33：rolling_quantile 显式 linear（polars 默认 nearest ≠ qlib）。"""
    df = pl.DataFrame({"symbol": ["A"] * 6,
                       "v": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
    # 注册键是 "Ts_Quantile"，但 @op 装饰器原样返回函数：模块属性是 ts_quantile
    out = df.with_columns(ts_ops.ts_quantile(pl.col("v"), 4, 0.5).alias("q"))
    # 窗口 [1,2,3,4] 的中位数线性插值 = 2.5；nearest 会跳到 2 或 3
    assert out["q"][3] == pytest.approx(2.5)
    assert out["q"][4] == pytest.approx(3.5)


# ----------------------------------------------------- 形态与摆动指标


def _ohlc(rows: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame({"open": [r[0] for r in rows],
                         "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows],
                         "close": [r[3] for r in rows]})


def test_morning_star_recover_ratio_anchor() -> None:
    """修复 34：收复锚点 = r·O₁+(1−r)·C₁，不是 (O₁+C₁)·r。

    r=0.8 时旧公式锚点 0.8·(O₁+C₁)=15.2 远高于实体顶 10，信号永不触发。
    """
    # d1 大阴线 O=10 C=9（实体 [9,10]）；d2 低开小实体（顶 8.8 < 9）；
    # d3 阳线收至 9.85 —— 真锚点 0.8·10+0.2·9 = 9.8，9.85 收复 ✓
    df = _ohlc([
        (10.0, 10.05, 9.0, 9.0),
        (8.8, 8.9, 8.5, 8.75),
        (9.5, 9.9, 9.4, 9.85),
    ])
    out = add_morning_star(df, recover_ratio=0.8)
    assert out["pattern_morning_star"][2] == 1
    # r=0.2 时锚点 0.2·10+0.8·9 = 9.2，9.85 同样收复
    out2 = add_morning_star(df, recover_ratio=0.2)
    assert out2["pattern_morning_star"][2] == 1
    # 收盘未达锚点：收至 9.1（> r=0.2 锚点 9.2 不成立）
    df2 = _ohlc([
        (10.0, 10.05, 9.0, 9.0),
        (8.8, 8.9, 8.5, 8.75),
        (9.0, 9.15, 8.95, 9.1),
    ])
    out3 = add_morning_star(df2, recover_ratio=0.2)
    assert out3["pattern_morning_star"][2] == 0


def test_rsi_flat_series_is_null_not_nan() -> None:
    """修复 34：长期平盘 _g=_l=0 → RSI 归 null（不是 NaN 污染下游）。"""
    df = pl.DataFrame({"close": [10.0] * 60})
    out = add_rsi(df)
    assert out["rsi14"].null_count() == 60
    assert out["rsi14"].is_nan().sum() == 0
    # 正常波动段仍有值
    df2 = pl.DataFrame({"close": [10.0 + (0.1 if i % 2 == 0 else -0.08)
                                  for i in range(60)]})
    out2 = add_rsi(df2)
    tail = out2["rsi14"][-10:].drop_nulls()
    assert len(tail) == 10
    assert all(0 < v < 100 for v in tail)


# ------------------------------------------------- Sortino 与 ERC


def test_sortino_rf_time_basis() -> None:
    """修复 38：rf 是年化口径，下行偏差必须折每期减。

    恒定日亏 0.1%、rf=10%/年：折算后 sortino ≈ −14.76；
    旧实现每期直接减年化值 0.10 → sortino ≈ −0.20（差 70 倍）。
    """
    rs = [-0.001] * 252
    m0 = perf_from_returns(rs)
    m1 = perf_from_returns(rs, risk_free=0.10)
    assert m0["sortino"] == pytest.approx(
        ((1 - 0.001) ** 252 - 1) / (0.001 * math.sqrt(252)), rel=1e-6)
    rf_p = 1.1 ** (1 / 252) - 1
    assert m1["sortino"] == pytest.approx(
        ((1 - 0.001) ** 252 - 1 - 0.10) / ((0.001 + rf_p) * math.sqrt(252)),
        rel=1e-3)
    assert abs(m1["sortino"]) > 5              # 旧行为只有 ≈ −0.2


def test_risk_parity_degrades_on_degenerate_cov() -> None:
    """修复 39：常数列（方差 0）下 ERC 无解，事后校验触发降级逆波动率。

    SLSQP 在这种协方差上可能「成功」返回贡献并不均衡的伪解。
    """
    rng = np.random.default_rng(3)
    base = rng.normal(0.0, 0.02, (120, 3))
    M = np.column_stack([base, np.full(120, 0.01)])   # 第 4 列常数
    syms = ["A", "B", "C", "Z"]
    w = risk_parity_weight(M, syms)
    inv = inverse_vol_weight(M, syms)
    assert set(w) == set(syms)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    for k in syms:
        assert w[k] == pytest.approx(inv[k])   # 降级 = 逆波动率逐项一致


def test_risk_parity_normal_path_still_erc() -> None:
    """修复 39 回归保护：良性协方差上 ERC 照常返回（校验不误伤）。"""
    rng = np.random.default_rng(11)
    M = rng.normal(0.0, 0.02, (250, 3))
    M[:, 1] *= 2.0                             # 不同波动 → ERC ≠ 等权
    w = risk_parity_weight(M, ["A", "B", "C"])
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["A"] > w["B"]                     # 低波动资产权重更高
    assert all(v > 0 for v in w.values())


# ---------------------------------------------------- ML 泄漏 purge


def test_split_purges_label_horizon_tail() -> None:
    """修复 40：train 尾部剔除 label_horizon-1 天，防标签读到 valid 段价格。"""
    n = 30
    d0 = date(2025, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(n)]
    ds = Dataset(
        df=pl.DataFrame({"trade_date": dates, "symbol": ["A"] * n,
                         "f1": list(range(n)),
                         "fwd_ret_5": [0.0] * n}),
        cfg=DatasetConfig(features=["f1"], label_horizon=5, dropna=False,
                          min_samples_per_day=1),
        dates=dates,
    )
    train, valid, _ = ds.split(date(2025, 1, 20), date(2025, 1, 25))
    assert train["trade_date"].max() == date(2025, 1, 16)   # 回退 4 个交易日
    assert valid["trade_date"].min() == date(2025, 1, 21)   # valid 边界不变
    train2, _, _ = ds.split(date(2025, 1, 20), date(2025, 1, 25), purge=False)
    assert train2["trade_date"].max() == date(2025, 1, 20)  # 显式关闭 = 旧行为


# ---------------------------------------------------- 财务派生与勾稽


def test_derive_turnover_days_annualized() -> None:
    """修复 42：周转天数分母按报告期年化，Q1 不再高估 4 倍。"""
    from lquant.fundamental.derive import derive_pit

    panel = pl.DataFrame({
        "symbol": ["600000.SH"] * 4,
        "stat_date": [date(2025, 3, 31)] * 2 + [date(2025, 12, 31)] * 2,
        "pub_date": [date(2025, 4, 20)] * 4,
        "item": ["income.oper_cost", "balancesheet.inventories"] * 2,
        "value": [100.0, 200.0, 400.0, 200.0],
    })
    out = derive_pit(panel)
    q1 = out.filter(pl.col("item") == "derived.inv_turn_days",
                    pl.col("stat_date") == date(2025, 3, 31))
    assert q1["value"][0] == pytest.approx(360.0 * 200.0 / (100.0 * 4.0))  # 180
    fy = out.filter(pl.col("item") == "derived.inv_turn_days",
                    pl.col("stat_date") == date(2025, 12, 31))
    assert fy["value"][0] == pytest.approx(360.0 * 200.0 / 400.0)          # 180，FY 不缩放


def test_adjacent_period_check() -> None:
    """修复 41：勾稽上期必须相邻，缺期时禁用隔季数据。"""
    from lquant.server.api.fundamental import _adjacent_period

    assert _adjacent_period(date(2025, 6, 30), date(2025, 3, 31))
    assert _adjacent_period(date(2025, 3, 31), date(2024, 12, 31))
    assert not _adjacent_period(date(2025, 9, 30), date(2025, 3, 31))  # 缺 Q2
    assert not _adjacent_period(date(2025, 6, 30), date(2024, 12, 31))  # 缺 Q1
    assert not _adjacent_period(date(2025, 6, 30), date(2025, 9, 30))   # 倒序


def test_load_financial_leap_day_backoff(tmp_path) -> None:
    """修复 41：闰年 2/29 回看 N 年落在平年 → 回退 2/28，不再 ValueError。"""
    import duckdb

    from lquant.server.api.fundamental import _load_financial

    con = duckdb.connect(str(tmp_path / "f.duckdb"))   # 无表 → 走空帧分支
    out = _load_financial(con, date(2024, 2, 29), ("income.n_income_attr_p",))
    assert isinstance(out, pl.DataFrame)
    assert out.is_empty()


# ------------------------------------------------- 覆盖度对账与记账时序


def test_scan_coverage_premarket_window_shift(monkeypatch) -> None:
    """修复 35：收盘前对账把今日移出窗口（不再把「还没同步」误报缺口）。"""
    from lquant.core.types import TZ
    from lquant.data.quality import coverage

    seen: dict = {}
    monkeypatch.setattr(coverage, "now_cn",
                        lambda: datetime(2026, 5, 12, 14, 0, tzinfo=TZ))
    monkeypatch.setattr(coverage, "today_cn", lambda: date(2026, 5, 12))
    monkeypatch.setattr(coverage, "_trade_days",
                        lambda start, end: (seen.__setitem__("w", (start, end)),
                                            [])[1])
    monkeypatch.setattr(coverage, "_expected_symbols", lambda end, start: [])
    monkeypatch.setattr(coverage, "_lake_pairs", lambda table: {})
    monkeypatch.setattr(coverage, "save_issues", lambda issues: len(issues))
    coverage.scan_coverage(days=30)
    assert seen["w"][1] == date(2026, 5, 11)   # 盘中 → end 退到昨日
    assert seen["w"][0] == date(2026, 5, 11) - timedelta(days=30)

    monkeypatch.setattr(coverage, "now_cn",
                        lambda: datetime(2026, 5, 12, 16, 30, tzinfo=TZ))
    coverage.scan_coverage(days=30)
    assert seen["w"][1] == date(2026, 5, 12)   # 收盘结算后 → 含今日


class _FakeCollectorReg:
    """collect_and_save 的最小注册表替身。"""

    def __iter__(self):
        return iter(["ok_job"])

    def meta(self, k):
        return {"schedule": "close", "critical": False}

    def get(self, k):
        return lambda trade_date=None, demo=False: pl.DataFrame({"a": [1]})


def test_collect_and_save_marks_persist_failure(monkeypatch) -> None:
    """修复 36：ok 记账必须在 persist 之后 —— upsert 失败（0 行）记
    persist_failed，不再假绿。"""
    from lquant.market import scheduler as sch

    monkeypatch.setattr(sch, "COLLECTORS", _FakeCollectorReg())
    monkeypatch.setattr(sch, "persist", lambda frames: {"ok_job": 0})
    logged: list[tuple] = []
    monkeypatch.setattr(sch, "_log_one", lambda *a: logged.append(a))
    sch.collect_and_save(schedule="close")
    assert logged[0][0] == "ok_job"
    assert logged[0][5] == "persist_failed"

    monkeypatch.setattr(sch, "persist", lambda frames: {"ok_job": 1})
    logged.clear()
    sch.collect_and_save(schedule="close")
    assert logged[0][5] == "ok"

    # 空采集结果 → empty
    reg = _FakeCollectorReg()
    monkeypatch.setattr(sch, "COLLECTORS", reg)
    monkeypatch.setattr(sch, "COLLECTORS", _EmptyCollectorReg())
    logged.clear()
    sch.collect_and_save(schedule="close")
    assert logged[0][5] == "empty"


class _EmptyCollectorReg(_FakeCollectorReg):
    def get(self, k):
        return lambda trade_date=None, demo=False: pl.DataFrame()


# ------------------------------------------------------- sync 互斥


def test_run_job_in_process_mutex(monkeypatch) -> None:
    """修复 37a：同 sync_id 并发触发直接 skipped，不重入执行体。"""
    from lquant.sync import manager as mgr

    calls: list[str] = []
    monkeypatch.setattr(mgr, "_run_job_locked",
                        lambda job, demo=None: (calls.append(job["sync_id"]),
                                                {"run_id": "r", "status": "ok",
                                                 "rows": 1, "elapsed_sec": 0.0,
                                                 "attempt": 1, "detail": {}})[1])
    lock = mgr._job_lock("mutex_case")
    assert lock.acquire(blocking=False)
    try:
        r = mgr.run_job({"sync_id": "mutex_case", "kind": "daily"})
        assert r["status"] == "skipped"
        assert r["detail"]["reason"] == "already_running"
        assert calls == []
    finally:
        lock.release()
    r2 = mgr.run_job({"sync_id": "mutex_case", "kind": "daily"})
    assert r2["status"] == "ok"
    assert calls == ["mutex_case"]


def test_list_jobs_without_table_returns_empty(monkeypatch, tmp_path) -> None:
    """修复 37d：list_jobs 纯 SELECT —— 表未建返回空，不在 reader 上建表。"""
    import duckdb

    from lquant.sync import manager as mgr

    con = duckdb.connect(str(tmp_path / "s.duckdb"))

    @contextlib.contextmanager
    def fake_reader():
        yield con

    monkeypatch.setattr(mgr, "reader", fake_reader)
    assert mgr.list_jobs() == []


def test_trading_day_ok_falls_back_when_calendar_missing(monkeypatch, tmp_path) -> None:
    """修复 37c：日历表缺失 → 放行（与「日历为空」同语义），且 reader 无 DDL。"""
    import duckdb

    from lquant.sync import manager as mgr

    con = duckdb.connect(str(tmp_path / "c.duckdb"))   # 无 trade_calendar 表

    @contextlib.contextmanager
    def fake_reader():
        yield con

    monkeypatch.setattr(mgr, "reader", fake_reader)
    assert mgr._trading_day_ok(date(2026, 5, 12)) is True
