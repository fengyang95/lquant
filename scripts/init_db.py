#!/usr/bin/env python3
"""建 DuckDB 表结构。幂等：可反复执行。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lquant.core.config import get_settings  # noqa: E402
from lquant.data.store.ddl import DDL_STATEMENTS  # noqa: E402
from lquant.market.schema import ensure_market_tables  # noqa: E402


def main() -> int:
    import duckdb

    s = get_settings()
    path = s.duckdb_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    # DuckDB 只允许单个写进程
    con = duckdb.connect(path)
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    n_market = ensure_market_tables(con)
    con.close()
    print(f"init db ok: {path}  ({len(DDL_STATEMENTS)} core + {n_market} market tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
