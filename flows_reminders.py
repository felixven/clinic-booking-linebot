# flows_reminders.py 建議 import

from datetime import datetime, timedelta
from queue_core import reminder_queue
import json, os

import requests

from flask import current_app as app

from line_client import line_bot_api
from linebot.v3.messaging import (
    TextMessage,
    TemplateMessage,
    CarouselTemplate,
    CarouselColumn,
    PushMessageRequest,
    PostbackAction,
)

# 🔹 Bookings 相關 helper
from bookings_core import (
    get_appointment_by_id,
    parse_booking_datetime_to_local,
    list_appointments_for_date
)

from zendesk_core import (
    _build_zendesk_headers,
    search_zendesk_tickets_for_reminder,  # 找 pending tickets
    mark_zendesk_ticket_queued,
    get_line_user_id_from_ticket,
    _get_ticket_cf_value,
)

from config import (
    ZENDESK_REMINDER_STATE_PENDING,
    ZENDESK_CF_APPOINTMENT_DATE,
    ZENDESK_CF_REMINDER_STATE,
    ZENDESK_CF_BOOKING_ID,
    REMINDER_DAYS_BEFORE,
)



def list_appointments_for_user_and_date(line_user_id: str, date_str: str) -> list[dict]:
    """
    找出某個 LINE user 在某一天的所有 Bookings 預約。

    依賴：
      - list_appointments_for_date(date_str) 會回傳那天所有預約
      - 每個 appointment.serviceNotes 內有 "[LINE_USER] {line_user_id}"
    """
    if not line_user_id:
        return []

    try:
        # 先抓該日期所有預約
        all_appts = list_appointments_for_date(date_str)
    except Exception as e:
        app.logger.error(f"[list_appointments_for_user_and_date] 取得 {date_str} 預約失敗: {e}")
        return []

    result: list[dict] = []
    for appt in all_appts:
        # 我們把 serviceNotes + customerNotes 一起掃，保險一點
        notes = (appt.get("serviceNotes") or "") + " " + (appt.get("customerNotes") or "")
        if line_user_id and line_user_id in notes:
            result.append(appt)

    app.logger.info(
        f"[list_appointments_for_user_and_date] {date_str} line_user_id={line_user_id} 找到 {len(result)} 筆"
    )
    return result

from linebot.v3.messaging import (
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    PostbackAction,
    PushMessageRequest,
)



# 正式好版本
def send_line_reminder(line_user_id: str, appt: dict):
    """
    純粹負責發 LINE 回診提醒（push，不是 reply）。

    現在邏輯：
    - 先算出這一筆 appointment 的「當地日期」
    - 找出同一個 line_user_id 在那一天的所有 Bookings 預約
    - 發一則文字 + 一條 Carousel，Carousel 每張是一筆預約（顯示時間＋門診別）
      - 點「確認回診」會送出 Postback: CONFIRM_APPT:<appt_id>
      - 後面由 handle_postback → flow_confirm_visit 處理
    """
    if not line_user_id:
        app.logger.warning("[send_line_reminder] 缺 line_user_id")
        return

    start_info = appt.get("startDateTime") or {}
    start_str = start_info.get("dateTime")
    if not start_str:
        app.logger.warning("[send_line_reminder] appointment 缺 startDateTime")
        return

    # 用你專案裡既有的 helper 轉成台灣時間（或診所當地時間）
    local_dt = parse_booking_datetime_to_local(start_str)
    if not local_dt:
        app.logger.warning("[send_line_reminder] 無法解析預約時間")
        return

    # 這一筆預約的日期 / 時間（這筆主要是拿來算 date_str 和顯示用）
    display_date = local_dt.strftime("%Y/%m/%d")
    display_time = local_dt.strftime("%H:%M")
    date_str = local_dt.strftime("%Y-%m-%d")   # 拿來查「這一天」的其他預約

    customer_name = appt.get("customerName") or "貴賓"
    service_name = appt.get("serviceName") or "門診"

    # === 1. 找出「這個人在這一天的所有預約」 ===
    same_day_appts = list_appointments_for_user_and_date(line_user_id, date_str)
    if not same_day_appts:
        # 理論上至少會有目前這一筆；保險起見，找不到就只用這一筆
        same_day_appts = [appt]

    # === 2. 組文字訊息（不管幾筆都會先發這段） ===
    text_msg = TextMessage(
        text=(
            f"{customer_name} 您好，\n"
            f"您在 {display_date} 有以下門診預約：\n"
            "請點選欲確認的時段進行回診確認。\n\n"
            "若屆時無法前來，請致電診所取消，謝謝！"
        )
    )

    # === 3. 組 Carousel，每一張是「一筆預約」 ===
    columns: list[CarouselColumn] = []
    for item in same_day_appts:
        s_info = item.get("startDateTime") or {}
        s_str = s_info.get("dateTime")
        s_local = parse_booking_datetime_to_local(s_str) if s_str else None
        if not s_local:
            continue

        time_str = s_local.strftime("%H:%M")
        svc_name = item.get("serviceName") or "門診"
        appt_id = item.get("id", "")

        # 只顯示「時間＋門診別」，例如：09:00 一般門診
        text = f"{time_str} {svc_name}"

        # 按鈕：送出 Postback，交給 handle_postback → flow_confirm_visit
        column = CarouselColumn(
            text=text[:120],  # LINE 限制長度，保險一點截斷
            actions=[
                PostbackAction(
                    label="確認回診",
                    data=f"CONFIRM_APPT:{appt_id}",
                    display_text=f"確認回診 {display_date} {time_str}",
                )
            ],
        )
        columns.append(column)

    if not columns:
        # 真的一筆都沒組出來（理論上不會），就先只發文字
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[text_msg],
            )
        )
        app.logger.info(
            f"[send_line_reminder] 只有文字提醒，line_user_id={line_user_id}, date={date_str}"
        )
        return

    carousel_msg = TemplateMessage(
        alt_text="回診提醒",
        template=CarouselTemplate(columns=columns),
    )

    # 真正發送：文字 + Carousel 一起推播
    line_bot_api.push_message(
        PushMessageRequest(
            to=line_user_id,
            messages=[text_msg, carousel_msg],
        )
    )

    app.logger.info(
        f"[send_line_reminder] 已對 line_user_id={line_user_id} 發送 {date_str} 共 {len(columns)} 筆預約的 Carousel 提醒"
    )

def send_line_reminder_with_appts(line_user_id: str, appts: list[dict]):
    """
    群組版推播（路線1核心）：
    - 不再去查 Bookings
    - 直接用 appts（同一個人同一天的一組）組文字 + Carousel
    """
    if not line_user_id:
        app.logger.warning("[send_line_reminder_with_appts] 缺 line_user_id")
        return

    if not appts:
        app.logger.warning("[send_line_reminder_with_appts] appts 為空")
        return

    # 用最早的時間排序，顯示比較自然
    def _sort_key(a: dict):
        s = (a.get("startDateTime") or {}).get("dateTime") or ""
        return s

    appts_sorted = sorted(appts, key=_sort_key)

    # 取第一筆決定 display_date/稱呼
    first = appts_sorted[0]
    first_start_str = (first.get("startDateTime") or {}).get("dateTime")
    first_local = parse_booking_datetime_to_local(first_start_str) if first_start_str else None
    if not first_local:
        app.logger.warning("[send_line_reminder_with_appts] 無法解析第一筆預約時間")
        return

    display_date = first_local.strftime("%Y/%m/%d")
    customer_name = first.get("customerName") or "貴賓"

    text_msg = TextMessage(
         text=(
            f"{customer_name} 您好，\n"
            f"您在 {display_date} 有以下門診預約：\n"
            "請點選欲確認的時段進行回診確認。\n\n"
            "若屆時無法前來，請致電診所取消，謝謝！"
        )
    )

    columns: list[CarouselColumn] = []
    for item in appts_sorted:
        s_str = (item.get("startDateTime") or {}).get("dateTime")
        s_local = parse_booking_datetime_to_local(s_str) if s_str else None
        if not s_local:
            continue

        time_str = s_local.strftime("%H:%M")
        svc_name = item.get("serviceName") or "門診"
        appt_id = item.get("id", "")

        col_text = f"{time_str} {svc_name}"

        columns.append(
            CarouselColumn(
                text=col_text[:120],
                actions=[
                    PostbackAction(
                        label="確認回診",
                        data=f"CONFIRM_APPT:{appt_id}",
                        display_text=f"確認回診 {display_date} {time_str}",
                    )
                ],
            )
        )

    if not columns:
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[text_msg],
            )
        )
        app.logger.info(
            f"[send_line_reminder_with_appts] 只有文字提醒 line_user_id={line_user_id} date={display_date}"
        )
        return

    carousel_msg = TemplateMessage(
        alt_text="回診提醒",
        template=CarouselTemplate(columns=columns),
    )

    line_bot_api.push_message(
        PushMessageRequest(
            to=line_user_id,
            messages=[text_msg, carousel_msg],
        )
    )

    app.logger.info(
        f"[send_line_reminder_with_appts] 已推播 line_user_id={line_user_id} date={display_date} count={len(columns)}"
    )



#正常正式版
def send_line_reminder_and_log(ticket: dict, appt: dict, days_before: int | None) -> bool:
    """
    整合流程：
    1. 從 ticket / appointment 找出 line_user_id
    2. 發 LINE 提醒
    3. 把 ticket 的 reminder_state 改成 queued、attempts + 1
    4. 在 ticket 留一則 internal note 紀錄這次提醒（含幾天前）

    days_before:
        - None  = 手動測試（沒特別指定幾天）
        - 0     = 當天提醒
        - >0    = 預約前 N 天提醒
    """

    ticket_id = ticket.get("id")
   

    if not ticket_id:
        app.logger.error("[send_line_reminder_and_log] ticket 沒有 id，無法處理")
        return False
    
    # 1. 找出 line_user_id
    line_user_id = get_line_user_id_from_ticket(ticket, appt)
    if not line_user_id:
        app.logger.warning(
            f"[send_line_reminder_and_log] ticket_id={ticket_id} 找不到 line_user_id，略過"
        )
        return False

    # 2. 先發 LINE 提醒
    try:
        send_line_reminder(line_user_id, appt)
        app.logger.info(
            f"[send_line_reminder_and_log] 已對 ticket_id={ticket_id} 發送 LINE 提醒"
        )
    except Exception as e:
        app.logger.error(
            f"[send_line_reminder_and_log] 發送 LINE 提醒失敗 ticket_id={ticket_id}: {e}"
        )
        return False

    # 3. 更新 ticket 狀態為 queued + attempts+1
    try:
        mark_zendesk_ticket_queued(ticket_id, ticket)
    except Exception as e:
        app.logger.error(
            f"[send_line_reminder_and_log] 更新 reminder_state=queued 失敗 ticket_id={ticket_id}: {e}"
        )
        # 就算這步失敗，還是視為有發過 LINE，所以這裡不 return False

    # 4. 留一則 internal note
    try:
        add_zendesk_reminder_comment(ticket_id, appt, days_before)
    except Exception as e:
        app.logger.error(
            f"[send_line_reminder_and_log] 新增提醒備註失敗 ticket_id={ticket_id}: {e}"
        )

    return True



def add_zendesk_reminder_comment(ticket_id: int, appt: dict, days_before: int | None) -> bool:
    """
    在 Zendesk ticket 上新增一則 internal note，
    紀錄「已發送 LINE 回診提醒」。

    days_before:
        - None  = 手動測試（沒特別指定幾天）
        - 0     = 當天提醒
        - >0    = 預約前 N 天提醒
    """
    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    # ----- 安全解析預約時間 -----
    start_info = appt.get("startDateTime") or {}
    start_str = start_info.get("dateTime")  # 這裡才是字串
    local_dt = None
    if start_str:
        try:
            local_dt = parse_booking_datetime_to_local(start_str)
        except Exception as e:
            app.logger.error(f"[add_zendesk_reminder_comment] 解析預約時間失敗: {e}")

    if local_dt:
        display_date = local_dt.strftime("%Y/%m/%d")
        display_time = local_dt.strftime("%H:%M")
        appt_part = f"{display_date} {display_time}"
    else:
        appt_part = "(預約時間解析失敗)"

    # ----- 說明這次提醒屬於什麼情境 -----
    if days_before is None:
        when_part = "（手動測試觸發）"
    elif days_before == 0:
        when_part = "（預約當天提醒）"
    elif days_before > 0:
        when_part = f"（預約前 {days_before} 天提醒）"
    else:
        when_part = f"（預約後 {abs(days_before)} 天觸發，請檢查排程邏輯）"

    body = (
        "已透過 LINE 發送回診提醒給病患。\n"
        f"預約時段：{appt_part}\n"
        f"{when_part}"
    )

    payload = {
        "ticket": {
            "comment": {
                "body": body,
                "public": False,   # internal note
            }
        }
    }

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(f"[add_zendesk_reminder_comment] 更新成功 ticket_id={ticket_id}")
        return True
    except Exception as e:
        app.logger.error(f"[add_zendesk_reminder_comment] 更新失敗 ticket_id={ticket_id}: {e}")
        return False
    
# def process_reminder_group(
#     line_user_id: str,
#     appt_date_str: str,
#     days_before: int | None,
#     items: list[tuple[dict, dict]],
# ) -> int:
#     """
#     RQ worker 使用的「群組提醒」job。

#     同一個 line_user_id、同一個看診日期、同一輪提醒（days_before）
#     - 發「一則」LINE 回診提醒（內含當天所有預約的 Carousel）
#     - 把這組裡所有 ticket 的 reminder_state 改成 queued 並寫入備註

#     回傳：實際處理幾張 ticket
#     """
#     if not items:
#         app.logger.info(
#             f"[process_reminder_group] line_user_id={line_user_id}, date={appt_date_str} items 為空，略過"
#         )
#         return 0

#     # 先拿第一張 ticket 做「代表」，走原本的整合流程（會發 LINE + queued + 備註）
#     first_ticket, first_appt = items[0]
#     first_ticket_id = first_ticket.get("id")

#     app.logger.info(
#         f"[process_reminder_group] 開始處理 line_user_id={line_user_id}, "
#         f"date={appt_date_str}, days_before={days_before}, "
#         f"tickets_in_group={len(items)}，first_ticket_id={first_ticket_id}"
#     )

#     ok = send_line_reminder_and_log(first_ticket, first_appt, days_before=days_before)
#     if not ok:
#         app.logger.error(
#             f"[process_reminder_group] 第一張 ticket_id={first_ticket_id} 發送失敗，"
#             "整組 ticket 先不繼續處理（避免狀態不同步）"
#         )
#         return 0

#     processed = 1  # 第一張已透過 send_line_reminder_and_log 處理

#     # 其餘 ticket：不再發 LINE，只更新 queued + 備註
#     for ticket, appt in items[1:]:
#         ticket_id = ticket.get("id")
#         if not ticket_id:
#             continue

#         # 3. 更新 ticket 狀態為 queued + attempts+1
#         try:
#             mark_zendesk_ticket_queued(ticket_id, ticket)
#         except Exception as e:
#             app.logger.error(
#                 f"[process_reminder_group] ticket_id={ticket_id} 更新 reminder_state=queued 失敗: {e}"
#             )

#         # 4. 留一則 internal note
#         try:
#             add_zendesk_reminder_comment(ticket_id, appt, days_before)
#         except Exception as e:
#             app.logger.error(
#                 f"[process_reminder_group] ticket_id={ticket_id} 新增提醒備註失敗: {e}"
#             )

#         processed += 1

#     app.logger.info(
#         f"[process_reminder_group] 完成 line_user_id={line_user_id}, date={appt_date_str}, "
#         f"days_before={days_before}，共處理 {processed} 張 ticket"
#     )
#     return processed

def process_reminder_group(
    line_user_id: str,
    appt_date_str: str,
    days_before: int | None,
    items: list[tuple[dict, dict]],
) -> int:
    """
    群組提醒（路線1）：
    - 發「一則」LINE 回診提醒（用 items 裡的 appt 組 carousel，不再重新查 Bookings）
    - 把這組裡所有 ticket 的 reminder_state 改成 queued 並寫入備註
    """

    app.logger.info(f"[process_reminder_group] START job for line_user_id={line_user_id} items={len(items)}")

    if not items:
        app.logger.info(
            f"[process_reminder_group] line_user_id={line_user_id}, date={appt_date_str} items 為空，略過"
        )
        return 0

    app.logger.info(
        f"[process_reminder_group] 開始處理 line_user_id={line_user_id}, "
        f"date={appt_date_str}, days_before={days_before}, tickets_in_group={len(items)}"
    )

    # 1) 先推播一次（只用 items 的 appt 組 carousel）
    try:
        appts = [appt for (_, appt) in items if appt]
        send_line_reminder_with_appts(line_user_id, appts)
    except Exception as e:
        app.logger.error(
            f"[process_reminder_group] 推播 LINE 失敗，整組不更新（避免狀態不同步）: {e}"
        )
        return 0

    # 2) 推播成功後：把整組 tickets 都 queued + note
    processed = 0
    for ticket, appt in items:
        ticket_id = ticket.get("id") if ticket else None
        if not ticket_id:
            continue

        try:
            mark_zendesk_ticket_queued(ticket_id, ticket)
        except Exception as e:
            app.logger.error(
                f"[process_reminder_group] ticket_id={ticket_id} 更新 reminder_state=queued 失敗: {e}"
            )

        try:
            add_zendesk_reminder_comment(ticket_id, appt, days_before)
        except Exception as e:
            app.logger.error(
                f"[process_reminder_group] ticket_id={ticket_id} 新增提醒備註失敗: {e}"
            )

        processed += 1

    app.logger.info(
        f"[process_reminder_group] 完成 line_user_id={line_user_id}, date={appt_date_str}, "
        f"days_before={days_before}，共處理 {processed} 張 ticket"
    )
    return processed




#好的正式版
def run_reminder_check(days_before: int | None = None) -> int:
    """
    跑一次「回呼提醒檢查」：
    - 找出 reminder_state = pending 的 ticket
    - 看它對應的約診是不是「還有 days_before 天」
    - 符合條件的就發 LINE + 更新 ticket（透過 RQ queue）
    回傳：這一輪 enqueue 了幾個「群組 job」

    以前：for ticket in tickets: 裡面直接 enqueue("...send_line_reminder_and_log", ticket, appt, days_before)
    現在：
    先把同一個 (line_user_id, appt_date_str, days_before) 的 ticket 放進同一組 items
    每組只 enqueue 一次，丟到 process_reminder_group
    """
    if days_before is None:
        days_before = REMINDER_DAYS_BEFORE

    today = datetime.now().date()
    target_date = today + timedelta(days=days_before)

    tickets = search_zendesk_tickets_for_reminder()

    # key: (line_user_id, appt_date_str, days_before) -> list[(ticket, appt)]
    groups: dict[tuple[str, str, int | None], list[tuple[dict, dict]]] = {}

    for ticket in tickets:
        ticket_id = ticket.get("id")

        state = _get_ticket_cf_value(ticket, ZENDESK_CF_REMINDER_STATE)
        if state != ZENDESK_REMINDER_STATE_PENDING:
            app.logger.info(
                f"[run_reminder_check] ticket_id={ticket_id} state={state}，略過不再發 LINE"
            )
            continue

        # 1. 先看日期是否是這一輪要處理的 target_date
        appt_date_str = _get_ticket_cf_value(ticket, ZENDESK_CF_APPOINTMENT_DATE)
        if not appt_date_str:
            continue

        try:
            appt_date = datetime.strptime(appt_date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        if appt_date != target_date:
            continue

        # 2. 找對應的 Bookings appointment
        booking_id = _get_ticket_cf_value(ticket, ZENDESK_CF_BOOKING_ID)
        appt, local_start = get_appointment_by_id(booking_id)
        if not appt or not local_start:
            continue

        # 3. 找 line_user_id（這個是之後分組的 key）
        line_user_id = get_line_user_id_from_ticket(ticket, appt)
        if not line_user_id:
            app.logger.warning(
                f"[run_reminder_check] ticket_id={ticket_id} 找不到 line_user_id，略過"
            )
            continue

        key = (line_user_id, appt_date_str, days_before)
        groups.setdefault(key, []).append((ticket, appt))

    # 4. 每一組 (line_user_id, date, days_before) enqueue 一個 group job
    processed_groups = 0

    for (line_user_id, appt_date_str, days), items in groups.items():
        if not items:
            continue

        job = reminder_queue.enqueue(
            "flows_reminders.process_reminder_group",  # 新增的 group handler
            line_user_id,
            appt_date_str,
            days,
            items,  # list[(ticket, appt)]，RQ 會用 pickle 存
        )
        app.logger.info(
            f"[run_reminder_check] 已 enqueue group job_id={job.id} "
            f"line_user_id={line_user_id} appointment_date={appt_date_str} "
            f"tickets_count={len(items)}"
        )
        processed_groups += 1

    return processed_groups

    
# 舊版會洗line訊息的run reminder check    
# def run_reminder_check(days_before: int | None = None) -> int:
#     """
#     跑一次「回呼提醒檢查」：
#     - 找出 reminder_state = pending 的 ticket
#     - 看它對應的約診是不是「還有 days_before 天」
#     - 符合條件的就發 LINE + 更新 ticket
#     回傳處理幾筆
#     """
#     # 如果呼叫方有帶自訂天數，就用呼叫方的；否則用全域設定
#     if days_before is None:
#         days_before = REMINDER_DAYS_BEFORE

#     today = datetime.now().date()
#     target_date = today + timedelta(days=days_before)

#     tickets = search_zendesk_tickets_for_reminder()

#     processed = 0
#     for ticket in tickets:
#         state = _get_ticket_cf_value(ticket, ZENDESK_CF_REMINDER_STATE)
#         if state != ZENDESK_REMINDER_STATE_PENDING:
#             # 已經不是 pending，就不要再發 LINE 了
#             app.logger.info(
#                 f"[run_reminder_check] ticket_id={ticket.get('id')} state={state}，略過不再發 LINE"
#             )
#             continue

#         # 從 ticket custom fields 拿看診日期（你之前有 ZENDESK_CF_APPOINTMENT_DATE）
#         appt_date_str = _get_ticket_cf_value(ticket, ZENDESK_CF_APPOINTMENT_DATE)
#         if not appt_date_str:
#             continue

#         try:
#             appt_date = datetime.strptime(appt_date_str, "%Y-%m-%d").date()
#         except Exception:
#             continue

#         # 只處理「剛好是 target_date 的那一天」
#         if appt_date != target_date:
#             continue

#         # 這裡就去抓該 booking + 發 LINE + 更新 ticket
#         booking_id = _get_ticket_cf_value(ticket, ZENDESK_CF_BOOKING_ID)
#         appt, local_start = get_appointment_by_id(booking_id)
#         if not appt or not local_start:
#             continue

#         # ok = send_line_reminder_and_log(ticket, appt, days_before)
#         # if ok:
#         #     processed += 1
#         # 改成丟到 RQ queue，交給 worker 在背景處理
     

#         job = reminder_queue.enqueue(
#             "flows_reminders.send_line_reminder_and_log",     # 用字串路徑，worker 會去 import
#             ticket,
#             appt,
#             days_before,
#         )
#         app.logger.info(
#             f"[run_reminder_check] 已 enqueue job_id={job.id} "
#             f"ticket_id={ticket.get('id')} appointment_date={appt_date_str}"
#         )
#         processed += 1  # 這裡代表「排了幾個 job」，不是「立即成功幾次」

#     return processed

# Debug備份版
# def run_reminder_check(days_before: int | None = None) -> int:
#     """
#     跑一次「回呼提醒檢查」：
#     - 找出 reminder_state = pending 的 ticket
#     - 看它對應的約診是不是「還有 days_before 天」
#     - 符合條件的就發 LINE + 更新 ticket（透過 RQ queue）
#     回傳：這一輪 enqueue 了幾個「群組 job」
#     """
#     if days_before is None:
#         days_before = REMINDER_DAYS_BEFORE

#     today = datetime.now().date()
#     target_date = today + timedelta(days=days_before)

#     app.logger.info(
#         f"[run_reminder_check][DEBUG] today={today}, days_before={days_before}, target_date={target_date}"
#     )

#     tickets = search_zendesk_tickets_for_reminder()
#     app.logger.info(
#         f"[run_reminder_check][DEBUG] 從 search_zendesk_tickets_for_reminder 撈到 {len(tickets)} 張 ticket"
#     )

#     # key: (line_user_id, appt_date_str, days_before) -> list[(ticket, appt)]
#     groups: dict[tuple[str, str, int | None], list[tuple[dict, dict]]] = {}

#     for ticket in tickets:
#         ticket_id = ticket.get("id")
#         app.logger.info(f"[run_reminder_check][DEBUG] 處理 ticket_id={ticket_id}")

#         state = _get_ticket_cf_value(ticket, ZENDESK_CF_REMINDER_STATE)
#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} reminder_state={state}"
#         )
#         if state != ZENDESK_REMINDER_STATE_PENDING:
#             app.logger.info(
#                 f"[run_reminder_check] ticket_id={ticket_id} state={state}，略過不再發 LINE"
#             )
#             continue

#         # 1. 看日期
#         appt_date_str = _get_ticket_cf_value(ticket, ZENDESK_CF_APPOINTMENT_DATE)
#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} appt_date_str={appt_date_str}"
#         )
#         if not appt_date_str:
#             app.logger.info(
#                 f"[run_reminder_check][DEBUG] ticket_id={ticket_id} 沒有 Appointment Date，自動略過"
#             )
#             continue

#         try:
#             appt_date = datetime.strptime(appt_date_str, "%Y-%m-%d").date()
#         except Exception as e:
#             app.logger.warning(
#                 f"[run_reminder_check][DEBUG] ticket_id={ticket_id} 解析 appt_date_str 失敗: {e}"
#             )
#             continue

#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} appt_date={appt_date}, target_date={target_date}"
#         )

#         if appt_date != target_date:
#             app.logger.info(
#                 f"[run_reminder_check][DEBUG] ticket_id={ticket_id} appt_date != target_date，略過"
#             )
#             continue

#         # 2. Bookings appointment
#         booking_id = _get_ticket_cf_value(ticket, ZENDESK_CF_BOOKING_ID)
#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} booking_id={booking_id}"
#         )
#         appt, local_start = get_appointment_by_id(booking_id)
#         if not appt or not local_start:
#             app.logger.warning(
#                 f"[run_reminder_check][DEBUG] ticket_id={ticket_id} 找不到 appointment，略過"
#             )
#             continue

#         # 3. 找 line_user_id
#         line_user_id = get_line_user_id_from_ticket(ticket, appt)
#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} line_user_id={line_user_id}"
#         )
#         if not line_user_id:
#             app.logger.warning(
#                 f"[run_reminder_check] ticket_id={ticket_id} 找不到 line_user_id，略過"
#             )
#             continue

#         key = (line_user_id, appt_date_str, days_before)
#         groups.setdefault(key, []).append((ticket, appt))
#         app.logger.info(
#             f"[run_reminder_check][DEBUG] ticket_id={ticket_id} 加入 group key={key}, "
#             f"目前 group size={len(groups[key])}"
#         )

#     # 4. 每一組 enqueue 一個 group job
#     processed_groups = 0

#     for (line_user_id, appt_date_str, days), items in groups.items():
#         if not items:
#             continue

#         job = reminder_queue.enqueue(
#             "flows_reminders.process_reminder_group",
#             line_user_id,
#             appt_date_str,
#             days,
#             items,
#         )
#         app.logger.info(
#             f"[run_reminder_check] 已 enqueue group job_id={job.id} "
#             f"line_user_id={line_user_id} appointment_date={appt_date_str} "
#             f"tickets_count={len(items)}"
#         )
#         processed_groups += 1

#     app.logger.info(
#         f"[run_reminder_check][DEBUG] 最終 group 數量={len(groups)}, enqueue group job 數量={processed_groups}"
#     )

#     return processed_groups



