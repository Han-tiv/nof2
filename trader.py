import asyncio
from database import redis_client
import json
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import BINANCE_API_KEY, BINANCE_API_SECRET
from account_positions import get_account_status
import math

client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
REDIS_KEY = "trading_records"

TP_SL_TYPES = {
    "sl": ["STOP", "STOP_MARKET"],
    "tp": ["TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
}

def save_trade_record(record: dict):
    """保存交易记录"""
    redis_client.lpush(REDIS_KEY, json.dumps(record))

# -----------------------------
# 异步工具函数
# -----------------------------
async def async_to_thread(func, *args, **kwargs):
    """将阻塞函数异步化"""
    return await asyncio.to_thread(func, *args, **kwargs)

# -----------------------------
# 异步价格、数量、最小下单额
# -----------------------------
async def get_min_notional_async(symbol: str, default=0):
    info = await async_to_thread(client.futures_exchange_info)
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "MIN_NOTIONAL":
                    try:
                        return float(f.get("notional", default))
                    except Exception:
                        return default
    return default

async def normalize_qty_async(symbol: str, qty: float):
    info = await async_to_thread(client.futures_exchange_info)
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            step = 1
            min_qty = 0
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize", 1))
                    min_qty = float(f.get("minQty", 0))
            decimals = max(0, -int(math.log10(step)))
            qty = max(qty, min_qty)
            qty = math.ceil(qty / step) * step
            qty = round(qty, decimals)

            min_notional = await get_min_notional_async(symbol)
            mark_price = float((await async_to_thread(client.futures_mark_price, symbol=symbol))["markPrice"])
            notional = qty * mark_price
            if notional < min_notional:
                qty = math.ceil(min_notional / mark_price / step) * step
                qty = round(qty, decimals)
            return qty
    return qty

async def normalize_price_async(symbol: str, price: float):
    info = await async_to_thread(client.futures_exchange_info)
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            tick_size = 0.01
            min_price = 0.0
            max_price = 0.0
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick_size = float(f.get("tickSize", 0.01))
                    min_price = float(f.get("minPrice", 0))
                    max_price = float(f.get("maxPrice", 0))
            price = math.floor(price / tick_size) * tick_size
            decimals = max(0, -int(math.log10(tick_size)))
            price = round(price, decimals)
            price = max(price, min_price)
            price = min(price, max_price)
            return price
    return price

# -----------------------------
# 异步 TP/SL 撤单与下单
# -----------------------------
async def cancel_algo_order_async(symbol, algoId=None, clientAlgoId=None):
    if not algoId and not clientAlgoId:
        print("⚠ 必须提供 algoId 或 clientAlgoId")
        return
    try:
        await async_to_thread(client.futures_cancel_algo_order,
                              symbol=symbol, algoId=algoId, clientAlgoId=clientAlgoId)
        print(f"♻ 撤销条件单成功: algoId={algoId}, clientAlgoId={clientAlgoId}")
    except Exception as e:
        print(f"⚠ 撤销条件单失败: algoId={algoId}, clientAlgoId={clientAlgoId}, 错误: {e}")

async def _cancel_tp_sl_async(symbol, position_side, cancel_sl=True, cancel_tp=True):
    types_to_cancel = []
    if cancel_sl:
        types_to_cancel += TP_SL_TYPES["sl"]
    if cancel_tp:
        types_to_cancel += TP_SL_TYPES["tp"]
    if not types_to_cancel:
        return

    try:
        open_orders = await async_to_thread(client.futures_get_open_orders, symbol=symbol)
    except Exception as e:
        print(f"⚠ 获取基础挂单失败: {e}")
        open_orders = []

    tasks = []
    seen_ids = set()
    for o in open_orders:
        if o.get("positionSide") == position_side and o.get("type") in types_to_cancel and o.get("status") in ["NEW", "PARTIALLY_FILLED"]:
            oid = o["orderId"]
            if oid not in seen_ids:
                seen_ids.add(oid)
                tasks.append(async_to_thread(client.futures_cancel_order, symbol=symbol, orderId=oid))
                print(f"♻ 计划取消基础单 {position_side} {o['type']} | id={oid}")

    try:
        algo_orders = await async_to_thread(client.futures_get_open_orders, symbol=symbol, conditional=True)
    except Exception as e:
        print(f"⚠ 获取条件单失败: {e}")
        algo_orders = []

    for o in algo_orders:
        if o.get("positionSide") == position_side and o.get("orderType") in types_to_cancel:
            tasks.append(cancel_algo_order_async(symbol=symbol, algoId=o.get("algoId"), clientAlgoId=o.get("clientAlgoId")))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def _place_tp_sl_async(symbol, position_side, sl=None, tp=None):
    results = []
    tasks = []

    if sl:
        sl_val = await normalize_price_async(symbol, float(sl))
        async def place_sl():
            try:
                order = await async_to_thread(client.futures_create_order,
                    symbol=symbol,
                    side="SELL" if position_side=="LONG" else "BUY",
                    positionSide=position_side,
                    type="STOP_MARKET",
                    stopPrice=sl_val,
                    closePosition=True,
                    timeInForce="GTC"
                )
                print(f"🛑 设置止损条件单成功 {symbol}: {sl_val}")
                results.append(order)
            except Exception as e:
                print(f"⚠ 止损条件单下单失败 {symbol}: {e}")
        tasks.append(place_sl())

    if tp:
        tp_val = await normalize_price_async(symbol, float(tp))
        async def place_tp():
            try:
                order = await async_to_thread(client.futures_create_order,
                    symbol=symbol,
                    side="SELL" if position_side=="LONG" else "BUY",
                    positionSide=position_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=tp_val,
                    closePosition=True,
                    timeInForce="GTC"
                )
                print(f"🎯 设置止盈条件单成功 {symbol}: {tp_val}")
                results.append(order)
            except Exception as e:
                print(f"⚠ 止盈条件单下单失败 {symbol}: {e}")
        tasks.append(place_tp())

    if tasks:
        await asyncio.gather(*tasks)
    return results

async def _update_tp_sl_async(symbol, position_side, sl=None, tp=None):
    await _cancel_tp_sl_async(symbol, position_side, cancel_sl=bool(sl), cancel_tp=bool(tp))
    return await _place_tp_sl_async(symbol, position_side, sl, tp)

# -----------------------------
# 主交易执行异步版
# -----------------------------
async def execute_trade_async(symbol: str, action: str, stop_loss=None, take_profit=None,
                              quantity=None, position_size=None):
    try:
        acc = get_account_status()
        pos = next((p for p in acc["positions"] if p["symbol"] == symbol), None)
        mark = float(pos["mark_price"]) if pos else float((await async_to_thread(client.futures_mark_price, symbol=symbol))["markPrice"])

        qty = None
        if position_size:
            qty = float(position_size) / mark
        elif quantity:
            qty = float(quantity)
        elif action in ["open_long", "open_short", "increase_position"]:
            print(f"⚠ {symbol} 缺少 position_size 或 quantity")
            return None

        if qty:
            qty = await normalize_qty_async(symbol, qty)
            print(f"ℹ {symbol} 最终下单数量: {qty}, 标记价: {mark}")

        current = abs(pos["size"]) if pos else 0

        async def place_order(**kwargs):
            order = await async_to_thread(client.futures_create_order, **kwargs)
            save_trade_record({
                "symbol": symbol,
                "action": action,
                "order": kwargs,
                "price": mark,
                "quantity": kwargs.get("quantity"),
                "status": order.get("status")
            })
            return order

        if action == "open_long":
            order = await place_order(symbol=symbol, side="BUY", positionSide="LONG",
                                      type="MARKET", quantity=qty)
            await _update_tp_sl_async(symbol, "LONG", sl=stop_loss, tp=take_profit)
            return order

        elif action == "open_short":
            order = await place_order(symbol=symbol, side="SELL", positionSide="SHORT",
                                      type="MARKET", quantity=qty)
            await _update_tp_sl_async(symbol, "SHORT", sl=stop_loss, tp=take_profit)
            return order

        elif action == "close_long":
            if not pos or pos["size"] <= 0:
                return None
            return await place_order(symbol=symbol, side="SELL", positionSide="LONG", type="MARKET", quantity=current)

        elif action == "close_short":
            if not pos or pos["size"] >= 0:
                return None
            return await place_order(symbol=symbol, side="BUY", positionSide="SHORT", type="MARKET", quantity=current)

        elif action == "reverse":
            if not pos or current <= 0:
                return None
            if pos["size"] > 0:
                await place_order(symbol=symbol, side="SELL", positionSide="LONG", type="MARKET", quantity=current)
                order = await place_order(symbol=symbol, side="SELL", positionSide="SHORT", type="MARKET", quantity=qty)
                await _update_tp_sl_async(symbol, "SHORT", sl=stop_loss, tp=take_profit)
                return order
            else:
                await place_order(symbol=symbol, side="BUY", positionSide="SHORT", type="MARKET", quantity=current)
                order = await place_order(symbol=symbol, side="BUY", positionSide="LONG", type="MARKET", quantity=qty)
                await _update_tp_sl_async(symbol, "LONG", sl=stop_loss, tp=take_profit)
                return order

        elif action == "increase_position":
            if not qty:
                print(f"⚠ {symbol} increase_position 缺少下单数量")
                return None
            if pos["size"] > 0:
                return await place_order(symbol=symbol, side="BUY", positionSide="LONG", type="MARKET", quantity=qty)
            elif pos["size"] < 0:
                return await place_order(symbol=symbol, side="SELL", positionSide="SHORT", type="MARKET", quantity=qty)

        elif action == "decrease_position":
            if not pos:
                return None
            reduce_qty = qty if qty else current / 2
            reduce_qty = min(reduce_qty, current)
            if pos["size"] > 0:
                return await place_order(symbol=symbol, side="SELL", positionSide="LONG", type="MARKET", quantity=reduce_qty)
            elif pos["size"] < 0:
                return await place_order(symbol=symbol, side="BUY", positionSide="SHORT", type="MARKET", quantity=reduce_qty)

        elif action == "update_stop_loss":
            if pos:
                side = "LONG" if pos["size"] > 0 else "SHORT"
                return await _update_tp_sl_async(symbol, side, sl=stop_loss, tp=None)
            return None

        elif action == "update_take_profit":
            if pos:
                side = "LONG" if pos["size"] > 0 else "SHORT"
                return await _update_tp_sl_async(symbol, side, sl=None, tp=take_profit)
            return None

        else:
            print(f"⚠ 未识别动作: {action}")
            return None

    except BinanceAPIException as e:
        print(f"❌ Binance 下单异常 → {symbol}: {e}")
        return None
    except Exception as e:
        print(f"❌ 其他异常 → {symbol}: {e}")
        return None
