"""看板表结构。

这五张表有个共同特点：**源站不提供历史回溯**。
涨停池、炸板池、盘口资金流当天不采就永久丢失，事后补不回来。
所以它们的调度优先级高于任何批处理任务，且失败必须告警而不是静默跳过。
"""
from __future__ import annotations

from typing import Any

__all__ = ["MARKET_TABLES", "ddl_statements", "ensure_market_tables", "TABLE_DOCS"]


MARKET_TABLES: dict[str, str] = {
    "limit_up_pool": """
        CREATE TABLE IF NOT EXISTS limit_up_pool (
            trade_date      DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            close           DOUBLE,
            change_pct      DOUBLE,
            amount          DOUBLE,
            seal_amount     DOUBLE,
            turnover_rate   DOUBLE,
            first_limit_time VARCHAR,
            last_limit_time  VARCHAR,
            open_count      INTEGER,
            limit_up_type   VARCHAR,
            industry        VARCHAR,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, symbol)
        )""",
    "limit_down_pool": """
        CREATE TABLE IF NOT EXISTS limit_down_pool (
            trade_date      DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            close           DOUBLE,
            change_pct      DOUBLE,
            amount          DOUBLE,
            seal_amount     DOUBLE,
            industry        VARCHAR,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, symbol)
        )""",
    "money_flow": """
        CREATE TABLE IF NOT EXISTS money_flow (
            trade_date      DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            close           DOUBLE,
            change_pct      DOUBLE,
            main_net_inflow DOUBLE,
            main_net_ratio  DOUBLE,
            super_large_net DOUBLE,
            large_net       DOUBLE,
            medium_net      DOUBLE,
            small_net       DOUBLE,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, symbol)
        )""",
    "sector_daily": """
        CREATE TABLE IF NOT EXISTS sector_daily (
            trade_date      DATE,
            sector_code     VARCHAR,
            sector_name     VARCHAR,
            kind            VARCHAR,
            change_pct      DOUBLE,
            turnover_rate   DOUBLE,
            amount          DOUBLE,
            main_net_inflow DOUBLE,
            leader_symbol   VARCHAR,
            leader_name     VARCHAR,
            leader_change   DOUBLE,
            up_count        INTEGER,
            down_count      INTEGER,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, sector_code)
        )""",
    "sentiment_daily": """
        CREATE TABLE IF NOT EXISTS sentiment_daily (
            trade_date          DATE,
            limit_up_count      INTEGER,
            limit_down_count    INTEGER,
            broken_count        INTEGER,
            broken_rate         DOUBLE,
            max_consecutive     INTEGER,
            yesterday_limit_up_return DOUBLE,
            up_count            INTEGER,
            down_count          INTEGER,
            total_amount        DOUBLE,
            sentiment_score     DOUBLE,
            collected_at        TIMESTAMP,
            PRIMARY KEY (trade_date)
        )""",
    "dragon_tiger": """
        CREATE TABLE IF NOT EXISTS dragon_tiger (
            trade_date      DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            close           DOUBLE,
            change_pct      DOUBLE,
            net_buy         DOUBLE,
            buy_amount      DOUBLE,
            sell_amount     DOUBLE,
            reason          VARCHAR,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, symbol)
        )""",
    "northbound_flow": """
        CREATE TABLE IF NOT EXISTS northbound_flow (
            trade_date       DATE,
            ts               TIMESTAMP,
            sh_deal_amt      DOUBLE,
            sz_deal_amt      DOUBLE,
            total_deal_amt   DOUBLE,
            deal_num         BIGINT,
            sh_net_inflow    DOUBLE,
            sz_net_inflow    DOUBLE,
            total_net_inflow DOUBLE,
            net_published    BOOLEAN,
            collected_at     TIMESTAMP,
            PRIMARY KEY (trade_date, ts)
        )""",
    "northbound_top10": """
        CREATE TABLE IF NOT EXISTS northbound_top10 (
            trade_date      DATE,
            board           VARCHAR,
            symbol          VARCHAR,
            name            VARCHAR,
            rank_no         INTEGER,
            close           DOUBLE,
            change_pct      DOUBLE,
            deal_amt        DOUBLE,
            mutual_ratio    DOUBLE,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, board, rank_no)
        )""",
    "index_daily": """
        CREATE TABLE IF NOT EXISTS index_daily (
            trade_date      DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            open            DOUBLE,
            high            DOUBLE,
            low             DOUBLE,
            close           DOUBLE,
            pre_close       DOUBLE,
            volume          DOUBLE,
            amount          DOUBLE,
            collected_at    TIMESTAMP,
            PRIMARY KEY (trade_date, symbol)
        )""",
}

TABLE_DOCS: dict[str, str] = {
    "limit_up_pool": "涨停池：连板数、首次封板时间、炸板次数。源站仅提供当日，必须当天采。",
    "limit_down_pool": "跌停池：与涨停池配对，衡量市场恐慌程度。",
    "money_flow": "个股资金流：主力/超大单/大单/中单/小单净额，用于判断资金真伪。",
    "sector_daily": "板块日度：涨跌幅、资金流、龙头股。行业轮动策略的输入。",
    "sentiment_daily": "市场情绪：涨停数、炸板率、最高连板、昨日涨停今日表现。",
    "dragon_tiger": "龙虎榜：机构与游资席位买卖，T+1 盘后公布。",
    "northbound_flow": "北向资金：沪深股通成交额/笔数（净买额 2024-08-19 起停发，net_published 标注口径）。",
    "northbound_top10": "北向前十大成交活跃证券：按成交额排名，含占该股成交额比例。",
    "index_daily": "指数日线：上证/沪深300/中证500 等，回测基准与大盘对比的数据源。",
}

# 允许写入的字段（用于写入前对齐，换源时列可能不全）
TABLE_COLUMNS: dict[str, list[str]] = {
    "limit_up_pool": ["trade_date", "symbol", "name", "close", "change_pct", "amount",
                      "seal_amount", "turnover_rate", "first_limit_time", "last_limit_time",
                      "open_count", "limit_up_type", "industry", "collected_at"],
    "limit_down_pool": ["trade_date", "symbol", "name", "close", "change_pct", "amount",
                        "seal_amount", "industry", "collected_at"],
    "money_flow": ["trade_date", "symbol", "name", "close", "change_pct", "main_net_inflow",
                   "main_net_ratio", "super_large_net", "large_net", "medium_net",
                   "small_net", "collected_at"],
    "sector_daily": ["trade_date", "sector_code", "sector_name", "kind", "change_pct",
                     "turnover_rate",
                     "amount", "main_net_inflow", "leader_symbol", "leader_name",
                     "leader_change", "up_count", "down_count", "collected_at"],
    "sentiment_daily": ["trade_date", "limit_up_count", "limit_down_count", "broken_count",
                        "broken_rate", "max_consecutive", "yesterday_limit_up_return",
                        "up_count", "down_count", "total_amount", "sentiment_score",
                        "collected_at"],
    "dragon_tiger": ["trade_date", "symbol", "name", "close", "change_pct", "net_buy",
                     "buy_amount", "sell_amount", "reason", "collected_at"],
    "northbound_flow": ["trade_date", "ts", "sh_deal_amt", "sz_deal_amt",
                        "total_deal_amt", "deal_num", "sh_net_inflow", "sz_net_inflow",
                        "total_net_inflow", "net_published", "collected_at"],
    "northbound_top10": ["trade_date", "board", "symbol", "name", "rank_no", "close",
                         "change_pct", "deal_amt", "mutual_ratio", "collected_at"],
    "index_daily": ["trade_date", "symbol", "name", "open", "high", "low", "close",
                    "pre_close", "volume", "amount", "collected_at"],
}


def ddl_statements() -> list[str]:
    return list(MARKET_TABLES.values())


def _migrate_northbound(con: Any) -> None:
    """北向表加列 + 清理「净买额已停发却写成 0」的历史行。

    旧采集器解析东财 `kamt/get` 的净买额，源站 2024-08-19 停发后它把 0.0
    当观测值写库。0 与「口径停发」在下游含义完全不同，因此这里：
    1. 补列（成交额/笔数/net_published），保留历史；
    2. 把 > 停发日 的净买额一律置 NULL 并标注 net_published=false。

    用 ALTER 而不是删表重建 —— 2024-08-16 之前的净买额是真实历史，删了就没了。
    """
    from datetime import date as _date

    try:
        cols = {r[0] for r in con.execute("DESCRIBE northbound_flow").fetchall()}
    except Exception:  # noqa: BLE001  表不存在，走正常建表
        return
    if not cols:
        return
    added = []
    for col, ddl in (("sh_deal_amt", "DOUBLE"), ("sz_deal_amt", "DOUBLE"),
                     ("total_deal_amt", "DOUBLE"), ("deal_num", "BIGINT"),
                     ("net_published", "BOOLEAN")):
        if col not in cols:
            con.execute(f"ALTER TABLE northbound_flow ADD COLUMN {col} {ddl}")
            added.append(col)
    if added:
        print(f"[migrate] northbound_flow 加列 {added}（历史净买额保留）")
    cutoff = _date(2024, 8, 16)
    # 只统计**真的需要修**的行：净买额非空，或 net_published 还不是 false。
    # 否则每天新采的正常行（净额本就是 NULL）都会被算进来，日志天天喊「清理 N 行」，
    # 真出问题时反而看不出异常。
    bad = con.execute(
        "SELECT count(*) FROM northbound_flow WHERE trade_date > ? AND ("
        " sh_net_inflow IS NOT NULL OR sz_net_inflow IS NOT NULL"
        " OR total_net_inflow IS NOT NULL OR net_published IS DISTINCT FROM false)",
        [cutoff]).fetchone()[0]
    if bad:
        con.execute(
            "UPDATE northbound_flow SET sh_net_inflow = NULL, sz_net_inflow = NULL, "
            "total_net_inflow = NULL, net_published = false WHERE trade_date > ?", [cutoff])
        print(f"[migrate] northbound_flow 清理 {bad} 行停发后的净买额（置 NULL，非 0）")
    con.execute(
        "UPDATE northbound_flow SET net_published = true "
        "WHERE trade_date <= ? AND net_published IS NULL", [cutoff])
    con.execute(
        "UPDATE northbound_flow SET net_published = false WHERE net_published IS NULL")


def ensure_market_tables(con: Any) -> int:
    """建看板表，返回建表数量。

    两类迁移（旧库结构与新采集器列不匹配会导致 upsert 静默失败）：
    1. 老版 ddl.py 的 sentiment_daily 列名不同（limit_up/...）→ 删表重建（可随时重算）；
    2. 任意表缺少 TABLE_COLUMNS 里的关键列（如 money_flow 旧结构
       symbol/main_net/retail_net）→ 删表重建。看板表都可由采集器重新生成，
       重建无损；换来的是 upsert 列永远对得上。
    """
    n = 0
    for table, sql in MARKET_TABLES.items():
        # 加列迁移：新增可空列时优先 ALTER（保留历史数据），不动 drop-rebuild 路径
        if table in ("limit_up_pool", "limit_down_pool"):
            # seal_amount（封板资金）：老库没有这列。涨停池源站**无历史回溯**，
            # 走 drop-rebuild 等于把历史永久删掉 —— 必须 ALTER 保数据。
            try:
                existing_cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
                if existing_cols and "seal_amount" not in existing_cols:
                    con.execute(
                        f"ALTER TABLE {table} ADD COLUMN seal_amount DOUBLE")
                    print(f"[migrate] {table} 加列 seal_amount（历史行为 NULL）")
            except Exception as e:  # noqa: BLE001  表可能不存在，走下面正常建表
                print(f"[migrate] {table} seal_amount 列检查跳过: {e}")
        if table == "sector_daily":
            try:
                existing_cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
                if existing_cols and "kind" not in existing_cols:
                    con.execute(
                        "ALTER TABLE sector_daily ADD COLUMN kind VARCHAR DEFAULT 'industry'")
                    print("[migrate] sector_daily 加列 kind（历史行默认 industry）")
            except Exception as e:  # noqa: BLE001  表可能不存在，走下面正常建表
                print(f"[migrate] sector_daily kind 列检查跳过: {e}")
        if table == "northbound_flow":
            _migrate_northbound(con)
        required = [c for c in TABLE_COLUMNS.get(table, []) if c != "collected_at"]
        try:
            existing = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
            if existing and not set(required) <= existing:
                print(f"[migrate] {table} 结构过旧（缺 {sorted(set(required) - existing)[:3]}）→ 重建")
                con.execute(f"DROP TABLE {table}")
                existing = set()
        except Exception:  # noqa: BLE001  表不存在
            existing = set()
        con.execute(sql)
        n += 1
    return n
