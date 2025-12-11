import json
import aiohttp
import asyncio
import logging
import time
import re
from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL
from database import redis_client
from volume_stats import (
    calc_volume_compare, get_open_interest, get_funding_rate, get_24hr_change, calc_smart_sentiment,
    get_oi_history, get_top_position_ratio, get_top_account_ratio, get_global_account_ratio)
from account_positions import account_snapshot, tp_sl_cache

required_intervals = ["1d", "4h", "1h", "15m", "5m"]
batch_cache = {}

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

def add_to_batch(symbol, interval, klines, indicators):
    if symbol not in batch_cache:
        batch_cache[symbol] = {}
    batch_cache[symbol][interval] = {"klines": klines, "indicators": indicators}


def _is_ready_for_push():
    for _, cycles in batch_cache.items():
        for tf in required_intervals:
            if tf not in cycles:
                return False
    return True

def sentiment_to_signal(score):
    if score >= 85:
        return "🚨 极端过热 | 警惕顶部反转"
    if score >= 70:
        return "🟢 牛势强劲 |"
    if score >= 50:
        return "⚪ 中性震荡 | 耐心等待突破"
    if score >= 30:
        return "🟡 恐慌缓解"
    return "🔥 极度恐慌"

def _read_prompt():
    try:
        with open("prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "你是一名专业量化策略分析引擎，请严格输出 JSON 数组或 JSON 对象形式的交易信号。"

def _format_dataset(dataset):
    start_time = time.time()  # ⏱ 记录开始时间
    text = []
    
    # 🧠 账户资金 & 持仓信息
    account = account_snapshot

    text.append("========= 📌 当前账户资金状态 =========")
    text.append(f"💰 总权益 Balance: {round(account['balance'], 4)}")
    text.append(f"🔓 可用余额 Available: {round(account['available'], 4)}")
    text.append(f"📉 总未实现盈亏 PnL: {round(account['total_unrealized'], 4)}")

    if account["positions"]:
        text.append("\n📌 当前持仓:")

        for p in account["positions"]:
            amt = float(p["size"])
            entry = float(p["entry"])
            mark = float(p["mark_price"])
            pnl = float(p["pnl"])
            lev = int(p["leverage"])

            side_icon = "🟢 多" if amt > 0 else "🔴 空"

            # 盈亏百分比
            if entry > 0:
                pnl_pct = round((mark - entry) / entry * 100, 2) if amt > 0 else round((entry - mark) / entry * 100, 2)
            else:
                pnl_pct = 0

            # 🔥 构建持仓基本信息
            line = (
                f"{p['symbol']} | {side_icon} | 数量 {abs(amt)} | "
                f"入场 {entry} → 当前价格 {mark} | 💵 盈亏 {pnl} ({pnl_pct}%)"
            )

            # 🔥 添加 TP/SL 信息
            pos_side = "LONG" if amt > 0 else "SHORT"
            tp_sl_orders = tp_sl_cache.get(p['symbol'], {}).get(pos_side, [])
            if tp_sl_orders:
                tp_sl_lines = [f"{o['type']}={o['stopPrice']}" for o in tp_sl_orders]
                line += " | TP/SL: " + ", ".join(tp_sl_lines)
            else:
                line += " | TP/SL: 无"

            text.append(line)

    else:
        text.append("\n📌 当前无持仓")

    for symbol, cycles in dataset.items():
        text.append(f"\n============ {symbol} 多周期行情快照 ============")
        # 🔥 统一获取一次基础数据（避免重复API调用）
        fr = get_funding_rate(symbol)
        p24 = get_24hr_change(symbol)

        if p24:
            text.append(f"• 24h 涨跌幅: {p24['priceChangePercent']}% → 最新 {p24['lastPrice']} (高 {p24['highPrice']} / 低 {p24['lowPrice']})")
            text.append(f"• 24h 成交额: {round(p24['quoteVolume']/1e6, 2)}M USD")
            
        text.append(f"💰 当前资金费率 Funding Rate: {fr if fr else '未知'}")
        
        for interval, data in cycles.items():
            kl = data["klines"]
            ind = data["indicators"]
            last = kl[-1]

            text.append(f"\n--- {interval} ---")
            text.append(f"📌 当前周期收盘价格: {last['Close']}")
            
            period = interval  # 周期动态跟随 interval

            # ⭐ 深度资金指标（周期自适应 + 自动缓存，无重复请求）
            try:
                oi_hist = get_oi_history(symbol, period, limit=10)
                big_pos = get_top_position_ratio(symbol, period, limit=1)
                big_acc = get_top_account_ratio(symbol, period, limit=1)
                global_acc = get_global_account_ratio(symbol, period, limit=1)
            except Exception:
                oi_hist = big_pos = big_acc = global_acc = None
                
            oi = get_open_interest(symbol)
            text.append(f"🧱 当前永续未平仓量 OI: {oi if oi else '未知'}")
            if oi_hist:
                arr = [round(i["openInterest"], 2) for i in oi_hist][-10:]
                text.append(f"•最新10条历史 OI 数据趋势: {arr}")

            if big_pos:
                text.append(f"• 大户持仓量多空比: {big_pos[-1]['ratio']} (多 {big_pos[-1]['long']}, 空 {big_pos[-1]['short']})")

            if big_acc:
                text.append(f"• 大户账户数多空比: {big_acc[-1]['ratio']} (多 {big_acc[-1]['long']}, 空 {big_acc[-1]['short']})")

            if global_acc:
                text.append(f"• 全网多空人数比: {global_acc[-1]['ratio']} (多 {global_acc[-1]['long']}, 空 {global_acc[-1]['short']})")
            
            # 🔥 CVD 与 ATR（你 indicators.py 生成的）
            text.append("\n📌 CVD 指标:")
            for key in ["CVD", "CVD_MOM", "CVD_DIVERGENCE", "CVD_PEAKFLIP", "CVD_NORM"]:
                if key in ind:
                    text.append(f"{key}: {ind[key]}")

            # =========================
            # ⭐ Smart Sentiment 多因子评分 + 操作信号
            # =========================
            try:
                sentiment = calc_smart_sentiment(symbol, period)
                score = sentiment["sentiment_score"]
                fac = sentiment["factors"]
                signal_text = sentiment_to_signal(score)

                text.append("\n📌 Smart Sentiment Score:")
                # text.append(f"🎯 情绪评分: {score}/100  →  {signal_text}")
                text.append(f"🎯 情绪评分: {score}/100")

                text.append(f"📊 分项因子(归一化):")
                text.append(f"· OI情绪: {fac['open_interest']}")
                text.append(f"· Funding情绪: {fac['funding_rate']}")
                text.append(f"· 大户情绪: {fac['big_whales']}")
                text.append(f"· 散户反向情绪: {fac['retail_inverse']}")
                text.append(f"· 成交量情绪: {fac['volume_emotion']}")

            except Exception as e:
                text.append("\n📌 Smart Sentiment Score: 计算失败")
                logging.warning(f"Sentiment calc error: {e}")

            text.append("\n📌 波动率指标:")
            if "ATR" in ind:
                text.append(f"ATR: {ind['ATR']}")

            # 🔥 主动买卖量分析
            last_buy = float(kl[-1]["TakerBuyVolume"])
            last_sell = float(kl[-1]["TakerSellVolume"])
            last_vol = float(kl[-1]["Volume"])
            ratio = round(last_buy / last_vol * 100, 2) if last_vol > 0 else 0

            text.append("\n📌 主动交易量:")
            text.append(f"主动买入量(Taker Buy): {last_buy}")
            text.append(f"主动卖出量(Taker Sell): {last_sell}")
            text.append(f"主动买入占比: {ratio}%")

            # 🔥 成交量对比（每个周期独立）
            vol_info = calc_volume_compare(kl)
            if vol_info:
                text.append("\n📌 成交量对比:")
                text.append(f"当前成交量: {vol_info['current_volume']}")
                text.append(f"100根均量: {vol_info['average_volume_100']}")
                text.append(f"当前/均量比值: {vol_info['ratio']}")
                
            opens = [k["Open"] for k in kl]
            highs = [k["High"] for k in kl]
            lows = [k["Low"] for k in kl]
            closes = [k["Close"] for k in kl]
            volumes = [k["Volume"] for k in kl]

            text.append("\n📌 K线数组格式从旧 → 新:")
            text.append(f"open: {opens}")
            text.append(f"high: {highs}")
            text.append(f"low: {lows}")
            text.append(f"close: {closes}")
            text.append(f"volume: {volumes}")
            
    # 🔥 调试的时候使用
    # text.append("\n🧠 现在请分析并输出决策（思维链 + JSON）")
    text.append("\n🧠 现在请分析并输出决策（简洁思维链 < 150 字 + JSON）")

    end_time = time.time()  # ⏱ 记录结束时间
    elapsed = end_time - start_time
    print(f"[_format_dataset] 函数执行耗时: {elapsed:.3f} 秒")  # 打印耗时
    return "\n".join(text)

# 🔍 优先解析 <decision> 标签内部 JSON
def _extract_decision_block(content: str):
    match = re.search(r"<decision>([\s\S]*?)</decision>", content, flags=re.I)
    if not match:
        return None
    block = match.group(1).strip()
    try:
        parsed = json.loads(block)
        if isinstance(parsed, list):
            return parsed
    except:
        pass
    return None

def _extract_all_json(content: str):
    """
    支持：
      {…}{…}
      [{…},{…}]
      单个 {…}
    只保留 action 存在的 JSON
    """
    results = []

    # 1) JSON 数组
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and "action" in x]
    except:
        pass

    # 2) 使用正则提取多个 { }
    matches = re.findall(r'\{[^{}]*\}', content, flags=re.S)
    for m in matches:
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and "action" in obj:
                results.append(obj)
        except:
            pass

    return results if results else None

async def push_batch_to_deepseek():
    if not _is_ready_for_push():
        return None

    dataset = batch_cache.copy()
    batch_cache.clear()

    timestamp = int(time.time() * 1000)

    loop = asyncio.get_running_loop()

    # ===========================
    # 🧠 1) 阻塞 CPU 的任务放进线程池避免卡住事件循环
    # ===========================
    formatted_dataset = await loop.run_in_executor(None, _format_dataset, dataset)
    system_prompt = await loop.run_in_executor(None, _read_prompt)

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": formatted_dataset}
        ],
        "temperature": 0.1,
        "max_tokens": 8000,  # 🔥 增加token限制，给推理足够空间
        "stream": False
    }

    # push request history
    redis_client.lpush(KEY_REQ, json.dumps({
        "timestamp": timestamp,
        "request": formatted_dataset
    }, ensure_ascii=False))

    start = time.perf_counter()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_URL,
                json=payload,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
            ) as resp:

                raw = await resp.text()
                print("DeepSeek 已返回", time.time())
                cost = round((time.perf_counter() - start) * 1000, 2)
                
                # ===============================
                # 🧠 2) JSON 解析也放入线程池
                # ===============================
                def parse_ai_response(raw):
                    try:
                        root = json.loads(raw)
                        content = root["choices"][0]["message"]["content"]
                    except Exception as e:
                        return None

                    # 优先 <decision>
                    d = _extract_decision_block(content)
                    if d:
                        return d
                    
                    # fallback
                    return _extract_all_json(content)

                signals = await loop.run_in_executor(None, parse_ai_response, raw)

                # save response
                redis_client.lpush(KEY_RES, json.dumps({
                    "timestamp": timestamp,
                    "response_raw": raw,
                    "response_json": signals,
                    "status_code": resp.status,
                    "cost_ms": cost
                }, ensure_ascii=False))

                print(f"\n⏱ DeepSeek 响应耗时: {cost} ms   HTTP: {resp.status}")
                # print("🧠 AI 解析后信号:", signals)

                return signals

    except Exception as e:
        logging.error(f"❌ DeepSeek 调用失败：{e}")
        return None
