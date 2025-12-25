import json
import asyncio
import logging
import aiohttp
import html
from decimal import Decimal
import time
import re
from concurrent.futures import ThreadPoolExecutor
from config import CLAUDE_API_KEY, CLAUDE_MODEL, CLAUDE_URL, AI_PROVIDER, timeframes
from database import redis_client
from volume_stats import get_open_interest, get_funding_rate, get_24hr_change
from account_positions import account_snapshot, tp_sl_cache
from trend_alignment import calculate_trend_alignment

_preload_executor = ThreadPoolExecutor(max_workers=12)

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

batch_cache = {}

# ================== 全局 HTTP Session（进程级） ==================
_http_session: aiohttp.ClientSession | None = None

async def init_http_session():
    """
    初始化全局 HTTP Session（只做一次）
    不在 Session 级别设置 timeout，timeout 交给单次 request 控制
    """
    global _http_session

    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
        print("🌐 全局 HTTP Session 已初始化")

async def get_http_session() -> aiohttp.ClientSession:
    if _http_session is None or _http_session.closed:
        raise RuntimeError("HTTP Session 尚未初始化，请先调用 init_http_session()")
    return _http_session

async def close_http_session():
    """
    程序退出时调用，优雅关闭
    """
    global _http_session

    if _http_session is not None:
        await _http_session.close()
        _http_session = None
        print("🛑 全局 HTTP Session 已关闭")

def json_safe_dumps(obj):
    return json.dumps(
        obj,
        ensure_ascii=False,
        default=lambda x: float(x) if isinstance(x, Decimal) else str(x)
    )

# ================== Batch 管理 ==================
def add_to_batch(symbol, interval, indicators=None):
    if symbol not in batch_cache:
        batch_cache[symbol] = {}

    payload = {}
    if indicators is not None:
        payload["indicators"] = indicators

    batch_cache[symbol][interval] = payload

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
        return "You are a crypto short-term trend trader [multi-coin batch signal generator]. "

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
    executor = _preload_executor
    tasks = []

    for symbol, cycles in dataset.items():
        tasks.append(loop.run_in_executor(executor, safe_call, get_funding_rate, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_24hr_change, symbol))
        tasks.append(loop.run_in_executor(executor, safe_call, get_open_interest, symbol))

    completed = await asyncio.gather(*tasks)
    idx = 0
    for symbol, cycles in dataset.items():
        results["funding"][symbol] = completed[idx]; idx += 1
        results["p24"][symbol] = completed[idx]; idx += 1
        results["oi"][symbol] = completed[idx]; idx += 1

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

# ================== JSON 提取（统一版） ==================
def _extract_decision_block(content: str):
    """提取 <decision> 标签内的 JSON 列表，支持 Claude HTML 转义形式"""
    if not content:
        return None

    # 1️⃣ 先把 HTML/Unicode 转义替换回原始符号
    content = html.unescape(content)  # \u003c -> <, \u003e -> >

    match = re.search(r"<decision>([\s\S]*?)</decision>", content, flags=re.I)
    if not match:
        return None

    block = match.group(1).strip()
    try:
        parsed = json.loads(block)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and "action" in x]
        if isinstance(parsed, dict) and "action" in parsed:
            return [parsed]
    except Exception as e:
        logging.warning(f"⚠️ JSON 解析失败: {e}")
        return None

# 同理也可以改 _extract_reasoning_block，解码 HTML 转义
def _extract_reasoning_block(content: str):
    """提取 <reasoning> 标签内容，支持 HTML 转义"""
    if not content:
        return None
    content = html.unescape(content)
    match = re.search(r"<reasoning>([\s\S]*?)</reasoning>", content, flags=re.I)
    if not match:
        return None
    return match.group(1).strip()

def _extract_all_json(content: str):
    """
    尝试提取所有可能的交易信号 JSON，
    兼容 DeepSeek / Gemini / Claude，支持 HTML/Unicode 转义
    """
    if not content:
        return None

    # 1️⃣ 先将 HTML / Unicode 转义解码
    content = html.unescape(content)  # \u003c -> <, \u003e -> >

    results = []

    # 2️⃣ 尝试直接解析整个内容
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict) and "action" in x]
        if isinstance(parsed, dict) and "action" in parsed:
            return [parsed]
    except:
        pass

    # 3️⃣ 尝试从 <decision> 标签中解析
    decision_match = re.search(r"<decision>([\s\S]*?)</decision>", content, flags=re.I)
    if decision_match:
        block = decision_match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict) and "action" in x]
            if isinstance(parsed, dict) and "action" in parsed:
                return [parsed]
        except:
            pass

    # 4️⃣ 匹配单层 JSON 对象的老逻辑，保底解析
    matches = re.findall(r'\{[^{}]*\}', content, flags=re.S)
    for m in matches:
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and "action" in obj:
                results.append(obj)
        except:
            pass

    return results if results else None

# ================== 持仓拆分 ==================
def split_positions_batch(account, dataset_all, max_symbols=5):
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
def split_dataset_by_symbol_limit(dataset: dict, max_symbols=5):
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
def _build_dataset_json(dataset, preloaded=None):
    """构建结构化 JSON 数据"""
    account = account_snapshot
    positions = dataset.get("positions", [])
    balance_info = dataset.get("balance_info") or {
        "balance": account.get("balance"),
        "available": account.get("available"),
        "total_unrealized": account.get("total_unrealized")
    }
    symbols_in_batch = [k for k in dataset.keys() if k not in ("positions", "balance_info")]
    
    # 过滤批次内持仓
    if positions and symbols_in_batch:
        positions = [p for p in positions if p["symbol"] in symbols_in_batch]
    
    output = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        "balance_info": {
            "balance": float(balance_info.get("balance", 0)),
            "available": float(balance_info.get("available", 0)),
            "total_unrealized": float(balance_info.get("total_unrealized", 0))
        },
        "positions": [
            {
                "symbol": p["symbol"],
                "side": "LONG" if float(p["size"]) > 0 else "SHORT",
                "size": abs(float(p["size"])),
                "entry": float(p["entry"]),
                "mark_price": float(p["mark_price"]),
                "pnl": float(p["pnl"]),
                "pnl_pct": round((float(p["mark_price"]) - float(p["entry"])) / float(p["entry"]) * 100, 2)
                           if float(p["size"]) > 0 else 
                           round((float(p["entry"]) - float(p["mark_price"])) / float(p["entry"]) * 100, 2),
                "tp_sl": [f"{o['type']}={o['stopPrice']}" 
                         for o in tp_sl_cache.get(p['symbol'], {}).get("LONG" if float(p["size"]) > 0 else "SHORT", [])]
            }
            for p in positions
        ],
        "markets": {}
    }
    
    # 构建市场数据
    for symbol in symbols_in_batch:
        cycles = dataset[symbol]
        fr = preloaded.get("funding", {}).get(symbol)
        p24 = preloaded.get("p24", {}).get(symbol)
        oi_now = preloaded.get("oi", {}).get(symbol)
        
        market_data = {
            "price": p24['lastPrice'] if p24 else None,
            "24h_high": p24['highPrice'] if p24 else None,
            "24h_low": p24['lowPrice'] if p24 else None,
            "24h_change_pct": p24['priceChangePercent'] if p24 else None,
            "24h_volume_usd": round(p24['quoteVolume'] / 1e6, 2) if p24 else None,
            "funding_rate": fr,
            "open_interest": oi_now,
            "timeframes": {}
        }
        
        for interval in cycles.keys():
            data = cycles[interval]
            ind = data.get("indicators") or {}
            market_data["timeframes"][interval] = {
                "indicators": {
                    k: round(v, 6) if isinstance(v, float) else v
                    for k, v in ind.items()
                }
            }

        output["markets"][symbol] = market_data
    
    return output

def build_llm_user_prompt(market_snapshot: dict) -> str:
    """
    把“约束文字 + JSON”组装成一次 LLM 的 user content
    """
    return f"""
Below is the current account status and market snapshot data (JSON).

This is a formal trading decision request.
Please strictly follow the [System Instructions] to complete the multi-timeframe analysis and output a single, unique trading action.

[Current Account Constraints]
- Available account balance is limited; overtrading is prohibited; capital must grow steadily
- Excessive trading and frequent repetitive entries are strictly prohibited
- If risk or structure is unclear, choosing wait or hold is permitted

[Current Account and Market Data]
<JSON>
{json_safe_dumps(market_snapshot)}
</JSON>
""".strip()

# ================== AIBTC.VIP 批量投喂 ==================
async def _push_single_batch_claude(dataset, preloaded, batch_idx, total_batches):
    """
    使用 AIBTC.VIP 模型进行批次投喂，兼容 Unicode/HTML 转义，保证 signals 完整
    """
    loop = asyncio.get_running_loop()
    json_data = await loop.run_in_executor(None, _build_dataset_json, dataset, preloaded)
    user_prompt = await loop.run_in_executor(None, build_llm_user_prompt, json_data)
    system_prompt = await loop.run_in_executor(None, _read_prompt)

    payload = {
        "model": CLAUDE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0,
        "max_tokens": 8000,
        "stream": False
    }

    max_retries = 3
    base_timeout = 45  # 超时基准秒数

    for attempt in range(max_retries):
        attempt_start = time.perf_counter()
        current_timeout = base_timeout * (attempt + 1)

        try:
            session = await get_http_session()

            async with session.post(
                CLAUDE_URL,
                json=payload,
                headers={"Authorization": f"Bearer {CLAUDE_API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=current_timeout)
            ) as resp:

                status = resp.status

                if status != 200:
                    raise aiohttp.ClientError(f"HTTP {status}")

                raw_text = await resp.text()
                attempt_time = round((time.perf_counter() - attempt_start) * 1000, 2)

                print(
                    f"✅ AIBTC.VIP 批次 {batch_idx} 第{attempt+1}次返回 | "
                    f"{attempt_time}ms | HTTP {status}"
                )

                content = None
                reasoning = None
                signals = []
                raw_json = None
                finish_reason = None

                try:
                    raw_json = json.loads(raw_text)
                    choice = raw_json.get("choices", [{}])[0]
                    content = choice.get("message", {}).get("content")
                    finish_reason = choice.get("finish_reason")
                    reasoning = _extract_reasoning_block(content)

                    if content:
                        from html import unescape
                        content_decoded = unescape(content)
                        signals = _extract_all_json(content_decoded) or []

                except Exception as parse_err:
                    logging.warning(f"⚠️ AIBTC.VIP JSON 解析失败: {parse_err}")

                return {
                    "batch_idx": batch_idx,
                    "formatted_request": user_prompt,  # ⭐ 已安全序列化
                    "content": content,
                    "reasoning": reasoning,
                    "signals": signals,
                    "raw_text": raw_text,
                    "raw_json": raw_json,
                    "finish_reason": finish_reason,
                    "http_status": status,
                    "ts": time.time(),
                    "attempt": attempt + 1,
                    "response_time_ms": attempt_time
                }

        except asyncio.TimeoutError:
            attempt_time = round((time.perf_counter() - attempt_start) * 1000, 2)
            print(f"⏱️ AIBTC.VIP 批次 {batch_idx} 第{attempt+1}次超时 ({attempt_time}ms)")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            else:
                return {
                    "batch_idx": batch_idx,
                    "formatted_request": user_prompt,
                    "signals": [],
                    "raw_text": None,
                    "raw_json": None,
                    "finish_reason": None,
                    "http_status": None,
                    "error": f"批次 {batch_idx} 在{max_retries}次尝试后超时",
                    "ts": time.time(),
                    "attempt": max_retries,
                    "response_time_ms": attempt_time
                }

        except aiohttp.ClientError as e:
            attempt_time = round((time.perf_counter() - attempt_start) * 1000, 2)
            print(f"🌐 Claude 批次 {batch_idx} 第{attempt+1}次网络错误: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            else:
                return {
                    "batch_idx": batch_idx,
                    "formatted_request": user_prompt,
                    "signals": [],
                    "raw_text": None,
                    "raw_json": None,
                    "finish_reason": None,
                    "http_status": None,
                    "error": f"批次 {batch_idx} 网络错误: {e}",
                    "ts": time.time(),
                    "attempt": max_retries,
                    "response_time_ms": attempt_time
                }

        except Exception as e:
            attempt_time = round((time.perf_counter() - attempt_start) * 1000, 2)
            error_msg = f"批次 {batch_idx} 未知错误: {e}"
            print(f"❌ {error_msg}")

            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            else:
                return {
                    "batch_idx": batch_idx,
                    "formatted_request": user_prompt,
                    "signals": [],
                    "raw_text": None,
                    "raw_json": None,
                    "finish_reason": None,
                    "http_status": None,
                    "error": error_msg,
                    "ts": time.time(),
                    "attempt": max_retries,
                    "response_time_ms": attempt_time
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
    positions_batches = split_positions_batch(account, dataset_all, max_symbols=5)
    positions_symbols = [p["symbol"] for batch in positions_batches for p in batch.get("positions", [])]

    # --- 2. 拆分非持仓币种 ---
    symbol_dataset = {k: v for k, v in dataset_all.items() if k not in positions_symbols}
    symbol_batches = split_dataset_by_symbol_limit(symbol_dataset, max_symbols=5)

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
        if AI_PROVIDER == "claude":
            tasks.append(
                _push_single_batch_claude(batch, preloaded, idx+1, len(batches))
            )
        else:
            raise ValueError(f"未知 AI_PROVIDER: {AI_PROVIDER}")

    # --- 6. 执行投喂 ---
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 🆕 添加调试输出
    # print(f"📦 原始 results 类型检查:")
    # for idx, r in enumerate(results):
        # print(f"  批次 {idx+1}: type={type(r).__name__}, value={str(r)[:200]}")

    # 统计超时情况
    success_count = 0
    timeout_count = 0
    total_elapsed_time = 0          # 全部耗时
    success_response_time = 0       # 仅成功耗时

    for r in results:
        if not isinstance(r, dict):
            timeout_count += 1
            continue

        rt = r.get("response_time_ms", 0)
        total_elapsed_time += rt

        if r.get("http_status") == 200:
            success_count += 1
            success_response_time += rt
        elif "超时" in (r.get("error") or ""):
            timeout_count += 1

    valid_results = [r for r in results if isinstance(r, dict)]
    valid_count = len(valid_results)
    overall_avg = total_elapsed_time / valid_count if valid_count else 0

    print(
        f"📊 请求统计: 成功 {success_count}/{len(valid_results)} | "
        f"超时 {timeout_count} | "
        f"整体平均耗时 {overall_avg:.0f}ms | "
        f"成功平均耗时 {success_response_time/success_count if success_count else 0:.0f}ms"
    )

    # --- 7. 保存请求/响应到 Redis，并汇总信号 ---
    for r in results:
        if not isinstance(r, dict):
            continue

        # ✅ 使用 json_safe_dumps 保存 Redis
        if r.get("formatted_request"):
            redis_client.rpush(KEY_REQ, json_safe_dumps({
                "batch_idx": r["batch_idx"],
                "request": r["formatted_request"],
                "timestamp": r["ts"]
            }))
        redis_client.rpush(KEY_RES, json_safe_dumps({
            "batch_idx": r["batch_idx"],
            "signals": r.get("signals", []),
            "reasoning": r.get("reasoning"),
            "content": r.get("content"),
            "raw_text": r.get("raw_text"),
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