from binance.client import Client
from binance.exceptions import BinanceAPIException
import time
from config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_ENVIRONMENT

def get_max_leverage(client: Client, symbol: str) -> int:
    """
    查询某个永续合约允许的最大杠杆
    """
    brackets = client.futures_leverage_bracket(symbol=symbol)

    # brackets 是 list，取第一个
    return max(
        int(b["initialLeverage"])
        for b in brackets[0]["brackets"]
    )

def main():
    client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET, testnet=BINANCE_ENVIRONMENT)

    exchange_info = client.futures_exchange_info()

    # 仅筛选 USDT-M 永续合约
    symbols = [
        s["symbol"]
        for s in exchange_info["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["status"] == "TRADING"
    ]

    print(f"发现永续合约数量: {len(symbols)}")

    ok, fail = 0, 0

    for i, symbol in enumerate(symbols, 1):
        try:
            max_leverage = get_max_leverage(client, symbol)

            result = client.futures_change_leverage(
                symbol=symbol,
                leverage=max_leverage
            )

            print(
                f"[{i}/{len(symbols)}] {symbol} 杠杆已设置为 {result['leverage']}x"
            )
            ok += 1
            time.sleep(0.05)

        except BinanceAPIException as e:
            print(
                f"[{i}/{len(symbols)}] {symbol} 设置失败: {e.message}"
            )
            fail += 1
            time.sleep(0.05)

        except Exception as e:
            print(
                f"[{i}/{len(symbols)}] {symbol} 未知错误: {str(e)}"
            )
            fail += 1
            time.sleep(0.05)

    print("\n====== 结果统计 ======")
    print(f"成功: {ok}")
    print(f"失败: {fail}")

if __name__ == "__main__":
    main()
