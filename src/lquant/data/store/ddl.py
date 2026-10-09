"""DuckDB 表结构。幂等 DDL 列表。

质量三表（data_quality_issue / data_version / golden_expected）的 DDL
在这里唯一定义，quality 层模块从这里 import —— 别处再写一份迟早漂移
（collect_log 前车之鉴）。
"""
from __future__ import annotations

from pathlib import Path

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
        enabled BOOLEAN DEFAULT TRUE, created_at TIMESTAMP,
        source VARCHAR DEFAULT 'manual', source_ref VARCHAR,
        factor_id VARCHAR, category VARCHAR DEFAULT '',
        -- 入库时的中性化协变量清单（JSON 数组，NULL = 未声明 → 复算走 DEFAULT_COVS）。
        -- 不落库的话同一因子在 submit 与 audit/run 是两套中性化口径，IC 不可比。
        covs JSON
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
        created_at TIMESTAMP,
        -- Phase 2.1：模型注册表。此前 ml_run 只有指标，没有 artifact 指针，
        -- 「这条记录对应哪个模型文件」无从回答，滚动重训也就无法回放。
        model_name      VARCHAR,          -- 逻辑模型线（同一策略的多个版本共享）
        model_version   INTEGER,          -- 该 model_name 下单调递增
        stage           VARCHAR,          -- candidate|staging|production|archived
        artifact_path   VARCHAR,          -- 模型文件（相对仓库根，便于迁移）
        processor_path  VARCHAR,          -- 特征处理器状态 JSON
        processor_state JSON,             -- 处理器的可读副本（不必开文件即可看口径）
        fit_window      JSON,             -- {train_start,train_stop,train_rows,...}
        dataset         JSON              -- 数据集摘要（行数/票数/交易日数）
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_model (
        name           VARCHAR,          -- 逻辑模型线，如 momentum_lgbm
        version        INTEGER,          -- 该 name 下单调递增（1 起）
        run_id         VARCHAR,          -- 对应的 ml_run（训练记录）
        stage          VARCHAR,          -- candidate|staging|production|archived
        artifact_path  VARCHAR,
        processor_path VARCHAR,
        metrics        JSON,
        fit_window     JSON,
        note           VARCHAR,          -- 晋级/回滚备注（审计用）
        created_at     TIMESTAMP,
        promoted_at    TIMESTAMP,        -- 进入当前 stage 的时刻（as-of 回放依据）
        PRIMARY KEY (name, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_signal (
        name          VARCHAR,          -- 逻辑模型线（与 ml_model.name 对应）
        trade_date    DATE,
        symbol        VARCHAR,
        signal        DOUBLE,           -- 模型原始输出（未做组合权重）
        model_version INTEGER,          -- 出这个信号时线上是哪一版（可审计）
        run_id        VARCHAR,
        created_at    TIMESTAMP,
        PRIMARY KEY (name, trade_date, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_model_event (
        event_id   VARCHAR PRIMARY KEY,
        name       VARCHAR,          -- 逻辑模型线
        version    INTEGER,
        from_stage VARCHAR,
        to_stage   VARCHAR,
        note       VARCHAR,
        occurred_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_ledger (
        agent VARCHAR PRIMARY KEY, eval_count INTEGER,
        eval_last TIMESTAMP, updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_ic (
        factor VARCHAR PRIMARY KEY, ic_raw DOUBLE, ic_neutral DOUBLE,
        rank_ic_neutral DOUBLE, n_days INTEGER, updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_ic_daily (
        factor VARCHAR, trade_date DATE,
        ic DOUBLE, rank_ic DOUBLE, n INTEGER,
        updated_at TIMESTAMP,
        PRIMARY KEY (factor, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_mining_run (
        run_id VARCHAR PRIMARY KEY, agent VARCHAR, generator VARCHAR,
        n_evaluated INTEGER, n_static_fail INTEGER, n_low_ic INTEGER,
        n_redundant INTEGER, n_size_proxy INTEGER, n_survivors INTEGER,
        corrections JSON, created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_replication (
        name VARCHAR, expr VARCHAR, claimed_ic DOUBLE, recomputed_ic DOUBLE,
        grade VARCHAR, agent VARCHAR, payload VARCHAR, created_at TIMESTAMP,
        PRIMARY KEY (name, expr, created_at)
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
    "CREATE TABLE IF NOT EXISTS strategy_def ("
    " id VARCHAR, name VARCHAR, description VARCHAR, kind VARCHAR DEFAULT 'jq',"
    " source VARCHAR, params_json VARCHAR, benchmark VARCHAR, config_json VARCHAR,"
    " version INTEGER, is_latest BOOLEAN DEFAULT TRUE, deleted BOOLEAN DEFAULT FALSE,"
    " created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS analysis_def ("
    " id VARCHAR, name VARCHAR, source VARCHAR, is_builtin BOOLEAN DEFAULT FALSE,"
    " deleted BOOLEAN DEFAULT FALSE, created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS backtest_record ("
    " run_id VARCHAR, trade_date DATE, key VARCHAR, value DOUBLE)",
    """
    CREATE TABLE IF NOT EXISTS app_setting (
        setting_key VARCHAR PRIMARY KEY,
        setting_value VARCHAR,
        source      VARCHAR,          -- default / config / runtime（运行时 PUT 写这里）
        updated_at  TIMESTAMP DEFAULT now()
    )
    """,
    # B5 条件级事后核验：冻结的研究判断 + 逐次核验结果。
    # 拆两张表而不是一张带状态列的表：条件本身**不可变**（锚点/阈值/历史指纹
    # 一旦冻结就不许改写，否则事后对账失去意义），核验结果则随窗口推进**多版本**
    # 累积。混在一张表里，「同条件重复核验幂等」就得先擦掉旧结果 —— 那正好把
    # 「窗口未走完时怎么判、走完后怎么判」的演进过程抹掉了。
    """
    CREATE TABLE IF NOT EXISTS research_condition (
        condition_id   VARCHAR PRIMARY KEY,   -- 内容哈希：同条件重冻结不新增行
        symbol         VARCHAR,
        as_of          DATE,                  -- 做出判断的交易日（锚点所在日）
        anchor         DOUBLE,                -- 冻结时的收盘价锚点，程序算出，不许事后编造
        conditions     JSON,                  -- [{metric,op,threshold,avg_days}, ...]
        window_days    INTEGER,               -- 「未来 N 个交易日内」
        note           VARCHAR,
        overlap_closes JSON,                  -- as_of 前的重叠收盘价指纹，用于检测复权/修订
        created_at     TIMESTAMP DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_verify_result (
        condition_id    VARCHAR,
        checked_through DATE,                 -- 本次核验用到的最近已完成交易日
        verdict         VARCHAR,              -- triggered / not_triggered / unavailable / unverifiable
        window_complete BOOLEAN,              -- 窗口是否已走完（与「未命中」是两件事）
        checked_days    INTEGER,
        missing_days    JSON,                 -- 窗口内缺日线的会话（停牌/采集缺失，不跳过）
        evidence        JSON,                 -- 逐日逐条件：实际值 / 阈值 / 是否命中
        reason          VARCHAR,
        created_at      TIMESTAMP DEFAULT now(),
        PRIMARY KEY (condition_id, checked_through)
    )
    """,
    # B4 研判闭环：不可变研判快照 + 逐次对账结果。
    # 拆表理由同 B5：研判快照**落库即不可改**（改判只能另起一个交易日的新研判），
    # 而「窗口推进到哪一天、当时怎么判」必须多版本累积。合成一张带状态列的表，
    # 就会为了更新状态而重写快照 —— 那正好把「当时写了什么」抹掉。
    #
    # `UNIQUE (trade_date)`：一个交易日**只允许一条**研判快照。应用层已经会对
    # 「同内容重复记录」幂等返回、对「同交易日不同内容」显式报错，这里再加一道
    # 数据库约束：并发写入也绝不会悄无声息地多出一行「改判版本」。
    """
    CREATE TABLE IF NOT EXISTS research_outlook (
        outlook_id   VARCHAR PRIMARY KEY,   -- 内容哈希：同内容重复记录不新增行
        trade_date   DATE,                  -- 做出研判的交易日（收盘后）
        market_state VARCHAR,
        evidence     JSON,                  -- 冻结的当时证据（regime 全量输出等）
        scenarios    JSON,
        directions   JSON,
        focus_next   JSON,
        checklist    JSON,                  -- 冻结后的可核验清单（含 FrozenCondition 全文）
        notes        VARCHAR,
        weights      JSON,                  -- 记录时使用的维度权重口径
        created_at   TIMESTAMP DEFAULT now(),
        UNIQUE (trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_outlook_validation (
        outlook_id      VARCHAR,
        checked_through DATE,               -- 本次对账用到的最近已完成交易日
        final           BOOLEAN,            -- 是否全部清单项窗口已走完
        score           DOUBLE,             -- 0~100；分母**只含已判定项**，未判定不拉低分数
        coverage        DOUBLE,             -- 已判定权重 / 全部权重（未判定留在分母里，不消失）
        counts          JSON,
        dimensions      JSON,
        items           JSON,
        lessons         JSON,
        realized_risks  JSON,
        created_at      TIMESTAMP DEFAULT now(),
        PRIMARY KEY (outlook_id, checked_through)
    )
    """,
]

# 注意：daily_bar / minute_bar 两张 DuckDB 表是**遗留占位**。
# 按设计（ARCHITECTURE §存储分工）日线/分钟线等大表只入 Parquet 湖，
# 从没有任何写入路径写这两张表 —— 查它们会"成功"但恒空。
# 读取一律走 store/parquet.py（read_daily / read_minute），
# SQL 侧用 lake_glob() 交给 read_parquet。保留 DDL 仅为兼容旧库。
def ensure_views(con, parquet_dir: str | Path | None = None) -> int:
    """建 lake 视图 v_daily / v_minute，返回创建条数。

    仅供即席 SQL 便利；Python 侧一律走 store/parquet.py 的读函数。
    路径必须**解析成绝对路径**：DuckDB 视图是持久化 catalog 对象，
    创建时的相对路径在进程 CWD 变化后会指向别处。

    空湖跳过：DuckDB 在 CREATE VIEW 时就要解析 read_parquet 的 schema，
    匹配不到文件直接报 IO Error —— 全新 checkout 不该因此建库失败。
    """
    if parquet_dir is None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        parquet_dir = get_settings().parquet_dir
    root = Path(parquet_dir).resolve()
    n = 0
    for name, kind in (("v_daily", "daily"), ("v_minute", "minute")):
        glob = root / kind / "**" / "*.parquet"
        if not any(root.glob(f"{kind}/**/*.parquet")):
            continue
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{glob}')"
        )
        n += 1
    return n


def ensure_factor_def_covs(con) -> int:
    """factor_def 增列迁移（covs）：入库时的中性化协变量清单（JSON 数组）。

    audit/run 复算要读回它；老库没有这一列时复算会静默退回 ``DEFAULT_COVS``，
    与 submit 的口径分叉。单独一条而不并进 ``ensure_factor_def_columns`` 的计数：
    那条函数的历史契约是「factor_def 的四个结构列加了几条」，很多调用方/测试
    按 4 计数；covs 属于评价口径，挂在同一条启动 ensure 链路上即可。幂等。
    """
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    if "covs" in cols:
        return 0
    con.execute("ALTER TABLE factor_def ADD COLUMN covs JSON")
    return 1


def ensure_factor_def_columns(con) -> int:
    """factor_def 增列迁移：source / source_ref / factor_id / category（+ covs）。

    covs 由 ``ensure_factor_def_covs`` 在同一条启动链路上补齐（单独计数，
    见该函数说明）。幂等：列已存在直接跳过。返回四个基础列执行了几条 ALTER。
    """
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    n = 0
    for col, typ in (("source", "VARCHAR DEFAULT 'manual'"), ("source_ref", "VARCHAR"),
                     ("factor_id", "VARCHAR"), ("category", "VARCHAR DEFAULT ''")):
        if col not in cols:
            con.execute(f"ALTER TABLE factor_def ADD COLUMN {col} {typ}")
            n += 1
    ensure_factor_def_covs(con)
    return n


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


def _ensure_table(con, table: str) -> int:
    """按 ``DDL_STATEMENTS`` 里的建表语句惰性补建 ``table``（幂等）。

    给「只进了 DDL_STATEMENTS、没跟着 init_db / 服务启动迁移」的新表用：
    老库上直接读写会裸 ``CatalogException``，补建一次即自愈。
    返回 1 = 本次真的建了表。
    """
    ddl = next(s for s in DDL_STATEMENTS if f"CREATE TABLE IF NOT EXISTS {table} " in s)
    before = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [table]
    ).fetchone()[0]
    con.execute(ddl)
    return 0 if before else 1


def ensure_backtest_run(con) -> int:
    """按需补建 ``backtest_run``（CLI 实验记录器与 Web 回测提交共用）。

    这张表一直只在 ``DDL_STATEMENTS`` 里（init_db / 服务启动才执行），而
    ``lq backtest run`` 默认落库 —— 老库上缺表时记录失败，只剩一条 stderr
    警告（回测结果不受影响）。惰性补建让老库自愈。返回 1 = 本次建了表。
    """
    return _ensure_table(con, "backtest_run")


def ensure_factor_ic_daily(con) -> int:
    """按需补建 ``factor_ic_daily``（因子在线监控的逐日 IC 表）。

    同 ``ensure_backtest_run``：监控写路径与 ``lq factor ic-health`` 读路径
    在老库上都撞到过裸的 ``CatalogException``。返回 1 = 本次建了表。
    """
    return _ensure_table(con, "factor_ic_daily")


def ensure_research_verify_tables(con) -> int:
    """按需补建条件核验两张表（research_condition / research_verify_result）。

    这两张表只进了 ``DDL_STATEMENTS``（init_db / 服务启动才执行）：老库或
    隔离测试库上直接写核验记录会撞裸 ``CatalogException`` —— 而「冻结了却
    没记下来」正是本功能要消灭的静默失败（下次没人知道当时判断的是什么）。
    惰性补建让老库自愈。返回本次建表数（0~2），幂等。
    """
    return (_ensure_table(con, "research_condition")
            + _ensure_table(con, "research_verify_result"))


def ensure_research_journal_tables(con) -> int:
    """按需补建研判闭环两张表（research_outlook / research_outlook_validation）。

    同 ``ensure_research_verify_tables``：这两张表只进了 ``DDL_STATEMENTS``，
    老库/隔离测试库上直接写研判快照会撞裸 ``CatalogException`` —— 而「判断
    说了却没留痕」正是本功能要消灭的静默失败。惰性补建让老库自愈。
    返回本次建表数（0~2），幂等。
    """
    return (_ensure_table(con, "research_outlook")
            + _ensure_table(con, "research_outlook_validation"))


def ensure_ml_run_columns(con) -> int:
    """ml_run 增列迁移（Phase 2.1 模型注册表）。

    老库的 ml_run 只有指标列，没有 artifact 指针 —— 训练记录无法回放到模型
    文件。加列是幂等且无损的（旧行新列为 NULL，`ml_model` 另行补齐版本信息）。
    返回新增列数。
    """
    cols = {r[0] for r in con.execute("DESCRIBE ml_run").fetchall()}
    n = 0
    for col, typ in (
        ("model_name", "VARCHAR"),
        ("model_version", "INTEGER"),
        ("stage", "VARCHAR"),
        ("artifact_path", "VARCHAR"),
        ("processor_path", "VARCHAR"),
        ("processor_state", "JSON"),
        ("fit_window", "JSON"),
        ("dataset", "JSON"),
    ):
        if col not in cols:
            con.execute(f"ALTER TABLE ml_run ADD COLUMN {col} {typ}")
            n += 1
    return n
