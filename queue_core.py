from redis import Redis
from rq import Queue
import os

REDIS_URL = os.getenv("REDIS_URL")

if REDIS_URL:
    redis_conn = Redis.from_url(REDIS_URL)
else:
    redis_conn = Redis(host="localhost", port=6379, db=0)

# LINE 訊息回呼用
reminder_queue = Queue("reminders", connection=redis_conn)

# LINE 外撥提醒用
voice_call_queue = Queue("voice_calls", connection=redis_conn)






