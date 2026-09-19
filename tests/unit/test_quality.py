"""质量体系测试：八项断言 / 五层 validators / flags / issues / lineage / golden / 门禁。

纯函数部分（asserts/validators/flags）不碰库；落库部分（issues/lineage/
golden/gate_daily）走隔离的 tmp duckdb —— 质量检查自身不能依赖真实数据环境。
"""
from __future__ import annotations

import os
from datetime import date, datetime

import polars as pl
import pytest

from lquant.data.quality.flags import (
    ADJ_ANOMALY,
    PRICE_OUT_OF_RANGE,
    SUSPENSION_FILL,
    ZOMBIE,
)


def _good_bars(n_days: int = 5, adj_factor: float | None = None) -> pl.DataFrame:
    """干净的测试数据：价格合法、OHLC 一致、量额同量级。"""
    d0 = date(2026, 1, 5)
    dates = [date.fromordinal(d0.toordinal() + i) for i in range(n_days)]
    closes = [10.0 + 0.1 * i for i in range(n_days)]
    rows = {
        "symbol": ["000001.SZ"] * n_days,
        "trade_date": dates,
        "open": closes,
        "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes],
        "close": closes,
        "pre_close": [10.0] + closes[:-1],
        "volume": [1e6] * n_days,
        "amount": [c * 1e6 for c in closes],   # ratio = 1.0
    }
    if adj_factor is not None:
        rows["adj_factor"] = [adj_factor] * n_days
    return pl.DataFrame(rows)


# ---------- flags ----------

def test_hit_and_or_flags():
    from lquant.data.quality.flags import hit, or_flags

    df = pl.DataFrame({"v": [1.0, -1.0]})
    out = or_flags(df, hit(PRICE_OUT_OF_RANGE, pl.col("v") < 0))
    assert out["quality_flags"].to_list() == [0, PRICE_OUT_OF_RANGE]
    # 已有 quality_flags 时做 OR 合并而不是覆盖
    out2 = or_flags(out, hit(SUSPENSION_FILL, pl.col("v") > 0))
    assert out2["quality_flags"].to_list() == [SUSPENSION_FILL, PRICE_OUT_OF_RANGE]


# ---------- 八项记录级断言 ----------

def test_clean_bars_no_flags():
    from lquant.data.quality.asserts import run_record_checks

    out, issues = run_record_checks(_good_bars(), raise_on_fatal=False)
    assert out["quality_flags"].to_list() == [0] * 5
    assert issues == []


def test_price_out_of_range_fatal():
    from lquant.data.quality.asserts import run_record_checks

    df = _good_bars().with_columns(pl.when(pl.arange(0, 5) == 2)
                                   .then(0.05).otherwise(pl.col("close")).alias("close"))
    out, issues = run_record_checks(df, raise_on_fatal=False)
    assert issues[0].rule == "PRICE_RANGE" and issues[0].severity == "fatal"
    assert (out["quality_flags"] & PRICE_OUT_OF_RANGE).to_list() == [0, 0, 1, 0, 0]
    # raise_on_fatal=True 默认抛 DataQualityError
    from lquant.core.errors import DataQualityError
    with pytest.raises(DataQualityError):
        run_record_checks(df)


def test_ohlc_and_negative_volume_fatal():
    from lquant.data.quality.asserts import run_record_checks

    df = _good_bars().with_columns(high=pl.col("low"))           # high < open/close
    _, issues = run_record_checks(df, raise_on_fatal=False)
    assert issues[0].rule == "OHLC_CONFLICT" and issues[0].severity == "fatal"

    df2 = _good_bars().with_columns(volume=-1.0)
    _, issues2 = run_record_checks(df2, raise_on_fatal=False)
    assert issues2[0].rule == "NEG_QTY" and issues2[0].severity == "fatal"


def test_null_pre_close_is_warn_not_fatal():
    """上市首日 pre_close 为空 —— 打标告警，绝不阻断整批 200 只（H6 教训）。"""
    from lquant.data.quality.asserts import run_record_checks
    from lquant.data.quality.flags import PRE_CLOSE_MISSING

    df = _good_bars().with_columns(
        pre_close=pl.when(pl.arange(0, 5) == 0).then(None).otherwise(pl.col("pre_close")))
    out, issues = run_record_checks(df, raise_on_fatal=False)   # 不抛
    assert not any(i.severity == "fatal" for i in issues)
    assert out["quality_flags"].to_list()[0] & PRE_CLOSE_MISSING
    assert out["quality_flags"].to_list()[1] == 0


def test_unit_mismatch_fatal():
    """amount 用了万元（差 1e4）→ ratio 出带宽 → fatal，且规则名独立。"""
    from lquant.data.quality.asserts import run_record_checks

    df = _good_bars().with_columns(amount=pl.col("amount") / 1e4)
    _, issues = run_record_checks(df, raise_on_fatal=False)
    assert issues[0].rule == "UNIT_MISMATCH" and issues[0].severity == "fatal"


def test_suspension_and_zombie_warn():
    from lquant.data.quality.asserts import run_record_checks

    # 零成交：open == close → 僵尸；open != close → 停牌填充
    df = _good_bars().with_columns(
        volume=pl.when(pl.arange(0, 5).is_in([1, 2])).then(0.0)
        .otherwise(pl.col("volume")))
    df = df.with_columns(
        close=pl.when(pl.arange(0, 5) == 2).then(pl.col("high"))
        .otherwise(pl.col("close")))
    out, issues = run_record_checks(df, raise_on_fatal=False)
    rules = {i.rule for i in issues}
    assert {"SUSPENSION_FILL", "ZOMBIE"} <= rules
    assert all(i.severity == "warn" for i in issues)
    flags = out["quality_flags"].to_list()
    assert flags[1] & ZOMBIE and flags[2] & SUSPENSION_FILL


def test_adj_jump_flag():
    from lquant.data.quality.asserts import run_record_checks

    df = _good_bars(adj_factor=1.0)
    df = df.with_columns(adj_factor=pl.when(pl.arange(0, 5) == 3)
                         .then(2.0).otherwise(pl.col("adj_factor")))
    out, issues = run_record_checks(df, raise_on_fatal=False)
    assert any(i.rule == "ADJ_JUMP" for i in issues)
    assert out["quality_flags"].to_list()[3] & ADJ_ANOMALY


def test_assert_no_dup():
    from lquant.core.errors import DataQualityError
    from lquant.data.quality.asserts import assert_no_dup

    assert_no_dup(_good_bars(), ["symbol", "trade_date"])
    with pytest.raises(DataQualityError):
        assert_no_dup(pl.concat([_good_bars(), _good_bars().head(1)]),
                      ["symbol", "trade_date"])


# ---------- validators ----------

def test_limit_breach_uses_board_and_st():
    from lquant.data.quality.validators import check_limit_breach

    # 主板 10%：单行 +15% 越界；占比 1/5 > 0.5% → error
    bars = _good_bars().with_columns(close=pl.when(pl.arange(0, 5) == 0)
                                     .then(pl.col("pre_close") * 1.15)
                                     .otherwise(pl.col("close")))
    sec = pl.DataFrame({"symbol": ["000001.SZ"], "board": ["main"], "is_st": [False]})
    issues = check_limit_breach(bars, sec)
    assert issues and issues[0].severity == "error" and issues[0].rule == "LIMIT_BREACH"

    # 少量命中（占比低）→ warn
    tiny = pl.concat([_good_bars()] * 100)      # 500 行里 1 行越界 = 0.2%
    tiny = tiny.with_columns(close=pl.when(pl.arange(0, 500) == 0)
                             .then(pl.col("pre_close") * 1.15).otherwise(pl.col("close")))
    issues2 = check_limit_breach(tiny, sec)
    assert issues2 and issues2[0].severity == "warn"

    # ST 5%：+15% 越界
    st_sec = pl.DataFrame({"symbol": ["000001.SZ"], "board": ["main"], "is_st": [True]})
    assert check_limit_breach(bars, st_sec)


def test_limit_breach_board_comes_from_code_not_snapshot():
    """板性以**代码段**为准：security.board 实测对全部股票都是 NULL，
    旧实现 fill_null("main") 把创业板/科创板/北交所的合法 20%/30% 波动
    全判成越界（实测 2026-08 起一个月窗口 1191 行命中里 1152 行是这么来的）。
    """
    from lquant.data.quality.validators import board_from_code, check_limit_breach

    assert board_from_code("300001.SZ") == "gem"
    assert board_from_code("688001.SH") == "star"
    assert board_from_code("689009.SH") == "star"      # 科创板 CDR
    assert board_from_code("920001.BJ") == "bse"
    assert board_from_code("600519.SH") == "main"
    assert board_from_code("510300.SH") == "unknown"   # 基金：代码段无板性

    bars = _good_bars().with_columns(close=pl.when(pl.arange(0, 5) == 0)
                                     .then(pl.col("pre_close") * 1.15)
                                     .otherwise(pl.col("close")))
    # 创业板 +15% 合法（20% 档）——即使快照里的 board 是 null
    gem = bars.with_columns(symbol=pl.lit("300001.SZ"))
    sec_null = pl.DataFrame({"symbol": ["300001.SZ"], "board": [None], "is_st": [False]})
    assert check_limit_breach(gem, sec_null) == []

    # 快照里的板性写错（主板代码却标 gem）也不能放宽：代码段优先
    sec_wrong = pl.DataFrame({"symbol": ["000001.SZ"], "board": ["gem"], "is_st": [False]})
    assert check_limit_breach(bars, sec_wrong) != []


def test_limit_breach_tolerates_dirty_symbol_and_thin_security_table():
    """兜底分支：代码段解析不了、security 连 board 列都没有 —— 都不能抛。

    这两条都是防守路径（上游串码 / 老 schema 的快照表）。质量门禁自己抛异常，
    等于从「报告问题」退化成「自己就是问题」—— 门禁不可用会掩盖真数据问题。
    未知板性一律不参与越界判定（宁可少报也不误报）。
    """
    from lquant.data.quality.validators import board_from_code, check_limit_breach

    assert board_from_code("not-a-symbol") == "unknown"
    assert board_from_code("") == "unknown"

    bars = _good_bars().with_columns(
        symbol=pl.lit("BADCODE"),
        close=pl.col("pre_close") * 1.5,      # 涨 50%：任何已知板性都该命中
    )
    # 老 schema 的 security：没有 board / sec_type / list_date 列
    sec = pl.DataFrame({"symbol": ["BADCODE"], "is_st": [False]})
    assert check_limit_breach(bars, sec) == []


def test_limit_breach_exempts_new_listings_and_tick_rounding():
    """两条实测误报源：上市初期无涨跌幅限制、涨跌停价取整到分。"""
    from lquant.data.quality.validators import check_limit_breach

    # 上市首日 +290%（601123.SH 2026-09-01 实测），list_date == 当日
    ipo = pl.DataFrame({
        "symbol": ["601123.SH"], "trade_date": [date(2026, 9, 1)],
        "close": [26.0], "pre_close": [6.65],
    })
    sec = pl.DataFrame({"symbol": ["601123.SH"], "board": [None], "is_st": [False],
                        "sec_type": ["stock"], "list_date": [date(2026, 9, 1)]})
    assert check_limit_breach(ipo, sec) == []

    # 同一只票上市满 6 个交易日后 +15% → 真的越界（主板 10%）
    late = pl.DataFrame({
        "symbol": ["601123.SH"] * 6,
        "trade_date": [date(2026, 9, 1 + i) for i in range(6)],
        "close": [6.65, 7.0, 7.0, 7.0, 7.0, 7.0 * 1.15],
        "pre_close": [6.65, 6.65, 7.0, 7.0, 7.0, 7.0],
    })
    assert check_limit_breach(late, sec) != []

    # 窗口起点晚于上市日 → 无法确认「上市几天了」，不豁免
    tail_only = pl.DataFrame({
        "symbol": ["601123.SH"], "trade_date": [date(2026, 10, 8)],
        "close": [7.0 * 1.5], "pre_close": [7.0],
    })
    assert check_limit_breach(tail_only, sec) != []

    # 边界：窗口起点只晚于上市日一两天（IPO 正好卡在窗口边缘）。
    # 实测 301632.SZ 上市 2025-08-12、窗口首条 08-13 = 上市第 2 个交易日，
    # 双创前 5 日不设限，当日 +28.2% 合法 —— 旧判据（list_date >= 窗口起点）
    # 会把它误报成超档。
    edge = pl.DataFrame({
        "symbol": ["301632.SZ"], "trade_date": [date(2025, 8, 13)],
        "close": [43.6], "pre_close": [34.01],
    })
    sec_edge = pl.DataFrame({"symbol": ["301632.SZ"], "board": [None], "is_st": [False],
                             "sec_type": ["stock"],
                             "list_date": [date(2025, 8, 12)]})
    assert check_limit_breach(edge, sec_edge) == []
    # 但间隔拉长到明显属于「历史中途切进来的窗口」就不豁免了
    far = edge.with_columns(trade_date=pl.lit(date(2025, 12, 1), dtype=pl.Date))
    assert check_limit_breach(far, sec_edge) != []

    # 低价股跌停取整：688496.SH 2026-09-10 pre_close 0.73 → 跌停价
    # 0.584 取整 0.58，收益率 20.55% > 20%+0.005，实为合法成交
    low = pl.DataFrame({"symbol": ["688496.SH"], "trade_date": [date(2026, 9, 10)],
                        "close": [0.58], "pre_close": [0.73]})
    sec_low = pl.DataFrame({"symbol": ["688496.SH"], "board": [None], "is_st": [False],
                            "sec_type": ["stock"],
                            "list_date": [date(2022, 12, 28)]})
    assert check_limit_breach(low, sec_low) == []

    # 指数/债券不参与（点位不是价格）
    idx = pl.DataFrame({"symbol": ["000300.SH"], "trade_date": [date(2026, 9, 1)],
                        "close": [5000.0], "pre_close": [3000.0]})
    sec_idx = pl.DataFrame({"symbol": ["000300.SH"], "board": [None], "is_st": [False],
                            "sec_type": ["index"], "list_date": [date(2005, 4, 8)]})
    assert check_limit_breach(idx, sec_idx) == []

    # 基金走宽档：创业板 ETF 的单日 19.77% 不是错误（159287.SZ 实测）
    etf = pl.DataFrame({"symbol": ["159287.SZ"], "trade_date": [date(2026, 8, 10)],
                        "close": [1.357], "pre_close": [1.133]})
    sec_etf = pl.DataFrame({"symbol": ["159287.SZ"], "board": [None], "is_st": [False],
                            "sec_type": ["etf"], "list_date": [date(2025, 8, 27)]})
    assert check_limit_breach(etf, sec_etf) == []


def test_ret_identity():
    from lquant.data.quality.validators import check_ret_identity

    hfq = pl.Series([10.0, 11.0, 12.1])
    qfq = hfq / 2.0                              # 常数因子，收益率恒等
    assert check_ret_identity(qfq, hfq) == []
    bad = pl.Series([10.0, 10.5, 12.1])          # 调错一段
    issues = check_ret_identity(bad, hfq, symbol="000001.SZ")
    assert issues and issues[0].rule == "ADJ_RET_IDENTITY"
    assert issues[0].severity == "error" and issues[0].symbol == "000001.SZ"


def test_ret_identity_misaligned_nulls():
    """停牌缺口不在同一行 —— 整行丢弃后对齐，合法数据不能误判（M1 教训）。"""
    from lquant.data.quality.validators import check_ret_identity

    qfq = pl.Series([10.0, None, 12.0, 12.5])
    hfq = pl.Series([20.0, 21.0, None, 25.0])    # qfq×2，数学上恒等
    assert check_ret_identity(qfq, hfq) == []


def _year_calendar(year: int, n: int = 242) -> list:
    """构造 n 个工作日（周一~周五），模拟一年交易日历。"""
    out = []
    d = date(year, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_calendar_alignment():
    from lquant.data.quality.validators import check_calendar_alignment

    cal = _year_calendar(2024) + _year_calendar(2025) + _year_calendar(2026)
    dates = cal[:5]
    assert check_calendar_alignment(dates, cal) == []
    stray = check_calendar_alignment([*dates, date(2026, 12, 26)], cal)   # 周六
    assert stray and stray[0].rule == "CALENDAR_STRAY" and stray[0].severity == "fatal"
    # 完整年天数异常 → fatal；窗口首尾年（跨年切片/进行中）不检查（H5 教训）
    short_mid = _year_calendar(2024) + _year_calendar(2025)[:100] + _year_calendar(2026)
    bad = check_calendar_alignment(dates, short_mid)
    assert bad and bad[0].rule == "CALENDAR_YEAR_LEN" and bad[0].severity == "fatal"
    assert bad[0].extra["years"] == {2025: 100}
    assert check_calendar_alignment(dates, cal[:100]) == []   # 尾年不满 → 不判


def test_coverage():
    """覆盖度以窗口中位数为基准 —— 抓突降，不拿今天的活跃清单当分母
    把整段历史误判成缺数（H4 教训）。"""
    from lquant.data.quality.validators import check_coverage

    two = pl.concat([_good_bars(), _good_bars().with_columns(symbol=pl.lit("600000.SH"))])
    assert check_coverage(two) == []
    # 某天只剩一只（另一只缺数）→ 突降 50% → fatal
    half = two.filter(~((pl.col("trade_date") == date(2026, 1, 7))
                        & (pl.col("symbol") == "600000.SH")))
    issues = check_coverage(half)
    assert len(issues) == 1 and issues[0].rule == "COVERAGE"
    assert issues[0].severity == "fatal" and issues[0].trade_date == date(2026, 1, 7)


def test_zombie_day():
    from lquant.data.quality.validators import check_zombie

    # 两个 symbol，其中一天全零收益（close == pre_close 且与昨日相同）
    df = pl.concat([
        _good_bars(),
        _good_bars().with_columns(symbol=pl.lit("600000.SH")),
    ])
    # 把两个 symbol 的某一天 close 都改成与前一天相同 → 零收益占比 100%
    dup = df.with_columns(close=pl.when(pl.col("trade_date") == date(2026, 1, 6))
                          .then(10.0).otherwise(pl.col("close")))
    issues = check_zombie(dup)
    assert issues and issues[0].rule == "ZOMBIE_DAY" and issues[0].severity == "error"
    assert check_zombie(_good_bars()) == []


def test_adj_factor_checks():
    from lquant.data.quality.validators import check_adj_factor

    # 后复权因子递减 → error
    df = _good_bars(adj_factor=1.0)
    df = df.with_columns(adj_factor=pl.when(pl.arange(0, 5) == 3)
                         .then(0.5).otherwise(pl.col("adj_factor")))
    issues = check_adj_factor(df)
    rules = {i.rule: i.severity for i in issues}
    assert rules.get("ADJ_DECREASE") == "error"
    assert rules.get("ADJ_JUMP") == "warn"
    assert check_adj_factor(_good_bars(adj_factor=1.0)) == []


# ---------- 落库部分（隔离 tmp duckdb）----------

def _ensure_cwd():
    """cwd 指向的目录被删（pytest tmp 清理）时，os.getcwd() 会炸 —— 先兜底恢复。"""
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir(os.path.expanduser("~"))


@pytest.fixture(scope="module")
def q_env(tmp_path_factory):
    _ensure_cwd()
    base = tmp_path_factory.mktemp("quality")
    old_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield base
    # 恢复 cwd —— chdir 泄漏会污染后续测试模块（M9 教训）
    os.chdir(old_cwd)
    get_settings.cache_clear()


def test_issues_roundtrip(q_env):
    from lquant.data.quality.issues import Issue, latest_issues, resolve_issue, save_issues

    n = save_issues([
        Issue(rule="ZOMBIE", severity="warn", detail="x", count=3),
        Issue(rule="LIMIT_BREACH", severity="error", detail="y", count=1,
              symbol="000001.SZ", trade_date=date(2026, 1, 6)),
    ], data_version="20260109.1")
    assert n == 2
    rows = latest_issues()
    assert len(rows) == 2
    by_rule = {r["rule_code"]: r for r in rows}
    assert by_rule["LIMIT_BREACH"]["severity"] == "error"
    assert by_rule["LIMIT_BREACH"]["data_version"] == "20260109.1"
    assert by_rule["LIMIT_BREACH"]["detail"]["message"] == "y"
    assert resolve_issue(by_rule["ZOMBIE"]["issue_id"])
    assert [r["rule_code"] for r in latest_issues()] == ["LIMIT_BREACH"]
    assert save_issues([]) == 0
    # 同一问题重复检出 → 内容指纹相同 → 覆盖原行而不是无限增长（M4 教训）
    save_issues([Issue(rule="LIMIT_BREACH", severity="error", detail="y", count=1,
                       symbol="000001.SZ", trade_date=date(2026, 1, 6))])
    assert len(latest_issues()) == 1


def test_issues_mixed_trade_date_none_and_date(q_env):
    """回归：多数行 trade_date=None（标的级稀疏）+ 少数行是真实 date
    （整日缺失）—— 首行 None 被 polars 推断为 Null 类型，后续 date
    追加炸 builder（/data/gaps 502 的根因）。显式 schema 后必须通过。"""
    from lquant.data.quality.issues import Issue, save_issues

    n = save_issues([
        Issue(rule="COVERAGE_GAP", severity="error", detail="s", count=3,
              symbol="000001.SZ", trade_date=None),
        Issue(rule="COVERAGE_GAP", severity="error", detail="g", count=100,
              trade_date=date(2026, 9, 18)),
    ])
    assert n == 2


def test_lineage(q_env):
    from lquant.data import lineage

    # 契约：new_version 只看库内已有版本，用完必须 register，否则同日重号
    v1 = lineage.new_version(datetime(2026, 1, 9, 10, 0))
    lineage.register(v1, "daily_bar", row_count=100)
    v2 = lineage.new_version(datetime(2026, 1, 9, 11, 0))
    lineage.register(v2, "daily_bar", row_count=80)
    v3 = lineage.new_version(datetime(2026, 1, 10, 9, 0))
    assert v1 == "20260109.1" and v2 == "20260109.2" and v3 == "20260110.1"
    lineage.register(v3, "daily_bar", row_count=50, trade_date=date(2026, 1, 10))
    assert lineage.latest("daily_bar") == v3
    assert lineage.latest("nonexistent") is None
    stamped = lineage.stamp(_good_bars(), "baostock", version="20260109.1")
    assert set(stamped.columns) >= {"source", "ingested_at", "data_version"}
    assert stamped["data_version"].unique().to_list() == ["20260109.1"]


def test_golden_freeze_and_run(q_env):
    from lquant.core.db import writer
    from lquant.data.quality.golden import GoldenCase, freeze, list_cases, run_all
    with writer() as w:
        w.register("_gb", _good_bars())
        w.execute("CREATE TABLE golden_src AS SELECT * FROM _gb")
    cases = [GoldenCase(name="bar_count", kind="structural",
                        sql="SELECT count(*) FROM golden_src", tolerance=0.0)]
    assert freeze(cases) == 1
    assert {c.name for c in list_cases()} == {"bar_count"}
    results = run_all()
    assert len(results) == 1 and results[0].ok and results[0].actual == 5.0
    # 数据变了 → golden 失败 → as_issue 是 error 级
    with writer() as w:
        w.execute("INSERT INTO golden_src SELECT * FROM golden_src LIMIT 1")
    results2 = run_all()
    assert not results2[0].ok
    issue = results2[0].as_issue()
    assert issue.rule == "GOLDEN_bar_count" and issue.severity == "error"
    # 冻结空查询必须报错，不允许埋噪音
    with pytest.raises(ValueError):
        freeze([GoldenCase(name="empty", kind="structural",
                           sql="SELECT NULL WHERE 1 = 0")])


def test_gate_daily_saves_issues_before_raise(q_env):
    """fatal 时 issue 也要先落库 —— 留证据，不是抛完就没了。"""
    from lquant.core.errors import DataQualityError
    from lquant.data.quality.issues import latest_issues
    from lquant.data.quality.pipeline import gate_daily

    bad = _good_bars().with_columns(close=0.05)
    with pytest.raises(DataQualityError):
        gate_daily(bad, data_version="20260109.1")
    rows = latest_issues()
    assert any(r["rule_code"] == "PRICE_RANGE" and r["severity"] == "fatal"
               for r in rows)

    # warn 不阻断，返回打标后的 df（close 抬到 high，保持 OHLC 一致）
    warn_df = _good_bars().with_columns(
        volume=pl.when(pl.arange(0, 5) == 0).then(0.0).otherwise(pl.col("volume")),
        close=pl.when(pl.arange(0, 5) == 0).then(pl.col("high"))
        .otherwise(pl.col("close")))
    out, issues = gate_daily(warn_df, data_version="20260109.1")
    assert any(i.rule == "SUSPENSION_FILL" for i in issues)
    assert out["quality_flags"].to_list()[0] & SUSPENSION_FILL


# ---------- VolumePctSlippage 回归（历史 bug：Broker 只传 2 参，冲击从未生效）----------

def test_volume_slippage_not_silently_degraded():
    from lquant.backtest.broker import Broker
    from lquant.backtest.events import Bar, Side
    from lquant.backtest.slippage import VolumePctSlippage

    bar = Bar(symbol="000001.SZ", trade_date=date(2026, 1, 6),
              open=10.0, high=10.5, low=9.5, close=10.2, pre_close=10.0,
              volume=1e6, amount=1e7)
    broker = Broker(rules={}, slippage=VolumePctSlippage(base_rate=0.0))

    small = broker._price(bar, Side.BUY, qty=100.0)     # 占比 1e-4 → sqrt = 0.01
    big = broker._price(bar, Side.BUY, qty=5e4)         # 占比 5%（打满 cap）
    assert small == pytest.approx(10.0 * (1 + 0.1 * (1e-4) ** 0.5))
    assert big > small > 10.0                            # 冲击随占比上升

    # 聚宽风格 2 参 duck-type 模型也能接入（不传量参数）
    class JQStyle:
        def apply(self, price, side):
            return price * 1.001 if side == Side.BUY else price * 0.999

    jq_broker = Broker(rules={}, slippage=JQStyle())
    assert jq_broker._price(bar, Side.BUY) == pytest.approx(10.01)


def test_impact_priced_on_filled_qty_not_order_qty():
    """冲击成本按实际成交量计，不给被截掉的部分付费（H1 教训）。

    委托 10 万股、引擎按成交量截到 1 万股 → 冲击必须按 1% 占比算；
    若按委托量算会打到 cap（5%），成本系统性虚高。
    """
    from datetime import date as _date

    from lquant.backtest.broker import Broker
    from lquant.backtest.events import Bar, Order, Side
    from lquant.backtest.rules.model import (
        Commission,
        InstrumentRules,
        PriceLimit,
        TaxSchedule,
    )
    from lquant.backtest.slippage import VolumePctSlippage
    from lquant.core.types import SecType, Symbol

    rules = InstrumentRules(
        symbol=Symbol("000001", "SZ"), sec_type=SecType.STOCK,
        commission=Commission(rate=0.0, min=0.0),
        tax=TaxSchedule([(_date(2000, 1, 1), _date(9999, 12, 31), 0.0)]),
        transfer_fee_rate=0.0,
        price_limit=PriceLimit("by_board", {"main": 0.10}),
        lot_size=100, sellable_after_days=1,
    )
    bar = Bar(symbol="000001.SZ", trade_date=_date(2026, 1, 6),
              open=10.0, high=10.5, low=9.5, close=10.2, pre_close=10.0,
              volume=1e6, amount=1e7)
    broker = Broker(rules={"000001.SZ": rules},
                    slippage=VolumePctSlippage(base_rate=0.0))

    order = Order(order_id="o1", symbol="000001.SZ", side=Side.BUY, qty=1e5)
    fill = broker.match(order, bar, _date(2026, 1, 6), max_qty=1e4)
    assert fill is not None and fill.qty == 1e4
    # 实际占比 1e4/1e6 = 1% → sqrt = 0.1 → rate = 0.01 → 10.10
    assert fill.price == pytest.approx(10.0 * (1 + 0.1 * (0.01 ** 0.5)))
    # 不带 max_qty 时按委托量打满 cap → 价格更高，证明截断发生在计价之前
    order2 = Order(order_id="o2", symbol="000001.SZ", side=Side.BUY, qty=1e5)
    fill2 = broker.match(order2, bar, _date(2026, 1, 6))
    assert fill2.price > fill.price
