r"""akshare 兼容层（pandas 3 Arrow 字符串后端）单元测试。

背景：pandas 3 默认 `future.infer_string=True`，文本列走 PyArrow 字符串后端，
`.str.replace(regex=True)` 交给 RE2，而 RE2 不接受 `\uXXXX` 转义 —— akshare 内部
的 `r"\u3000"` 清洗会让 `ak.stock_news_em` 之类接口直接抛 ArrowInvalid。
"""

from __future__ import annotations

import pandas as pd
import pytest

from lquant.news.sources.akcompat import ak_call, plain_string_backend


def _infer_string() -> bool:
    try:
        return bool(pd.get_option("future.infer_string"))
    except (KeyError, AttributeError, ValueError):  # pragma: no cover - 旧版 pandas
        pytest.skip("当前 pandas 无 future.infer_string 选项")
        raise


def test_plain_string_backend_disables_then_restores() -> None:
    previous = _infer_string()
    with plain_string_backend():
        assert pd.get_option("future.infer_string") is False
    assert pd.get_option("future.infer_string") == previous


def test_plain_string_backend_restores_after_exception() -> None:
    previous = _infer_string()
    with pytest.raises(RuntimeError, match="boom"), plain_string_backend():
        raise RuntimeError("boom")
    assert pd.get_option("future.infer_string") == previous


def test_ak_call_passes_through_args_and_result() -> None:
    assert ak_call(lambda a, b=1: a + b, 1, b=2) == 3
    assert ak_call(lambda: pd.get_option("future.infer_string")) is False


def test_ak_call_makes_unicode_escape_regex_work() -> None:
    """复刻 akshare 的清洗写法：不套兼容层时 Arrow 后端会拒掉 r"\\u3000"。"""
    previous = _infer_string()
    pd.set_option("future.infer_string", True)
    try:

        def upstream() -> pd.DataFrame:
            df = pd.DataFrame({"内容": ["甲\u3000乙"]})
            df["内容"] = df["内容"].str.replace(r"\u3000", "", regex=True)
            return df

        assert ak_call(upstream).iloc[0, 0] == "甲乙"
    finally:
        pd.set_option("future.infer_string", previous)
