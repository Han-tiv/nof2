import json
import numpy as np
import talib
from database import redis_client
from deepseek_batch_pusher import add_to_batch
from config import timeframes, EMA_CONFIG
from datetime import datetime, timezone
from decimal import Decimal, getcontext

# 提高累加精度
getcontext().prec = 30

# ==========================================================
# 🔥 CVD 系列指标计算
# ==========================================================
def compute_cvd_indicators(rows):
    """
    计算 CVD 系列指标，保证跨服务器结果一致
    输入:
        rows: K 线列表，每项包含 TakerBuyVolume 和 TakerSellVolume
    输出:
        dict: 包含 CVD, CVD_MOM, CVD_NORM, CVD_DIVERGENCE, CVD_PEAKFLIP
    """
    cvd = []
    cumulative = Decimal('0')
    closes = [Decimal(str(k["Close"])) for k in rows]

    for k in rows:
        buy = Decimal(str(k.get("TakerBuyVolume", '0')))
        sell = Decimal(str(k.get("TakerSellVolume", '0')))
        cumulative += buy - sell
        cvd.append(cumulative)

    # 累积值
    CVD = cvd[-1]
    CVD_MOM = CVD - cvd[-6] if len(cvd) > 6 else Decimal('0')

    # 归一化
    mn, mx = min(cvd), max(cvd)
    CVD_NORM = (CVD - mn) / (mx - mn) if mx > mn else Decimal('0.5')

    # 分析背离
    price_now = closes[-1]
    price_prev = closes[-6] if len(closes) > 6 else closes[0]
    cvd_prev = cvd[-6] if len(cvd) > 6 else cvd[0]

    if price_now > price_prev and CVD < cvd_prev:
        CVD_DIV = "bearish"
    elif price_now < price_prev and CVD > cvd_prev:
        CVD_DIV = "bullish"
    else:
        CVD_DIV = "neutral"

    # 峰值翻转
    if len(cvd) > 3:
        if cvd[-1] < cvd[-2] and cvd[-2] > cvd[-3]:
            CVD_PEAKFLIP = "top"
        elif cvd[-1] > cvd[-2] and cvd[-2] < cvd[-3]:
            CVD_PEAKFLIP = "bottom"
        else:
            CVD_PEAKFLIP = "none"
    else:
        CVD_PEAKFLIP = "none"

    # 保留固定小数位输出，避免 float 转换引入误差
    return {
        "CVD": CVD.quantize(Decimal('0.01')),
        "CVD_MOM": CVD_MOM.quantize(Decimal('0.01')),
        "CVD_NORM": CVD_NORM.quantize(Decimal('0.000001')),
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
    # ==========================================================
    # 🔥 EMA（按周期动态参数）
    # ==========================================================
    ema_periods = EMA_CONFIG.get(interval, [])
    ema_values = {}
    for p in ema_periods:
        ema_series = talib.EMA(closes, timeperiod=p)
        ema_values[f"EMA_{p}"] = float(ema_series[-1])
        
    ema_trend = "unknown"
    ema_strength = None

    if len(ema_periods) >= 2:
        fast_p = min(ema_periods)
        slow_p = max(ema_periods)

        ema_fast = ema_values.get(f"EMA_{fast_p}")
        ema_slow = ema_values.get(f"EMA_{slow_p}")

        if ema_fast and ema_slow:
            diff = abs(ema_fast - ema_slow) / ema_slow

            if diff < 0.001:
                ema_trend = "flat"
            elif ema_fast > ema_slow:
                ema_trend = "bull"
            else:
                ema_trend = "bear"

            ema_strength = round(diff, 6)
        
    # 🔥 ATR（14周期）
    atr_series = talib.ATR(highs, lows, closes, timeperiod=14)
    atr_current = atr_series[-1]

    # 🔥 ATR MA20（数据不足则跳过）
    atr_valid = atr_series[np.isfinite(atr_series)]

    if atr_valid.size >= 20:
        atr_ma20 = np.nanmean(atr_valid[-20:])
    elif atr_valid.size > 0:
        atr_ma20 = np.nanmean(atr_valid)
    else:
        atr_ma20 = None

    # 🔥 CVD 系列指标
    cvd_pack = compute_cvd_indicators(rows)
    if atr_ma20 and atr_current and atr_ma20 > 0:
        atr_ratio = round(float(atr_current) / float(atr_ma20), 6)
    else:
        atr_ratio = None

    # 汇总指标
    indicators = {
        **cvd_pack,
        **ema_values,
        "EMA_TREND": ema_trend,
        "EMA_TREND_STRENGTH": ema_strength,
        "ATR": float(atr_current) if np.isfinite(atr_current) else None,
        "ATR_MA20": float(atr_ma20) if atr_ma20 is not None else None,
        "ATR_RATIO": atr_ratio,
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

