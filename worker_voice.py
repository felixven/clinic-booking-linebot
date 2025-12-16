from rq import Worker
from queue_core import redis_conn, reminder_queue
from app import app

if __name__ == "__main__":
    with app.app_context():
        worker = Worker([reminder_queue], connection=redis_conn)
        worker.work()
