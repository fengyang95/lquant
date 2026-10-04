#!/usr/bin/env bash
# 交叉验证哨兵（计划 Phase 4.6）：把两套独立对拍串起来，任何一项超差即非零退出。
#
# 为什么不做成 CI job：两套对拍都需要**真实数据湖**（7000+ 标的的 parquet）
# 与两个隔离 venv（`.venv-qlib` / `.venv-alphapurify`），CI runner 上都没有 ——
# 硬塞进去只会得到一个永远 skip 的绿灯，比没有更糟。它的定位是
# 「定期在开发机/预发布环境跑一次的回归哨兵」：
#
#     bash scripts/xval/sentinel.sh          # 或 make xval-sentinel
#
# 两套覆盖的能力（互补，不重叠）：
#   1. alphapurify 对拍 → 因子评价内核（IC/RankIC 逐日序列）与预处理方法口径
#      包括上游自身 bug 的「是否仍存在」看门狗（修复了会提醒你改回 match）
#   2. qlib 对拍       → 基准口径（原生 index_daily ↔ qlib bin）一致，
#      并用隔离 venv 真跑一遍 workflow（证明导出物能被 qlib 真正消费）
set -uo pipefail          # 刻意不用 -e：每一项都要跑完再汇总

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(cd "$HERE/../.." && pwd)"
MAIN_REPO="${MAIN_REPO:-/Users/lyp/code/lquant}"

# alphapurify 侧
AP_START="${AP_START:-2023-01-01}"
AP_END="${AP_END:-2024-12-31}"
AP_SYMBOLS="${AP_SYMBOLS:-400}"
AP_OUT="${AP_OUT:-$WORKTREE/.xval-out}"

# qlib 侧（基准窗口受 index_daily 覆盖限制，默认值见 qlib/README.md）
QLIB_START="${QLIB_START:-2026-01-01}"
QLIB_END="${QLIB_END:-2026-09-30}"
QLIB_DIR="${QLIB_DIR:-$WORKTREE/data/qlib-xval}"
QLIB_PY="${QLIB_PY:-$MAIN_REPO/.venv-qlib/bin/python}"

echo "================================================================"
echo "交叉验证哨兵"
echo "  被测代码: $WORKTREE/src"
echo "  数据湖  : $MAIN_REPO/data"
echo "  AP 窗口 : $AP_START → $AP_END（$AP_SYMBOLS 只）"
echo "  qlib 窗口: $QLIB_START → $QLIB_END"
echo "================================================================"

RC_AP=0
RC_QLIB=0
RC_WF=0
SKIPPED=""

# ---------------- 1. AlphaPurify 对拍 ----------------
echo
echo "########## [1/3] AlphaPurify × lquant 对拍 ##########"
if [[ -x "$WORKTREE/.venv-alphapurify/bin/python" ]]; then
  OUT="$AP_OUT" CHECK=1 \
    bash "$HERE/alphapurify/run_xval.sh" "$AP_START" "$AP_END" "$AP_SYMBOLS" || RC_AP=1
else
  echo "跳过：缺 $WORKTREE/.venv-alphapurify（uv venv + uv pip install alphapurify）" >&2
  SKIPPED="$SKIPPED alphapurify-venv"
fi

# ---------------- 2. qlib 基准口径对拍 ----------------
echo
echo "########## [2/3] qlib 基准口径对拍 ##########"
bash "$HERE/qlib/run_xval.sh" "$MAIN_REPO" "$QLIB_DIR" "$QLIB_START" "$QLIB_END" || RC_QLIB=1

# ---------------- 3. qlib workflow 真跑（回归哨兵） ----------------
echo
echo "########## [3/3] qlib workflow 真跑（隔离 venv） ##########"
if [[ -x "$QLIB_PY" ]]; then
  METRICS="$QLIB_DIR/last_metrics.json"
  ( cd "$MAIN_REPO" && PYTHONPATH="$WORKTREE/src" "$QLIB_PY" "$WORKTREE/src/lquant/qlib_io/runner.py" \
      --provider "$QLIB_DIR" \
      --config "$HERE/qlib/workflow_xval_smoke.yaml" \
      --out "$METRICS" ) || RC_WF=1
  if [[ -f "$METRICS" ]]; then
    # 断言口径而不是断言收益数字：收益会随窗口漂移，但「基准被找到且覆盖整个
    # 回测窗口」（ffr=1.0）与「超额收益算得出来」是导出链路的硬约束。
    # 指标名带 "<period>." 前缀（1day.xxx），所以按后缀匹配而不是写死全名 ——
    # 换个频率（如 5min）就不该让哨兵误报缺失。
    "$MAIN_REPO/.venv/bin/python" - "$METRICS" <<'PY' || RC_WF=1
import json, math, sys

m = json.load(open(sys.argv[1], encoding="utf-8"))


def find(suffix):
    hits = {k: v for k, v in m.items() if k == suffix or k.endswith("." + suffix)}
    if len(hits) != 1:
        raise SystemExit(f"[sentinel] 失败：指标 {suffix} 命中 {sorted(hits)}，预期恰好 1 个")
    return next(iter(hits.items()))


k_ex, excess = find("excess_return_with_cost.mean")
k_ffr, ffr = find("ffr")
print(f"[sentinel] {k_ex} = {excess}; {k_ffr} = {ffr}")
if excess is None or not math.isfinite(float(excess)):
    print("[sentinel] 失败：超额收益非有限 —— 基准链路断了", file=sys.stderr)
    raise SystemExit(1)
if ffr is None or float(ffr) < 1.0:
    print(f"[sentinel] 失败：ffr={ffr} < 1.0 —— 基准序列没有覆盖整个回测窗口", file=sys.stderr)
    raise SystemExit(1)
print("[sentinel] qlib workflow 断言通过")
PY
  else
    echo "[sentinel] 失败：没有产出 $METRICS" >&2
    RC_WF=1
  fi
else
  echo "跳过：缺 $QLIB_PY（.venv-qlib 未建）" >&2
  SKIPPED="$SKIPPED qlib-venv"
fi

# ---------------- 汇总 ----------------
echo
echo "================================================================"
echo "哨兵汇总"
[[ "$RC_AP" == "0" ]]   && echo "  [OK]   alphapurify 对拍" || echo "  [FAIL] alphapurify 对拍"
[[ "$RC_QLIB" == "0" ]] && echo "  [OK]   qlib 基准口径对拍" || echo "  [FAIL] qlib 基准口径对拍"
[[ "$RC_WF" == "0" ]]   && echo "  [OK]   qlib workflow 真跑" || echo "  [FAIL] qlib workflow 真跑"
[[ -n "$SKIPPED" ]] && echo "  [SKIP] 缺少环境:$SKIPPED"
echo "================================================================"

if [[ "$RC_AP$RC_QLIB$RC_WF" != "000" ]]; then
  exit 1
fi
exit 0
