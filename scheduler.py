import asyncio
from datetime import datetime, timezone
from config import monitor_symbols, mainstream_symbols
from indicators import calculate_signal_single
from deepseek_batch_pusher import push_batch_to_deepseek
from kline_fetcher import fetch_all
from ai_trade_notifier import send_tg_trade_signal
from position_cache import position_records
from account_positions import get_account_status
from database import redis_client
from trader import execute_trade_async  # 异步下单版本
import time

tf_order = ["1d", "4h", "1h", "15m", "5m"]
last_trigger = {tf: None for tf in tf_order}

async def schedule_loop_async():
    print("⏳ 启动最简调度循环（周期触发 → 下载K线 → 投喂AI + 并行下单）")

    while True:
        now = datetime.now(timezone.utc)
        m, h = now.minute, now.hour
        current_key = None

        if h == 0 and m == 0:
            current_key = "1d"
        elif h % 4 == 0 and m == 0:
            current_key = "4h"
        elif m == 0:
            current_key = "1h"
        elif m % 15 == 0:
            current_key = "15m"
        elif m % 5 == 0:
            current_key = "5m"

        if current_key:
            mark = now.strftime("%Y-%m-%d %H:%M")
            if last_trigger[current_key] != mark:
                last_trigger[current_key] = mark

                # 🔄 刷新持仓缓存
                get_account_status()

                # 🔥 合成监控池 = 主流币 + 持仓币 + OI异动币 + ai500
                raw_oi = redis_client.smembers("OI_SYMBOLS") or set()
                oi_symbols = list(raw_oi)
                ai500_symbols = redis_client.lrange("AI500_SYMBOLS", 0, -1)
                pos_symbols = list(position_records)

                monitor_symbols[:] = list(
                    dict.fromkeys(mainstream_symbols + pos_symbols + oi_symbols + ai500_symbols)
                    # dict.fromkeys(mainstream_symbols + pos_symbols)
                )

                print(f"🔍 监控池: {monitor_symbols} (共 {len(monitor_symbols)} 个币)")

                # 下载 K 线
                fetch_all()

                print("📌 所有 K 线下载完成 → 计算指标")
                for sym in monitor_symbols:
                    calculate_signal_single(sym)

                try:
                    # 1️⃣ AI 投喂
                    start_ai = time.perf_counter()
                    ai_res = await push_batch_to_deepseek()
                    # print("🔥 AI 原始返回:", ai_res)
                    end_ai = time.perf_counter()
                    print(f"⏱ AI返回耗时: {round(end_ai - start_ai, 3)} 秒")

                    if ai_res and isinstance(ai_res, list):

                        valid_actions = {
                            "open_long", "open_short",
                            "close_long", "close_short",
                            "reverse",
                            "stop_loss", "take_profit",
                            "update_stop_loss", "update_take_profit",
                            "increase_position", "decrease_position"
                        }

                        # 只保留有效信号
                        exec_list = [sig for sig in ai_res if sig.get("action") in valid_actions]

                        # 2️⃣ 并发下单
                        tasks = [
                            asyncio.create_task(
                                execute_trade_async(
                                    symbol=sig.get("symbol"),
                                    action=sig.get("action"),
                                    stop_loss=sig.get("stop_loss"),
                                    take_profit=sig.get("take_profit"),
                                    position_size=sig.get("position_size") or sig.get("order_value") or sig.get("amount"),
                                    quantity=sig.get("quantity")
                                )
                            )
                            for sig in exec_list
                        ]

                        start_exec = time.perf_counter()
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        end_exec = time.perf_counter()
                        print(f"⏱ 并行下单耗时: {round(end_exec - start_exec, 3)} 秒")

                        # 3️⃣ 异步 TG 推送
                        if exec_list:
                            start_tg = time.perf_counter()
                            if asyncio.iscoroutinefunction(send_tg_trade_signal):
                                await send_tg_trade_signal(exec_list)
                            else:
                                # 同步函数使用线程池
                                await asyncio.to_thread(send_tg_trade_signal, exec_list)
                            end_tg = time.perf_counter()
                            print(f"🟢 执行交易 & 推送TG完成: {exec_list}")
                            print(f"⏱ TG推送耗时: {round(end_tg - start_tg, 3)} 秒")
                    else:
                        print("⚠ AI 未返回有效信号，不推送，不下单")

                finally:
                    # 🧹 清理 Redis 旧 K线
                    valid = set(monitor_symbols)
                    for key in redis_client.keys("historical_data:*"):
                        k = key if isinstance(key, str) else key.decode()
                        parts = k.split(":")
                        if len(parts) == 3:
                            _, symbol, _ = parts
                            if symbol not in valid:
                                redis_client.delete(key)
                                print(f"🗑 清理无效缓存币: {symbol}")

                print("🎯 本轮调度完成\n")

        await asyncio.sleep(1)
