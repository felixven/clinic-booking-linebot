from datetime import datetime, timedelta
import os
import requests
import re
from flask import current_app as app  # 用 app.logger

from config import (
    BOOKING_BUSINESS_ID,
    BOOKING_DEMO_SERVICE_ID,
    BOOKING_DEMO_STAFF_ID,
    APPOINTMENT_DURATION_MINUTES,
    SLOT_INTERVAL_MINUTES,
    SLOT_START,             # 看診起始時間（第一個）
    SLOT_END
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
    

def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
    """
    將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 datetime」。
    例如:
        "2025-11-20T06:00:00Z"
        "2025-11-20T06:00:00.0000000Z"
    都會轉成：2025-11-20 14:00:00 (UTC+8)
    """
    if not start_dt_str:
        return None

    try:
        s = start_dt_str.strip()

        # 1) 去掉尾巴的 Z
        if s.endswith("Z"):
            s = s[:-1]

        # 2) 有小數秒就只留到秒
        if "." in s:
            s = s.split(".", 1)[0]

        # 3) 變成 datetime（目前視為 naive UTC）
        utc_dt = datetime.fromisoformat(s)

    except Exception as e:
        app.logger.error(
            f"[parse_booking_datetime_to_local] 解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}"
        )
        return None

    # 4) 加上 8 小時變成台北時間（之後真的上線要改成用 tz aware 再說）
    local_dt = utc_dt + timedelta(hours=8)
    return local_dt

# --- 輔助函式：取得指定日期所有預約 (實際呼叫 Graph API) ---
def list_appointments_for_date(date_str: str) -> list:
    """
    從 Bookings 取得指定日期 (台北時間, YYYY-MM-DD) 的所有預約列表。
    回傳: 預約列表 (list of dict)
    """
    token: str = get_graph_token()
    business_id: str = os.environ.get("BOOKING_BUSINESS_ID") or BOOKING_BUSINESS_ID

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID，請檢查環境變數。")

    # 1. 計算 UTC 範圍 (將台北時間 T+08:00 轉換為 UTC)
    try:
        # 台北時間 (UTC+8) 的 00:00:00
        local_start_dt: datetime = datetime.strptime(f"{date_str} 00:00:00", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        app.logger.error(f"日期格式錯誤，請使用 YYYY-MM-DD: {date_str}")
        return []

    local_end_dt: datetime = local_start_dt + timedelta(days=1)

    # 轉為 UTC 時間 (減 8 小時)
    utc_start_dt: datetime = local_start_dt - timedelta(hours=8)
    utc_end_dt: datetime = local_end_dt - timedelta(hours=8)

    # 格式化為 Graph API 要求的 ISO 格式
    start_time: str = utc_start_dt.isoformat() + "Z"
    end_time: str = utc_end_dt.isoformat() + "Z"

    # 2. 呼叫 calendarView API
    url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/calendarView"

    headers: dict = {
        "Authorization": f"Bearer {token}"
    }

    params: dict = {
        "start": start_time,
        "end": end_time
    }

    # 執行 API 呼叫
    resp = requests.get(url, headers=headers, params=params)
    app.logger.info(
        f"CALENDAR VIEW STATUS: {resp.status_code}, URL: {resp.url}")

    resp.raise_for_status()

    # calendarView 回傳的結果已經是該日期範圍內 (UTC+8) 的預約
    return resp.json().get("value", [])

def list_appointments_for_range(start_local: datetime, end_local: datetime):
    """
    一次從 Bookings 抓「某個時間範圍內」所有 appointments。

    傳入的 start_local / end_local 是「台北時間（naive）」，
    我們會轉成 UTC 後呼叫 Graph API：
    GET /solutions/bookingBusinesses/{business_id}/appointments?
        startDateTime=...&endDateTime=...

    回傳：list[dict]（appointments 清單）
    """
    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")
    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    # 先把台北時間（UTC+8）轉成 UTC 時間
    start_utc = start_local - timedelta(hours=8)
    end_utc = end_local - timedelta(hours=8)

    # 轉成 ISO 格式，補上 Z
    start_iso = start_utc.replace(microsecond=0).isoformat() + "Z"
    end_iso = end_utc.replace(microsecond=0).isoformat() + "Z"

    url = (
        f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
        f"?startDateTime={start_iso}&endDateTime={end_iso}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"LIST APPTS RANGE STATUS: {resp.status_code}, BODY: {resp.text[:500]}"
    )
    resp.raise_for_status()

    data = resp.json()
    # 通常 Graph 會把結果放在 value 裡
    return data.get("value", [])

def get_appointment_by_id(appt_id: str):
    """
    用 Bookings appointment id 取得單一預約資訊。
    回傳 (appointment_dict, local_start_dt)；
    找不到或解析失敗則回 (None, None)。
    """
    if not appt_id:
        return None, None

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"GET APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

    if resp.status_code == 404:
        # 已被刪除或不存在
        return None, None

    resp.raise_for_status()
    appt = resp.json()

    app.logger.info(f"APPOINTMENT KEYS: {list(appt.keys())}")
    app.logger.info(
        f"APPT NOTES FIELDS: serviceNotes={appt.get('serviceNotes')}, "
        f"customerNotes={appt.get('customerNotes')}"
    )

    start_info = appt.get("startDateTime", {})
    local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
    if not local_dt:
        return None, None

    return appt, local_dt



def cancel_booking_appointment(appt_id: str):
    """
    DEMO 版：直接呼叫 DELETE 取消 Bookings appointment。
    （正式版如果要改成「標記取消」也可以，改這裡就好。）
    """
    if not appt_id:
        raise Exception("cancel_booking_appointment: appt_id 為空")

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.delete(url, headers=headers)
    app.logger.info(
        f"DELETE APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

    # 204 No Content / 200 / 202
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()

def update_booking_service_notes(appt_id: str, notes_text: str):
    """
    將指定 appointment 的 serviceNotes 更新為 notes_text。(診所／工作人員可以看的備註)
    """
    if not appt_id:
        raise Exception("update_booking_service_notes: appt_id 為空")

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "serviceNotes": notes_text
    }

    resp = requests.patch(url, headers=headers, json=payload)
    app.logger.info(
        f"PATCH APPT SERVICE NOTES {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")
    resp.raise_for_status()

def get_available_slots_for_date(date_str: str) -> list:
    """
    回傳指定日期「可預約」的時段列表，例如：
    ["09:00", "09:30", "10:00", ...]
    規則：SLOT_START–SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘，排除當天已被預約的開始時段。
    """
    appts: list = list_appointments_for_date(date_str)

    booked_times: set = set()
    for appt in appts:
        start_info: dict = appt.get("startDateTime", {})
        # "2025-11-20T06:00:00.0000000Z"
        start_dt_str: str = start_info.get("dateTime")
        if not start_dt_str:
            continue

        try:
            s: str = start_dt_str
            if s.endswith("Z"):
                s = s[:-1]
            s = s.split(".")[0]
            utc_dt: datetime = datetime.fromisoformat(s)
        except Exception as e:
            app.logger.error(
                f"解讀 startDateTime 失敗（get_available_slots）：{start_dt_str}, error: {e}")
            continue

        local_dt: datetime = utc_dt + timedelta(hours=8)
        hhmm: str = local_dt.strftime("%H:%M")  # 例如 "14:00"
        booked_times.add(hhmm)

    # SLOT_START ~ SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘一格
    # 這裡假設日期是今天，只取時間部分
    start_dt_only: datetime = datetime.strptime(SLOT_START, "%H:%M").replace(year=2000, month=1, day=1)
    end_dt_only: datetime = datetime.strptime(SLOT_END, "%H:%M").replace(year=2000, month=1, day=1)


    slots: list = []
    cur: datetime = start_dt_only
    while cur <= end_dt_only:
        hhmm: str = cur.strftime("%H:%M")
        if hhmm not in booked_times:
            slots.append(hhmm)
        cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots

def create_booking_appointment(
    date_str: str,
    time_str: str,
    customer_name: str,
    customer_phone: str,
    zendesk_customer_id: str, # <--- 修正為 str
    line_display_name: str = None,
    line_user_id: str = None,
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
    business_id: str = os.environ.get("BOOKING_BUSINESS_ID") or BOOKING_BUSINESS_ID 

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    # --- 1. 準備 Bookings Payload (邏輯與您的原始碼一致) ---
    local_str: str = f"{date_str} {time_str}:00"
    local_dt: datetime = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S") # 預約的台北時間 (UTC+8)

    # Bookings API 使用 UTC 台北時間 - 8 小時
    utc_dt: datetime = local_dt - timedelta(hours=8)
    utc_iso: str = utc_dt.isoformat() + "Z"

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

    payload: dict = {
        "customerName": booking_customer_name,
        "customerEmailAddress": None,
        "customerPhone": customer_phone,
        "serviceId": BOOKING_DEMO_SERVICE_ID,
        "serviceName": "一般門診",
        "startDateTime": { "dateTime": utc_iso, "timeZone": "UTC" },
        "endDateTime": {
            "dateTime": (utc_dt + timedelta(minutes=duration)).isoformat() + "Z",
            "timeZone": "UTC",
        },
        "priceType": "free",
        "price": 0.0,
        "smsNotificationsEnabled": False,
        "staffMemberIds": [BOOKING_DEMO_STAFF_ID],
        "maximumAttendeesCount": 1,
        "filledAttendeesCount": 1,
    }

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


