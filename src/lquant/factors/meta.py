"""因子元信息读取（注册表侧）。

报告需要「这个因子是干什么的」（经济含义）—— 它存在 ``factor_def.description``，
但报告生成器不该自己去连库（它是纯函数、可离线调用）。所以这里提供一个小读口，
由接线方（API / CLI）查好再传进去；查不到就返回空串，报告写「未提供」。
"""
from __future__ import annotations

__all__ = ["factor_description"]


def factor_description(name: str | None) -> str:
    """按注册名读因子描述（经济含义）。查不到 / 读库失败 → ``""``。"""
    if not name:
        return ""
    try:
        from lquant.core.db import reader

        with reader() as con:
            row = con.execute(
                "SELECT description FROM factor_def WHERE name = ?", [name]).fetchone()
    except Exception:  # noqa: BLE001 - 元信息是锦上添花，绝不影响报告生成
        return ""
    return str(row[0] or "") if row else ""
