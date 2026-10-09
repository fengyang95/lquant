"""扩展参考实现（模板）：复制本目录为 ``extensions/<你的扩展名>/`` 即生效。

为什么目录名带 ``_`` 前缀：扫描器跳过 ``.`` / ``_`` 开头的条目，所以模板本身
永远**不会**被自动装载（否则每个装了 lquant 的人都会看到一个叫 _template 的
扩展）。复制改名后新目录才会被扫描到。

契约三条（``src/lquant/core/extensions.py`` 是唯一权威）：
1. 声明整数 ``EXTENSION_API_VERSION``；与核心不符 → 跳过 + warning，不抛。
2. 定义 ``setup(registrar)``：只往 registrar 里声明贡献（staging）。
3. 可选定义 ``startup(context)``：拿到窄上下文（只读），只在装载成功后调用一次。

本事例不写任何业务逻辑，只演示「怎么声明」和「拿得到什么」。
"""

from __future__ import annotations

# 契约版本：从核心导入而不是写字面量，升级契约时一处改、处处跟随。
# 核心按它决定是否装载；不符只跳过本扩展，不会让应用起不来。
from lquant.core.extensions import EXTENSION_API_VERSION as CORE_API_VERSION
from lquant.core.extensions import ExtensionContext, ExtensionRegistrar

EXTENSION_API_VERSION = CORE_API_VERSION

# 可选：扩展身份。不写则默认取目录名；显式声明后改目录名不会改变「扩展身份」。
# EXTENSION_ID = "your_extension_id"


def setup(registrar: ExtensionRegistrar) -> None:
    """声明扩展贡献。这里只能写 staging —— 提交/回滚都由核心负责。

    `add_route` 的参数不合法会抛 ValueError，但只会让本扩展进入报告的
    ``failed``，应用和其他扩展照常启动。
    """

    def hello() -> dict[str, str]:
        # 真实扩展在这里 import 自己的实现模块；本模板用内联函数保持最小。
        return {"extension": registrar.name, "message": "hello from lquant extension"}

    registrar.add_route("/ext/demo/hello", handler=hello, name="demo_hello")


def startup(context: ExtensionContext) -> None:
    """可选钩子：装载成功后调用一次（核心上下文此时可用）。

    上下文刻意窄：只有 ``data_dir``（路径）与只读仓储 ``repository``；
    拿不到 app / settings / service。要写数据必须走核心的显式入口。
    """
    del context
