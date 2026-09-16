"""生成 launchd 定时同步 plist（deploy/launchd/com.lquant.sync.plist）。

为什么用脚本生成：plist 有 15 个 StartCalendarInterval 条目（工作日 × 3 时点），
手写 XML 易错；且用 plistlib 落盘天然保证可被 launchd 解析。

触发点设计：09:15 盘前补齐（backfill 09:10 已过）、15:10 收盘采集（close 15:05
已过）、19:20 盘后窗口（evening 18:00 / daily 18:30 / daily_basic 18:45 /
financial 19:15 都已过）。节假日由 manager._trading_day_ok 过滤，plist 只按
工作日触发。
"""
from __future__ import annotations

import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLIST = REPO / "deploy" / "launchd" / "com.lquant.sync.plist"

# (Hour, Minute) 触发点：见模块 docstring
FIRE_TIMES = ((9, 15), (15, 10), (19, 20))
WEEKDAYS = (1, 2, 3, 4, 5)


def build_config(repo_dir: str = "/Users/lyp/code/lquant") -> dict:
    return {
        "Label": "com.lquant.sync",
        "Comment": ("lquant 大盘/日线每日同步：工作日触发 lq sync tick。"
                    "到期作业由 sync_job 调度语义决定（跨天补跑、交易日过滤在 manager 内），"
                    "不依赖服务器进程在线。"),
        "ProgramArguments": [f"{repo_dir}/.venv/bin/lq", "sync", "tick"],
        "WorkingDirectory": repo_dir,
        "StartCalendarInterval": [
            {"Weekday": wd, "Hour": h, "Minute": m}
            for wd in WEEKDAYS for h, m in FIRE_TIMES
        ],
        "StandardOutPath": f"{repo_dir}/data/logs/sync-launchd.log",
        "StandardErrorPath": f"{repo_dir}/data/logs/sync-launchd.log",
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
    }


def main() -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump(build_config(), f, sort_keys=False)
    print(f"written {PLIST}")


if __name__ == "__main__":
    main()
