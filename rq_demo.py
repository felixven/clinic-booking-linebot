# rq_demo.py
from redis import Redis
from rq import Queue
import time

# 建立 Redis 連線 & queue（名字叫 "demo"）
redis_conn = Redis(host="localhost", port=6379, db=0)
q = Queue("demo", connection=redis_conn)


def demo_job(name: str):
    """
    一個超簡單的工作：睡個 2 秒，然後印一句話。
    到時候會在 worker 的 terminal 看到這個 print。
    """
    print(f"[demo_job] Hello {name}! I'm running inside an RQ worker.")
    time.sleep(2)
    print(f"[demo_job] Done for {name}.")


def enqueue_demo():
    """
    丟一個 demo_job 進 queue，給 worker 拿去做。
    """
    job = q.enqueue(demo_job, "Ven")
    print(f"Enqueued job: {job.id}")
