BINANCE_API_KEY = "111111111111111" #币安私钥
BINANCE_API_SECRET = "1111111111111" #币安公钥

TELEGRAM_BOT_TOKEN = "1111111111" #tg token
TELEGRAM_CHAT_ID = "-111111111111" #tg 频道

DEEPSEEK_API_KEY = "sk-111111111111"  # deepseek key
DEEPSEEK_MODEL = "deepseek-chat"
# DEEPSEEK_MODEL = "deepseek-reasoner"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# ===== 静态主流币（始终监控） =====
mainstream_symbols = ['ETHUSDT', 'SOLUSDT']

# ===== OI 动态池（OI 扫描自动写入，不要手动填写） =====
altcoins_symbols = []          # ← OI 发现新币时自动 append

# ===== 监控池（深度分析用） =====
monitor_symbols = mainstream_symbols + altcoins_symbols

# ===== 周期 =====
timeframes = ["5m", "15m", "1h", "4h", "1d"]

# ===== Redis =====
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 10

# ===== OI 异动扫描配置 =====
OI_THRESHOLD = 5               # 达到 ±5% 波动才算异动
OI_INTERVAL_MINUTES = 5        # 扫描周期（5分钟）
OI_CONCURRENCY = 20            # 并发请求
OI_EXPIRE_MINUTES = 30         # OI 不再异常 → 自动移除
OI_BASE_URL = "https://fapi.binance.com"

# ===== 扫描市场范围 =====
OI_USE_WHITELIST = False       # False → 扫全市场并发现新币
OI_WHITELIST = altcoins_symbols  # 用于 True 时扫描固定列表