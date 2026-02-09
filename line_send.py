# line_send.py
import time
import socket
import json
import os
import uuid
from queue_core import redis_conn

from flask import current_app

# try:
#     import urllib3
#     from urllib3.exceptions import ReadTimeoutError
# except Exception:
#     urllib3 = None
#     ReadTimeoutError = None

# try:
#     import requests
# except Exception:
#     requests = None

from linebot.v3.messaging import ReplyMessageRequest, PushMessageRequest, TextMessage
from queue_core import redis_conn

PUSH_GUARD_PREFIX = "linebot:pushguard:"


def _guard_key(to_id: str) -> str:
    # 只用 to_id 做 guard（避免 reply timeout 後同一人短時間被重複 push 洗版）
    return f"{PUSH_GUARD_PREFIX}{to_id}"


def _acquire_push_guard(to_id: str, label: str, event_key: str | None, ttl_sec: int) -> bool:
    """
    同一顆事件（event_key）60 秒只允許一次
    - return True：成功取得 guard（允許 push）
    - return False：guard 已存在（跳過 push）
    """
    if not to_id:
        return False
    
    #萬一沒有event_key就直接放行
    if not event_key:
        print("[LINE_guard] missing event_key -> allow", flush=True)
        return True

    ttl_sec = int(ttl_sec or 60)
    if ttl_sec <= 0:
        ttl_sec = 60

    key = f"line:push_guard:{to_id}:{label}:{event_key}"

    try:
        ok = redis_conn.set(name=key, value=b"1", nx=True, ex=ttl_sec)
        # redis-py: ok 可能是 True / False；有些版本回 b'OK'
        acquired = (ok is True) or (ok == b"OK") or (ok == "OK")
        print(f"[LINE_guard] acquired={acquired} key={key} ttl={ttl_sec}", flush=True)
        return acquired

    except Exception as e:
        # guard 壞了也不要讓主流程爆炸：寧可放行（避免 push 永遠不送）
        current_app.logger.warning("[LINE_guard] error=%s key=%s -> allow", repr(e), key)
        print(f"[LINE_guard] error={repr(e)} key={key} -> allow", flush=True)
        return True

    
def _event_key(event) -> str:
    # 用 reply_token 當事件 key（夠用、最穩）
    return getattr(event, "reply_token", "") or ""

def _acquire_push_guard_by_event(event_key: str, label: str, ttl_sec: int) -> bool:
    if not event_key or ttl_sec <= 0:
        return True
    try:
        safe_label = (label or "send").replace(" ", "_")
        key = f"{PUSH_GUARD_PREFIX}{safe_label}:{event_key}"
        ok = redis_conn.set(key, "1", nx=True, ex=ttl_sec)
        return bool(ok)
    except Exception as e:
        current_app.logger.warning("[LINE_%s] push guard redis error -> allow push err=%s", label, repr(e))
        return True
    
def _latest_key(uid: str, label: str) -> str:
    return f"line:latest:{uid}:{label}"

def _set_latest(uid: str, label: str, event_key: str, ttl_sec: int = 120) -> None:
    try:
        redis_conn.set(_latest_key(uid, label), event_key, ex=ttl_sec)
    except Exception:
        # latest guard 掛掉就當沒這功能，不要影響主流程
        pass

def _is_latest(uid: str, label: str, event_key: str) -> bool:
    try:
        v = redis_conn.get(_latest_key(uid, label))
        if v is None:
            return True  # 沒資料就放行
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="ignore")
        return v == event_key
    except Exception:
        return True  # fail-open

    
try:
    import requests
except Exception:
    requests = None

def _get_channel_access_token(line_bot_api) -> str:
    # 盡量從 SDK 物件取，取不到再用 env
    token = ""
    try:
        token = getattr(getattr(line_bot_api, "api_client", None), "configuration", None).access_token or ""
    except Exception:
        token = ""
    if not token:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    return token

def push_message_raw(line_bot_api, to_id: str, messages, *, timeout=(3, 10), retry_key: str | None = None) -> bool:
    """
    用 raw HTTP 送 push，才能加 X-Line-Retry-Key（避免重送造成重複）
    """
    token = _get_channel_access_token(line_bot_api)
    if not token or not to_id:
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key  # ✅ 官方 retry-key :contentReference[oaicite:3]{index=3}

    payload = {
        "to": to_id,
        "messages": [m.to_dict() for m in messages],  # linebot v3 message objects
    }

    try:
        t0 = time.time()
        if requests is None:
            return False
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        ok = resp.status_code in (200, 202, 409)  # 409 = 已受理過同 retry-key :contentReference[oaicite:4]{index=4}
        current_app.logger.info(
            "[LINE_push_raw] ok=%s status=%s elapsed=%.2fs to=%s retry=%s body=%s",
            ok, resp.status_code, time.time() - t0, to_id, retry_key, resp.text[:200]
        )
        return ok
    except Exception as e:
        current_app.logger.exception("[LINE_push_raw] failed to=%s err=%s", to_id, repr(e))
        return False


def is_timeout_exc(e: Exception) -> bool:
    # 1) 常見 timeout 型別
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True

    # ✅ 關鍵：只做一次 import & 一次型別抓取（避免到處 from xxx import）
    ReadTimeoutError = None
    try:
        import urllib3 as _urllib3
        ReadTimeoutError = _urllib3.exceptions.ReadTimeoutError
    except Exception:
        pass

    # 2) urllib3 的 ReadTimeoutError
    if ReadTimeoutError and isinstance(e, ReadTimeoutError):
        return True

    # 3) 追 cause/context（urllib3 常包多層）
    cur = e
    for _ in range(8):
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        if not cur:
            break

        if isinstance(cur, (TimeoutError, socket.timeout)):
            return True
        if ReadTimeoutError and isinstance(cur, ReadTimeoutError):
            return True

    # 4) 兜底（防止型別抓不到）
    s = repr(e)
    if ("ReadTimeoutError" in s) or ("read timeout" in s) or ("timed out" in s):
        return True

    return False

# def is_timeout_exc(e: Exception) -> bool:
#     # 1) 常見 timeout 型別
#     if isinstance(e, (TimeoutError, socket.timeout)):
#         return True

#     # 2) urllib3 的 ReadTimeoutError
#     try:
#         from urllib3.exceptions import ReadTimeoutError
#         if isinstance(e, ReadTimeoutError):
#             return True
#     except Exception:
#         pass

#     # 3) 追 cause/context（urllib3 常包多層）
#     cur = e
#     for _ in range(8):
#         cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
#         if not cur:
#             break

#         if isinstance(cur, (TimeoutError, socket.timeout)):
#             return True
#         try:
#             from urllib3.exceptions import ReadTimeoutError
#             if isinstance(cur, ReadTimeoutError):
#                 return True
#         except Exception:
#             pass

#     # 4) 兜底（防止型別抓不到）
#     s = repr(e)
#     if ("ReadTimeoutError" in s) or ("read timeout" in s) or ("timed out" in s):
#         return True

#     return False



def _event_to_to_id(event) -> str:
    src = getattr(event, "source", None)
    if not src:
        return ""
    uid = getattr(src, "user_id", None)
    if uid:
        return uid
    gid = getattr(src, "group_id", None)
    if gid:
        return gid
    rid = getattr(src, "room_id", None)
    if rid:
        return rid
    return ""

def start_loading_animation(line_bot_api, chat_id: str, *, loading_seconds: int = 30, timeout=(3, 5)) -> bool:
    """
    顯示 loading animation（不吃 reply token）
    - 只支援一對一 chat（chat_id = userId）
    - loading_seconds 只能是 5,10,15...60
    """
    if not chat_id:
        return False

    # 群組/多人聊天室不支援（通常會是 C... / R...）
    if chat_id.startswith("C") or chat_id.startswith("R"):
        return False

    # LINE 允許的秒數：5~60 且只能是 5 的倍數
    if loading_seconds < 5:
        loading_seconds = 5
    if loading_seconds > 60:
        loading_seconds = 60
    if loading_seconds % 5 != 0:
        loading_seconds = 5 * round(loading_seconds / 5)

    # ✅ 關鍵：函式內 local import，不吃檔案頂部是否有 import
    try:
        import requests as _requests
    except Exception:
        _requests = None

    try:
        import urllib3 as _urllib3
    except Exception:
        _urllib3 = None

    token = _get_channel_access_token(line_bot_api)
    if not token:
        current_app.logger.warning("[LINE_loading] missing channel access token -> skip")
        return False

    url = "https://api.line.me/v2/bot/chat/loading/start"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {"chatId": chat_id, "loadingSeconds": loading_seconds}

    try:
        t0 = time.time()

        if _requests is not None:
            resp = _requests.post(url, headers=headers, json=payload, timeout=timeout)
            ok = (resp.status_code == 202)
        else:
            if _urllib3 is None:
                return False
            http = _urllib3.PoolManager()
            resp = http.request(
                "POST",
                url,
                body=json.dumps(payload).encode("utf-8"),
                headers=headers,
                timeout=_urllib3.Timeout(connect=timeout[0], read=timeout[1]),
            )
            ok = (resp.status == 202)

        current_app.logger.info(
            "[LINE_loading] start ok=%s elapsed=%.2fs chat_id=%s seconds=%s",
            ok, time.time() - t0, chat_id, loading_seconds
        )
        return ok

    except Exception as e:
        current_app.logger.warning("[LINE_loading] start failed err=%s chat_id=%s", repr(e), chat_id)
        return False
# def start_loading_animation(line_bot_api, chat_id: str, *, loading_seconds: int = 30, timeout=(3, 5)) -> bool:
#     """
#     顯示 loading animation（不吃 reply token）
#     - 只支援一對一 chat（chat_id = userId）
#     - loading_seconds 只能是 5,10,15...60
#     """
#     if not chat_id:
#         return False

#     # 群組/多人聊天室不支援（通常會是 C... / R...）
#     if chat_id.startswith("C") or chat_id.startswith("R"):
#         return False

#     # LINE 允許的秒數：5~60 且只能是 5 的倍數
#     if loading_seconds < 5:
#         loading_seconds = 5
#     if loading_seconds > 60:
#         loading_seconds = 60
#     if loading_seconds % 5 != 0:
#         loading_seconds = 5 * round(loading_seconds / 5)

#     token = _get_channel_access_token(line_bot_api)
#     if not token:
#         current_app.logger.warning("[LINE_loading] missing channel access token -> skip")
#         return False

#     url = "https://api.line.me/v2/bot/chat/loading/start"
#     headers = {
#         "Content-Type": "application/json",
#         "Authorization": f"Bearer {token}",
#     }
#     payload = {"chatId": chat_id, "loadingSeconds": loading_seconds}

#     try:
#         t0 = time.time()
#         if requests is not None:
#             resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
#             ok = (resp.status_code == 202)
#         else:
#             if urllib3 is None:
#                 return False
#             http = urllib3.PoolManager()
#             resp = http.request(
#                 "POST",
#                 url,
#                 body=json.dumps(payload).encode("utf-8"),
#                 headers=headers,
#                 timeout=urllib3.Timeout(connect=timeout[0], read=timeout[1]),
#             )
#             ok = (resp.status == 202)

#         current_app.logger.info(
#             "[LINE_loading] start ok=%s elapsed=%.2fs chat_id=%s seconds=%s",
#             ok, time.time() - t0, chat_id, loading_seconds
#         )
#         return ok
#     except Exception as e:
#         current_app.logger.warning("[LINE_loading] start failed err=%s chat_id=%s", repr(e), chat_id)
#         return False

def _get_event_key(event) -> str | None:
    # linebot v3 的 event 可能是 webhook_event_id 或 webhookEventId
    return (
        getattr(event, "webhook_event_id", None)
        or getattr(event, "webhookEventId", None)
        or getattr(event, "webhookEventID", None)  # 保險
    )
    

def send_line(
    line_bot_api,
    event,
    messages,
    *,
    timeout=(3, 12),          # reply timeout
    push_timeout=(3, 10),     # ✅ push 不沿用 reply timeout
    label="send",
    push_guard_ttl_sec=60,
    event_key: str | None = None,
):
    to_id = _event_to_to_id(event)
    t0 = time.time()
    req = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)

    if event_key is None:          # 沒傳要自己抓
        event_key = _get_event_key(event)

    # ✅ 先用 print 把路徑釘死（暫時用，之後再拿掉）
    print(f"[SEND_LINE_ENTER] label={label} to_id={to_id}", flush=True)
    print(f"[SEND_LINE] label={label} uid={to_id} event_key={event_key} is_reply={bool(event and getattr(event,'reply_token',None))}", flush=True)

    # 記住：這個 uid + label 的最新事件是誰
    _set_latest(to_id, label, event_key, ttl_sec=120)


    try:
        if os.getenv("FORCE_REPLY_TIMEOUT") in ("1", "true", "True", "yes", "Y"):
            raise TimeoutError("FORCE_REPLY_TIMEOUT=1")
        try:
            resp = line_bot_api.reply_message(req, _request_timeout=timeout)
        except TypeError:
            # 不要 return，讓它還在 try 裡
            resp = line_bot_api.reply_message_with_http_info(req, _request_timeout=timeout)

        print(f"[SEND_LINE_REPLY_OK] label={label} elapsed={time.time()-t0:.2f}s", flush=True)
        return resp
    except Exception as e:
        # ✅ 印出更多資訊：ApiException 要看到 status/body 才知道 LINE 嫌什麼
        status = getattr(e, "status", None)
        body = getattr(e, "body", None)
        reason = getattr(e, "reason", None)

        print(
            f"[SEND_LINE_REPLY_FAIL] label={label} elapsed={time.time()-t0:.2f}s "
            f"err={repr(e)} status={status} reason={reason} body={body}",
            flush=True
        )

        # ✅ 不管是不是 timeout，都要嘗試 fallback（但仍然尊重 stale/latest 與 guard）
        if not to_id:
            return None

        try:
            force_guard_bypass = os.getenv("FORCE_GUARD_BYPASS", "0") in ("1", "true", "True", "yes", "Y")

            # ✅ 最新事件守門：不是最新就不要 push（避免舊事件晚到）
            if not _is_latest(to_id, label, event_key):
                print(f"[SEND_LINE_STALE_SKIP] label={label} to_id={to_id} event_key={event_key}", flush=True)
                return None

            if not force_guard_bypass:
                if not _acquire_push_guard(to_id, label, event_key, push_guard_ttl_sec):
                    print(f"[SEND_LINE_GUARD_HIT] label={label} to_id={to_id} event_key={event_key}", flush=True)
                    return None
            else:
                print(f"[SEND_LINE_GUARD_BYPASS] label={label} to_id={to_id} event_key={event_key}", flush=True)

            # ✅ 重要：如果原本 messages 本身就「不合法」(400)，push 同一包也會失敗
            # 所以這裡做一個保險：先試原 messages，失敗再改推純文字兜底
            try:
                preq = PushMessageRequest(to=to_id, messages=messages)
                t1 = time.time()
                r = line_bot_api.push_message(preq, _request_timeout=push_timeout)
                print(f"[SEND_LINE_PUSH_OK] label={label} elapsed={time.time()-t1:.2f}s to_id={to_id}", flush=True)
                return r
            except Exception as e_push:
                status2 = getattr(e_push, "status", None)
                body2 = getattr(e_push, "body", None)
                reason2 = getattr(e_push, "reason", None)
                print(
                    f"[SEND_LINE_PUSH_FAIL] label={label} err={repr(e_push)} status={status2} reason={reason2} body={body2} "
                    f"to_id={to_id} event_key={event_key}",
                    flush=True
                )

                # ✅ 再兜底：推純文字（至少讓病患看到「系統忙碌」）
                fallback = [TextMessage(text="系統忙碌中，請稍後再試一次")]
                preq2 = PushMessageRequest(to=to_id, messages=fallback)
                t2 = time.time()
                r2 = line_bot_api.push_message(preq2, _request_timeout=push_timeout)
                print(f"[SEND_LINE_PUSH_OK_FALLBACK_TEXT] label={label} elapsed={time.time()-t2:.2f}s to_id={to_id}", flush=True)
                return r2

        except Exception as e2:
            status3 = getattr(e2, "status", None)
            body3 = getattr(e2, "body", None)
            reason3 = getattr(e2, "reason", None)
            print(
                f"[SEND_LINE_FALLBACK_FATAL] label={label} err={repr(e2)} status={status3} reason={reason3} body={body3} "
                f"to_id={to_id} event_key={event_key}",
                flush=True
            )
            return None


    # except Exception as e:
    #     print(f"[SEND_LINE_REPLY_FAIL] label={label} elapsed={time.time()-t0:.2f}s err={repr(e)}", flush=True)

    #     print(f"[SEND_LINE_IS_TIMEOUT] label={label} is_timeout={is_timeout_exc(e)}", flush=True)

    #     if not is_timeout_exc(e):
    #         return None
    #     if not to_id:
    #         return None

    #     # guard
    #     try:
    #         force_guard_bypass = os.getenv("FORCE_GUARD_BYPASS", "0") in ("1", "true", "True", "yes", "Y")

    #         # ✅ 最新事件守門：不是最新就不要 push（避免舊事件晚到）
    #         if not _is_latest(to_id, label, event_key):
    #             print(f"[SEND_LINE_STALE_SKIP] label={label} to_id={to_id} event_key={event_key}", flush=True)
    #             return None

    #         if not force_guard_bypass:
    #             if not _acquire_push_guard(to_id, label, event_key, push_guard_ttl_sec):
    #                 print(f"[SEND_LINE_GUARD_HIT] label={label} to_id={to_id} event_key={event_key}", flush=True)
    #                 return None
    #         else:
    #             print(f"[SEND_LINE_GUARD_BYPASS] label={label} to_id={to_id} event_key={event_key}", flush=True)

    #         preq = PushMessageRequest(to=to_id, messages=messages)
    #         t1 = time.time()
    #         r = line_bot_api.push_message(preq, _request_timeout=push_timeout)
    #         print(f"[SEND_LINE_PUSH_OK] label={label} elapsed={time.time()-t1:.2f}s to_id={to_id}", flush=True)
    #         return r

    #     except Exception as e2:
    #         print(f"[SEND_LINE_PUSH_FAIL] label={label} err={repr(e2)} to_id={to_id} event_key={event_key}", flush=True)
    #         return None


    
# def send_line(
#     line_bot_api,
#     event,
#     messages,
#     *,
#     timeout=(3, 30),          # ✅ 我建議不要卡 30s，12s 夠判斷“怪了”
#     label="send",
#     push_guard_ttl_sec=60,
# ):
#     to_id = _event_to_to_id(event)

#     t0 = time.time()
#     req = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)

#     try:
#         return line_bot_api.reply_message(req, _request_timeout=timeout)

#     except TypeError:
#         return line_bot_api.reply_message_with_http_info(req, _request_timeout=timeout)

#     except Exception as e:
#         current_app.logger.exception(
#             "[LINE_%s] reply failed elapsed=%.2fs err=%s",
#             label, time.time() - t0, repr(e)
#         )

#         # 只在 timeout 才考慮備援 push
#         if not is_timeout_exc(e):
#             return None

#         if not to_id:
#             return None

#         # ✅ fallback push：任何錯都不能把 callback 弄爆
#         try:
#             if not _acquire_push_guard(to_id, label, push_guard_ttl_sec):  # 或 2 參數版本
#                 current_app.logger.warning("[LINE_%s] guard hit -> skip push to=%s", label, to_id)
#                 return None

#             preq = PushMessageRequest(to=to_id, messages=messages)
#             r = line_bot_api.push_message(preq, _request_timeout=(3, 10))
#             current_app.logger.info("[LINE_%s] fallback push ok to=%s", label, to_id)
#             return r

#         except Exception as e2:
#             current_app.logger.exception("[LINE_%s] fallback push failed err=%s to=%s", label, repr(e2), to_id)
#             return None




# def send_line(
#     line_bot_api,
#     event,
#     messages,
#     *,
#     timeout=(3, 30),          # reply read timeout 設定 30 秒
#     label="send",
#    # 60 秒內同一 user 只允許一次 fallback push
# ):
#     """
#     體驗版機制：
#     0) (可選) 先顯示 loading animation（不吃 reply token）
#     1) 再 reply（timeout 拉長到 30）
#     2) 只有 reply timeout 才 fallback push
#     3) push 前用 Redis guard 擋「短時間重複 push」
#     """
#     to_id = _event_to_to_id(event)

#     # 1) reply
#     t0 = time.time()
#     req = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)

#     try:
#         return line_bot_api.reply_message(req, _request_timeout=timeout)

#     except TypeError:
#         return line_bot_api.reply_message_with_http_info(req, _request_timeout=timeout)

#     except Exception as e:
#         current_app.logger.exception(
#             "[LINE_%s] reply failed elapsed=%.2fs err=%s",
#             label, time.time() - t0, repr(e)
#         )

#         # 非 timeout：不要 push（例如 token/權限/格式錯）
#         if not is_timeout_exc(e):
#             return None
        
#         if not push_on_timeout:
#             current_app.logger.warning("[LINE_%s] reply timeout but push_on_timeout=False -> skip push", label)
#             return None

#         if not to_id:
#             current_app.logger.warning("[LINE_%s] reply timeout but cannot resolve to_id -> skip push", label)
#             return None

#         # 3) guard：短時間只允許一次 fallback push
#         if not _acquire_push_guard(to_id, push_guard_ttl_sec):
#             current_app.logger.warning(
#                 "[LINE_%s] reply timeout -> skip fallback push (guard hit ttl=%ss) to=%s",
#                 label, push_guard_ttl_sec, to_id
#             )
#             return None

#         # 2) fallback push
#         preq = PushMessageRequest(to=to_id, messages=messages)
#         try:
#             current_app.logger.warning("[LINE_%s] reply timeout -> fallback push to=%s", label, to_id)
#             t1 = time.time()
#             r = line_bot_api.push_message(preq, _request_timeout=timeout)
#             current_app.logger.info(
#                 "[LINE_%s] fallback push ok elapsed=%.2fs to=%s",
#                 label, time.time() - t1, to_id
#             )
#             return r
#         except Exception as e2:
#             current_app.logger.exception("[LINE_%s] push failed err=%s to=%s", label, repr(e2), to_id)
#             return None

# import time
# import socket
# from flask import current_app

# try:
#     import urllib3
# except Exception:
#     urllib3 = None

# from linebot.v3.messaging import ReplyMessageRequest, PushMessageRequest

# # 用 Redis 做 push guard（避免同一事件一直拼 push）
# # 你專案裡應該已經有 queue_core.redis_conn
# from queue_core import redis_conn


# PUSH_GUARD_PREFIX = "linebot:pushguard:"


# def _guard_key(to_id: str, label: str) -> str:
#     # label 也放進 key，避免不同訊息互相影響；你也可以改成只用 to_id 做全域 guard
#     safe_label = (label or "send").replace(" ", "_")
#     return f"{PUSH_GUARD_PREFIX}{to_id}:{safe_label}"


# def _acquire_push_guard(to_id: str, label: str, ttl_sec: int) -> bool:
#     """
#     True  = 這次允許 push（第一次）
#     False = guard hit（ttl 內已 push 過，這次跳過）
#     """
#     if not to_id:
#         return True
#     if ttl_sec <= 0:
#         return True
#     try:
#         key = _guard_key(to_id, label)
#         # NX: 只在 key 不存在時 set
#         # EX: 過期秒數
#         ok = redis_conn.set(key, "1", nx=True, ex=ttl_sec)
#         return bool(ok)
#     except Exception as e:
#         # Redis 有問題時，不要讓整個流程炸掉；此時就放行（但會比較容易 push 多次）
#         current_app.logger.warning("[LINE_%s] push guard redis error -> allow push err=%s", label, repr(e))
#         return True


# def is_timeout_exc(e: Exception) -> bool:
#     # TimeoutError / socket.timeout
#     if isinstance(e, (TimeoutError, socket.timeout)):
#         return True

#     # urllib3 ReadTimeoutError
#     if urllib3 is not None:
#         try:
#             if isinstance(e, urllib3.exceptions.ReadTimeoutError):
#                 return True
#         except Exception:
#             pass

#     # 有些情況會包在字串訊息裡
#     msg = repr(e)
#     if "Read timed out" in msg or "ReadTimeoutError" in msg:
#         return True

#     return False


# def _event_to_to_id(event) -> str:
#     """
#     你原本應該就有這個邏輯：
#     - MessageEvent / PostbackEvent: event.source.user_id
#     - 群組/聊天室可用 group_id/room_id（看你業務需求）
#     """
#     src = getattr(event, "source", None)
#     if not src:
#         return ""
#     # user_id 優先
#     uid = getattr(src, "user_id", None)
#     if uid:
#         return uid
#     # 群組/聊天室 fallback
#     gid = getattr(src, "group_id", None)
#     if gid:
#         return gid
#     rid = getattr(src, "room_id", None)
#     if rid:
#         return rid
#     return ""


# def send_line(
#     line_bot_api,
#     event,
#     messages,
#     *,
#     timeout=(3, 20),               # read timeout 拉長到 20 秒
#     label="send",
#     push_on_timeout=True,          # timeout 才 fallback push
#     push_guard_ttl_sec=60,         # 60 秒內同一 label 只 push 一次/防止一直push
# ):
#     """
#     機制（講人話）：
#     1) 先 reply（省訊息、體驗好）
#     2) 若 reply “timeout” 才考慮 push（因 reply 可能其實送成功但你沒收到 response）
#     3) push 前先用 Redis guard 擋：避免一直拼 push 洗版/燒 quota
#     """
#     t0 = time.time()
#     req = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)

#     try:
#         return line_bot_api.reply_message(req, _request_timeout=timeout)

#     except TypeError:
#         # 某些版本 timeout 只吃 *_with_http_info
#         return line_bot_api.reply_message_with_http_info(req, _request_timeout=timeout)

#     except Exception as e:
#         elapsed = time.time() - t0
#         current_app.logger.exception(
#             "[LINE_%s] reply failed elapsed=%.2fs err=%s", label, elapsed, repr(e)
#         )

#         # 非 timeout：直接結束（不要推，避免 token/權限錯誤時狂推）
#         if not is_timeout_exc(e):
#             return None

#         # timeout 才可能 push
#         if not push_on_timeout:
#             current_app.logger.warning("[LINE_%s] reply timeout but push_on_timeout=False -> skip push", label)
#             return None

#         to_id = _event_to_to_id(event)
#         if not to_id:
#             current_app.logger.warning("[LINE_%s] reply timeout but cannot resolve to_id -> skip push", label)
#             return None

#         # ✅ Redis guard：ttl 內已 push 過就跳過
#         if not _acquire_push_guard(to_id, label, push_guard_ttl_sec):
#             current_app.logger.warning(
#                 "[LINE_%s] reply timeout -> skip fallback push (guard hit ttl=%ss) to=%s",
#                 label, push_guard_ttl_sec, to_id
#             )
#             return None

#         preq = PushMessageRequest(to=to_id, messages=messages)
#         try:
#             current_app.logger.warning("[LINE_%s] reply timeout -> fallback push to=%s", label, to_id)
#             t1 = time.time()
#             r = line_bot_api.push_message(preq, _request_timeout=timeout)
#             current_app.logger.info("[LINE_%s] fallback push ok elapsed=%.2fs to=%s", label, time.time() - t1, to_id)
#             return r
#         except Exception as e2:
#             current_app.logger.exception("[LINE_%s] push failed err=%s to=%s", label, repr(e2), to_id)
#             return None



# # line_send.py
# # import time
# # import urllib3
# # import socket
# # try:
# #     import urllib3
# #     _URLLIB3_TIMEOUT_EXCS = (
# #         urllib3.exceptions.ReadTimeoutError,
# #         urllib3.exceptions.ConnectTimeoutError,
# #         urllib3.exceptions.MaxRetryError,
# #     )
# # except Exception:
# #     urllib3 = None
# #     _URLLIB3_TIMEOUT_EXCS = tuple()
# # from flask import current_app
# # from linebot.v3.messaging import ReplyMessageRequest, PushMessageRequest

# # TIMEOUT_EXCS = (
# #     socket.timeout,
# #     TimeoutError,
# #     urllib3.exceptions.ReadTimeoutError,
# #     urllib3.exceptions.ConnectTimeoutError,
# #     urllib3.exceptions.MaxRetryError,  # 有時候 timeout 會包在這裡
# # )


# # def is_timeout_exc(e: Exception) -> bool:
# #     # 先用「型別」判斷（最準）
# #     if isinstance(e, (socket.timeout, TimeoutError) + _URLLIB3_TIMEOUT_EXCS):
# #         return True

# #     # 再用「訊息字串」補強（避免被包裝漏掉）
# #     msg = repr(e).lower()
# #     return (
# #         "timed out" in msg
# #         or "timeout" in msg
# #         or "readtimeout" in msg
# #         or "connecttimeout" in msg
# #     )

# # def _event_to_to_id(event) -> str:
# #     src = event.source
# #     if getattr(src, "user_id", None):
# #         return src.user_id
# #     if getattr(src, "group_id", None):
# #         return src.group_id
# #     if getattr(src, "room_id", None):
# #         return src.room_id
# #     raise ValueError("Unknown event source")

# # def send_line(line_bot_api, event, messages, *, timeout=(3, 10), label="send"):
# #     """
# #     - 先 reply（一定帶 _request_timeout）
# #     - 只有 timeout 才 fallback push
# #     - 非 timeout：只 log（讓你修 bug / 修 token）
# #     """
# #     t0 = time.time()
# #     req = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)

# #     try:
# #         return line_bot_api.reply_message(req, _request_timeout=timeout)
# #     except TypeError:
# #         # 某些版本 timeout 只吃 *_with_http_info
# #         return line_bot_api.reply_message_with_http_info(req, _request_timeout=timeout)
# #     except Exception as e:
# #         elapsed = time.time() - t0
# #         current_app.logger.exception("[LINE_%s] reply failed elapsed=%.2fs err=%s", label, elapsed, repr(e))

# #         # ✅ 只有 timeout 才 push
# #         if not is_timeout_exc(e):
# #             return None

# #         to_id = _event_to_to_id(event)
# #         preq = PushMessageRequest(to=to_id, messages=messages)

# #         to_id = _event_to_to_id(event)
# #         preq = PushMessageRequest(to=to_id, messages=messages)

# #         try:
# #             current_app.logger.warning("[LINE_%s] reply timeout -> fallback push to=%s", label, to_id)
# #             t1 = time.time()
# #             r = line_bot_api.push_message(preq, _request_timeout=timeout)
# #             current_app.logger.info("[LINE_%s] fallback push ok elapsed=%.2fs to=%s", label, time.time() - t1, to_id)
# #             return r
# #         except Exception as e2:
# #             current_app.logger.exception("[LINE_%s] push failed err=%s to=%s", label, repr(e2), to_id)
# #             return None



