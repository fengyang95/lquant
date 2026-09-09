"""缺陷 #6：DSL 字段白名单校验 —— 拼错字段立刻报错并给近似候选。"""
from __future__ import annotations

import pytest

from lquant.core.errors import FactorError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.parser import parse

ALLOWED = {"close", "open", "volume", "amount"}


def test_typo_field_raises_with_suggestion():
    ast = parse("Ts_Mean($closs, 5)", "t")
    with pytest.raises(FactorError) as ei:
        check(ast, allowed_fields=ALLOWED)
    assert "close" in str(ei.value), "错误消息必须给出近似候选"


def test_unknown_field_no_candidate():
    ast = parse("$xyzzy + 1", "t")
    with pytest.raises(FactorError):
        check(ast, allowed_fields=ALLOWED)


def test_valid_fields_pass():
    ast = parse("Ts_Mean($close, 5) - Ts_Mean($open, 5)", "t")
    check(ast, allowed_fields=ALLOWED)          # 不抛即过
    check(ast, allowed_fields=None)             # 不传白名单 = 不校验（兼容旧行为）
