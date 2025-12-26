# flows_appointments.py

from flask import current_app as app
from datetime import datetime, timedelta

# --- LINE SDK ---
from linebot.v3.messaging import (
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    MessageAction,
    PostbackAction,
    CarouselTemplate,
    CarouselColumn,
)

# --- 這個 line_bot_api 一定要從 app 取 ---
from line_client import line_bot_api


# --- Bookings (核心 API) ---
from bookings_core import (
    get_appointment_by_id,
    cancel_booking_appointment,
    update_booking_service_notes,
)

# --- Zendesk (ticket 操作) ---
from zendesk_core import (
    find_zendesk_ticket_by_booking_id,
    mark_zendesk_ticket_confirmed,
    mark_zendesk_ticket_cancelled,
)

from patient_core import (
    get_future_appointments_for_line_user,
    get_next_upcoming_appointment_for_line_user,
)

# --- flow 共用工具（你會放在同檔案） ---
from config import (
    CONFIRM_NOTE_KEYWORD,
    DEMO_CUSTOMER_NAME,
    CONFIRM_OPEN_DAYS_BEFORE, # 原本 2，現在 +1
    CANCEL_DEADLINE_DAYS_BEFORE,
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_PHONE,
)

from utils import(
    can_confirm,
    can_cancel
)

def get_days_until(local_dt: datetime) -> int:
    """
    傳入「台北時間的預約起始 datetime」，回傳「距離今天還有幾天」（用日曆天數）。
    例：今天 12/10，預約 12/13 → 回傳 3。
    """
    today = datetime.now().date()
    appt_date = local_dt.date()
    return (appt_date - today).days

# version 4
def flow_query_next_appointment(event, text: str):
    """
    約診查詢 Flow：
    改用 line_user_id + Zendesk phone 過濾 Bookings，
    顯示「這位 LINE 使用者」的所有 future 預約（Carousel）。
    """
    # 先拿 LINE userId
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    try:
        if line_user_id:
            matched_list = get_future_appointments_for_line_user(line_user_id)
        else:
            matched_list = []
    except Exception as e:
        app.logger.error(f"查詢約診失敗: {e}")
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="約診查詢失敗，請稍後再試")]
            )
        )
        return

    # ① 沒有任何他的 future 預約，引導去線上約診（沿用原本行為）
    if not matched_list:
        buttons_template = ButtonsTemplate(
            title="目前沒有約診紀錄",
            text="若需預約看診，請點擊「線上預約」。",
            actions=[
                MessageAction(
                    label="線上約診",
                    text="線上約診"
                ),
            ],
        )

        template_message = TemplateMessage(
            alt_text="沒有約診紀錄",
            template=buttons_template
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[template_message]
            )
        )
        return

    # ② 有 future 預約 → 組成 Carousel
    columns: list[CarouselColumn] = []

    # LINE Carousel 最多 10 個 column，超過先截斷並記 log
    if len(matched_list) > 10:
        app.logger.info(
            f"[flow_query_next_appointment] 預約筆數 {len(matched_list)} 超過 10，僅顯示前 10 筆"
        )
        matched_list = matched_list[:10]

    for local_start, appt in matched_list:
        days_left = get_days_until(local_start)

        display_date = local_start.strftime("%Y/%m/%d")
        display_time = local_start.strftime("%H:%M")

        customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
        appt_id = appt.get("id", "")

        service_notes = appt.get("serviceNotes") or ""
        is_confirmed = CONFIRM_NOTE_KEYWORD in service_notes

        # Title：日期 + 時間
        title = f"{display_date} {display_time}"

        actions = []

        # ②-0 若已在 LINE 確認過 → 顯示「已確認」版本，兩個 action 也都要存在
        if is_confirmed:
            text_body = f"{customer_name}\n已完成回診確認，請準時報到。"
            # 第一顆：無動作按鈕（白按鈕）
            actions.append(
                PostbackAction(
                    label="　",       # 全形空白（看起來像空白按鈕）
                    data="NOOP",      # 不會觸發任何後端事件
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # ②-1 距離看診 >= 3 天 → 可取消
        elif can_cancel(local_start):
            text_body = f"{customer_name}\n距離看診還有 {days_left} 天，可取消。"
            actions.append(
                PostbackAction(
                    label="取消約診",
                    data=f"CANCEL_APPT:{appt_id}",
                    display_text="取消約診",
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # ②-2 距離看診 < 3 天 → 不能取消，只能確認
        else:
            text_body = f"{customer_name}\n距離看診少於 {CANCEL_DEADLINE_DAYS_BEFORE} 天，請確認是否回診。"
            actions.append(
                PostbackAction(
                    label="確認回診",
                    data=f"CONFIRM_APPT:{appt_id}",
                    display_text="確認回診",
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # 防呆：確保每個 column 至少有兩個 actions（符合 LINE Carousel 規則）
        while len(actions) < 2:
            actions.append(
                MessageAction(
                    label="約診查詢",
                    text="約診查詢",
                )
            )

        # 防呆：LINE 規格 text 要有內容
        if not text_body:
            text_body = customer_name

        column = CarouselColumn(
            title=title,
            text=text_body,
            actions=actions,
        )
        columns.append(column)

    carousel = CarouselTemplate(columns=columns)
    template_message = TemplateMessage(
        alt_text="您的門診預約列表",
        template=carousel
    )

    # 前面加一段說明文字
    intro_text = (
        f"您有 {len(columns)} 筆門診預約：\n"
        "請在約診紀錄選擇是否「確認回診」或「取消約診」。"
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=intro_text),
                template_message
            ]
        )
    )
    return

def flow_cancel_request(event, text: str):
    """
    Flow：處理「取消約診 {id}」
    - 優先用傳進來的 appt_id
    - 如果沒有帶 id，就用目前這個 LINE 使用者的預約來當目標（不再用 demo 全診所那種）
    """
    parts = text.split()
    appt_id = parts[1] if len(parts) >= 2 else ""

    # 先拿 LINE userId（用於沒帶 id 的 fallback）
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    # ① 沒帶 id → 用這個 LINE 使用者自己的最近一筆 future 預約
    if not appt_id:
        if not line_user_id:
            # 理論上不會發生，但防呆一下
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="暫時無法取得您的身分，請稍後再試或重新點選「約診查詢」。")]
                )
            )
            return

        appt, local_start = get_next_upcoming_appointment_for_line_user(line_user_id)

    # ② 有帶 id → 直接依 id 查那一筆
    else:
        appt, local_start = get_appointment_by_id(appt_id)

    # ③ 找不到可取消的約診
    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到可取消的約診，請先使用「約診查詢」。")]
            )
        )
        return

    # ④ 判斷距離看診日
    days_left = get_days_until(local_start)
    if not can_cancel(local_start):
        msg = (
            f"距離看診日已少於 {CANCEL_DEADLINE_DAYS_BEFORE} 天，無法透過 LINE 取消約診。\n"
            "如有特殊狀況請致電診所。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # ⑤ 組畫面
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
    appt_id = appt.get("id", "")

    detail_text = (
        "您即將取消以下約診：\n"
        f"姓名：{customer_name}\n"
        f"看診時間：{display_date} {display_time}\n\n"
        "確定要取消嗎？"
    )

    buttons_template = ButtonsTemplate(
        title="確認取消約診",
        text="請選擇是否取消本次約診。",
        actions=[
            # 這裡我們已經改成 PostbackAction 了，如果你還沒改可以先保留舊版
            PostbackAction(
                label="確認取消",
                data=f"CANCEL_CONFIRM:{appt_id}",
                display_text="確認取消",
            ),
            PostbackAction(
                label="保留約診",
                data="CANCEL_KEEP",
                display_text="保留約診",
            ),
        ],
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=detail_text),
                TemplateMessage(alt_text="確認取消約診", template=buttons_template),
            ]
        )
    )
    return


def flow_confirm_cancel(event, text: str):
    """
    Flow：處理「確認取消 {id}」
    規則：
    - 只允許看診日前 ≥ 3 天取消
    - 成功取消 Bookings 後，同步把對應的 Zendesk ticket 標記為「取消 / 不需再提醒」
    - ✅ 一律要求帶有 appt_id（只給按鈕觸發用）
    """
    # 這樣寫可以避免中間多空白出事
    parts = text.split(maxsplit=1)
    appt_id = parts[1].strip() if len(parts) >= 2 else ""

    # ✅ 不再嘗試用 line_user_id 做 fallback，只允許「帶 id 的取消」
    if not appt_id:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text="要取消的資訊不完整，請重新透過「約診查詢」列表中的按鈕進行操作。"
                )]
            )
        )
        return

    # 再查一次這筆約診（避免早就被改時間或取消）
    appt, local_start = get_appointment_by_id(appt_id)
    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到這筆約診，請重新查詢。")]
            )
        )
        return

    days_left = get_days_until(local_start)
    if not can_cancel(local_start):
        msg = (
            f"距離看診日已少於 {CANCEL_DEADLINE_DAYS_BEFORE} 天，"
            "無法透過 LINE 取消約診。\n"
            "如有特殊狀況請電話聯繫診所。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # 真的取消（DELETE Bookings appointment）
    try:
        cancel_booking_appointment(appt_id)
    except Exception as e:
        app.logger.error(f"取消預約失敗: {e}")
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="取消時發生錯誤，請稍後再試")]
            )
        )
        return

    # --- 同步更新 Zendesk ticket：這筆 booking 已經取消，不用再提醒 ---
    booking_id = appt.get("id") or appt_id
    if booking_id:
        try:
            ticket = find_zendesk_ticket_by_booking_id(booking_id)
            if ticket:
                ticket_id = ticket.get("id")
                mark_zendesk_ticket_cancelled(ticket_id)
            else:
                app.logger.info(
                    f"[flow_confirm_cancel] 找不到對應 booking_id={booking_id} 的 ticket，略過同步。"
                )
        except Exception as e:
            app.logger.error(f"[flow_confirm_cancel] 更新 Zendesk ticket 失敗: {e}")
    else:
        app.logger.warning("[flow_confirm_cancel] 這筆 appt 沒有 id，無法同步 Zendesk ticket")

    # === 回覆給使用者 ===
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME

    msg = (
        "已為您取消以下約診：\n"
        f"姓名：{customer_name}\n"
        f"時間：{display_date} {display_time}"
    )

    buttons_template = ButtonsTemplate(
        title="需要重新約診嗎？",
        text="如需重新預約請點選「線上約診」。",
        actions=[
            MessageAction(label="線上約診", text="線上約診"),
        ],
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=msg),
                TemplateMessage(alt_text="約診已取消", template=buttons_template),
            ]
        )
    )
    return


def flow_confirm_visit(event, text: str):
    """
    Flow：處理「確認回診 {id}」
    規則：
    - 只允許看診日前 < 3 天確認
    - serviceNotes 已含 CONFIRM_NOTE_KEYWORD → 不再 PATCH，只回「已確認」
    - 第一次確認時，寫入一行 `Confirmed via LINE on ...`
    並同步更新 Zendesk Ticket 狀態（success + solved）
    """
    # parts = text.split(maxsplit=1)
    # appt_id = parts[1].strip() if len(parts) >= 2 else ""

    # # 先拿 LINE userId（給「沒帶 id」的 fallback 用）
    # line_user_id = None
    # if event.source and hasattr(event.source, "user_id"):
    #     line_user_id = event.source.user_id

    # # 沒帶 id → 用這個 LINE 使用者的最近一筆 future 預約
    # if not appt_id:
    #     if not line_user_id:
    #         line_bot_api.reply_message(
    #             ReplyMessageRequest(
    #                 reply_token=event.reply_token,
    #                 messages=[TextMessage(text="暫時無法取得您的身分，請稍後再試或重新點選「約診查詢」。")]
    #             )
    #         )
    #         return
    #     appt, local_start = get_next_upcoming_appointment_for_line_user(line_user_id)
    # else:
    #     appt, local_start = get_appointment_by_id(appt_id)

    parts = text.split(maxsplit=1)
    appt_id = parts[1].strip() if len(parts) >= 2 else ""

    # ✅ 現在：沒有 appt_id 就直接擋掉，不再幫他抓「下一筆預約」
    if not appt_id:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請先點選「約診查詢」中的列表按鈕進行回診確認。")]
            )
        )
        return

    # 有帶 id 才會真的去撈那一筆預約
    appt, local_start = get_appointment_by_id(appt_id)


    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到需要確認的約診，請先使用「約診查詢」確認預約狀態。")]
            )
        )
        return

    days_left = get_days_until(local_start)
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
    appt_id = appt.get("id", "")

    # ① 太早確認（≥ 3 天） → 擋掉
    if not can_confirm(local_start):
        msg = (
            f"目前距離看診日仍大於 {CONFIRM_OPEN_DAYS_BEFORE} 天，暫不開放線上確認回診。\n"
            f"可於看診前 {CONFIRM_OPEN_DAYS_BEFORE} 天內再透過 LINE 進行確認。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # ② 看這筆約診是不是已經 Confirm 過
    service_notes = appt.get("serviceNotes") or ""
    already_confirmed = (CONFIRM_NOTE_KEYWORD in service_notes)

    # 已確認 → 不再 PATCH，只回提示＋位置按鈕
    if already_confirmed:
        detail_text = (
            "您已完成回診確認\n"
            f"姓名：{customer_name}\n"
            f"看診時間：{display_date} {display_time}\n"
            "\n如需導航，可點選下方「查詢診所位置」。"
        )
        detail_message = TextMessage(text=detail_text)

        buttons_template = ButtonsTemplate(
            title="回診資訊確認",
            text="預約已確認，如需導航請點選下方。",
            actions=[
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置"
                ),
            ],
        )

        template_message = TemplateMessage(
            alt_text="已確認回診資訊",
            template=buttons_template
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[detail_message, template_message]
            )
        )
        return  # ⬅ 一定要 return，避免下面再 PATCH

    # ③ 尚未確認 → 這裡才會真的 PATCH，一次寫入 Confirmed via LINE
    now_local = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    new_line = f"{CONFIRM_NOTE_KEYWORD} on {now_local} (UTC+8)"

    if service_notes:
        merged_notes = service_notes + "\n" + new_line
    else:
        merged_notes = new_line

    # 先試著更新 Bookings 備註（失敗只記 log，不擋流程）
    try:
        update_booking_service_notes(appt_id, merged_notes)
    except Exception as e:
        app.logger.error(f"更新 Bookings 備註失敗: {e}")
        # 寫備註失敗不影響使用者體驗，只記 log

    # --- 同步更新 Zendesk ticket 狀態 ---
    booking_id = appt.get("id")
    if booking_id:
        try:
            ticket = find_zendesk_ticket_by_booking_id(booking_id)
            if ticket:
                ticket_id = ticket.get("id")
                mark_zendesk_ticket_confirmed(ticket_id)
            else:
                app.logger.info(
                    f"[flow_confirm_visit] 找不到對應 booking_id={booking_id} 的 ticket，略過同步。"
                )
        except Exception as e:
            app.logger.error(f"[flow_confirm_visit] 更新 Zendesk ticket 失敗: {e}")
    else:
        app.logger.warning("[flow_confirm_visit] 這筆 appt 沒有 id，無法同步 Zendesk ticket")

    # ====== 回 LINE 提醒文字＋位置導航按鈕 ======
    detail_text = (
        "回診提醒：\n"
        f"姓名：{customer_name}\n"
        f"看診時間：{display_date} {display_time}\n"
        "\n請於門診開始前 10 分鐘至診所報到。"
    )
    detail_message = TextMessage(text=detail_text)

    buttons_template = ButtonsTemplate(
        title="回診資訊已確認",
        text="如需導航至診所，請點選下方按鈕。",
        actions=[
            MessageAction(
                label="查詢診所位置",
                text="查詢診所位置",
            ),
        ],
    )

    template_message = TemplateMessage(
        alt_text="回診資訊確認",
        template=buttons_template
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[detail_message, template_message]
        )
    )
    return