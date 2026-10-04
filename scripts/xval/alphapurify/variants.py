"""AlphaPurify × lquant 交叉验证的**唯一真源**：跑哪些因子、比哪些方法。

为什么单独一个文件：三个脚本（`lquant_side.py` / `alphapurify_side.py` /
`compare.py`）必须对同一份清单达成一致。任一侧自持 `if/elif`，迟早出现
「lquant 跑了一个方法、AP 侧漏掉」这种**静默的覆盖缺口** ——
AlphaPurify 自己的注册表就是活反例（`neutralize("random_forest")` 报
`NotImplementedError`，能用的名字 `randomforest` 反而不在注册表里）。

这里**只有数据**：不 import lquant，也不 import alphapurify，
所以三个隔离环境都能 import 它，仍然满足「两侧互不 import」的隔离原则。

每个变体：
- ``key``    : 列名（两侧产物用同一个 key，对比时 1:1 对齐，不再交叉配对）
- ``lq``     : lquant `pipeline_run` 的 step 列表
- ``ap``     : `[(AlphaPurifier 方法名, 位置参数元组), ...]`，按顺序链式调用。
               AP 的链式签名是 `(method, *args)`，**不接受关键字参数**
               （examples 里的 keyword 写法直接 TypeError）。
- ``expect`` : ``match``（必须一致）/ ``diverge``（已知且已解释的口径差）/
               ``upstream_broken``（上游实现不可用，只验证「它仍然是坏的」）
- ``tol``    : ``match`` 的绝对误差上限
- ``note``   : 差异原因；非 ``match`` 必填
- ``ap_bug`` : ``upstream_broken`` 时被上游写坏的列名（用于验证 bug 仍存在）
"""
from __future__ import annotations

from typing import Any

#: 与 `lquant.factors.preprocess.winsorize::MAD_K` 一致
MAD_K = 1.4826

#: (因子名, lquant DSL 表达式)。四类因子的用途：
#: - mom20/mom5：原有的动量因子（延续历史结论）
#: - ma20_ratio：强自相关的水平类因子，滚动族口径差异最容易在它身上显形
#: - std20_ratio：波动类因子，右偏重尾 → 幂变换（boxcox/yeo_johnson）的靶子
FACTORS: tuple[tuple[str, str], ...] = (
    ("mom20", "Ts_Return($close, 20)"),
    ("mom5", "Ts_Return($close, 5)"),
    ("ma20_ratio", "Ts_Mean($close, 20) / $close"),
    ("std20_ratio", "Ts_Std($close, 20) / $close"),
)

_W20 = 20


def _v(key: str, label: str, lq: list[dict], ap: list[tuple[str, tuple]],
       expect: str = "match", tol: float = 1e-9, note: str = "",
       ap_bug: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "lq": lq, "ap": ap,
            "expect": expect, "tol": tol, "note": note, "ap_bug": ap_bug}


PREP_VARIANTS: tuple[dict[str, Any], ...] = (
    _v("winsor_mad_n3",
       "AP 出厂默认口径：med ± 3×MAD",
       [{"op": "winsorize", "method": "mad", "n": 3.0 / MAD_K},
        {"op": "standardize", "method": "zscore"}],
       [("winsorize", ("mad", 3.0)), ("standardize", ("zscore",))],
       note="lquant 的 n 是等效 σ 倍数（内部乘 1.4826），AP 的 n 是 MAD 倍数。"
            "两侧都用 n=3×MAD，所以必须逐值一致。"),

    _v("winsor_mad_n5_sigma",
       "lquant 出厂默认口径：med ± 5×1.4826×MAD",
       [{"op": "winsorize", "method": "mad", "n": 5.0},
        {"op": "standardize", "method": "zscore"}],
       [("winsorize", ("mad", 5.0 * MAD_K)), ("standardize", ("zscore",))],
       note="把 lquant 默认（n=5，等效 5σ）折算成 AP 的 MAD 倍数 5×1.4826。"),

    _v("rolling_zscore_w20",
       "滚动 Z-Score（按标的，窗口 20，含当日）",
       [{"op": "standardize", "method": "rolling_zscore", "window": _W20}],
       [("standardize", ("rolling", _W20))]),

    _v("rolling_robust_w20",
       "滚动稳健 Z-Score（中位数 + 1.4826×MAD）",
       [{"op": "standardize", "method": "rolling_robust_zscore", "window": _W20}],
       [("standardize", ("rolling_robust", _W20))],
       note="两侧的 mad 都是「先算每行相对该行 trailing 中位数的偏差，再对偏差序列"
            "取 trailing 中位数」——即口径一致地不同于教科书滚动 MAD。"),

    _v("rolling_minmax_w20",
       "滚动 Min-Max（按标的，窗口 20）",
       [{"op": "standardize", "method": "rolling_minmax", "window": _W20}],
       [("standardize", ("rolling_minmax", _W20))],
       expect="upstream_broken",
       ap_bug="trade_date",
       note="**上游实现不可用（实测确认）**：`rolling_minmax_standardize` 的签名是"
            "`(df, factor_col, trade_date, symbol_col)`，而其它同族函数都是"
            "`(df, trade_date, symbol_col, factor_col)`，`AlphaPurifier.standardize`"
            "按后者统一传参 —— 于是它对 **trade_date 列**做滚动 Min-Max 并写回"
            "（该列被改成 float/null），真正的因子列原封不动。"
            "本仓的 rolling_minmax 语义正确，此变体只用来盯「上游是否修好」。"),

    _v("volatility_scaling_w20_shift",
       "波动率缩放（σ 取 t−1，避免当日进自己分母）",
       [{"op": "standardize", "method": "volatility_scaling", "window": _W20}],
       [("standardize", ("volatility_scaling", _W20, None, True))],
       note="两侧默认都是 shift_vol=True，显式写出来以免默认值漂移悄悄改变口径。"),

    _v("yeo_johnson_l05",
       "Yeo-Johnson 幂变换 + 截面 Z（λ=0.5）",
       [{"op": "standardize", "method": "yeo_johnson", "lambda_": 0.5}],
       [("standardize", ("yeo_johnson", 0.5))],
       # AP 用 (σ+eps) 做分母、本仓用 σ≈0→1.0 兜底；eps=1e-9、σ~O(1) → 差 ~1e-9
       tol=1e-6,
       note="唯一差异是分母：AP 用 σ+1e-9，本仓用 σ≈0 兜底 1.0。"),

    _v("ewma_l094",
       "EWMA 波动缩放（λ=0.94）",
       [{"op": "standardize", "method": "ewma", "lambda_": 0.94}],
       [("standardize", ("EWMA", 0.94))],
       expect="diverge",
       note="**预期分歧（上游 bug）**：AP 把 (1−λ)λ^k 的权重挂在绝对时间下标上再"
            "反向累加，等价于 σ²_t = Σ_{u≥t}(1−λ)λ^u x²_u —— 用到 t 之后的数据"
            "（前视泄漏），且权重随绝对下标而非距离衰减。本仓用递归形式。"),

    _v("boxcox_l025",
       "Box-Cox 幂变换 + 截面 Z（λ=0.25）",
       [{"op": "standardize", "method": "boxcox", "lambda_": 0.25}],
       [("standardize", ("boxcox", 0.25))],
       expect="diverge",
       note="**预期分歧（上游 bug）**：AP 用**全样本**（含未来日期）最小值做平移，"
            "因子值随新数据整体漂移；本仓把平移限制在当日截面。"),
)


def variant_keys() -> list[str]:
    return [v["key"] for v in PREP_VARIANTS]


def by_key(key: str) -> dict[str, Any]:
    for v in PREP_VARIANTS:
        if v["key"] == key:
            return v
    raise KeyError(f"未定义的预处理变体 {key!r}，可选: {variant_keys()}")
