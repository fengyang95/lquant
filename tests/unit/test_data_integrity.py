"""B1「值级覆盖检测 + 自动修复」回归：**行数相同但收盘价错了**。

核心用例（:func:`test_row_count_blind_while_value_check_catches`）刻意构造
「分区与权威来源行数完全一致、只有 close 差 0.01」的分区：模拟 2026 年那次
真实事故（实时端点收盘后长期返回旧价，3392/5554 只股票收盘价与官方日线不符，
行数校验零命中）。其余用例覆盖哨兵三态、覆盖集口径、dry-run 不落盘。
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from lquant.data.quality import integrity as ig

CN = timezone(timedelta(hours=8))
DAY = date(2026, 9, 10)


def _ms(hour: int, minute: int = 0, *, day: date = DAY) -> int:
    """上海墙钟 → epoch 毫秒（哨兵列的口径）。"""
    return int(
        datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN).timestamp() * 1000
    )


def _frame(
    symbols: list[str],
    *,
    day: date = DAY,
    close: float | list[float] = 10.0,
    volume: float | list[float] = 1_000_000.0,
    suspended: bool | list[bool] = False,
    quote_ts: list[int | None] | None = None,
) -> pl.DataFrame:
    """构造一个交易日的分区帧（列名对齐 DAILY_BAR 的关键子集）。"""
    n = len(symbols)

    def rep(value):
        return [value] * n if not isinstance(value, list) else value

    data: dict[str, list] = {
        "symbol": symbols,
        "trade_date": [day] * n,
        "close": rep(close),
        "volume": rep(volume),
        "is_suspended": rep(suspended),
    }
    if quote_ts is not None:
        data["quote_ts"] = quote_ts
    return pl.DataFrame(data)


def _write_day(root: Path, day: date, frame: pl.DataFrame,
               name: str = "part-0.parquet") -> Path:
    """按参考实现的 date= 日分区布局写盘。"""
    part = root / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / name
    frame.write_parquet(path)
    return path


def _write_year(root: Path, frames: list[pl.DataFrame],
                year: int = 2026) -> Path:
    """按 lquant 的 year=YYYY/part-0.parquet 年文件布局写盘。"""
    part = root / f"year={year}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / "part-0.parquet"
    pl.concat(frames, how="diagonal_relaxed").write_parquet(path)
    return path


# ---------------------------------------------------------------------------
# 值级比对：行数盲区
# ---------------------------------------------------------------------------
def test_row_count_blind_while_value_check_catches(tmp_path: Path) -> None:
    """行数完全相同、只有 close 差 0.01 —— 行数校验无感，值级校验必须报。

    这是本次要防的核心盲区：真实事故里 3392/5554 只股票的当日收盘价与官方
    日线不符，但每只都有行。
    """
    day = DAY
    partition = tmp_path / "polluted"
    _write_day(
        partition, day,
        _frame(["000001.SZ", "600519.SH", "300750.SZ"],
               close=[10.00, 1700.00, 200.00]),
    )
    authority = _frame(["000001.SZ", "600519.SH", "300750.SZ"],
                       close=[10.01, 1700.00, 200.00])

    # 修改前的检测盲区：按行数看，分区与权威来源「一致」，不会触发任何修复。
    lake_rows = ig.read_partition_frame(partition, day).height
    assert lake_rows == authority.height  # 行数校验在这里是零信号

    result = ig.check_partition_values(partition, day, authority)

    assert result.status == ig.VALUE_MISMATCH
    assert result.checked == 3
    assert [m.symbol for m in result.mismatches] == ["000001.SZ"]
    assert abs(result.mismatches[0].diff - 0.01) < 1e-9


def test_value_tolerance_is_half_tick(tmp_path: Path) -> None:
    """阈值口径：差值 = 半个最小报价单位（0.005）不算不一致，超过才算。"""
    day = DAY
    partition = tmp_path / "p"
    _write_day(partition, day, _frame(["000001.SZ"], close=[10.00]))

    at_boundary = ig.check_partition_values(
        partition, day, _frame(["000001.SZ"], close=[10.005])
    )
    above = ig.check_partition_values(
        partition, day, _frame(["000001.SZ"], close=[10.006])
    )

    assert at_boundary.status == ig.VALUE_OK
    assert ig.HALF_TICK == 0.005
    assert above.status == ig.VALUE_MISMATCH


def test_missing_authority_is_explicit_not_a_pass(tmp_path: Path) -> None:
    """没有权威来源时如实说「无法比对」——绝不拿分区自己跟自己比。"""
    day = DAY
    partition = tmp_path / "p"
    _write_day(partition, day, _frame(["000001.SZ"], close=[10.0]))

    result = ig.check_partition_values(partition, day, None)

    assert result.status == ig.VALUE_NO_AUTHORITY
    assert not result.comparable  # 不是「通过」，是「没比」
    assert "无法比对" in result.note


def test_authority_pointing_back_at_lake_is_rejected(tmp_path: Path) -> None:
    """权威来源指回被检查的表根 = 自己比自己（必然恒一致）→ 拒绝。"""
    day = DAY
    partition = tmp_path / "lake"
    _write_day(partition, day, _frame(["000001.SZ"], close=[10.0]))

    result = ig.check_partition_values(partition, day, partition)

    assert result.status == ig.VALUE_SELF_REFERENCE


# ---------------------------------------------------------------------------
# 覆盖集比对：按停牌过滤后的集合，而不是行数
# ---------------------------------------------------------------------------
def test_coverage_ignores_suspended_symbols(tmp_path: Path) -> None:
    """正常剔除停牌会少行 —— 集合口径判「完整」，行数口径会误报。"""
    day = DAY
    partition = tmp_path / "p"
    # 分区只有 3 行（停牌股按正常流程被剔除）
    _write_day(
        partition, day,
        _frame(["000001.SZ", "600519.SH", "300750.SZ"]),
    )
    # 权威来源 4 行，其中一行停牌（volume=0 且 is_suspended=True）
    authority = _frame(
        ["000001.SZ", "600519.SH", "300750.SZ", "601988.SH"],
        volume=[1e6, 1e6, 1e6, 0.0],
        suspended=[False, False, False, True],
    )

    result = ig.check_partition_coverage(partition, day, authority)

    assert result.status == ig.COVERAGE_OK
    assert result.n_missing == 0
    # 行数确实不同（4 vs 3）——若按行数比对，这个健康分区会被判为不完整
    assert result.row_count_differs


def test_coverage_reports_real_missing_symbol(tmp_path: Path) -> None:
    """真缺一个可交易标的时必须报出来（不是把所有少行都当正常）。"""
    day = DAY
    partition = tmp_path / "p"
    _write_day(partition, day, _frame(["000001.SZ", "600519.SH"]))
    authority = _frame(["000001.SZ", "600519.SH", "300750.SZ"])

    result = ig.check_partition_coverage(partition, day, authority)

    assert result.status == ig.COVERAGE_GAP
    assert result.missing == ("300750.SZ",)
    assert result.n_missing == 1


def test_coverage_without_halt_evidence_is_explicit(tmp_path: Path) -> None:
    """权威来源没有停牌证据 → 无法构建期望覆盖集，明说「判不了」。"""
    day = DAY
    partition = tmp_path / "p"
    _write_day(partition, day, _frame(["000001.SZ"]))
    authority = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "trade_date": [day],
        "close": [10.0],
    })

    result = ig.check_partition_coverage(partition, day, authority)

    assert result.status == ig.COVERAGE_NO_HALT_FILTER


# ---------------------------------------------------------------------------
# 哨兵
# ---------------------------------------------------------------------------
def test_missing_sentinel_is_unknown_not_authoritative(tmp_path: Path) -> None:
    """哨兵缺失 → UNKNOWN。这正是事故分区「永远不进修复」的成因所在。"""
    day = DAY
    partition = tmp_path / "p"
    _write_day(partition, day, _frame(["000001.SZ"]))  # 没有 quote_ts 列，也没有 meta

    verdict = ig.partition_is_snapshot(partition, day)

    assert verdict.state == ig.UNKNOWN
    assert verdict.is_unknown
    assert not verdict.is_snapshot
    assert "不能假定为权威历史" in verdict.reason


def test_sentinel_column_states(tmp_path: Path) -> None:
    """哨兵列三态：收盘前 → 快照；全空/盘后 → 权威。"""
    day = DAY
    root = tmp_path / "lake"

    pre = root / "pre"
    _write_day(pre, day, _frame(["000001.SZ"], quote_ts=[_ms(14, 30)]))
    assert ig.partition_is_snapshot(pre, day).state == ig.SNAPSHOT

    post = root / "post"
    _write_day(post, day, _frame(["000001.SZ"], quote_ts=[_ms(15, 30)]))
    assert ig.partition_is_snapshot(post, day).state == ig.AUTHORITATIVE

    batch = root / "batch"
    _write_day(batch, day, _frame(["000001.SZ"], quote_ts=[None]))
    assert ig.partition_is_snapshot(batch, day).state == ig.AUTHORITATIVE


def test_suspended_realtime_residue_is_not_snapshot(tmp_path: Path) -> None:
    """停牌股的收盘前零成交实时残留 + 批量权威行 → 不算快照。

    实时轮询会写入停牌股 09:15/零成交的孤立行；batch 侧过滤停牌日不会覆盖
    它们。若把这种残留当快照，分区会反复进入修复（参考实现踩过的坑）。
    """
    day = DAY
    partition = tmp_path / "p"
    batch = _frame(["000001.SZ", "600519.SH", "300750.SZ"], quote_ts=[None] * 3)
    residue = _frame(
        ["601988.SH"], volume=[0.0], suspended=[True], quote_ts=[_ms(9, 15)]
    )
    _write_day(partition, day, batch, name="part-0.parquet")
    _write_day(partition, day, residue, name="part-1.parquet")

    verdict = ig.partition_is_snapshot(partition, day)

    assert verdict.state == ig.AUTHORITATIVE
    assert verdict.suspicious_rows == 1
    assert verdict.batch_rows == 3


def test_sidecar_meta_sentinel(tmp_path: Path) -> None:
    """旁挂 meta 形态：无哨兵列但有 _snapshot_meta.json 时仍能判定。"""
    day = DAY
    root = tmp_path / "lake"

    pre = root / "pre"
    _write_day(pre, day, _frame(["000001.SZ"]))
    (pre / f"date={day.isoformat()}" / ig.META_FILE).write_text(
        json.dumps({"quote_ts_ms": _ms(14, 30)}), encoding="utf-8"
    )
    assert ig.partition_is_snapshot(pre, day).state == ig.SNAPSHOT

    post = root / "post"
    _write_day(post, day, _frame(["000001.SZ"]))
    (post / f"date={day.isoformat()}" / ig.META_FILE).write_text(
        json.dumps({"days": {day.isoformat(): {"quote_ts_ms": _ms(15, 10)}}}),
        encoding="utf-8",
    )
    assert ig.partition_is_snapshot(post, day).state == ig.AUTHORITATIVE


# ---------------------------------------------------------------------------
# 修复：默认 dry-run，显式执行才落盘
# ---------------------------------------------------------------------------
def test_apply_repair_dry_run_deletes_nothing(tmp_path: Path) -> None:
    """默认 dry-run 必须只报不改 —— 删除是不可逆的破坏性动作。"""
    day = DAY
    lake = tmp_path / "lake"
    path = _write_day(lake, day, _frame(["000001.SZ"], close=[10.00]))
    authority = _frame(["000001.SZ"], close=[10.01])

    plan = ig.plan_repair(lake, [day], authority=authority)
    assert plan.repair_days == (day,)
    assert ig.REASON_VALUE_MISMATCH in plan.items[0].reasons

    report = ig.apply_repair(plan)  # 默认 dry_run=True

    assert report.dry_run is True
    assert path.exists()
    assert ig.read_partition_frame(lake, day).height == 1
    assert report.actions[0].action == "rmtree"
    assert report.deleted_rows == 1  # 报告「将要删多少」，但没删


def test_apply_repair_explicit_deletes_date_partition(tmp_path: Path) -> None:
    """显式 dry_run=False 时才真删，并逐条报告删了什么。"""
    day = DAY
    lake = tmp_path / "lake"
    path = _write_day(lake, day, _frame(["000001.SZ"], close=[10.00]))
    plan = ig.plan_repair(lake, [day], authority=_frame(["000001.SZ"], close=[10.01]))

    report = ig.apply_repair(plan, dry_run=False)

    assert report.dry_run is False
    assert not path.exists()
    assert not (lake / f"date={day.isoformat()}").exists()
    assert report.actions[0].path == str(lake / f"date={day.isoformat()}")
    assert report.deleted_rows == 1


def test_apply_repair_purges_one_day_from_year_file(tmp_path: Path) -> None:
    """lquant 年文件布局：只删该交易日的行，其余日原样保留。

    直接删年文件会把整年历史一起删掉 —— 那是灾难，所以这里必须是逐行 purge。
    """
    bad_day = DAY
    good_day = DAY + timedelta(days=1)
    lake = tmp_path / "lake"
    path = _write_year(lake, [
        _frame(["000001.SZ", "600519.SH"], day=bad_day, close=[10.00, 20.00]),
        _frame(["000001.SZ"], day=good_day, close=[11.00]),
    ])
    authority = _frame(["000001.SZ", "600519.SH"], day=bad_day, close=[10.01, 20.00])

    plan = ig.plan_repair(lake, [bad_day], authority=authority)
    report = ig.apply_repair(plan, dry_run=False)

    assert report.actions[0].action == "rewrite"
    assert report.deleted_rows == 2
    remaining = pl.read_parquet(path)
    assert set(remaining["trade_date"].to_list()) == {good_day}
    assert ig.read_partition_frame(lake, bad_day).height == 0


def test_plan_repair_keeps_unknown_sentinel_out_of_repair(tmp_path: Path) -> None:
    """哨兵未知不进修复清单：没有证据的删除是破坏，只记 unknown_sentinel。"""
    day = DAY
    lake = tmp_path / "lake"
    _write_day(lake, day, _frame(["000001.SZ"], close=[10.00]))  # 无哨兵、无权威

    plan = ig.plan_repair(lake, [day])

    assert plan.empty
    assert plan.unknown_sentinel == (day,)
    assert plan.repair_days == ()


def test_plan_repair_collects_snapshot_and_value_reasons(tmp_path: Path) -> None:
    """同一日期可同时命中「快照污染」与「值级不一致」。"""
    day = DAY
    lake = tmp_path / "lake"
    _write_day(lake, day, _frame(["000001.SZ"], close=[10.00], quote_ts=[_ms(14, 30)]))
    authority = _frame(["000001.SZ"], close=[10.01])

    plan = ig.plan_repair(lake, [day], authority=authority)

    assert plan.items[0].reasons == (ig.REASON_SNAPSHOT, ig.REASON_VALUE_MISMATCH)
    detail = plan.to_dict()["items"][0]
    assert detail["detail"]["sentinel"] == ig.SNAPSHOT
    assert detail["detail"]["n_mismatch"] == 1


# ---------------------------------------------------------------------------
# 汇总报告：只报尾部缺口
# ---------------------------------------------------------------------------
def test_integrity_report_reports_tail_gaps_only(tmp_path: Path) -> None:
    """尾部缺口照报；历史内部空洞不报（避免噪声淹没近端事故）。"""
    lake = tmp_path / "lake"
    present_days = [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 8)]
    _write_year(lake, [_frame(["000001.SZ"], day=d) for d in present_days])
    calendar = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]

    report = ig.integrity_report(
        lake,
        calendar_days=calendar,
        today=date(2026, 9, 10),
        lookback_days=30,
    )

    assert report.calendar_checked
    assert report.latest_present == date(2026, 9, 8)
    # 只有晚于湖内最新日期的缺失日才算尾部缺口
    assert report.tail_gaps == (date(2026, 9, 9), date(2026, 9, 10))
    # 09-01 是内部空洞（早于 latest），按口径不在此报告
    assert date(2026, 9, 1) not in report.tail_gaps
    assert any("内部空洞" in n for n in report.notes)


def test_integrity_report_flags_snapshot_and_missing_authority(tmp_path: Path) -> None:
    """报告能同时给出「快照污染日」与「因缺权威来源而没法比值的日子」。"""
    day = DAY
    lake = tmp_path / "lake"
    _write_day(lake, day, _frame(["000001.SZ"], close=[10.00], quote_ts=[_ms(13, 5)]))

    report = ig.integrity_report(
        lake, calendar_days=[day], today=day, lookback_days=1
    )

    assert report.snapshot_polluted == (day,)
    assert report.no_authority == (day,)
    assert any("无法做值级比对" in n for n in report.notes)


# ---------------------------------------------------------------------------
# 辅助函数分支：时间归一 / 权威来源形态 / 去重
# ---------------------------------------------------------------------------


def test_to_ms_and_sentinel_ms_forms() -> None:
    """任意时刻表示 → epoch ms；哨兵列 Int64/Datetime（naive 按上海墙钟）三态。"""
    assert ig._to_ms(None) is None and ig._to_ms(True) is None
    assert ig._to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert ig._to_ms(1.5e12) == 1_500_000_000_000
    naive = datetime(2026, 9, 10, 15, 0)
    assert ig._to_ms(naive) == _ms(15, 0)  # naive 按上海墙钟
    assert ig._to_ms(datetime(2026, 9, 10, 15, 0, tzinfo=CN)) == _ms(15, 0)
    assert ig._to_ms("1700000000000") == 1_700_000_000_000
    assert ig._to_ms("2026-09-10 15:00:00") == _ms(15, 0)
    assert ig._to_ms("不是时间") is None
    assert ig._to_ms(object()) is None

    assert ig._sentinel_ms(pl.DataFrame({"a": [1]})) is None  # 无哨兵列
    assert ig._sentinel_ms(_frame(["A"], quote_ts=[_ms(9, 30)])).to_list() == [_ms(9, 30)]
    dt = pl.DataFrame({"quote_ts": [naive]}).with_columns(
        pl.col("quote_ts").dt.replace_time_zone("Asia/Shanghai"))
    assert ig._sentinel_ms(dt).to_list() == [_ms(15, 0)]
    aware = pl.DataFrame({"quote_ts": [datetime(2026, 9, 10, 7, 0, tzinfo=UTC)]})
    assert ig._sentinel_ms(aware).to_list() == [_ms(15, 0)]


def test_load_authority_frame_forms(tmp_path: Path) -> None:
    """权威来源支持 Frame/Mapping/回调/路径/LazyFrame；拒绝自比与空来源。"""
    part = _write_day(tmp_path / "lake", DAY, _frame(["600000.SH"]))
    day_dir = part.parent
    frame = _frame(["600000.SH"])

    assert ig._load_authority_frame(None, DAY, day_dir)[1] == ig.VALUE_NO_AUTHORITY
    assert ig._load_authority_frame({"x": 1}, DAY, day_dir)[1] == ig.VALUE_NO_AUTHORITY
    got, status = ig._load_authority_frame({DAY: frame}, DAY, day_dir)
    assert status == "" and got.height == 1
    assert ig._load_authority_frame(lambda d: None, DAY, day_dir)[1] == ig.VALUE_NO_AUTHORITY
    got, status = ig._load_authority_frame(lambda d: frame, DAY, day_dir)
    assert status == "" and got.height == 1
    got, status = ig._load_authority_frame(frame.lazy(), DAY, day_dir)
    assert status == "" and got.height == 1
    got, status = ig._load_authority_frame(frame, DAY, day_dir)
    assert status == "" and got.height == 1
    assert ig._load_authority_frame(42, DAY, day_dir)[1] == ig.VALUE_NO_AUTHORITY

    # 自比陷阱：权威路径指回表根 → 直接拒绝
    assert ig._load_authority_frame(tmp_path / "lake", DAY, day_dir)[1] == ig.VALUE_SELF_REFERENCE
    assert ig._load_authority_frame(tmp_path / "nope.parquet", DAY,
                                    day_dir)[1] == ig.VALUE_NO_AUTHORITY
    # 路径形态（不是被检查的表根）能正常读入
    auth_path = tmp_path / "auth.parquet"
    frame.write_parquet(auth_path)
    got, status = ig._load_authority_frame(auth_path, DAY, day_dir)
    assert status == "" and got.height == 1

    # 权威有 trade_date 但当日无行 → empty_authority；无 trade_date 列则原样返回
    other = _frame(["600000.SH"], day=DAY + timedelta(days=1))
    assert ig._load_authority_frame(other, DAY, day_dir)[1] == ig.VALUE_EMPTY_AUTHORITY
    got, status = ig._load_authority_frame(pl.DataFrame({"symbol": ["A"]}), DAY, day_dir)
    assert status == "" and got.height == 1

    # 去重：无 symbol 列 → 原样返回 0 丢弃
    f2, dropped = ig._dedup_symbols(pl.DataFrame({"a": [1]}), "close")
    assert dropped == 0 and f2.height == 1


def test_check_partition_values_more_branches(tmp_path: Path) -> None:
    """无权威 / 权威缺列 / 无共同标的 / 重复行与超量截断提示。"""
    frame = _frame(["600000.SH", "000001.SZ"], close=[10.0, 20.0])
    part = _write_day(tmp_path / "lake", DAY, frame)

    r = ig.check_partition_values(part, DAY)
    assert r.status == ig.VALUE_NO_AUTHORITY and "无法比对" in r.note

    r = ig.check_partition_values(
        part, DAY, pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [DAY]}))
    assert r.status == ig.VALUE_NO_VALUE_COLUMN and "权威来源缺少价格列" in r.note

    r = ig.check_partition_values(part, DAY, _frame(["999999.SZ"], close=[1.0]))
    assert r.status == ig.VALUE_NO_OVERLAP

    r = ig.check_partition_values(tmp_path / "empty", DAY)
    assert r.status == ig.VALUE_EMPTY_PARTITION

    part2 = _write_day(tmp_path / "t2", DAY, _frame(["A"]).drop("close"))
    r = ig.check_partition_values(part2, DAY, _frame(["A"]).drop("close"))
    assert r.status == ig.VALUE_NO_VALUE_COLUMN and "分区缺少价格列" in r.note

    # 同标的重复行去重提示（不炸笛卡尔积）
    dup = _write_day(tmp_path / "t3", DAY,
                     _frame(["600000.SH", "600000.SH"], close=[10.0, 10.02]))
    r = ig.check_partition_values(dup, DAY, _frame(["600000.SH"], close=[10.0]),
                                  tolerance=ig.HALF_TICK)
    assert "重复行去重" in r.note

    # 不一致只数超过 max_samples → 截断提示但状态仍是 mismatch
    syms = [f"60{i:04d}.SH" for i in range(25)]
    big = _write_day(tmp_path / "t4", DAY, _frame(syms, close=[1.0] * 25))
    r = ig.check_partition_values(big, DAY, _frame(syms, close=[2.0] * 25),
                                  tolerance=ig.HALF_TICK, max_samples=5)
    assert r.status == ig.VALUE_MISMATCH and "仅列前 5 只" in r.note
    assert len(r.mismatches) == 5


def test_check_partition_coverage_more_branches(tmp_path: Path) -> None:
    """空分区 / 无权威 / 权威缺停牌证据 / 分区侧缺证据 / 超量截断。"""
    part = _write_day(tmp_path / "lake", DAY, _frame(["600000.SH"]))
    assert ig.check_partition_coverage(tmp_path / "empty",
                                       DAY).status == ig.COVERAGE_EMPTY_PARTITION
    assert ig.check_partition_coverage(part, DAY).status == ig.COVERAGE_NO_AUTHORITY

    no_halt = _frame(["600000.SH"]).drop(["volume", "is_suspended"])
    r = ig.check_partition_coverage(part, DAY, no_halt)
    assert r.status == ig.COVERAGE_NO_HALT_FILTER

    # 权威有停牌证据、分区侧没有 → missing 仍可靠，但 extra 不保证，必须写进 note
    nohalt_part = _write_day(tmp_path / "t2", DAY,
                             _frame(["600000.SH"]).drop(["volume", "is_suspended"]))
    auth = _frame(["600000.SH", "000001.SZ"])
    r = ig.check_partition_coverage(nohalt_part, DAY, auth)
    assert r.status == ig.COVERAGE_GAP and r.missing == ("000001.SZ",)
    assert "未做停牌过滤" in r.note

    # 缺口规模超采样上限 → 只列前 N 只，但计数是全量
    syms = [f"60{i:04d}.SH" for i in range(60)]
    big = _write_day(tmp_path / "t3", DAY, _frame(syms))
    small = _write_day(tmp_path / "t4", DAY, _frame(syms[:1]))
    r = ig.check_partition_coverage(small, DAY, big, max_samples=5)
    assert r.status == ig.COVERAGE_GAP and r.n_missing == 59 and len(r.missing) == 5
    assert "缺失 59 只" in r.note


# ---------------------------------------------------------------------------
# 修复计划/执行的退化路径
# ---------------------------------------------------------------------------


def test_plan_repair_read_failure_and_empty_day(tmp_path: Path) -> None:
    """坏文件只记 note 不抛；无行的日子归尾部缺口而不是修复清单。"""
    day = DAY
    lake = tmp_path / "lake"
    part = lake / f"date={day.isoformat()}"
    part.mkdir(parents=True)
    (part / "part-0.parquet").write_bytes(b"not parquet at all")

    plan = ig.plan_repair(lake, [day], authority=_frame(["000001.SZ"]))
    assert plan.empty
    assert any("读取失败" in n for n in plan.notes)

    empty_day = day + timedelta(days=1)
    plan2 = ig.plan_repair(tmp_path / "clean_lake", [empty_day])
    assert any("无行" in n for n in plan2.notes)
    assert plan2.empty


def test_count_and_purge_day_from_year_file(tmp_path: Path) -> None:
    """年文件逐日计数；缺日期列/无该日/部分命中/整文件命中四条路径。"""
    good = DAY + timedelta(days=1)
    path = _write_year(tmp_path / "lake", [
        _frame(["000001.SZ", "600519.SH"], day=DAY),
        _frame(["000001.SZ"], day=good),
    ])
    hit, total = ig._count_day_rows(path, DAY)
    assert (hit, total) == (2, 3)

    no_date = tmp_path / "nodate" / "year=2025" / "part-0.parquet"
    no_date.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"]}).write_parquet(no_date)
    assert ig._purge_day_from_file(no_date, DAY) is None  # 无 trade_date 列
    assert ig._purge_day_from_file(path, date(2026, 9, 30)) is None  # 无该日行

    action, rows = ig._purge_day_from_file(path, DAY)
    assert (action, rows) == ("rewrite", 2)
    assert pl.read_parquet(path).height == 1

    solo = _write_year(tmp_path / "solo", [_frame(["000001.SZ"], day=DAY)])
    assert ig._purge_day_from_file(solo, DAY) == ("unlink", 1)
    assert not solo.exists()


def test_apply_repair_error_is_isolated_per_day(tmp_path: Path) -> None:
    """单个分区失败写进 errors，不中断其余分区。"""
    bad = ig.RepairItem(day=DAY, reasons=(ig.REASON_VALUE_MISMATCH,), detail={},
                        paths=(str(tmp_path / "ghost" / "date=2026-09-10"),))
    good_dir = tmp_path / "date=2026-09-11"
    good_dir.mkdir(parents=True)
    _frame(["000001.SZ"], day=DAY + timedelta(days=1)).write_parquet(
        good_dir / "part-0.parquet")
    good = ig.RepairItem(day=DAY + timedelta(days=1),
                         reasons=(ig.REASON_VALUE_MISMATCH,), detail={},
                         paths=(str(good_dir),))
    plan = ig.RepairPlan(table_dir=str(tmp_path), days=(DAY,),
                         items=(bad, good))
    # ghost 路径不存在 → 该条目无动作也不炸；good 目录被 dry-run 统计
    report = ig.apply_repair(plan, dry_run=True)
    assert report.errors == ()
    assert any(a.day == DAY + timedelta(days=1) for a in report.actions)


# ── 结果对象的序列化、旁挂 meta 与汇总报告分支（覆盖率补齐） ───────────


def test_result_objects_to_dict() -> None:
    mismatch = ig.ValueMismatch("600000.SH", 10.0, 10.5, 0.5)
    assert mismatch.to_dict()["diff"] == 0.5

    vr = ig.ValueCheckResult(DAY, ig.VALUE_OK, 1, ig.HALF_TICK,
                             mismatches=(mismatch,))
    assert vr.ok and vr.comparable and vr.to_dict()["n_mismatch"] == 1
    assert not ig.ValueCheckResult(DAY, ig.VALUE_NO_AUTHORITY, 0, 0.005).comparable

    cr = ig.CoverageCheckResult(DAY, ig.COVERAGE_OK, 2, 2,
                                rows_partition=2, rows_authority=3)
    assert cr.ok and cr.row_count_differs and cr.to_dict()["n_extra"] == 0

    verdict = ig.SnapshotVerdict(state=ig.UNKNOWN, reason="缺哨兵", rows=1)
    assert verdict.to_dict()["state"] == ig.UNKNOWN and verdict.is_unknown

    action = ig.RepairAction(DAY, "p", "rmtree", 1)
    assert action.to_dict()["action"] == "rmtree"
    report = ig.RepairReport(dry_run=True, actions=(action,), deleted_rows=1)
    assert report.to_dict()["deleted_rows"] == 1

    plan = ig.RepairPlan(table_dir="t", days=(DAY,),
                         items=(ig.RepairItem(DAY, ("snapshot",), {}),),
                         notes=("n",))
    d = plan.to_dict()
    assert d["repair_days"] == [DAY.isoformat()] and not plan.empty


def test_date_expr_columns_and_activity(tmp_path: Path) -> None:
    # 字符串形态的 trade_date 也要归一到 Date（否则比较静默全 False）
    expr = ig._to_date_expr(pl.String, pl.col("x"))
    frame = pl.DataFrame({"x": ["2026-09-10"]}).with_columns(expr.alias("d"))
    assert frame["d"].to_list() == [date(2026, 9, 10)]

    part = _write_day(tmp_path / "lake", DAY, _frame(["600000.SH"]))
    assert ig.read_partition_frame(part.parent, DAY, columns=["symbol"]).columns == ["symbol"]
    # 单文件读取（不带 day）
    assert ig.read_partition_frame(part).height == 1
    # 没有 trade_date 列时按「该日无行」处理，不抛
    no_date = tmp_path / "nodate" / "date=2026-09-10"
    no_date.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(no_date / "part.parquet")
    assert ig.read_partition_frame(no_date, DAY).height == 0

    assert ig._has_activity(pl.DataFrame({"volume": [0.0, 1.0]})) is True
    assert ig._has_activity(pl.DataFrame({"volume": [0.0, 0.0]})) is False
    assert ig._has_activity(pl.DataFrame({"x": [1]})) is None  # 无相关列


def test_sidecar_meta_corrupt_and_missing_entry(tmp_path: Path) -> None:
    part = _write_day(tmp_path / "lake", DAY, _frame(["600000.SH"], quote_ts=[_ms(10, 0)]))
    meta = part.parent / ig.META_FILE
    meta.write_text("{ 坏 JSON", encoding="utf-8")
    assert "解析失败" in ig._sidecar_quote_ts(part.parent, DAY)[1]
    meta.write_text(json.dumps("不是对象"), encoding="utf-8")
    assert "结构非法" in ig._sidecar_quote_ts(part.parent, DAY)[1]
    meta.write_text(json.dumps({"days": {}}), encoding="utf-8")
    assert "无该交易日条目" in ig._sidecar_quote_ts(part.parent, DAY)[1]
    meta.write_text(json.dumps({"days": {DAY.isoformat(): {"quote_ts_ms": _ms(10, 0)}}}),
                    encoding="utf-8")
    assert ig._sidecar_quote_ts(part.parent, DAY)[0] == _ms(10, 0)


def test_present_days_and_integrity_report_branches(tmp_path: Path) -> None:
    assert ig._present_days(tmp_path / "empty") == set()
    no_date = tmp_path / "nodate"
    no_date.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(no_date / "part-0.parquet")
    assert ig._present_days(no_date) == set()

    # 未提供日历 → 只做已有分区检查，并明说无法判尾部缺口
    lake = tmp_path / "lake"
    _write_day(lake, DAY, _frame(["600000.SH"]))
    rep = ig.integrity_report(lake, today=DAY)
    assert rep.calendar_checked is False
    assert any("未提供交易日历" in n for n in rep.notes)
    rep.to_dict()

    # 给了日历但湖里没有任何数据 → 不判定尾部缺口
    empty_lake = tmp_path / "empty_lake"
    empty_lake.mkdir()
    rep = ig.integrity_report(empty_lake, calendar_days=[DAY], today=DAY, lookback_days=1)
    assert rep.tail_gaps == ()
    assert any("无任何数据" in n for n in rep.notes)

    # 内部空洞 + 未来日期提示 + 显式 days 限定
    _write_day(lake, DAY + timedelta(days=2), _frame(["600000.SH"], day=DAY + timedelta(days=2)))
    rep = ig.integrity_report(
        lake, calendar_days=[DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)],
        today=DAY + timedelta(days=5), lookback_days=30)
    assert DAY + timedelta(days=1) not in rep.tail_gaps  # 早于 latest → 内部空洞
    assert any("内部空洞" in n for n in rep.notes)
    rep2 = ig.integrity_report(lake, days=[DAY], today=DAY)
    assert rep2.window == (DAY, DAY)
