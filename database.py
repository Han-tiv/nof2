import redis
from config import REDIS_HOST, REDIS_PORT, REDIS_DB

redis_client = redis.StrictRedis(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
)

def clear_redis():
    keep = {
        "deepseek_analysis_request_history",
        "deepseek_analysis_response_history",
        "profit:ultra_simple",
        "trading_records"
    }

    keys = redis_client.keys("*")
    deleted = 0

    for key in keys:
        if key not in keep:
            redis_client.delete(key)
            deleted += 1

    print(f"🗑 Redis 清理完成 — 删除 {deleted} 个键，保留历史记录")
