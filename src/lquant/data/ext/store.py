"""扩展表配置的持久化与路径解析。

落点选择：``<数据根>/ext/<table_id>/config.json``，与数据 parquet 同级。

为什么不用 DuckDB 表存配置：扩展表是「数据 + 配置 + 拉取状态」三位一体，
配置必须跟数据一起可搬迁、可在 DuckDB 文件损坏时靠 parquet 原地恢复。
DuckDB 侧的集成走 **视图**（``ext_<id>``，见 duckdb.py）与 **因子注册**
（``factor_def`` 表，见 factors/ext_bridge.py）—— 配置本身保持文件形态，
与 lquant ``config/*.yaml`` 「配置即代码」的既有习惯一致。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger

from lquant.data.ext.models import ExtConfig

#: 配置目录签名缓存。读配置是热路径（每次查询/因子注册都要枚举），
#: 每次 iterdir + 逐文件 read/parse 纯属重复；用目录签名失效。
#: 新增/编辑/删除配置都会改变目录名或 mtime/size 签名。
_LOAD_ALL_CACHE: dict[str, tuple[tuple, list[ExtConfig]]] = {}


def resolve_data_root(data_root: str | Path | None = None) -> Path:
    """数据根：显式传入优先，否则取 ``config/app.yaml`` 的 ``paths.data_dir``。

    显式参数是给测试与工具用的 —— 单测必须能把数据落到 tmp，绝不能因为
    ``get_settings()`` 缓存而写进真实数据湖（同一形状的坑在 TSP 里也踩过）。
    """
    if data_root is not None:
        return Path(data_root)
    from lquant.core.config import get_settings

    s = get_settings()
    raw_dir = (s.raw.get("paths") or {}).get("data_dir")
    if raw_dir:
        return Path(raw_dir)
    # 没配 data_dir 时退到 parquet 的父目录（paths.parquet 默认 ./data/parquet）
    return Path(s.parquet_dir).parent


def ext_root(data_root: str | Path | None = None) -> Path:
    """扩展数据区域：``<数据根>/ext/``（独立于官方数据集的 parquet 树）。"""
    return resolve_data_root(data_root) / "ext"


def table_dir(table_id: str, data_root: str | Path | None = None) -> Path:
    """一张扩展表的目录（会做 id 白名单校验，防路径穿越）。"""
    from lquant.data.ext.models import _require_identifier

    tid = _require_identifier(table_id, what="表 id")
    return ext_root(data_root) / tid


def _dir_signature(base: Path) -> tuple | None:
    """``base`` 下所有 config.json 的 (目录名, mtime_ns, size) 签名。

    出错返回 None = 禁用缓存（fail-open：缓存失败不该让配置读不出来）。
    """
    try:
        sig = []
        for d in sorted(base.iterdir()):
            cfg = d / "config.json"
            if d.is_dir() and cfg.exists():
                st = cfg.stat()
                sig.append((d.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)
    except OSError:
        return None


class ExtConfigStore:
    """扩展表配置的读写（一个表一个目录）。"""

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = resolve_data_root(data_root)
        self.base = self.data_root / "ext"

    # ------------------------------------------------------------------ 读
    def load_all(self) -> list[ExtConfig]:
        """列出全部配置。

        单份配置解析失败只记日志跳过，不让整张列表 500 —— 一份手改坏的
        config.json 不该让其它扩展表全部不可用。
        """
        sig = _dir_signature(self.base)
        if sig is not None:
            cached = _LOAD_ALL_CACHE.get(str(self.base))
            if cached is not None and cached[0] == sig:
                return copy.deepcopy(cached[1])
        if not self.base.exists():
            return []
        out: list[ExtConfig] = []
        for d in sorted(self.base.iterdir()):
            cfg = d / "config.json"
            if not (d.is_dir() and cfg.exists()):
                continue
            try:
                out.append(ExtConfig.from_dict(json.loads(cfg.read_text(encoding="utf-8"))))
            except Exception as e:  # noqa: BLE001 - 单表损坏不拖垮列表
                logger.warning(f"扩展表配置解析失败 {cfg}: {e}")
        if sig is not None and out:
            # 缓存私有副本，命中时返回深拷贝：调用方改配置对象不会污染缓存。
            _LOAD_ALL_CACHE[str(self.base)] = (sig, copy.deepcopy(out))
        return out

    def get(self, table_id: str) -> ExtConfig | None:
        try:
            path = table_dir(table_id, self.data_root) / "config.json"
        except Exception:  # noqa: BLE001 - 非法 id 视为不存在（调用方给 404）
            return None
        if not path.exists():
            return None
        try:
            return ExtConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            # 已存在但解析失败：与「不存在」语义不同，必须报错而不是静默返回 None
            raise ValueError(f"扩展表 {table_id} 配置损坏: {e}") from e

    def ids(self) -> list[str]:
        return [c.id for c in self.load_all()]

    # ------------------------------------------------------------------ 写
    def save(self, config: ExtConfig) -> ExtConfig:
        """新增/覆盖配置（原子写 + 失效派生缓存）。"""
        config.updated_at = datetime.now().isoformat()
        if not config.created_at:
            config.created_at = config.updated_at
        path = table_dir(config.id, self.data_root) / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        invalidate_ext_caches(self.data_root)
        return config

    def delete(self, table_id: str) -> bool:
        """删除整张表（配置 + 数据）。"""
        try:
            d = table_dir(table_id, self.data_root)
        except Exception:  # noqa: BLE001
            return False
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        invalidate_ext_caches(self.data_root)
        return True


def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace：读方不会看到半截 JSON。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def invalidate_ext_caches(data_root: str | Path | None = None) -> None:
    """扩展数据/配置变更后的统一失效入口（写入路径自动调用）。

    清两处：
    1) 本模块的配置清单缓存；
    2) 因子/帧缓存（``factors/ext_bridge``，惰性导入避免模块级循环依赖 ——
       bridge 反过来要读本模块的配置）。
    """
    root = str(resolve_data_root(data_root))
    _LOAD_ALL_CACHE.pop(str(Path(root) / "ext"), None)
    from lquant.data.ext.pit import invalidate_frame_cache

    invalidate_frame_cache(root)
    try:
        from lquant.factors.ext_bridge import invalidate_ext_caches as _bridge_invalidate

        _bridge_invalidate(root)
    except Exception as e:  # noqa: BLE001 - 失效失败不应阻断写入本身
        logger.warning(f"扩展数据导出的因子缓存失效失败: {e}")
