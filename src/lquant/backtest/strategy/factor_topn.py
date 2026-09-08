"""示例：因子选股 TopN 等权。"""
from __future__ import annotations

from lquant.backtest.events import Bar
from lquant.backtest.strategy import register_strategy
from lquant.backtest.strategy.base import Context, Strategy


class FactorTopNStrategy(Strategy):
    def __init__(self, factor: str = "mom_20", top_n: int = 30) -> None:
        super().__init__(factor=factor, top_n=top_n)

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        scores = {s: b.fields.get(self.params["factor"]) for s, b in bars.items()
                  if b.fields.get(self.params["factor"]) is not None}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        picked = [s for s, _ in ranked[: self.params["top_n"]]]
        w = 1.0 / len(picked) if picked else 0.0
        return [(s, w) for s in picked]


register_strategy("factor_topn", {"label": "因子 TopN 等权",
                                  "params": {"factor": "因子列名", "top_n": "持仓数"}})(FactorTopNStrategy)
