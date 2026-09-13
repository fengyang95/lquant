"""新闻来源采集器。

import 即注册（news.sources.base.register 装饰器）：
新增采集器模块后在此补一行 import，get_sources() 才能看到它。
"""
from lquant.news.sources import base, em_news, tables, telegraph  # noqa: F401
