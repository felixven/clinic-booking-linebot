# dedupe_store.py
import hashlib
import sys
import threading
import time
from queue_core import redis_conn

PREFIX = "linebot:dedupe:webhook:"   # 跟 linebot:pending: 同風格
DEFAULT_TTL_SEC = 6 * 60 * 60        # 6 小時（可調 1~6 小時）
_LOCAL_DEDUPE_LOCK = threading.Lock()
_LOCAL_DEDUPE: dict[str, float] = {}

def _key(key_id: str) -> str:
    return f"{PREFIX}{key_id}"


def _check_and_mark_local(k: str, ttl_sec: int) -> bool:
    now = time.time()
    with _LOCAL_DEDUPE_LOCK:
        expired = [key for key, expires_at in _LOCAL_DEDUPE.items() if expires_at <= now]
        for key in expired:
            _LOCAL_DEDUPE.pop(key, None)

        expires_at = _LOCAL_DEDUPE.get(k)
        if expires_at and expires_at > now:
            return False

        _LOCAL_DEDUPE[k] = now + ttl_sec
        return True

def _make_key_id(evt_id: str | None, msg_id: str | None, evt_ts: int | None, body: str) -> str:
    """
    入口可取得的唯一鍵優先序：
    1) webhookEventId
    2) message.id
    3) timestamp + body hash（兜底）
    """
    if evt_id:
        return f"evt:{evt_id}"
    if msg_id:
        return f"msg:{msg_id}"

    h = hashlib.sha1((body or "").encode("utf-8")).hexdigest()[:12]
    ts_part = str(evt_ts) if evt_ts is not None else "na"
    return f"ts:{ts_part}:h:{h}"

def check_and_mark_webhook(*, evt_id=None, msg_id=None, evt_ts=None, body="", ttl_sec: int = DEFAULT_TTL_SEC):
    """
    回傳值：
    - True  => 第一次看到（放行）
    - False => 已看過（重送，直接擋）
    - Redis 出錯時改走 process-local fallback，仍會回 True / False
    """
    key_id = _make_key_id(evt_id, msg_id, evt_ts, body)
    k = _key(key_id)

    try:
        # SET NX EX：不存在才寫入，並設定 TTL
        ok = redis_conn.set(k, "1", nx=True, ex=ttl_sec)
        return True if ok else False
    except Exception as e:
        # Redis 掛掉時改用 process-local fallback，避免直接 fail-open。
        print(f"[DEDUPE_EX] evt_id={evt_id} msg_id={msg_id} err={repr(e)}", file=sys.stderr, flush=True)
        local_ok = _check_and_mark_local(k, ttl_sec)
        print(
            f"[DEDUPE_LOCAL] evt_id={evt_id} msg_id={msg_id} ok={local_ok}",
            file=sys.stderr,
            flush=True,
        )
        return local_ok


# def check_and_mark_webhook(*, evt_id=None, msg_id=None, evt_ts=None, body="", ttl_sec: int = DEFAULT_TTL_SEC):
#     """
#     回傳值：
#     - True  => 第一次看到（放行）
#     - False => 已看過（重送，直接擋）
#     - None  => Redis 出錯（fail-open 放行，但要 log）
#     """
#     key_id = _make_key_id(evt_id, msg_id, evt_ts, body)
#     k = _key(key_id)

#     try:
#         #  SET NX EX：不存在才寫入，並設定 TTL
#         ok = redis_conn.set(k, "1", nx=True, ex=ttl_sec)
#         return True if ok else False
#     except Exception:
#         return None
