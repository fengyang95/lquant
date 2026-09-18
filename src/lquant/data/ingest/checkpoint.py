"""断点续传。

BaoStock 长任务（5000 只标的 / 逐季财务）动辄跑几小时，
中途被杀是常态。每个长任务一个 checkpoint 文件，重跑时自动跳过已完成项。

文件：data/cache/checkpoints/<name>.json

两种记账粒度（都保存在同一个文件里）：
- ``done``      : 键跑过至少一次。适合「键本身就是全部语义」的任务
  （标的清单同步：这只补过详情了）。
- ``coverage``  : 键被**实际拉取覆盖到哪个日期区间**（区间列表）。
  适合带时间窗口的增量任务 —— 键跑过 ≠ 这个窗口被覆盖过。

为什么必须区分：财务增量的窗口是「今天往回 90 天」，而 done 是**按 symbol**
记的。全市场回填把 5898 只标成 done 之后，每日作业 ``remaining`` 返回空集，
于是**永远空转**：新发财报、新上市标的都进不来，且作业状态显示 ok
（实测 financial_pit_tushare.json done=5898 / meta 无窗口）。只剩 done 时
无法回答「2026-06-20~09-18 这段公告日的数据拉过没有」，所以窗口语义必须
单独记区间。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from lquant.core.config import get_settings
from lquant.core.types import now_cn_naive


def _dir() -> Path:
    p = Path(get_settings().cache_dir) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _merge_intervals(spans: list[tuple[date, date]], lo: date, hi: date) -> list[tuple[date, date]]:
    """把新区间 [lo, hi] 并入区间列表；相邻/重叠的区间合并。

    相邻也要合并（[a, 09-14] + [09-15, b] → [a, b]）：两段拼接后中间没有
    空洞，把它们留成两段只是浪费，判定时要逐个试反而更容易写错。
    绝不跨越空洞合并 —— 那会凭空宣称一段没拉过的区间已覆盖。
    """
    merged: list[list[date]] = []
    for a, b in sorted([*spans, (lo, hi)]):
        if merged and a <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


class Checkpoint:
    """记录"已完成"的键集合，原子写入（写临时文件再 rename）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.path = _dir() / f"{name}.json"
        self._data: dict = {"done": [], "updated_at": None, "meta": {}, "coverage": {}}
        if self.path.exists():
            # 坏文件直接当空，不阻塞任务
            with contextlib.suppress(json.JSONDecodeError):
                self._data = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def done(self) -> set[str]:
        return set(self._data.get("done", []))

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark(self, keys: list[str] | set[str]) -> None:
        d = self.done | set(keys)
        self._data["done"] = sorted(d)
        self._flush()

    def unmark(self, retry_keys: list[str] | set[str]) -> None:
        """从 done 集合移除（retry 场景），下次重跑自动重试这些键。

        覆盖区间一并清掉：留着区间但清掉 done 会让「跑过」与「覆盖到哪」
        两个口径不一致，调用方按覆盖区间判定时仍会误跳。
        """
        drop = set(retry_keys)
        self._data["done"] = sorted(self.done - drop)
        cov = self._data.get("coverage") or {}
        for k in drop:
            cov.pop(k, None)
        self._data["coverage"] = cov
        self._flush()

    def remaining(self, keys: list[str], *, start: date | None = None,
                  end: date | None = None) -> list[str]:
        """未完成的键。

        不给 start/end 时用 ``done``（历史语义，零改动）。给了窗口则用
        ``coverage`` 判定：只有存在一个**完整包含** [start, end] 的已覆盖
        区间才算完成 —— 没有区间记录（老 checkpoint）一律视为未覆盖，
        宁可按窗口重拉一遍，也不留静默缺口。
        """
        if start is None and end is None:
            done = self.done
            return [k for k in keys if k not in done]
        if start is None or end is None:
            raise ValueError("start/end 必须同时给或同时不给")
        return [k for k in keys if not self.covers(k, start, end)]

    def record_coverage(self, keys, start: date, end: date) -> None:
        """记录这批键已实际拉取覆盖 [start, end]（闭区间），并计入 done。"""
        if end < start:
            return
        cov = self._data.setdefault("coverage", {})
        for k in keys:
            spans = self._spans(k)
            cov[k] = [[a.isoformat(), b.isoformat()]
                      for a, b in _merge_intervals(spans, start, end)]
        self._data["done"] = sorted(self.done | set(keys))
        self._flush()

    def _spans(self, key: str) -> list[tuple[date, date]]:
        raw = (self._data.get("coverage") or {}).get(key) or []
        out: list[tuple[date, date]] = []
        for pair in raw:
            try:
                out.append((date.fromisoformat(pair[0]), date.fromisoformat(pair[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def covers(self, key: str, start: date, end: date) -> bool:
        """是否存在一个已覆盖区间完整包含 [start, end]。"""
        return any(a <= start and b >= end for a, b in self._spans(key))

    def covered_window(self, key: str) -> tuple[date, date] | None:
        """该键覆盖区间的并集外框（用于分组增量拉取/展示）；无记录返回 None。"""
        spans = self._spans(key)
        if not spans:
            return None
        return min(a for a, _ in spans), max(b for _, b in spans)

    def set_meta(self, **kw) -> None:
        self._data.setdefault("meta", {}).update(kw)
        self._flush()

    @property
    def meta(self) -> dict:
        return self._data.get("meta", {})

    def clear(self) -> None:
        self._data = {"done": [], "updated_at": None, "meta": {}, "coverage": {}}
        self._flush()

    def _flush(self) -> None:
        self._data["updated_at"] = now_cn_naive().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def __len__(self) -> int:
        return len(self.done)
