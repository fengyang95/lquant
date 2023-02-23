import backtrader as bt
import akshare as ak
import os
import pandas as pd
import talib as ta
DAY_FORMAT = "%Y-%m-%d"


class BasicStrategy(bt.Strategy):
    def next(self):
        today = self.datas[0].datetime.date()
        for data in self.datas:
            symbol = data._name
            close = data.close[0]
            #print(f"today:{today} symbol:{symbol} close:{close}")


if __name__ == "__main__":
    cerebro = bt.Cerebro()

    stock_data_dir = "../../data/沪深300"
    for file_name in os.listdir(stock_data_dir)[:1]:
        stock_df = pd.read_csv(os.path.join(stock_data_dir, file_name))
        stock_code, stock_name = file_name.split('.')[0].split('_')
        stock_df = stock_df.rename(
            columns={
                "日期": "date", "开盘": 'open', "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "振幅": "swing", "涨跌幅": "chg_pct",
                "涨跌额": "chg_amount", "换手率": "turnover"
            }
        )
        stock_df['date'] = pd.to_datetime(stock_df['date'], format=DAY_FORMAT)
        stock_df.set_index("date",inplace=True)

        stock_df['dif'], stock_df['dea'], stock_df['macd'] = ta.MACD(stock_df.close, fastperiod=12, slowperiod=26, signalperiod=9)
        print(stock_df.tail(5))
        cerebro.adddata(bt.feeds.PandasData(dataname=stock_df), name=stock_code)

    cerebro.addstrategy(BasicStrategy)
    cerebro.run()
