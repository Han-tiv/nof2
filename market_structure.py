# market_structure.py
from typing import List, Dict, Optional, Tuple

class MarketStructure:
    """
    结构识别（pivot -> HH/HL/LH/LL -> 趋势 -> BOS/CHoCH）

    生产版改进点（相对你当前版本）：
    1) Pivot 改为“允许同价，但只认窗口内最靠右的极值”（避免同价双顶/双底导致 pivot 过少或重复）
    2) 清洗 pivots：同一根K线同时出现H/L时保留更“极端”的那个；连续同类型pivot去重保留更极端者
    3) 趋势判定：用最近 N 组高低点变化做“投票”（更稳，不只看最后一组）
    4) 区间边界：range_high/range_low 用最近 K 个 pivot 的极值（比“最后一个 pivot”更贴近箱体）
    5) BOS/CHoCH：趋势里 CHoCH 用最后 HL/LH；BOS 用最近 HH/LL；区间 BOS 用 range_high/range_low
    """

    def __init__(
        self,
        swing_size: int = 10,
        keep_pivots: int = 12,
        trend_vote_lookback: int = 3,  # 趋势投票看最近几次高低点变化（建议 3）
        range_pivot_k: int = 3,        # 区间边界取最近几个 pivot 极值（建议 3）
    ):
        self.swing_size = swing_size
        self.keep_pivots = keep_pivots
        self.trend_vote_lookback = trend_vote_lookback
        self.range_pivot_k = range_pivot_k

    # ==========================================================
    # 内部工具：pivot 检测（允许同价，但只认窗口内最靠右的极值）
    # ==========================================================
    def _pivot_high(self, highs: List[float], idx: int) -> bool:
        s = self.swing_size
        if idx < s or idx + s >= len(highs):
            return False
        window = highs[idx - s : idx + s + 1]
        h = highs[idx]
        m = max(window)
        if h != m:
            return False
        # 只认“最靠右”的最大值：若窗口内最大值最后一次出现的位置不是中心，则不认
        last_pos = max(i for i, v in enumerate(window) if v == m)
        return last_pos == s

    def _pivot_low(self, lows: List[float], idx: int) -> bool:
        s = self.swing_size
        if idx < s or idx + s >= len(lows):
            return False
        window = lows[idx - s : idx + s + 1]
        l = lows[idx]
        m = min(window)
        if l != m:
            return False
        # 只认“最靠右”的最小值
        last_pos = max(i for i, v in enumerate(window) if v == m)
        return last_pos == s

    # ==========================================================
    # pivot 清洗：同index冲突 & 连续同类型去重
    # ==========================================================
    def _resolve_same_index_conflict(
        self,
        pivots: List[Tuple[str, int, float]],
        highs: List[float],
        lows: List[float],
    ) -> List[Tuple[str, int, float]]:
        """
        若同一 index 同时被标为 H 与 L（极端波动K可能发生），只保留更“极端”的那个：
        简化规则：保留振幅更极端的一端（优先保留 H；你也可以按策略改成保留 L 或按趋势偏好选择）
        """
        by_idx: Dict[int, List[Tuple[str, int, float]]] = {}
        for p in pivots:
            by_idx.setdefault(p[1], []).append(p)

        out: List[Tuple[str, int, float]] = []
        for idx in sorted(by_idx.keys()):
            ps = by_idx[idx]
            if len(ps) == 1:
                out.append(ps[0])
                continue

            # 理论上同价+最右规则后很少发生，但保底处理：
            # 默认优先保留 H（你可按需求调整）
            pick = None
            for p in ps:
                if p[0] == "H":
                    pick = p
                    break
            if pick is None:
                # 没有 H 就保留第一个
                pick = ps[0]
            out.append(pick)

        return out

    def _dedupe_consecutive_same_type(
        self, pivots: List[Tuple[str, int, float]]
    ) -> List[Tuple[str, int, float]]:
        """
        连续同类型 pivot 去重：保留更“极端”的那个
        - 连续 H：保留价格更高的 H
        - 连续 L：保留价格更低的 L
        """
        if not pivots:
            return pivots
        out = [pivots[0]]
        for p in pivots[1:]:
            last = out[-1]
            if p[0] != last[0]:
                out.append(p)
                continue

            # same type
            if p[0] == "H":
                if p[2] > last[2]:
                    out[-1] = p
            else:
                if p[2] < last[2]:
                    out[-1] = p
        return out

    # ==========================================================
    # 结构点 tag（HH/HL/LH/LL）
    # ==========================================================
    def _tag_structure(self, pivots: List[Tuple[str, int, float]]) -> List[Dict]:
        last_high: Optional[float] = None
        last_low: Optional[float] = None

        points: List[Dict] = []
        for ptype, idx, price in pivots:
            if ptype == "H":
                if last_high is None:
                    tag = "HH"
                else:
                    tag = "HH" if price >= last_high else "LH"
                last_high = price
            else:
                if last_low is None:
                    tag = "HL"
                else:
                    tag = "HL" if price >= last_low else "LL"
                last_low = price

            points.append({"type": ptype, "tag": tag, "price": price, "index": idx})

        return points

    # ==========================================================
    # 趋势判定（投票法：更稳）
    # ==========================================================
    def _classify_trend(self, points: List[Dict]) -> str:
        highs = [p["price"] for p in points if p["type"] == "H"]
        lows = [p["price"] for p in points if p["type"] == "L"]

        n = self.trend_vote_lookback
        if len(highs) < n + 1 or len(lows) < n + 1:
            return "range"

        up_votes = 0
        down_votes = 0

        # 看最近 n 次变化（例如 n=3，则比较 [-3,-2],[-2,-1],[-1,0] 的变化）
        for i in range(-n, 0):
            # highs
            if highs[i] > highs[i - 1]:
                up_votes += 1
            elif highs[i] < highs[i - 1]:
                down_votes += 1
            # lows
            if lows[i] > lows[i - 1]:
                up_votes += 1
            elif lows[i] < lows[i - 1]:
                down_votes += 1

        # 阈值：n=3 时，总票数最多 6，>=3 表示有明显方向性
        if up_votes >= 3 and up_votes > down_votes:
            return "up"
        if down_votes >= 3 and down_votes > up_votes:
            return "down"
        return "range"

    def _last_point(self, points: List[Dict], ptype: str, tag: Optional[str] = None) -> Optional[Dict]:
        for p in reversed(points):
            if p["type"] != ptype:
                continue
            if tag is None or p["tag"] == tag:
                return p
        return None

    # ==========================================================
    # 区间边界：用最近 K 个 pivot 的极值
    # ==========================================================
    def _range_bounds(self, points: List[Dict]) -> Tuple[Optional[float], Optional[float]]:
        k = self.range_pivot_k
        highs = [p["price"] for p in points if p["type"] == "H"]
        lows = [p["price"] for p in points if p["type"] == "L"]

        range_high = None
        range_low = None

        if highs:
            if len(highs) >= k:
                range_high = max(highs[-k:])
            else:
                range_high = highs[-1]

        if lows:
            if len(lows) >= k:
                range_low = min(lows[-k:])
            else:
                range_low = lows[-1]

        return range_high, range_low

    # ==========================================================
    # 主分析函数
    # ==========================================================
    def analyze(self, rows: List[Dict]) -> Dict:
        min_len = self.swing_size * 2 + 1
        if len(rows) < min_len:
            return {"valid": False, "reason": "not_enough_rows", "need": min_len, "have": len(rows)}

        highs = [float(k["High"]) for k in rows]
        lows = [float(k["Low"]) for k in rows]
        closes = [float(k["Close"]) for k in rows]

        raw_pivots: List[Tuple[str, int, float]] = []

        # 1) 找 pivot highs / lows（允许同index都进raw，后面清洗）
        for i in range(len(rows)):
            if self._pivot_high(highs, i):
                raw_pivots.append(("H", i, highs[i]))
            if self._pivot_low(lows, i):
                raw_pivots.append(("L", i, lows[i]))

        if len(raw_pivots) < 4:
            return {"valid": False, "reason": "not_enough_pivots_raw", "pivots_found": len(raw_pivots)}

        # 2) pivot 清洗
        pivots = self._resolve_same_index_conflict(raw_pivots, highs, lows)
        pivots = sorted(pivots, key=lambda x: x[1])
        pivots = self._dedupe_consecutive_same_type(pivots)

        if len(pivots) < 4:
            return {"valid": False, "reason": "not_enough_pivots_clean", "pivots_used": len(pivots)}

        # 3) 只保留最近 pivots（防结构过期）
        pivots = pivots[-self.keep_pivots :]

        # 4) 标注 HH/HL/LH/LL
        structure_points = self._tag_structure(pivots)

        # 5) 趋势判断（投票法）
        trend = self._classify_trend(structure_points)

        # 6) 区间边界（用于区间 BOS）
        range_high, range_low = self._range_bounds(structure_points)

        # 7) BOS / CHoCH 检测（基于最后 close）
        last_close = closes[-1]
        last_break = "none"

        # 最近 swing（用于回显/调试）
        last_swing_high = self._last_point(structure_points, "H")
        last_swing_low = self._last_point(structure_points, "L")

        # 结构点（用于 choch）
        last_HL = self._last_point(structure_points, "L", "HL")
        last_LH = self._last_point(structure_points, "H", "LH")

        # 趋势延续 BOS 阈值（用最近 HH / LL）
        last_HH = self._last_point(structure_points, "H", "HH")
        last_LL = self._last_point(structure_points, "L", "LL")

        if trend == "up":
            # CHoCH：跌破最后 HL
            if last_HL and last_close < last_HL["price"]:
                last_break = "choch_down"
            # BOS：突破最近 HH（趋势延续信号）
            elif last_HH and last_close > last_HH["price"]:
                last_break = "bos_up"

        elif trend == "down":
            # CHoCH：突破最后 LH
            if last_LH and last_close > last_LH["price"]:
                last_break = "choch_up"
            # BOS：跌破最近 LL（趋势延续信号）
            elif last_LL and last_close < last_LL["price"]:
                last_break = "bos_down"

        else:
            # 区间：突破区间边界（更合理）
            if range_high is not None and last_close > range_high:
                last_break = "bos_up"
            elif range_low is not None and last_close < range_low:
                last_break = "bos_down"

        # 8) 输出摘要
        bias = 1 if trend == "up" else -1 if trend == "down" else 0

        return {
            "valid": True,
            "trend": trend,               # up / down / range
            "bias": bias,                 # +1 / 0 / -1

            # 区间边界（箱体上下沿，更适合假突破/边界交易）
            "range_high": range_high,
            "range_low": range_low,

            # swing 边界（最近一个 pivot，高频调试用）
            "swing_high": last_swing_high["price"] if last_swing_high else None,
            "swing_low": last_swing_low["price"] if last_swing_low else None,

            # 结构点（更适合 CHoCH 判定）
            "last_HL": last_HL["price"] if last_HL else None,
            "last_LH": last_LH["price"] if last_LH else None,
            "last_HH": last_HH["price"] if last_HH else None,
            "last_LL": last_LL["price"] if last_LL else None,

            "last_break": last_break,     # bos_up / bos_down / choch_up / choch_down / none

            # 最近关键结构点（用于可视化/调试）
            "structure_points": structure_points[-6:],

            # 便于你做监控
            "meta": {
                "swing_size": self.swing_size,
                "keep_pivots": self.keep_pivots,
                "trend_vote_lookback": self.trend_vote_lookback,
                "range_pivot_k": self.range_pivot_k,
                "pivots_found": len(raw_pivots),
                "pivots_used": len(pivots),
                "rows_used": len(rows),
            },
        }
