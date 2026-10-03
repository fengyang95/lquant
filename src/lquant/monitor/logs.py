"""监控页运行日志：tail logs/lquant.log + 级别过滤 + 关键字搜索。

只读固定路径（log_file_path()），不接受任意路径 —— 避免目录穿越。
多行记录（traceback）合并到首行记录的 message。
"""
from __future__ import annotations

import re
from pathlib import Path

from lquant.core.logging import log_file_path

__all__ = ["tail_app_logs"]

_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK = {name: i for i, name in enumerate(_LEVELS)}

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| (\w+) {1,7}\| "
    r"(.{0,12}?)\s*\| (.+?) - (.*)$")

# tail 窗口：2MB ≈ 8 万条日志，远超 limit 上限 1000 条所需
_TAIL_BYTES = 2_000_000


def _parse_and_filter(path: Path, min_rank: int, needle: str) -> list[dict]:
    """解析日志文件，返回按时间升序的记录（多行合并进 message）。

    只读尾部 _TAIL_BYTES（文件 sink 轮转 50MB，全量读浪费 90%+ 工作）；
    丢掉首个可能被截断的半行。文件小于窗口时等同全量。
    """
    records: list[dict] = []
    cur: dict | None = None
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
                f.readline()  # 丢弃截断的半行
            text = f.read(_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            if cur:
                records.append(cur)
            ts, level, run_id, src, msg = m.groups()
            level = level.upper()
            run_id = (run_id or "").strip() or None
            cur = {
                "ts": ts, "level": level, "run_id": run_id,
                "source": src, "first": msg, "extra": [],
                "level_ok": _LEVEL_RANK.get(level, 99) >= min_rank,
            }
        elif cur is not None:
            cur["extra"].append(line)
    if cur is not None:
        records.append(cur)
    for r in records:
        first, extra = r.pop("first"), r.pop("extra")
        r["message"] = first + ("\n" + "\n".join(extra) if extra else "")
    return records


def tail_app_logs(level: str | None = None, q: str = "",
                  limit: int | None = None) -> dict:
    """tail 应用日志：倒序返回记录（先全量解析再倒序截断）。

    level 语义是「该级别及以上」（WARNING 会同时含 ERROR/CRITICAL）；
    level 缺省/非法按 DEBUG 处理 = 文件里全部记录（「全部」按钮的真实语义）。
    limit=None 返回窗口内**全部**匹配记录（面板「全部」语义，不再截 200）；
    给定 limit 时只取最近 limit 条。实际范围受 _TAIL_BYTES 读窗约束。
    文件缺失/不可读返回空集（首次运行为常态）。
    """
    rank = _LEVEL_RANK.get((level or "DEBUG").upper(), _LEVEL_RANK["DEBUG"])
    recs = _parse_and_filter(log_file_path(), rank, q or "")
    needle = (q or "").lower()
    matched = [r for r in recs
               if r["level_ok"] and (not needle or needle in r["message"].lower())]
    for r in matched:
        del r["level_ok"]
    total = len(matched)
    if limit is not None and limit > 0:
        matched = matched[-limit:]
    return {"items": matched[::-1], "total": total}
