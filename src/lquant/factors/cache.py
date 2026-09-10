"""因子两级缓存：内存 LRU + 磁盘 parquet（因子域 compute_many 的缓存层）。

层级：
  mem  : {key: DataFrame}，上限 max_mem_entries；跨批次热计算省重复 collect
  disk : {cache_dir}/factors/<key>.parquet（zstd），重启不丢

key 由「因子 defs 集 + 窗口 + data_version」哈希而成。data_version 随每日同步
推进，key 自动失效 —— 缓存永远锚在数据版本上，不存在「数据变了还用旧缓存」。

粒度是整组因子（compute_many 一次算一组）。逐因子缓存留给更细的局部失效场景，
不在本模块范围内。

纯 IO + 字典：不依赖网络，可离线单测。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import polars as pl
from loguru import logger

from lquant.core.config import get_settings


def _fmt(d: date) -> str:
    return d.isoformat()


class TwoTierCache:
    """两级因子缓存。线程不保证并发写（调度器单线程跑计算）。"""

    def __init__(self, max_mem_entries: int = 64) -> None:
        self._mem: dict[str, pl.DataFrame] = {}
        self._max = max_mem_entries

    def _root(self) -> Path:
        # 显式 env 覆盖（测试隔离）；否则用 config 的 cache_dir
        override = os.getenv("LQUANT_CACHE_DIR")
        base = Path(override) if override else Path(get_settings().cache_dir)
        p = base / "factors"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def key(defs: list[dict], start: date, end: date, version: str,
            steps: list[dict] | None = None) -> str:
        """稳定 key：defs 排序签名 + steps 有序序列化（流水线顺序有语义）。

        steps 是预处理配方，改配方必须换 key —— 否则「改了参数没反应」（缺陷 #12）。
        """
        ordered = sorted(defs, key=lambda d: (d["name"],
                                              d.get("expression", d.get("formula", ""))))
        sig = json.dumps(
            [{"name": d["name"], "expr": d.get("expression", d.get("formula", ""))}
             for d in ordered],
            sort_keys=True, ensure_ascii=False,
        )
        steps_sig = (json.dumps(steps, sort_keys=True, ensure_ascii=False)
                     if steps else "")
        raw = f"{sig}|{steps_sig}|{_fmt(start)}|{_fmt(end)}|{version}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def get(self, key: str) -> pl.DataFrame | None:
        """命中内存直接返回；否则查磁盘并回填内存。"""
        if key in self._mem:
            return self._mem[key]
        p = self._root() / f"{key}.parquet"
        if p.exists():
            try:
                df = pl.read_parquet(p)
                self._mem[key] = df
                logger.debug(f"factor cache disk hit: {key}")
                return df
            except Exception as e:  # noqa: BLE001  缓存坏了当 miss，重算覆盖
                logger.warning(f"factor cache disk read failed {p}: {e}")
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
        return None

    def set(self, key: str, df: pl.DataFrame) -> None:
        """写内存 + 磁盘。内存超限按插入序淘汰最旧（简单 FIFO）。"""
        self._mem[key] = df
        if len(self._mem) > self._max:
            oldest = next(iter(self._mem))
            self._mem.pop(oldest, None)
        try:
            self._root().joinpath(f"{key}.parquet").write_parquet(
                df, compression="zstd")
        except Exception as e:  # noqa: BLE001  缓存写失败不该中断计算
            logger.warning(f"factor cache disk write failed {key}: {e}")

    def clear(self) -> None:
        """清内存缓存（磁盘不动 —— 磁盘层是共享的持久缓存）。"""
        self._mem.clear()


_cache: TwoTierCache | None = None
_cache_root: str | None = None


def get_cache() -> TwoTierCache:
    """进程级单例：不同模块共享同一内存层，省的反复重建。

    LQUANT_CACHE_DIR 变更（测试隔离）时重建 —— 否则上个测试环境的内存
    条目会串进下个测试（缓存命中断言随机翻车）。
    """
    global _cache, _cache_root
    override = os.getenv("LQUANT_CACHE_DIR")
    if _cache is None or override != _cache_root:
        _cache = TwoTierCache()
        _cache_root = override
    return _cache