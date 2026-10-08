"""B1「值级覆盖检测 + 自动修复」回归：**行数相同但收盘价错了**。

核心用例（:func:`test_row_count_blind_while_value_check_catches`）刻意构造
「分区与权威来源行数完全一致、只有 close 差 0.01」的分区：模拟 2026 年那次
真实事故（实时端点收盘后长期返回旧价，3392/5554 只股票收盘价与官方日线不符，
行数校验零命中）。其余用例覆盖哨兵三态、覆盖集口径、dry-run 不落盘。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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
