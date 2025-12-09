from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    MessageAction,
    LocationMessage,
)

from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    PostbackEvent, 
)

from datetime import datetime, timedelta, date

from dotenv import load_dotenv
load_dotenv()
import os
from line_client import line_bot_api, handler


from bookings_core import (
    list_appointments_for_date,
    get_available_slots_for_date,
    create_booking_appointment,
    get_graph_token
)


from zendesk_core import (
    search_zendesk_user_by_line_id,
    create_zendesk_user,
    upsert_zendesk_user_basic_profile,
    create_zendesk_appointment_ticket,
)

from patient_core import (
    is_registered_patient,
)

from flows_appointments import (
   flow_query_next_appointment,
   flow_cancel_request,
   flow_confirm_cancel,
   flow_confirm_visit,
)

from flows_slots import (
    show_dates_for_week,
    build_slots_carousel,
    is_slot_available,
    validate_appointment_date,
)

from flows_reminders import(
    run_reminder_check,
)


app = Flask(__name__)

@app.route("/line-booking", methods=["GET"])
def health_check():
    return "OK", 200

from config import (
    WEEKDAY_ZH,
    BOOKING_DEMO_SERVICE_ID,
    BOOKING_DEMO_STAFF_ID,
    BOOKING_BUSINESS_ID,
    GRAPH_TENANT_ID,
    GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET,
    ZENDESK_SUBDOMAIN,
    ZENDESK_EMAIL,
    ZENDESK_API_TOKEN,
    ZENDESK_CF_LINE_USER_ID,
    ZENDESK_CF_BOOKING_ID,
    ZENDESK_CF_APPOINTMENT_DATE,
    ZENDESK_CF_APPOINTMENT_TIME,
    ZENDESK_CF_REMINDER_STATE,
    ZENDESK_CF_REMINDER_ATTEMPTS,
    ZENDESK_CF_LAST_CALL_ID,
    ZENDESK_APPOINTMENT_FORM_ID,

    PROFILE_STATUS_EMPTY, 
    PROFILE_STATUS_NEED_PHONE,
    PROFILE_STATUS_COMPLETE,

    ZENDESK_REMINDER_STATE_PENDING,
    ZENDESK_REMINDER_STATE_QUEUED,
    ZENDESK_REMINDER_STATE_SUCCESS,
    ZENDESK_REMINDER_STATE_FAILED,
    ZENDESK_REMINDER_STATE_CANCELLED,
    REMINDER_DAYS_BEFORE,
    SLOT_START,         # 看診起始時間（第一個）
    SLOT_END,       # 看診結束時間（最後一個）
    SLOT_INTERVAL_MINUTES,      # 每一格 slot 間隔（目前半小時）
    APPOINTMENT_DURATION_MINUTES, # 實際預約時長（要跟 Bookings duration 對齊）
    WEEKDAY_ZH,
    CLINIC_IMAGE_URL,
    CLINIC_NAME, 
    CLINIC_ADDRESS,
    CLINIC_LAT,
    CLINIC_LNG,
    WEEK_IMAGE_URL, 
    CONFIRM_NOTE_KEYWORD,
    PENDING_REGISTRATIONS,
    DEMO_CUSTOMER_NAME,
    DEMO_CUSTOMER_EMAIL,
    DEMO_CUSTOMER_PHONE
    )

# PENDING_REGISTRATIONS = {}


# DEMO 測試的
# def get_next_upcoming_appointment_for_demo():
#     """
#     取得患者「最近一筆未來的約診」。（DEMO）
#     - startDateTime > 現在
#     - 只看 Bookings 裡 customerEmailAddress == DEMO_CUSTOMER_EMAIL 的預約
#     - 如果沒有符合條件，回傳 (None, None)
#     - 如果有，回傳 (appointment_dict, local_start_dt)
#     """
#     token = get_graph_token()
#     business_id = os.environ.get("BOOKING_BUSINESS_ID")

#     if not business_id:
#         raise Exception("缺 BOOKING_BUSINESS_ID，請在終端機 export")

#     url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
#     headers = {
#         "Authorization": f"Bearer {token}"
#     }

#     resp = requests.get(url, headers=headers)
#     app.logger.info(
#         f"APPOINTMENTS (for upcoming demo) STATUS: {resp.status_code}, BODY: {resp.text}")
#     resp.raise_for_status()

#     all_appts = resp.json().get("value", [])

#     now_local = datetime.now()
#     best_appt = None
#     best_local_start = None

#     for a in all_appts:
#         # 如果 Bookings 有 isCancelled 之類的欄位，可以在這裡排除
#         if a.get("isCancelled") is True:
#             continue

#         # 只看 DEMO 患者的預約（用 email 過濾）
#         customer_email = (a.get("customerEmailAddress") or "").lower()
#         if customer_email != DEMO_CUSTOMER_EMAIL.lower():
#             continue

#         start_info = a.get("startDateTime", {})
#         local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
#         if not local_dt:
#             continue

#         # 只看未來的預約
#         if local_dt <= now_local:
#             continue

#         # 找最近的一筆（時間最早）
#         if best_local_start is None or local_dt < best_local_start:
#             best_local_start = local_dt
#             best_appt = a

#     return best_appt, best_local_start

# def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
#     """
#     將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 datetime」。
#     例如 "2025-11-20T06:00:00.0000000Z" → 2025-11-20 14:00:00 (UTC+8)
#     """
#     if not start_dt_str:
#         return None

#     try:
#         s = start_dt_str
#         if s.endswith("Z"):
#             s = s[:-1]
#         s = s.split(".")[0]
#         utc_dt = datetime.fromisoformat(s)
#     except Exception as e:
#         app.logger.error(
#             f"解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}")
#         return None

#     # 轉成台北時間（UTC+8）
#     local_dt = utc_dt + timedelta(hours=8)
#     return local_dt


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

# ======================================
#  LINE Event Handlers 區/訊息處理
# ======================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = event.message.text.strip()
    app.logger.info(f"收到使用者訊息: {text}")

    # === 0. 檢查是否處於首次建檔流程 ===
    line_user_id_for_state = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id_for_state = event.source.user_id

    # === -1. 使用者主動中斷建檔流程 ===
    if text == "取消建檔":
        if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
            del PENDING_REGISTRATIONS[line_user_id_for_state]
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="已為您取消建檔流程，謝謝。"
                    )]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="目前沒有正在進行的建檔流程。\n如需開始建檔，請輸入「測試身分」。"
                    )]
                )
            )
        return


    if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
        state = PENDING_REGISTRATIONS[line_user_id_for_state]
        step = state.get("step")

        # 0-1. 問姓名
                # 0-1. 問姓名
        if step == "ask_name":
            name = text.strip()
            if not name:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="姓名不能是空白，請再次輸入您的姓名。")]
                    )
                )
                return

            # 先把姓名寫進 Zendesk，並標記 profile_status = need_phone
            if line_user_id_for_state:
                try:
                    user = upsert_zendesk_user_basic_profile(
                        line_user_id=line_user_id_for_state,
                        name=name,
                        phone=None,
                        profile_status=PROFILE_STATUS_NEED_PHONE,
                    )
                    if not user:
                        app.logger.warning("[handle_message] 寫入 Zendesk 姓名失敗，但仍繼續問手機")
                except Exception as e:
                    app.logger.error(f"[handle_message] 更新 Zendesk user 姓名失敗: {e}")
                    # 不中斷流程，仍然繼續問手機

            state["name"] = name
            state["step"] = "ask_phone"
            PENDING_REGISTRATIONS[line_user_id_for_state] = state

            reply_text = f"{name} 您好，請輸入您的手機號碼（格式：09xxxxxxxx）："

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

                # 0-2. 問手機
        elif step == "ask_phone":
            phone_raw = text.strip()
            digits = "".join(ch for ch in phone_raw if ch.isdigit())

            if not (len(digits) == 10 and digits.startswith("09")):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="手機格式不正確，請以 09xxxxxxxx 格式重新輸入。")]
                    )
                )
                return

            name = state.get("name") or "未填姓名"

            # 寫進 Zendesk：phone + profile_status=complete
            user = None
            if line_user_id_for_state:
                try:
                    user = upsert_zendesk_user_basic_profile(
                        line_user_id=line_user_id_for_state,
                        name=name,
                        phone=digits,
                        profile_status=PROFILE_STATUS_COMPLETE,
                    )
                except Exception as e:
                    app.logger.error(f"[handle_message] 更新 Zendesk user 手機失敗: {e}")
                    user = None

            if not user:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="建立病患資料時發生問題，請稍後再試。")]
                    )
                )
                return

            # 成功 → 清除狀態
            del PENDING_REGISTRATIONS[line_user_id_for_state]

            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{digits}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

            buttons_template = ButtonsTemplate(
                title="線上預約",
                text="請選擇要預約的日期範圍：",
                thumbnail_image_url=WEEK_IMAGE_URL,
                actions=[
                    MessageAction(label="本週", text="我要預約本週"),
                    MessageAction(label="下週", text="我要預約下週"),
                    MessageAction(label="其他日期", text="其他日期"),
                ],
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=info_text),
                        TemplateMessage(alt_text="線上預約時段選擇", template=buttons_template)
                    ]
                )
            )
            return


        # 0-3. 例外 step → reset
        else:
            del PENDING_REGISTRATIONS[line_user_id_for_state]
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始流程。")]
                )
            )
            return


    # === 測試：從後端跟 Entra 拿 Graph token ===
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
                    messages=[TextMessage(text="Graph token 申請失敗，請稍後再試")]
                )
            )
        return

    # === 查詢某天預約 ===
    if text.startswith("查 "):
        parts = text.split()
        if len(parts) >= 2:
            date_str = parts[1]
            try:
                appts = list_appointments_for_date(date_str)
                reply_text = f"{date_str} 有 {len(appts)} 筆預約"
            except Exception as e:
                app.logger.error(f"查預約失敗: {e}")
                reply_text = "查預約失敗，請稍後再試"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請輸入：查 YYYY-MM-DD，例：查 2025-01-15")]
                )
            )
        return

    # === 預約 YYYY-MM-DD：顯示 Carousel ===


        # === 預約 YYYY-MM-DD：顯示 Carousel（需限制三週內＋需已建檔） ===
    elif text.startswith("預約 "):
        date_str = text.replace("預約", "").strip()

        # 取得 LINE userId
        line_user_id = None
        if event.source and hasattr(event.source, "user_id"):
            line_user_id = event.source.user_id

        # 1. 檢查是否已有 Zendesk 病患資料（避免未建檔客戶亂預約）
        if not is_registered_patient(line_user_id):
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text="目前系統尚未有您的基本資料，請先點選「線上約診」完成建檔，再進行預約喔。"
                        )
                    ],
                )
            )
            return

        # 2. 驗證日期（格式正確／三週內／非過去）
        ok, msg = validate_appointment_date(date_str)
        if not ok:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg)],
                )
            )
            return

        # 3. 通過檢查才真的去查某天的時段
        try:
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                reply_msg = TextMessage(text=f"{date_str} 沒有可預約時段")
            else:
                reply_msg = build_slots_carousel(date_str, available_slots)
        except Exception as e:
            app.logger.error(f"取得可預約時段失敗: {e}")
            reply_msg = TextMessage(text="取得可預約時段失敗，請稍後再試")

        # 回傳 Carousel 或是錯誤訊息
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[reply_msg],
            )
        )
        return

    # === ① 線上約診：先判斷 Zendesk 有沒有這個病患 ===
    elif text == "線上約診":
        # 1-1 取得 LINE userId
        line_user_id = None
        if event.source and hasattr(event.source, "user_id"):
            line_user_id = event.source.user_id

        if not line_user_id:
            # 理論上 1:1 聊天一定有 user_id，這裡只是保險用
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="找不到 LINE userId，請改用 1 對 1 聊天測試。")]
                )
            )
            return

        # 1-2 先到 Zendesk 查這個 line_user_id 是否已建檔
        try:
            count, user = search_zendesk_user_by_line_id(line_user_id)
        except Exception as e:
            app.logger.error(f"查詢 Zendesk 使用者失敗: {e}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="後端查詢病患資料發生錯誤，請稍後再試。")]
                )
            )
            return

                # 1-3 沒找到或拿不到 user → 視為新病患，啟動首次建檔流程（問姓名）
        if count == 0 or not user:
            try:
                profile = line_bot_api.get_profile(user_id=line_user_id)
                display_name = getattr(profile, "display_name", None) or "您好"
            except Exception as e:
                app.logger.error(f"取得 LINE Profile 失敗: {e}")
                display_name = "您好"

            PENDING_REGISTRATIONS[line_user_id] = {
                "step": "ask_name",
                "display_name": display_name,
            }

            reply_text = (
                f"{display_name} 您好，歡迎使用線上約診服務。\n"
                "請先完成基本資料建檔再使用本服務。\n\n"
                "請輸入您的姓名（全名）："
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

        # 1-4 找到一筆 user → 依 profile_status 決定要問什麼
        user_fields = user.get("user_fields") or {}
        profile_status = user_fields.get("profile_status")

        # 後備判斷：舊資料可能還沒有 profile_status
        if not profile_status:
            phone = user.get("phone") or ""
            name = user.get("name") or ""
            if phone:
                profile_status = PROFILE_STATUS_COMPLETE
            elif name:
                profile_status = PROFILE_STATUS_NEED_PHONE
            else:
                profile_status = PROFILE_STATUS_EMPTY

        # 1-4-1 還沒留任何資料 → 當成新病患，問姓名
        if profile_status == PROFILE_STATUS_EMPTY:
            try:
                profile = line_bot_api.get_profile(user_id=line_user_id)
                display_name = getattr(profile, "display_name", None) or "您好"
            except Exception as e:
                app.logger.error(f"取得 LINE Profile 失敗: {e}")
                display_name = "您好"

            PENDING_REGISTRATIONS[line_user_id] = {
                "step": "ask_name",
                "display_name": display_name,
            }

            reply_text = (
                f"{display_name} 您好，歡迎使用線上約診服務。\n"
                "請先完成基本資料建檔再使用本服務。\n\n"
                "請輸入您的姓名（全名）："
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

        # 1-4-2 已有姓名但缺手機 → 直接問手機
        if profile_status == PROFILE_STATUS_NEED_PHONE:
            name = user.get("name") or "貴賓"
            PENDING_REGISTRATIONS[line_user_id] = {
                "step": "ask_phone",
                "name": name,
            }

            reply_text = (
                f"{name} 您好，系統中已有您的姓名，尚未留下手機號碼。\n"
                "請先完成建檔再使用「線上預約」功能\n\n"

                "請輸入您的手機號碼（格式：09xxxxxxxx）：\n\n"

                "如需取消填寫資料，請輸入「取消建檔」"

            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

        # 1-4-3 已完整建檔 → 老病患流程（沿用你原本的 code）
        if profile_status == PROFILE_STATUS_COMPLETE:
            name = user.get("name") or "貴賓"
            phone = user.get("phone") or "（未留電話）"

            info_text = (
                f"{name} 您好，系統中已有您的資料：\n"
                f"手機：{phone}\n\n"
                "請選擇要預約的日期範圍："
            )

            buttons_template = ButtonsTemplate(
                title="線上預約",
                text="請選擇要預約的日期範圍：",
                thumbnail_image_url=WEEK_IMAGE_URL,
                actions=[
                    MessageAction(label="本週", text="我要預約本週"),
                    MessageAction(label="下週", text="我要預約下週"),
                    MessageAction(label="其他日期", text="其他日期"),
                ],
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=info_text),
                        TemplateMessage(
                            alt_text="線上預約時段選擇",
                            template=buttons_template
                        ),
                    ]
                )
            )
            return

        # 1-5 其他異常狀況（理論上不太會進來）
        warn_text = (
            f"系統偵測到此帳號的建檔資料異常，"
            "請聯繫診所人員協助處理。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=warn_text)]
            )
        )
        return


    # === 測試：用目前這個 LINE 使用者去 Zendesk 查身分 ===
    elif text == "測試身分":
        # 1. 從 event 取得 LINE userId
        line_user_id = None
        if event.source and hasattr(event.source, "user_id"):
            line_user_id = event.source.user_id

        if not line_user_id:
            # 理論上 1:1 聊天一定有 user_id，這裡只是保險
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="找不到 LINE userId，請改用 1 對 1 聊天測試。")]
                )
            )
            return

        # 2. 先到 Zendesk 查這個 line_user_id 是否已經建過檔
        try:
            count, user = search_zendesk_user_by_line_id(line_user_id)
        except Exception as e:
            app.logger.error(f"查詢 Zendesk 使用者失敗: {e}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="後端查詢病患資料發生錯誤，請稍後再試。")]
                )
            )
            return

        # 2-1. 已經是老病患 → 先簡單回覆（之後可以在這裡直接串預約）
        if count > 0 and user is not None:
            name = user.get("name") or "貴賓"
            phone = user.get("phone") or "（未留電話）"
            reply_text = (
                f"{name} 您好，系統中已有您的資料：\n"
                f"手機：{phone}\n\n"
                "之後預約將會直接使用這份資料。"
            )
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

        # 2-2. 找不到 → 視為第一次使用，需要建檔
        # 這裡多一步：呼叫 LINE profile 拿 displayName 來打招呼
        display_name = "您好"
        try:
            profile = line_bot_api.get_profile(user_id=line_user_id)
            # v3 SDK 通常是 display_name
            if profile and getattr(profile, "display_name", None):
                display_name = profile.display_name
        except Exception as e:
            app.logger.error(f"取得 LINE Profile 失敗: {e}")
            # 拿不到就維持預設「您好」

        # 3. 把狀態記在 PENDING_REGISTRATIONS 裡，進入 ask_name 流程
        PENDING_REGISTRATIONS[line_user_id] = {
            "step": "ask_name",
            "display_name": display_name,
        }

        reply_text = (
            f"{display_name} 您好，歡迎使用線上預約服務。\n"
            "請先完成基本資料建檔再使用本服務。\n\n"
            "請輸入您的姓名（全名）："
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
        return

    # === ②-1 其他日期：再提供兩週後／三週後選項 ===
    elif text == "其他日期":
        buttons_template = ButtonsTemplate(
            title="選擇其他日期",
            text="請選擇要預約的日期範圍：",
            thumbnail_image_url=WEEK_IMAGE_URL,
            actions=[
                MessageAction(label="兩週後", text="我要預約兩週後"),
                MessageAction(label="三週後", text="我要預約三週後"),
            ],
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="選擇其他日期",
                        template=buttons_template
                    )
                ]
            )
        )
        return


    # === ② 我要預約本週 ===
    elif text == "我要預約本週":
        show_dates_for_week(0, event)
        return

    # === ③ 我要預約下週 ===
    elif text == "我要預約下週":
        show_dates_for_week(1, event)
        return

    # === ③-2 我要預約兩週後 ===
    elif text == "我要預約兩週後":
        show_dates_for_week(2, event)
        return

    # === ③-3 我要預約三週後 ===
    elif text == "我要預約三週後":
        show_dates_for_week(3, event)
        return

    # === ④ 我想預約 YYYY-MM-DD HH:MM ===
    # === 我想預約 YYYY-MM-DD HH:MM（需限制三週內＋需已建檔） ===
    elif text.startswith("我想預約"):
        payload = text.replace("我想預約", "").strip()
        parts = payload.split()

        # 是否符合「YYYY-MM-DD HH:MM」格式
        if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
            date_str, time_str = parts
            display_date = date_str.replace("-", "/")

            # 取得 userId
            line_user_id = None
            if event.source and hasattr(event.source, "user_id"):
                line_user_id = event.source.user_id

            # 1. 檢查是否已有 Zendesk 病患資料（避免未建檔亂預約）
            if not is_registered_patient(line_user_id):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(
                                text="目前系統尚未有您的基本資料，請先點選「線上約診」完成建檔，再進行預約喔。"
                            )
                        ],
                    )
                )
                return

            # 2. 日期驗證（三週內／非過去）
            ok, msg = validate_appointment_date(date_str)
            if not ok:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)],
                    )
                )
                return

            # 3. 通過檢查，顯示「預約確認」按鈕（此處只是確認，不會直接預約）
            buttons_template = ButtonsTemplate(
                title="預約確認",
                text=f"您選擇的時段是：\n{display_date} {time_str}\n\n是否確認預約？",
                actions=[
                    MessageAction(label="確認預約", text=f"確認預約 {date_str} {time_str}"),
                    MessageAction(label="取消", text="取消預約流程"),
                ],
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TemplateMessage(
                            alt_text="預約確認", template=buttons_template
                        )
                    ],
                )
            )
            return

        # 格式不正確 → 直接提示
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text="請用格式：我想預約 YYYY-MM-DD HH:MM")
                    ],
                )
            )
            return
        
    # === 使用者取消預約流程（我想預約 → 預約確認 → 取消） ===
    elif text == "取消預約流程":
        buttons_template = ButtonsTemplate(
            title="已經取消約診流程",
            text="若需預約看診，請點擊「線上約診」。",
            actions=[
                MessageAction(
                    label="線上約診",
                    text="線上約診"
                ),
            ],
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="已取消預約流程",
                        template=buttons_template
                    )
                ]
            )
        )   
        return 

    # === ⑤ 確認預約 ===
    elif text.startswith("確認預約"):
        payload = text.replace("確認預約", "").strip()
        parts = payload.split()

        if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
            date_str, time_str = parts
            display_date = date_str.replace("-", "/")

            # ① 先拿 LINE userId
            line_user_id = None
            if event.source and hasattr(event.source, "user_id"):
                line_user_id = event.source.user_id

            # ② 檢查是否已在 Zendesk 建檔（防止未建檔暴力確認）
            if not is_registered_patient(line_user_id):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(
                                text="目前系統尚未有您的基本資料，請先點選「線上約診」完成建檔，再進行預約喔。"
                            )
                        ],
                    )
                )
                return

            # ③ 檢查日期是否合法（三週內／非過去）
            ok, msg = validate_appointment_date(date_str)
            if not ok:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)],
                    )
                )
                return

            # ④ 檢查該時段目前是否仍可預約（防止暴力輸入或已被別人搶走）
            if not is_slot_available(date_str, time_str):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(
                                text="很抱歉，您選擇的時段已滿或無法預約，請重新選擇其他時段。"
                            )
                        ],
                    )
                )
                return

            # ⑤ 預設先用 DEMO（避免真的炸掉）
            customer_name = DEMO_CUSTOMER_NAME
            customer_phone = DEMO_CUSTOMER_PHONE
            line_display_name = None
            # 初始化 Zendesk 客戶 ID
            zendesk_customer_id = None

            # ⑥ 如果拿得到 line_user_id，就去 Zendesk 找 user
            if line_user_id:
                try:
                    zd_count, zd_user = search_zendesk_user_by_line_id(line_user_id)
                    if zd_user:
                        # Zendesk 裡的 name / phone
                        zd_name = zd_user.get("name") or customer_name
                        zd_phone = zd_user.get("phone") or customer_phone
                        customer_name = zd_name
                        customer_phone = zd_phone
                        # 🚨 關鍵：從 Zendesk User 物件中取得 ID
                        zendesk_customer_id = zd_user.get("id")

                except Exception as e:
                    app.logger.error(f"用 line_user_id 查 Zendesk user 失敗: {e}")

                # ⑦ 再嘗試拿 LINE 顯示名稱（例如 Kevin）
                try:
                    profile = line_bot_api.get_profile(line_user_id)
                    if profile and hasattr(profile, "display_name"):
                        line_display_name = profile.display_name
                except Exception as e:
                    app.logger.error(f"取得 LINE profile 失敗: {e}")

            # ⑧ 呼叫新的 create_booking_appointment（會寫入 LINE_USER 到 serviceNotes）
            try:
                created = create_booking_appointment(
                    date_str=date_str,
                    time_str=time_str,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    # 🚨 傳入 Zendesk 客戶 ID 給 Bookings API 函式 (讓它能繼續傳給 Zendesk Ticket 函式)
                    zendesk_customer_id=zendesk_customer_id,
                    line_display_name=line_display_name,
                    line_user_id=line_user_id,
                )

                appt_id = created.get("id", "（沒有取得 ID）")

                try:
                    booking_id = created.get("id")
                    if not booking_id:
                        app.logger.error(
                            "[handle_message] Bookings 預約建立成功，但沒有取得 booking id，無法建立 Zendesk ticket"
                        )
                    elif not zendesk_customer_id:
                        app.logger.warning(
                            "[handle_message] 未取得 Zendesk User ID，跳過建立預約 Ticket 流程。"
                        )
                    else:
                        try:
                            zendesk_id_int = int(zendesk_customer_id)
                        except ValueError:
                            app.logger.error(
                                f"[handle_message] Zendesk User ID 不是整數: {zendesk_customer_id}，跳過建立 Ticket"
                            )
                        else:
                            # 用使用者剛選的本地時間組一個 datetime，當作門診時間
                           
                            local_start_dt = datetime.strptime(
                            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                            )

                            ticket_result = create_zendesk_appointment_ticket(
                                booking_id=booking_id,
                                local_start_dt=local_start_dt,
                                zendesk_customer_id=zendesk_id_int,
                                customer_name=customer_name,
                            )
                            app.logger.info(
                                f"[handle_message] 建立預約 Ticket 結果: {ticket_result}"
                            )
                except Exception as e:
                    app.logger.error(
                        f"[handle_message] 建立 Zendesk Ticket 發生錯誤（不影響病患畫面）: {e}"
                    )


                # 這裡顯示給病患看的姓名，沿用 booking_customer_name 的邏輯
                if line_display_name:
                    display_name = f"{customer_name}（{line_display_name}）"
                else:
                    display_name = customer_name

                detail_text = (
                    "已為您完成預約，請準時報到。\n"
                    f"姓名：{display_name}\n"
                    f"時段：{display_date} {time_str}"
                )

                buttons_template = ButtonsTemplate(
                    title="診所位置",
                    text="如需導航，請點選下方按鈕。",
                    actions=[
                        MessageAction(label="位置導航", text="查詢診所位置")
                    ],
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(text=detail_text),
                            TemplateMessage(
                                alt_text="診所位置導航",
                                template=buttons_template,
                            ),
                        ],
                    )
                )
                return

            except Exception as e:
                app.logger.error(f"建立 Bookings 預約失敗: {e}")
                reply_text = "未成功預約，請重新操作"

        else:
            reply_text = "格式：確認預約 YYYY-MM-DD HH:MM"

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )
        return
    
    # === 約診查詢 ===
    elif text == "約診查詢":
        return flow_query_next_appointment(event, text)

    # === ⑤-1 取消約診 ===
    elif text.startswith("取消約診"):
        return flow_cancel_request(event, text)

    elif text.startswith("確認取消"):
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")]
            )
        )
        return


    # === ⑦ 確認回診 ===
    elif text.startswith("確認回診"):
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")]
            )
        )
        return


    # === 查詢診所位置 ===
    elif text == "查詢診所位置":
        location_message = LocationMessage(
            title=CLINIC_NAME,
            address=CLINIC_ADDRESS,
            latitude=CLINIC_LAT,
            longitude=CLINIC_LNG
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[location_message])
        )
        return

    # === 診所資訊 ===
    elif text == "診所資訊":
        short_text = f"地址：{CLINIC_ADDRESS}\n點擊下方查看地圖位置"

        clinic_info_template = ButtonsTemplate(
            thumbnail_image_url=CLINIC_IMAGE_URL,
            title=CLINIC_NAME,
            text=short_text,
            actions=[MessageAction(label="查看地圖位置", text="查看地圖位置")]
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
            longitude=CLINIC_LNG
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(alt_text="診所資訊", template=clinic_info_template),
                    opening_hours_message,
                    location_message
                ]
            )
        )
        return

    # === 查看地圖位置 ===
    elif text == "查看地圖位置":
        location_message = LocationMessage(
            title=CLINIC_NAME,
            address=CLINIC_ADDRESS,
            latitude=CLINIC_LAT,
            longitude=CLINIC_LNG
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[location_message])
        )
        return

    # === 其他訊息 ===
    else:
        app.logger.info("非線上約診相關指令，請聯繫客服")

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data or ""
    app.logger.info(f"收到 Postback data: {data}")

    # ① 按下「取消約診」按鈕（從約診查詢畫面）
    if data.startswith("CANCEL_APPT:"):
        appt_id = data.split(":", 1)[1].strip()
        # 用假的 text 丟回原本的 flow，沿用同一套邏輯
        fake_text = f"取消約診 {appt_id}"
        return flow_cancel_request(event, fake_text)

    # ② 按下「確認取消」按鈕（第二階段確認）
    #    🔧 這裡同時支援舊的 CONFIRM_CANCEL: 與新的 CANCEL_CONFIRM:
    elif data.startswith("CANCEL_CONFIRM:") or data.startswith("CONFIRM_CANCEL:"):
        appt_id = data.split(":", 1)[1].strip()
        fake_text = f"確認取消 {appt_id}"
        return flow_confirm_cancel(event, fake_text)

    # ②-1 按下「保留約診」按鈕
    elif data == "CANCEL_KEEP":
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="已為您保留原本的約診，謝謝。")]
            )
        )
        return

    # ③ 按下「確認回診」按鈕
    elif data.startswith("CONFIRM_APPT:"):
        appt_id = data.split(":", 1)[1].strip()
        fake_text = f"確認回診 {appt_id}"
        return flow_confirm_visit(event, fake_text)

    # 其他沒處理到的 Postback 先記 log
    else:
        app.logger.warning(f"未處理的 Postback data: {data}")
        return

@app.route("/cron/run-reminder", methods=["GET"])
def cron_run_reminder():
    days_str = request.args.get("days")  # 例如 ?days=1
    custom_days = None
    if days_str is not None:
        try:
            custom_days = int(days_str)
        except ValueError:
            custom_days = None

    count = run_reminder_check(days_before=custom_days)
    return {"status": "ok", "processed": count}, 200




# 本機用5001，Azure則用賦予的port
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
