import time
import threading
from notifier import message_worker
from database import clear_redis
from kline_fetcher import fetch_all
from indicators import calculate_signal
from config import monitor_symbols, timeframes
import asyncio
from scheduler import schedule_loop_async
from deepseek_batch_pusher import _is_ready_for_push, push_batch_to_deepseek, close_global_session
from ai500 import update_oi_symbols
from oi import scheduler
import subprocess
import signal
import os

async def run_async():
    # 并行启动多个异步任务
    await asyncio.gather(
        scheduler(),           # OI 异动扫描
        schedule_loop_async()  # 原来的调度循环
    )

def main():
    clear_redis()
    threading.Thread(target=message_worker, daemon=True).start()

    # ===== 启动 ai500 2分钟定时任务 =====
    print("⏳ 启动 OI 监控定时任务 (2分钟一次, 跳过整5分钟节点)")
    update_oi_symbols()  # 初次调用，内部会自循环

    print("⏳ 启动异步调度循环")
    try:
        asyncio.run(run_async())

    except KeyboardInterrupt:
        print("\n⚠ 捕获 Ctrl+C → 准备退出...")

    finally:
        # 关闭 DeepSeek 全局 session
        try:
            asyncio.run(close_global_session())
            print("✅ DeepSeek 全局 session 已关闭")
        except Exception as e:
            print(f"❌ 关闭 DeepSeek session 失败: {e}")

        print("👋 程序已退出")
        
if __name__ == "__main__":
    # os.environ['http_proxy'] = 'http://127.0.0.1:7890'
    # os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7890'

    # os.environ['https_proxy'] = 'http://127.0.0.1:7890'
    # os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7890'
    main()
