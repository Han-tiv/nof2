import asyncio
import time
from datetime import datetime, timezone, timedelta
from ai_trade_notifier import send_tg_trade_signal
from config import monitor_symbols
from indicators import calculate_signal_single
from deepseek_batch_pusher import push_batch_to_deepseek
from kline_fetcher import fetch_all
from position_cache import position_records
from account_positions import get_account_status, account_snapshot
from trader import execute_trade_async
from profit_tracker import update_profit_curve
from database import redis_client

# ========= 工具函数 =========
_RUN_LOCK = asyncio.Lock()
def get_pos_symbols_from_account_snapshot() -> list[str]:
    syms = []
    for p in (account_snapshot.get("positions") or []):
        try:
            size = float(p.get("size", 0))
            if size != 0:
                sym = p.get("symbol")
                if sym:
                    syms.append(sym)
        except Exception:
            continue
    return list(dict.fromkeys(syms))
 
def is_3m_boundary(now: datetime, tolerance: int = 2) -> bool:
    return now.minute % 3 == 0 and now.second <= tolerance

def is_15m_boundary(now: datetime, tolerance: int = 2) -> bool:
    return now.minute % 15 == 0 and now.second <= tolerance

def seconds_to_next_15m_close(now: datetime) -> float:
    """返回距离下一个 15m 整点（K线收盘）还有多少秒"""
    minute = (now.minute // 15 + 1) * 15
    next_run = now.replace(second=0, microsecond=0)
    if minute >= 60:
        next_run = next_run.replace(minute=0) + timedelta(hours=1)
    else:
        next_run = next_run.replace(minute=minute)
    return max(1.0, (next_run - now).total_seconds())

def is_trade_action(action: str, mode: str) -> bool:
    """
    mode = "manage"：仅允许风控动作（禁止开新仓）
    mode = "scan"：允许开仓/平仓/更新
    """
    if mode == "manage":
        return action in {
            "update_stop_loss",
            "update_take_profit",
            "close_long",
            "close_short",
            "reverse",
        }
    # scan
    return action in {
        "open_long",
        "open_short",
        "close_long",
        "close_short",
        "reverse",
        "update_stop_loss",
        "update_take_profit",
    }

def valid_action(action: str) -> bool:
    """动作闭集：用于保留/记录信号（包含 hold/wait），但不一定下单"""
    return action in {
        "open_long", "open_short",
        "close_long", "close_short",
        "reverse",
        "update_stop_loss", "update_take_profit",
        "hold", "wait",
    }

# ========= 核心：单轮执行 =========
async def run_once(mode: str = "scan"):
    """
    mode:
      - "manage": 只管理持仓币（1m）
      - "scan": 扫描主流+持仓（15m）
    """
    async with _RUN_LOCK:  # ✅ 防止 manage/scan 两个 loop 互相踩 monitor_symbols
        print(f"🚀 执行一轮交易调度 | mode={mode}")

        # 刷新账户/持仓与收益曲线
        get_account_status()
        update_profit_curve()
        # print("DEBUG position_records len =", len(position_records or []))
        # print("DEBUG account_snapshot positions len =", len((account_snapshot.get("positions") or [])))
        pos_symbols = get_pos_symbols_from_account_snapshot()
        ai500_symbols = redis_client.lrange("AI500_SYMBOLS", 0, -1)
        has_position = bool(pos_symbols)

        # 本轮监控池
        if mode == "manage":
            if not has_position:
                print("⚠️ manage 模式但当前无仓位，跳过本轮")
                return
            monitor_symbols[:] = list(dict.fromkeys(pos_symbols))
        else:
            monitor_symbols[:] = list(dict.fromkeys(monitor_symbols + pos_symbols + ai500_symbols))

        # ✅ 关键：保存本轮 symbols 的本地副本（后面清理用它，避免并发被改）
        symbols_this_round = list(monitor_symbols)

        try:
            # 拉K线与算指标
            fetch_all()
            for sym in symbols_this_round:
                calculate_signal_single(sym)

            # AI 投喂
            start_ai = time.perf_counter()
            ai_res = await push_batch_to_deepseek()
            end_ai = time.perf_counter()
            print(f"⏱ AI返回耗时: {round(end_ai - start_ai, 3)} 秒")

            if not ai_res or not isinstance(ai_res, list):
                print("⚠ AI 未返回有效信号，不推送，不下单")
                return

            # 过滤：只保留动作闭集内信号（含 wait/hold）
            signals = [sig for sig in ai_res if valid_action(sig.get("action", ""))]

            # manage 模式：只允许持仓币信号（避免模型对非持仓币发号施令）
            if mode == "manage":
                signals = [s for s in signals if s.get("symbol") in pos_symbols]

            # 只对“需要交易/改单”的动作执行；wait/hold 不执行但可以留作日志
            exec_list = [s for s in signals if is_trade_action(s.get("action", ""), mode)]

            # 并发下单
            tasks = []
            for sig in exec_list:
                tasks.append(asyncio.create_task(
                    execute_trade_async(
                        symbol=sig.get("symbol"),
                        action=sig.get("action"),
                        stop_loss=sig.get("stop_loss"),
                        take_profit=sig.get("take_profit"),
                        position_size=(
                            sig.get("position_size")
                            or sig.get("order_value")
                            or sig.get("amount")
                        ),
                        quantity=sig.get("quantity")
                    )
                ))

            if tasks:
                start_exec = time.perf_counter()
                await asyncio.gather(*tasks, return_exceptions=True)
                end_exec = time.perf_counter()
                print(f"⏱ 并行下单耗时: {round(end_exec - start_exec, 3)} 秒")
            else:
                print("ℹ 本轮无需要执行的下单动作（可能是 wait/hold 或无信号）")

            # 3️⃣ 推送 TG（只推送会执行的动作）
            if exec_list:
                start_tg = time.perf_counter()
                try:
                    if asyncio.iscoroutinefunction(send_tg_trade_signal):
                        await send_tg_trade_signal(exec_list)
                    else:
                        await asyncio.to_thread(send_tg_trade_signal, exec_list)
                except Exception as e:
                    print(f"⚠️ TG推送失败: {e}")
                end_tg = time.perf_counter()
                print(f"⏱ TG推送耗时: {round(end_tg - start_tg, 3)} 秒")

        finally:
            # 🧹 清理 Redis 旧 K 线：只在 scan 模式做，避免 manage 每分钟 keys 扫描
            if mode == "scan":
                try:
                    valid = set(symbols_this_round)
                    for key in redis_client.keys("historical_data:*"):
                        k = key if isinstance(key, str) else key.decode()
                        parts = k.split(":")
                        if len(parts) == 3:
                            _, symbol, _ = parts
                            if symbol not in valid:
                                redis_client.delete(key)
                except Exception as e:
                    print(f"⚠️ Redis清理异常: {e}")

        print("🎯 本轮调度完成\n")

# ========= 两个并行调度 Loop =========
async def manage_loop():
    """
    持仓管理：
    - 仅在 3m 整点执行
    - 若该 3m 整点恰好是 15m 整点，则跳过
    """
    while True:
        try:
            now = datetime.now(timezone.utc)

            # ⏳ 等到下一个 3 分钟整点
            while not is_3m_boundary(now):
                await asyncio.sleep(1)
                now = datetime.now(timezone.utc)

            # 🚫 15m 整点：直接跳过（让 scan 独占）
            if is_15m_boundary(now):
                print("⏭ manage_loop 命中15m整点，跳过本轮 manage")
                await asyncio.sleep(3)  # 防止重复命中同一整点
                continue

            await run_once(mode="manage")

        except Exception as e:
            print(f"❌ manage_loop 异常: {e}")

        # 防止重复触发同一个 3m 整点
        await asyncio.sleep(3)

async def scan_loop():
    """对齐15m收盘：扫描全市场机会（启动时先等到下一个15m整点）"""
    # ✅ 启动时先对齐到下一个 15m 整点，避免立刻全量扫
    now = datetime.now(timezone.utc)
    first_sleep = seconds_to_next_15m_close(now)
    print(f"⏳ 首次全量扫描将在 {int(first_sleep)} 秒后（下一个15m整点）")
    await asyncio.sleep(first_sleep)

    while True:
        try:
            await run_once(mode="scan")
        except Exception as e:
            print(f"❌ scan_loop 异常: {e}")

        now = datetime.now(timezone.utc)
        sleep_seconds = seconds_to_next_15m_close(now)
        print(f"⏳ 距离下次15m扫描还有 {int(sleep_seconds)} 秒")
        await asyncio.sleep(sleep_seconds)

async def schedule_loop_async():
    print("⏳ 启动双循环调度：3m持仓管理 + 15m全市场扫描")
    await asyncio.gather(
        manage_loop(),
        scan_loop(),
    )
