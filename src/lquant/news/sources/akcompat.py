"""akshare 调用兼容层。

本仓库的 pandas 3.x 默认 `future.infer_string = True` —— DataFrame 文本列落在
PyArrow 字符串后端上，`.str.replace(regex=True)` 会走 pyarrow 的 RE2 引擎。
RE2 只认 `\\x{3000}` 这类转义，**不接受 `\\uXXXX`**；而 akshare 内部大量使用
`.str.replace(r"\\u3000", "", regex=True)` 清洗全角空格，于是整条调用在读数据阶段
就抛 `pyarrow.lib.ArrowInvalid: Invalid regular expression: invalid escape sequence: \\u`。

典型受害者是 `ak.stock_news_em`（个股新闻）—— 采集器一行都拿不到，任务明细里记
`failed` 却看不出原因。

统一约定：**所有 akshare 调用都经 `ak_call()`**，在调用期间把字符串后端切回 Python
对象，调用结束自动还原（`plain_string_backend` 上下文管理器可单独复用）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

import pandas as pd

_OPTION = "future.infer_string"


@contextlib.contextmanager
def plain_string_backend() -> Iterator[None]:
    """调用期间关闭 pandas 的 Arrow 字符串后端；选项不存在（旧版 pandas）则原样放行。"""
    try:
        previous = pd.get_option(_OPTION)
    except (KeyError, AttributeError, ValueError):  # pragma: no cover - 旧版 pandas
        yield
        return
    pd.set_option(_OPTION, False)
    try:
        yield
    finally:
        pd.set_option(_OPTION, previous)


def ak_call[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在 Python 字符串后端下调用 akshare 接口（原因见模块 docstring）。"""
    with plain_string_backend():
        return fn(*args, **kwargs)
