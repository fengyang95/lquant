"""告警规则引擎：规则 CRUD + 评估 + 冷却持久化（sqlite，独立于 DuckDB 主库）。

借鉴 daily_stock_analysis 的 Alert Center 形态，按 lquant 的克制方式裁剪：

- **规则即数据**：AlertRule 存 SQLite（``LQ_NOTIFY_DB``，缺省与 paper 同级），
  重启不丢；评估是无副作用的纯函数，触发动作（发通知）由 ``run_rules`` 编排。
- **冷却持久化到 DB**（``cooldown_until``）：跨进程重启不丢 —— 与
  ``service.should_suppress`` 的进程内降噪互补：那边管"同一进程连发"，
  这边管"这条规则 N 秒内只许触发一次"。
- **DryRun 一等公民**：``evaluate()`` 只判不记、不发；``run_rules()`` 才
  落触发状态 + 发通知。前端"测试规则"按钮直接复用 evaluate。
- **评估失败不静默**：缺字段/类型错返回 ``evaluation_error`` 并带原因，
  绝不悄悄当成"未触发"—— 错误的规则比没有规则更危险。

alert_type 首批五种（都是纯数值比较，不碰数据源）：
  price_above / price_below   —— ctx 需 last_price
  pct_change_up / pct_change_down —— ctx 需 last_price + pre_close
  ic_below                    —— ctx 需 ic（因子监控消费，见 factors/monitor.py：
                                 run_daily_check 把因子近窗口 IC 组装成 ctx 喂进来，
                                 target 放因子名；threshold 语义 = IC 下限，跌破即告警）
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = [
    "AlertRule",
    "RuleStore",
    "get_store",
    "evaluate",
    "run_rules",
    "TRIGGERED",
    "NOT_TRIGGERED",
    "EVAL_ERROR",
    "COOLDOWN",
]

TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
EVAL_ERROR = "evaluation_error"
COOLDOWN = "cooldown"

ALERT_TYPES = ("price_above", "price_below", "pct_change_up", "pct_change_down", "ic_below")
SCOPES = ("single_symbol", "watchlist", "portfolio", "market")
SEVERITIES = ("info", "warning", "critical")


def db_path() -> Path:
    env = os.getenv("LQ_NOTIFY_DB")
    if env:
        p = Path(env)
    else:
        from lquant.core.config import get_settings

        p = Path(get_settings().parquet_dir).parent / "notify" / "rules.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class AlertRule:
    """告警规则。parameters 语义由 alert_type 决定（threshold / pct 等，全数值）。"""

    name: str
    target_scope: str = "single_symbol"  # single_symbol|watchlist|portfolio|market
    target: str = ""  # symbol（600000.SH）或池名；market 留空
    alert_type: str = "price_above"
    parameters: dict = field(default_factory=lambda: {"threshold": 0.0})
    severity: str = "warning"
    enabled: bool = True
    cooldown_seconds: int = 300  # 触发后 N 秒内不再重复触发
    id: int | None = None
    last_triggered_at: str | None = None
    cooldown_until: str | None = None

    def validate(self) -> list[str]:
        """返回问题清单（空 = 合法）。API 创建/更新前必须过这道闸。"""
        errs = []
        if not self.name:
            errs.append("name 不能为空")
        if self.target_scope not in SCOPES:
            errs.append(f"target_scope 须为 {'/'.join(SCOPES)}")
        elif self.target_scope in ("watchlist", "portfolio"):
            # run_rules 只按 symbol 匹配 ctx；池名永不命中会让规则「配了却
            # 永远不响」—— 与其静默不触发，不如创建时就拒绝（不静默哲学）。
            errs.append(
                f"target_scope={self.target_scope} 暂不支持（当前仅 "
                "single_symbol / market 可实际触发）"
            )
        if self.alert_type not in ALERT_TYPES:
            errs.append(f"alert_type 须为 {'/'.join(ALERT_TYPES)}")
        if self.severity not in SEVERITIES:
            errs.append(f"severity 须为 {'/'.join(SEVERITIES)}")
        th = self.parameters.get("threshold")
        if not isinstance(th, (int, float)) or isinstance(th, bool):
            errs.append("parameters.threshold 须为数值")
        return errs


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  target_scope TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  alert_type TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  severity TEXT NOT NULL DEFAULT 'warning',
  enabled INTEGER NOT NULL DEFAULT 1,
  cooldown_seconds INTEGER NOT NULL DEFAULT 300,
  last_triggered_at TEXT,
  cooldown_until TEXT,
  created_at TEXT NOT NULL
);
"""


class RuleStore:
    """规则与冷却状态的 SQLite 持久化。WAL + 每次新建连接，跨进程安全。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else db_path()
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _row_to_rule(r: sqlite3.Row) -> AlertRule:
        return AlertRule(
            id=r["id"],
            name=r["name"],
            target_scope=r["target_scope"],
            target=r["target"],
            alert_type=r["alert_type"],
            parameters=json.loads(r["parameters_json"]),
            severity=r["severity"],
            enabled=bool(r["enabled"]),
            cooldown_seconds=r["cooldown_seconds"],
            last_triggered_at=r["last_triggered_at"],
            cooldown_until=r["cooldown_until"],
        )

    def add(self, rule: AlertRule) -> AlertRule:
        errs = rule.validate()
        if errs:
            raise ValueError("; ".join(errs))
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO notify_rules(name,target_scope,target,alert_type,"
                "parameters_json,severity,enabled,cooldown_seconds,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    rule.name,
                    rule.target_scope,
                    rule.target,
                    rule.alert_type,
                    json.dumps(rule.parameters),
                    rule.severity,
                    int(rule.enabled),
                    rule.cooldown_seconds,
                    _now_iso(),
                ],
            )
            rule.id = cur.lastrowid
        return rule

    def update(self, rule_id: int, **fields) -> AlertRule:
        cur_rule = self.get(rule_id)
        if cur_rule is None:
            raise ValueError(f"规则不存在: {rule_id}")
        for k, v in fields.items():
            if k == "parameters":
                continue
            if hasattr(cur_rule, k):
                setattr(cur_rule, k, v)
        if "parameters" in fields:
            cur_rule.parameters = fields["parameters"]
        errs = cur_rule.validate()
        if errs:
            raise ValueError("; ".join(errs))
        with self._conn() as con:
            con.execute(
                "UPDATE notify_rules SET name=?,target_scope=?,target=?,"
                "alert_type=?,parameters_json=?,severity=?,enabled=?,"
                "cooldown_seconds=? WHERE id=?",
                [
                    cur_rule.name,
                    cur_rule.target_scope,
                    cur_rule.target,
                    cur_rule.alert_type,
                    json.dumps(cur_rule.parameters),
                    cur_rule.severity,
                    int(cur_rule.enabled),
                    cur_rule.cooldown_seconds,
                    rule_id,
                ],
            )
        return cur_rule

    def get(self, rule_id: int) -> AlertRule | None:
        with self._conn() as con:
            r = con.execute("SELECT * FROM notify_rules WHERE id=?", [rule_id]).fetchone()
        return self._row_to_rule(r) if r else None

    def list(self, enabled_only: bool = False) -> list[AlertRule]:
        sql = "SELECT * FROM notify_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with self._conn() as con:
            rows = con.execute(sql).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def delete(self, rule_id: int) -> bool:
        with self._conn() as con:
            cur = con.execute("DELETE FROM notify_rules WHERE id=?", [rule_id])
        return cur.rowcount > 0

    def mark_triggered(self, rule_id: int, cooldown_seconds: int) -> None:
        """触发即落冷却状态 —— 重启不丢，这是 DB 状态区别于进程内降噪的全部意义。"""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        until = (now + timedelta(seconds=cooldown_seconds)).isoformat()
        with self._conn() as con:
            con.execute(
                "UPDATE notify_rules SET last_triggered_at=?, cooldown_until=? WHERE id=?",
                [now.isoformat(), until, rule_id],
            )


_store: RuleStore | None = None


def get_store() -> RuleStore:
    """进程级单例；LQ_NOTIFY_DB 变更时重建（对齐 get_cache 的测试隔离做法）。"""
    global _store
    env = os.getenv("LQ_NOTIFY_DB")
    if _store is None or (env and str(_store._path) != str(Path(env))):
        _store = RuleStore()
    return _store


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _in_cooldown(rule: AlertRule, now: datetime | None = None) -> bool:
    if not rule.cooldown_until:
        return False
    try:
        until = datetime.fromisoformat(rule.cooldown_until)
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        return until.tzinfo is not None and now <= until
    except ValueError:
        return False


def evaluate(rule: AlertRule, ctx: dict) -> tuple[str, str]:
    """评估单条规则。返回 (status, detail)；只判不记不发（DryRun 语义）。"""
    if _in_cooldown(rule):
        return COOLDOWN, f"冷却至 {rule.cooldown_until}"
    try:
        threshold = float(rule.parameters.get("threshold"))
        missing_ic = False
        if rule.alert_type in ("price_above", "price_below"):
            px = float(ctx["last_price"])
            hit = px >= threshold if rule.alert_type == "price_above" else px <= threshold
        elif rule.alert_type == "ic_below":
            raw = ctx["ic"]  # KeyError → EVAL_ERROR（缺字段不是「未触发」）
            ic = None if raw is None else float(raw)
            # IC 为 None/NaN/Inf：窗口内因子已无可用截面信息（如某日截面内
            # 退化成常数时 pl.corr 返回 NaN）。这正是 ic_below 要抓的失效，
            # 此前 `nan <= threshold` 恒为 False —— 退化的因子被静默放过。
            missing_ic = ic is None or not math.isfinite(ic)
            hit = missing_ic or ic <= threshold
        else:  # pct_change_up / pct_change_down
            px, pre = float(ctx["last_price"]), float(ctx["pre_close"])
            if pre <= 0:
                raise ValueError(f"pre_close 非法: {pre}")
            pct = (px / pre - 1) * 100
            hit = pct >= threshold if rule.alert_type == "pct_change_up" else pct <= -abs(threshold)
    except KeyError as e:
        return EVAL_ERROR, f"ctx 缺字段 {e} —— 规则评估失败不是未触发"
    except (TypeError, ValueError) as e:
        return EVAL_ERROR, f"ctx 数值非法: {e}"
    if hit:
        if missing_ic:
            return TRIGGERED, f"ic_below 命中：IC 缺失/非有限（threshold={threshold}）"
        return TRIGGERED, f"{rule.alert_type} 命中 (threshold={threshold})"
    return NOT_TRIGGERED, "未命中"


def run_rules(ctxs: list[dict], store: RuleStore | None = None, notify_fn=None) -> list[dict]:
    """评估全部启用规则；命中的发通知并落冷却状态。

    ctxs: 每个标的一行 ``{"symbol":.., "last_price":.., "pre_close":..}``；
    规则按 target 匹配 symbol（scope=market 的规则吃全部 ctx）。
    notify_fn 可注入（测试替身）；缺省用 lquant.notify.notify。
    """
    if notify_fn is None:
        from lquant.notify import notify as notify_fn_default

        notify_fn = notify_fn_default
    store = store or get_store()
    out = []
    for rule in store.list(enabled_only=True):
        matched = [
            c for c in ctxs if rule.target_scope == "market" or c.get("symbol") == rule.target
        ]
        if rule.target_scope != "market" and not matched:
            out.append(
                {"rule_id": rule.id, "status": NOT_TRIGGERED, "detail": "ctx 中无 target 标的行情"}
            )
            continue
        # 同一规则对多条行情逐一评估，任一命中即触发一次（合并 detail，不刷屏）
        results = [evaluate(rule, c) for c in matched]
        hit_idx = next((i for i, r in enumerate(results) if r[0] == TRIGGERED), None)
        err = next((r for r in results if r[0] == EVAL_ERROR), None)
        if hit_idx is not None:
            hit = results[hit_idx]
            store.mark_triggered(rule.id, rule.cooldown_seconds)
            # 必须用命中的**那一行**下标取 symbol：此前 matched[results.index(hit)]
            # 是元组相等查找，两个标的给出同样的 (status, detail) 时会指到先出现
            # 的那个 —— 通知里的标的和证据不是同一只。
            sym = matched[hit_idx].get("symbol", "")
            notify_fn(
                f"[{rule.severity}] {rule.name}",
                f"{sym} {hit[1]}",
                category="alert",
                severity=rule.severity,
            )
            out.append({"rule_id": rule.id, "status": TRIGGERED, "detail": f"{sym} {hit[1]}"})
        elif err:
            out.append({"rule_id": rule.id, "status": EVAL_ERROR, "detail": err[1]})
        else:
            cd = next((r for r in results if r[0] == COOLDOWN), None)
            out.append(
                {
                    "rule_id": rule.id,
                    "status": cd[0] if cd else NOT_TRIGGERED,
                    "detail": (cd[1] if cd else "未命中"),
                }
            )
    return out


def rule_to_dict(rule: AlertRule) -> dict:
    return asdict(rule)
