from market_structure import MarketStructure

#币安账号API
BINANCE_API_KEY = "111111111111111"
BINANCE_API_SECRET = "1111111111111111"
BINANCE_ENVIRONMENT = False # False 实盘   True 模拟盘

#TG配置
TELEGRAM_BOT_TOKEN = "11111111111111"
TELEGRAM_CHAT_ID = "-11111111111"

#AIBTC.VIP大模型配置
CLAUDE_API_KEY = "1111111111111111"  #对应模型的 key
CLAUDE_MODEL = "claude-opus-4-5-20251101"  #对应模型的名称，gpt-5.2/gemini-3-pro-preview/deepseek-chat
CLAUDE_URL = "https://api.aibtc.vip/v1/chat/completions"

AI_PROVIDER = "claude"  # 这里不能改

# ===== 固定币种监控池 =====
monitor_symbols = ['ETHUSDT', 'SOLUSDT']
# monitor_symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
# monitor_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'HYPEUSDT']

OI_BASE_URL = "https://fapi.binance.com"

# ===== 多周期 =====
timeframes = ["4h", "1h", "15m"]

# ===== Redis =====
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 10


#定义「周期 → EMA 参数映射」
EMA_CONFIG = {
    "15m": [20, 50],
    "1h":  [50],
}

#定义「周期 → K线数量」
KLINE_LIMITS = {
    "15m": 301,
    "1h": 501,
    "4h": 801,
}

#结构计算
STRUCTURE_PARAMS = {
    "15m": {"swing_size": 4, "keep_pivots": 10, "trend_vote_lookback": 3, "range_pivot_k": 3},
    "1h":  {"swing_size": 6, "keep_pivots": 12, "trend_vote_lookback": 3, "range_pivot_k": 3},
    "4h":  {"swing_size": 10, "keep_pivots": 14, "trend_vote_lookback": 3, "range_pivot_k": 3},
}

# 每个话题在 TG 群里的 message_thread_id
TOPIC_MAP = {
    "Trading-signals": 58069,      # 交易信号
    "On-chain-monitoring": 58071,        # 链上监控
    "Abnormal-signal": 58065,      # 交易话题
}

DEFAULT_TOPIC = None   # None = 主聊天


