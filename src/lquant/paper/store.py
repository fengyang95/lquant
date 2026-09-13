"""模拟盘状态持久化（sqlite，独立于 DuckDB 主库）。

为什么不用 DuckDB：
- 模拟盘是「反复短进程」的（CLI 每次 tick 起一个进程）或常驻的（server），
  而本仓库 DuckDB reader() 是读写模式打开 —— 模拟盘进程与 uvicorn 常驻
  连接互撞 Conflicting lock 是已知事故模式。模拟盘状态量小（百行级），
  sqlite WAL 足够，且跨进程并发安全。

布局：账户 / 委托 / 持仓 / 净值四张表。净值按 (trade_date, source) 记录，
source: intraday = 盘中快照近似值；official = 收盘后官方日线对账重算值。
对外绩效一律读 official，intraday 保留作审计。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import polars as pl

from lquant.paper.engine import PaperBroker, PaperConfig, PaperOrder, PaperPosition


def db_path() -> Path:
    env = os.getenv("LQ_PAPER_DB")
    if env:
        p = Path(env)
    else:
        from lquant.core.config import get_settings
        p = Path(get_settings().parquet_dir).parent / "paper" / "paper.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account(
  name TEXT PRIMARY KEY,
  initial_cash REAL NOT NULL,
  cash REAL NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  strategy TEXT NOT NULL DEFAULT '',
  universe_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_order(
  account TEXT NOT NULL,
  order_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  filled_qty INTEGER NOT NULL DEFAULT 0,
  filled_price REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(account, order_id)
);
CREATE TABLE IF NOT EXISTS paper_position(
  account TEXT NOT NULL,
  symbol TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,
  available INTEGER NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  last_price REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(account, symbol)
);
CREATE TABLE IF NOT EXISTS paper_nav(
  account TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  nav REAL NOT NULL,
  cash REAL NOT NULL,
  n_positions INTEGER NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account, trade_date, source)
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(db_path(), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


class AccountNotFound(KeyError):
    pass


# 账户级读-改-写串行锁：load_broker → mutate → save_broker 的整段周期
# 必须互斥，否则两个并发调用（如 uvicorn 线程池里 order 与 tick 同时进来）
# 各自 load 同一状态、后 save 的把先 save 的成交/现金整个冲掉。
# sqlite 只串行化单条写，不保护跨语句的 RMW 周期。
# 两层：线程 RLock 管同进程（uvicorn 线程池），flock 文件锁管跨进程
# （CLI tick 进程与常驻 server 并发，sqlite busy_timeout 挡不住 RMW）。
_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def account_lock(name: str):
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.get(name)
        if lock is None:
            lock = threading.RLock()
            _ACCOUNT_LOCKS[name] = lock
    with lock:
        import fcntl

        lock_path = db_path().with_name(f"{db_path().name}.{name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def create_account(name: str, initial_cash: float, strategy: str = "",
                   universe: list[str] | None = None) -> dict:
    with _conn() as con:
        con.execute(
            "INSERT INTO paper_account(name, initial_cash, cash, seq, strategy, "
            "universe_json, created_at, updated_at) VALUES (?,?,?,0,?,?,?,?)",
            [name, initial_cash, initial_cash, strategy,
             json.dumps(universe or []), now_iso(), now_iso()])
    return get_account(name)


def get_account(name: str) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT name, initial_cash, cash, seq, strategy, universe_json, "
            "created_at, updated_at FROM paper_account WHERE name = ?", [name]
        ).fetchone()
    if not row:
        raise AccountNotFound(name)
    return {"name": row[0], "initial_cash": row[1], "cash": row[2], "seq": row[3],
            "strategy": row[4], "universe": json.loads(row[5] or "[]"),
            "created_at": row[6], "updated_at": row[7]}


def list_accounts() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT name, initial_cash, cash, strategy, updated_at "
            "FROM paper_account ORDER BY created_at").fetchall()
    return [{"name": r[0], "initial_cash": r[1], "cash": r[2],
             "strategy": r[3], "updated_at": r[4]} for r in rows]


def now_iso() -> str:
    from lquant.core.types import now_cn
    return now_cn().isoformat(timespec="seconds")


def load_broker(name: str, cfg: PaperConfig | None = None) -> PaperBroker:
    """从库中重建 PaperBroker（现金/持仓/委托/单号序列全部还原）。"""
    acct = get_account(name)
    config = cfg or PaperConfig(initial_cash=acct["initial_cash"])
    broker = PaperBroker(config)
    broker.cash = acct["cash"]
    broker._seq = acct["seq"]
    with _conn() as con:
        pos_rows = con.execute(
            "SELECT symbol, qty, available, avg_cost, last_price "
            "FROM paper_position WHERE account = ? AND qty > 0", [name]).fetchall()
        order_rows = con.execute(
            "SELECT order_id, ts, symbol, side, qty, price, status, reason, "
            "filled_qty, filled_price FROM paper_order WHERE account = ? "
            "ORDER BY ts, order_id", [name]).fetchall()
    for r in pos_rows:
        broker.positions[r[0]] = PaperPosition(
            symbol=r[0], qty=r[1], available=r[2], avg_cost=r[3], last_price=r[4])
    for r in order_rows:
        broker.orders.append(PaperOrder(
            order_id=r[0], ts=from_iso(r[1]), symbol=r[2], side=r[3], qty=r[4],
            price=r[5], status=r[6], reason=r[7], filled_qty=r[8],
            filled_price=r[9]))
    return broker


def save_broker(name: str, broker: PaperBroker) -> None:
    """全量覆写持仓 + 逐单 upsert 委托 + 回写现金/单号序列。

    模拟盘数据量小（百行级），全量覆写换实现简单、状态无歧义。
    """
    with _conn() as con:
        for p in broker.positions.values():
            con.execute(
                "INSERT INTO paper_position(account, symbol, qty, available, "
                "avg_cost, last_price) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(account, symbol) DO UPDATE SET qty=excluded.qty, "
                "available=excluded.available, avg_cost=excluded.avg_cost, "
                "last_price=excluded.last_price",
                [name, p.symbol, p.qty, p.available, p.avg_cost, p.last_price])
        for o in broker.orders:
            con.execute(
                "INSERT INTO paper_order(account, order_id, ts, symbol, side, "
                "qty, price, status, reason, filled_qty, filled_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account, order_id) DO UPDATE SET status=excluded.status, "
                "reason=excluded.reason, filled_qty=excluded.filled_qty, "
                "filled_price=excluded.filled_price",
                [name, o.order_id, o.ts.isoformat(), o.symbol, o.side, o.qty,
                 o.price, o.status, o.reason, o.filled_qty, o.filled_price])
        con.execute(
            "UPDATE paper_account SET cash=?, seq=?, updated_at=? WHERE name=?",
            [broker.cash, broker._seq, now_iso(), name])


def set_universe(name: str, universe: list[str]) -> None:
    with _conn() as con:
        con.execute("UPDATE paper_account SET universe_json=?, updated_at=? "
                    "WHERE name=?", [json.dumps(universe), now_iso(), name])


def record_nav(name: str, d: date, nav: float, cash: float,
               n_positions: int, source: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO paper_nav(account, trade_date, nav, cash, n_positions, "
            "source, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(account, trade_date, source) DO UPDATE SET "
            "nav=excluded.nav, cash=excluded.cash, "
            "n_positions=excluded.n_positions, created_at=excluded.created_at",
            [name, d.isoformat(), round(nav, 2), round(cash, 2),
             n_positions, source, now_iso()])


def nav_frame(name: str, source: str | None = None) -> pl.DataFrame:
    sql = ("SELECT trade_date, nav, cash, n_positions, source FROM paper_nav "
           "WHERE account = ?")
    args: list = [name]
    if source:
        sql += " AND source = ?"
        args.append(source)
    with _conn() as con:
        rows = con.execute(sql + " ORDER BY trade_date", args).fetchall()
    if not rows:
        return pl.DataFrame(schema={"trade_date": pl.Utf8, "nav": pl.Float64,
                                    "cash": pl.Float64, "n_positions": pl.Int64,
                                    "source": pl.Utf8})
    return pl.DataFrame({"trade_date": [r[0] for r in rows],
                         "nav": [r[1] for r in rows],
                         "cash": [r[2] for r in rows],
                         "n_positions": [r[3] for r in rows],
                         "source": [r[4] for r in rows]})


def from_iso(s: str):
    from datetime import datetime
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return s
