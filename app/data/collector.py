import akshare as ak
import pandas as pd
import os


class AStockCollector:

    def __init__(self, save_path="data/stock"):
        self.save_path = save_path
        os.makedirs(self.save_path, exist_ok=True)

    def fetch_daily(self, symbol: str,
                    start_date: str = "20250101",
                    end_date: str = "20251231",
                    adjust: str = "qfq"):
        """
        获取A股日线数据

        symbol: 股票代码，如 000001
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        adjust: qfq(前复权) / hfq(后复权) / None
        """

        print(f"正在下载 {symbol} 数据...")

        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust
        )

        # === 标准化列名（非常重要，给Backtrader用）===
        df = df.rename(columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume"
        })

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")

        # === 保存CSV ===
        file_path = os.path.join(self.save_path, f"{symbol}.csv")
        df.to_csv(file_path, index=False)

        print(f"保存完成：{file_path}")

        return df


if __name__ == "__main__":
    collector = AStockCollector()

    df = collector.fetch_daily(
        symbol="000001",
        start_date="20200101",
        end_date="20251231"
    )

    print(df.head())