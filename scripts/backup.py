"""数据备份（方案 SYS5，P1）。

DuckDB 单文件复制即备份；Parquet 湖按目录压缩。
用法：
    python scripts/backup.py                # 备份到 data/backts/<时间戳>/
    python scripts/backup.py --keep 7       # 只保留最近 7 份（默认 7）
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3  # noqa: F401  (仅为占位说明：DuckDB 不用 WAL，直接 copy 即一致)
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7, help="保留最近 N 份备份")
    ap.add_argument("--out", default=str(ROOT / "data" / "backups"))
    args = ap.parse_args()

    from lquant.core.config import get_settings
    from lquant.core.db import writer

    s = get_settings()
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(args.out) / ts
    dest.mkdir(parents=True, exist_ok=True)

    # DuckDB：先 checkpoint 把 WAL 落盘，再复制单文件
    db = Path(s.duckdb_path)
    if db.exists():
        try:
            with writer() as con:
                con.execute("CHECKPOINT")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] checkpoint 失败（仍继续复制）: {e}")
        shutil.copy2(db, dest / db.name)
        print(f"✓ DuckDB → {dest / db.name}")
    else:
        print(f"⚠ DuckDB 不存在: {db}")

    # Parquet 湖：整目录压缩
    pq = Path(s.parquet_dir)
    if pq.exists() and any(pq.rglob("*.parquet")):
        archive = dest / "parquet.zip"
        shutil.make_archive(str(archive.with_suffix("")), "zip", pq)
        print(f"✓ Parquet → {archive}")
    else:
        print(f"⚠ Parquet 湖为空: {pq}")

    # 清理旧备份
    backups = sorted((Path(args.out)).iterdir())
    for old in backups[:-args.keep] if args.keep > 0 else []:
        if old.is_dir():
            shutil.rmtree(old)
            print(f"清理旧备份 {old.name}")

    print(f"备份完成: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
