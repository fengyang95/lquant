"""最小 .env 加载器（零依赖，不覆盖已有环境变量）。

查找顺序（先到先得，先设置的 key 优先）：
1. CWD/.env        —— 运行上下文（worktree）级配置
2. <root>/.env     —— 仓库根
3. <package>/.env  —— src/lquant/.env（与代码同目录，用户 token 常放这里）

安全约定：.env 一律 gitignore（.gitignore 的裸 `.env` 模式匹配任意层级，
src/lquant/.env 已验证被忽略）；绝不把读到的值写日志。
"""
from __future__ import annotations

import os
from pathlib import Path

_LOADED: set[str] = set()


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if line.startswith("export "):  # bash 风格 export 前缀
        line = line[len("export "):].strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip("'\"")
    if not key or not value:
        return None
    return key, value


def load_env_files(*candidates: Path | str) -> list[str]:
    """按顺序解析候选 .env 文件；os.environ 已有的 key 不覆盖。

    LQ_ENV_SKIP=1 时整体跳过（单测隔离用，防止读到开发者本地 .env）。
    返回实际加载了键值对的文件路径列表（调试用；值不外泄）。
    """
    if os.environ.get("LQ_ENV_SKIP"):
        return []
    loaded: list[str] = []
    for cand in candidates:
        p = Path(cand).expanduser()
        if not p.is_file():
            continue
        keys = str(p.resolve())
        if keys in _LOADED:
            continue  # 同一文件一次进程只解析一遍
        count = 0
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            kv = _parse_line(line)
            if kv is None:
                continue
            key, value = kv
            if key in os.environ:
                continue
            os.environ[key] = value
            count += 1
        _LOADED.add(keys)
        if count:
            loaded.append(keys)
    return loaded


def discover_env_files() -> list[Path]:
    """默认候选路径：CWD → 仓库根 → 包目录。"""
    out = [Path.cwd() / ".env"]
    try:
        from lquant.core.config import find_root  # noqa: PLC0415 延迟导入防环

        out.append(find_root() / ".env")
    except Exception:  # noqa: BLE001 - root 探测失败不阻塞
        pass
    out.append(Path(__file__).resolve().parent.parent / ".env")
    return out


def load_env() -> list[str]:
    """按默认顺序加载 .env；供 CLI / build_chain 入口调用。"""
    return load_env_files(*discover_env_files())


__all__ = ["load_env", "load_env_files", "discover_env_files"]
