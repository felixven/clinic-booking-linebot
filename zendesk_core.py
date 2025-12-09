import base64
import json
import requests
from flask import current_app as app


from datetime import datetime, timedelta, date

from config import (
    PROFILE_STATUS_EMPTY,
    PROFILE_STATUS_NEED_PHONE,
    PROFILE_STATUS_COMPLETE,
    ZENDESK_SUBDOMAIN,
    ZENDESK_EMAIL,
    ZENDESK_API_TOKEN,
    ZENDESK_CF_BOOKING_ID,
    ZENDESK_CF_APPOINTMENT_DATE,
    ZENDESK_CF_APPOINTMENT_TIME,
    ZENDESK_CF_REMINDER_STATE,
    ZENDESK_CF_REMINDER_ATTEMPTS,
    ZENDESK_CF_LAST_CALL_ID,
    ZENDESK_UF_LINE_USER_ID_KEY,
    ZENDESK_UF_PROFILE_STATUS_KEY,
    ZENDESK_APPOINTMENT_FORM_ID,
    ZENDESK_REMINDER_STATE_PENDING,
    ZENDESK_REMINDER_STATE_QUEUED,
    ZENDESK_REMINDER_STATE_SUCCESS,
    ZENDESK_REMINDER_STATE_FAILED,
    ZENDESK_REMINDER_STATE_CANCELLED,
    APPOINTMENT_DURATION_MINUTES,

)

from bookings_core import (
    parse_booking_datetime_to_local,
)

# ===================== Zendesk Helper：用 line_user_id 查使用者 =====================

def _build_zendesk_headers() -> tuple[str, dict]:
    
    """
    回傳 (base_url, headers)
    """
    base_url: str = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com"
    auth_str: str = f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}"
    auth_bytes: bytes = auth_str.encode("utf-8")
    auth_header: str = base64.b64encode(auth_bytes).decode("utf-8")

    headers: dict = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/json",
    }
    return base_url, headers

def _get_ticket_cf_value(ticket: dict, field_id: int, default=None):
    """
    從 ticket.custom_fields 裡面拿特定欄位的 value。
    """
    for cf in ticket.get("custom_fields") or []:
        if cf.get("id") == field_id:
            return cf.get("value")
    return default

def create_zendesk_user(line_user_id: str, name: str, phone: str):
    """
    建立 Zendesk end-user，並寫入 user_fields.line_user_id。

    流程：
      1. 先檢查是否已有此 line_user_id 的使用者 → 有則直接回傳
      2. 若沒有 → 建立新的 user（含 name / phone / user_fields.line_user_id）
    """
    if not line_user_id:
        app.logger.warning("[create_zendesk_user] 缺少 line_user_id，略過建立 Zendesk user")
        return None

    # 1) 先搜是否已有使用者
    try:
        count, existing_user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[create_zendesk_user] 搜尋 line_user_id 時發生錯誤: {e}")
        existing_user = None

    if existing_user:
        app.logger.info(
            f"[create_zendesk_user] 已存在對應的 Zendesk user, id={existing_user.get('id')}"
        )
        return existing_user

    # 2) 沒有舊資料 → 建立新 user
    base_url, headers = _build_zendesk_headers()  # ⬅️ 新版！統一認證

    url = f"{base_url}/api/v2/users.json"

    # Field key 要和 Zendesk user field 一致（line_user_id）
    payload = {
        "user": {
            "name": name,
            "role": "end-user",
            "phone": phone,
            "verified": True,  # 讓使用者不會 pending verification
            "user_fields": {
                ZENDESK_UF_LINE_USER_ID_KEY: line_user_id,
                ZENDESK_UF_PROFILE_STATUS_KEY: PROFILE_STATUS_COMPLETE,
            },
        }
    }


    app.logger.info(
        f"[create_zendesk_user] 建立新 Zendesk user, name={name}, phone={phone}, line_user_id={line_user_id}"
    )

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[create_zendesk_user] 呼叫 Zendesk API 建立 user 失敗: {e}")
        return None

    data = resp.json()
    user = data.get("user") or {}

    app.logger.info(f"[create_zendesk_user] 建立成功, id={user.get('id')}")
    return user

def upsert_zendesk_user_basic_profile(line_user_id, name=None, phone=None, profile_status=None):
    """
    依 line_user_id 建立或更新一個 Zendesk user：
    - 若已存在 → 更新 name / phone / profile_status（有給才更新）
    - 若不存在 → 建立新的 end-user
    回傳 user dict 或 None。
    """
    if not line_user_id:
        app.logger.warning("[upsert_zendesk_user_basic_profile] 缺少 line_user_id")
        return None

    try:
        count, user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[upsert_zendesk_user_basic_profile] 搜尋 user 失敗: {e}")
        count, user = 0, None

    base_url, headers = _build_zendesk_headers()

    # === 已存在 → update ===
    if user and count == 1:
        user_id = user.get("id")
        if not user_id:
            return user

        url = f"{base_url}/api/v2/users/{user_id}.json"

        user_payload = {}
        if name is not None:
            user_payload["name"] = name
        if phone is not None:
            user_payload["phone"] = phone

        user_fields = user.get("user_fields") or {}
        if line_user_id:
            user_fields[ZENDESK_UF_LINE_USER_ID_KEY] = line_user_id
        if profile_status is not None:
            user_fields[ZENDESK_UF_PROFILE_STATUS_KEY] = profile_status

        if user_fields:
            user_payload["user_fields"] = user_fields

        if not user_payload:
            return user  # 沒東西要更新

        payload = {"user": user_payload}

        try:
            resp = requests.put(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            updated = data.get("user") or user
            app.logger.info(f"[upsert_zendesk_user_basic_profile] 更新 user_id={updated.get('id')} 成功")
            return updated
        except Exception as e:
            app.logger.error(f"[upsert_zendesk_user_basic_profile] 更新 user 失敗: {e}")
            return user

    # === 找不到 → create ===
    url = f"{base_url}/api/v2/users.json"

    user_fields = {}
    if line_user_id:
        user_fields[ZENDESK_UF_LINE_USER_ID_KEY] = line_user_id
    if profile_status is not None:
        user_fields[ZENDESK_UF_PROFILE_STATUS_KEY] = profile_status

    user_body = {
        "role": "end-user",
        "verified": True,
    }
    if name is not None:
        user_body["name"] = name
    if phone is not None:
        user_body["phone"] = phone
    if user_fields:
        user_body["user_fields"] = user_fields

    payload = {"user": user_body}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        created = data.get("user") or {}
        app.logger.info(f"[upsert_zendesk_user_basic_profile] 建立新 user 成功 id={created.get('id')}")
        return created
    except Exception as e:
        app.logger.error(f"[upsert_zendesk_user_basic_profile] 建立 user 失敗: {e}")
        return None


def get_zendesk_user_by_id(user_id: int) -> dict | None:
    """
    給一個 Zendesk user_id，回傳該使用者的完整資料（dict），失敗回傳 None。
    """
    if not user_id:
        return None

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/users/{user_id}.json"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[get_zendesk_user_by_id] 取得使用者失敗 user_id={user_id}: {e}")
        return None

    data = resp.json() or {}
    user = data.get("user")
    if not user:
        app.logger.warning(f"[get_zendesk_user_by_id] 找不到使用者 user_id={user_id}")
        return None

    return user


def get_line_user_id_from_ticket(ticket: dict, appt: dict | None = None) -> str | None:
    """
    優先順序：
    1) 從 ticket.requester_id 對應的 Zendesk User 的 user_fields.line_user_id 拿
    2) 拿不到的話，如果有提供 appt，就從 Bookings appointment 的 notes 裡面解析 [LINE_USER] xxx
    """

    if not ticket:
        return None

    # ① 從 ticket 的 requester_id → user_fields.line_user_id 拿
    try:
        requester_id = ticket.get("requester_id")
        if requester_id:
            # 有時候會是字串，保險轉成 int
            try:
                requester_id_int = int(requester_id)
            except Exception:
                requester_id_int = requester_id

            user = get_zendesk_user_by_id(requester_id_int)
            if user:
                user_fields = user.get("user_fields") or {}
                line_user_id = user_fields.get("line_user_id")  
                if line_user_id:
                    return line_user_id
    except Exception as e:
        app.logger.warning(f"[get_line_user_id_from_ticket] 從 requester user_fields 讀取 line_user_id 失敗: {e}")

    # ② 如果沒拿到，且有 appointment，就從 serviceNotes / customerNotes 找 [LINE_USER]
    if appt:
        notes_parts = []
        service_notes = appt.get("serviceNotes") or ""
        customer_notes = appt.get("customerNotes") or ""
        notes_parts.append(service_notes)
        notes_parts.append(customer_notes)

        notes_text = " ".join(notes_parts).strip()

        marker = "[LINE_USER]"
        if marker in notes_text:
            try:
                # 假設格式為: "[LINE_USER] Ud459ce2c777aaebf52d8f483c9440c47"
                after = notes_text.split(marker, 1)[1].strip()
                candidate = after.split()[0].strip()
                if candidate:
                    return candidate
            except Exception as e:
                app.logger.warning(f"[get_line_user_id_from_ticket] 解析 LINE_USER 失敗: {e}")

    # ③ 真的都找不到
    return None


def search_zendesk_user_by_line_id(line_user_id: str):
    """
    給一個 LINE userId，去 Zendesk 搜尋 user_fields.line_user_id = 這個值 的使用者。

    回傳：
        - count: 幾筆 (int)
        - user: 若 count == 1，回傳那一個 dict，否則 None
    """
    if not line_user_id:
        return 0, None

    # 共用 helper 拿 base_url + headers
    base_url, headers = _build_zendesk_headers()
    search_url: str = f"{base_url}/api/v2/search.json"

    # query 語法：type:user line_user_id:<xxx>
    params: dict = {
        "query": f"type:user line_user_id:{line_user_id}"
    }

    try:
        resp = requests.get(search_url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"Zendesk 搜尋失敗: {e}")
        return 0, None

    data: dict = resp.json()
    count: int = data.get("count", 0)
    results: list = data.get("results") or []

    if count >= 1 and results:
        if count > 1:
            app.logger.warning(
                f"[search_zendesk_user_by_line_id] line_user_id={line_user_id} 有 {count} 筆結果，僅使用第一筆 id={results[0].get('id')}"
            )
        return count, results[0]
    # 完全沒有結果
    return 0, None

    


# =========================================================================
#  Zendesk 核心功能：預約 Ticket 建立
# =========================================================================
def create_zendesk_appointment_ticket(
    booking_id: str,
    local_start_dt: datetime,
    zendesk_customer_id: int,
    customer_name: str,
    booking_service_name: str = "一般門診",
):
    """
    在 Zendesk 內建立一個新的 Ticket，作為預約確認提醒的排程觸發點。
    """
    # 先處理時間相關（不用在這裡組 base_url 了）
    try:
        duration_minutes: int = APPOINTMENT_DURATION_MINUTES
        local_end_dt: datetime = local_start_dt + timedelta(minutes=duration_minutes)
    except NameError as e:
        app.logger.error(
            f"Zendesk 全域變數未定義 (例如 {e})，無法建立 Ticket。"
            "請檢查 APPOINTMENT_DURATION_MINUTES。"
        )
        return None
    except Exception:
        app.logger.warning(
            "APPOINTMENT_DURATION_MINUTES 定義有誤或缺失，使用預設 30 分鐘計算結束時間。"
        )
        local_end_dt: datetime = local_start_dt + timedelta(minutes=30)

    # 共用 helper 拿 base_url + headers
    base_url, headers = _build_zendesk_headers()
    url: str = f"{base_url}/api/v2/tickets.json"

    # ====== 1. 組 subject / body ======
    ticket_subject: str = (
        f"【預約提醒】{customer_name}，將於 "
        f"{local_start_dt.strftime('%Y/%m/%d %H:%M')} 看診"
    )

    ticket_body: str = (
        "這是由 LINE Bot 自動建立的預約提醒 Ticket。\n"
        "請在 **預約日期前 3 天** 確認此 Ticket 狀態。\n\n"
        "--- 預約資料 ---\n"
        f"Bookings ID: {booking_id}\n"
        f"客戶 ID (Zendesk): {zendesk_customer_id}\n"
        f"預約時間: {local_start_dt.strftime('%Y/%m/%d %H:%M')}  ～ "
        f"{local_end_dt.strftime('%H:%M')}\n"
        f"服務項目: {booking_service_name}\n\n"
        "--- 提醒流程 ---\n"
        "如果到期時，Bookings 備註內『尚未』顯示 'Confirmed via LINE'，"
        "則需要通知 LINE Bot 進行回呼確認。"
    )

    # ====== 2. custom_fields ======
    appt_date_str: str = local_start_dt.strftime("%Y-%m-%d")
    appt_time_str: str = local_start_dt.strftime("%H:%M")

    custom_fields = [
        {"id": ZENDESK_CF_BOOKING_ID, "value": booking_id},
        {"id": ZENDESK_CF_APPOINTMENT_DATE, "value": appt_date_str},
        {"id": ZENDESK_CF_APPOINTMENT_TIME, "value": appt_time_str},
        {"id": ZENDESK_CF_REMINDER_STATE, "value": ZENDESK_REMINDER_STATE_PENDING},
        {"id": ZENDESK_CF_REMINDER_ATTEMPTS, "value": 0},
        {"id": ZENDESK_CF_LAST_CALL_ID, "value": ""},
    ]

    payload: dict = {
        "ticket": {
            # ✅ 指定使用「預約專用 Form」
            "ticket_form_id": ZENDESK_APPOINTMENT_FORM_ID,
            "subject": ticket_subject,
            "comment": {"body": ticket_body},
            "requester_id": zendesk_customer_id,
            "status": "pending",
            "tags": ["line_bot_appointment", "pending_confirmation", "booking_sync"],
            "custom_fields": custom_fields,
        }
    }

    # ====== 3. 呼叫 Zendesk API ======
    try:
        app.logger.info(
            f"ZENDESK TICKET PAYLOAD: {json.dumps(payload, ensure_ascii=False)}"
        )
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        ticket = resp.json().get("ticket", {})
        ticket_id: int = ticket.get("id")
        app.logger.info(f"Zendesk Ticket 建立成功，ID: {ticket_id}")
        return resp.json()
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"Zendesk Ticket 建立失敗，HTTP 錯誤: {e.response.status_code}")
        app.logger.error(f"Zendesk 錯誤回應: {e.response.text}")
        return None
    except Exception as e:
        app.logger.error(f"Zendesk Ticket 建立過程中發生未知錯誤: {e}")
        return None
    
def find_zendesk_ticket_by_booking_id(booking_id):
    """
    給一個 Bookings appointment 的 booking_id，
    到 Zendesk 找對應的 Ticket（看 custom_field_XXXXX 裡的值）。

    回傳：
        - 有找到：回傳那一筆 ticket (dict)
        - 沒找到：回傳 None
    """
    if not booking_id:
        app.logger.warning("[find_zendesk_ticket_by_booking_id] 缺少 booking_id，略過搜尋")
        return None

    base_url, headers = _build_zendesk_headers()

    # 這裡用 custom_field_<ticket_field_id>:<value> 的新寫法
    # ZENDESK_CF_BOOKING_ID 是你的 ticket field id（例如 14459987905295）
    field_key = "custom_field_%s" % ZENDESK_CF_BOOKING_ID

    # booking_id 裡面有 = 等字元，包成雙引號比較安全
    query = 'type:ticket %s:"%s"' % (field_key, booking_id)

    search_url = "%s/api/v2/search.json" % base_url
    params = {"query": query}

    try:
        resp = requests.get(search_url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[find_zendesk_ticket_by_booking_id] 呼叫 Zendesk Search 失敗: {e}")
        return None

    data = resp.json()
    results = data.get("results") or []
    count = data.get("count", 0)

    app.logger.info(
        "[find_zendesk_ticket_by_booking_id] STATUS=%s, URL=%s, count=%s"
        % (resp.status_code, resp.url, count)
    )

    if not results:
        app.logger.info(
            "[find_zendesk_ticket_by_booking_id] 找不到 booking_id=%s 的 ticket" % booking_id
        )
        return None

    if len(results) > 1:
        app.logger.warning(
            "[find_zendesk_ticket_by_booking_id] 找到多筆 booking_id=%s 的 ticket，先取第一筆 id=%s"
            % (booking_id, results[0].get("id"))
        )

    return results[0]

def mark_zendesk_ticket_confirmed(ticket_id: int):
    """
    使用者完成「確認回診」後，更新對應的 Zendesk ticket：

      - 將 reminder_state 改成 success
      - 將 ticket 狀態改成 solved

    Args:
        ticket_id: Zendesk ticket id
    """
    if not ticket_id:
        app.logger.warning("[mark_zendesk_ticket_confirmed] 缺少 ticket_id")
        return

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    payload = {
        "ticket": {
            "status": "solved",
            "custom_fields": [
                {
                    "id": ZENDESK_CF_REMINDER_STATE,
                    "value": ZENDESK_REMINDER_STATE_SUCCESS
                }
            ]
        }
    }

    app.logger.info(
        f"[mark_zendesk_ticket_confirmed] 更新 ticket_id={ticket_id}, payload="
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(
            f"[mark_zendesk_ticket_confirmed] 更新成功 ticket_id={ticket_id}"
        )
    except Exception as e:
        app.logger.error(f"[mark_zendesk_ticket_confirmed] 更新失敗: {e}")

def mark_zendesk_ticket_cancelled(ticket_id: int):
    """
    使用者「取消約診」後，更新該 ticket 狀態：

      - reminder_state 改成cancelled）
      - ticket 狀態改成 solved

    Args:
        ticket_id: Zendesk ticket id
    """
    if not ticket_id:
        app.logger.warning("[mark_zendesk_ticket_cancelled] 缺少 ticket_id")
        return

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    payload = {
        "ticket": {
            "status": "solved",
            "custom_fields": [
                {
                    "id": ZENDESK_CF_REMINDER_STATE,
                    "value": ZENDESK_REMINDER_STATE_CANCELLED
                }
            ]
        }
    }

    app.logger.info(
        f"[mark_zendesk_ticket_cancelled] 更新 ticket_id={ticket_id}, payload="
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(
            f"[mark_zendesk_ticket_cancelled] 更新成功 ticket_id={ticket_id}"
        )
    except Exception as e:
        app.logger.error(f"[mark_zendesk_ticket_cancelled] 更新失敗: {e}")

def mark_zendesk_ticket_queued(ticket_id: int, ticket: dict | None = None):
    """
    將提醒狀態改成「已排入外撥」，並把 reminder_attempts + 1。
    """
    if not ticket_id:
        app.logger.warning("[mark_zendesk_ticket_queued] 缺少 ticket_id")
        return

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    # 嘗試從 ticket 算 attempts，沒有就從 0 開始
    attempts = 0
    if ticket is not None:
        try:
            attempts = _get_ticket_cf_value(ticket, ZENDESK_CF_REMINDER_ATTEMPTS, 0) or 0
            attempts = int(attempts)
        except Exception:
            attempts = 0
    attempts += 1

    payload = {
        "ticket": {
            "custom_fields": [
                # ✅ 用常數，不要硬寫 "queued"
                {"id": ZENDESK_CF_REMINDER_STATE, "value": ZENDESK_REMINDER_STATE_QUEUED},
                {"id": ZENDESK_CF_REMINDER_ATTEMPTS, "value": attempts},
            ]
        }
    }

    app.logger.info(
        f"[mark_zendesk_ticket_queued] 更新 ticket_id={ticket_id}, payload={json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(f"[mark_zendesk_ticket_queued] 更新成功 ticket_id={ticket_id}")
    except Exception as e:
        app.logger.error(f"[mark_zendesk_ticket_queued] 更新失敗: {e}")

    
def search_zendesk_tickets_for_reminder():
    """
    找出：
    - 使用預約表單 (ticket_form_id = ZENDESK_APPOINTMENT_FORM_ID)
    - reminder_state = pending
    - 狀態不是 solved 的 ticket
    """
    base_url, headers = _build_zendesk_headers()
    search_url = f"{base_url}/api/v2/search.json"

    field_key = f"custom_field_{ZENDESK_CF_REMINDER_STATE}"
    query = (
        f"type:ticket "
        f"ticket_form_id:{ZENDESK_APPOINTMENT_FORM_ID} "
        f"-status:solved "
        f"{field_key}:{ZENDESK_REMINDER_STATE_PENDING}"
    )

    params = {"query": query}

    try:
        resp = requests.get(search_url, headers=headers, params=params, timeout=10)
        app.logger.info(f"[search_zendesk_tickets_for_reminder] URL = {resp.url}")
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[search_zendesk_tickets_for_reminder] 失敗: {e}")
        return []

    data = resp.json() or {}
    results = data.get("results") or []
    app.logger.info(
        f"[search_zendesk_tickets_for_reminder] 命中 {len(results)} 筆候選 ticket（reminder_state = pending）"
    )

    if results:
        first = results[0]
        app.logger.info(
            "[search_zendesk_tickets_for_reminder] 第一筆 custom_fields = "
            + json.dumps(first.get("custom_fields") or [], ensure_ascii=False)
        )

    return results