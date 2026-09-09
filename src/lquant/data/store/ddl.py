"""DuckDB 表结构。幂等 DDL 列表。

质量三表（data_quality_issue / data_version / golden_expected）的 DDL
在这里唯一定义，quality 层模块从这里 import —— 别处再写一份迟早漂移
（collect_log 前车之鉴）。
"""
from __future__ import annotations

DDL_DATA_QUALITY_ISSUE = """
CREATE TABLE IF NOT EXISTS data_quality_issue (
    issue_id     VARCHAR PRIMARY KEY,
    dataset      VARCHAR,
    symbol       VARCHAR,
    trade_date   DATE,
    rule_code    VARCHAR,     -- LIMIT_BREACH / ADJ_JUMP / ZOMBIE / CALENDAR_STRAY ...
    severity     VARCHAR,     -- fatal / error / warn / info
    detail       JSON,
    count        INTEGER,
    data_version VARCHAR,
    resolved     BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMP DEFAULT now()
)
"""

DDL_DATA_VERSION = """
CREATE TABLE IF NOT EXISTS data_version (
    version   VARCHAR PRIMARY KEY,
    dataset   VARCHAR,
    trade_date DATE,
    row_count BIGINT,
    created_at TIMESTAMP DEFAULT now()
)
"""

DDL_GOLDEN_EXPECTED = """
CREATE TABLE IF NOT EXISTS golden_expected (
    name       VARCHAR PRIMARY KEY,
    kind       VARCHAR,          -- structural / calc / factor_ic
    sql        VARCHAR,          -- 产出单值（首行首列）的查询
    expected   DOUBLE,
    tolerance  DOUBLE,
    frozen_at  TIMESTAMP DEFAULT now()
)
"""

DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS security (
        symbol      VARCHAR PRIMARY KEY,
        name        VARCHAR,
        sec_type    VARCHAR,
        board       VARCHAR,
        list_date   DATE,
        delist_date DATE,          -- 幸存者偏差防护
        is_st       BOOLEAN,
        source      VARCHAR,
        updated_at  TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_calendar (
        trade_date DATE PRIMARY KEY,
        is_open    BOOLEAN,
        exchange   VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_bar (
        symbol        VARCHAR,
        trade_date    DATE,
        open          DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        pre_close     DOUBLE,
        volume        DOUBLE,      -- 股
        amount        DOUBLE,      -- 元
        turnover_rate DOUBLE,
        adj_factor    DOUBLE,
        sec_type      VARCHAR,
        quality_flags INTEGER,     -- 位掩码，0 = 干净
        source        VARCHAR,
        ingested_at   TIMESTAMP,
        data_version  VARCHAR,
        PRIMARY KEY (symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS minute_bar (
        symbol VARCHAR, ts TIMESTAMP, freq VARCHAR,
        open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        volume DOUBLE, amount DOUBLE, adj_factor DOUBLE,
        source VARCHAR, ingested_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adj_factor (
        symbol VARCHAR, trade_date DATE,
        factor DOUBLE,            -- 后复权因子；前复权由最新因子归一
        source VARCHAR,
        PRIMARY KEY (symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_pit (
        symbol VARCHAR, stat_date DATE, pub_date DATE,
        report_type VARCHAR, item VARCHAR, value DOUBLE, unit VARCHAR,
        source VARCHAR, ingested_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS industry_classify (
        symbol VARCHAR, std VARCHAR, code VARCHAR, name VARCHAR,
        std_date DATE,             -- 生效日：防止用今天的分类回测十年前
        source VARCHAR,
        PRIMARY KEY (symbol, std_date)                -- 同日内重投→OR REPLACE，幂等
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_cons (
        index_code VARCHAR, symbol VARCHAR,
        weight DOUBLE,          -- 成分权重（%）；公开文件只有部分指数会给
        eff_date DATE,          -- 生效日：用真实成分公布日，不用今天的成分回测十年前
        source VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS etf_meta (
        symbol VARCHAR PRIMARY KEY,
        name VARCHAR, track_index VARCHAR, fund_type VARCHAR,
        is_cross_border BOOLEAN,
        sellable_after_days TINYINT,   -- per-instrument T+N，优先于规则表默认值
        management_fee DOUBLE, custody_fee DOUBLE,
        fund_size DOUBLE, share_outstanding DOUBLE,
        as_of DATE, source VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_def (
        name VARCHAR PRIMARY KEY,
        expression VARCHAR, description VARCHAR,
        enabled BOOLEAN DEFAULT TRUE, created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_run (
        run_id     VARCHAR PRIMARY KEY,
        model      VARCHAR,
        params     JSON,
        features   JSON,
        metrics    JSON,
        train_rows INTEGER,
        test_rows  INTEGER,
        train_end  DATE,
        test_end   DATE,
        created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_value (
        factor VARCHAR, symbol VARCHAR, trade_date DATE,
        raw DOUBLE, processed DOUBLE,
        PRIMARY KEY (factor, symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_run (
        run_id VARCHAR PRIMARY KEY,
        strategy VARCHAR, params JSON,
        start_date DATE, end_date DATE,     -- start/end 是 DuckDB 保留字
        status VARCHAR, metrics JSON, created_at TIMESTAMP, finished_at TIMESTAMP
    )
    """,
    "CREATE TABLE IF NOT EXISTS backtest_order (run_id VARCHAR, ts TIMESTAMP, symbol VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE, fee DOUBLE)",
    "CREATE TABLE IF NOT EXISTS backtest_position (run_id VARCHAR, trade_date DATE, symbol VARCHAR, qty DOUBLE, avg_cost DOUBLE)",
    "CREATE TABLE IF NOT EXISTS backtest_nav (run_id VARCHAR, trade_date DATE, nav DOUBLE, drawdown DOUBLE)",
    """
    CREATE TABLE IF NOT EXISTS market_snapshot (
        symbol VARCHAR, ts TIMESTAMP,
        last DOUBLE, pct_chg DOUBLE, volume DOUBLE, amount DOUBLE,
        iopv DOUBLE, discount DOUBLE,
        is_stale BOOLEAN,          -- 僵尸报价：不能当真实价用
        source VARCHAR
    )
    """,
    "CREATE TABLE IF NOT EXISTS sector (code VARCHAR, name VARCHAR, level INTEGER, ts TIMESTAMP, pct_chg DOUBLE, amount DOUBLE, source VARCHAR)",
    # 注意：money_flow / dragon_tiger / sentiment_daily 的 DDL 由
    # market/schema.py::ensure_market_tables 统一拥有（旧版这里的三张表
    # 结构与采集器列不匹配，曾导致资金流落库静默失败）。
    "CREATE TABLE IF NOT EXISTS collect_log (job VARCHAR, trade_date DATE, started_at TIMESTAMP, finished_at TIMESTAMP, rows INTEGER, status VARCHAR, message VARCHAR, PRIMARY KEY (job, trade_date))",
    DDL_DATA_QUALITY_ISSUE,
    DDL_DATA_VERSION,
    DDL_GOLDEN_EXPECTED,
    """
    CREATE TABLE IF NOT EXISTS sync_job (
        sync_id      VARCHAR PRIMARY KEY,
        name         VARCHAR,
        kind         VARCHAR,        -- collect | daily | adj_factor
        schedule_time VARCHAR,       -- HH:MM（本地时间）
        weekdays     VARCHAR,        -- ISO 周几逗号分隔，1=周一；空 = 每天
        params       JSON,
        enabled      BOOLEAN DEFAULT TRUE,
        last_run_at  TIMESTAMP,
        last_status  VARCHAR,
        last_rows    INTEGER,
        created_at   TIMESTAMP,
        updated_at   TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_run (
        run_id     VARCHAR PRIMARY KEY,
        sync_id    VARCHAR,
        job_name   VARCHAR,
        kind       VARCHAR,
        started_at TIMESTAMP, finished_at TIMESTAMP,
        rows INTEGER, status VARCHAR, detail JSON
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_setting (
        setting_key VARCHAR PRIMARY KEY,
        setting_value VARCHAR,
        source      VARCHAR,          -- default / config / runtime（运行时 PUT 写这里）
        updated_at  TIMESTAMP DEFAULT now()
    )
    """,
]

VIEWS: list[str] = [
    """
    CREATE OR REPLACE VIEW v_daily AS
    SELECT * FROM read_parquet('data/parquet/daily/**/*.parquet')
    """
]


def ensure_factor_def(con) -> int:
    """factor_def 结构迁移：旧表（expr/category/universe 列）→ 新表（expression/description）。

    迁移时搬运 name/expr→expression/enabled/created_at；因子定义可随时重注册，
    重建无损。幂等：新结构直接跳过。返回是否执行了迁移。
    """
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    if "expression" in cols:
        return 0
    con.execute("ALTER TABLE factor_def RENAME TO factor_def_old")
    new_ddl = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS factor_def" in s)
    con.execute(new_ddl)
    select_cols = ", ".join(
        ["name", "expr AS expression" if "expr" in cols else "NULL AS expression",
         *(c for c in ("enabled", "created_at") if c in cols)])
    con.execute(f"INSERT INTO factor_def (name, expression, enabled, created_at) "
                f"SELECT {select_cols} FROM factor_def_old")
    con.execute("DROP TABLE factor_def_old")
    return 1


def ensure_classify_snapshots(con) -> int:
    """industry_classify 主键迁移：老库无 PK → 重建立 (symbol, std_date)。

    申万分类表是 ingest 可重刷的小参考表，直接删表重建无损（同 collect_log 先例）。
    不加这个，老库上 _upsert 无 PK 分支会因没有 epoch 键而退化成语义错误的「纯追加」，
    重投一次胖一轮。返回是否重排了。
    """
    cols = con.execute("DESCRIBE industry_classify").fetchall()
    if any(r[3] == "PRI" for r in cols):
        return 0
    con.execute("DROP TABLE industry_classify")
    new_ddl = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS industry_classify" in s)
    con.execute(new_ddl)
    return 1


def ensure_collect_log(con) -> int:
    """collect_log 迁移：旧表无主键 → 加 (job, trade_date) 主键。

    日志数据可再生，直接删表重建无损。返回是否执行了迁移。
    """
    cols = con.execute("DESCRIBE collect_log").fetchall()
    has_pk = any(r[3] == "PRI" for r in cols)
    if has_pk:
        return 0
    con.execute("DROP TABLE collect_log")
    new_ddl = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS collect_log" in s)
    con.execute(new_ddl)
    return 1
