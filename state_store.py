# state_store.py
import json
from queue_core import redis_conn

PREFIX = "linebot:pending:"        # key prefix
DEFAULT_TTL_SEC = 15 * 60          # 15 分鐘，夠跑完一輪流程

def _key(line_user_id: str) -> str:
    return f"{PREFIX}{line_user_id}"

def get_state(line_user_id: str) -> dict:
    if not line_user_id:
        return {}
    raw = redis_conn.get(_key(line_user_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # 壞資料就當不存在
        return {}

def set_state(line_user_id: str, state: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
    if not line_user_id:
        return
    if state is None:
        state = {}
    redis_conn.set(_key(line_user_id), json.dumps(state, ensure_ascii=False), ex=ttl_sec)

def clear_state(line_user_id: str) -> bool:
    if not line_user_id:
        return False
    return redis_conn.delete(_key(line_user_id)) > 0
