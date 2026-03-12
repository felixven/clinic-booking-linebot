from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import requests
import re
import json
from flask import current_app as app  # 用 app.logger

from config import (
    BOOKINGS_DEMO_SERVICE_ID,
    BOOKINGS_DEMO_STAFF_ID,
    BOOKINGS_BUSINESS_CLINIC_ID,
    SLOT_INTERVAL_MINUTES,
    APPOINTMENT_DURATION_MINUTES,
    MORNING_START, 
    MORNING_END,
    AFTERNOON_START,
    AFTERNOON_END,
    FRI_MORNING_START, 
    FRI_MORNING_END,
    SAT_MORNING_START, 
    SAT_MORNING_END,
    CLOSED_WEEKDAYS,
    ACU_SLOTS,
    ACU_SERVICE_IDS,
    ACU_STAFF_BED1_ID,
    ACU_STAFF_BED2_ID,
    BOOKINGS_BUSINESS_ACU_ID,
    BOOKINGS_SERVICE_ACU_BED1_ID,
    BOOKINGS_SERVICE_ACU_BED2_ID,
    BOOKINGS_BUSINESS_ACU_BED1_ID,
    BOOKINGS_BUSINESS_ACU_BED2_ID,
    BOOKINGS_SERVICE_ACU_BED_ID,
    CLINIC_MORNING_START, 
    CLINIC_MORNING_END,
    CLINIC_EVENING_END,
    CLINIC_EVENING_START
)

# ======== 跟 Entra 拿 Microsoft Graph 的 access token ========

def get_graph_token():
    tenant_id = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET")

    if not tenant_id or not client_id or not client_secret:
        raise Exception(
            "GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET 有缺，先到終端機 export")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    resp = requests.post(url, data=data)
    app.logger.info(
        f"GRAPH TOKEN STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()
    return resp.json()["access_token"]


def _get_clinic_day_intervals_by_session(session: str) -> list[tuple[str, str]]:
    # session: "morning" / "evening"
    if session == "morning":
        return [(CLINIC_MORNING_START, CLINIC_MORNING_END)]
    if session == "evening":
        return [(CLINIC_EVENING_START, CLINIC_EVENING_END)]
    return []

    
def get_available_clinic_slots_for_session(date_str: str, business_id: str, session: str) -> list[str]:
    intervals = _get_clinic_day_intervals_by_session(session)
    if not intervals:
        return []

    appts = list_appointments_for_date(date_str, business_id=business_id)

    booked_times = set()
    for appt in appts:
        s = (appt.get("startDateTime", {}) or {}).get("dateTime")
        if not s:
            continue
        try:
            if s.endswith("Z"):
                s = s[:-1]
            if "." in s:
                s = s.split(".", 1)[0]
            utc_dt = datetime.fromisoformat(s)
            local_dt = utc_dt + timedelta(hours=8)
            booked_times.add(local_dt.strftime("%H:%M"))
        except Exception:
            continue

    slots = []
    duration = APPOINTMENT_DURATION_MINUTES

    for start_hhmm, end_hhmm in intervals:
        cur = datetime.strptime(start_hhmm, "%H:%M")
        end_dt = datetime.strptime(end_hhmm, "%H:%M")

        while cur + timedelta(minutes=duration) <= end_dt:
            hhmm = cur.strftime("%H:%M")
            if hhmm not in booked_times:
                slots.append(hhmm)
            cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots

def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
    """
    將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 naive datetime」。
    例如:
        2025-11-20T06:00:00Z
        2025-11-20T06:00:00.0000000Z
    -> 2025-11-20 14:00:00
    """
    if not start_dt_str:
        return None

    try:
        s = start_dt_str.strip()

        if s.endswith("Z"):
            s = s[:-1]

        if "." in s:
            s = s.split(".", 1)[0]

        utc_dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone(ZoneInfo("Asia/Taipei"))

        # 回傳 naive local，避免舊程式比較時炸掉
        return local_dt.replace(tzinfo=None)

    except Exception as e:
        app.logger.error(
            f"[parse_booking_datetime_to_local] 解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}"
        )
        return None

# def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
#     """
#     將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成台北時間 datetime
#     例如:
#         2025-11-20T06:00:00Z
#         2025-11-20T06:00:00.0000000Z
#     -> 2025-11-20 14:00:00+08:00
#     """
#     if not start_dt_str:
#         return None

#     try:
#         s = start_dt_str.strip()

#         # 去掉尾巴 Z
#         if s.endswith("Z"):
#             s = s[:-1]

#         # 有小數秒就只留到秒
#         if "." in s:
#             s = s.split(".", 1)[0]

#         # 先視為 UTC
#         utc_dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

#         # 轉台北時間
#         local_dt = utc_dt.astimezone(ZoneInfo("Asia/Taipei"))
#         return local_dt

#     except Exception as e:
#         app.logger.error(
#             f"[parse_booking_datetime_to_local] 解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}"
#         )
#         return None
    
def get_clinic_period_from_local_dt(local_dt: datetime | None) -> str | None:
    if not local_dt:
        return None

    hhmm = local_dt.strftime("%H:%M")

    if MORNING_START <= hhmm <= MORNING_END:
        return "morning"

    if AFTERNOON_START <= hhmm <= AFTERNOON_END:
        return "evening"

    return None

import re

def extract_line_user_id_from_service_notes(service_notes: str) -> str | None:
    if not service_notes:
        return None

    m = re.search(r"\[LINE_USER\]\s*(\S+)", service_notes)
    return m.group(1).strip() if m else None


def has_existing_clinic_period_booking(line_user_id: str, date_str: str, period: str) -> bool:
    """
    檢查同一病患是否已存在同一天同時段（早/晚）的門診預約
    """
    try:
        appts = list_appointments_for_date(
            date_str=date_str,
            business_id=BOOKINGS_BUSINESS_CLINIC_ID,
        ) or []

        for appt in appts:
            service_notes = appt.get("serviceNotes") or ""
            appt_line_user_id = extract_line_user_id_from_service_notes(service_notes)

            if appt_line_user_id != line_user_id:
                continue

            start_obj = appt.get("startDateTime") or {}
            start_dt_str = start_obj.get("dateTime")
            local_dt = parse_booking_datetime_to_local(start_dt_str)
            appt_period = get_clinic_period_from_local_dt(local_dt)

            if appt_period != period:
                continue

            return True

        return False

    except Exception as e:
        app.logger.error(f"[has_existing_clinic_period_booking] error: {e}")
        return False

# def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
#     """
#     將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 datetime」。
#     例如:
#         "2025-11-20T06:00:00Z"
#         "2025-11-20T06:00:00.0000000Z"
#     都會轉成：2025-11-20 14:00:00 (UTC+8)
#     """
#     if not start_dt_str:
#         return None

#     try:
#         s = start_dt_str.strip()

#         # 1) 去掉尾巴的 Z
#         if s.endswith("Z"):
#             s = s[:-1]

#         # 2) 有小數秒就只留到秒
#         if "." in s:
#             s = s.split(".", 1)[0]

#         # 3) 變成 datetime（目前視為 naive UTC）
#         utc_dt = datetime.fromisoformat(s)

#     except Exception as e:
#         app.logger.error(
#             f"[parse_booking_datetime_to_local] 解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}"
#         )
#         return None

#     # 4) 加上 8 小時變成台北時間（之後真的上線要改成用 tz aware 再說）
#     local_dt = utc_dt + timedelta(hours=8)
#     return local_dt

# --- 輔助函式：取得指定日期所有預約 (實際呼叫 Graph API) ---
def list_appointments_for_date(date_str: str, business_id: str) -> list:
    """
    從 Bookings 取得指定日期 (台北時間, YYYY-MM-DD) 的所有預約列表。
    回傳: 預約列表 (list of dict)
    """
    token: str = get_graph_token()

    # 用傳入的 business_id；沒傳才 fallback（最好都要傳）
    business_id = business_id or os.environ.get("BOOKING_BUSINESS_CLINIC_ID") or BOOKINGS_BUSINESS_CLINIC_ID
    if not business_id:
        raise Exception("缺 business_id（BOOKING_BUSINESS_CLINIC_ID），請檢查環境變數。")
    

    # 1. 計算 UTC 範圍 (將台北時間 T+08:00 轉換為 UTC)
    try:
        local_start_dt: datetime = datetime.strptime(f"{date_str} 00:00:00", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        app.logger.error(f"日期格式錯誤，請使用 YYYY-MM-DD: {date_str}")
        return []

    local_end_dt: datetime = local_start_dt + timedelta(days=1)

    utc_start_dt: datetime = local_start_dt - timedelta(hours=8)
    utc_end_dt: datetime = local_end_dt - timedelta(hours=8)

    start_time: str = utc_start_dt.replace(microsecond=0).isoformat() + "Z"
    end_time: str = utc_end_dt.replace(microsecond=0).isoformat() + "Z"

    app.logger.info(f"[list_appointments_for_date] business_id={business_id} date={date_str}")
    url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/calendarView"
    headers: dict = {"Authorization": f"Bearer {token}"}
    params: dict = {"start": start_time, "end": end_time}

    resp = requests.get(url, headers=headers, params=params)
    app.logger.info(f"CALENDAR VIEW STATUS: {resp.status_code}, URL: {resp.url}")
    resp.raise_for_status()
    return resp.json().get("value", [])

# def list_appointments_for_date(date_str: str, business_id: str) -> list:
#     """
#     從 Bookings 取得指定日期 (台北時間, YYYY-MM-DD) 的所有預約列表。
#     回傳: 預約列表 (list of dict)
#     """
#     token: str = get_graph_token()
#     business_id: str = os.environ.get("BOOKING_BUSINESS_ID") or BOOKING_BUSINESS_ID

#     if not business_id:
#         raise Exception("缺 BOOKING_BUSINESS_ID，請檢查環境變數。")

#     # 1. 計算 UTC 範圍 (將台北時間 T+08:00 轉換為 UTC)
#     try:
#         # 台北時間 (UTC+8) 的 00:00:00
#         local_start_dt: datetime = datetime.strptime(f"{date_str} 00:00:00", "%Y-%m-%d %H:%M:%S")
#     except ValueError:
#         app.logger.error(f"日期格式錯誤，請使用 YYYY-MM-DD: {date_str}")
#         return []

#     local_end_dt: datetime = local_start_dt + timedelta(days=1)

#     # 轉為 UTC 時間 (減 8 小時)
#     utc_start_dt: datetime = local_start_dt - timedelta(hours=8)
#     utc_end_dt: datetime = local_end_dt - timedelta(hours=8)

#     # 格式化為 Graph API 要求的 ISO 格式
#     start_time: str = utc_start_dt.isoformat() + "Z"
#     end_time: str = utc_end_dt.isoformat() + "Z"

#     # 2. 呼叫 calendarView API
#     url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/calendarView"

#     headers: dict = {
#         "Authorization": f"Bearer {token}"
#     }

#     params: dict = {
#         "start": start_time,
#         "end": end_time
#     }

#     # 執行 API 呼叫
#     resp = requests.get(url, headers=headers, params=params)
#     app.logger.info(
#         f"CALENDAR VIEW STATUS: {resp.status_code}, URL: {resp.url}")

#     resp.raise_for_status()

#     # calendarView 回傳的結果已經是該日期範圍內 (UTC+8) 的預約
#     return resp.json().get("value", [])

def list_appointments_for_range(
    start_local: datetime,
    end_local: datetime,
    business_id: str | None = None,
):
    """
    一次從 Bookings 抓「某個時間範圍內」所有 appointments。

    傳入的 start_local / end_local 是「台北時間（naive）」，
    我們會轉成 UTC 後呼叫 Graph API：
    GET /solutions/bookingBusinesses/{business_id}/appointments?
        startDateTime=...&endDateTime=...

    回傳：list[dict]（appointments 清單）
    """
    print(
        f"[LIST_APPTS_RANGE] enter "
        f"start_local={start_local!r} end_local={end_local!r} "
        f"business_id_arg={business_id!r}",
        flush=True
    )

    token = get_graph_token()

    # business_id 來源優先順序：
    # 1) 呼叫端傳入
    # 2) 環境變數 BOOKING_BUSINESS_ID（舊版相容）
    # 3) （可選）自己加 BOOKING_BUSINESS_CLINIC_ID / BOOKING_BUSINESS_ACU_ID 在外面做分流後傳入
    business_id = (business_id or "").strip() or (os.environ.get("BOOKING_BUSINESS_ID") or "").strip()

    print(f"[LIST_APPTS_RANGE] resolved business_id={business_id!r}", flush=True)

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID（或呼叫時未傳入 business_id）")

    # ===== 時間處理（穩健版）=====
    # start_local / end_local 是台北時間 naive
    # 把 naive 視為 Asia/Taipei → 轉成 UTC → 格式化為 ISO + Z
    tz_taipei = ZoneInfo("Asia/Taipei")

    if start_local.tzinfo is None:
        start_local_aware = start_local.replace(tzinfo=tz_taipei)
    else:
        start_local_aware = start_local.astimezone(tz_taipei)

    if end_local.tzinfo is None:
        end_local_aware = end_local.replace(tzinfo=tz_taipei)
    else:
        end_local_aware = end_local.astimezone(tz_taipei)

    start_utc = start_local_aware.astimezone(timezone.utc)
    end_utc = end_local_aware.astimezone(timezone.utc)

    # 轉成 ISO 格式，補上 Z（Graph 接受）
    start_iso = start_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_iso = end_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    print(
        f"[LIST_APPTS_RANGE] time_convert "
        f"start_local_aware={start_local_aware.isoformat()} "
        f"end_local_aware={end_local_aware.isoformat()} "
        f"start_iso={start_iso} end_iso={end_iso}",
        flush=True
    )

    # ===== Graph 呼叫 =====
    url = (
        f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
        f"?startDateTime={start_iso}&endDateTime={end_iso}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    all_items: list[dict] = []
    page = 0

    while True:
        page += 1
        print(f"[LIST_APPTS_RANGE] GET page={page} url={url}", flush=True)

        resp = requests.get(url, headers=headers)

        # print狀態碼
        print(
            f"[LIST_APPTS_RANGE] RESP page={page} status={resp.status_code} body_head={resp.text[:200]!r}",
            flush=True
        )
        app.logger.info(
            f"[LIST_APPTS_RANGE] STATUS page={page}: {resp.status_code}, BODY_HEAD: {resp.text[:500]}"
        )

        # 失敗就直接噴
        resp.raise_for_status()

        data = resp.json() or {}
        items = data.get("value") or []
        all_items.extend(items)

        next_link = data.get("@odata.nextLink") or data.get("odata.nextLink")
        if not next_link:
            break

        # Graph nextLink 已含完整 URL
        url = next_link

    print(f"[LIST_APPTS_RANGE] done total_items={len(all_items)}", flush=True)
    return all_items

# 不支援business ID版本
# def list_appointments_for_range(start_local: datetime, end_local: datetime):
#     """
#     一次從 Bookings 抓「某個時間範圍內」所有 appointments。

#     傳入的 start_local / end_local 是「台北時間（naive）」，
#     我們會轉成 UTC 後呼叫 Graph API：
#     GET /solutions/bookingBusinesses/{business_id}/appointments?
#         startDateTime=...&endDateTime=...

#     回傳：list[dict]（appointments 清單）
#     """
#     token = get_graph_token()
#     business_id = os.environ.get("BOOKING_BUSINESS_ID")

#     if not business_id:
#         raise Exception("缺 BOOKING_BUSINESS_ID")

#     # 先把台北時間（UTC+8）轉成 UTC 時間
#     start_utc = start_local - timedelta(hours=8)
#     end_utc = end_local - timedelta(hours=8)

#     # 轉成 ISO 格式，補上 Z
#     start_iso = start_utc.replace(microsecond=0).isoformat() + "Z"
#     end_iso = end_utc.replace(microsecond=0).isoformat() + "Z"

#     url = (
#         f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
#         f"?startDateTime={start_iso}&endDateTime={end_iso}"
#     )

#     headers = {
#         "Authorization": f"Bearer {token}",
#         "Content-Type": "application/json",
#     }

#     resp = requests.get(url, headers=headers)
#     app.logger.info(
#         f"LIST APPTS RANGE STATUS: {resp.status_code}, BODY: {resp.text[:500]}"
#     )
#     resp.raise_for_status()

#     data = resp.json()
#     # 通常 Graph 會把結果放在 value 裡
#     return data.get("value", [])

# 0212新增版本
def get_appointment_by_id(appt_id: str, business_id: str):
    """
    用指定 bookingBusiness 的 appointment id 取得單一預約資訊。
    回傳 (appointment_dict, local_start_dt)；
    找不到或解析失敗則回 (None, None)。
    """
    if not appt_id:
        return None, None
    if not business_id:
        raise Exception("get_appointment_by_id: business_id 為空")

    token = get_graph_token()

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"GET APPOINTMENT biz={business_id} appt={appt_id} STATUS: {resp.status_code}, BODY: {resp.text}"
    )

    if resp.status_code == 404:
        return None, None

    resp.raise_for_status()
    appt = resp.json()
    appt["_business_id"] = business_id  # ✅ 回傳時順便帶著

    start_info = appt.get("startDateTime", {})
    local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
    if not local_dt:
        return None, None

    return appt, local_dt


# 0212前版本
# def get_appointment_by_id(appt_id: str):
#     """
#     用 Bookings appointment id 取得單一預約資訊。
#     回傳 (appointment_dict, local_start_dt)；
#     找不到或解析失敗則回 (None, None)。
#     """
#     if not appt_id:
#         return None, None

#     token = get_graph_token()
#     business_id = os.environ.get("BOOKINGS_BUSINESS_CLINIC_ID")

#     if not business_id:
#         raise Exception("缺 BOOKINGS_BUSINESS_CLINIC_ID")

#     url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
#     headers = {
#         "Authorization": f"Bearer {token}"
#     }

#     resp = requests.get(url, headers=headers)
#     app.logger.info(
#         f"GET APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

#     if resp.status_code == 404:
#         # 已被刪除或不存在
#         return None, None

#     resp.raise_for_status()
#     appt = resp.json()

#     app.logger.info(f"APPOINTMENT KEYS: {list(appt.keys())}")
#     app.logger.info(
#         f"APPT NOTES FIELDS: serviceNotes={appt.get('serviceNotes')}, "
#         f"customerNotes={appt.get('customerNotes')}"
#     )

#     start_info = appt.get("startDateTime", {})
#     local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
#     if not local_dt:
#         return None, None

#     return appt, local_dt



def cancel_booking_appointment(appt_id: str, business_id: str = None):
    """
    取消 Bookings appointment（DELETE）。

    - 正式：由呼叫端傳入 business_id（clinic / acu 各自的 Bookings business）
    - 相容：如果沒傳 business_id，才 fallback 用 env: BOOKING_BUSINESS_ID（舊 DEMO 行為）
    """
    if not appt_id:
        raise Exception("cancel_booking_appointment: appt_id 為空")

    token = get_graph_token()

    # ✅ 正式優先用傳入的 biz；沒傳才 fallback 舊 env
    biz = (business_id or "").strip() or os.environ.get("BOOKING_BUSINESS_ID")
    if not biz:
        raise Exception("缺 business_id（未傳入且 env BOOKING_BUSINESS_ID 也不存在）")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{biz}/appointments/{appt_id}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.delete(url, headers=headers)
    app.logger.info(
        f"DELETE APPOINTMENT biz={biz} appt={appt_id} STATUS: {resp.status_code}, BODY: {resp.text}"
    )

    # 204 No Content / 200 / 202 都算成功
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


def update_booking_service_notes(appt_id: str, notes_text: str, business_id: str):
    """
    將指定 appointment 的 serviceNotes 更新為 notes_text。(診所／工作人員可以看的備註)
    """
    if not appt_id:
        raise Exception("update_booking_service_notes: appt_id 為空")
    
    if not business_id:
        raise Exception("update_booking_service_notes: business_id 為空")

    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"serviceNotes": notes_text}

    resp = requests.patch(url, headers=headers, json=payload)
    app.logger.info(
        f"PATCH APPT SERVICE NOTES biz={business_id} appt={appt_id} STATUS: {resp.status_code}, BODY: {resp.text}"
    )
    resp.raise_for_status()

def _get_day_intervals(date_str: str):
    """
    回傳當天可預約的門診區間（多段）
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    wd = d.weekday()  # 0=Mon ... 6=Sun

    # 週三、週日休診
    if wd in CLOSED_WEEKDAYS:
        return []

    # 週一、週二、週四
    if wd in (0, 1, 3):
        return [
            (MORNING_START, MORNING_END),
            (AFTERNOON_START, AFTERNOON_END),
        ]

    # 週五
    if wd == 4:
        return [(FRI_MORNING_START, FRI_MORNING_END)]

    # 週六
    if wd == 5:
        return [(SAT_MORNING_START, SAT_MORNING_END)]

    return []

# def _get_day_hours(date_str: str) -> tuple[str | None, str | None]:
#     """
#     依日期回傳當天可預約的 start/end（HH:MM）。
#     end 定義為「門診結束時間」（不是最後一格開始時間）。
#     """
#     d = datetime.strptime(date_str, "%Y-%m-%d").date()
#     wd = d.weekday()  # 0=Mon ... 6=Sun

#     if wd in CLOSED_WEEKDAYS:
#         return None, None

#     # 週六半天
#     if wd == 5:
#         return SLOT_START, SAT_END

#     # 週三早上不看
#     if wd == 2:
#         return WED_START, SLOT_END

#     # 週一、二、四、五
#     return SLOT_START, SLOT_END


def get_available_slots_for_date(date_str: str, business_id: str) -> list:
    """
    回傳指定日期「可預約」的時段列表（20 分鐘一格）
    """
    intervals = _get_day_intervals(date_str)
    if not intervals:
        return []

    # 取得當天已存在的 Bookings 預約
    appts = list_appointments_for_date(date_str, business_id=business_id)

    booked_times = set()
    for appt in appts:
        start_info = appt.get("startDateTime", {})
        s = start_info.get("dateTime")
        if not s:
            continue
        try:
            if s.endswith("Z"):
                s = s[:-1]
            if "." in s:
                s = s.split(".", 1)[0]
            utc_dt = datetime.fromisoformat(s)
            local_dt = utc_dt + timedelta(hours=8)
            booked_times.add(local_dt.strftime("%H:%M"))
        except Exception:
            continue

    slots = []
    duration = APPOINTMENT_DURATION_MINUTES

    for start_hhmm, end_hhmm in intervals:
        cur = datetime.strptime(start_hhmm, "%H:%M")
        end_dt = datetime.strptime(end_hhmm, "%H:%M")

        while cur + timedelta(minutes=duration) <= end_dt:
            hhmm = cur.strftime("%H:%M")
            if hhmm not in booked_times:
                slots.append(hhmm)
            cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots


# def get_available_slots_for_date(date_str: str) -> list:
#     """
#     回傳指定日期「可預約」的時段列表，例如：
#     ["09:00", "09:30", "10:00", ...]
#     規則：SLOT_START–SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘，排除當天已被預約的開始時段。
#     """
#     appts: list = list_appointments_for_date(date_str)

#     booked_times: set = set()
#     for appt in appts:
#         start_info: dict = appt.get("startDateTime", {})
#         # "2025-11-20T06:00:00.0000000Z"
#         start_dt_str: str = start_info.get("dateTime")
#         if not start_dt_str:
#             continue

#         try:
#             s: str = start_dt_str
#             if s.endswith("Z"):
#                 s = s[:-1]
#             s = s.split(".")[0]
#             utc_dt: datetime = datetime.fromisoformat(s)
#         except Exception as e:
#             app.logger.error(
#                 f"解讀 startDateTime 失敗（get_available_slots）：{start_dt_str}, error: {e}")
#             continue

#         local_dt: datetime = utc_dt + timedelta(hours=8)
#         hhmm: str = local_dt.strftime("%H:%M")  # 例如 "14:00"
#         booked_times.add(hhmm)

#     # SLOT_START ~ SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘一格
#     # 這裡假設日期是今天，只取時間部分
#     start_dt_only: datetime = datetime.strptime(SLOT_START, "%H:%M").replace(year=2000, month=1, day=1)
#     end_dt_only: datetime = datetime.strptime(SLOT_END, "%H:%M").replace(year=2000, month=1, day=1)


#     slots: list = []
#     cur: datetime = start_dt_only
#     while cur <= end_dt_only:
#         hhmm: str = cur.strftime("%H:%M")
#         if hhmm not in booked_times:
#             slots.append(hhmm)
#         cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

#     return slots

def create_booking_appointment(
    date_str: str,
    time_str: str,
    customer_name: str,
    customer_phone: str,
    zendesk_customer_id: str, # <--- 修正為 str
    line_display_name: str = None,
    line_user_id: str = None,
    business_id: str = None,
    service_id: str = None,            # 新增 - 針灸
    staff_member_ids: list[str] = None # 新增（可傳可不傳）
):
    """
    建立一筆 Bookings 預約。
    - 改用真實病患資料（Zendesk 的姓名＋手機）
    - customerName：姓名 +（LINE 名稱）→ 例如：王凱文（Kevin）
    - serviceNotes：第一行寫入 [LINE_USER] <line_user_id>，方便後續排程／查詢
    
    並在成功後，自動建立 Zendesk Ticket 進行提醒排程。
    回傳: 建立的預約 dict。
    """

    token: str = get_graph_token()

    business_id = business_id or os.environ.get("BOOKING_BUSINESS_CLINIC_ID") or BOOKINGS_BUSINESS_CLINIC_ID
    if not business_id:
        raise Exception("缺 business_id（BOOKING_BUSINESS_ID）")

    # --- 1. 準備 Bookings Payload (邏輯與您的原始碼一致) ---
    local_str: str = f"{date_str} {time_str}:00"
    local_dt: datetime = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S") # 預約的台北時間 (UTC+8)

    # Bookings API 使用 UTC 台北時間 - 8 小時
    utc_dt: datetime = local_dt - timedelta(hours=8)
    utc_iso: str = utc_dt.isoformat() + "Z"

    #Booking UI 顯示的資料
    hh, mm = time_str.split(":")
    display_time = f"{int(hh)}：{mm}"
    display_title = f"{customer_name} {display_time}"

    # 要寫進 Bookings 的姓名
    if line_display_name:
        booking_customer_name: str = f"{customer_name}（{line_display_name}）"
    else:
        booking_customer_name: str = customer_name

    # 預先組好 serviceNotes
    service_notes_lines: list = []
    if line_user_id:
        service_notes_lines.append(f"[LINE_USER] {line_user_id}")

    if zendesk_customer_id:
        service_notes_lines.append(f"[ZD_USER] {zendesk_customer_id}")

    service_notes: str = "\n".join(service_notes_lines) if service_notes_lines else None

    # URL 和 Duration 常數
    url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
    duration: int = APPOINTMENT_DURATION_MINUTES 

    # fallback：沒傳就用原本 demo 常數（不影響流程）
    final_service_id = service_id or BOOKINGS_DEMO_SERVICE_ID

    # staff：針灸才一定需要；內科若不想指定，就讓它 None
    # （目前呼叫端：針灸會傳 staff_member_ids；內科傳 None）
    final_staff_ids = staff_member_ids  # 不再 fallback 成 demo staff，避免內科/針灸 business 不相容


    # payload: dict = {
    #     "customerName": booking_customer_name,
    #     "customerEmailAddress": None,
    #     "customerPhone": customer_phone,
    #     "serviceId": final_service_id,
    #     "serviceName": display_title,
    #     "startDateTime": { "dateTime": utc_iso, "timeZone": "UTC" },
    #     "endDateTime": {
    #         "dateTime": (utc_dt + timedelta(minutes=duration)).isoformat() + "Z",
    #         "timeZone": "UTC",
    #     },
    #     "priceType": "free",
    #     "price": 0.0,
    #     "smsNotificationsEnabled": False,
    #     "staffMemberIds": final_staff_ids,
    #     "maximumAttendeesCount": 1,
    #     "filledAttendeesCount": 1,
    # }
    payload: dict = {
        "customerName": booking_customer_name,
        "customerPhone": customer_phone,
        "serviceId": final_service_id,
        "startDateTime": { "dateTime": utc_iso, "timeZone": "UTC" },
        "endDateTime": {
            "dateTime": (utc_dt + timedelta(minutes=duration)).replace(microsecond=0).isoformat() + "Z",
            "timeZone": "UTC",
        },
        "smsNotificationsEnabled": False,
    }

    # 針灸：有 staff 才塞（內科不塞，讓 Bookings 自己決定）
    if final_staff_ids:
        payload["staffMemberIds"] = final_staff_ids

    # 有內容時才塞 serviceNotes
    if service_notes:
        payload["serviceNotes"] = service_notes


    headers: dict = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # --- 2. 建立 Bookings 預約 ---
    resp = requests.post(url, headers=headers, json=payload)
    app.logger.info(f"CREATE APPT STATUS: {resp.status_code}, BODY: {resp.text}")

    # ===== DEBUG（保留 print，不刪）=====
    print(
        "[CREATE_APPT] ready "
        f"business_id={business_id} "
        f"service_id={final_service_id} "
        f"staff_ids={final_staff_ids} "
        f"local={date_str} {time_str} "
        f"utc_start={utc_iso} "
        f"duration_min={duration} "
        f"customer_phone={customer_phone} "
        f"zd_user={zendesk_customer_id} "
        f"line_user={line_user_id}",
        flush=True
    )

    # 也把 payload 重要欄位印出來（避免整包太長）
    try:
        _dbg_payload = {
            "customerName": payload.get("customerName"),
            "customerPhone": payload.get("customerPhone"),
            "serviceId": payload.get("serviceId"),
            "staffMemberIds": payload.get("staffMemberIds"),
            "startDateTime": payload.get("startDateTime"),
            "endDateTime": payload.get("endDateTime"),
            "smsNotificationsEnabled": payload.get("smsNotificationsEnabled"),
            "serviceNotes": payload.get("serviceNotes"),
        }
        print(f"[CREATE_APPT] payload_core={json.dumps(_dbg_payload, ensure_ascii=False)}", flush=True)
    except Exception as e:
        print(f"[CREATE_APPT] payload_core dump failed err={repr(e)}", flush=True)


    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # ✅ 把 Graph 的錯誤 JSON 印出來，通常會有 message + innerError
        app.logger.error(f"[CREATE_APPT][HTTPError] status={resp.status_code} body={resp.text}")
        app.logger.error(f"[CREATE_APPT][payload] {json.dumps(payload, ensure_ascii=False)}")
        raise

    resp.raise_for_status()
    created_booking: dict = resp.json()
    
    # --- 3. 整合功能：呼叫 Zendesk Ticket 建立 (在 Bookings 成功後) ---
    # 這裡檢查 zendesk_customer_id 是否存在，並將其從 str 轉換為 int
    # if zendesk_customer_id:
    #     try:
    #         zendesk_id_int: int = int(zendesk_customer_id)
    #     except ValueError:
    #         app.logger.error(f"Zendesk User ID 無法轉換為整數: {zendesk_customer_id}，跳過建立 Ticket 流程。")
    #         return created_booking

    #     booking_id: str = created_booking.get("id")
    #     if not booking_id:
    #         app.logger.error("Bookings 預約建立成功，但未取得 Bookings ID，無法建立 Zendesk Ticket。")
    #     else:
    #         ticket_result: dict = create_zendesk_appointment_ticket(
    #             booking_id=booking_id,
    #             local_start_dt=local_dt, 
    #             zendesk_customer_id=zendesk_id_int, # 傳入 int
    #             customer_name=customer_name,
    #         )
    #         if ticket_result:
    #             app.logger.info(f"Zendesk Ticket ID: {ticket_result.get('ticket', {}).get('id')}")
    #         else:
    #             app.logger.error("Zendesk Ticket 建立失敗。")
    # else:
    #     app.logger.warning("未取得 Zendesk User ID，跳過建立預約 Ticket 流程。")


    return created_booking

def extract_zd_user_id_from_service_notes(service_notes: str | None) -> int | None:
    if not service_notes:
        return None
    m = re.search(r"\[ZD_USER\]\s*(\d+)", service_notes)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None

####### 針灸 ###########

def _extract_staff_ids(appt: dict) -> set[str]:
    """
    Graph 的 appointment 可能回 staffMemberIds: ["id1","id2"] 或 staffMemberIds: [{"id":...}]（看版本/欄位）
    我們做一個保險的解析。
    """
    raw = appt.get("staffMemberIds")
    if not raw:
        return set()

    out = set()
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, str):
                out.add(x.strip())
            elif isinstance(x, dict) and x.get("id"):
                out.add(str(x["id"]).strip())
    return out


def get_available_acu_slots_for_date(date_str: str) -> list[str]:
    """
    針灸：回傳該日期可預約的時段（依固定表 + 依床位 staff 是否已被佔用）
    - 只查一次 BOOKING_BUSINESS_ACU_ID
    - 用 staffMemberIds 判斷是床1或床2
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    wd = d.weekday()

    day_map = ACU_SLOTS.get(wd) or {}
    if not day_map:
        app.logger.info(f"[acu_slots] date={date_str} wd={wd} -> no day_map (closed?)")
        return []

    acu_business_id = BOOKINGS_BUSINESS_ACU_ID
    app.logger.info(
        f"[acu_slots] date={date_str} wd={wd} acu_business={acu_business_id} bed1_staff={ACU_STAFF_BED1_ID} bed2_staff={ACU_STAFF_BED2_ID}"
    )

    appts = list_appointments_for_date(date_str, business_id=acu_business_id)

    booked_by_bed = {"bed1": set(), "bed2": set()}

    for appt in appts:
        # （可選）只看針灸 service，避免同 business 裡有別的服務影響
        sid = (appt.get("serviceId") or "").strip()
        if BOOKINGS_SERVICE_ACU_BED_ID and sid and sid != BOOKINGS_SERVICE_ACU_BED_ID:
            continue

        staff_ids = _extract_staff_ids(appt)

        s = (appt.get("startDateTime", {}) or {}).get("dateTime")
        if not s:
            continue

        try:
            if s.endswith("Z"):
                s = s[:-1]
            if "." in s:
                s = s.split(".", 1)[0]
            utc_dt = datetime.fromisoformat(s)
            local_dt = utc_dt + timedelta(hours=8)
        except Exception:
            continue

        if ACU_STAFF_BED1_ID in staff_ids:
            booked_by_bed["bed1"].add(hhmm)
        if ACU_STAFF_BED2_ID in staff_ids:
            booked_by_bed["bed2"].add(hhmm)

    available = []
    for hhmm, bed in day_map.items():
        if hhmm not in booked_by_bed.get(bed, set()):
            available.append(hhmm)

    available.sort()

    app.logger.info(f"[acu_slots] date={date_str} day_map_keys={list(day_map.keys())}")
    app.logger.info(f"[acu_slots] booked bed1={sorted(booked_by_bed['bed1'])}")
    app.logger.info(f"[acu_slots] booked bed2={sorted(booked_by_bed['bed2'])}")
    app.logger.info(f"[acu_slots] available={available}")

    return available


# def get_available_acu_slots_for_date(date_str: str) -> list[str]:
#     """
#     針灸：回傳該日期可預約的時段（依固定表 + 依床位 business 是否已被佔用）
#     - bed1 -> BOOKINGS_BUSINESS_ACU_BED1_ID (Bookings2@...)
#     - bed2 -> BOOKINGS_BUSINESS_ACU_BED2_ID (Bookings3@...)
#     """
#     d = datetime.strptime(date_str, "%Y-%m-%d").date()
#     wd = d.weekday()

#     day_map = ACU_SLOTS.get(wd) or {}   # {"08:50":"bed1", ...}
#     if not day_map:
#         app.logger.info(f"[acu_slots] date={date_str} wd={wd} -> no day_map (closed?)")
#         return []

#     # 兩床各自查一次（各自是獨立 business）
#     app.logger.info(
#         f"[acu_slots] date={date_str} wd={wd} "
#         f"bed1_business={BOOKINGS_BUSINESS_ACU_BED1_ID} bed2_business={BOOKINGS_BUSINESS_ACU_BED2_ID}"
#     )

#     appts_bed1 = list_appointments_for_date(date_str, business_id=BOOKINGS_BUSINESS_ACU_BED1_ID)
#     appts_bed2 = list_appointments_for_date(date_str, business_id=BOOKINGS_BUSINESS_ACU_BED2_ID)

#     booked_by_bed = {"bed1": set(), "bed2": set()}

#     def _add_booked(appts: list, bed_key: str):
#         for appt in appts:
#             s = (appt.get("startDateTime", {}) or {}).get("dateTime")
#             if not s:
#                 continue
#             try:
#                 ss = s[:-1] if s.endswith("Z") else s
#                 if "." in ss:
#                     ss = ss.split(".", 1)[0]
#                 utc_dt = datetime.fromisoformat(ss)
#                 local_dt = utc_dt + timedelta(hours=8)
#                 hhmm = local_dt.strftime("%H:%M")
#                 booked_by_bed[bed_key].add(hhmm)
#             except Exception as e:
#                 app.logger.warning(f"[acu_slots] parse_time_failed bed={bed_key} s={s} err={repr(e)}")
#                 continue

#     _add_booked(appts_bed1, "bed1")
#     _add_booked(appts_bed2, "bed2")

#     # === DEBUG LOG===
#     app.logger.info(f"[acu_slots] date={date_str} day_map_keys={sorted(list(day_map.keys()))}")
#     app.logger.info(f"[acu_slots] booked bed1={sorted(list(booked_by_bed['bed1']))}")
#     app.logger.info(f"[acu_slots] booked bed2={sorted(list(booked_by_bed['bed2']))}")

#     available = []
#     for hhmm, bed in day_map.items():
#         # bed 只會是 "bed1"/"bed2"
#         if hhmm not in booked_by_bed.get(bed, set()):
#             available.append(hhmm)

#     available.sort()
#     app.logger.info(f"[acu_slots] available={available}")

#     return available

# def get_available_acu_slots_for_date(date_str: str) -> list[str]:
#     """
#     針灸：回傳該日期可預約的時段（依固定表 + 依床位 serviceId 是否已被佔用）
#     """
#     d = datetime.strptime(date_str, "%Y-%m-%d").date()
#     wd = d.weekday()

#     day_map = ACU_SLOTS.get(wd) or {}   # {"08:50":"bed1", ...}
#     if not day_map:
#         return []

#     appts = list_appointments_for_date(date_str, business_id=business_id)

#     booked_by_bed = {"bed1": set(), "bed2": set()}

#     for appt in appts:
#         sid = (appt.get("serviceId") or "").strip()
#         if sid not in ACU_SERVICE_IDS:
#             continue

#         s = (appt.get("startDateTime", {}) or {}).get("dateTime")
#         if not s:
#             continue

#         try:
#             if s.endswith("Z"):
#                 s = s[:-1]
#             if "." in s:
#                 s = s.split(".", 1)[0]
#             utc_dt = datetime.fromisoformat(s)
#             local_dt = utc_dt + timedelta(hours=8)
#             hhmm = local_dt.strftime("%H:%M")
#         except Exception:
#             continue

#         if sid == BOOKINGS_SERVICE_ACU_BED1_ID:
#             booked_by_bed["bed1"].add(hhmm)
#         elif sid == BOOKINGS_SERVICE_ACU_BED2_ID:
#             booked_by_bed["bed2"].add(hhmm)

#     available = []
#     for hhmm, bed in day_map.items():
#         if hhmm not in booked_by_bed.get(bed, set()):
#             available.append(hhmm)

#     available.sort()
#     return available


def is_acu_slot_available(date_str: str, time_str: str) -> bool:
    """
    針灸：檢查該日期+時間是否仍可預約（固定表內 + 對應床位 staff 尚未被佔用）
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    wd = d.weekday()

    day_map = ACU_SLOTS.get(wd) or {}
    bed = day_map.get(time_str)
    if not bed:
        return False

    target_staff_id = None
    if bed == "bed1":
        target_staff_id = ACU_STAFF_BED1_ID
    elif bed == "bed2":
        target_staff_id = ACU_STAFF_BED2_ID
    else:
        return False

    appts = list_appointments_for_date(date_str, business_id=BOOKINGS_BUSINESS_ACU_ID)

    for appt in appts:
        sid = (appt.get("serviceId") or "").strip()
        if BOOKINGS_SERVICE_ACU_BED_ID and sid and sid != BOOKINGS_SERVICE_ACU_BED_ID:
            continue

        staff_ids = _extract_staff_ids(appt)
        if target_staff_id not in staff_ids:
            continue

        s = (appt.get("startDateTime", {}) or {}).get("dateTime")
        if not s:
            continue

        try:
            if s.endswith("Z"):
                s = s[:-1]
            if "." in s:
                s = s.split(".", 1)[0]
            utc_dt = datetime.fromisoformat(s)
            local_dt = utc_dt + timedelta(hours=8)
            hhmm = local_dt.strftime("%H:%M")
        except Exception:
            continue

        if hhmm == time_str:
            return False

    return True



# def is_acu_slot_available(date_str: str, time_str: str) -> bool:
#     """
#     針灸：檢查該日期+時間是否仍可預約（固定表內 + 對應床位尚未被佔用）
#     """
#     d = datetime.strptime(date_str, "%Y-%m-%d").date()
#     wd = d.weekday()

#     day_map = ACU_SLOTS.get(wd) or {}
#     bed = day_map.get(time_str)
#     if not bed:
#         return False  # 不是針灸可預約時間

#     # 依床位選 serviceId + businessId
#     if bed == "bed1":
#         target_service_id = BOOKINGS_SERVICE_ACU_BED1_ID
#         business_id = BOOKINGS_BUSINESS_ACU_BED1_ID
#     elif bed == "bed2":
#         target_service_id = BOOKINGS_SERVICE_ACU_BED2_ID
#         business_id = BOOKINGS_BUSINESS_ACU_BED2_ID
#     else:
#         return False

#     appts = list_appointments_for_date(date_str, business_id=business_id)

#     # 只檢查「該床位 service」在該時間有沒有被佔用
#     for appt in appts:
#         sid = (appt.get("serviceId") or "").strip()
#         if sid != target_service_id:
#             continue

#         s = (appt.get("startDateTime", {}) or {}).get("dateTime")
#         if not s:
#             continue

#         try:
#             if s.endswith("Z"):
#                 s = s[:-1]
#             if "." in s:
#                 s = s.split(".", 1)[0]
#             utc_dt = datetime.fromisoformat(s)
#             local_dt = utc_dt + timedelta(hours=8)
#             hhmm = local_dt.strftime("%H:%M")
#         except Exception:
#             continue

#         if hhmm == time_str:
#             return False

#     return True




