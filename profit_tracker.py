import time
import json
from threading import Lock
from database import redis_client
from account_positions import get_account_status

REDIS_KEY = "profit:ultra_simple"
_lock = Lock()

def load_state():
    data = redis_client.hgetall(REDIS_KEY)
    if not data:
        return {
            "initial_equity": 0.0,
            "curve": []
        }

    return {
        "initial_equity": float(data.get("initial_equity", 0.0)),
        "curve": json.loads(data.get("curve", "[]"))
    }

def save_state(state):
    redis_client.hset(
        REDIS_KEY,
        mapping={
            "initial_equity": state["initial_equity"],
            "curve": json.dumps(state["curve"])
        }
    )

def update_profit_curve():
    """
    每 15 分钟调用一次
    """
    with _lock:
        state = load_state()
        now = int(time.time() * 1000)

        acc = get_account_status()
        current_equity = (
            float(acc.get("balance", 0.0)) +
            float(acc.get("total_unrealized", 0.0))
        )

        # 第一次运行：记录初始总权益
        if state["initial_equity"] == 0:
            state["initial_equity"] = current_equity
            save_state(state)
            return None

        state["curve"].append({
            "ts": now,
            "equity": round(current_equity, 4),
            "profit": round(current_equity - state["initial_equity"], 4)
        })

        state["curve"] = state["curve"][-1000:]

        save_state(state)
        return state["curve"][-1]

def get_profit_curve():
    return load_state()["curve"]

def get_current_profit():
    state = load_state()
    acc = get_account_status()

    current_equity = (
        float(acc.get("balance", 0.0)) +
        float(acc.get("total_unrealized", 0.0))
    )

    return {
        "equity": round(current_equity, 4),
        "profit": round(current_equity - state["initial_equity"], 4)
    }