"""回测实验记录器（借鉴 qlib R 实验记录的思想）。

每一次回测都是一个实验：参数、数据窗口、代码版本、结果指标应当可以被
事后检索与对比，否则「这次比上次好」永远说不清是谁改了什么。

存储复用 DuckDB 的 ``backtest_run`` 表（DDL 在 ``data/store/ddl.py``，
Web API 提交的回测也落这张表）—— CLI 与 Web 的实验历史**同源可查**。
与 API 层（``server/api/backtests.py::_persist_result`` 落 nav/order/
position 逐日明细）不同，本模块只记 run 头：因子分层回测没有逐日
nav 与成交流，硬造明细只会得到空表。

代码版本追溯：params JSON 里自动附 ``git_hash``（非 git 环境、缺 git
可执行文件时省略该键 —— 版本追溯是尽力而为，绝不阻断记录本身）。

失败语义：record/list/get/diff 的数据库错误**原样上抛**（fail-loudly），
由调用方决定是否降级 —— CLI 的 ``run`` 命令会把记录失败转成 stderr
警告（回测结果已经算出来了，不能因为记账失败而丢掉主输出）。
"""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import uuid
from datetime import date, datetime
from pathlib import Path

__all__ = ["count_runs", "diff_runs", "get_run", "git_hash", "list_runs", "record_run"]

# 本包上溯的仓库根（editable 安装 = src 上一级）。pip 装进 site-packages 时
# 该目录不在 git 仓库内 → git_hash 返回 None，不会误记别处仓库的 hash。
_REPO_ROOT = Path(__file__).resolve().parents[3]


def git_hash() -> str | None:
    """lquant 仓库当前短 hash；非 git 环境（pip 安装、无 .git）返回 None。

    显式固定 cwd 到仓库根：常驻进程（server/agent）的 cwd 可能落在
    **别的** git 仓库里，git 就近解析会把异库 hash 记进 params，
    版本追溯失真。
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            cwd=_REPO_ROOT,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - 版本追溯尽力而为，不阻断记录
        return None


def _json_safe(obj):
    """NaN/Inf → None、numpy 标量 → Python 标量（与 API 层 _json_safe 同规）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (bool, str)) or obj is None:
        return obj
    if hasattr(obj, "item"):
        try:
            return _json_safe(obj.item())
        except Exception:  # noqa: BLE001
            return str(obj)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _dumps(obj) -> str:
    return json.dumps(_json_safe(obj), ensure_ascii=False, default=str)


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _now() -> datetime:
    """仓库约定时区（Asia/Shanghai）的 naive 时间戳，不用裸本地时间。"""
    from lquant.core.types import now_cn_naive

    return now_cn_naive()


def _as_date(v) -> date | None:
    """DuckDB start_date/end_date 是 DATE 列：字符串在此显式转 date，
    不依赖驱动的隐式 cast（转换失败在记账前就 fail-loudly）。"""
    if v is None or isinstance(v, datetime):
        return v.date() if isinstance(v, datetime) else v
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def record_run(
    strategy: str,
    params: dict,
    metrics: dict,
    *,
    start_date=None,
    end_date=None,
    status: str = "done",
) -> str:
    """记一条实验，返回 run_id。

    params 里自动附 ``git_hash``（拿得到时）。数据库错误原样上抛
    （含 run_id 撞键：12 hex 随机主键撞概率极低，但 OR REPLACE 的
    「静默覆盖旧实验」后果不可接受 —— 宁可报错，由 CLI 转警告）。

    写前按需补建 ``backtest_run``：该表只在 ``DDL_STATEMENTS`` 里（init_db /
    服务启动执行），老库或隔离测试库上缺表会让「默认记录」变成一条警告 ——
    惰性补建把这条路径修成「记录就该成功」。
    """
    run_id = uuid.uuid4().hex[:12]
    payload = dict(params or {})
    gh = git_hash()
    if gh:
        payload["git_hash"] = gh
    now = _now()
    with _writer() as con:
        _ensure_table(con)
        con.execute(
            "INSERT INTO backtest_run (run_id, strategy, params, start_date, "
            "end_date, status, metrics, created_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                strategy,
                _dumps(payload),
                _as_date(start_date),
                _as_date(end_date),
                status,
                _dumps(metrics),
                now,
                now,
            ],
        )
    return run_id


def list_runs(limit: int = 20, strategy: str | None = None) -> list[dict]:
    """最近优先的实验清单（run 头，params/metrics 已反序列化）。"""
    sql = (
        "SELECT run_id, strategy, params, start_date, end_date, status, "
        "metrics, created_at FROM backtest_run"
    )
    args: list = []
    if strategy:
        sql += " WHERE strategy = ? OR json_extract_string(params, '$.factor') = ?"
        args.extend([strategy, strategy])
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    _ensure_table()  # 先补表（写锁），再读；避免读连接里开写连接
    with _reader() as con:
        rows = con.execute(sql, args).fetchall()
    return [
        {
            "run_id": r[0],
            "strategy": r[1],
            "params": _loads(r[2]),
            "start_date": r[3],
            "end_date": r[4],
            "status": r[5],
            "metrics": _loads(r[6]),
            "created_at": r[7],
        }
        for r in rows
    ]


def count_runs(strategy: str | None = None) -> int:
    """试验台账行数 —— DSR 的 n_trials 来源。

    台账纪律：必须统计**所有**试过的配置（含放弃的、失败的），只数
    活下来的会让 DSR 变成橡皮图章。所以这里数的是 backtest_run 全表
    （可选按策略过滤），不做任何「成功」过滤。

    过滤口径必须**同时**匹配 ``strategy`` 列与 ``params.factor``：
    ``lq backtest run`` 落库时 strategy 列恒为 ``factor_quantile``，因子名
    只写在 params 里，而 README 的示例是 ``--strategy mom_20`` ——
    只比 strategy 列会让文档给的命令恒得 0 次试验，进而静默跳过 DSR
    （把「台账为空」误报成「无选择偏差可校正」）。
    """
    sql = "SELECT count(*) FROM backtest_run"
    args: list = []
    if strategy:
        sql += " WHERE strategy = ? OR json_extract_string(params, '$.factor') = ?"
        args.extend([strategy, strategy])
    _ensure_table()  # 先补表（写锁），再读；避免读连接里开写连接
    with _reader() as con:
        return int(con.execute(sql, args).fetchone()[0])


def get_run(run_id: str) -> dict:
    """单条实验详情；不存在抛 KeyError（fail-loudly，CLI 层转 ClickException）。"""
    _ensure_table()  # 先补表（写锁），再读；避免读连接里开写连接
    with _reader() as con:
        row = con.execute(
            "SELECT run_id, strategy, params, start_date, end_date, status, "
            "metrics, created_at FROM backtest_run WHERE run_id = ?",
            [run_id],
        ).fetchone()
    if not row:
        raise KeyError(run_id)
    return {
        "run_id": row[0],
        "strategy": row[1],
        "params": _loads(row[2]),
        "start_date": row[3],
        "end_date": row[4],
        "status": row[5],
        "metrics": _loads(row[6]),
        "created_at": row[7],
    }


def diff_runs(run_a: str, run_b: str) -> dict:
    """两个实验的 params / metrics 键级对比。

    返回 ``{"a": 头, "b": 头, "params": {...}, "metrics": {...}}``；
    每个 diff 节是 ``{"only_a":…, "only_b":…, "changed": {k: [va, vb]}}``。
    git_hash 不同也会被列出 —— 正是「同一表达式不同代码版本结果不可比」
    的显式证据。
    """
    a = get_run(run_a)
    b = get_run(run_b)

    def _diff(da: dict, db: dict) -> dict:
        keys_a, keys_b = set(da), set(db)
        return {
            "only_a": {k: da[k] for k in sorted(keys_a - keys_b)},
            "only_b": {k: db[k] for k in sorted(keys_b - keys_a)},
            "changed": {k: [da[k], db[k]] for k in sorted(keys_a & keys_b) if da[k] != db[k]},
        }

    return {
        "a": a,
        "b": b,
        "params": _diff(a["params"], b["params"]),
        "metrics": _diff(a["metrics"], b["metrics"]),
    }


def _writer():
    """延迟 import：避免 CLI --help 之外的场景拖起 DuckDB 连接栈。"""
    from lquant.core.db import writer

    return writer()


def _reader():
    from lquant.core.db import reader

    return reader()


def _ensure_table(con=None) -> None:
    """按需补建 backtest_run（迁移路径，见 ``ddl.ensure_backtest_run``）。

    读写两条路径都先补一次：``lq backtest list/show`` 在老库上撞缺表时，
    报出来的应该是「还没有实验记录」而不是驱动层的 CatalogException。

    写路径传入已持有的写连接（避免重入写锁）；读路径不带参数 → 单独开一次
    写连接补表（DDL 走写锁，符合 db.py 的并发约定）。补建失败（只读文件系统 /
    无权限）不阻断：真缺表时后面的查询会照旧报错。
    """
    from lquant.data.store.ddl import ensure_backtest_run

    if con is not None:
        # 迁移尽力而为：失败不掩盖真正的查询错误（缺表时后面的查询会照旧报错）
        with contextlib.suppress(Exception):
            ensure_backtest_run(con)
        return
    with contextlib.suppress(Exception), _writer() as wcon:
        ensure_backtest_run(wcon)
