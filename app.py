from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    CarouselTemplate,
    CarouselColumn,
    LocationMessage,
    MessageAction,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)

from datetime import datetime, timedelta

import certifi 
import os 
import requests 


app = Flask(__name__)

# ======== LINE 基本設定（記得換成你自己的） ========
configuration = Configuration( access_token="foYlKgBuLjIHB8ekKkfkYjVrjABqWg/ZaSve6YjntmGiuO7PZGPtoE49pmLf6iaOji8jvR8E1tSdMBNZUKBdTEWu67T8EAop+PzLsjTwD5Gb+rULtbRaR2jcLjQ+Dpcnb+TuVAUwNRYU4Qwmy80KnwdB04t89/1O/w1cDnyilFU=" ) 
configuration.ssl_ca_cert = certifi.where() 
handler = WebhookHandler("0a35ddd79939b228c5934101a4c979f8")

# ======== 預約時段相關設定（之後要改時段只改這裡） ========
SLOT_START = "09:00"             # 看診起始時間（第一個）
SLOT_END = "21:00"               # 看診結束時間（最後一個）
SLOT_INTERVAL_MINUTES = 30       # 每一格 slot 間隔（目前半小時）
APPOINTMENT_DURATION_MINUTES = 30  # 實際預約時長（要跟 Bookings duration 對齊）
# 禮拜幾
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

# ======== DEMO 患者資料（目前先寫死，之後會改成從 JSON/DB 來） ========
DEMO_CUSTOMER_NAME = "陳女士"
DEMO_CUSTOMER_EMAIL = "test@example.com"
DEMO_CUSTOMER_PHONE = "0912345678"

# ======== 診所假資料（之後你要改再改） ========
CLINIC_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763351663/benyamin-bohlouli-B_sK_xgzwVA-unsplash_n6jy9m.jpg"
CLINIC_NAME = "中醫診所"
CLINIC_ADDRESS = "臺中市西屯區青海路二段242之32號"
CLINIC_LAT = 24.1718527355441
CLINIC_LNG = 120.64402133835931


# 線上預約用的共用圖片
WEEK_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763314182/pulse_ultzw0.jpg"

# ========Booking 相關資料==============
BOOKING_DEMO_SERVICE_ID = "172a2a02-a28b-453c-9704-1249633c87b7"
BOOKING_DEMO_STAFF_ID = "cc6bf258-7441-40be-ab8c-78101d228870"

# serviceNotes 裡當「確認」的標記字串
CONFIRM_NOTE_KEYWORD = "Confirmed via LINE"




# ======== 跟 Entra 拿 Microsoft Graph 的 access token ========
def get_graph_token():
    tenant_id = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET")

    if not tenant_id or not client_id or not client_secret:
        raise Exception("GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET 有缺，先到終端機 export")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    resp = requests.post(url, data=data)
    app.logger.info(f"GRAPH TOKEN STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()
    return resp.json()["access_token"]


def list_appointments_for_date(date_str):
    """
    取得某一天的所有預約（從 Bookings 讀取，依「台北當地日期」判斷）
    例： date_str = "2025-11-15"
    """
    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID，先到終端機 export")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(f"APPOINTMENTS STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()

    all_appts = resp.json().get("value", [])

    result = []
    for a in all_appts:
        start_info = a.get("startDateTime", {})
        start_dt_str = start_info.get("dateTime")  # 例如 "2025-11-20T06:00:00.0000000Z"
        if not start_dt_str:
            continue

        try:
            # 去掉尾巴的 'Z' 跟小數秒
            s = start_dt_str
            if s.endswith("Z"):
                s = s[:-1]
            s = s.split(".")[0]
            utc_dt = datetime.fromisoformat(s)
        except Exception as e:
            app.logger.error(f"解讀 startDateTime 失敗: {start_dt_str}, error: {e}")
            continue

        # 轉成台北時間（UTC+8）
        local_dt = utc_dt + timedelta(hours=8)
        local_date_str = local_dt.date().isoformat()  # 'YYYY-MM-DD'

        if local_date_str == date_str:
            result.append(a)

    return result

def get_next_upcoming_appointment_for_demo():
    """
    取得患者「最近一筆未來的約診」。（DEMO）
    - startDateTime > 現在
    - 只看 Bookings 裡 customerEmailAddress == DEMO_CUSTOMER_EMAIL 的預約
    - 如果沒有符合條件，回傳 (None, None)
    - 如果有，回傳 (appointment_dict, local_start_dt)
    """
    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID，請在終端機 export")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(f"APPOINTMENTS (for upcoming demo) STATUS: {resp.status_code}, BODY: {resp.text}")
    resp.raise_for_status()

    all_appts = resp.json().get("value", [])

    now_local = datetime.now()
    best_appt = None
    best_local_start = None

    for a in all_appts:
        # 如果 Bookings 有 isCancelled 之類的欄位，可以在這裡排除
        if a.get("isCancelled") is True:
            continue

        # 只看 DEMO 患者的預約（用 email 過濾）
        customer_email = (a.get("customerEmailAddress") or "").lower()
        if customer_email != DEMO_CUSTOMER_EMAIL.lower():
            continue

        start_info = a.get("startDateTime", {})
        local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
        if not local_dt:
            continue

        # 只看未來的預約
        if local_dt <= now_local:
            continue

        # 找最近的一筆（時間最早）
        if best_local_start is None or local_dt < best_local_start:
            best_local_start = local_dt
            best_appt = a

    return best_appt, best_local_start

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
    app.logger.info(f"GET APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

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
    app.logger.info(f"DELETE APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

    # 204 No Content / 200 / 202
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


def update_booking_service_notes(appt_id: str, notes_text: str):
    """
    將指定 appointment 的 serviceNotes 更新為 notes_text。
    「診所／工作人員可見的備註」。
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
    app.logger.info(f"PATCH APPT SERVICE NOTES {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")
    resp.raise_for_status()






def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
    """
    將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 datetime」。
    例如 "2025-11-20T06:00:00.0000000Z" → 2025-11-20 14:00:00 (UTC+8)
    """
    if not start_dt_str:
        return None

    try:
        s = start_dt_str
        if s.endswith("Z"):
            s = s[:-1]
        s = s.split(".")[0]
        utc_dt = datetime.fromisoformat(s)
    except Exception as e:
        app.logger.error(f"解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}")
        return None

    # 轉成台北時間（UTC+8）
    local_dt = utc_dt + timedelta(hours=8)
    return local_dt




def get_available_slots_for_date(date_str: str) -> list[str]:
    """
    回傳指定日期「可預約」的時段列表，例如：
    ["09:00", "09:30", "10:00", ...]
    規則：09:00–21:00，每 30 分鐘，排除當天已被預約的開始時段。
    """
    appts = list_appointments_for_date(date_str)

    booked_times = set()
    for appt in appts:
        start_info = appt.get("startDateTime", {})
        start_dt_str = start_info.get("dateTime")  # "2025-11-20T06:00:00.0000000Z"
        if not start_dt_str:
            continue

        try:
            s = start_dt_str
            if s.endswith("Z"):
                s = s[:-1]
            s = s.split(".")[0]
            utc_dt = datetime.fromisoformat(s)
        except Exception as e:
            app.logger.error(f"解讀 startDateTime 失敗（get_available_slots）：{start_dt_str}, error: {e}")
            continue

        local_dt = utc_dt + timedelta(hours=8)
        hhmm = local_dt.strftime("%H:%M")  # 例如 "14:00"
        booked_times.add(hhmm)

    # 09:00 ~ 21:00，每 30 分鐘
    # SLOT_START ~ SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘一格
    start = datetime.strptime(SLOT_START, "%H:%M")
    end = datetime.strptime(SLOT_END, "%H:%M")

    # 生成 SLOT_START ~ SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘一格
    slots: list[str] = []
    cur = start
    while cur <= end:
        hhmm = cur.strftime("%H:%M")
        if hhmm not in booked_times:
            slots.append(hhmm)
        cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots


def create_booking_appointment(date_str: str, time_str: str):
    """
    用最簡化方式建立一筆 Bookings 預約。
    - 只填必要欄位
    - 目前客戶資料是假資料（之後想接 LINE user 資料再改）
    """

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    # 合併日期與時間，轉成 ISO 格式
    # 例如 date_str="2025-11-21", time_str="15:00"
    local_str = f"{date_str} {time_str}:00"  # "2025-11-21 15:00:00"
    local_dt = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S")

    # Bookings API 是吃 UTC → 所以要 -8 小時
    utc_dt = local_dt - timedelta(hours=8)
    utc_iso = utc_dt.isoformat() + "Z"       # "2025-11-21T07:00:00Z"

    # Booking duration（跟 SLOT_INTERVAL/預約時長一致）
    duration = f"PT{APPOINTMENT_DURATION_MINUTES}M"
 

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"

    
    
    payload = {
       "customerName": DEMO_CUSTOMER_NAME,
        "customerEmailAddress": DEMO_CUSTOMER_EMAIL,
        "customerPhone": DEMO_CUSTOMER_PHONE,

        # 🔸 這兩個用你現有的 service/staff
        "serviceId": BOOKING_DEMO_SERVICE_ID,
        "serviceName": "一般門診",              # 看要叫什麼？

        "startDateTime": {
            "dateTime": utc_iso,
            "timeZone": "UTC"
        },
        "endDateTime": {
            "dateTime": (utc_dt + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)).isoformat() + "Z",
            "timeZone": "UTC"
        },

        "priceType": "free",
        "price": 0.0,
        "smsNotificationsEnabled": False,

        # 🔸 至少填一個 staff
        "staffMemberIds": [BOOKING_DEMO_STAFF_ID],

        "maximumAttendeesCount": 1,
        "filledAttendeesCount": 1,
    }


    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    resp = requests.post(url, headers=headers, json=payload)

    app.logger.info(f"CREATE APPT STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()

    return resp.json()

def get_days_until(local_dt: datetime) -> int:
    """
    傳入「台北時間的預約起始 datetime」，回傳「距離今天還有幾天」（用日曆天數）。
    例：今天 12/10，預約 12/13 → 回傳 3。
    """
    today = datetime.now().date()
    appt_date = local_dt.date()
    return (appt_date - today).days



def build_slots_carousel(date_str: str, slots: list[str]) -> TemplateMessage:
    """
    將某一天的可預約時段變成 LINE CarouselTemplate。
    slots 例如：["09:00", "09:30", "10:00", ...]
    每個 column 要固定 3 個 actions才符合 LINE 要求。
    """
    columns = []
    BUTTONS_PER_COLUMN = 3

    for i in range(0, len(slots), BUTTONS_PER_COLUMN):
        chunk = slots[i:i+BUTTONS_PER_COLUMN]

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
                        label="　",  # 全形空白，看起來像空格
                        text="請選擇上方有時間的按鈕",
                    )
                )

        col_index = (i // BUTTONS_PER_COLUMN) + 1
        columns.append(
            CarouselColumn(
                # title=f"{date_str}（第 {col_index} 組）",
                title=f"{date_str}",
                text="請選擇看診時段",
                actions=actions,
            )
        )

    return TemplateMessage(
        alt_text=f"{date_str} 可預約時段",
        template=CarouselTemplate(columns=columns),
    )



# ========= Webhook 入口 =========

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return "OK"


# ========= 訊息處理 =========

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = event.message.text.strip()
    app.logger.info(f"收到使用者訊息: {text}")

    # 參考的範例：在 handler 裡面用 ApiClient
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # === 測試：從這支後端跟 Entra 拿 Graph token ===
        if text == "測試token":
            try:
                token = get_graph_token()
                app.logger.info(f"GRAPH ACCESS TOKEN (HEAD): {token[:30]}...")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="成功取得 Graph token")]
                    )
                )
            except Exception as e:
                app.logger.error(f"Graph token 申請失敗: {e}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="Graph token 申請失敗，後端資訊：")]
                    )
                )
            return

        # === 測試：查某一天 Bookings 預約（指令範例：查 2025-01-15） ===
                # === 測試：查某一天 Bookings 預約（指令範例：查 2025-01-15） ===
        if text.startswith("查 "):
            parts = text.split()
            if len(parts) >= 2:
                date_str = parts[1]   # 第二個字串當日期
                try:
                    appts = list_appointments_for_date(date_str)
                    reply_text = f"{date_str} 有 {len(appts)} 筆預約"
                except Exception as e:
                    app.logger.error(f"查預約失敗: {e}")
                    reply_text = "查預約失敗，後端資訊："

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
            else:
                # 只打了「查」沒帶日期
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入：查 YYYY-MM-DD，例：查 2025-01-15")]
                    )
                )
            return

        # === 預約 YYYY-MM-DD → 顯示動態可預約時段 Carousel ===
        elif text.startswith("預約 "):
            # 範例：預約 2025-02-01
            date_str = text.replace("預約", "").strip()

            try:
                available_slots = get_available_slots_for_date(date_str)
                if not available_slots:
                    reply_msg = TextMessage(text=f"{date_str} 沒有可預約時段")
                else:
                    reply_msg = build_slots_carousel(date_str, available_slots)
            except Exception as e:
                app.logger.error(f"取得可預約時段失敗: {e}")
                reply_msg = TextMessage(text="取得可預約時段時發生錯誤，請稍後再試")

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[reply_msg]
                )
            )
            return

        
        # ① 「線上約診」→ 本週 / 下週按鈕
        if text == "線上約診":
            buttons_template = ButtonsTemplate(
                title="線上預約",
                text="目前僅開放預約本週及下週的時段：",
                thumbnail_image_url=WEEK_IMAGE_URL,
                actions=[
                    MessageAction(
                        label="本週",
                        text="我要預約本週"
                    ),
                    MessageAction(
                        label="下週",
                        text="我要預約下週"
                    ),
                ],
            )

            template_message = TemplateMessage(
                alt_text="線上預約時段選擇",
                template=buttons_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )

        # ② 「我要預約本週」→ 動態顯示本週剩餘可預約日期（不含週日）
        elif text == "我要預約本週":
            today = datetime.now()
            weekday = today.weekday()  # Monday=0 ... Sunday=6

            # 本週一 = 今天 - weekday 天
            monday = today - timedelta(days=weekday)
            saturday = monday + timedelta(days=5)  # 本週六（不含週日）

            # 本週要顯示的日期：從「明天」開始，到本週六為止
            start_date = today + timedelta(days=1)

            candidate_dates = []
            cur = start_date
            while cur.date() <= saturday.date():
                # cur 本身一定是 Mon~Sat，所以不用另外排除 Sunday
                candidate_dates.append(cur.date())
                cur += timedelta(days=1)

            columns = []

            for d in candidate_dates:
                date_str = d.isoformat()  # "YYYY-MM-DD"
                # 查這一天還有沒有可預約 slot
                available_slots = get_available_slots_for_date(date_str)
                if not available_slots:
                    # 當天已滿 / 沒開診 → 不顯示這張卡片
                    continue

                # 顯示名稱，例如：本週四（11/20）
                mmdd = d.strftime("%m/%d")
                weekday_label = WEEKDAY_ZH[d.weekday()]  # 0~6 → 一二三四五六日
                title = f"本週{weekday_label}（{mmdd}）"

                columns.append(
                    CarouselColumn(
                        title=title,
                        text="點擊查看可預約時段。",
                        actions=[
                            MessageAction(
                                label="查看可預約時段",
                                text=f"預約 {date_str}",  #丟給「預約 YYYY-MM-DD」分支
                            ),
                        ],
                    )
                )

            if not columns:
                # 本週沒有任何有空位的日期
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="本週目前沒有可預約的日期")]
                    )
                )
                return

            carousel_template = CarouselTemplate(columns=columns)
            template_message = TemplateMessage(
                alt_text="本週可預約日期列表",
                template=carousel_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )
            return

        
        # ③ 「我要預約下週」→ 動態顯示下週一～下週六的可預約日期
        elif text == "我要預約下週":
            today = datetime.now()
            weekday = today.weekday()  # Monday=0 ... Sunday=6

            # 本週一 + 7 天 = 下週一
            monday = today - timedelta(days=weekday)
            next_monday = monday + timedelta(days=7)
            next_saturday = next_monday + timedelta(days=5)  # 下週六（不含週日）

            candidate_dates = []
            cur = next_monday
            while cur.date() <= next_saturday.date():
                candidate_dates.append(cur.date())
                cur += timedelta(days=1)

            columns = []

            for d in candidate_dates:
                date_str = d.isoformat()  # "YYYY-MM-DD"
                available_slots = get_available_slots_for_date(date_str)
                if not available_slots:
                    continue

                # 顯示名稱，例如：下週三（11/26）
                mmdd = d.strftime("%m/%d")
                weekday_label = WEEKDAY_ZH[d.weekday()]
                title = f"下週{weekday_label}（{mmdd}）"

                columns.append(
                    CarouselColumn(
                        title=title,
                        text="點擊查看可預約時段。",
                        actions=[
                            MessageAction(
                                label="查看這天時段",
                                text=f"預約 {date_str}",  # 一樣丟給「預約 YYYY-MM-DD」分支
                            ),
                        ],
                    )
                )

            if not columns:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="下週目前沒有可預約的日期")]
                    )
                )
                return

            carousel_template = CarouselTemplate(columns=columns)
            template_message = TemplateMessage(
                alt_text="下週可預約日期列表",
                template=carousel_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )
            return


        # ④ 使用者挑好時段（先顯示確認畫面，還沒建立預約）
        elif text.startswith("我想預約"):
            # 預期格式：我想預約 YYYY-MM-DD HH:MM
            payload = text.replace("我想預約", "").strip()
            parts = payload.split()  # 例如 ["2025-11-21", "15:00"]

            if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
                date_str, time_str = parts

                # 顯示用的日期格式（2025/11/21 15:00）
                display_date = date_str.replace("-", "/")
                display_text = f"您選擇的時段是：\n{display_date} {time_str}\n\n是否確認預約？"

                # 確認／取消按鈕
                buttons_template = ButtonsTemplate(
                    title="預約確認",
                    text=display_text,
                    actions=[
                        MessageAction(
                            label="確認預約",
                            text=f"確認預約 {date_str} {time_str}",
                        ),
                        MessageAction(
                            label="取消",
                            text="取消",
                        ),
                    ],
                )

                template_message = TemplateMessage(
                    alt_text="預約確認",
                    template=buttons_template
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[template_message]
                    )
                )
                return

            else:
                # 格式不正確（防呆）
                reply_text = "請用格式：我想預約 YYYY-MM-DD HH:MM"

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
                return

        # ⑤ 使用者按下「確認預約」→ 真正建立 Bookings 預約 + 顯示完成畫面
        elif text.startswith("確認預約"):
            # 預期格式：確認預約 YYYY-MM-DD HH:MM
            payload = text.replace("確認預約", "").strip()
            parts = payload.split()  # 例如 ["2025-11-21", "15:00"]

            if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
                date_str, time_str = parts

                try:
                    created = create_booking_appointment(date_str, time_str)
                    appt_id = created.get("id", "（沒有取得 ID）")

                    display_date = date_str.replace("-", "/")

                    # 完成預約的文字說明（之後這裡可以換成真的患者姓名）
                    detail_text = (
                        "已為您完成預約，請準時報到。\n"
                        f"姓名：{DEMO_CUSTOMER_NAME}\n"
                        f"時段：{display_date} {time_str}\n"
                        # f"預約 ID：{appt_id}"
                    )
                    detail_message = TextMessage(text=detail_text)

                    # Buttons：提供「位置導航」按鈕
                    buttons_template = ButtonsTemplate(
                        title="診所位置",
                        text="如需導航，請點選下方按鈕查看地圖。",
                        actions=[
                            MessageAction(
                                label="位置導航",git add .
                                text="查詢診所位置"
                            ),
                        ],
                    )

                    template_message = TemplateMessage(
                        alt_text="診所位置導航",
                        template=buttons_template
                    )

                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[detail_message, template_message]
                        )
                    )
                    return

                except Exception as e:
                    app.logger.error(f"建立 Bookings 預約失敗: {e}")
                    reply_text = "未成功預約，請重新操作"

            else:
                reply_text = "格式：確認預約 YYYY-MM-DD HH:MM"

            # 格式錯誤或建立失敗
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return


         # ⑤ 約診查詢：
        #   - 沒有 future 預約 → 提示無約診 + 「線上約診」
        #   - 有預約且剩餘天數 >= 3 → 顯示約診 + 「取消約診」按鈕
        #   - 有預約且剩餘天數 < 3 → 顯示約診 + 「確認回診」按鈕
        #   - 若 serviceNotes 已含 Confirmed via LINE → 顯示「已確認」版本，只剩「查詢診所位置」
        elif text == "約診查詢":
            try:
                appt, local_start = get_next_upcoming_appointment_for_demo()
            except Exception as e:
                app.logger.error(f"查詢約診失敗: {e}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="約診查詢失敗，請稍後再試")]
                    )
                )
                return

            # ① 沒有任何 future 預約 → 引導去線上約診
            if not appt or not local_start:
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

            # ② 有 future 預約 → 先算天數，再看有沒有已確認
            days_left = get_days_until(local_start)

            display_date = local_start.strftime("%Y/%m/%d")
            display_time = local_start.strftime("%H:%M")

            customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
            appt_id = appt.get("id", "")

            # 詳細資訊放在 TextMessage（不限 60 字）
            base_text = (
                f"姓名：{customer_name}\n"
                f"看診時間：{display_date} {display_time}\n"
            )

            # ②-0 若已在 LINE 確認過 → 直接顯示「已確認」版本
            service_notes = appt.get("serviceNotes") or ""
            if CONFIRM_NOTE_KEYWORD in service_notes:
                detail_text = (
                    "您已完成回診確認 ✅\n"
                    f"姓名：{customer_name}\n"
                    f"看診時間：{display_date} {display_time}\n"
                    "\n如需導航，可點選下方「查詢診所位置」。"
                )
                detail_message = TextMessage(text=detail_text)

                buttons_template = ButtonsTemplate(
                    title="已確認回診門診",
                    text="如需導航請點下方按鈕。",
                    actions=[
                        MessageAction(
                            label="查詢診所位置",
                            text="查詢診所位置"
                        ),
                    ],
                )

                template_message = TemplateMessage(
                    alt_text="已確認回診",
                    template=buttons_template
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[detail_message, template_message]
                    )
                )
                return

            # ②-1 距離看診 >= 3 天 → 可取消
            if days_left >= 3:
                detail_text = (
                    base_text +
                    f"\n目前距離看診還有 {days_left} 天，"
                    "如需變更請先取消本次預約。"
                )
                detail_message = TextMessage(text=detail_text)

                buttons_template = ButtonsTemplate(
                    title="可取消的門診預約",
                    text="是否取消預約？",
                    actions=[
                        MessageAction(
                            label="取消約診",
                            text=f"取消約診 {appt_id}",   # 之後進入取消流程
                        ),
                    ],
                )

                template_message = TemplateMessage(
                    alt_text="可取消的門診預約",
                    template=buttons_template
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[detail_message, template_message]
                    )
                )
                return

            # ②-2 距離看診 < 3 天 → 不能取消，只能確認
            else:
                detail_text = (
                    base_text +
                    "\n目前距離看診已少於三天，無法透過 LINE 取消預約。\n"
                    "如果您會準時前來，請先完成回診確認。"
                )
                detail_message = TextMessage(text=detail_text)

                buttons_template = ButtonsTemplate(
                    title="即將到診的門診",
                    text="是否確認回診？",
                    actions=[
                        MessageAction(
                            label="確認回診",
                            text=f"確認回診 {appt_id}",   # 之後進入確認流程
                            # text=f"確認回診",
                        ),
                    ],
                )

                template_message = TemplateMessage(
                    alt_text="即將到診的門診",
                    template=buttons_template
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[detail_message, template_message]
                    )
                )
                return
            
        # ⑤-1 「取消約診 {id}」→ 再次確認是否要取消
        # elif text.startswith("取消約診"):
        #     parts = text.split()
        #     appt_id = parts[1] if len(parts) >= 2 else ""

        #     # 如果沒有帶 id，視為要取消「最近一筆 future 預約」（DEMO 用）
        #     if not appt_id:
        #         appt, local_start = get_next_upcoming_appointment_for_demo()
        #     else:
        #         appt, local_start = get_appointment_by_id(appt_id)

        #     if not appt or not local_start:
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="找不到可取消的約診，請先使用「查詢約診」確認目前預約狀態。")]
        #             )
        #         )
        #         return

        #     days_left = get_days_until(local_start)

        #     # < 3 天 → 直接套你要的固定文案
        #     if days_left < 3:
        #         msg = (
        #             "由於距離看診日已少於三天，無法透過 LINE 取消約診。\n"
        #             "如有特殊狀況請直接電話聯繫診所，謝謝理解。"
        #         )
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text=msg)]
        #             )
        #         )
        #         return

        #     # >= 3 天 → 正常進入「確認取消」畫面
        #     display_date = local_start.strftime("%Y/%m/%d")
        #     display_time = local_start.strftime("%H:%M")
        #     customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
        #     appt_id = appt.get("id", "")

        #     detail_text = (
        #         f"您即將取消以下約診：\n"
        #         f"姓名：{customer_name}\n"
        #         f"看診時間：{display_date} {display_time}\n"
        #         f"\n確定要取消嗎？"
        #     )
        #     detail_message = TextMessage(text=detail_text)

        #     buttons_template = ButtonsTemplate(
        #         title="確認取消約診",
        #         text="請選擇是否取消本次約診。",
        #         actions=[
        #             MessageAction(
        #                 label="確認取消",
        #                 text=f"確認取消 {appt_id}",
        #             ),
        #             MessageAction(
        #                 label="保留約診",
        #                 text="查詢約診",   # 回去看一下現況
        #             ),
        #         ],
        #     )

        #     template_message = TemplateMessage(
        #         alt_text="確認取消約診",
        #         template=buttons_template
        #     )

        #     line_bot_api.reply_message(
        #         ReplyMessageRequest(
        #             reply_token=event.reply_token,
        #             messages=[detail_message, template_message]
        #         )
        #     )
        #     return
        
        #         # ⑤-2 「確認取消 {id}」→ 真正呼叫 Bookings 取消預約
        # elif text.startswith("確認取消"):
        #     parts = text.split()
        #     appt_id = parts[1] if len(parts) >= 2 else ""

        #     if not appt_id:
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="要取消的約診資訊不完整，請重新操作「約診查詢」。")]
        #             )
        #         )
        #         return

        #     # 再查一次這筆約診（避免被改時間或已經被取消）
        #     appt, local_start = get_appointment_by_id(appt_id)
        #     if not appt or not local_start:
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="找不到這筆約診，可能已被取消或不存在，請重新查詢約診。")]
        #             )
        #         )
        #         return

        #     days_left = get_days_until(local_start)
        #     if days_left < 3:
        #         msg = (
        #             "由於距離看診日已少於三天，無法透過 LINE 取消約診。\n"
        #             "如有特殊狀況請直接電話聯繫診所，謝謝理解。"
        #         )
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text=msg)]
        #             )
        #         )
        #         return

        #     # 真的取消
        #     try:
        #         cancel_booking_appointment(appt_id)
        #     except Exception as e:
        #         app.logger.error(f"取消預約失敗: {e}")
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="取消約診時發生錯誤，請稍後再試或聯繫診所。")]
        #             )
        #         )
        #         return

        #     display_date = local_start.strftime("%Y/%m/%d")
        #     display_time = local_start.strftime("%H:%M")
        #     customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME

        #     msg = (
        #         "已為您取消以下約診：\n"
        #         f"姓名：{customer_name}\n"
        #         f"看診時間：{display_date} {display_time}\n"
        #         "\n如需重新預約，歡迎使用「線上約診」。"
        #     )
        #     text_message = TextMessage(text=msg)

        #     buttons_template = ButtonsTemplate(
        #         title="下一步操作",
        #         text="如需再次預約可點選下方按鈕。",
        #         actions=[
        #             MessageAction(
        #                 label="線上約診",
        #                 text="線上約診",
        #             ),
        #         ],
        #     )

        #     template_message = TemplateMessage(
        #         alt_text="約診已取消",
        #         template=buttons_template
        #     )

        #     line_bot_api.reply_message(
        #         ReplyMessageRequest(
        #             reply_token=event.reply_token,
        #             messages=[text_message, template_message]
        #         )
        #     )
        #     return

        # ⑤-1 「取消約診 {id}」→ 再次確認是否要取消
        elif text.startswith("取消約診"):
            parts = text.split()
            appt_id = parts[1] if len(parts) >= 2 else ""

            # 如果沒有帶 id，就是取消「最近一筆 future 預約」
            if not appt_id:
                appt, local_start = get_next_upcoming_appointment_for_demo()
            else:
                appt, local_start = get_appointment_by_id(appt_id)

            if not appt or not local_start:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="找不到可取消的約診，請先使用「查詢約診」確認預約狀態。")]
                    )
                )
                return

            days_left = get_days_until(local_start)

            # < 3 天 → 直接套固定文案
            if days_left < 3:
                msg = (
                    "由於距離看診日已少於三天，無法透過 LINE 取消約診。\n"
                    "如有特殊狀況請致電診所，謝謝您的諒解。"
                )
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)]
                    )
                )
                return

            # >= 3 天 → 正常進入「確認取消」畫面
            display_date = local_start.strftime("%Y/%m/%d")
            display_time = local_start.strftime("%H:%M")
            customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
            appt_id = appt.get("id", "")

            detail_text = (
                f"您即將取消以下約診：\n"
                f"姓名：{customer_name}\n"
                f"看診時間：{display_date} {display_time}\n"
                f"\n確定要取消嗎？"
            )
            detail_message = TextMessage(text=detail_text)

            buttons_template = ButtonsTemplate(
                title="確認取消約診",
                text="請選擇是否取消本次約診。",
                actions=[
                    MessageAction(
                        label="確認取消",
                        text=f"確認取消 {appt_id}",
                        # text=f"確認取消",
                    ),
                    MessageAction(
                        label="保留約診",
                        text="約診查詢",   # 回去看一下現況
                    ),
                ],
            )

            template_message = TemplateMessage(
                alt_text="確認取消約診",
                template=buttons_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[detail_message, template_message]
                )
            )
            return
        
        # ⑤-2 「確認取消 {id}」→ 真正呼叫 Bookings 取消預約
        elif text.startswith("確認取消"):
            parts = text.split()
            appt_id = parts[1] if len(parts) >= 2 else ""

            if not appt_id:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="要取消的約診資訊不完整，請重新操作「查詢約診」。")]
                    )
                )
                return

            # 再查一次這筆約診（避免被改時間或已經被取消）
            appt, local_start = get_appointment_by_id(appt_id)
            if not appt or not local_start:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="找不到這筆約診，可能已被取消或不存在，請重新查詢約診。")]
                    )
                )
                return

            days_left = get_days_until(local_start)
            if days_left < 3:
                msg = (
                    "由於距離看診日已少於三天，無法透過 LINE 取消約診。\n"
                    "如有特殊狀況請直接電話聯繫診所，謝謝理解。"
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
                        messages=[TextMessage(text="取消約診時發生錯誤，請稍後再試或聯繫診所。")]
                    )
                )
                return

            display_date = local_start.strftime("%Y/%m/%d")
            display_time = local_start.strftime("%H:%M")
            customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME

            msg = (
                "已為您取消以下約診：\n"
                f"姓名：{customer_name}\n"
                f"時間：{display_date} {display_time}\n"
            )
            text_message = TextMessage(text=msg)

            buttons_template = ButtonsTemplate(
                title="需要重新約診嗎？",
                text="如需重新預約請點選「線上約診」。",
                actions=[
                    MessageAction(
                        label="線上約診",
                        text="線上約診",
                    ),
                ],
            )

            template_message = TemplateMessage(
                alt_text="約診已取消",
                template=buttons_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[text_message, template_message]
                )
            )
            return

        
        # # ⑤-3 「確認回診 {id}」→ 提醒文字＋位置導航（目前先不寫回 Bookings 備註）
        # elif text.startswith("確認回診"):
        #     parts = text.split()
        #     appt_id = parts[1] if len(parts) >= 2 else ""

        #     # 沒帶 id 就用最近一筆 future 預約（DEMO 用）
        #     if not appt_id:
        #         appt, local_start = get_next_upcoming_appointment_for_demo()
        #     else:
        #         appt, local_start = get_appointment_by_id(appt_id)

        #     if not appt or not local_start:
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="找不到可確認的約診，請先使用「查詢約診」確認目前預約狀態。")]
        #             )
        #         )
        #         return

        #     display_date = local_start.strftime("%Y/%m/%d")
        #     display_time = local_start.strftime("%H:%M")
        #     customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME

        #     detail_text = (
        #         "回診提醒：\n"
        #         f"姓名：{customer_name}\n"
        #         f"看診時間：{display_date} {display_time}\n"
        #         "\n請於門診開始前 10 分鐘至診所報到。"
        #     )
        #     detail_message = TextMessage(text=detail_text)

        #     buttons_template = ButtonsTemplate(
        #         title="回診資訊已確認",
        #         text="如需導航至診所，請點選下方按鈕。",
        #         actions=[
        #             MessageAction(
        #                 label="查詢診所位置",
        #                 text="查詢診所位置",
        #             ),
        #         ],
        #     )

        #     template_message = TemplateMessage(
        #         alt_text="回診資訊確認",
        #         template=buttons_template
        #     )

        #     line_bot_api.reply_message(
        #         ReplyMessageRequest(
        #             reply_token=event.reply_token,
        #             messages=[detail_message, template_message]
        #         )
        #     )
        #     return


        # ⑦ 「確認回診 {id}」→ 寫入 Bookings 備註（僅第一次）＋提醒文字＋位置導航
        elif text.startswith("確認回診"):
            parts = text.split(maxsplit=1)
            appt_id = parts[1].strip() if len(parts) >= 2 else ""

            # 沒帶 id → DEMO：抓最近一筆 future 預約
            if not appt_id:
                appt, local_start = get_next_upcoming_appointment_for_demo()
            else:
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
            if days_left >= 3:
                msg = (
                    "目前距離看診日仍大於三天，暫不開放線上確認回診。\n"
                    "可於看診前三天內再透過 LINE 進行確認。"
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

            # 已確認 → 不再 PATCH，只回提示＋位置按鈕，然後一定要 return
            if already_confirmed:
                detail_text = (
                    "您已完成回診確認 ✅\n"
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
                return  # ⬅⬅⬅ 超重要：這樣下面就不會再 PATCH 了

            # ③ 尚未確認 → 這裡才會真的 PATCH，一次寫入 Confirmed via LINE
            now_local = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
            new_line = f"{CONFIRM_NOTE_KEYWORD} on {now_local} (UTC+8)"

            if service_notes:
                merged_notes = service_notes + "\n" + new_line
            else:
                merged_notes = new_line

            try:
                # 用你現在的 helper 名稱（你的是 update_booking_service_notes）
                update_booking_service_notes(appt_id, merged_notes)
            except Exception as e:
                app.logger.error(f"更新 Bookings 備註失敗: {e}")
                # 寫備註失敗不影響使用者體驗，只記 log

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



        # ⑦ 查詢診所位置 → 回傳 Location（地圖）
        elif text == "查詢診所位置":
            location_message = LocationMessage(
                title=CLINIC_NAME,
                address=CLINIC_ADDRESS,
                latitude=CLINIC_LAT,
                longitude=CLINIC_LNG,
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[location_message]
                )
            )


        # ⑦ 「診所資訊」→ 卡片 + 門診時間文字 + 地圖
        elif text == "診所資訊":
            short_text = f"地址：{CLINIC_ADDRESS}\n點擊下方查看地圖位置"

            clinic_info_template = ButtonsTemplate(
                thumbnail_image_url=CLINIC_IMAGE_URL,
                title=CLINIC_NAME,
                text=short_text,
                actions=[
                    MessageAction(
                        label="查看地圖位置",
                        text="查看地圖位置"
                    ),
                ],
            )

            clinic_info_message = TemplateMessage(
                alt_text="診所資訊",
                template=clinic_info_template
            )

            opening_hours_message = TextMessage(
                text=(
                    "門診時間：\n"
                    "週一～週六\n"
                    "早診 09:00–12:00\n"
                    "午診 14:00–17:00\n"
                    "晚診 18:00–21:00"
                )
            )

            location_message = LocationMessage(
                title=CLINIC_NAME,
                address=CLINIC_ADDRESS,
                latitude=CLINIC_LAT,
                longitude=CLINIC_LNG,
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[clinic_info_message, opening_hours_message, location_message]
                )
            )

        # ⑧ 「我要看診所地圖」→ 只回地圖一則
        elif text == "查看地圖位置":
            location_message = LocationMessage(
                title=CLINIC_NAME,
                address=CLINIC_ADDRESS,
                latitude=CLINIC_LAT,
                longitude=CLINIC_LNG,
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[location_message]
                )
            )

        else:
            # 其他訊息先不處理（之後你要串 Copilot / AI 再延伸）
            app.logger.info("非線上約診相關指令，請聯繫客服")


if __name__ == "__main__":
    app.run(port=5001)