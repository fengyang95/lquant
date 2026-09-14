"""探针:attribute_history positional 'close' 与边界行为(复杂策略 E2E KeyError 复现)。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.backtest.jqapi import JQRunner

rows = []
d = date(2024, 12, 1)
for i in range(60):
    d = date.fromordinal(d.toordinal() + 1)
    if d.weekday() >= 5:
        continue
    rows.append({"symbol": "000001.SZ", "open": 10, "high": 11,
                 "low": 9, "close": 10 + i * 0.1, "pre_close": 10, "volume": 1e6,
                 "amount": 1e7, "is_st": False, "is_suspended": False,
                 "trade_date": d})
bars = pl.DataFrame(rows)

CODE = '''
def initialize(context):
    set_benchmark('000300.SH')
    run_daily(probe, time='every_bar')

def probe(context):
    n = attribute_history('000001.SZ', 121, '1d', 'close', skip_paused=True)
    log.info("cols=%r len=%d", list(n.columns), len(n))
    try:
        mom = n['close'][-1] / n['close'][0] - 1
        log.info("mom ok %.4f", mom)
    except KeyError as e:
        log.info("REPRO KeyError %r", e)
'''

res = JQRunner(CODE, initial_cash=1_000_000).run(bars)
print("error:", res.error)
for line in res.logs[-6:]:
    print("LOG:", line)
