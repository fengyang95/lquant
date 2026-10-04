#!/usr/bin/env python
"""基准口径对拍：lquant `index_daily` ↔ qlib 导出的 `<BENCH>/$close.day.bin`。

Phase 1.3 把 qlib 工作流的基准从 `SH600000` 机械代理换成真实指数
（默认 `SH000300`）。换了以后必须回答一个问题：**两侧算超额收益用的
基准序列是不是同一条？** 只要基准一致，剩下的差异就纯粹来自组合构建与
交易成本，而不是「基准口径漂移」。

本脚本做三件事（全部只读，不写库）：

1. 从 qlib 导出目录读 `calendars/day.txt` 与 `features/<BENCH>/close.day.bin`；
2. 从 DuckDB `index_daily` 读同一指数的收盘序列；
3. 在**共同交易日**上比较：日期集合、收盘价、以及由收盘价推出的日收益。

退出码：0 = 一致（max|Δ| ≤ 容差）；1 = 不一致（打印差异明细）。

用法（主 venv 即可，不需要 pyqlib）：

    python scripts/xval/qlib/benchmark_parity.py \
        --qlib-dir data/qlib-xval --symbol 000300.SH \
        [--repo-root /path/to/lquant] [--tol 1e-6]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]


def _qlib_symbol(symbol: str) -> str:
    code, _, sfx = symbol.partition(".")
    return f"{sfx.upper()}{code}"


def _read_calendar(qlib_dir: Path) -> list[date]:
    p = qlib_dir / "calendars" / "day.txt"
    if not p.exists():
        raise SystemExit(f"{p} 不存在（先跑 `lq qlib export --out {qlib_dir}`）")
    return [date.fromisoformat(x) for x in p.read_text().split()]


def _read_bin(qlib_dir: Path, qsym: str, field: str = "close") -> tuple[int, np.ndarray]:
    p = qlib_dir / "features" / qsym / f"{field}.day.bin"
    if not p.exists():
        raise SystemExit(f"{p} 不存在（导出时未包含基准？检查 qlib_export_meta.json）")
    arr = np.fromfile(p, dtype="<f4")
    return int(arr[0]), arr[1:]


def _native_series(symbol: str, repo_root: Path) -> dict[date, float]:
    """从 DuckDB index_daily 读原生基准序列（与引擎用的是同一个函数）。"""
    sys.path.insert(0, str(repo_root / "src"))
    from lquant.backtest.benchmark import load_index_series

    series = load_index_series(symbol)
    if not series:
        raise SystemExit(f"index_daily 无 {symbol} 数据（跑 `lq data index`）")
    return dict(series)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="基准口径对拍（lquant index_daily ↔ qlib bin）")
    ap.add_argument("--qlib-dir", default="data/qlib-xval")
    ap.add_argument("--symbol", default="000300.SH", help="lquant 形态指数代码")
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="日收益的绝对容差（收益是小数，1e-6 已是 float32 极限）")
    ap.add_argument("--rtol", type=float, default=1e-4,
                    help="收盘价位的相对容差（bin 是 float32，指数点位 ~4500 时"
                         "绝对误差必然在 1e-3 量级，只能用相对误差判）")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args(argv)

    qlib_dir = Path(args.qlib_dir).resolve()
    repo_root = Path(args.repo_root).resolve()
    qsym = _qlib_symbol(args.symbol)

    cal = _read_calendar(qlib_dir)
    start_i, close = _read_bin(qlib_dir, qsym)
    # bin 是「日历下标 + 值」：第 i 个值对应 cal[start_i + i]
    qlib_series: dict[date, float] = {}
    for i, v in enumerate(close):
        idx = start_i + i
        if idx >= len(cal):
            break
        if np.isfinite(v) and v > 0:
            qlib_series[cal[idx]] = float(v)

    native = _native_series(args.symbol, repo_root)

    common = sorted(set(qlib_series) & set(native))
    only_qlib = sorted(set(qlib_series) - set(native))
    only_native = sorted(set(native) - set(qlib_series))

    report: dict = {
        "qlib_dir": str(qlib_dir),
        "symbol": args.symbol,
        "qlib_symbol": qsym,
        "calendar_days": len(cal),
        "qlib_days": len(qlib_series),
        "native_days": len(native),
        "common_days": len(common),
        "only_in_qlib": [str(d) for d in only_qlib][:10],
        "only_in_native": [str(d) for d in only_native][:10],
        "n_only_qlib": len(only_qlib),
        "n_only_native": len(only_native),
        "close_max_abs_diff": None,
        "ret_max_abs_diff": None,
        "passed": False,
    }

    if len(common) < 2:
        report["reason"] = "共同交易日不足 2 天，无法对拍"
        _emit(report, args.out)
        return 1

    qc = np.array([qlib_series[d] for d in common], dtype=float)
    nc = np.array([native[d] for d in common], dtype=float)
    close_diff = float(np.max(np.abs(qc - nc)))
    close_rel_diff = float(np.max(np.abs(qc - nc) / np.maximum(np.abs(nc), 1e-9)))
    # 日收益：两侧用同一套「相邻共同日」口径，避免把缺口算进收益
    qr = qc[1:] / qc[:-1] - 1.0
    nr = nc[1:] / nc[:-1] - 1.0
    ret_diff = float(np.max(np.abs(qr - nr)))

    report["close_max_abs_diff"] = close_diff
    report["close_max_rel_diff"] = close_rel_diff
    report["ret_max_abs_diff"] = ret_diff
    report["from"] = str(common[0])
    report["to"] = str(common[-1])
    # 基准腿的区间总收益：两侧算超额收益时共用的那一项。
    # 它一致 ⇒ 超额收益的差异只可能来自组合腿，不可能来自基准口径。
    wr_q = float(np.prod(qc[1:] / qc[:-1]) - 1.0)
    wr_n = float(np.prod(nc[1:] / nc[:-1]) - 1.0)
    report["benchmark_window_return_qlib"] = wr_q
    report["benchmark_window_return_native"] = wr_n
    report["benchmark_window_return_abs_diff"] = abs(wr_q - wr_n)
    # bin 是 float32：点位只能按相对误差判，收益按绝对误差判。
    report["passed"] = bool(close_rel_diff <= args.rtol and ret_diff <= args.tol)

    _emit(report, args.out)
    if report["passed"]:
        print(f"\n✓ 基准一致：{len(common)} 个共同交易日，"
              f"close max|Δ|={close_diff:.3e}（相对 {close_rel_diff:.2e}），"
              f"ret max|Δ|={ret_diff:.3e}")
        return 0
    print(f"\n✗ 基准不一致：close 相对 max|Δ|={close_rel_diff:.6g}，"
          f"ret max|Δ|={ret_diff:.6g}")
    return 1


def _emit(report: dict, out: str | None) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
