import json
import numpy as np
import talib
from database import redis_client
from deepseek_batch_pusher import add_to_batch
from config import timeframes
from datetime import datetime, timezone

# ==========================================================
# 🔥 CVD 系列指标计算
# ==========================================================
def compute_cvd_indicators(rows):
    cvd = []
    cumulative = 0
    closes = [float(k["Close"]) for k in rows]

    for k in rows:
        buy = float(k.get("TakerBuyVolume", 0))
        sell = float(k.get("TakerSellVolume", 0))
        cumulative += buy - sell
        cvd.append(cumulative)

    CVD = cvd[-1]
    CVD_MOM = CVD - cvd[-6] if len(cvd) > 6 else 0

    mn, mx = min(cvd), max(cvd)
    CVD_NORM = (CVD - mn) / (mx - mn) if mx > mn else 0.5

    price_now = closes[-1]
    price_prev = closes[-6] if len(closes) > 6 else closes[0]
    cvd_prev = cvd[-6] if len(cvd) > 6 else cvd[0]

    if price_now > price_prev and CVD < cvd_prev:
        CVD_DIV = "bearish"
    elif price_now < price_prev and CVD > cvd_prev:
        CVD_DIV = "bullish"
    else:
        CVD_DIV = "neutral"

    if len(cvd) > 3:
        if cvd[-1] < cvd[-2] and cvd[-2] > cvd[-3]:
            CVD_PEAKFLIP = "top"
        elif cvd[-1] > cvd[-2] and cvd[-2] < cvd[-3]:
            CVD_PEAKFLIP = "bottom"
        else:
            CVD_PEAKFLIP = "none"
    else:
        CVD_PEAKFLIP = "none"

    return {
        "CVD": float(CVD),
        "CVD_MOM": float(CVD_MOM),
        "CVD_NORM": float(round(CVD_NORM, 6)),
        "CVD_DIVERGENCE": CVD_DIV,
        "CVD_PEAKFLIP": CVD_PEAKFLIP,
    }

# ==========================================================
# 🔥 计算单周期指标
# ==========================================================
def calculate_signal(symbol, interval):
    rkey = f"historical_data:{symbol}:{interval}"
    data = redis_client.hgetall(rkey)
    if not data:
        return

    rows = sorted(data.items(), key=lambda x: int(x[0]))
    rows = [{"Timestamp": int(ts), **json.loads(v)} for ts, v in rows]

    # if len(rows) < 120:
        # print(f"⚠ {symbol} {interval} 数据不足，无法计算指标\n")
        # return

    # 🔥 ATR（唯一保留的传统指标）
    closes = np.array([float(k["Close"]) for k in rows], dtype=np.float64)
    highs = np.array([float(k["High"]) for k in rows], dtype=np.float64)
    lows = np.array([float(k["Low"]) for k in rows], dtype=np.float64)
    atr = talib.ATR(highs, lows, closes, timeperiod=14)[-1]

    # 🔥 CVD 系列指标
    cvd_pack = compute_cvd_indicators(rows)

    indicators = {
        **cvd_pack,
        "ATR": float(atr),
    }

    # 仅投喂最近 10 根 K 线
    last_klines = rows[-20:]
    add_to_batch(symbol, interval, last_klines, indicators)
    # print(f"📌 {symbol} {interval} 指标已添加进 {interval} 批量队列\n")

    # ===== 打印最近 10 根 K 线 =====
    # print(f"📄 {symbol} {interval} 最近 10 根K线：")
    # for k in last_klines:
        # ts = datetime.fromtimestamp(k['Timestamp'] / 1000).strftime('%Y-%m-%d %H:%M')
        # print(f"{ts} → O:{k['Open']} H:{k['High']} L:{k['Low']} C:{k['Close']} V:{k['Volume']}")
    # print("")   # 空行美化

def calculate_signal_single(symbol):
    for tf in timeframes:
        calculate_signal(symbol, tf)

