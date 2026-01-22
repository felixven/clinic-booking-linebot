# state_store.py
import json
import sys
from queue_core import redis_conn

PREFIX = "linebot:pending:"        # key prefix
DEFAULT_TTL_SEC = 15 * 60          # 15 分鐘，夠跑完一輪流程
LOCK_PREFIX = "linebot:lock:" #上鎖用的

def _key(line_user_id: str) -> str:
    return f"{PREFIX}{line_user_id}"

def get_state(line_user_id: str) -> dict:
    if not line_user_id:
        return {}

    try:
        raw = redis_conn.get(_key(line_user_id))
    except Exception as e:
        print(f"[STATE_GET_EX] uid={line_user_id} err={repr(e)}", file=sys.stderr, flush=True)
        return {}

    if not raw:
        return {}

    try:
        return json.loads(raw)
    except Exception:
        # 壞資料就當不存在
        return {}


# def get_state(line_user_id: str) -> dict:
#     if not line_user_id:
#         return {}
#     raw = redis_conn.get(_key(line_user_id))
#     if not raw:
#         return {}
#     try:
#         return json.loads(raw)
#     except Exception:
#         # 壞資料就當不存在
#         return {}

def set_state(line_user_id: str, state: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
    if not line_user_id:
        return
    if state is None:
        state = {}

    try:
        redis_conn.set(_key(line_user_id), json.dumps(state, ensure_ascii=False), ex=ttl_sec)
    except Exception as e:
        print(f"[STATE_SET_EX] uid={line_user_id} err={repr(e)}", file=sys.stderr, flush=True)
        return


# def set_state(line_user_id: str, state: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
#     if not line_user_id:
#         return
#     if state is None:
#         state = {}
#     redis_conn.set(_key(line_user_id), json.dumps(state, ensure_ascii=False), ex=ttl_sec)

def clear_state(line_user_id: str) -> bool:
    if not line_user_id:
        return False
    return redis_conn.delete(_key(line_user_id)) > 0

def acquire_lock(key: str, ttl_sec: int = 30) -> bool:
    """
    用 Redis SET NX 做鎖：同 key 在 ttl 內只能成功一次。
    """
    if not key:
        return False
    try:
        ok = bool(redis_conn.set(f"{LOCK_PREFIX}{key}", "1", nx=True, ex=ttl_sec))
        print(f"[LOCK] key={key} ok={ok} ttl={ttl_sec}", flush=True)  # ✅唯一必加
        return ok
    except Exception as e:
        print(f"[LOCK] EXC key={key} err={repr(e)} -> allow", flush=True)  # ✅線上抓證據用
        return True  # 跟你原策略一致：Redis 掛了就放行