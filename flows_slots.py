# flows_slots.py

from datetime import datetime, timedelta, date
from flask import current_app as app

from linebot.v3.messaging import (
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    CarouselTemplate,
    CarouselColumn,
    MessageAction,
    ButtonsTemplate
)

from linebot.v3.webhooks import MessageEvent

from line_client import line_bot_api


from bookings_core import (
    get_available_slots_for_date,
    get_available_acu_slots_for_date,
    _get_clinic_day_intervals_by_session,
    list_appointments_for_date
)

from line_send import(
    send_line,
)


from state_store import get_state

from config import (
    WEEKDAY_ZH,
    CLOSED_WEEKDAYS,
    BOOKINGS_BUSINESS_CLINIC_ID,
    APPOINTMENT_DURATION_MINUTES,
    SLOT_INTERVAL_MINUTES,
    AFTERNOON_START,

)


def get_week_offset_for_date(target_date: "date") -> int | None:
    """
    給一個日期，判斷它是從「本週一」算起的第幾週：
    0 = 本週、1 = 下週、2 = 兩週後、3 = 三週後
    若不在這四週範圍內，回傳 None。
    """
    today = datetime.now()
    weekday = today.weekday()  # 0=週一
    monday = today - timedelta(days=weekday)  # 本週一

    # 把本週一、target_date 都拉成 date 物件
    base = monday.date()
    delta_days = (target_date - base).days

    if delta_days < 0:
        return None  # 過去的日期，就不管它了

    # 算出是第幾週（每 7 天一週）
    offset = delta_days // 7

    if 0 <= offset <= 3:
        return offset
    return None

# def show_dates_for_week(offset: int, event: MessageEvent):
#     """
#     根據 offset 顯示某一週可預約的日期 Carousel。
#     offset = 0: 本週
#     offset = 1: 下週
#     offset = 2: 兩週後
#     offset = 3: 三週後（目前上限）
#     """

#     today = datetime.now()
#     weekday = today.weekday()  # 0=週一 ... 6=週日
#     monday = today - timedelta(days=weekday)  # 本週一

#     # 這一週的週一～週六
#     week_start = monday + timedelta(days=offset * 7)
#     week_end = week_start + timedelta(days=5)

#     # --- 起始日期：本週從「明天」開始，其他週從該週一開始 ---
#     if offset == 0:
#         start_date = today + timedelta(days=1)  # 明天
#         if start_date.date() < week_start.date():
#             start_date = week_start
#     else:
#         start_date = week_start

#     # --- 收集候選日期 ---
#     candidate_dates = []
#     cur = start_date
#     while cur.date() <= week_end.date():
#         candidate_dates.append(cur.date())
#         cur += timedelta(days=1)

#     columns = []

#     # --- 每個日期，如果有可預約時段，就變成一個 column ---
#     # --- 每個日期，如果有可預約時段，就變成一個 column ---
#     for d in candidate_dates:
#         date_str = d.isoformat()  # YYYY-MM-DD

#         ok, _ = validate_appointment_date(date_str)
#         if not ok:
#             continue

#         # ★ 依 booking_type 分流：看診用「內科 business」，針灸用固定表 + 兩床 business
#         _uid = getattr(getattr(event, "source", None), "user_id", None)
#         state = get_state(_uid) or {}
#         booking_type = (state.get("booking_type") or "clinic").strip()

#         if booking_type == "acupuncture":
#             available_slots = get_available_acu_slots_for_date(date_str)
#         else:
#             available_slots = get_available_slots_for_date(date_str, business_id=BOOKING_BUSINESS_CLINIC_ID)

#         if not available_slots:
#             continue  # 沒有任何時段就略過


#     # --- 這一週完全沒有可預約日期 ---
#     if not columns:
#         if offset == 0:
#             no_text = "本週目前沒有可預約的日期。"
#         elif offset == 1:
#             no_text = "下週目前沒有可預約的日期。"
#         elif offset == 2:
#             no_text = "兩週後目前沒有可預約的日期。"
#         else:
#             no_text = (
#                 "三週後目前沒有可預約的日期。\n"
#                 "目前僅開放四週內預約，如需更後日期請聯繫診所。"
#             )

#         # line_bot_api.reply_message(
#         #     ReplyMessageRequest(
#         #         reply_token=event.reply_token,
#         #         messages=[TextMessage(text=no_text)]
#         #     )
#         # )
#         send_line(
#             line_bot_api,
#             event,
#             messages=[TextMessage(text=no_text)],
#             label="show_dates_for_week:no_columns",  # 可選
#         )
#         return

#     # --- 在最後加「沒有適合的日期？再下一週」的 column（最多到 offset=2）---
#     if offset <= 2:
#         if offset == 0:
#             next_label = "查看下週"
#             next_text = "我要預約下週"
#         elif offset == 1:
#             next_label = "查看兩週後"
#             next_text = "我要預約兩週後"
#         else:  # offset == 2
#             next_label = "查看三週後"
#             next_text = "我要預約三週後"

#         columns.append(
#             CarouselColumn(
#                 title="沒有適合的日期？",
#                 text="可以看看下一週的門診時段。",
#                 actions=[
#                     MessageAction(
#                         label=next_label,
#                         text=next_text
#                     )
#                 ]
#             )
#         )

#     # --- alt_text 依照週次換字 ---
#     if offset == 0:
#         alt_text = "本週可預約日期列表"
#     elif offset == 1:
#         alt_text = "下週可預約日期列表"
#     elif offset == 2:
#         alt_text = "兩週後可預約日期列表"
#     else:
#         alt_text = "三週後可預約日期列表"

#     # carousel = CarouselTemplate(columns=columns)
#     # line_bot_api.reply_message(
#     #     ReplyMessageRequest(
#     #         reply_token=event.reply_token,
#     #         messages=[
#     #             TemplateMessage(
#     #                 alt_text=alt_text,
#     #                 template=carousel
#     #             )
#     #         ]
#     #     )
#     # )
#     carousel = CarouselTemplate(columns=columns)
#     send_line(
#         line_bot_api,
#         event,
#         messages=[
#             TemplateMessage(
#                 alt_text=alt_text,
#                 template=carousel
#             )
#         ],
#         label=f"show_dates_for_week:carousel:{offset}",  # 可選
#     )

def pick_first_available_clinic_time(date_str: str, period: str, business_id: str) -> str | None:
    """
    period: "morning" 或 "evening"
    回傳該 date_str 在該時段第一個可預約 HH:MM；如果沒有回 None
    """
    slots = get_available_slots_for_date(date_str, business_id=business_id)  # 你原本就有
    if not slots:
        return None

    if period == "morning":
        # 你門診早上最晚能約到 12:20（你常數 MORNING_END）
        for hhmm in slots:
            if hhmm < AFTERNOON_START:   # 16:30 前都算早上區間（簡單粗暴但有效）
                return hhmm
        return None

    if period == "evening":
        for hhmm in slots:
            if hhmm >= AFTERNOON_START:
                return hhmm
        return None

    return None


def show_dates_for_week(offset: int, event: MessageEvent, line_user_id: str = None):
    """
    根據 offset 顯示某一週可預約的日期 Carousel。
    offset = 0: 本週
    offset = 1: 下週
    offset = 2: 兩週後
    offset = 3: 三週後（目前上限）
    """

    # === ① 決定 uid：優先用參數，不夠才用 event ===
    uid_from_param = line_user_id
    uid_from_event = getattr(getattr(event, "source", None), "user_id", None)
    line_user_id = uid_from_param or uid_from_event

    if not line_user_id:
        app.logger.error(f"[show_week] missing line_user_id (param={uid_from_param}, event={uid_from_event})")
        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text="系統無法取得您的使用者資訊，請稍後再試。")],
            label="show_dates_for_week:missing_uid",
        )
        return

    # === ② booking_type ===
    state = get_state(line_user_id) or {}
    booking_type = (state.get("booking_type") or "clinic").strip()
    app.logger.info(f"[show_week] enter uid={line_user_id} offset={offset} booking_type={booking_type}")

    # === ③ 算週範圍 ===
    today = datetime.now()
    weekday = today.weekday()  # 0=週一 ... 6=週日
    monday = today - timedelta(days=weekday)

    week_start = monday + timedelta(days=offset * 7)
    week_end = week_start + timedelta(days=5)  # 週一～週六

    if offset == 0:
        start_date = today + timedelta(days=1)  # 明天
        if start_date.date() < week_start.date():
            start_date = week_start
    else:
        start_date = week_start

    candidate_dates = []
    cur = start_date
    while cur.date() <= week_end.date():
        candidate_dates.append(cur.date())
        cur += timedelta(days=1)

    app.logger.info(
        f"[show_week] week_start={week_start.date().isoformat()} week_end={week_end.date().isoformat()} "
        f"candidate_dates={[d.isoformat() for d in candidate_dates]}"
    )

    columns = []

    # === ④ 逐日檢查可預約 ===
    for d in candidate_dates:
        date_str = d.isoformat()

        ok, reason = validate_appointment_date(date_str)
        if not ok:
            app.logger.info(f"[show_week] skip date={date_str} reason=validate_failed detail={reason}")
            continue

        try:
            if booking_type == "acupuncture":
                slots = get_available_acu_slots_for_date(date_str)
            else:
                slots = get_available_slots_for_date(date_str, business_id=BOOKINGS_BUSINESS_CLINIC_ID)
        except Exception as e:
            app.logger.error(f"[show_week] skip date={date_str} reason=get_slots_failed err={repr(e)}")
            continue

        if not slots:
            app.logger.info(f"[show_week] skip date={date_str} reason=no_slots booking_type={booking_type}")
            continue

        app.logger.info(f"[show_week] date={date_str} ok slots_count={len(slots)} first_slots={slots[:5]}")

        mmdd = d.strftime("%m/%d")
        weekday_label = WEEKDAY_ZH[d.weekday()]  # 你自己決定 WEEKDAY_ZH 要不要含「週」
        title = f"週{weekday_label}（{mmdd}）"

        columns.append(
            CarouselColumn(
                title=title,
                text="點擊查看可預約時段。",
                actions=[MessageAction(label="查看可預約時段", text=f"預約 {date_str}")],
            )
        )
        app.logger.info(f"[show_week] appended date={date_str} columns_len={len(columns)}")

    app.logger.info(f"[show_week] done_build columns_len={len(columns)}")

    # === ⑤ 沒有任何欄位 ===
    if not columns:
        no_text_map = {
            0: "本週目前沒有可預約的日期。",
            1: "下週目前沒有可預約的日期。",
            2: "兩週後目前沒有可預約的日期。",
            3: "三週後目前沒有可預約的日期。\n目前僅開放四週內預約，如需更後日期請聯繫診所。",
        }
        no_text = no_text_map.get(offset, "目前沒有可預約的日期。")

        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text=no_text)],
            label="show_dates_for_week:no_columns",
        )
        return

    # === ⑥ 加下一週提示欄位 ===
    if offset <= 2:
        if offset == 0:
            next_label, next_text = "查看下週", "我要預約下週"
        elif offset == 1:
            next_label, next_text = "查看兩週後", "我要預約兩週後"
        else:
            next_label, next_text = "查看三週後", "我要預約三週後"

        columns.append(
            CarouselColumn(
                title="沒有適合的日期？",
                text="可以看看下一週的門診時段。",
                actions=[MessageAction(label=next_label, text=next_text)],
            )
        )

    alt_text_list = ["本週可預約日期列表", "下週可預約日期列表", "兩週後可預約日期列表", "三週後可預約日期列表"]
    alt_text = alt_text_list[offset] if 0 <= offset < len(alt_text_list) else "可預約日期列表"

    carousel = CarouselTemplate(columns=columns)
    send_line(
        line_bot_api,
        event,
        messages=[TemplateMessage(alt_text=alt_text, template=carousel)],
        label=f"show_dates_for_week:carousel:{offset}",
    )

# def show_dates_for_week(offset: int, event: MessageEvent, line_user_id: str):

#     """
#     根據 offset 顯示某一週可預約的日期 Carousel。
#     offset = 0: 本週
#     offset = 1: 下週
#     offset = 2: 兩週後
#     offset = 3: 三週後（目前上限）
#     """
#     # === 取 line_user_id / booking_type ===
#     line_user_id = getattr(getattr(event, "source", None), "user_id", None)
#     state = get_state(line_user_id) or {}
#     booking_type = (state.get("booking_type") or "clinic").strip()

#     app.logger.info(f"[show_week] enter uid={line_user_id} offset={offset} booking_type={booking_type}")

#     today = datetime.now()
#     weekday = today.weekday()  # 0=週一 ... 6=週日
#     monday = today - timedelta(days=weekday)  # 本週一

#     week_start = monday + timedelta(days=offset * 7)
#     week_end = week_start + timedelta(days=5)  # 週一～週六

#     if offset == 0:
#         start_date = today + timedelta(days=1)  # 明天
#         if start_date.date() < week_start.date():
#             start_date = week_start
#     else:
#         start_date = week_start

#     candidate_dates = []
#     cur = start_date
#     while cur.date() <= week_end.date():
#         candidate_dates.append(cur.date())
#         cur += timedelta(days=1)

#     app.logger.info(f"[show_week] candidate_dates={[d.isoformat() for d in candidate_dates]} week_end={week_end.date().isoformat()}")

#     columns = []

#     for d in candidate_dates:
#         date_str = d.isoformat()

#         ok, reason = validate_appointment_date(date_str)
#         app.logger.info(f"[show_week] date={date_str} validate_ok={ok} reason={reason}")
#         if not ok:
#             continue

#         # === 依 booking_type 取可預約時段 ===
#         try:
#             if booking_type == "acupuncture":
#                 slots = get_available_acu_slots_for_date(date_str)
#             else:
#                 # 你「一般內科」那個 business id 要自己帶進去
#                 slots = get_available_slots_for_date(date_str, business_id=BOOKINGS_BUSINESS_CLINIC_ID)
#         except Exception as e:
#             app.logger.error(f"[show_week] date={date_str} get_slots_failed err={repr(e)}")
#             continue

#         app.logger.info(f"[show_week] date={date_str} slots_count={len(slots)} slots={slots}")

#         if not slots:
#             continue

#         mmdd = d.strftime("%m/%d")
#         weekday_label = WEEKDAY_ZH[d.weekday()]
#         title = f"週{weekday_label}（{mmdd}）"

#         columns.append(
#             CarouselColumn(
#                 title=title,
#                 text="點擊查看可預約時段。",
#                 actions=[
#                     MessageAction(label="查看可預約時段", text=f"預約 {date_str}")
#                 ],
#             )
#         )
#         app.logger.info(f"[show_week] date={date_str} -> appended column (now columns_len={len(columns)})")

#     app.logger.info(f"[show_week] done_build columns_len={len(columns)}")

#     if not columns:
#         if offset == 0:
#             no_text = "本週目前沒有可預約的日期。"
#         elif offset == 1:
#             no_text = "下週目前沒有可預約的日期。"
#         elif offset == 2:
#             no_text = "兩週後目前沒有可預約的日期。"
#         else:
#             no_text = (
#                 "三週後目前沒有可預約的日期。\n"
#                 "目前僅開放四週內預約，如需更後日期請聯繫診所。"
#             )

#         send_line(
#             line_bot_api,
#             event,
#             messages=[TextMessage(text=no_text)],
#             label="show_dates_for_week:no_columns",
#         )
#         return

#     # 加「下一週」提示欄位
#     if offset <= 2:
#         if offset == 0:
#             next_label = "查看下週"; next_text = "我要預約下週"
#         elif offset == 1:
#             next_label = "查看兩週後"; next_text = "我要預約兩週後"
#         else:
#             next_label = "查看三週後"; next_text = "我要預約三週後"

#         columns.append(
#             CarouselColumn(
#                 title="沒有適合的日期？",
#                 text="可以看看下一週的門診時段。",
#                 actions=[MessageAction(label=next_label, text=next_text)],
#             )
#         )

#     alt_text = ["本週可預約日期列表", "下週可預約日期列表", "兩週後可預約日期列表", "三週後可預約日期列表"][offset]

#     carousel = CarouselTemplate(columns=columns)
#     send_line(
#         line_bot_api,
#         event,
#         messages=[TemplateMessage(alt_text=alt_text, template=carousel)],
#         label=f"show_dates_for_week:carousel:{offset}",
#     )




def build_clinic_session_buttons(date_str: str, has_morning: bool, has_evening: bool):
    """
    內科：只顯示「早診 / 晚診」兩顆按鈕（不選每個時段）
    使用 MessageAction，點下去會送出文字：
      門診 YYYY-MM-DD 早
      門診 YYYY-MM-DD 晚
    """
    # LINE 不能真正 disable 按鈕，所以若額滿就讓它送出「額滿」文字，後面 handler 再回覆提示
    morning_text = f"門診 {date_str} 早" if has_morning else f"門診 {date_str} 早已滿"
    evening_text = f"門診 {date_str} 晚" if has_evening else f"門診 {date_str} 晚已滿"

    actions = [
        MessageAction(label="早診", text=morning_text),
        MessageAction(label="晚診", text=evening_text),
    ]

    tmpl = ButtonsTemplate(
        title=f"{date_str} 門診預約",
        text="請選擇時段：早診 / 晚診",
        actions=actions,
    )
    return TemplateMessage(alt_text=f"{date_str} 門診預約", template=tmpl)

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



def build_slots_carousel(date_str: str, slots: list[str]) -> TemplateMessage:
    """
    將某一天的可預約時段變成 LINE CarouselTemplate。
    slots 例如：["09:00", "09:30", "10:00", ...]
    每個 column 固定 3 個 actions（足夠好看）。
    最後多一個「看其他日期」的 column。
    總 column 數控制在 10 以內（LINE 限制）。
    """
    columns = []
    BUTTONS_PER_COLUMN = 3

    # 一共最多留 9 個 column 給時段，最後 1 個留給「看其他日期」
    MAX_SLOT_COLUMNS = 9
    max_slots = MAX_SLOT_COLUMNS * BUTTONS_PER_COLUMN
    slots_for_display = slots[:max_slots]

    # 解析日期，等一下要拿來算週次＆顯示
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        target_date = datetime.now().date()
    display_date = target_date.strftime("%Y/%m/%d")

    # === 一、照你原本的方式，把時段塞進 columns ===
    for i in range(0, len(slots_for_display), BUTTONS_PER_COLUMN):
        chunk = slots_for_display[i:i+BUTTONS_PER_COLUMN]

        actions = []
        for idx in range(BUTTONS_PER_COLUMN):
            if idx < len(chunk):
                # 真正有時段的按鈕
                time_str = chunk[idx]
                msg_text = f"我想預約 {date_str} {time_str}"
                actions.append(
                    MessageAction(
                        label=time_str,
                        text=msg_text,
                    )
                )
            else:
                # 用「空白按鈕」補滿，避免不同 column actions 數量不同
                actions.append(
                    MessageAction(
                        label="　",  # 全形空白
                        text="請選擇上方有時間的按鈕",
                    )
                )

        columns.append(
            CarouselColumn(
                title=f"{display_date}",
                text="請選擇看診時段",
                actions=actions,
            )
        )

    # === 二、最後加上一個「看其他日期」 column ===
    offset = get_week_offset_for_date(target_date)
    back_text = None

    if offset == 0:
        back_text = "我要預約本週"
    elif offset == 1:
        back_text = "我要預約下週"
    elif offset == 2:
        back_text = "我要預約兩週後"
    elif offset == 3:
        back_text = "我要預約三週後"

    if back_text:
        # 這個 column 也維持 3 個 actions，第一個是真正的按鈕，後兩個當空白
        actions = [
            MessageAction(
                label="看其他日期",
                text=back_text,
            ),
            MessageAction(
                label="　",
                text="請選擇上方按鈕",
            ),
            MessageAction(
                label="　",
                text="請選擇上方按鈕",
            ),
        ]

        columns.append(
            CarouselColumn(
                title="看其他日期",
                text="回到該週的日期列表重新選擇。",
                actions=actions,
            )
        )

    # 這裡 columns 最多會是 9（時段）+1（看其他日期）= 10，不會再超過
    return TemplateMessage(
        alt_text=f"{display_date} 可預約時段",
        template=CarouselTemplate(columns=columns),
    )

def build_clinic_period_carousel(date_str: str):
    display_date = date_str.replace("-", "/")
    columns = [
        CarouselColumn(
            title=f"{display_date}",
            text="一般內科門診時段：",
            actions=[
                MessageAction(label="早診", text=f"門診早 {date_str}"),
                MessageAction(label="晚診", text=f"門診晚 {date_str}"),
            ],
        )
    ]
    return TemplateMessage(
        alt_text=f"{display_date} 門診時段選擇",
        template=CarouselTemplate(columns=columns),
    )


def is_slot_available(date_str: str, time_str: str, business_id: str) -> bool:
    """
    內科：再檢查一次某日期的某時段是否仍可預約。
    """
    try:
        t = (time_str or "").strip()

        # 正規化 "9:00" -> "09:00"
        try:
            t = datetime.strptime(t, "%H:%M").strftime("%H:%M")
        except Exception:
            try:
                t = datetime.strptime(t, "%H:%M")  # 其實這行不會進
            except Exception:
                # 嘗試 "%-H:%M" 在部分環境不支援，所以用 split 方式
                if ":" in t:
                    h, m = t.split(":", 1)
                    if h.isdigit() and m.isdigit():
                        t = f"{int(h):02d}:{int(m):02d}"

        slots = get_available_slots_for_date(date_str, business_id=business_id)

        # 看 slots 到底是什麼
        app.logger.info(f"[slot_check_clinic] date={date_str} time={t} business_id={business_id} slots_count={len(slots)} head={slots[:10]}")

        return t in slots

    except Exception as e:
        app.logger.error(f"[slot_check_clinic] failed date={date_str} time={time_str} business_id={business_id} err={repr(e)}")
        return False


# def is_slot_available(date_str: str, time_str: str) -> bool:
#     """
#     再檢查一次某日期的某時段是否仍可預約。
#     內部直接利用既有的 get_available_slots_for_date(date_str)。
#     """
#     try:
#         slots = get_available_slots_for_date(date_str)  # 例如 ["09:00", "09:30", ...]
#     except Exception as e:
#         app.logger.error(f"檢查時段可用性失敗: {e}")
#         # 保守一點：查不到就當不可預約，避免超收
#         return False

#     return time_str in slots



def validate_appointment_date(date_str: str) -> tuple[bool, str]:
    """
    驗證預約日期是否合規：
    - 格式正確（YYYY-MM-DD）
    - 不是過去
    - 不超過未來 21 天（三週）
    """
    try:
        appt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False, "日期格式錯誤，請使用 YYYY-MM-DD，例如：2025-12-03"

    # today = datetime.today().date()
    # latest = today + timedelta(days=21)
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())  # 本週一
    latest = monday + timedelta(days=26)              # 第 4 週的週六

    if appt_date < today:
        return False, "目前無法預約過去的日期，請重新選擇預約日期。"

    if appt_date > latest:
        return False, "目前僅開放未來三週內的門診預約，請重新選擇預約日期。"

    if appt_date.weekday() in CLOSED_WEEKDAYS:
        return False, "該日期為休診日，請重新選擇其他日期。"

    return True, ""