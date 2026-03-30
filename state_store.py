# state_store.py
import json
import sys
import copy
import threading
import time
from queue_core import redis_conn

PREFIX = "linebot:pending:"        # key prefix
DEFAULT_TTL_SEC = 15 * 60          # 15 分鐘，夠跑完一輪流程
LOCK_PREFIX = "linebot:lock:" #上鎖用的
_LOCAL_LOCKS: dict[str, float] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_STATE: dict[str, tuple[dict, float]] = {}
_LOCAL_STATE_GUARD = threading.Lock()

def _key(line_user_id: str) -> str:
    return f"{PREFIX}{line_user_id}"


def _acquire_local_lock(key: str, ttl_sec: int) -> bool:
    now = time.time()
    full_key = f"{LOCK_PREFIX}{key}"

    with _LOCAL_LOCKS_GUARD:
        expired = [k for k, expires_at in _LOCAL_LOCKS.items() if expires_at <= now]
        for k in expired:
            _LOCAL_LOCKS.pop(k, None)

        expires_at = _LOCAL_LOCKS.get(full_key)
        if expires_at and expires_at > now:
            return False

        _LOCAL_LOCKS[full_key] = now + ttl_sec
        return True


def _get_local_state(line_user_id: str) -> dict:
    now = time.time()
    with _LOCAL_STATE_GUARD:
        item = _LOCAL_STATE.get(line_user_id)
        if not item:
            return {}

        state, expires_at = item
        if expires_at <= now:
            _LOCAL_STATE.pop(line_user_id, None)
            return {}

        return copy.deepcopy(state)


def _set_local_state(line_user_id: str, state: dict, ttl_sec: int) -> None:
    if not line_user_id:
        return

    safe_ttl = int(ttl_sec or DEFAULT_TTL_SEC)
    if safe_ttl <= 0:
        safe_ttl = DEFAULT_TTL_SEC

    with _LOCAL_STATE_GUARD:
        _LOCAL_STATE[line_user_id] = (copy.deepcopy(state or {}), time.time() + safe_ttl)


def _clear_local_state(line_user_id: str) -> bool:
    if not line_user_id:
        return False

    with _LOCAL_STATE_GUARD:
        existed = line_user_id in _LOCAL_STATE
        _LOCAL_STATE.pop(line_user_id, None)
        return existed

def get_state(line_user_id: str) -> dict:
    if not line_user_id:
        return {}

    try:
        raw = redis_conn.get(_key(line_user_id))
    except Exception as e:
        print(f"[STATE_GET_EX] uid={line_user_id} err={repr(e)}", file=sys.stderr, flush=True)
        return _get_local_state(line_user_id)

    if not raw:
        return {}

    try:
        state = json.loads(raw)
        _set_local_state(line_user_id, state, DEFAULT_TTL_SEC)
        return state
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
        _set_local_state(line_user_id, state, ttl_sec)
        return

    _set_local_state(line_user_id, state, ttl_sec)


# def set_state(line_user_id: str, state: dict, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
#     if not line_user_id:
#         return
#     if state is None:
#         state = {}
#     redis_conn.set(_key(line_user_id), json.dumps(state, ensure_ascii=False), ex=ttl_sec)

def clear_state(line_user_id: str) -> bool:
    if not line_user_id:
        return False
    try:
        deleted = redis_conn.delete(_key(line_user_id)) > 0
    except Exception as e:
        print(f"[STATE_CLEAR_EX] uid={line_user_id} err={repr(e)}", file=sys.stderr, flush=True)
        deleted = False

    local_deleted = _clear_local_state(line_user_id)
    return deleted or local_deleted

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
        local_ok = _acquire_local_lock(key, ttl_sec)
        print(
            f"[LOCK] EXC key={key} err={repr(e)} -> local_ok={local_ok}",
            flush=True,
        )  # ✅線上抓證據用
        return local_ok
