import time
import requests
import logging
from queue import Queue
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOPIC_MAP

message_queue = Queue()

def send_telegram_message(message: str, topic: str | None = None):
    """
    topic: TOPIC_MAP 里的 key，例如 "Trading-signals"
    不传 topic 或 topic 不存在 -> 发到主聊天
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    # 如果指定了 topic，就把消息发到对应话题
    if topic:
        thread_id = TOPIC_MAP.get(topic)
        if thread_id is None:
            logging.warning(f"未知topic: {topic}，将发送到主聊天")
        else:
            payload["message_thread_id"] = int(thread_id)

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            logging.warning(f"TG返回失败: {data}")
    except Exception as e:
        logging.warning(f"TG发送失败: {e}")

def queue_message(msg: str, topic: str | None = None):
    """
    把消息入队，topic 可选
    """
    message_queue.put({"text": msg, "topic": topic})

def message_worker():
    while True:
        item = message_queue.get()
        try:
            if item:
                send_telegram_message(item["text"], item.get("topic"))
                time.sleep(2)
        finally:
            message_queue.task_done()
