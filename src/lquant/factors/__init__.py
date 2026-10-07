"""因子层：DSL → AST → Polars LazyExpr，含预处理与评价。"""

# 触发协变量 provider 注册（看板另类数据等，factors/sources/board.py）。
# factors 包是所有因子路径（挖掘/eval/submit/audit/服务端评价）的公共祖先，
# 在这里 import 一次即保证任何入口都拿得到全部 provider —— 与 ops/__init__
# 的算子注册触发同款模式。缺这行，@provider 装饰器永远不执行，build_covariates
# 找不到看板 provider，coverage=0 还会被误读成「没采集数据」。
from lquant.factors.sources import board  # noqa: F401  触发注册
