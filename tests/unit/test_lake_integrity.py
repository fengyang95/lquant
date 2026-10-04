"""数据湖结构性检查的回归用例（2026-10-03 审计 A1/A2 + P1）。

两类此前**完全静默**的故障：

1. **影子湖** —— ``parquet_dir`` 解析成 ``<x>/parquet/parquet``（``LQ_DATA_DIR``
   已含 ``/parquet`` 时 ``config/app.yaml`` 又拼一次），或同一 data 目录下出现
   两棵日线湖。写入进的是另一棵树，两条路径各自报成功。
2. **整年分区缺失** —— ``read_daily`` 用 glob，缺一年就是静默少一年；
   ``latest_trade_date()`` 依然返回最近日期，看起来完全健康。
   实测真实湖整整缺了 2025 年（约 124 万行 / 5,143 只标的）。

另外覆盖 ``run_lake_checks`` 的空帧早退修复：结构性检查必须在读日线帧**之前**
跑，且不受「帧为空」影响 —— 湖整年缺失时帧恰恰可能为空。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.store import integrity


def _write_partition(lake: Path, year: int, dates: list[date]) -> None:
    d = lake / "daily" / f"year={year}"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"trade_date": dates}).write_parquet(d / "part-0.parquet")


def _cal(years: list[int]) -> list[date]:
    out: list[date] = []
    for y in years:
        out += [date(y, 1, 4), date(y, 6, 30), date(y, 12, 31)]
    return out


# ======================================================================
# 数据根形状自检（廉价，每次取根可跑）
# ======================================================================


def test_nested_lake_reason_detects_double_parquet(tmp_path):
    """<x>/parquet/parquet —— LQ_DATA_DIR 已含 /parquet 的双重拼接。"""
    p = tmp_path / "data" / "parquet" / "parquet"
    reason = integrity.nested_lake_reason(p)
    assert reason is not None
    assert "parquet" in reason and "LQ_DATA_DIR" in reason


def test_nested_lake_reason_none_for_normal_root(tmp_path):
    p = tmp_path / "data" / "parquet"
    assert integrity.nested_lake_reason(p) is None


def test_nested_lake_reason_detects_nesting_inside_lake(tmp_path):
    """自己被嵌在另一棵湖之内（父目录含 daily/）—— 同一份数据两套真相。"""
    lake = tmp_path / "lake"
    (lake / "daily" / "year=2024").mkdir(parents=True)
    # parquet_dir 直接落在另一棵湖的根下（父目录含 daily/）
    p = lake / "shadow"
    reason = integrity.nested_lake_reason(p)
    assert reason is not None
    assert "嵌套" in reason


def test_nested_lake_reason_uses_settings_when_omitted(tmp_path, monkeypatch):
    # 不设 LQ_ROOT：让 find_root() 走到本仓库的 config/app.yaml，
    # 才能真正验证 `${LQ_DATA_DIR:./data}/parquet` 的双重拼接
    # （设了 LQ_ROOT 会因 <tmp>/config/app.yaml 不存在而退回默认值）。
    monkeypatch.setenv("LQ_DATA_DIR", str(tmp_path / "parquet"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        # LQ_DATA_DIR 以 /parquet 结尾 → parquet_dir 变成 <tmp>/parquet/parquet
        assert get_settings().parquet_dir.endswith("parquet/parquet")
        assert integrity.nested_lake_reason() is not None
    finally:
        get_settings.cache_clear()


# ======================================================================
# find_lake_roots：有界扫描、不跟随符号链接
# ======================================================================


def test_find_lake_roots_finds_lakes_and_skips_symlinks(tmp_path):
    real = tmp_path / "real"
    (real / "daily" / "year=2024").mkdir(parents=True)
    (real / "minute" / "year=2024").mkdir(parents=True)
    (real / "not_a_lake" / "year=2024").mkdir(parents=True)  # 名字不对，不算

    link = tmp_path / "link_to_real"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:  # pragma: no cover - 平台不支持符号链接
        pytest.skip("平台不支持符号链接")

    roots = integrity.find_lake_roots(tmp_path)
    assert real / "daily" in roots
    assert real / "minute" in roots
    assert real / "not_a_lake" not in roots
    # 符号链接不被跟随：同一棵树不会因为链接而重复出现
    assert not any(str(r).startswith(str(link)) for r in roots)


def test_find_lake_roots_missing_dir_returns_empty(tmp_path):
    assert integrity.find_lake_roots(tmp_path / "nope") == []


# ======================================================================
# check_data_root：影子湖
# ======================================================================


def test_check_data_root_reports_shadow_lake(tmp_path):
    base = tmp_path / "data"
    canonical = base / "parquet"
    shadow = canonical / "parquet"
    _write_partition(canonical, 2024, [date(2024, 1, 2)])
    _write_partition(shadow, 2024, [date(2024, 1, 2)])

    issues = integrity.check_data_root(canonical)
    rules = {i.rule for i in issues}
    assert "SHADOW_LAKE" in rules
    shadow_issue = next(i for i in issues if i.rule == "SHADOW_LAKE")
    assert shadow_issue.severity == "fatal"
    assert str(shadow) in shadow_issue.detail
    # 形状正常（父目录不叫 parquet），所以不该误报 DATA_ROOT_NESTED
    assert "DATA_ROOT_NESTED" not in rules


def test_check_data_root_reports_nested_shape(tmp_path):
    p = tmp_path / "data" / "parquet" / "parquet"
    p.mkdir(parents=True)
    issues = integrity.check_data_root(p)
    assert "DATA_ROOT_NESTED" in {i.rule for i in issues}
    assert all(i.severity == "fatal" for i in issues)


def test_check_data_root_clean_lake_has_no_issues(tmp_path):
    lake = tmp_path / "data" / "parquet"
    _write_partition(lake, 2024, [date(2024, 1, 2)])
    assert integrity.check_data_root(lake) == []


# ======================================================================
# check_partition_continuity：整年缺失 / 年份截断
# ======================================================================


def test_continuity_reports_interior_missing_year(tmp_path):
    lake = tmp_path / "lake"
    for y in (2021, 2022, 2024):
        _write_partition(lake, y, [date(y, 1, 4), date(y, 12, 31)])

    issues = integrity.check_partition_continuity(
        _cal([2019, 2020, 2021, 2022, 2023, 2024]), parquet_dir=lake, today=date(2024, 12, 31)
    )
    missing = [i for i in issues if i.rule == "PARTITION_MISSING"]
    assert [i.trade_date.year for i in missing] == [2023]
    assert missing[0].severity == "fatal"
    assert missing[0].count == 3  # 日历里 2023 的 3 个交易日


def test_continuity_ignores_pre_history_gap(tmp_path):
    """湖从 2021 开始：2019/2020 没有数据属于覆盖策略，不是 bug。"""
    lake = tmp_path / "lake"
    _write_partition(lake, 2021, [date(2021, 1, 4), date(2021, 12, 31)])

    issues = integrity.check_partition_continuity(
        _cal([2019, 2020, 2021]), parquet_dir=lake, today=date(2021, 12, 31)
    )
    assert [i for i in issues if i.rule == "PARTITION_MISSING"] == []


def test_continuity_reports_start_truncation_for_middle_year(tmp_path):
    lake = tmp_path / "lake"
    _write_partition(lake, 2021, [date(2021, 1, 4), date(2021, 12, 31)])
    _write_partition(lake, 2022, [date(2022, 6, 1), date(2022, 12, 31)])
    _write_partition(lake, 2023, [date(2023, 1, 4), date(2023, 12, 31)])

    issues = integrity.check_partition_continuity(
        _cal([2021, 2022, 2023]), parquet_dir=lake, today=date(2023, 12, 31)
    )
    trunc = [i for i in issues if i.rule == "PARTITION_TRUNCATED"]
    assert any("起始被截断" in i.detail for i in trunc)
    assert all(i.severity == "error" for i in trunc)


def test_continuity_last_year_end_truncation_capped_by_today(tmp_path):
    """最后一年：日历预填未来交易日时，上界必须收敛到 today（不误报）。"""
    lake = tmp_path / "lake"
    _write_partition(lake, 2024, [date(2024, 1, 2), date(2024, 6, 30)])

    cal = [date(2024, 1, 2), date(2024, 6, 28), date(2024, 6, 30), date(2024, 12, 31)]
    # today=06-30 → exp_hi=min(12-31, 06-30)=06-30 == 实际 hi → 无截断
    assert (
        integrity.check_partition_continuity(cal, parquet_dir=lake, today=date(2024, 6, 30)) == []
    )
    # 若不设上界（today 推到年末），同一份数据就会被报成「末尾截断」
    issues = integrity.check_partition_continuity(cal, parquet_dir=lake, today=date(2024, 12, 31))
    assert any(i.rule == "PARTITION_TRUNCATED" and "末尾被截断" in i.detail for i in issues)


def test_continuity_reports_genuine_end_truncation(tmp_path):
    lake = tmp_path / "lake"
    _write_partition(lake, 2024, [date(2024, 1, 2), date(2024, 5, 31)])

    cal = [date(2024, 1, 2), date(2024, 6, 28), date(2024, 6, 30)]
    issues = integrity.check_partition_continuity(cal, parquet_dir=lake, today=date(2024, 6, 30))
    trunc = [i for i in issues if i.rule == "PARTITION_TRUNCATED"]
    assert any("末尾被截断" in i.detail for i in trunc)


def test_continuity_skips_without_calendar_or_lake(tmp_path):
    """日历缺失 / 湖不存在：降级跳过，绝不成为检查链路的单点故障。"""
    lake = tmp_path / "lake"
    _write_partition(lake, 2024, [date(2024, 1, 2)])
    assert integrity.check_partition_continuity(None, parquet_dir=lake) == []
    assert integrity.check_partition_continuity([], parquet_dir=lake) == []
    assert (
        integrity.check_partition_continuity(_cal([2024]), parquet_dir=tmp_path / "missing") == []
    )


def test_continuity_survives_corrupt_partition_file(tmp_path):
    """坏文件只跳过该分区，不让整个检查崩掉。"""
    lake = tmp_path / "lake"
    _write_partition(lake, 2023, [date(2023, 1, 4), date(2023, 12, 31)])
    _write_partition(lake, 2025, [date(2025, 1, 2), date(2025, 12, 31)])
    bad = lake / "daily" / "year=2024"  # 中间年份的坏文件
    bad.mkdir(parents=True)
    (bad / "part-0.parquet").write_bytes(b"not a parquet file")

    issues = integrity.check_partition_continuity(
        _cal([2023, 2024, 2025]), parquet_dir=lake, today=date(2025, 12, 31)
    )
    # 2024 读不动 → 不计入 have → 区间内部整年缺失被报出来
    assert any(i.rule == "PARTITION_MISSING" and i.trade_date.year == 2024 for i in issues)


# ======================================================================
# pipeline：结构性检查不受「日线帧为空」影响（空帧早退修复）
# ======================================================================


def test_check_lake_structure_never_raises_without_duckdb(tmp_path, monkeypatch):
    """日历/参考表不可用时只降级跳过，check_lake_structure 不抛异常。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings
    from lquant.data.quality.pipeline import check_lake_structure

    get_settings.cache_clear()
    try:
        assert isinstance(check_lake_structure(), list)
    finally:
        get_settings.cache_clear()


def test_run_lake_checks_reports_structure_on_empty_frame(tmp_path, monkeypatch):
    """湖是空的（只有影子湖）时，run_lake_checks 仍须报出 SHADOW_LAKE。

    旧实现 ``if not len(df): return []`` —— 恰恰在最该报的整年缺失/影子湖
    场景下把结构性问题全吞掉。
    """
    base = tmp_path / "data"
    shadow = base / "parquet" / "parquet"
    _write_partition(shadow, 2024, [date(2024, 1, 2)])  # 生效根下没有任何日线

    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.setenv("LQ_DATA_DIR", str(base))
    monkeypatch.chdir(tmp_path)
    # 不依赖 duckdb：data_version 与日历都降级
    monkeypatch.setattr("lquant.data.lineage.latest", lambda *a, **k: None)

    from lquant.core.config import get_settings
    from lquant.data.quality.pipeline import run_lake_checks

    get_settings.cache_clear()
    try:
        issues = run_lake_checks()
    finally:
        get_settings.cache_clear()

    assert "SHADOW_LAKE" in {i.rule for i in issues}
