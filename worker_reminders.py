# worker_reminders.py
from rq import Worker
from queue_core import redis_conn, reminder_queue
from app import app  # Flask app 物件

# 讓 Flask 的 current_app, app.logger 等可以正常運作
if __name__ == "__main__":

    # Worker 直接聽我們在 queue_core 裡定義好的 reminder_queue
    with app.app_context():
        worker = Worker([reminder_queue], connection=redis_conn)
        worker.work()
 

        