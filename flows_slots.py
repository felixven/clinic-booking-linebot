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
)

from linebot.v3.webhooks import MessageEvent

from line_client import line_bot_api


from bookings_core import (
    get_available_slots_for_date,
)

from config import (
    WEEKDAY_ZH,
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

def show_dates_for_week(offset: int, event: MessageEvent):
    """
    根據 offset 顯示某一週可預約的日期 Carousel。
    offset = 0: 本週
    offset = 1: 下週
    offset = 2: 兩週後
    offset = 3: 三週後（目前上限）
    """

    today = datetime.now()
    weekday = today.weekday()  # 0=週一 ... 6=週日
    monday = today - timedelta(days=weekday)  # 本週一

    # 這一週的週一～週六
    week_start = monday + timedelta(days=offset * 7)
    week_end = week_start + timedelta(days=5)

    # --- 起始日期：本週從「明天」開始，其他週從該週一開始 ---
    if offset == 0:
        start_date = today + timedelta(days=1)  # 明天
        if start_date.date() < week_start.date():
            start_date = week_start
    else:
        start_date = week_start

    # --- 收集候選日期 ---
    candidate_dates = []
    cur = start_date
    while cur.date() <= week_end.date():
        candidate_dates.append(cur.date())
        cur += timedelta(days=1)

    columns = []

    # --- 每個日期，如果有可預約時段，就變成一個 column ---
    for d in candidate_dates:
        date_str = d.isoformat()  # YYYY-MM-DD
        available_slots = get_available_slots_for_date(date_str)
        if not available_slots:
            continue  # 沒有任何時段就略過

        mmdd = d.strftime("%m/%d")
        weekday_label = WEEKDAY_ZH[d.weekday()]  # 週一、週二...

        title = f"週{weekday_label}（{mmdd}）"
        columns.append(
            CarouselColumn(
                title=title,
                text="點擊查看可預約時段。",
                actions=[
                    MessageAction(
                        label="查看可預約時段",
                        text=f"預約 {date_str}"
                    )
                ]
            )
        )

    # --- 這一週完全沒有可預約日期 ---
    if not columns:
        if offset == 0:
            no_text = "本週目前沒有可預約的日期。"
        elif offset == 1:
            no_text = "下週目前沒有可預約的日期。"
        elif offset == 2:
            no_text = "兩週後目前沒有可預約的日期。"
        else:
            no_text = (
                "三週後目前沒有可預約的日期。\n"
                "目前僅開放四週內預約，如需更後日期請聯繫診所。"
            )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=no_text)]
            )
        )
        return

    # --- 在最後加「沒有適合的日期？再下一週」的 column（最多到 offset=2）---
    if offset <= 2:
        if offset == 0:
            next_label = "查看下週"
            next_text = "我要預約下週"
        elif offset == 1:
            next_label = "查看兩週後"
            next_text = "我要預約兩週後"
        else:  # offset == 2
            next_label = "查看三週後"
            next_text = "我要預約三週後"

        columns.append(
            CarouselColumn(
                title="沒有適合的日期？",
                text="可以看看下一週的門診時段。",
                actions=[
                    MessageAction(
                        label=next_label,
                        text=next_text
                    )
                ]
            )
        )

    # --- alt_text 依照週次換字 ---
    if offset == 0:
        alt_text = "本週可預約日期列表"
    elif offset == 1:
        alt_text = "下週可預約日期列表"
    elif offset == 2:
        alt_text = "兩週後可預約日期列表"
    else:
        alt_text = "三週後可預約日期列表"

    carousel = CarouselTemplate(columns=columns)
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TemplateMessage(
                    alt_text=alt_text,
                    template=carousel
                )
            ]
        )
    )

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

def is_slot_available(date_str: str, time_str: str) -> bool:
    """
    再檢查一次某日期的某時段是否仍可預約。
    內部直接利用既有的 get_available_slots_for_date(date_str)。
    """
    try:
        slots = get_available_slots_for_date(date_str)  # 例如 ["09:00", "09:30", ...]
    except Exception as e:
        app.logger.error(f"檢查時段可用性失敗: {e}")
        # 保守一點：查不到就當不可預約，避免超收
        return False

    return time_str in slots



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

    today = datetime.today().date()
    latest = today + timedelta(days=21)

    if appt_date < today:
        return False, "目前無法預約過去的日期，請重新選擇預約日期。"

    if appt_date > latest:
        return False, "目前僅開放未來三週內的門診預約，請重新選擇預約日期。"

    return True, ""