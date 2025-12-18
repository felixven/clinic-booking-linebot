from dotenv import load_dotenv
load_dotenv()

from rq import Worker
from queue_core import redis_conn, voice_call_queue
from app import app

if __name__ == "__main__":
    with app.app_context():
        worker = Worker([voice_call_queue], connection=redis_conn)
        worker.work()
