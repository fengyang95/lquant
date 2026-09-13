"""用户代码执行的收口：受限内建 + 危险调用黑名单。

**这是一层纵深防御，不是容器级隔离。**
exec 语义下，只要用户能触达任意对象，就存在通过对象图
（``().__class__.__bases__[0].__subclasses__()``）逃逸的理论路径。
本模块的目标是把「一句 ``__import__("os").system(...)`` 就能 RCE」
抬高到「必须刻意构造逃逸链」，并让静态校验在源头记一笔。

真正可靠的边界是进程级隔离（rlimit 限内存/CPU + 禁网 + 超时强杀），
配套的服务默认只绑 ``127.0.0.1``（见 ``lquant.sh`` 的 ``LQ_API_HOST``）。

用法::

    ns = {"__name__": "__analysis__", ...}
    ns["__builtins__"] = safe_builtins()
    exec(source, ns)
"""
from __future__ import annotations

import builtins

__all__ = [
    "ALLOWED_IMPORTS",
    "FORBIDDEN_ATTRS",
    "FORBIDDEN_CALL_NAMES",
    "FORBIDDEN_GLOBAL_NAMES",
    "SAFE_BUILTIN_NAMES",
    "safe_builtins",
]

# 用户代码可 import 的顶层模块。静态校验与运行期 __import__ 共用这一份，
# 避免「静态放行、运行期炸」或「静态拦住、运行期能绕」两头不一致。
ALLOWED_IMPORTS = frozenset({
    "math", "datetime", "collections", "itertools", "functools",
    "statistics", "numpy", "pandas", "polars",
})

# 名字调用了就直接拒：这些是「一句话 RCE」的主力。
FORBIDDEN_CALL_NAMES = frozenset({
    "__import__", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "input", "breakpoint", "exit", "quit", "help", "memoryview",
})

# 属性访问黑名单：对象图逃逸的必经之路。
# __dict__ 一并拒（与 vars 同类：能拿到实例内部状态，是逃逸链的第一跳）。
FORBIDDEN_ATTRS = frozenset({
    "__class__", "__bases__", "__subclasses__", "__mro__", "__dict__",
    "__globals__", "__builtins__", "__getattribute__", "__setattr__",
    "__delattr__", "__code__", "__closure__", "__func__", "__self__",
    "__loader__", "__spec__", "__reduce__", "__reduce_ex__",
})

# 以全局名形式直接引用这些名字也拒（防止 `__builtins__[...]` 之类的花招）。
# 刻意不含 __import__：运行期那个名字已被换成白名单守卫版（见 _guarded_import），
# 裸引用无害；而显式调用 __import__("os") 由 FORBIDDEN_CALL_NAMES 覆盖，
# 放这里只会产生一条重复诊断。
FORBIDDEN_GLOBAL_NAMES = frozenset({
    "__builtins__", "__loader__", "__spec__", "__debug__",
})

# 允许暴露给用户代码的内建。刻意**不含**上面三张黑名单里的任何名字。
SAFE_BUILTIN_NAMES = frozenset({
    # 类型（True/False/None 是关键字，不在 builtins 里，无需也不应列入）
    "bool", "bytes", "bytearray", "complex", "dict", "float", "frozenset",
    "int", "list", "object", "range", "set", "slice", "str", "tuple", "type",
    # 常用函数
    "abs", "all", "any", "bin", "callable", "chr", "divmod", "enumerate",
    "filter", "format", "hasattr", "hash", "hex", "isinstance", "issubclass",
    "iter", "len", "map", "max", "min", "next", "oct", "ord", "pow",
    "print", "repr", "reversed", "round", "sorted", "sum", "zip",
    # 类定义所需（否则用户代码里的 class 语句会 NameError）
    "__build_class__", "classmethod", "property", "staticmethod", "super",
    # 异常类型（except 子句要能查到名字，否则 try/except 直接 NameError）
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "Exception", "FloatingPointError", "IndexError", "KeyError", "LookupError",
    "MemoryError", "NameError", "NotImplementedError", "OSError", "OverflowError",
    "RecursionError", "RuntimeError", "StopIteration", "SyntaxError",
    "TypeError", "UnicodeError", "ValueError", "ZeroDivisionError",
    "UserWarning", "Warning",
})


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    """白名单版 ``__import__``。

    ``import`` 语句在运行期就是调这个内建 —— 直接从受限集合里删掉 ``__import__``
    会让用户代码里合法的 ``import statistics`` 直接 ``ImportError``。
    所以给一个只看顶层模块名的守卫版本：白名单内放行，其余一律拒。

    注意与静态层的 ``FORBIDDEN_CALL_NAMES`` 不冲突：那里拦的是用户**显式**写
    ``__import__("os")``；这里服务的是 ``import`` 语句的隐式调用。
    """
    root = str(name).split(".")[0]
    if level != 0 or root not in ALLOWED_IMPORTS:
        raise ImportError(
            f"不允许 import {root}（白名单: {', '.join(sorted(ALLOWED_IMPORTS))}）")
    return builtins.__import__(name, globals, locals, fromlist, level)


def safe_builtins() -> dict[str, object]:
    """返回一份**新的**受限内建字典（每次 exec 单独一份，互不污染）。

    返回新副本而不是共享单例：用户代码拿到的是 globals 里的 ``__builtins__``，
    能改它。给副本至少把影响限制在本次执行内。
    """
    sb = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES}
    sb["__import__"] = _guarded_import
    return sb
