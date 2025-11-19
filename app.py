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

# ======== 跟 Entra 拿 Microsoft Graph 的 access token ========
def get_graph_token():
    tenant_id = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET")

    if not tenant_id or not client_id or not client_secret:
        raise Exception("GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET 有缺，請先在終端機 export")

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
    例如 date_str = "2025-11-15"
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
            app.logger.error(f"解析 startDateTime 失敗: {start_dt_str}, error: {e}")
            continue

        # 轉成台北時間（UTC+8）
        local_dt = utc_dt + timedelta(hours=8)
        local_date_str = local_dt.date().isoformat()  # 'YYYY-MM-DD'

        if local_date_str == date_str:
            result.append(a)

    return result



def get_available_slots_for_date(date_str: str) -> list[str]:
    """
    回傳指定日期「可預約」的時段列表，例如：
    ["09:00", "09:30", "10:00", ...]
    規則：09:00–21:00，每 30 分鐘，排除當天已被預約的「台北時間」開始時段。
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
            app.logger.error(f"解析 startDateTime 失敗（get_available_slots）：{start_dt_str}, error: {e}")
            continue

        local_dt = utc_dt + timedelta(hours=8)
        hhmm = local_dt.strftime("%H:%M")  # 例如 "14:00"
        booked_times.add(hhmm)

    # 生成 09:00 ~ 21:00，每 30 分鐘
    start = datetime.strptime("09:00", "%H:%M")
    end = datetime.strptime("21:00", "%H:%M")

    slots: list[str] = []
    cur = start
    while cur <= end:
        hhmm = cur.strftime("%H:%M")
        if hhmm not in booked_times:
            slots.append(hhmm)
        cur += timedelta(minutes=30)

    return slots

def create_booking_appointment(date_str: str, time_str: str):
    """
    用最簡化方式建立一筆 Bookings 預約。
    - 實際只填必要欄位
    - 客戶資料用假資料（之後你想接 LINE user 資料再改）
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

    # Booking duration（你可先固定 30 分鐘）
    duration = "PT30M"  

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"

    
    
    payload = {
        "customerName": "陳女士",              # 假資料
        "customerEmailAddress": "test@example.com",
        "customerPhone": "0912345678",

        # 🔸 這兩個用你現有的 service/staff
        "serviceId": BOOKING_DEMO_SERVICE_ID,
        "serviceName": "一般門診",              # 看你要叫什麼，都可以

        "startDateTime": {
            "dateTime": utc_iso,
            "timeZone": "UTC"
        },
        "endDateTime": {
            "dateTime": (utc_dt + timedelta(minutes=30)).isoformat() + "Z",
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


def build_slots_carousel(date_str: str, slots: list[str]) -> TemplateMessage:
    """
    將某一天的可預約時段變成 LINE CarouselTemplate。
    slots 例如：["09:00", "09:30", "10:00", ...]
    ✅ 修正版：每個 column 固定 3 個 actions，符合 LINE 要求。
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
                title=f"{date_str}（第 {col_index} 組）",
                text="請選擇看診時段",
                actions=actions,
            )
        )

    return TemplateMessage(
        alt_text=f"{date_str} 可預約時段",
        template=CarouselTemplate(columns=columns),
    )






# ======== 診所假資料（之後你要改再改） ========
CLINIC_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763351663/benyamin-bohlouli-B_sK_xgzwVA-unsplash_n6jy9m.jpg"
CLINIC_NAME = "中診所"
CLINIC_ADDRESS = "台中市西屯區市政路 123 號"
CLINIC_LAT = 24.1500
CLINIC_LNG = 120.6500

# 線上預約用的共用圖片
WEEK_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763314182/pulse_ultzw0.jpg"

BOOKING_DEMO_SERVICE_ID = "172a2a02-a28b-453c-9704-1249633c87b7"
BOOKING_DEMO_STAFF_ID = "cc6bf258-7441-40be-ab8c-78101d228870"



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

    # 模仿你參考的範例：在 handler 裡面用 ApiClient
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
                        messages=[TextMessage(text="成功取得 Graph token，可以往 Bookings 下一步了。")]
                    )
                )
            except Exception as e:
                app.logger.error(f"取得 Graph token 失敗: {e}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="取得 Graph token 失敗，詳情請看後端 log。")]
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
                    reply_text = "查預約失敗，請看後端 log"

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
            else:
                # 使用者只打了「查」沒帶日期
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入：查 YYYY-MM-DD，例如：查 2025-01-15")]
                    )
                )
            return

        
                # === 預約 YYYY-MM-DD → 顯示動態可預約時段 Carousel ===
                # === 預約 YYYY-MM-DD → 顯示動態可預約時段 Carousel ===
        elif text.startswith("預約 "):
            # 範例：預約 2025-02-01
            date_str = text.replace("預約", "").strip()

            try:
                available_slots = get_available_slots_for_date(date_str)
                if not available_slots:
                    reply_msg = TextMessage(text=f"{date_str} 當天目前沒有可預約時段喔～")
                else:
                    reply_msg = build_slots_carousel(date_str, available_slots)
            except Exception as e:
                app.logger.error(f"取得可預約時段失敗: {e}")
                reply_msg = TextMessage(text="取得可預約時段時發生錯誤，請稍後再試 QQ")

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
                text="目前僅開放預約本週及下週的時段，請選擇：",
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

        # ② 「我要預約本週」→ Carousel
        elif text == "我要預約本週":
            columns = [
                CarouselColumn(
                    title="本週四（11/20）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約本週四 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約本週四 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約本週四 晚診"),
                    ],
                ),
                CarouselColumn(
                    title="本週五（11/21）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約本週五 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約本週五 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約本週五 晚診"),
                    ],
                ),
                CarouselColumn(
                    title="本週六（11/22）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約本週六 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約本週六 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約本週六 晚診"),
                    ],
                ),
            ]

            carousel_template = CarouselTemplate(columns=columns)
            template_message = TemplateMessage(
                alt_text="本週可預約門診列表",
                template=carousel_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )

        # ③ 「我要預約下週」→ Carousel
        elif text == "我要預約下週":
            columns = [
                CarouselColumn(
                    title="下週一（11/24）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約下週一 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約下週一 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約下週一 晚診"),
                    ],
                ),
                CarouselColumn(
                    title="下週三（11/26）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約下週三 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約下週三 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約下週三 晚診"),
                    ],
                ),
                CarouselColumn(
                    title="下週五（11/28）",
                    text="可預約門診：早診 / 午診 / 晚診",
                    actions=[
                        MessageAction(label="早診 09:00-12:00", text="我想預約下週五 早診"),
                        MessageAction(label="午診 14:00-17:00", text="我想預約下週五 午診"),
                        MessageAction(label="晚診 18:00-21:00", text="我想預約下週五 晚診"),
                    ],
                ),
            ]

            carousel_template = CarouselTemplate(columns=columns)
            template_message = TemplateMessage(
                alt_text="下週可預約門診列表",
                template=carousel_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )

        # ④ 使用者挑好門診（我想預約本週四 早診）
        # ④ 使用者挑好門診 / 指定時段
               # ④ 使用者挑好門診 / 指定時段（正式建立 Bookings 預約）
        elif text.startswith("我想預約"):
            # 預期格式：我想預約 YYYY-MM-DD HH:MM
            payload = text.replace("我想預約", "").strip()
            parts = payload.split()  # ["2025-11-21", "15:00"]

            if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
                date_str, time_str = parts

                try:
                    created = create_booking_appointment(date_str, time_str)
                    appt_id = created.get("id", "（沒有取得 ID）")

                    reply_text = (
                        "預約成功！🎉\n"
                        f"📅 日期：{date_str}\n"
                        f"🕒 時間：{time_str}\n"
                        f"預約 ID：{appt_id}\n"
                        "\n目前客戶資料為 DEMO 假資料。"
                    )
                except Exception as e:
                    app.logger.error(f"建立 Bookings 預約失敗: {e}")
                    reply_text = "建立預約失敗了，請稍後再試 QQ"
            else:
                # 格式不正確（防呆）
                reply_text = "請用格式：我想預約 YYYY-MM-DD HH:MM 喔！"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return



       # ⑤ 查詢約診 → 顯示一筆假資料 + 「確認回診」按鈕
        elif text == "查詢約診":
            appointment_title = "11/20（週四）早診"
            appointment_text = "時段：9:00–12:00\n姓名：王小明\n預約編號：A123456"

            buttons_template = ButtonsTemplate(
                title=appointment_title,
                text=appointment_text,
                actions=[
                    MessageAction(
                        label="確認回診",
                        text="確認回診"
                    ),
                ],
            )

            template_message = TemplateMessage(
                alt_text="約診查詢結果（DEMO）",
                template=buttons_template
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[template_message]
                )
            )

        # ⑥ 確認回診 → 顯示約診資料 + 提醒 + 「查詢診所位置」按鈕
        elif text == "確認回診":
            # 詳細資料用文字顯示
            detail_text = (
                "回診提醒\n"
                "日期：11/20（週四）\n"
                "時段：14:00–17:00\n"
                "姓名：王小明\n"
                "預約編號：A123456\n"
                "\n請準時於門診開始前 10 分鐘至診所報到。"
            )

            reminder_message = TextMessage(text=detail_text)

            # ButtonsTemplate：只負責提供「查詢診所位置」按鈕
            buttons_template = ButtonsTemplate(
                title="回診資訊確認",
                text="如需導航，請點選下方按鈕查詢診所位置。",
                actions=[
                    MessageAction(
                        label="查詢診所位置",
                        text="查詢診所位置"
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
                    messages=[reminder_message, template_message]
                )
            )

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
                        text="我要看診所地圖"
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

        # ⑧ 「我要看診所地圖」→ 只回地圖一則（補上這個分支會比較完整）
        elif text == "我要看診所地圖":
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
            app.logger.info("非線上約診相關指令，暫不回覆")


if __name__ == "__main__":
    app.run(port=5001)