import threading
import asyncio
from notifier import message_worker
from database import clear_redis
from kline_fetcher import fetch_all
from indicators import calculate_signal
from config import monitor_symbols, timeframes
from scheduler import schedule_loop_async
from api_history import run_api_server
from ai500 import update_oi_symbols
from deepseek_batch_pusher import init_http_session, close_http_session

async def main_async():
    # ⭐⭐⭐ 1. 启动时初始化全局 HTTP Session（只一次）
    await init_http_session()

    try:
        # 并行启动异步调度循环（你现在只有一个，也保持不变）
        await asyncio.gather(
            schedule_loop_async()
        )
    finally:
        # ⭐⭐⭐ 2. 程序退出时优雅关闭 Session
        await close_http_session()

def main():
    # 🚀 启动 FastAPI 前端服务
    threading.Thread(
        target=run_api_server,
        daemon=True
    ).start()

    print("🌐 API History 服务已启动: http://localhost:8600")

    # 清空 Redis
    clear_redis()

    # 启动消息推送线程
    threading.Thread(target=message_worker, daemon=True).start()

    # 启动 ai500 定时任务
    update_oi_symbols()

    print("⏳ 启动异步调度循环")

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n⚠ 捕获 Ctrl+C → 准备退出...")
        print("👋 程序已退出")

if __name__ == "__main__":
    main()
