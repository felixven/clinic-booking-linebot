import re
from datetime import datetime, timedelta

from zendesk_core import search_zendesk_users_by_phone, get_future_booking_count_by_user_id
from flows_slots import validate_appointment_date
from bookings_core import get_available_slots_for_date, create_booking_appointment, list_appointments_for_date, _get_day_intervals
from state_store import acquire_lock

from config import(
    BOOKINGS_BUSINESS_CLINIC_ID,
    BOOKINGS_SERVICE_CLINIC_ID,
    WEEKDAY_ZH,
)

def normalize_phone(phone_raw: str) -> str:
    """
    只保留數字，並整理成台灣手機格式。
    支援：
    - 09xxxxxxxx
    - 8869xxxxxxxx -> 09xxxxxxxx
    """
    digits = re.sub(r"\D", "", phone_raw or "")

    if digits.startswith("886") and len(digits) == 12:
        digits = "0" + digits[3:]

    return digits

def is_hhmm_in_interval(hhmm: str, start_hhmm: str, end_hhmm: str) -> bool:
    return start_hhmm <= hhmm <= end_hhmm


def resolve_clinic_period_by_time(date_str: str, hhmm: str) -> str | None:
    """
    根據 _get_day_intervals(date_str) 判斷某個 HH:MM 落在哪個門診時段。
    規則：
    - 第一段 interval -> morning
    - 第二段 interval -> evening
    """
    intervals = _get_day_intervals(date_str)
    if not intervals:
        return None

    if len(intervals) >= 1:
        start1, end1 = intervals[0]
        if is_hhmm_in_interval(hhmm, start1, end1):
            return "morning"

    if len(intervals) >= 2:
        start2, end2 = intervals[1]
        if is_hhmm_in_interval(hhmm, start2, end2):
            return "evening"

    return None


def is_placeholder_name(name: str) -> bool:
    if not name:
        return True

    s = name.strip()
    placeholders = {
        "未填姓名",
        "未提供姓名",
        "貴賓",
        "訪客",
        "guest",
        "test",
        "unknown",
    }
    return s.lower() in {p.lower() for p in placeholders}


def identify_patient_by_phone(phone_raw: str, flow: str = "clinic_booking") -> dict:
    """
    第一版 voice 規則：
    - 只支援既有客戶
    - 查不到 Zendesk user => 不可繼續
    - 查到但姓名不完整/placeholder => 不可繼續
    """
    phone = normalize_phone(phone_raw)

    def _empty_raw_user() -> dict:
        return {
            "active": False,
            "alias": "",
            "created_at": "",
            "custom_role_id": 0,
            "default_group_id": 0,
            "details": "",
            "email": None,
            "external_id": "",
            "iana_time_zone": "",
            "id": 0,
            "is_billing_admin": None,
            "last_login_at": None,
            "locale": "",
            "locale_id": 0,
            "moderator": False,
            "name": "",
            "notes": "",
            "only_private_comments": False,
            "organization_id": 0,
            "phone": phone,
            "photo": None,
            "report_csv": False,
            "restricted_agent": False,
            "role": "",
            "role_type": None,
            "shared": False,
            "shared_agent": False,
            "shared_phone_number": False,
            "signature": None,
            "suspended": False,
            "tags": [],
            "ticket_restriction": "",
            "time_zone": "",
            "two_factor_auth_enabled": None,
            "updated_at": "",
            "url": "",
            "user_fields": {
                "clinic_acu_doctor_approved": False,
                "clinic_acu_internal_patient": False,
                "clinic_acu_seen_within_3_months": False,
                "future_booking_count": 0,
                "line_name": "",
                "line_user_id": "",
                "profile_status": "",
                "record_no": None,
                "state": None,
            },
            "verified": False,
        }

    def _base_response() -> dict:
        return {
            "success": False,
            "patient_found": False,
            "can_continue": False,
            "reason": "",
            "message": "",
            "phone": phone,
            "zendesk_user_id": "",
            "patient_name": "",
            "line_name": "",
            "line_user_id": "",
            "profile_status": "",
            "future_booking_count": 0,
            "raw_user": _empty_raw_user(),
        }

    if not phone:
        result = _base_response()
        result.update(
            {
                "reason": "invalid_phone",
                "message": "手機格式不正確",
            }
        )
        return result

    try:
        users = search_zendesk_users_by_phone(phone) or []
    except Exception as e:
        print(f"[VOICE_IDENTIFY] search_zendesk_users_by_phone failed phone={phone} err={repr(e)}")
        result = _base_response()
        result.update(
            {
                "reason": "zendesk_search_failed",
                "message": "查詢客戶資料失敗",
            }
        )
        return result

    if not users:
        result = _base_response()
        result.update(
            {
                "success": True,
                "reason": "patient_not_found",
                "message": "查無既有客戶資料",
            }
        )
        return result

    # 第一版先取第一筆
    user = users[0]
    user_id = user.get("id")
    user_name = (user.get("name") or "").strip()
    user_phone = normalize_phone(user.get("phone") or phone)
    raw_user = _empty_raw_user()
    raw_user.update(user)
    raw_user_fields = dict(raw_user.get("user_fields") or {})
    raw_user_fields.setdefault("clinic_acu_doctor_approved", False)
    raw_user_fields.setdefault("clinic_acu_internal_patient", False)
    raw_user_fields.setdefault("clinic_acu_seen_within_3_months", False)
    raw_user_fields.setdefault("future_booking_count", 0)
    raw_user_fields.setdefault("line_name", "")
    raw_user_fields.setdefault("line_user_id", "")
    raw_user_fields.setdefault("profile_status", "")
    raw_user_fields.setdefault("record_no", None)
    raw_user_fields.setdefault("state", None)
    raw_user["user_fields"] = raw_user_fields
    line_name = str(raw_user_fields.get("line_name") or "")
    line_user_id = str(raw_user_fields.get("line_user_id") or "")
    profile_status = str(raw_user_fields.get("profile_status") or "")
    future_booking_count = raw_user_fields.get("future_booking_count") or 0
    try:
        future_booking_count = int(future_booking_count)
    except Exception:
        future_booking_count = 0

    if is_placeholder_name(user_name):
        result = _base_response()
        result.update(
            {
                "success": True,
                "patient_found": True,
                "reason": "invalid_name",
                "message": "客戶姓名資料不完整",
                "phone": user_phone,
                "zendesk_user_id": str(user_id) if user_id else "",
                "patient_name": user_name,
                "line_name": line_name,
                "line_user_id": line_user_id,
                "profile_status": profile_status,
                "future_booking_count": future_booking_count,
                "raw_user": raw_user,
            }
        )
        return result

    result = _base_response()
    result.update(
        {
            "success": True,
            "patient_found": True,
            "can_continue": True,
            "reason": "",
            "message": "ok",
            "phone": user_phone,
            "zendesk_user_id": str(user_id) if user_id else "",
            "patient_name": user_name,
            "line_name": line_name,
            "line_user_id": line_user_id,
            "profile_status": profile_status,
            "future_booking_count": future_booking_count,
            "raw_user": raw_user,
        }
    )
    return result

def _build_candidate_dates_for_week(week_offset: int = 0) -> list:
    """
    仿照你 show_dates_for_week 的週邏輯，但只回 date list，不碰 LINE event。
    """
    today = datetime.now()
    weekday = today.weekday()  # 0=週一
    monday = today - timedelta(days=weekday)

    week_start = monday + timedelta(days=week_offset * 7)
    week_end = week_start + timedelta(days=5)  # 週一～週六

    if week_offset == 0:
        start_date = today + timedelta(days=1)  # 明天開始
        if start_date.date() < week_start.date():
            start_date = week_start
    else:
        start_date = week_start

    candidate_dates = []
    cur = start_date
    while cur.date() <= week_end.date():
        candidate_dates.append(cur.date())
        cur += timedelta(days=1)

    return candidate_dates

from datetime import datetime, timedelta

def _build_candidate_dates_forward(days_ahead: int = 21) -> list:
    today = datetime.now().date()
    start_date = today + timedelta(days=1)

    candidate_dates = []
    for i in range(days_ahead):
        candidate_dates.append(start_date + timedelta(days=i))

    return candidate_dates


def get_clinic_date_options(page: int = 0, page_size: int = 3) -> list[dict]:
    """
    page=0 -> 最近 3 個可約日期
    page=1 -> 再往後 3 個可約日期
    """
    candidate_dates = _build_candidate_dates_forward(days_ahead=21)

    available_dates = []
    target_count = (page + 1) * page_size  # page0=3, page1=6

    for d in candidate_dates:
        date_str = d.isoformat()

        ok, reason = validate_appointment_date(date_str)
        if not ok:
            print(f"[VOICE_DATE_OPTIONS] skip date={date_str} reason=validate_failed detail={reason}")
            continue

        period_options = get_clinic_period_options(date_str)
        if not period_options:
            print(f"[VOICE_DATE_OPTIONS] skip date={date_str} reason=no_period_options")
            continue

        available_dates.append(d)

        # 找到這一頁需要的總數就停，不用把後面全部掃完
        if len(available_dates) >= target_count:
            break

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_dates = available_dates[start_idx:end_idx]

    options = []
    for idx, d in enumerate(page_dates, start=1):
        weekday_label = WEEKDAY_ZH[d.weekday()]
        label = f"{d.month}月{d.day}日 星期{weekday_label}"

        options.append({
            "key": str(idx),
            "label": label,
            "value": d.isoformat(),
        })

    return options

# def get_clinic_date_options(week_offset: int = 0, max_options: int = 3) -> list[dict]:
#     """
#     回傳門診可預約日期 options。
#     注意：門診只問日期，真正早診/晚診另外問。
#     """
#     candidate_dates = _build_candidate_dates_for_week(week_offset=week_offset)
#     options = []

#     for d in candidate_dates:
#         date_str = d.isoformat()

#         ok, reason = validate_appointment_date(date_str)
#         if not ok:
#             print(f"[VOICE_DATE_OPTIONS] skip date={date_str} reason=validate_failed detail={reason}")
#             continue

#         # 這裡用 get_clinic_period_options 來判斷當天是否至少還有早/晚其中一個可約
#         period_options = get_clinic_period_options(date_str)
#         if not period_options:
#             print(f"[VOICE_DATE_OPTIONS] skip date={date_str} reason=no_period_options")
#             continue

#         weekday_label = WEEKDAY_ZH[d.weekday()]
#         label = f"{d.month}月{d.day}日 星期{weekday_label}"

#         options.append({
#             "key": str(len(options) + 1),
#             "label": label,
#             "value": date_str,
#         })

#         if len(options) >= max_options:
#             break

#     return options
def pick_first_available_clinic_time(date_str: str, period: str, business_id: str) -> str | None:
    """
    根據 _get_day_intervals(date_str) 取得指定門診時段的第一個可預約 HH:MM。
    - 第一段 interval -> morning
    - 第二段 interval -> evening
    """
    slots = sorted(get_available_slots_for_date(date_str, business_id=business_id) or [])
    if not slots:
        return None

    intervals = _get_day_intervals(date_str)
    if not intervals:
        return None

    target_interval = None

    if period == "morning":
        if len(intervals) >= 1:
            target_interval = intervals[0]

    elif period == "evening":
        if len(intervals) >= 2:
            target_interval = intervals[1]

    if not target_interval:
        return None

    start_hhmm, end_hhmm = target_interval

    candidate_slots = [
        s for s in slots
        if is_hhmm_in_interval(s, start_hhmm, end_hhmm)
    ]

    return candidate_slots[0] if candidate_slots else None

# def pick_first_available_clinic_time(date_str: str, period: str, business_id: str) -> str | None:
#     """
#     依門診早/晚，從「虛擬 20 分鐘 slots」中挑第一個可用 slot。
#     使用者不會看到這個時間，只給 Bookings 用。

#     目前先用時間區間切：
#     - morning: 00:00 ~ 11:59
#     - evening: 12:00 ~ 23:59

#     這裡請你之後依實際門診時段再微調成更精準的範圍。
#     """
#     all_slots = get_available_slots_for_date(
#         date_str=date_str,
#         business_id=business_id,
#     ) or []

#     if period == "morning":
#         candidate_slots = [s for s in all_slots if s < "12:00"]
#     elif period == "evening":
#         candidate_slots = [s for s in all_slots if s >= "12:00"]
#     else:
#         candidate_slots = []

#     return candidate_slots[0] if candidate_slots else None

def get_clinic_period_options(date_str: str) -> list[dict]:
    """
    回傳某日期可預約的門診時段：
    - 第一段 interval -> 早診 (morning)
    - 第二段 interval -> 晚診 (evening)
    完全對齊 _get_day_intervals(date_str)
    """
    all_slots = sorted(
        get_available_slots_for_date(
            date_str=date_str,
            business_id=BOOKINGS_BUSINESS_CLINIC_ID,
        ) or []
    )

    intervals = _get_day_intervals(date_str)
    if not intervals or not all_slots:
        return []

    options = []

    # 第一段 -> morning
    if len(intervals) >= 1:
        start1, end1 = intervals[0]
        first_interval_slots = [
            s for s in all_slots
            if is_hhmm_in_interval(s, start1, end1)
        ]
        if first_interval_slots:
            options.append({
                "key": str(len(options) + 1),
                "label": "早診",
                "value": "morning",
            })

    # 第二段 -> evening
    if len(intervals) >= 2:
        start2, end2 = intervals[1]
        second_interval_slots = [
            s for s in all_slots
            if is_hhmm_in_interval(s, start2, end2)
        ]
        if second_interval_slots:
            options.append({
                "key": str(len(options) + 1),
                "label": "晚診",
                "value": "evening",
            })

    return options

# def get_clinic_period_options(date_str: str) -> list[dict]:
#     """
#     回傳某日期可預約的門診時段：
#     - 第一段 interval -> 早診 (morning)
#     - 第二段 interval -> 晚診 (evening)
#     完全對齊 _get_day_intervals(date_str)
#     """
#     all_slots = sorted(
#         get_available_slots_for_date(
#             date_str=date_str,
#             business_id=BOOKINGS_BUSINESS_CLINIC_ID,
#         ) or []
#     )

#     intervals = _get_day_intervals(date_str)
#     if not intervals or not all_slots:
#         return []

#     options = []

#     # 第一段 -> morning
#     if len(intervals) >= 1:
#         start1, end1 = intervals[0]
#         first_interval_slots = [
#             s for s in all_slots
#             if is_hhmm_in_interval(s, start1, end1)
#         ]
#         if first_interval_slots:
#             options.append({
#                 "key": str(len(options) + 1),
#                 "label": "早診",
#                 "value": "morning",
#             })

#     # 第二段 -> evening
#     if len(intervals) >= 2:
#         start2, end2 = intervals[1]
#         second_interval_slots = [
#             s for s in all_slots
#             if is_hhmm_in_interval(s, start2, end2)
#         ]
#         if second_interval_slots:
#             options.append({
#                 "key": str(len(options) + 1),
#                 "label": "晚診",
#                 "value": "evening",
#             })

#     return options

# def get_clinic_period_options(date_str: str) -> list[dict]:
#     """
#     回傳某日期可預約的門診時段：
#     - 早診
#     - 晚診
#     """
#     options = []

#     morning_time = pick_first_available_clinic_time(
#         date_str=date_str,
#         period="morning",
#         business_id=BOOKINGS_BUSINESS_CLINIC_ID,
#     )
#     if morning_time:
#         options.append({
#             "key": "1",
#             "label": "早診",
#             "value": "morning",
#         })

#     evening_time = pick_first_available_clinic_time(
#         date_str=date_str,
#         period="evening",
#         business_id=BOOKINGS_BUSINESS_CLINIC_ID,
#     )
#     if evening_time:
#         options.append({
#             "key": str(len(options) + 1),
#             "label": "晚診",
#             "value": "evening",
#         })

#     return options


def has_existing_clinic_period_booking_by_phone(phone: str, date_str: str, period: str) -> bool:
    """
    檢查同一支手機在同一天是否已經有相同門診時段（早診/晚診）的預約。
    第一版直接查 Bookings calendarView，不碰 Zendesk ticket。
    """
    normalized_phone = normalize_phone(phone)

    try:
        appts = list_appointments_for_date(
            date_str=date_str,
            business_id=BOOKINGS_BUSINESS_CLINIC_ID,
        ) or []
    except Exception as e:
        print(
            f"[VOICE_DUP] list_appointments_for_date failed "
            f"phone={normalized_phone} date={date_str} period={period} err={repr(e)}",
            flush=True
        )
        return False

    print(
        f"[VOICE_DUP] start phone={normalized_phone} date={date_str} period={period} appts_count={len(appts)}",
        flush=True
    )

    for appt in appts:
        appt_phone = normalize_phone(appt.get("customerPhone") or "")
        if appt_phone != normalized_phone:
            continue

        start_info = appt.get("startDateTime", {}) or {}
        s = start_info.get("dateTime")
        if not s:
            continue

        try:
            # Graph 回 UTC，例如 2026-03-19T01:00:00.0000000Z
            if s.endswith("Z"):
                s = s[:-1]
            if "." in s:
                s = s.split(".", 1)[0]

            utc_dt = datetime.fromisoformat(s)
            local_dt = utc_dt + timedelta(hours=8)
            hhmm = local_dt.strftime("%H:%M")
        except Exception as e:
            print(
                f"[VOICE_DUP] parse startDateTime failed "
                f"appt_id={appt.get('id')} raw={start_info} err={repr(e)}",
                flush=True
            )
            continue

        appt_period = resolve_clinic_period_by_time(date_str, hhmm)

        print(
            f"[VOICE_DUP] inspect appt_id={appt.get('id')} appt_phone={appt_phone} "
            f"hhmm={hhmm} appt_period={appt_period}",
            flush=True
        )

        if appt_period == period:
            print(
                f"[VOICE_DUP] duplicate found phone={normalized_phone} "
                f"date={date_str} period={period} appt_id={appt.get('id')} hhmm={hhmm}",
                flush=True
            )
            return True

    print(
        f"[VOICE_DUP] no duplicate phone={normalized_phone} date={date_str} period={period}",
        flush=True
    )
    return False


def create_clinic_booking(phone: str, date_str: str, period: str) -> dict:
    """
    voice 門診預約第一版：
    - 只支援既有客戶
    - 使用者只選日期 + 早/晚診
    - 真正 HH:MM 由 backend 自己挑
    - Zendesk ticket 先不當成功必要條件
    """
    identify_result = identify_patient_by_phone(phone, flow="clinic_booking")
    if not identify_result.get("success") or not identify_result.get("can_continue"):
        return {
            "success": False,
            "reason": identify_result.get("reason"),
            "message": identify_result.get("message"),
        }

    normalized_phone = identify_result["phone"]
    zendesk_user_id = identify_result["zendesk_user_id"]
    patient_name = identify_result["patient_name"]
    future_booking_count = get_future_booking_count_by_user_id(zendesk_user_id)

    if future_booking_count >= 2:
        return {
            "success": False,
            "reason": "future_booking_limit_reached",
            "message": "您目前已有 2 筆未來預約，如需重新安排，請先取消既有預約後再試",
        }

    ok, msg = validate_appointment_date(date_str)
    if not ok:
        return {
            "success": False,
            "reason": "invalid_date",
            "message": msg,
        }

    if period not in {"morning", "evening"}:
        return {
            "success": False,
            "reason": "invalid_period",
            "message": "無效的門診時段",
        }

    if has_existing_clinic_period_booking_by_phone(normalized_phone, date_str, period):
        return {
            "success": False,
            "reason": "duplicate_booking",
            "message": "您已預約此日期與門診時段",
        }

    # 鎖用 phone + date + period 即可
    lock_key = f"voice:clinic:{normalized_phone}:{date_str}:{period}"
    lock_ok = acquire_lock(lock_key, ttl_sec=30)
    print(
        f"[VOICE_CREATE_BOOKING] lock phone={normalized_phone} date={date_str} period={period} ok={lock_ok}"
    )

    if not lock_ok:
        return {
            "success": False,
            "reason": "duplicate_request",
            "message": "正在建立預約中，請勿重複操作",
        }

    # 最後一刻再挑一次最新 slot，避免中間被搶位
    time_str = pick_first_available_clinic_time(
        date_str=date_str,
        period=period,
        business_id=BOOKINGS_BUSINESS_CLINIC_ID,
    )
    if not time_str:
        return {
            "success": False,
            "reason": "clinic_period_full",
            "message": "此門診時段已滿，請改選其他日期或時段",
        }

    try:
        created = create_booking_appointment(
            date_str=date_str,
            time_str=time_str,
            customer_name=patient_name,
            customer_phone=normalized_phone,
            zendesk_customer_id=zendesk_user_id,
            line_display_name=None,
            line_user_id=None,
            business_id=BOOKINGS_BUSINESS_CLINIC_ID,
            service_id=BOOKINGS_SERVICE_CLINIC_ID,
            staff_member_ids=None,
        )
    except Exception as e:
        print(
            f"[VOICE_CREATE_BOOKING] create_booking_appointment failed "
            f"phone={normalized_phone} date={date_str} period={period} actual_time={time_str} err={repr(e)}"
        )
        return {
            "success": False,
            "reason": "create_booking_failed",
            "message": "預約建立失敗，請稍後再試",
        }

    if not isinstance(created, dict) or not created.get("id"):
        print(
            f"[VOICE_CREATE_BOOKING] invalid booking response phone={normalized_phone} "
            f"date={date_str} period={period} actual_time={time_str} created={created}"
        )
        return {
            "success": False,
            "reason": "create_booking_failed",
            "message": "預約建立失敗，請稍後再試",
        }

    period_label = "早診" if period == "morning" else "晚診"

    return {
        "success": True,
        "reason": "",
        "message": "booking_created",
        "patient_name": patient_name,
        "phone": normalized_phone,
        "date": date_str,
        "display_date": date_str.replace("-", "/"),
        "period": period,
        "period_label": period_label,
        "actual_time": time_str,  # 給 log / debug 用，voice 話術不要念
        "booking_id": created.get("id"),
        "booking": created,
    }
