# trend_alignment.py

TF_WEIGHTS = {
    "1d": 30,
    "4h": 25,
    "1h": 20,
}

def calculate_trend_alignment(cycles: dict):
    """
    cycles: dataset[symbol] → { interval: { indicators: {...} } }

    return:
    {
        "score": 0~100,
        "direction": "bull" | "bear" | "mixed",
        "details": { interval: trend }
    }
    """

    total_weight = sum(TF_WEIGHTS.values())
    score = 0
    details = {}

    for tf, weight in TF_WEIGHTS.items():
        data = cycles.get(tf)
        if not data:
            continue

        ind = data.get("indicators", {})
        trend = ind.get("EMA_TREND", "flat")
        details[tf] = trend

        if trend == "bull":
            score += weight
        elif trend == "bear":
            score -= weight
        # flat → 0

    # 映射到 0~100
    normalized = round((score + total_weight) / (2 * total_weight) * 100, 2)

    if normalized > 60:
        direction = "bull"
    elif normalized < 40:
        direction = "bear"
    else:
        direction = "mixed"

    return {
        "TREND_ALIGNMENT_SCORE": normalized,
        "TREND_ALIGNMENT_DIRECTION": direction,
        "TREND_ALIGNMENT_DETAIL": details
    }
