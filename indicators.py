# indicators.py
import json
import numpy as np
import talib
from database import redis_client
from deepseek_batch_pusher import add_to_batch
from config import timeframes, EMA_CONFIG, STRUCTURE_PARAMS
from market_structure import MarketStructure
from payload_builder import save_unified_payload


# ==========================================================
# 区间位置：支持 above_range / below_range
# ==========================================================
def calc_range_location(close: float, range_low: float, range_high: float) -> dict:
    if close is None or range_low is None or range_high is None:
        return {"pos": None, "location": "unknown", "out_of_range": False}
    if range_high <= range_low:
        return {"pos": None, "location": "unknown", "out_of_range": False}

    if close < range_low:
        return {"pos": 0.0, "location": "below_range", "out_of_range": True}
    if close > range_high:
        return {"pos": 1.0, "location": "above_range", "out_of_range": True}

    pos = (close - range_low) / (range_high - range_low)
    pos = max(0.0, min(1.0, float(pos)))

    if pos <= 0.2:
        loc = "near_low"
    elif pos >= 0.8:
        loc = "near_high"
    else:
        loc = "middle"

    return {"pos": pos, "location": loc, "out_of_range": False

            }


# ==========================================================
# 结构分析器：按周期初始化
# ==========================================================
STRUCTURE_CONFIG = {
    tf: MarketStructure(**params)
    for tf, params in STRUCTURE_PARAMS.items()
}


# ==========================================================
# 将单周期结果快照写入 Redis（供聚合器统一裁判/投喂GPT）
# ==========================================================
def save_signal_snapshot(symbol: str, interval: str, indicators: dict, ttl_sec: int = 600):
    key = f"signal_snapshot:{symbol}:{interval}"
    redis_client.set(key, json.dumps(indicators, ensure_ascii=False), ex=ttl_sec)


# ==========================================================
# 读取 TF 快照（用于 15m signal 受“制度/位置”约束）
# ==========================================================
def get_tf_snapshot(symbol: str, tf: str):
    try:
        v = redis_client.get(f"signal_snapshot:{symbol}:{tf}")
        return json.loads(v) if v else None
    except Exception:
        return None


# ==========================================================
# range_break 分类：假突破 / 真突破（15m 用 4H 箱体边界判断）
# ==========================================================
def classify_range_break_15m(rows_15m, range_low: float, range_high: float, atr_15m: float | None) -> str:
    """
    返回：
      - "none"
      - "fake_break_up" / "fake_break_down"
      - "true_break_up" / "true_break_down"

    规则（轻量版）：
      - 用最近 3 根 close：
        * 上一根出界，当前回到区间内 => fake_break_*
        * 当前出界，且连续两根都出界 => true_break_*
        * 当前出界，且超出距离 >= ATR * 0.35 => true_break_*
        * 其它 => none（等待确认）
    """
    if range_low is None or range_high is None or range_high <= range_low:
        return "none"
    if rows_15m is None or len(rows_15m) < 3:
        return "none"

    closes = [float(r["Close"]) for r in rows_15m]
    c1, c2, c3 = closes[-3], closes[-2], closes[-1]

    def side(c: float) -> str:
        if c > range_high:
            return "up"
        if c < range_low:
            return "down"
        return "in"

    s1, s2, s3 = side(c1), side(c2), side(c3)

    # 上一根出界，当前回到区间 => 假突破
    if s2 in ("up", "down") and s3 == "in":
        return f"fake_break_{s2}"

    # 当前出界 => 判断是否站稳
    if s3 in ("up", "down"):
        # 连续两根出界 => 真突破
        if s2 == s3:
            return f"true_break_{s3}"

        # 单根出界：看是否超出足够距离（用 ATR 尺度）
        if atr_15m is not None and atr_15m > 0:
            dist = (c3 - range_high) if s3 == "up" else (range_low - c3)
            if dist >= atr_15m * 0.35:
                return f"true_break_{s3}"

        return "none"

    return "none"


# ==========================================================
# 15m 触发器：受 4H 制度/位置约束 + 假/真突破分类
# ==========================================================
def calc_15m_signal(rows_15m, structure_15m: dict, out_of_range_15m: bool, atr_15m: float | None,
                    tf4h_snapshot: dict | None) -> str:
    """
    返回：
      - none
      - fake_break_up/down
      - true_break_up/down
      - break_confirmed   （趋势里 bos_up/bos_down）
      - choch_reversal    （边界处 choch_up/choch_down 提示）
    """
    if not structure_15m or not structure_15m.get("valid"):
        return "none"

    lb15 = structure_15m.get("last_break", "none")

    # 没有 4H 快照：保守处理（只认 bos）
    if not tf4h_snapshot or not tf4h_snapshot.get("structure") or not tf4h_snapshot["structure"].get("valid"):
        if lb15 in ("bos_up", "bos_down"):
            return "break_confirmed"
        return "none"

    s4 = tf4h_snapshot["structure"]
    trend4 = s4.get("trend", "range")
    loc4 = tf4h_snapshot.get("range_location", "unknown")

    # 4H 区间：必须在边界才允许触发
    if trend4 == "range":
        if loc4 not in ("near_low", "near_high"):
            return "none"

        # 用 4H 的箱体边界来判真假突破
        range_low_4h = s4.get("range_low")
        range_high_4h = s4.get("range_high")

        br = classify_range_break_15m(rows_15m, range_low_4h, range_high_4h, atr_15m)
        if br != "none":
            return br

        # 边界+15m BOS：确认突破（补充）
        if lb15 in ("bos_up", "bos_down"):
            return "break_confirmed"

        # 边界+15m CHoCH：反转提示（可作为区间反转触发之一）
        if lb15 in ("choch_up", "choch_down"):
            return "choch_reversal"

        return "none"

    # 4H 趋势：允许 15m BOS 作为触发
    if lb15 in ("bos_up", "bos_down"):
        return "break_confirmed"

    return "none"


def pack_klines(rows, limit=20, include_v=True):
    """
    rows: [{"Timestamp":..., "Open":..., "High":..., "Low":..., "Close":..., "Volume":...}, ...]
    输出紧凑格式，便于投喂：[{t,o,h,l,c,v}, ...]
    """
    if not rows:
        return []

    cut = rows[-limit:] if len(rows) > limit else rows
    out = []
    for r in cut:
        k = {
            "t": int(r["Timestamp"]),
            "o": float(r["Open"]),
            "h": float(r["High"]),
            "l": float(r["Low"]),
            "c": float(r["Close"]),
        }
        if include_v:
            # 你的 redis 行里字段名不确定：可能是 "Volume" 或 "Vol" 或 "QuoteVolume"
            v = r.get("Volume", r.get("Vol", r.get("volume", None)))
            if v is not None:
                k["v"] = float(v)
        out.append(k)
    return out


# ==========================================================
# 🔥 计算单周期指标
# ==========================================================
def calculate_signal(symbol: str, interval: str):
    rkey = f"historical_data:{symbol}:{interval}"
    data = redis_client.hgetall(rkey)
    if not data:
        return

    rows = sorted(data.items(), key=lambda x: int(x[0]))
    rows = [{"Timestamp": int(ts), **json.loads(v)} for ts, v in rows]
    if len(rows) < 5:
        return

    # ------------------------------
    # OHLC arrays
    # ------------------------------
    closes = np.array([float(k["Close"]) for k in rows], dtype=np.float64)
    highs = np.array([float(k["High"]) for k in rows], dtype=np.float64)
    lows = np.array([float(k["Low"]) for k in rows], dtype=np.float64)

    last = rows[-1]
    last_ts = last["Timestamp"]
    last_open = float(last["Open"])
    last_high = float(last["High"])
    last_low = float(last["Low"])
    last_close = float(last["Close"])

    # ------------------------------
    # EMA
    # ------------------------------
    ema_periods = EMA_CONFIG.get(interval, [])
    ema_values = {}
    for p in ema_periods:
        ema_series = talib.EMA(closes, timeperiod=p)
        ema_values[f"EMA_{p}"] = float(ema_series[-1]) if np.isfinite(ema_series[-1]) else None

    # ------------------------------
    # ATR
    # ------------------------------
    atr_series = talib.ATR(highs, lows, closes, timeperiod=14)
    atr_current = float(atr_series[-1]) if np.isfinite(atr_series[-1]) else None

    atr_valid = atr_series[np.isfinite(atr_series)]
    if atr_valid.size >= 20:
        atr_ma20 = float(np.nanmean(atr_valid[-20:]))
    elif atr_valid.size > 0:
        atr_ma20 = float(np.nanmean(atr_valid))
    else:
        atr_ma20 = None

    atr_ratio = None
    if atr_current is not None and last_close != 0.0:
        atr_ratio = float(atr_current / last_close)

    # ------------------------------
    # ✅ 市场结构
    # ------------------------------
    ms = STRUCTURE_CONFIG.get(interval)
    structure = ms.analyze(rows) if ms else {"valid": False, "reason": "no_analyzer"}

    # ------------------------------
    # ✅ 区间位置（用本周期结构的 range_low/range_high）
    # ------------------------------
    range_pos = None
    range_loc = "unknown"
    out_of_range = False

    if structure and structure.get("valid"):
        rh = structure.get("range_high")
        rl = structure.get("range_low")
        if rh is not None and rl is not None:
            loc_info = calc_range_location(last_close, rl, rh)
            range_pos = loc_info["pos"]
            range_loc = loc_info["location"]
            out_of_range = loc_info["out_of_range"]

    # ------------------------------
    # ✅ 事件型K线（客观可复核，不输出形态结论）
    # ------------------------------
    total = last_high - last_low
    body = abs(last_close - last_open)
    upper = last_high - max(last_open, last_close)
    lower = min(last_open, last_close) - last_low

    candle_stats = {
        "body_ratio": float(body / total) if total > 0 else None,
        "upper_wick_ratio": float(upper / total) if total > 0 else None,
        "lower_wick_ratio": float(lower / total) if total > 0 else None,
    }

    # 默认：只在 15m 输出 events（控体积）；4h/1h 不输出（仅 stats）
    candle_events = {}

    # ------------------------------
    # ✅ 15m signal：假/真突破 + 制度约束（只读一次 4H snapshot）
    # ------------------------------
    signal = "none"
    tf4h_snapshot = None
    klines = None

    if interval == "15m":
        tf4h_snapshot = get_tf_snapshot(symbol, "4h")

        signal = calc_15m_signal(
            rows_15m=rows,
            structure_15m=structure,
            out_of_range_15m=out_of_range,
            atr_15m=atr_current,
            tf4h_snapshot=tf4h_snapshot,
        )

        # 15m candle_events：仅当结构字段存在时才计算
        if structure and structure.get("valid"):
            last_hl = structure.get("last_HL")
            last_lh = structure.get("last_LH")
            if last_hl is not None:
                candle_events["close_above_last_HL"] = bool(last_close > float(last_hl))
            if last_lh is not None:
                candle_events["close_below_last_LH"] = bool(last_close < float(last_lh))

        # 15m: 复用真假突破分类（基于 4H 箱体）
        if tf4h_snapshot and tf4h_snapshot.get("structure", {}).get("valid"):
            s4 = tf4h_snapshot["structure"]
            rl4, rh4 = s4.get("range_low"), s4.get("range_high")
            candle_events["range_break_4h_box"] = classify_range_break_15m(rows, rl4, rh4, atr_current)

        # ✅ 15m: 打包最近 N 根 K 线
        klines = pack_klines(rows, limit=20, include_v=True)

    # ------------------------------
    # ✅ 输出
    # ------------------------------
    indicators = {
        "symbol": symbol,
        "tf": interval,
        "timestamp": last_ts,

        "close": last_close,
        "atr_ratio": atr_ratio,
        "atr": atr_current,
        "atr_ma20": atr_ma20,

        "ema": ema_values,

        "candle_stats": candle_stats,
        # 仅 15m 带 events（控 payload）；其他周期 events 为空字典也可
        "candle_events": candle_events if interval == "15m" else {},

        "structure": structure,
        "range_location": range_loc,
        "range_pos": range_pos,
        "out_of_range": out_of_range,

        "signal": signal,
    }

    if interval == "15m":
        indicators["klines"] = klines

    # ------------------------------
    # ✅ 1) 写快照
    # ------------------------------
    save_signal_snapshot(symbol, interval, indicators)

    # ------------------------------
    # ✅ 2) 进入 batch
    # ------------------------------
    add_to_batch(symbol, interval, indicators)

    # ------------------------------
    # ✅ 3) 只在 15m 更新时聚合 unified payload + 裁判日志
    # ------------------------------
    if interval == "15m":
        payload = save_unified_payload(symbol)
        if payload:
            ref = payload["referee"]
            _ = ref.get("strategy_type")


def calculate_signal_single(symbol: str):
    for tf in timeframes:
        calculate_signal(symbol, tf)
