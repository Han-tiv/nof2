# ai500.py
import requests
from threading import Timer
from datetime import datetime
from database import redis_client

# 配置
INTERVAL = 120  # 每2分钟执行一次
REDIS_KEY = "AI500_SYMBOLS"

EXCLUDE_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
OI_ANOMALY_URL = "http://nofxaios.com:30006/api/ai500/list?auth=cm_568c67eae410d912c54c"
OI_TOP_URL = "http://nofxaios.com:30006/api/oi/top-ranking?limit=10&duration=15m&auth=cm_568c67eae410d912c54c"


def _fetch_ai500_symbols():
    """
    从接口获取最新币种列表
    """
    symbols_set = set()
    try:
        # OI 异动（score>70）
        resp = requests.get(OI_ANOMALY_URL, timeout=5)
        coins = resp.json().get("data", {}).get("coins", [])
        for c in coins:
            if c.get("pair") and c.get("score", 0) > 70:
                symbols_set.add(c["pair"])

        # OI Top Ranking
        resp = requests.get(OI_TOP_URL, timeout=5)
        positions = resp.json().get("data", {}).get("positions", [])
        for p in positions:
            if p.get("symbol"):
                symbols_set.add(p["symbol"])

        symbols = [s for s in symbols_set if s not in EXCLUDE_SYMBOLS]
        return symbols

    except Exception as e:
        print(f"❌ ai500获取失败: {e}")
        return []


def _schedule_next():
    """
    启动下一次 Timer（守护线程）
    """
    t = Timer(INTERVAL, update_oi_symbols)
    t.daemon = True
    t.start()


def update_oi_symbols():
    """
    主函数：获取 OI 异动币并更新 Redis
    """
    now = datetime.now()
    # 跳过整5分钟节点
    if now.minute % 5 == 0:
        print(f"⏭️ {now.strftime('%H:%M')} 是整5分钟节点，跳过执行")
    else:
        symbols = _fetch_ai500_symbols()
        if symbols:
            redis_client.delete(REDIS_KEY)
            redis_client.rpush(REDIS_KEY, *symbols)
            print(f"🔥 ai500更新Redis成功: {symbols}")
        else:
            print("⚠ ai500获取为空，Redis不更新")

    # 调度下一次执行
    _schedule_next()
