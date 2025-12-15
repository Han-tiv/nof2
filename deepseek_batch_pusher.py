import json
import asyncio
import logging
import aiohttp
from decimal import Decimal
import time
import re
from concurrent.futures import ThreadPoolExecutor
from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL,
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_PROJECT,
    AI_PROVIDER
)
from database import redis_client
from volume_stats import (
    calc_volume_compare, get_open_interest, get_funding_rate, get_24hr_change, calc_smart_sentiment,
    get_oi_history, get_top_position_ratio, get_top_account_ratio, get_global_account_ratio
)
from account_positions import account_snapshot, tp_sl_cache
from trend_alignment import calculate_trend_alignment
import google.genai as genai

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

batch_cache = {}
required_intervals = ["1d", "4h", "1h", "15m", "5m"]

_global_connector = None
_global_session = None
_global_session_lock = asyncio.Lock()

# ================== Session 管理 ==================
async def close_global_session():
    global _global_session
    async with _global_session_lock:
        if _global_session and not _global_session.closed:
            await _global_session.close()
            _global_session = None
            print("✅ 全局 aiohttp session 已关闭")
            
async def get_global_session():
    global _global_session
    async with _global_session_lock:
        if _global_session is None or _global_session.closed:
            _global_session = aiohttp.ClientSession(
                connector=get_global_connector(),
                timeout=aiohttp.ClientTimeout(total=60)
            )
    return _global_session

def get_global_connector():
    global _global_connector
    if _global_connector is None:
        _global_connector = aiohttp.TCPConnector(
            limit=200,
            limit_per_host=100,
            ttl_dns_cache=300,
            force_close=False
        )
    return _global_connector

def json_safe_dumps(obj):
    return json.dumps(
        obj,
        ensure_ascii=False,
        default=lambda x: float(x) if isinstance(x, Decimal) else str(x)
    )

# ================== Batch 管理 ==================
def add_to_batch(symbol, interval, klines, indicators):
    if symbol not in batch_cache:
        batch_cache[symbol] = {}
    batch_cache[symbol][interval] = {"klines": klines, "indicators": indicators}

def _is_ready_for_push():
    """
    检查 batch_cache 是否有至少一个币种有数据。
    放宽要求，不再强制每个周期都必须完整。
    """
    if not batch_cache:
        print("⚠️ batch_cache 为空，无法投喂")
        return False

    ready_symbols = []
    for symbol, cycles in batch_cache.items():
        if cycles:  # 至少有一个周期数据
            ready_symbols.append(symbol)
        else:
            print(f"⚠️ {symbol} 缺少任何周期数据")

    if not ready_symbols:
        print("⚠️ 没有币种满足投喂条件")
        return False

    print(f"✅ 准备投喂的币种: {ready_symbols}")
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
    except Exception:
        return "你是一名专业量化策略分析引擎，请严格输出 JSON 数组或 JSON 对象形式的交易信号。"

# ================== API 预加载 ==================
async def preload_all_api(dataset):
    results = {
        "funding": {}, "p24": {}, "oi": {}, "sentiment": {},
        "oi_hist": {}, "big_pos": {}, "big_acc": {}, "global_acc": {},
    }

    def safe_call(func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            return None

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=20)
    tasks = []

    for symbol, cycles in dataset.items():
        tasks.append(loop.run_in_executor(executor, safe_call, get_funding_rate, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_24hr_change, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_open_interest, symbol))

        for interval in cycles.keys():
            key = f"{symbol}:{interval}"
            tasks.append(loop.run_in_executor(executor, safe_call, get_oi_history, symbol, interval, 10))
            tasks.append(loop.run_in_executor(executor, safe_call, get_top_position_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, get_top_account_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, get_global_account_ratio, symbol, interval, 1))
            tasks.append(loop.run_in_executor(executor, safe_call, calc_smart_sentiment, symbol, interval))

    completed = await asyncio.gather(*tasks)
    idx = 0
    for symbol, cycles in dataset.items():
        results["funding"][symbol] = completed[idx]; idx += 1
        results["p24"][symbol] = completed[idx]; idx += 1
        results["oi"][symbol] = completed[idx]; idx += 1
        for interval in cycles.keys():
            key = f"{symbol}:{interval}"
            results["oi_hist"][key] = completed[idx]; idx += 1
            results["big_pos"][key] = completed[idx]; idx += 1
            results["big_acc"][key] = completed[idx]; idx += 1
            results["global_acc"][key] = completed[idx]; idx += 1
            results["sentiment"][key] = completed[idx]; idx += 1

    return results

async def preload_all_api_global(dataset_all):
    unified_dataset = {}
    for batch in dataset_all:
        for symbol, cycles in batch.items():
            if symbol not in unified_dataset:
                unified_dataset[symbol] = {}
            for interval, data in cycles.items():
                if interval not in unified_dataset[symbol]:
                    unified_dataset[symbol][interval] = data
    print(f"🔄 全局预加载合并了 {len(unified_dataset)} 个币种")
    return await preload_all_api(unified_dataset)

# ================== JSON 提取 ==================
def _extract_decision_block(content: str):
    match = re.search(r"<decision>([\s\S]*?)</decision>", content, flags=re.I)
    if not match: return None
    block = match.group(1).strip()
    try:
        parsed = json.loads(block)
        if isinstance(parsed, list): return parsed
    except: pass
    return None

def _extract_all_json(content: str):
    results = []
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and "action" in x]
    except: pass
    matches = re.findall(r'\{[^{}]*\}', content, flags=re.S)
    for m in matches:
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and "action" in obj:
                results.append(obj)
        except: pass
    return results if results else None

# ================== 持仓拆分 ==================
def split_positions_batch(account, dataset_all, max_symbols=2):
    """
    拆分持仓批次，每个批次只包含一部分持仓币种 + positions + balance_info
    支持部分币种缺失数据
    """
    positions = account.get("positions", [])
    if not positions:
        print("⚠️ 当前账户无持仓")
        return []

    balance_info = {
        "balance": account.get("balance"),
        "available": account.get("available"),
        "total_unrealized": account.get("total_unrealized")
    }

    # 持仓币种对应数据
    symbol_data = {}
    for p in positions:
        symbol = p["symbol"]
        if symbol in dataset_all and dataset_all[symbol]:  # 只加入有数据的币种
            symbol_data[symbol] = dataset_all[symbol]
        else:
            print(f"⚠️ 持仓币种 {symbol} 缺少数据，将跳过")

    symbols = list(symbol_data.keys())
    if not symbols:
        print("⚠️ 所有持仓币种数据缺失，跳过持仓拆分")
        return []

    batches = []
    for i in range(0, len(symbols), max_symbols):
        batch_symbols = symbols[i:i+max_symbols]
        batch = {"positions": positions, "balance_info": balance_info}
        for s in batch_symbols:
            batch[s] = symbol_data[s]
        batches.append(batch)

    print(f"✅ 拆分持仓批次数量: {len(batches)}")
    return batches

# ================== 批次拆分 ==================
def split_dataset_by_symbol_limit(dataset: dict, max_symbols=2):
    """
    拆分非持仓币种批次，每批最多 max_symbols 个币种
    支持部分币种缺少数据
    """
    batches = []

    if "positions" in dataset:
        batches.append({"positions": dataset["positions"], "balance_info": dataset.get("balance_info", {})})

    symbols = [k for k in dataset.keys() if k != "positions"]
    items = [(k, dataset[k]) for k in symbols if dataset[k]]  # 只保留有数据的币种

    if not items:
        print("⚠️ 非持仓币种数据为空，跳过拆分")
        return batches

    for i in range(0, len(items), max_symbols):
        batch = dict(items[i:i + max_symbols])
        batches.append(batch)

    print(f"✅ 拆分非持仓批次数量: {len(batches)}")
    return batches

# ================== 数据格式化 ==================
def _format_dataset(dataset, preloaded=None):
    start_time = time.time()
    text = []
    append = text.append
    account = account_snapshot
    balance_info = dataset.get("balance_info") or {
        "balance": account.get("balance"),
        "available": account.get("available"),
        "total_unrealized": account.get("total_unrealized")
    }

    append("========= 📌 当前账户资金状态 =========")
    append(f"💰 总权益 Balance: {round(balance_info.get('balance', 0), 4)}")
    # append(f"🔓 可用余额 Available: {round(balance_info.get('available', 0), 4)}")
    # append(f"📉 总未实现盈亏 PnL: {round(balance_info.get('total_unrealized', 0), 4)}")

    # --- 只显示批次内的持仓币 ---
    positions = dataset.get("positions", [])
    symbols_in_batch = [k for k in dataset.keys() if k not in ("positions", "balance_info")]
    if positions and symbols_in_batch:
        positions = [p for p in positions if p["symbol"] in symbols_in_batch]

    if positions:
        append("\n📌 当前持仓:")
        for p in positions:
            amt = float(p["size"])
            entry = float(p["entry"])
            mark = float(p["mark_price"])
            pnl = float(p["pnl"])
            side_icon = "🟢 多" if amt > 0 else "🔴 空"
            pnl_pct = round((mark - entry) / entry * 100, 2) if amt > 0 else round((entry - mark) / entry * 100, 2) if entry > 0 else 0
            line = (
                f"{p['symbol']} | {side_icon} | 数量 {abs(amt)} | "
                f"入场 {entry} → 当前价格 {mark} | 💵 盈亏 {pnl} ({pnl_pct}%)"
            )
            pos_side = "LONG" if amt > 0 else "SHORT"
            tp_sl_orders = tp_sl_cache.get(p['symbol'], {}).get(pos_side, [])
            if tp_sl_orders:
                tp_sl_lines = [f"{o['type']}={o['stopPrice']}" for o in tp_sl_orders]
                line += " | TP/SL: " + ", ".join(tp_sl_lines)
            else:
                line += " | TP/SL: 无"
            append(line)
    else:
        append("\n📌 当前无持仓")

    # --- 遍历批次内币种 ---
    for symbol in symbols_in_batch:
        cycles = dataset[symbol]
        append(f"\n============ {symbol} 多周期行情快照 ============")
        fr     = preloaded.get("funding", {}).get(symbol)
        p24    = preloaded.get("p24", {}).get(symbol)
        oi_now = preloaded.get("oi", {}).get(symbol)
        trend_score = calculate_trend_alignment(cycles)

        if p24:
            append(f"• 24h 涨跌幅: {p24['priceChangePercent']}% → 最新 {p24['lastPrice']} (高 {p24['highPrice']} / 低 {p24['lowPrice']})")
            append(f"• 24h 成交额: {round(p24['quoteVolume'] / 1e6, 2)}M USD")
        append(f"💰 当前资金费率 Funding Rate: {fr if fr is not None else '未知'}")
        append("\n📌 趋势一致性 (Trend Alignment):")
        append(f"📐 趋势方向: {trend_score['TREND_ALIGNMENT_DIRECTION']}")
        append(f"📈 综合得分: {trend_score['TREND_ALIGNMENT_SCORE']}/100")
        append(f"🧩 周期明细: {trend_score['TREND_ALIGNMENT_DETAIL']}")

        for interval in required_intervals:
            if interval not in cycles:
                continue
            data = cycles[interval]
            kl = data["klines"]
            ind = data["indicators"]
            last = kl[-1]
            append(f"\n--- {interval} ---")
            append(f"📌 当前周期收盘价格: {last['Close']}")

            ema_keys = sorted([k for k in ind.keys() if k.startswith("EMA_") and k[4:].isdigit()], key=lambda x: int(x.split("_")[1]))
            if ema_keys:
                append("\n📌 趋势指标（EMA）:")
                for k in ema_keys:
                    append(f"{k}: {round(ind[k], 6)}")
                if "EMA_TREND" in ind:
                    trend = ind["EMA_TREND"]
                    strength = ind.get("EMA_TREND_STRENGTH")
                    append(f"📈 EMA 趋势判断: {trend}" + (f" | 强度: {round(strength, 4)}" if strength else ""))

            append("\n📌 波动率指标:")
            append(f"ATR14: {ind.get('ATR', '数据不足')}")
            append(f"ATR14 20周期均值: {ind.get('ATR_MA20', '数据不足')}")
            append(f"ATR比率: {ind.get('ATR_RATIO', '数据不足')}")

            key = f"{symbol}:{interval}"
            oi_hist    = preloaded.get("oi_hist", {}).get(key)
            big_pos    = preloaded.get("big_pos", {}).get(key)
            big_acc    = preloaded.get("big_acc", {}).get(key)
            global_acc = preloaded.get("global_acc", {}).get(key)
            sentiment  = preloaded.get("sentiment", {}).get(key)

            append(f"\n🧱 当前永续未平仓量 OI: {oi_now if oi_now is not None else '未知'}")
            if oi_hist: arr = [round(i["openInterest"], 2) for i in oi_hist][-10:]; append(f"• 最新10条历史 OI 数据趋势: {arr}")
            if big_pos: b = big_pos[-1]; append(f"• 大户持仓量多空比: {b['ratio']} (多 {b['long']}, 空 {b['short']})")
            if big_acc: b = big_acc[-1]; append(f"• 大户账户数多空比: {b['ratio']} (多 {b['long']}, 空 {b['short']})")
            if global_acc: g = global_acc[-1]; append(f"• 全网多空人数比: {g['ratio']} (多 {g['long']}, 空 {g['short']})")
            if sentiment:
                try:
                    score = sentiment["sentiment_score"]
                    fac = sentiment["factors"]
                    append("\n📌 Smart Sentiment Score:")
                    append(f"🎯 情绪评分: {score}/100")
                    append("📊 分项因子(归一化):")
                    append(f"· OI情绪: {fac['open_interest']}")
                    append(f"· Funding情绪: {fac['funding_rate']}")
                    append(f"· 大户情绪: {fac['big_whales']}")
                    append(f"· 散户反向情绪: {fac['retail_inverse']}")
                    append(f"· 成交量情绪: {fac['volume_emotion']}")
                except Exception:
                    append("\n📌 Smart Sentiment Score: 计算失败")
            else:
                append("\n📌 Smart Sentiment Score: 计算失败")

            append("\n📌 CVD 指标:")
            for k in ["CVD", "CVD_MOM", "CVD_DIVERGENCE", "CVD_PEAKFLIP", "CVD_NORM"]:
                if k in ind:
                    append(f"{k}: {ind[k]}")

            last_buy  = float(last.get("TakerBuyVolume", 0))
            last_sell = float(last.get("TakerSellVolume", 0))
            last_vol  = float(last.get("Volume", 0))
            ratio     = round(last_buy / last_vol * 100, 2) if last_vol > 0 else 0
            append("\n📌 主动交易量:")
            append(f"主动买入量(Taker Buy): {last_buy}")
            append(f"主动卖出量(Taker Sell): {last_sell}")
            append(f"主动买入占比: {ratio}%")

            vol_info = calc_volume_compare(kl)
            if vol_info:
                append("\n📌 成交量对比:")
                append(f"当前成交量: {vol_info['current_volume']}")
                append(f"100根均量: {vol_info['average_volume_100']}")
                append(f"当前/均量比值: {vol_info['ratio']}")

            opens   = [k["Open"] for k in kl]
            highs   = [k["High"] for k in kl]
            lows    = [k["Low"] for k in kl]
            closes  = [k["Close"] for k in kl]
            volumes = [k["Volume"] for k in kl]
            append("\n📌 K线数组格式从旧 → 新:")
            append(f"open: {opens}")
            append(f"high: {highs}")
            append(f"low: {lows}")
            append(f"close: {closes}")
            append(f"volume: {volumes}")

    append("\n🧠 请直接输出交易决策，不需要推理过程，只需JSON格式：")
    append("指令：只输出<decision>标签内的JSON数组，不要任何解释文字。")
    print(f"[_format_dataset] 执行耗时: {time.time() - start_time:.3f} 秒")
    return "\n".join(text)

# ================== DeepSeek 批次投喂 ==================
async def _push_single_batch_deepseek(dataset, preloaded, batch_idx, total_batches, session):
    loop = asyncio.get_running_loop()
    formatted_dataset = await loop.run_in_executor(None, _format_dataset, dataset, preloaded)
    system_prompt = await loop.run_in_executor(None, _read_prompt)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system_prompt},{"role": "user", "content": formatted_dataset}],
        "temperature": 0.1,
        "max_tokens": 8000,
        "stream": False
    }
    start = time.perf_counter()
    try:
        async with session.post(DEEPSEEK_URL, json=payload, headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            raw = await resp.text()
            print(f"✅ DeepSeek 批次 {batch_idx} 完成 | {round((time.perf_counter()-start)*1000,2)} ms | HTTP {resp.status}")
            try:
                root = json.loads(raw)
                content = root["choices"][0]["message"]["content"]
                signals = _extract_decision_block(content) or _extract_all_json(content) or []
            except: signals = []
            return {"batch_idx": batch_idx, "formatted_request": formatted_dataset, "signals": signals, "raw_response": raw, "ts": time.time(), "http_status": resp.status}
    except Exception as e:
        logging.error(f"❌ DeepSeek 批次 {batch_idx} 失败: {e}")
        return {"batch_idx": batch_idx, "formatted_request": formatted_dataset, "signals": [], "raw_response": str(e), "ts": time.time(), "http_status": None, "error": str(e)}

# ================== Gemini 批次投喂 ==================
async def _push_single_batch_gemini(dataset, preloaded, batch_idx, total_batches):
    loop = asyncio.get_running_loop()
    formatted_dataset = await loop.run_in_executor(None, _format_dataset, dataset, preloaded)
    system_prompt = await loop.run_in_executor(None, _read_prompt)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": formatted_dataset}
        ]

        # 使用 generate_content 而非 chat.create
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=system_prompt + "\n\n" + formatted_dataset,  # 合并成单条字符串
            config={
                "temperature": 0.1,
                "max_output_tokens": 8000
            }
        )
        content = resp.text
        signals = _extract_decision_block(content) or _extract_all_json(content) or []

        print(f"✅ Gemini 批次 {batch_idx}/{total_batches} 完成 | HTTP 200")
        return {
            "batch_idx": batch_idx,
            "formatted_request": formatted_dataset,
            "signals": signals,
            "raw_response": content,
            "ts": time.time(),
            "http_status": 200
        }

    except Exception as e:
        return {
            "batch_idx": batch_idx,
            "formatted_request": formatted_dataset,
            "signals": [],
            "raw_response": str(e),
            "ts": time.time(),
            "http_status": None,
            "error": str(e)
        }

# ================== 通用批量投喂 ==================
async def push_batch_to_ai():
    if not _is_ready_for_push():
        return None

    start_total = time.perf_counter()  # 记录总开始时间

    dataset_all = batch_cache.copy()
    batch_cache.clear()
    all_signals = []

    account = account_snapshot

    # --- 1. 拆分持仓批次 ---
    positions_batches = split_positions_batch(account, dataset_all, max_symbols=2)
    positions_symbols = [p["symbol"] for batch in positions_batches for p in batch.get("positions", [])]

    # --- 2. 拆分非持仓币种 ---
    symbol_dataset = {k: v for k, v in dataset_all.items() if k not in positions_symbols}
    symbol_batches = split_dataset_by_symbol_limit(symbol_dataset, max_symbols=2)

    # --- 3. 合并所有批次 ---
    batches = positions_batches + symbol_batches

    # --- 4. 预加载，只针对批次里的币种 ---
    preloaded_batches = []
    for batch in batches:
        symbols_only = {k: v for k, v in batch.items() if k not in ("positions", "balance_info")}
        preloaded = await preload_all_api(symbols_only) if symbols_only else {}
        preloaded_batches.append(preloaded)

    # --- 5. 创建投喂任务 ---
    tasks = []
    for idx, batch in enumerate(batches):
        preloaded = preloaded_batches[idx]
        if AI_PROVIDER == "deepseek":
            session = await get_global_session()
            tasks.append(_push_single_batch_deepseek(batch, preloaded, idx+1, len(batches), session))
        else:
            tasks.append(_push_single_batch_gemini(batch, preloaded, idx+1, len(batches)))

    # --- 6. 执行投喂 ---
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # --- 7. 保存请求/响应到 Redis，并汇总信号 ---
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("formatted_request"):
            redis_client.rpush(KEY_REQ, json_safe_dumps({
                "batch_idx": r["batch_idx"],
                "request": r["formatted_request"],
                "timestamp": r["ts"]
            }))
        redis_client.rpush(KEY_RES, json_safe_dumps({
            "batch_idx": r["batch_idx"],
            "signals": r.get("signals", []),
            "raw_response": r.get("raw_response"),
            "timestamp": r["ts"],
            "http_status": r.get("http_status"),
            "error": r.get("error")
        }))
        all_signals.extend(r.get("signals", []))

    end_total = time.perf_counter()
    print(f"📊 请求统计: 投喂批次 {len(results)} | 总耗时 {round((end_total - start_total), 2)} 秒")

    return all_signals if all_signals else None

# 别名保留
push_batch_to_deepseek = push_batch_to_ai