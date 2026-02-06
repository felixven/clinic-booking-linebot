from datetime import datetime, timedelta

from flask import current_app as app

from zendesk_core import search_zendesk_user_by_line_id
from bookings_core import (
list_appointments_for_range,
parse_booking_datetime_to_local
)

from config import (
    PROFILE_STATUS_COMPLETE,
    BOOKINGS_BUSINESS_CLINIC_ID,
    BOOKINGS_BUSINESS_ACU_ID,
    is_valid_name
    )


def normalize_phone(phone: str) -> str:
    """
    將電話號碼轉成統一格式，用來比對：
    - 只留數字
    - 把 886 開頭的改成 0 開頭（例如 +8869xxxx → 09xxxx）
    """
    if not phone:
        return ""

    # 只留數字
    digits = "".join(ch for ch in phone if ch.isdigit())

    # 處理台灣號碼：+8869xxx or 8869xxx → 09xxx
    if digits.startswith("8869"):
        digits = "0" + digits[3:]  # 8869xxxxxxxx → 09xxxxxxxx

    return digits

def get_future_appointments_for_line_user(line_user_id: str, max_days: int = 30) -> list[tuple[datetime, dict]]:
    matched: list[tuple[datetime, dict]] = []

    # ① 先從 Zendesk 找 user，拿 phone 當備援 key
    try:
        count, zd_user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[get_future_for_line] 用 line_user_id 查 Zendesk user 失敗: {e}")
        return []

    if not zd_user:
        app.logger.info(f"[get_future_for_line] line_user_id={line_user_id} 在 Zendesk 中查無使用者")
        return []

    target_phone = normalize_phone(zd_user.get("phone") or "") or ""
    if not target_phone:
        app.logger.info(f"[get_future_for_line] Zendesk user 沒有 phone，之後僅用 [LINE_USER] 比對")

    # ② 查詢範圍：現在 ~ 未來 max_days 天
    now_local = datetime.now()
    end_local = now_local + timedelta(days=max_days)
    app.logger.info(f"[get_future_for_line] 查詢範圍：{now_local} ~ {end_local}, line_user_id={line_user_id}")

    # ✅ ③ 一次查多個 business（門診 + 針灸）
    business_ids = []
    if BOOKINGS_BUSINESS_CLINIC_ID:
        business_ids.append(("clinic", BOOKINGS_BUSINESS_CLINIC_ID))
    if BOOKINGS_BUSINESS_ACU_ID:
        business_ids.append(("acupuncture", BOOKINGS_BUSINESS_ACU_ID))

    all_appts: list[dict] = []
    for btype, bid in business_ids:
        try:
            appts = list_appointments_for_range(now_local, end_local, business_id=bid)
            # 把來源標記進去，避免 UI/後續判斷混在一起
            for a in appts:
                a["_booking_type"] = btype
                a["_business_id"] = bid
            all_appts.extend(appts)
        except Exception as e:
            app.logger.error(f"[get_future_for_line] list_appointments_for_range failed type={btype} bid={bid} err={e}")
            # 不要整個壞掉，繼續查下一個 business
            continue

    app.logger.info(f"[get_future_for_line] 範圍內共取得 {len(all_appts)} 筆 appointments（合併後）")

    for appt in all_appts:
        appt_phone = normalize_phone(appt.get("customerPhone") or "")
        service_notes = appt.get("serviceNotes") or ""

        matched_by_phone = (target_phone and appt_phone and appt_phone == target_phone)
        matched_by_line_id = (line_user_id and "[LINE_USER]" in service_notes and line_user_id in service_notes)

        if not (matched_by_phone or matched_by_line_id):
            continue

        start_info = appt.get("startDateTime") or {}
        start_str = start_info.get("dateTime")
        if not start_str:
            continue

        local_start = parse_booking_datetime_to_local(start_str)
        if not local_start:
            app.logger.warning(f"[get_future_for_line] 無法解析 startDateTime: {start_str}")
            continue

        if local_start < now_local:
            continue

        matched.append((local_start, appt))

    matched.sort(key=lambda x: x[0])
    app.logger.info(f"[get_future_for_line] 共 {len(matched)} 筆屬於該 LINE 使用者的 future 預約")
    return matched


# def get_future_appointments_for_line_user(line_user_id: str, max_days: int = 30) -> list[tuple[datetime, dict]]:
#     """
#     取得指定 LINE 使用者從「現在起 ~ 未來 max_days 天內」的所有預約（已排序）。

#     回傳：
#         [(local_start_dt, appt_dict), ...]
#     若找不到 / 發生錯誤，回傳 []。
#     """

#     matched: list[tuple[datetime, dict]] = []

#     # ① 先從 Zendesk 找 user，拿 phone 當備援 key
#     try:
#         count, zd_user = search_zendesk_user_by_line_id(line_user_id)
#     except Exception as e:
#         app.logger.error(f"[get_future_for_line] 用 line_user_id 查 Zendesk user 失敗: {e}")
#         return []

#     if not zd_user:
#         app.logger.info(f"[get_future_for_line] line_user_id={line_user_id} 在 Zendesk 中查無使用者")
#         return []

#     raw_phone = zd_user.get("phone") or ""
#     target_phone = normalize_phone(raw_phone)
#     if not target_phone:
#         app.logger.info(f"[get_future_for_line] Zendesk user 沒有 phone，之後僅用 [LINE_USER] 比對")
#         target_phone = ""

#     # ② 準備查詢範圍：現在 ~ 未來 max_days 天（台北時間，naive）
#     now_local = datetime.now()
#     end_local = now_local + timedelta(days=max_days)

#     app.logger.info(
#         f"[get_future_for_line] 查詢範圍：{now_local} ~ {end_local}, line_user_id={line_user_id}"
#     )

#     # ✅ FIX：多 business 查詢（門診 + 針灸），合併 appointments
#     biz_ids = []
#     if BOOKINGS_BUSINESS_CLINIC_ID:
#         biz_ids.append(BOOKINGS_BUSINESS_CLINIC_ID)
#     if BOOKINGS_BUSINESS_ACU_ID:
#         biz_ids.append(BOOKINGS_BUSINESS_ACU_ID)

#     if not biz_ids:
#         app.logger.error("[get_future_for_line] 缺 Bookings business id（BOOKINGS_BUSINESS_CLINIC_ID / BOOKINGS_BUSINESS_ACU_ID）")
#         return []

#     appts: list[dict] = []
#     seen_ids: set[str] = set()

#     try:
#         for biz in biz_ids:
#             sub = list_appointments_for_range(now_local, end_local, business_id=biz)  # ✅ 你缺的就是這個
#             app.logger.info(f"[get_future_for_line] business_id={biz} 取得 {len(sub)} 筆 appointments")
#             for a in sub:
#                 aid = (a.get("id") or "").strip()
#                 if aid and aid in seen_ids:
#                     continue
#                 if aid:
#                     seen_ids.add(aid)
#                 appts.append(a)
#     except Exception as e:
#         app.logger.error(f"[get_future_for_line] list_appointments_for_range 失敗: {e}")
#         return []

#     app.logger.info(f"[get_future_for_line] 範圍內合併後共取得 {len(appts)} 筆 appointments")

#     for appt in appts:
#         appt_phone = normalize_phone(appt.get("customerPhone") or "")
#         service_notes = appt.get("serviceNotes") or ""

#         # ③ 比對條件：
#         matched_by_phone = (target_phone and appt_phone and appt_phone == target_phone)
#         matched_by_line_id = (
#             line_user_id
#             and "[LINE_USER]" in service_notes
#             and line_user_id in service_notes
#         )

#         if not (matched_by_phone or matched_by_line_id):
#             continue

#         # ④ 解析 startDateTime → 使用共用 helper 轉成台北時間
#         start_info = appt.get("startDateTime") or {}
#         start_str = start_info.get("dateTime")
#         if not start_str:
#             continue

#         local_start = parse_booking_datetime_to_local(start_str)
#         if not local_start:
#             app.logger.warning(f"[get_future_for_line] 無法解析 startDateTime: {start_str}")
#             continue

#         if local_start < now_local:
#             continue

#         matched.append((local_start, appt))

#     if not matched:
#         app.logger.info("[get_future_for_line] 找不到符合條件的預約")
#         return []

#     matched.sort(key=lambda x: x[0])
#     app.logger.info(f"[get_future_for_line] 共 {len(matched)} 筆屬於該 LINE 使用者的 future 預約")
#     return matched


def get_next_upcoming_appointment_for_line_user(line_user_id: str, max_days: int = 30):
    """
    依照 LINE userId 找「未來最近一筆」屬於他的預約。

    ✅ 現在內部改成呼叫 get_future_appointments_for_line_user，
      但對外行為不變：回傳 (appt, local_start) 或 (None, None)
    """
    matched = get_future_appointments_for_line_user(line_user_id, max_days=max_days)

    if not matched:
        return None, None

    local_start, appt = matched[0]
    app.logger.info(
        f"[get_next_for_line_range] 找到預約 id={appt.get('id')} local_start={local_start}"
    )
    return appt, local_start

def is_registered_patient(line_user_id: str) -> bool:
    if not line_user_id:
        return False

    try:
        count, user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[is_registered_patient] 查詢 Zendesk 失敗: {e}")
        # 查詢失敗不要硬說未建檔，避免流程最後一刻打回
        # 這裡建議回 False，但要搭配呼叫端顯示「系統查詢異常，請稍後再試」
        return False

    # count>0 但 user=None → 這是「查詢異常」，不是「未建檔」
    if count >= 1 and not user:
        app.logger.error(f"[is_registered_patient] count={count} 但 user=None，疑似 Zendesk search 解析/索引延遲 line_user_id={line_user_id}")
        return False

    if count < 1 or not user:
        return False

    # 統一標準：要能預約 = name + phone 都有（避免半套資料混入預約流程）
    name = (user.get("name") or "").strip()
    phone = (user.get("phone") or "").strip()

    if not phone:
        return False
    if not is_valid_name(name):
        return False

    return True

