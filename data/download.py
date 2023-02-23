import akshare as ak
if __name__=="__main__":

    index_stock_cons_df=ak.index_stock_cons(symbol='000300')
    print(index_stock_cons_df)

    for index,row in index_stock_cons_df.iterrows():
        stock_code,stock_name=row['品种代码'],row['品种名称']
        stock_data=ak.stock_zh_a_hist(
            symbol=stock_code,
            start_date='20150101',
            end_date='20221231',
            adjust='qfq'
        )
        stock_data.to_csv(f"./{stock_code}_{stock_name}.csv")
        print(f"{index}:{stock_name}")
