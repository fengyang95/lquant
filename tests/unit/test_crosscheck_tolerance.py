"""跨源对拍分字段容差：量额类字段（volume/amount）阈值放宽，价格类不变。

背景：统计口径/含盘后交易与否，两源量额差 20-50% 常见 → 旧逻辑按价格阈值
统一判定会刷大量假 L3 error issue。
"""
from datetime import date

import polars as pl
import pytest

from lquant.data.quality.crosscheck import L1, L2, L3, classify_divergence


def _frame(values: dict) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001"],
        "trade_date": [date(2024, 1, 2)],
        **values,
    })


def _classify(field: str, a: float, b: float) -> pl.DataFrame:
    return classify_divergence(
        _frame({field: [a]}), _frame({field: [b]}), fields=(field,),
    )


def test_price_diff_30pct_still_l3():
    # 价格类字段行为不变：30% 偏差（1.3× > 1.2× 硬阈值）→ L3
    out = _classify("close", 100.0, 130.0)
    assert out["level"][0] == L3


def test_price_diff_15pct_is_l2():
    # 价格类字段行为不变：15% 偏差（<1.2×）→ L2 material
    out = _classify("close", 100.0, 115.0)
    assert out["level"][0] == L2


def test_price_ratio_1_5_still_l3():
    # 价格类字段：1.5× 数量级差 → L3（旧硬阈值 1.2× 保留）
    out = _classify("close", 100.0, 150.0)
    assert out["level"][0] == L3


def test_volume_diff_30pct_not_l3():
    # 量额类：30% 偏差降为 L1 提示（>=20% 且 <50%），不再是 L3
    out = _classify("volume", 1_000_000.0, 1_300_000.0)
    assert out["level"][0] == L1


def test_volume_diff_45pct_is_l1():
    # 量额类：>=20% 且 <50% → L1（不 material）
    out = _classify("volume", 1_000_000.0, 1_450_000.0)
    assert out["level"][0] == L1


def test_volume_diff_60pct_is_l2():
    # 量额类：>=50% 且 <2× → L2 material，打标但不降级
    out = _classify("volume", 1_000_000.0, 1_600_000.0)
    assert out["level"][0] == L2


def test_volume_triple_is_l3():
    # 量额类：3 倍数量级差仍要报 L3
    out = _classify("volume", 1_000_000.0, 3_000_000.0)
    assert out["level"][0] == L3


def test_amount_uses_relaxed_thresholds():
    # amount 同样按量额类放宽（含 _right 后缀式命名 endswith 判定）
    out = _classify("amount", 10_000_000.0, 12_000_000.0)
    assert out["level"][0] == L1


def test_pre_close_is_price_field():
    # pre_close 属价格类字段：30% 偏差（1.3×）仍按价格阈值报 L3
    out = _classify("pre_close", 100.0, 130.0)
    assert out["level"][0] == L3


def test_amount_fields_injection():
    # keyword-only amount_fields：注入自定义量额字段集合
    out = classify_divergence(
        _frame({"turnover": [100.0]}), _frame({"turnover": [150.0]}),
        fields=("turnover",), amount_fields=frozenset({"turnover"}),
    )
    # 1.5× 在量额类下仍属 L2（< 2.0× 数量级差）
    assert out["level"][0] == L2


def test_zero_vs_nonzero_volume_still_l3():
    # 零 vs 非零：量额类也保持 L3（硬伤不放行）
    out = _classify("volume", 0.0, 500_000.0)
    assert out["level"][0] == L3


def test_output_schema_unchanged():
    out = _classify("volume", 100.0, 130.0)
    assert out.columns == ["symbol", "trade_date", "field", "primary",
                           "peer", "rel_diff", "missing", "level"]
    assert out["rel_diff"][0] == pytest.approx(0.3)
