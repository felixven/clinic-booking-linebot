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

import certifi 
import os 
import requests 
import json

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
    取得某一天的所有預約（從 Bookings 讀取）
    例如 date_str = "2025-01-15"
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

    # 過濾出「指定日期」的預約
    result = [a for a in all_appts if a["startDateTime"]["date"] == date_str]

    return result



# ======== 診所假資料（之後你要改再改） ========
CLINIC_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763351663/benyamin-bohlouli-B_sK_xgzwVA-unsplash_n6jy9m.jpg"
CLINIC_NAME = "中診所"
CLINIC_ADDRESS = "台中市西屯區市政路 123 號"
CLINIC_LAT = 24.1500
CLINIC_LNG = 120.6500

# 線上預約用的共用圖片
WEEK_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763314182/pulse_ultzw0.jpg"


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
        if text.startswith("查"):
            parts = text.split()
            if len(parts) >= 2:
                date_str = parts[1]   # 第二個字串當日期
                try:
                    appts = list_appointments_for_date(date_str)
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"{date_str} 有 {len(appts)} 筆預約")]
                        )
                    )
                except Exception as e:
                    app.logger.error(f"查預約失敗: {e}")
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="查預約失敗，請看後端 log")]
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

        # ① 「線上約診」→ 本週 / 下週按鈕
        if text == "線上約診":
            ...

        
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
        elif text.startswith("我想預約"):
            # 把「我想預約」後面的字抓出來，當作顯示文字
            slot = text.replace("我想預約", "").strip()  # 例如「本週四 晚診」

            # ButtonsTemplate 的 text 只能放很短，詳細內容另外用 TextMessage 補充
            buttons_template = ButtonsTemplate(
                title="預約成功",
                text="完成預約，請注意約診時間", 
                actions=[
                    MessageAction(
                        label="查詢約診",
                        text="查詢約診"
                    ),
                ],
            )

            template_message = TemplateMessage(
                alt_text="預約成功（DEMO）",
                template=buttons_template
            )

            # 詳細資訊用一般文字訊息，沒字數限制
            detail_text = (
                "預約成功\n"
                f"門診時段：{slot}\n"
                "就診人姓名：王小明\n"
                "預約編號：A123456\n"
                "\n（假資料）"
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        template_message,
                        TextMessage(text=detail_text)
                    ]
                )
            )


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
    app.run(port=5678)