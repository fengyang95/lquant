"""跨进程并发写 parquet 湖：_file_lock 的 flock 层不得丢行。

回归点：_file_lock 曾只有进程内 threading.Lock —— API 服务进程与 `lq` CLI
进程同时写同一 `year=YYYY/part-0.parquet` 时，各自「读旧 → 合并 → 整文件
os.replace」，后写者整文件覆盖，先写者的行被静默吞掉。
"""
from __future__ import annotations

import multiprocessing as mp
import os

import polars as pl
import pytest

from lquant.core.config import get_settings
from lquant.data.schema import SCHEMAS
from lquant.data.store.parquet import read_daily, write_daily

N_PROCS = 3
ROWS_PER_PROC = 100
YEAR = 2024


def _child_write(root: str, proc_id: int) -> None:
    """子进程入口：必须模块顶层定义（spawn 可 picklable）。

    各自重新 import（spawn 下模块级状态全新），仅 LQ_ROOT 指向同一 tmp 目录。
    """
    os.environ["LQ_ROOT"] = root
    get_settings.cache_clear()
    from datetime import date

    symbols = [f"P{proc_id:02d}{i:04d}.SZ" for i in range(ROWS_PER_PROC)]
    part = pl.DataFrame(
        {
            "symbol": symbols,
            "trade_date": [date(YEAR, 1, 2)] * ROWS_PER_PROC,
            "close": [float(proc_id)] * ROWS_PER_PROC,
        },
        schema={"symbol": pl.Utf8, "trade_date": pl.Date, "close": pl.Float64},
    )
    # diagonal 拼上空 schema 帧，补齐 daily_bar 全列（缺列补 null）
    df = pl.concat([part, pl.DataFrame(schema=SCHEMAS["daily_bar"])], how="diagonal")
    write_daily(df)


@pytest.fixture
def lake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # parquet_dir 相对 CWD，chdir 隔离
    get_settings.cache_clear()
    yield str(tmp_path)
    get_settings.cache_clear()


def test_concurrent_processes_write_no_loss(lake_env):
    """3 个子进程各写 100 个不同 symbol 到同一年分片：总行数不丢。"""
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_child_write, args=(lake_env, i)) for i in range(N_PROCS)
    ]
    for p in procs:
        p.start()
    errors = []
    for p in procs:
        p.join(timeout=120)
        if p.exitcode not in (0, None):
            errors.append(f"proc exitcode={p.exitcode}")
    assert not errors, errors

    df = read_daily().collect()
    assert df.height == N_PROCS * ROWS_PER_PROC
    assert df["symbol"].n_unique() == N_PROCS * ROWS_PER_PROC


def test_lock_files_cleaned_or_harmless(lake_env):
    """残留 .lock 文件不影响读取（rglob 只匹配 *.parquet）。"""
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_child_write, args=(lake_env, i)) for i in range(N_PROCS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    daily_dir = os.path.join(lake_env, "data", "parquet", "daily")
    locks = list(__import__("pathlib").Path(daily_dir).rglob("*.lock"))
    assert len(locks) >= 1  # 残留即残留，允许
    df = read_daily().collect()
    assert df.height == N_PROCS * ROWS_PER_PROC
    assert not df.select(pl.col("symbol").str.contains(".lock$").any()).item()
