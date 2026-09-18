"""业务日期口径守卫：业务关键模块不许再用本机时区的 `date.today()` / `datetime.now()`。

为什么值得一条测试：服务器/容器时区非 Asia/Shanghai 时，本机日期会与业务日
错位一天 —— 采到错日的数据、作业窗口整体偏移、资讯按日聚合落到前一天，
而且**不报任何错**。这类缺陷只能靠约定 + 守卫测试守住（全仓统一走
core.types 的 today_cn() / now_cn() / now_cn_naive()）。

只覆盖「业务日期/业务时刻」模块。纯 wall-clock 记录（health 的 ts、
created_at、报告生成时间、指标保留 cutoff）不属于本约定，显式列在
WALL_CLOCK_OK 里，避免守卫变成噪音而被整体忽略。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "lquant"

# 业务日期/业务时刻关键模块（相对 src/lquant）
BUSINESS_MODULES = (
    "core/types.py",
    "sync/manager.py",
    "news/tasks.py",
    "news/store.py",
    "data/ingest/daily.py",
    "data/ingest/daily_basic.py",
    "data/ingest/reference.py",
    "data/ingest/tasks.py",
    "data/ingest/checkpoint.py",
    "data/ingest/financial.py",
    "data/ingest/index_cons.py",
    "data/ingest/adj.py",
    "data/lineage.py",
    "data/quality/coverage.py",
    "data/providers/baostock.py",
    "data/providers/tushare.py",
    "data/providers/akshare.py",
    "market/scheduler.py",
    "market/collect_log.py",
    "market/ticks.py",
    "market/collectors/limit_up.py",
    "market/collectors/sentiment.py",
    "market/collectors/sector.py",
    "market/collectors/money_flow.py",
    "market/collectors/dragon_tiger.py",
    "market/collectors/index_daily.py",
    "monitor/flusher.py",
    "server/api/news.py",
    "server/api/market.py",
    "agent/mcp_server.py",
    "research/dialect/jq_shim.py",
    "backtest/jqapi.py",
)

# 本机时区是正确语义的地方：这些不是业务日期，写 today_cn() 反而错
# （按行内特征白名单，而不是整个文件豁免 —— 否则文件里新增的违规也放过去了）
WALL_CLOCK_OK: dict[str, tuple[str, ...]] = {
    # 指标保留 cutoff 与 epoch 时间戳列比较，必须是本机时钟
    "monitor/flusher.py": ("else datetime.now()",),
}

# 用注释/文档串说明历史错误的行不算违规（只查真正的调用）
_CALL_RE = re.compile(r"(?<![\w.])(?:date|datetime)\.(today|now)\(\s*\)")


def _strip_comments_and_docstrings(text: str) -> list[tuple[int, str]]:
    """返回 (行号, 代码行)，剔除注释行与三引号块内的行。

    文档串里会正当地出现 `date.today()`（解释为什么不能用它），
    不能因此判违规。
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if in_block:
            if '"""' in stripped or "'''" in stripped:
                in_block = False
            continue
        if stripped.count('"""') == 1 or stripped.count("'''") == 1:
            in_block = True
            # 单行开头就是文档串 → 该行不是代码
            if stripped.startswith(('"""', "'''")):
                continue
        if stripped.startswith("#"):
            continue
        out.append((i, line.split("#")[0] if "#" in line else line))
    return out


def test_no_local_timezone_business_dates():
    offenders: list[str] = []
    for rel in BUSINESS_MODULES:
        p = _SRC / rel
        assert p.exists(), f"守卫清单里的模块不存在（改名后请同步更新）: {rel}"
        allowed = WALL_CLOCK_OK.get(rel, ())
        for lineno, code in _strip_comments_and_docstrings(p.read_text(encoding="utf-8")):
            if _CALL_RE.search(code) and not any(m in code for m in allowed):
                offenders.append(f"{rel}:{lineno}: {code.strip()}")
    assert not offenders, (
        "业务日期必须走 core.types（today_cn/now_cn/now_cn_naive），"
        "本机时区会在非 Asia/Shanghai 服务器上错位一天:\n  "
        + "\n  ".join(offenders))


def test_core_types_helpers_use_shanghai_tz():
    """三个助手本身必须是同一个时区口径，且 naive 版本不丢偏移。"""
    from datetime import date, datetime, timedelta

    from lquant.core.types import TZ, now_cn, now_cn_naive, today_cn

    assert str(TZ) == "Asia/Shanghai"
    assert now_cn().tzinfo is not None
    assert now_cn_naive().tzinfo is None
    # 同一时刻、同一墙钟：naive 版本只是把偏移摘掉，不换时区
    delta = abs(now_cn_naive() - now_cn().replace(tzinfo=None))
    assert delta < timedelta(seconds=5)
    assert today_cn() == now_cn().date()
    assert isinstance(today_cn(), date) and not isinstance(today_cn(), datetime)


def test_sync_manager_uses_cn_clock(monkeypatch):
    """tick 的缺省时刻与 run_job 的业务日都必须来自 CN 墙钟。

    用「UTC 08:00 == CST 16:00」证明：15:05 的作业在 UTC 视角下
    datetime.now() 是 08:00（未到点，不会跑），而 CN 墙钟是 16:00（该跑）。
    """
    from datetime import UTC, datetime

    from lquant.sync import manager

    job = {"sync_id": "close", "name": "c", "kind": "collect", "schedule_time": "15:05",
           "weekdays": "1,2,3,4,5", "params": {}, "enabled": True, "last_run_at": None}
    monkeypatch.setattr(manager, "list_jobs", lambda: [job])
    monkeypatch.setattr(manager, "_trading_day_ok", lambda d: True)
    ran: list[str] = []
    monkeypatch.setattr(manager, "run_job",
                        lambda j, **kw: ran.append(j["sync_id"]) or
                        {"status": "ok", "rows": 0})

    manager.tick(datetime(2026, 9, 8, 8, 0, tzinfo=UTC))    # CST 16:00 → 到期
    assert ran == ["close"]
    ran.clear()
    manager.tick(datetime(2026, 9, 8, 6, 0, tzinfo=UTC))    # CST 14:00 → 未到期
    assert ran == []
