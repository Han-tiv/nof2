from notifier import queue_message
from database import redis_client
import json

def _get_latest_5m_close(symbol):
    key = f"historical_data:{symbol}:5m"
    try:
        if not redis_client.exists(key):
            return None
        fields = redis_client.hkeys(key)
        if not fields:
            return None
        latest_ts = max(int(x) for x in fields)
        raw = redis_client.hget(key, latest_ts)
        if not raw:
            return None
        return json.loads(raw).get("Close")
    except Exception:
        return None

async def send_tg_trade_signal(ai_results):
    if not ai_results:
        print("⚠ AI 返回空，不推送 TG")
        return

    if isinstance(ai_results, dict):
        ai_results = [ai_results]

    for res in ai_results:
        action = res.get("action")
        symbol = res.get("symbol")

        if action not in ("open_long", "open_short", "close_long", "close_short", "reverse", "increase_position", "decrease_position"):
            continue

        sym_display = symbol or "（未提供）"
        price = res.get("entry")
        price_display = price if price is not None else "未知"

        msg = (
            f"🚨 AIBTC.VIP 交易信号\n\n"
            f"📌 交易对: {sym_display}\n"
            f"🎯 动作: {action}\n"
        )

        if res.get("entry") is not None:
            msg += f"📍 最新价: {res['entry']}\n"

        if res.get("stop_loss") is not None:
            msg += f"🛑 止损: {res['stop_loss']}\n"

        if res.get("take_profit") is not None:
            msg += f"🎯 止盈: {res['take_profit']}\n"

        if res.get("reason"):
            msg += f"\n🧠 原因:\n{res['reason']}\n"

        # print(f"📌 生成推送内容:\n{msg}")
        queue_message(msg, topic="Trading-signals")
