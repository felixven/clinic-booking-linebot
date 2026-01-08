# queue_core.py
import os
import logging
from redis import Redis
from rq import Queue

logger = logging.getLogger(__name__)

def _mask(s: str | None) -> str:
    if not s:
        return "None"
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}...{s[-4:]}(len={len(s)})"

def build_redis_conn() -> Redis:
    # 你也可以用 REDIS_URL，但你現在走 host/port/password 也 OK
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        logger.error("[REDIS_ENV] mode=URL REDIS_URL=%s", redis_url)
        return Redis.from_url(redis_url, decode_responses=True)

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD")

    # 重點：username 可能「不支援」，所以允許不設定
    username = os.getenv("REDIS_USERNAME")  # 若沒設就 None

    use_ssl = str(os.getenv("REDIS_SSL", "true")).lower() in ("1", "true", "yes")
    if host == "localhost":
        use_ssl = False  # 本機通常不走 TLS

    logger.error(
        "[REDIS_ENV] host=%s port=%s ssl=%s username=%s password=%s",
        host, port, use_ssl, (username or "None"), _mask(password)
    )

    kwargs = dict(
        host=host,
        port=port,
        password=password,
        ssl=use_ssl,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
    )

    # Azure TLS 有時候會卡憑證驗證，先放寬（你也可以日後再收緊）
    if use_ssl:
        kwargs["ssl_cert_reqs"] = None

    # ✅ 只有在你真的設定了 REDIS_USERNAME 才帶 username
    if username:
        kwargs["username"] = username

    return Redis(**kwargs)

redis_conn = build_redis_conn()

# LINE 訊息回呼用
reminder_queue = Queue("reminders", connection=redis_conn)

# LINE 外撥提醒用
voice_call_queue = Queue("voice_calls", connection=redis_conn)
