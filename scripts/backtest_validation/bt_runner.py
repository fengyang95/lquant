"""backtrader 参考引擎执行器（在独立 venv 里跑，与 lquant 环境隔离）。

用法：
    btval-python bt_runner.py --task sma_cross --data <parquet> --out <json>
        [--symbols 600519.SH] [--fast 5] [--slow 20] ...

读入前复权日线 parquet，用与 lquant 基准策略完全一致的规则跑 backtrader，
输出 {dates, nav, n_trades, positions} JSON。费率两侧硬编码一致：
佣金万2.5（最低5元）+ 卖出印花税千一（ETF 免）+ 过户费十万分之一。

镜像规则注意（与 lquant backtest/benchmarks.py 一一对应）：
- 信号都在 T 日收盘算出，市价单 T+1 开盘价成交（backtrader 默认）；
- lquant 的 min_order_value=1000 → 只有目标市值差 >= 1000 才调 order_target_size；
- lquant 全仓买入 qty = NAV/收盘价 向下取整一手 → bt 侧 size 同口径；
- **T+1 模拟**：bt 原生没有 A 股 T+1 —— 按 lots 记录买入日期，
  卖出只能卖「买入日 +1 天 ≤ 今天」的份额，与 lquant 引擎一致；
- 资金不足：bt 执行时现金不足 → 整单作废（bbroker _execute cash<0 → opened=0），
  lquant 侧同样整单作废。
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta

import backtrader as bt
import pandas as pd


class CNCommInfo(bt.CommInfoBase):
    """A股费率：双边佣金（最低5元）+ 卖出印花税 + 双边过户费。"""

    params = (("stocklike", True), ("commtype", bt.CommInfoBase.COMM_PERC),
              ("stamp", 0.001), ("transfer", 0.00001), ("mincomm", 5.0))

    def _getcommission(self, size, price, pseudoexec):
        amount = abs(size) * price
        comm = max(amount * self.p.commission, self.p.mincomm)
        if size < 0:
            comm += amount * self.p.stamp
        comm += amount * self.p.transfer
        return comm


class _Base(bt.Strategy):
    """公共骨架：净值曲线 + min_order_value 门 + 一手取整。"""

    params = (("min_order_value", 1000.0), ("lot", 100))

    def __init__(self) -> None:
        self.dates: list[str] = []
        self.navs: list[float] = []
        self.n_trades = 0
        self.pos_snapshot: list[dict] = []
        self._lots: dict[str, list[list]] = {}   # data name -> [[buy_date, size], ...]

    def next(self):
        self.dates.append(self.data.datetime.date(0).isoformat())
        self.navs.append(float(self.broker.getvalue()))
        self.pos_snapshot.append(
            {d._name: float(self.getposition(d).size) for d in self.datas})

    def notify_order(self, order):
        if order.status != order.Completed:
            return
        dt = bt.num2date(order.executed.dt).date()
        lots = self._lots.setdefault(order.data._name, [])
        if order.isbuy():
            lots.append([dt, float(order.executed.size)])
        else:
            remain = float(order.executed.size)
            for lot in lots:
                take = min(lot[1], remain)
                lot[1] -= take
                remain -= take
            self._lots[order.data._name] = [l for l in lots if l[1] > 1e-6]

    def _sellable(self, data, today) -> float:
        """T+1 可卖份额：买入日 +1 天 <= 今天的 lots 之和（A 股 T+1 模拟）。"""
        return sum(s for d0, s in self._lots.get(data._name, [])
                   if d0 + timedelta(days=1) <= today)

    def notify_trade(self, trade):
        if trade.isclosed:
            self.n_trades += 1

    def _maybe_target(self, frac: float, data=None) -> None:
        """|目标市值差| 达到 min_order_value 才动（对齐 lquant 引擎）。

        买入按**差额**取整下单（lq：qty = min(delta, cash)/px 向下取整一手）；
        减仓受 T+1 约束：只能卖「买入日 +1 天 ≤ 今天」的份额（整手）。
        """
        data = data or self.data
        px = float(data.close[0])
        cur = float(self.getposition(data).size)
        want_raw = float(self.broker.getvalue()) * frac / px      # 未取整目标股数
        if abs(want_raw * px - cur * px) < self.p.min_order_value:
            return
        if want_raw > cur:
            delta = int((want_raw - cur) / self.p.lot) * self.p.lot
            if delta > 0:
                self.buy(data=data, size=delta)
        else:
            sellable = min(cur - want_raw, self._sellable(data, self.data.datetime.date(0)))
            sellable = int(sellable / self.p.lot) * self.p.lot
            if sellable > 0:
                self.sell(data=data, size=sellable)


class SmaCrossBT(_Base):
    params = (("fast", 5), ("slow", 20))

    def __init__(self) -> None:
        super().__init__()
        self.closes: list[float] = []

    def next(self):
        super().next()
        self.closes.append(float(self.data.close[0]))
        f, s = self.p.fast, self.p.slow
        if len(self.closes) < s:
            return
        fast_ma = sum(self.closes[-f:]) / f
        slow_ma = sum(self.closes[-s:]) / s
        self._maybe_target(1.0 if fast_ma > slow_ma else 0.0)


class MomentumRotationBT(_Base):
    params = (("window", 22), ("nsyms", 2))

    def __init__(self) -> None:
        super().__init__()
        self.closes: dict[int, list[float]] = {i: [] for i in range(self.p.nsyms)}

    def next(self):
        super().next()
        for i, d in enumerate(self.datas):
            self.closes[i].append(float(d.close[0]))
        w = self.p.window
        if any(len(c) <= w for c in self.closes.values()):
            return
        scores = {i: c[-1] / c[-1 - w] - 1.0 for i, c in self.closes.items()}
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            # 切换目标：先卖掉非 best 的持仓（对齐 lquant「不在目标里的持仓全卖」）
            for i, d in enumerate(self.datas):
                if i != best:
                    self._maybe_target(0.0, data=d)
            self._maybe_target(1.0, data=self.datas[best])
        else:
            for d in self.datas:
                self._maybe_target(0.0, data=d)


class TurtleBT(_Base):
    params = (("entry", 20), ("exitp", 10), ("atr_n", 20), ("risk", 0.01))

    def __init__(self) -> None:
        super().__init__()
        self.h: list[tuple[float, float, float]] = []   # (high, low, close)

    def next(self):
        super().next()
        self.h.append((float(self.data.high[0]), float(self.data.low[0]),
                       float(self.data.close[0])))
        n_in, n_out = self.p.entry, self.p.exitp
        if len(self.h) <= max(n_in, self.p.atr_n):
            return
        highs = [x[0] for x in self.h]
        lows = [x[1] for x in self.h]
        closes = [x[2] for x in self.h]
        don_high = max(highs[-n_in - 1:-1])
        don_low = min(lows[-n_out - 1:-1])
        trs = []
        for i in range(-self.p.atr_n, 0):
            hi, lo, cl = self.h[i]
            pc = closes[i - 1]
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        atr = sum(trs) / len(trs)
        close = closes[-1]
        # 只在状态翻转时下单（对齐 lquant 海龟：入场一次性建仓、出场一次性清仓）
        in_pos = float(self.getposition(self.data).size) != 0
        if close > don_high and atr > 0 and not in_pos:
            self._maybe_target(min(1.0, self.p.risk * close / atr))
        elif close < don_low and in_pos:
            self._maybe_target(0.0)


class GridBT(_Base):
    params = (("grid_pct", 0.05), ("levels", 8))

    def __init__(self) -> None:
        super().__init__()
        self.anchor: float | None = None

    def next(self):
        super().next()
        close = float(self.data.close[0])
        if self.anchor is None:
            self.anchor = close
            self._maybe_target(0.5)
            return
        rel = close / self.anchor - 1.0
        frac = 0.5 - 0.5 * (rel / self.p.grid_pct) / self.p.levels
        self._maybe_target(min(1.0, max(0.0, frac)))


class BaselineMultifactorBT(_Base):
    """基准多因子对账：按主仓预计算的权重调度表执行（order_target_value 语义）。

    调度表 ``{date: {sym: weight}}``（等权 1/N，由 lquant 侧基准策略在调仓日
    open 时点导出，主仓 venv 跑 JQRunner 生成）。撮合对齐 lquant JQRunner 的
    「open 时点委托即以当日开盘价撮合」：cheat-on-open + 以当日开盘价口径估
    total_value，复刻差额整手取整、资金上限 clamp（afford 公式一致）、T+1 可卖。
    调度表里没有的持仓当日清仓；执行顺序先卖后买（对齐 lquant 合成策略）。
    """

    params = (("schedule", None),)

    def __init__(self) -> None:
        super().__init__()
        self._sched = {str(k): dict(v) for k, v in (self.p.schedule or {}).items()}

    def next(self):
        super().next()
        today = self.data.datetime.date(0)      # data0 = 全交易日参考标的（日历轴）
        w = self._sched.get(today.isoformat())
        if not w:
            return
        # 当日开盘价口径 total_value（对齐 lquant JQ open 时点 total_value）
        cash = float(self.broker.getcash())
        mv = 0.0
        advanced: dict[str, bool] = {}
        for data in self.datas:
            adv = bt.num2date(data.datetime[0]).date() == today
            advanced[data._name] = adv
            if adv:
                mv += float(self.getposition(data).size) * float(data.open[0])
        total = cash + mv
        # 1) 持仓不在调度表 → 清仓
        for data in self.datas:
            nm = data._name
            if (advanced[nm] and float(self.getposition(data).size) != 0
                    and nm not in w):
                self._jq_target_value(data, 0.0, today)
        # 2) 减仓（目标市值低于当前市值）
        for data in self.datas:
            nm = data._name
            if not advanced[nm] or nm not in w:
                continue
            cur_val = float(self.getposition(data).size) * float(data.open[0])
            if cur_val > total * w[nm]:
                self._jq_target_value(data, total * w[nm], today)
        # 3) 加仓（含新建仓；停牌标的 open 取的是旧 bar，与 lquant 无参考价跳过对齐）
        for data in self.datas:
            nm = data._name
            if not advanced[nm] or nm not in w:
                continue
            cur_val = float(self.getposition(data).size) * float(data.open[0])
            if cur_val <= total * w[nm]:
                self._jq_target_value(data, total * w[nm], today)

    def _jq_target_value(self, data, value: float, today) -> None:
        """镜像 lquant JQ order_target_value → _submit：差额整手取整、
        买单按 afford 公式 clamp 到现金、卖出受 T+1 约束、|差额| < 1 股跳过。"""
        px = float(data.open[0])
        if px <= 0:
            return
        cur = float(self.getposition(data).size)
        amount = value / px - cur
        if abs(amount) < 1.0:                   # lquant: |amount| < 1 返回 None
            return
        if amount > 0:
            cash = float(self.broker.getcash())
            afford = cash / (px * (1 + 0.00025 + 0.001))   # lquant _submit 同式
            qty = int(min(amount, afford)) // self.p.lot * self.p.lot
            if qty > 0:
                self.buy(data=data, size=qty)
        else:
            avail = self._sellable(data, today)             # T+1 可卖份额
            qty = int(min(-amount, avail)) // self.p.lot * self.p.lot
            if qty > 0:
                self.sell(data=data, size=qty)


TASKS = {"sma_cross": SmaCrossBT, "momentum_rotation": MomentumRotationBT,
         "turtle_donchian": TurtleBT, "grid_trading": GridBT,
         "baseline_multifactor": BaselineMultifactorBT}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, choices=sorted(TASKS))
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--window", type=int, default=22)
    ap.add_argument("--nsyms", type=int, default=2)
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--exitp", type=int, default=10)
    ap.add_argument("--atr_n", type=int, default=20)
    ap.add_argument("--grid_pct", type=float, default=0.05)
    ap.add_argument("--levels", type=int, default=8)
    ap.add_argument("--schedule", default=None,
                    help="baseline_multifactor: 权重调度表 JSON 路径")
    ap.add_argument("--ref-symbol", default="510300.SH",
                    help="baseline_multifactor: 全交易日参考标的（日历轴，不交易）")
    ap.add_argument("--tax", type=float, default=0.001,
                    help="卖出印花税率（默认 0.001；2023-08-28 后为 0.0005）")
    ap.add_argument("--etf", action="store_true", help="ETF：免印花税")
    args = ap.parse_args()

    pdf = pd.read_parquet(args.data)
    pdf["trade_date"] = pd.to_datetime(pdf["trade_date"])
    pdf = pdf.set_index("trade_date")

    # baseline：参考标的必须排第一（bt 以 data0 的日期做日历轴）
    if args.task == "baseline_multifactor":
        if args.ref_symbol not in set(pdf["symbol"].unique()):
            raise SystemExit(f"参考标的 {args.ref_symbol} 不在数据里")
        pdf = pd.concat([pdf[pdf["symbol"] == args.ref_symbol],
                         pdf[pdf["symbol"] != args.ref_symbol]])

    cerebro = bt.Cerebro()
    for sym in pdf["symbol"].unique():
        sub = pdf[pdf["symbol"] == sym].drop(columns=["symbol"])
        feed = bt.feeds.PandasData(dataname=sub,
                                   datetime=None, open="open", high="high",
                                   low="low", close="close", volume="volume",
                                   openinterest=None)
        cerebro.adddata(feed, name=sym)
    if args.task == "baseline_multifactor":
        with open(args.schedule) as f:
            task_kw = {"schedule": json.load(f)}
    else:
        task_kw = {
            "sma_cross": {"fast": args.fast, "slow": args.slow},
            "momentum_rotation": {"window": args.window, "nsyms": args.nsyms},
            "turtle_donchian": {"entry": args.entry, "exitp": args.exitp,
                                "atr_n": args.atr_n},
            "grid_trading": {"grid_pct": args.grid_pct, "levels": args.levels},
        }[args.task]
    cerebro.addstrategy(TASKS[args.task], **task_kw)
    cerebro.broker.setcash(1_000_000.0)
    cerebro.broker.set_checksubmit(False)   # 换仓买单依赖同批卖单资金，不能预检拒单
    if args.task == "baseline_multifactor":
        cerebro.broker.set_coo(True)        # 委托当日开盘价成交（对齐 JQ open 时点）
    comm = CNCommInfo(commission=0.00025, stamp=args.tax)
    if args.etf:
        comm = CNCommInfo(commission=0.00025, stamp=0.0, transfer=0.0)
    cerebro.broker.addcommissioninfo(comm)
    cerebro.broker.set_slippage_perc(0.0)

    strat = cerebro.run()[0]
    out = {"dates": strat.dates, "nav": strat.navs, "n_trades": strat.n_trades,
           "final_value": float(cerebro.broker.getvalue())}
    out["positions"] = strat.pos_snapshot
    with open(args.out, "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
