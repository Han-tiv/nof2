# payload_builder.py
import json
from typing import Optional
from database import redis_client


def decide_strategy_type(tf4h: dict, tf1h: dict, tf15m: dict) -> str:
    """
    返回策略类型：
      - range_reversal    区间反转（假突破/边界反转）
      - breakout_follow   真突破跟随（区间突破后跟随）
      - trend_pullback    顺势回调（趋势制度下执行）
      - none
    """
    s4 = (tf4h or {}).get("structure", {})
    trend4 = s4.get("trend", "range")
    loc4 = (tf4h or {}).get("range_location", "unknown")
    sig15 = (tf15m or {}).get("signal", "none")

    # 4H 区间制度
    if trend4 == "range":
        if loc4 not in ("near_low", "near_high"):
            return "none"

        if sig15 in ("fake_break_up", "fake_break_down", "choch_reversal"):
            return "range_reversal"

        if sig15 in ("true_break_up", "true_break_down"):
            return "breakout_follow"

        # 其它触发（比如 break_confirmed）也可以按你的风格归类
        if sig15 == "break_confirmed":
            return "breakout_follow"

        return "none"

    # 4H 趋势制度
    if trend4 in ("up", "down"):
        if sig15 in ("break_confirmed", "true_break_up", "true_break_down"):
            return "trend_pullback"
        return "none"

    return "none"

def referee_snapshot(tf4h: dict, tf1h: dict, tf15m: dict) -> dict:
    """
    裁判输出：
      - verdict: ALLOW_TRADE / NO_TRADE
      - reason_code: 机器统计
      - strategy_type: 仅在 ALLOW_TRADE 时给出
      - context: 关键上下文
    """
    verdict = "NO_TRADE"
    reason_code = "UNKNOWN"

    # 4H 必须有效
    if not tf4h or not tf4h.get("structure") or not tf4h["structure"].get("valid"):
        reason_code = "4H_STRUCTURE_INVALID"
        s4 = (tf4h or {}).get("structure", {})
        loc4 = (tf4h or {}).get("range_location", "unknown")
        sig = (tf15m or {}).get("signal", "none") if tf15m else "none"
        return {
            "verdict": verdict,
            "reason_code": reason_code,
            "context": {
                "4h_trend": s4.get("trend"),
                "4h_loc": loc4,
                "4h_pos": (tf4h or {}).get("range_pos"),
                "1h_break": (tf1h or {}).get("structure", {}).get("last_break") if tf1h else None,
                "15m_signal": sig,
            },
        }

    s4 = tf4h["structure"]
    loc4 = tf4h.get("range_location", "unknown")
    sig = (tf15m or {}).get("signal", "none") if tf15m else "none"

    # 规则1：4H 区间中部禁止
    if s4.get("trend") == "range" and loc4 in ("middle", "unknown"):
        reason_code = "4H_RANGE_MIDDLE"
        return {
            "verdict": verdict,
            "reason_code": reason_code,
            "context": {
                "4h_trend": s4.get("trend"),
                "4h_loc": loc4,
                "4h_pos": tf4h.get("range_pos"),
                "1h_break": (tf1h or {}).get("structure", {}).get("last_break") if tf1h else None,
                "15m_signal": sig,
            },
        }

    # 规则2：1H 过渡期过滤（choch）
    if tf1h and tf1h.get("structure") and tf1h["structure"].get("valid"):
        br1 = tf1h["structure"].get("last_break", "none")
        if br1.startswith("choch_") and loc4 not in ("near_low", "near_high"):
            reason_code = "1H_CHOCH_TRANSITION"
            return {
                "verdict": verdict,
                "reason_code": reason_code,
                "context": {
                    "4h_trend": s4.get("trend"),
                    "4h_loc": loc4,
                    "4h_pos": tf4h.get("range_pos"),
                    "1h_break": br1,
                    "15m_signal": sig,
                },
            }

    # 规则3：15m 必须有触发器
    if sig == "none":
        reason_code = "15M_NO_TRIGGER"
        return {
            "verdict": verdict,
            "reason_code": reason_code,
            "context": {
                "4h_trend": s4.get("trend"),
                "4h_loc": loc4,
                "4h_pos": tf4h.get("range_pos"),
                "1h_break": (tf1h or {}).get("structure", {}).get("last_break") if tf1h else None,
                "15m_signal": sig,
            },
        }

    # ✅ 通过：允许交易 + 给策略类型
    strategy_type = decide_strategy_type(tf4h, tf1h, tf15m)
    if strategy_type == "none":
        # 触发器虽有，但无法归类为可执行策略 => 仍拒绝（更安全）
        reason_code = "STRATEGY_NOT_CLASSIFIED"
        return {
            "verdict": verdict,
            "reason_code": reason_code,
            "context": {
                "4h_trend": s4.get("trend"),
                "4h_loc": loc4,
                "4h_pos": tf4h.get("range_pos"),
                "1h_break": (tf1h or {}).get("structure", {}).get("last_break") if tf1h else None,
                "15m_signal": sig,
            },
        }

    verdict = "ALLOW_TRADE"
    reason_code = "PASSED"
    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "strategy_type": strategy_type,
        "context": {
            "4h_trend": s4.get("trend"),
            "4h_loc": loc4,
            "4h_pos": tf4h.get("range_pos"),
            "1h_break": (tf1h or {}).get("structure", {}).get("last_break") if tf1h else None,
            "15m_signal": sig,
        },
    }

def _get_snapshot(symbol: str, tf: str) -> Optional[dict]:
    key = f"signal_snapshot:{symbol}:{tf}"
    v = redis_client.get(key)
    return json.loads(v) if v else None

def build_unified_payload(symbol: str) -> Optional[dict]:
    tf4h = _get_snapshot(symbol, "4h")
    tf1h = _get_snapshot(symbol, "1h")
    tf15m = _get_snapshot(symbol, "15m")

    if not tf4h or not tf1h or not tf15m:
        return None

    ref = referee_snapshot(tf4h, tf1h, tf15m)

    payload = {
        "symbol": symbol,
        "timestamp": tf15m.get("timestamp") or tf15m.get("ts") or None,
        "ready": True,
        "tf_4h": tf4h,
        "tf_1h": tf1h,
        "tf_15m": tf15m,
        "referee": ref,
    }
    return payload

def save_unified_payload(symbol: str, ttl_sec: int = 300) -> Optional[dict]:
    payload = build_unified_payload(symbol)
    if not payload:
        return None

    redis_client.set(
        f"unified_payload:{symbol}",
        json.dumps(payload, ensure_ascii=False),
        ex=ttl_sec,
    )
    return payload
