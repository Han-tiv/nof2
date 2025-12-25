# ai500.py
import requests
from threading import Timer
from datetime import datetime
from database import redis_client

# 配置
INTERVAL = 600  # 每10分钟执行一次
REDIS_KEY = "AI500_SYMBOLS"

EXCLUDE_SYMBOLS = {"BTCUSDT", "PAXGUSDT"}
LATEST_URL = "https://token.aibtc.vip/latest"

def _fetch_symbols():
    """
    获取所有符合条件的币种列表
    只从最新接口获取，排除 EXCLUDE_SYMBOLS
    """
    symbols_set = set()

    # --- 最新接口 ---
    try:
        resp = requests.get(LATEST_URL, timeout=5)
        coins = resp.json().get("data", {}).get("coins", [])
        for c in coins:
            pair = c.get("pair")
            if pair and pair not in EXCLUDE_SYMBOLS:
                symbols_set.add(pair)
    except Exception as e:
        print(f"❌ latest获取失败: {e}")

    merged_list = sorted(symbols_set)
    return merged_list

def _schedule_next():
    """
    启动下一次 Timer（守护线程）
    """
    t = Timer(INTERVAL, update_oi_symbols)
    t.daemon = True
    t.start()

def update_oi_symbols():
    """
    主函数：获取币种并更新 Redis
    """
    now = datetime.now()

    # ⏭️ 跳过整 1 小时节点（HH:00）
    if now.minute == 0:
        print(f"⏭️ {now.strftime('%H:%M')} 是整点，跳过执行")
    else:
        symbols = _fetch_symbols()
        if symbols:
            redis_client.delete(REDIS_KEY)
            redis_client.rpush(REDIS_KEY, *symbols)
            # print(f"🔥 更新Redis成功: {symbols}")
        else:
            print("⚠ 获取为空，Redis不更新")

    # 调度下一次执行
    _schedule_next()
