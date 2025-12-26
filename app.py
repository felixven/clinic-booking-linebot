import requests
import json, uuid, time

from flask import Flask, request, abort,jsonify
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

from datetime import datetime

from dotenv import load_dotenv
load_dotenv()
import os
from line_client import line_bot_api, handler


from bookings_core import (
    list_appointments_for_date,
    get_available_slots_for_date,
    create_booking_appointment,
    get_graph_token,
    extract_zd_user_id_from_service_notes
)


from zendesk_core import (
    search_zendesk_user_by_line_id,
    upsert_zendesk_user_basic_profile,
    create_zendesk_appointment_ticket,
    search_zendesk_users_by_phone,
    _build_zendesk_headers
)

from patient_core import (
    is_registered_patient,
    normalize_phone,
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
    #run_reminder_check_direct
)

from queue_core import voice_call_queue
from flows_voice_calls import process_voice_call_task

from flows_voice_scheduler import build_voice_groups_and_enqueue

from utils import (
    reply_consent_input, 
    enter_input_step,
    clear_pending_state,
    is_binding_complete
    )

from state_store import get_state, set_state,clear_state



# Demo 測試用
from voice_demo import trigger_voice_demo

from flows_voice_webhook import handle_livehub_webhook

FORCE_ZD_ID_FROM_NOTES = os.environ.get("FORCE_ZD_ID_FROM_NOTES", "0") == "1"


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
    ZENDESK_UF_LINE_USER_ID_KEY,
    ZENDESK_UF_PROFILE_STATUS_KEY,
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
    PROFILE_STATUS_NEED_NAME,
    is_valid_name,
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


def reply_date_range_buttons(event, info_text: str):
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
            ],
        )
    )



# ========= Webhook 入口 =========

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)

    # --- DEBUG TRACE (minimal) ---
    req_id = uuid.uuid4().hex[:8]
    evt_id = None
    msg_id = None
    evt_ts = None
    try:
        payload = json.loads(body)
        events = payload.get("events") or []
        if events:
            e0 = events[0]
            evt_id = e0.get("webhookEventId")
            evt_ts = e0.get("timestamp")
            msg = e0.get("message") or {}
            msg_id = msg.get("id")
    except Exception as e:
        app.logger.warning(f"[TRACE][{req_id}] json parse fail: {e}")

    app.logger.info(
        f"[TRACE][{req_id}] incoming webhook "
        f"evt_id={evt_id} msg_id={msg_id} ts={evt_ts} "
        f"len_body={len(body)}"
    )
    # --- END TRACE ---

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
    text = (event.message.text or "").strip()

    evt_id = getattr(event, "webhook_event_id", None) or getattr(event, "webhookEventId", None)
    msg_id = getattr(event.message, "id", None)
    ts = getattr(event, "timestamp", None)
    uid = None
    if event.source and hasattr(event.source, "user_id"):
        uid = event.source.user_id

    app.logger.info(
        f"[HANDLE] evt_id={evt_id} msg_id={msg_id} ts={ts} uid={uid} text={text}"
    )

    # === 0. 檢查是否處於首次建檔流程 ===
    line_user_id_for_state = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id_for_state = event.source.user_id

    # === -1. 使用者主動中斷建檔流程 ===
    if text in {"取消建檔", "取消流程", "取消"}:
        # if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
        #     del PENDING_REGISTRATIONS[line_user_id_for_state]
        #     line_bot_api.reply_message(
        #         ReplyMessageRequest(
        #             reply_token=event.reply_token,
        #             messages=[TextMessage(
        #                 text="已為您取消建檔流程，謝謝。"
        #             )]
        #         )
        #     )
        # else:
        #     line_bot_api.reply_message(
        #         ReplyMessageRequest(
        #             reply_token=event.reply_token,
        #             messages=[TextMessage(
        #                 text="目前沒有正在進行的建檔流程。\n如需開始建檔，請輸入「測試身分」。"
        #             )]
        #         )
        #     )
        # return
        cleared = clear_pending_state(line_user_id_for_state)
        app.logger.info(f"[取消建檔] uid={line_user_id_for_state} cleared={cleared}")

        if cleared:
            msg = "已為您取消建檔流程，謝謝。"
        else:
            msg = "目前沒有正在進行的流程。\n如需開始預約，請輸入「線上約診」。"

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return


    if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
        state = PENDING_REGISTRATIONS[line_user_id_for_state]
        step = state.get("step")
        # ===== 流程中保護：避免把「線上約診/取消預約...」當成姓名或手機 =====
        flow_commands = {
            "線上約診", "約診查詢", "取消預約", "取消預約流程", "查詢診所位置",
            "我要預約本週", "我要預約下週", "我要預約兩週後", "我要預約三週後", "其他日期",
        }
        if text in flow_commands:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="您目前正在填寫資料中。\n如要取消請按「取消」或輸入「取消建檔」。")]
                )
            )
            return
        
        # ===== 等待同意：使用者若直接輸入，不要 reset，提示按按鈕 =====
        if step in {"wait_consent_new_name", "wait_consent_name_after_phone", "wait_consent_phone"}:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請先按下方按鈕「好的，我要開始輸入」後再輸入喔。若要取消請輸入「取消」。")]
                )
            )
            return

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

            # 先把姓名寫進 Zendesk，同時標記 profile_status = need_phone
            if line_user_id_for_state:
                try:
                    user = upsert_zendesk_user_basic_profile(
                        line_user_id=line_user_id_for_state,
                        name=name,
                        phone=None,
                        profile_status=PROFILE_STATUS_NEED_PHONE,
                    )
                    if user and user.get("id"):
                        state["zendesk_user_id"] = user.get("id")
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
        
        # 0-1.5 問姓名（手機已經有了，補姓名用）
        elif step == "ask_name_after_phone":
            name = text.strip()
            if not is_valid_name(name):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入您的真實姓名（不可空白）。")]
                    )
                )
                return

            zendesk_user_id = state.get("zendesk_user_id")
            if not zendesk_user_id:
                # 保守：如果意外沒有 user_id，就回到問手機重新走
                state["step"] = "ask_phone"
                PENDING_REGISTRATIONS[line_user_id_for_state] = state
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="資料狀態異常，請重新輸入手機號碼（09xxxxxxxx）：")]
                    )
                )
                return

            # 更新 Zendesk：name + profile_status=complete（手機已經有了）
            base_url, headers = _build_zendesk_headers()
            url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"
            payload = {
                "user": {
                    "name": name,
                    "phone": (state.get("phone") or "").strip(),
                    "external_id": line_user_id_for_state,
                    "user_fields": {
                        ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
                        ZENDESK_UF_PROFILE_STATUS_KEY: (PROFILE_STATUS_COMPLETE if is_valid_name(name) else PROFILE_STATUS_NEED_NAME),
                    },
                }
            }

            try:
                resp = requests.put(url, headers=headers, json=payload, timeout=10)
                app.logger.info(f"[ask_name_after_phone][PUT] status={resp.status_code} body={resp.text[:300]}")
                resp.raise_for_status()
            except Exception as e:
                app.logger.error(f"[ask_name_after_phone] 更新 Zendesk 姓名失敗 user_id={zendesk_user_id}: {e}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="更新姓名時發生問題，請稍後再試。")]
                    )
                )
                return

            # 成功 → 清狀態 → 進入選日期範圍（跟你原本完成建檔一致）
            del PENDING_REGISTRATIONS[line_user_id_for_state]

            phone_display = state.get("phone") or "（已留存）"
            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{phone_display}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

            reply_date_range_buttons(event,info_text)
            
            return

        # elif step == "confirm_existing_by_phone":
        #     # 期待使用者回覆：是我本人 / 不是我
        #     if text not in {"是我本人", "不是我"}:
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="請點選按鈕確認：是我本人 / 不是我")]
        #             )
        #         )
        #         return

        #     # 取出狀態資料
        #     zendesk_user_id = state.get("zendesk_user_id")
        #     phone = state.get("phone")  # digits
        #     found_name = (state.get("found_name") or "").strip()

        #     if text == "不是我":
        #         # 回到輸入手機
        #         PENDING_REGISTRATIONS[line_user_id_for_state] = {"step": "ask_phone"}
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="了解，請重新輸入您的手機號碼（09xxxxxxxx）：")]
        #             )
        #         )
        #         return

        #     # === 是我本人：把這筆 Zendesk user 綁到此 LINE (external_id + user_fields.line_user_id) ===
        #     if not zendesk_user_id or not phone:
        #         del PENDING_REGISTRATIONS[line_user_id_for_state]
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="資料狀態異常，請重新輸入「線上約診」再試一次。")]
        #             )
        #         )
        #         return

        #     base_url, headers = _build_zendesk_headers()
        #     url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"

        #     payload = {
        #         "user": {
        #             "external_id": line_user_id_for_state,
        #             "user_fields": {
        #                 ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
        #                 # 狀態你想留就留，但放行不要靠它
        #                 ZENDESK_UF_PROFILE_STATUS_KEY: (PROFILE_STATUS_COMPLETE if is_valid_name(found_name) else PROFILE_STATUS_NEED_NAME),
        #             },
        #         }
        #     }

        #     try:
        #         resp = requests.put(url, headers=headers, json=payload, timeout=10)
        #         app.logger.info(f"[claim][PUT] status={resp.status_code} body={resp.text[:300]}")
        #         resp.raise_for_status()
        #         updated = (resp.json() or {}).get("user") or {}
        #     except Exception as e:
        #         app.logger.error(f"[claim] bind failed user_id={zendesk_user_id}: {e}")
        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="綁定資料時發生問題，請稍後再試或聯繫診所。")]
        #             )
        #         )
        #         return

        #     name_now = (updated.get("name") or "").strip()
        #     phone_now = (updated.get("phone") or phone or "").strip()

        #     # 若姓名仍是 placeholder → 要求補姓名
        #     if not is_valid_name(name_now):
        #         state["step"] = "ask_name_after_phone"
        #         state["zendesk_user_id"] = zendesk_user_id
        #         state["phone"] = phone_now

        #         PENDING_REGISTRATIONS[line_user_id_for_state] = state
        #         app.logger.info(f"[confirm_existing_by_phone] set step=ask_name_after_phone uid={line_user_id_for_state} zd_user_id={zendesk_user_id} phone={phone_now}")

        #         line_bot_api.reply_message(
        #             ReplyMessageRequest(
        #                 reply_token=event.reply_token,
        #                 messages=[TextMessage(text="手機已確認，請再輸入您的真實姓名（全名）：")]
        #             )
        #         )
        #         return

        #     # ✅ 姓名有效 → 清狀態 → 放行
        #     del PENDING_REGISTRATIONS[line_user_id_for_state]
        #     info_text = (
        #         f"{name_now} 您好，已為您完成身分綁定。\n"
        #         f"手機：{phone_now}\n\n"
        #         "請選擇要預約的日期範圍："
        #     )
        #     reply_date_range_buttons(event, info_text)
        #     return

        elif step == "confirm_name_after_claim":
            # 期待：姓名正確 / 我要修改姓名
            if text not in {"姓名正確", "我要修改姓名"}:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請點選按鈕：姓名正確 / 我要修改姓名")]
                    )
                )
                return

            zendesk_user_id = state.get("zendesk_user_id")
            phone = (state.get("phone") or "").strip()
            found_name = (state.get("found_name") or "").strip()

            if not zendesk_user_id:
                del PENDING_REGISTRATIONS[line_user_id_for_state]
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始。")]
                    )
                )
                return

            # 使用者選「我要修改姓名」→ 直接進入補姓名
            if text == "我要修改姓名":
                state["step"] = "ask_name_after_phone"
                # ask_name_after_phone 會負責把 name + phone + external_id 一次寫入 Zendesk
                PENDING_REGISTRATIONS[line_user_id_for_state] = state

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入您要更新的真實姓名（全名）：")]
                    )
                )
                return

            # 使用者選「姓名正確」→ 只做綁定（external_id / user_fields），不改名
            base_url, headers = _build_zendesk_headers()
            url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"

            payload = {
                "user": {
                    "external_id": line_user_id_for_state,
                    "user_fields": {
                        ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
                        ZENDESK_UF_PROFILE_STATUS_KEY: (PROFILE_STATUS_COMPLETE if is_valid_name(found_name) else PROFILE_STATUS_NEED_NAME),
                    },
                }
            }

            # 如果 state 有 phone，就一起補上（不然 Zendesk 有些資料會留空）
            if phone:
                payload["user"]["phone"] = phone

            try:
                resp = requests.put(url, headers=headers, json=payload, timeout=10)
                app.logger.info(f"[confirm_name_after_claim][PUT] status={resp.status_code} body={resp.text[:300]}")
                resp.raise_for_status()
            except Exception as e:
                app.logger.error(f"[confirm_name_after_claim] bind failed user_id={zendesk_user_id}: {e}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="綁定資料時發生問題，請稍後再試。")]
                    )
                )
                return

            del PENDING_REGISTRATIONS[line_user_id_for_state]

            info_text = (
                f"{found_name or '貴賓'} 您好，已為您完成身分綁定。\n"
                f"手機：{phone or '（已確認）'}\n\n"
                "請選擇要預約的日期範圍："
            )
            reply_date_range_buttons(event, info_text)
            return

        
        elif step == "ask_name_for_multi_claim":
            name = text.strip()

            candidates = state.get("candidates") or []
            phone = state.get("phone") or ""
            mode = (state.get("mode") or "").strip()

            # already_bound：不做姓名格式檢查，直接拿來比對；比對結果最後都導客服
            if mode != "already_bound":
                if not is_valid_name(name):
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="請輸入您的真實姓名（全名），以便確認資料。")]
                        )
                    )
                    return


            # 用「全等」比對（最保守，不做模糊匹配）
            matched = []
            for u in candidates:
                u_name = (u.get("name") or "").strip()
                if u_name == name:
                    matched.append(u)

            if len(matched) == 1:
                if mode == "already_bound":
                    del PENDING_REGISTRATIONS[line_user_id_for_state]
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="此手機號碼已綁定其他帳號，系統無法線上轉移綁定，請聯繫診所客服協助處理。")]
                        )
                    )
                    return
                
                found = matched[0]
                found_name = (found.get("name") or "").strip()

                # ✅ 姓名 placeholder → 直接補姓名
                if not is_valid_name(found_name):
                    PENDING_REGISTRATIONS[line_user_id_for_state] = {
                        "step": "ask_name_after_phone",
                        "zendesk_user_id": found.get("id"),
                        "phone": phone,
                        "found_name": found_name,
                    }
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="已確認您的手機，請輸入您的真實姓名（全名）：")]
                        )
                    )
                    return

                # ✅ 姓名有效 → 進入確認姓名
                PENDING_REGISTRATIONS[line_user_id_for_state] = {
                    "step": "confirm_name_after_claim",
                    "zendesk_user_id": found.get("id"),
                    "phone": phone,
                    "found_name": found_name,
                }

                buttons_template = ButtonsTemplate(
                    title="確認姓名",
                    text=f"我們找到您的資料：\n姓名：{found_name}\n手機：{phone}\n\n姓名是否正確？",
                    actions=[
                        MessageAction(label="正確", text="姓名正確"),
                        MessageAction(label="我要修改", text="我要修改姓名"),
                    ],
                )
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TemplateMessage(alt_text="確認姓名", template=buttons_template)]
                    )
                )
                return


            if len(matched) == 0:
                if mode == "already_bound":
                    del PENDING_REGISTRATIONS[line_user_id_for_state]
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="此手機號碼已綁定其他帳號，系統無法線上轉移綁定，請聯繫診所客服協助處理。")]
                        )
                    )
                    return
                
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="找不到符合此姓名的資料。請確認後重新輸入姓名，或聯繫診所協助。")]
                    )
                )
                return

            # matched > 1：同手機+同姓名仍多筆，只能擋
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="此姓名仍對應多筆資料，請聯繫診所協助確認。")]
                )
            )
            return

        # 0-2. 問手機
        elif step == "ask_phone":
            phone_raw = text.strip()
            digits = normalize_phone(phone_raw)

            if not (len(digits) == 10 and digits.startswith("09")):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="手機格式不正確，請以 09xxxxxxxx 格式重新輸入。")]
                    )
                )
                return
            # === A路線：先用手機找 Zendesk seed 老客，做認領 ===
            # 只有在「此 LINE 尚未綁定」時才做認領，避免老用戶更新資料時誤觸

            try:
                bound_count, bound_user = search_zendesk_user_by_line_id(line_user_id_for_state, retries=1)
            except Exception as e:
                app.logger.error(f"[ask_phone][claim] search by line_id failed: {e}")
                bound_user = None
            # ===== Guard：若此 LINE 已有綁定中的 user（不論 complete/need_name），不允許更換手機去認領別人 =====
            # 目的：避免「先留了一支手機（或半成品）→ 下一次輸入另一支手機」造成搶綁與資料錯亂
            if bound_user:
                ufs = bound_user.get("user_fields") or {}
                bound_phone = normalize_phone(bound_user.get("phone") or "")
                bound_profile = (ufs.get(ZENDESK_UF_PROFILE_STATUS_KEY) or "").strip()

                # ✅ 若 Zendesk 已留 phone，且使用者輸入的 digits 與既有 phone 不同 → 直接擋
                if bound_phone and bound_phone != digits:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="此帳號已綁定其他手機號碼，系統不允許線上更換。請聯繫診所客服協助處理。")]
                        )
                    )
                    return

                # ✅ 若 profile_status 不是 complete（例如 need_name）→ 直接導向補姓名（不要走認領）
                bound_name = (bound_user.get("name") or "").strip()
                if bound_profile != PROFILE_STATUS_COMPLETE or (not is_valid_name(bound_name)):
                    PENDING_REGISTRATIONS[line_user_id_for_state] = {
                        "step": "wait_consent_name_after_phone",
                        "zendesk_user_id": bound_user.get("id"),
                        "phone": (bound_phone or digits),
                    }
                    reply_consent_input(
                        line_bot_api=line_bot_api,
                        event=event,
                        title="補齊姓名",
                        text="我們已確認您的手機。為完成身分綁定，請先補上您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                        ok_data="CONSENT_NAME_AFTER_PHONE",
                        cancel_data="CANCEL_FLOW",
                    )
                    return
                
                reply_date_range_buttons(event, "已確認您的身分，請選擇要預約的日期範圍：")
                return


            if not bound_user:
                try:
                    candidates = search_zendesk_users_by_phone(digits)  # 你已經放在 zendesk_core
                except Exception as e:
                    app.logger.error(f"[ask_phone][claim] search by phone failed: {e}")
                    candidates = []

                # 只允許認領「external_id 空白」的（避免搶綁）
                unbound = []
                for u in candidates:
                    ext = (u.get("external_id") or "").strip()
                    if not ext:
                        unbound.append(u)

                if len(unbound) == 1:
                    found = unbound[0]
                    found_name = (found.get("name") or "").strip()

                    # ✅ Case 1：姓名是 placeholder → 直接補姓名（要同意開關）
                    if not is_valid_name(found_name):
                        PENDING_REGISTRATIONS[line_user_id_for_state] = {
                            "step": "wait_consent_name_after_phone",
                            "zendesk_user_id": found.get("id"),
                            "phone": digits,
                        }
                        reply_consent_input(
                            line_bot_api=line_bot_api,
                            event=event,
                            title="補齊姓名",
                            text="已找到您的資料（手機已確認）。\n為完成身分綁定，請補上您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                            ok_data="CONSENT_NAME_AFTER_PHONE",
                            cancel_data="CANCEL_FLOW",
                        )
                        return


                    # ✅ Case 2：姓名有效 → 進入「確認姓名是否正確」的按鈕
                    PENDING_REGISTRATIONS[line_user_id_for_state] = {
                        "step": "confirm_name_after_claim",
                        "zendesk_user_id": found.get("id"),
                        "phone": digits,
                        "found_name": found_name,
                    }

                    buttons_template = ButtonsTemplate(
                        title="確認姓名",
                        text=f"我們找到您的資料：\n姓名：{found_name}\n手機：{digits}\n\n姓名是否正確？",
                        actions=[
                            MessageAction(label="正確", text="姓名正確"),
                            MessageAction(label="我要修改", text="我要修改姓名"),
                        ],
                    )
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TemplateMessage(alt_text="確認姓名", template=buttons_template)]
                        )
                    )
                    return


                if len(unbound) > 1:
                    # 進入「多筆資料 → 輸入姓名縮小範圍」
                    PENDING_REGISTRATIONS[line_user_id_for_state] = {
                        "step": "ask_name_for_multi_claim",
                        "phone": digits,
                        "candidates": [
                            {"id": u.get("id"), "name": u.get("name") or ""}
                            for u in unbound
                        ],
                    }
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="此手機號碼對應多筆資料，請輸入您的姓名（全名）以確認身分：")]
                        )
                    )
                    return

                # candidates 有資料但都已綁 external_id（代表被別人認領過）
                # candidates 有資料但都已綁 external_id
                 # candidates 有資料但都已綁 external_id（可能是本人舊綁定，也可能被別人綁走）
                if candidates and len(unbound) == 0:
                    # 先看是不是「已綁到自己」：是的話直接放行（不用再比對姓名）
                    mine = []
                    for u in candidates:
                        ext = (u.get("external_id") or "").strip()
                        if ext and ext == line_user_id_for_state:
                            mine.append(u)

                    if len(mine) == 1:
                        found = mine[0]
                        found_name = (found.get("name") or "").strip()
                        found_phone = normalize_phone(found.get("phone") or digits)

                        # 姓名不完整 → 走補姓名（之後會寫回並綁定）
                        if not is_valid_name(found_name):
                            PENDING_REGISTRATIONS[line_user_id_for_state] = {
                                "step": "ask_name_after_phone",
                                "zendesk_user_id": found.get("id"),
                                "phone": found_phone,
                                "found_name": found_name,
                            }
                            line_bot_api.reply_message(
                                ReplyMessageRequest(
                                    reply_token=event.reply_token,
                                    messages=[TextMessage(text="已確認您的手機，請輸入您的真實姓名（全名）：")]
                                )
                            )
                            return

                        # 姓名完整 → 直接放行預約
                        reply_date_range_buttons(event, f"{found_name} 您好，\n請選擇要預約的日期範圍：")
                        return

                    # 不是綁到自己（或多筆混雜）→ 依規格：先輸入姓名比對，失敗才叫客服
                    PENDING_REGISTRATIONS[line_user_id_for_state] = {
                        "step": "ask_name_for_multi_claim",
                        "phone": digits,
                        "candidates": [{"id": u.get("id"), "name": u.get("name") or "", "external_id": (u.get("external_id") or "")} for u in candidates],
                        "mode": "already_bound",  # 用來讓後續分支知道這是「已綁走」情境
                    }
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="此手機號碼已有資料。為了確認身分，請輸入您的姓名（全名）：")]
                        )
                    )
                    return
                

            # === 若沒有找到可認領的 seed 老客,才進入原本的新朋友流程 ===
            name = state.get("name") or "未填姓名"
            profile_status_value = PROFILE_STATUS_COMPLETE if is_valid_name(name) else PROFILE_STATUS_NEED_NAME

            # 寫進 Zendesk：phone + profile_status=complete
            user = None
            zendesk_user_id = state.get("zendesk_user_id")

            # 優先：直接更新剛剛那一筆（不靠 search）
            if line_user_id_for_state and zendesk_user_id:
                base_url, headers = _build_zendesk_headers()
                app.logger.info(f"[ask_phone] will update zendesk_user_id={zendesk_user_id} line_user_id={line_user_id_for_state}")
                url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"

                payload = {
                    "user": {
                        "name": name,
                        "phone": digits,
                        "external_id": line_user_id_for_state,
                        "user_fields": {
                            ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
                            ZENDESK_UF_PROFILE_STATUS_KEY: profile_status_value,
                        },
                    }
                }

                try:
                    resp = requests.put(url, headers=headers, json=payload, timeout=10)
                    app.logger.info(f"[ask_phone][PUT] status={resp.status_code} body={resp.text[:300]}")
                    resp.raise_for_status()
                    user = (resp.json() or {}).get("user")
                    app.logger.info(f"[ask_phone] 更新 Zendesk user_id={zendesk_user_id} 成功")
                except Exception as e:
                    app.logger.error(f"[ask_phone] 更新 Zendesk user_id={zendesk_user_id} 失敗: {e}")
                    user = None

            # 保險：真的失敗才退回 upsert
            if not user and line_user_id_for_state:
                try:
                    user = upsert_zendesk_user_basic_profile(
                        line_user_id=line_user_id_for_state,
                        name=name,
                        phone=digits,
                        profile_status=profile_status_value,
                        # profile_status=PROFILE_STATUS_COMPLETE,
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
            # ✅ 不看 flow：只要姓名無效（含 未填姓名）→ 補姓名（要同意開關）
            if not is_valid_name(name):
                state["zendesk_user_id"] = user.get("id") or state.get("zendesk_user_id")
                state["phone"] = digits
                state["step"] = "wait_consent_name_after_phone"
                PENDING_REGISTRATIONS[line_user_id_for_state] = state

                reply_consent_input(
                    line_bot_api=line_bot_api,
                    event=event,
                    title="補齊姓名",
                    text="手機已確認。\n為完成身分綁定，請補上您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                    ok_data="CONSENT_NAME_AFTER_PHONE",
                    cancel_data="CANCEL_FLOW",
                )
                return


            # 姓名有效 → 清狀態 → 放行選日期範圍
            del PENDING_REGISTRATIONS[line_user_id_for_state]

            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{digits}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

            reply_date_range_buttons(event, info_text)
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

    # === 預約 YYYY-MM-DD：顯示 Carousel（限制三週內＋需已建檔）
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
            count, user = search_zendesk_user_by_line_id(line_user_id, retries=1)
        except Exception as e:
            app.logger.error(f"查詢 Zendesk 使用者失敗: {e}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="後端查詢病患資料發生錯誤，請稍後再試。")]
                )
            )
            return

        app.logger.info(
        f"[線上約診][debug] line_user_id={line_user_id} count={count} "
        f"user_none={user is None} user_id={(user or {}).get('id')} "
        f"uf_line={((user or {}).get('user_fields') or {}).get(ZENDESK_UF_LINE_USER_ID_KEY)} "
        f"profile_status={((user or {}).get('user_fields') or {}).get(ZENDESK_UF_PROFILE_STATUS_KEY)} "
        f"name={(user or {}).get('name')} phone={(user or {}).get('phone')}"
        )

        # 1-3 沒找到或拿不到 user → 視為新病患，啟動首次建檔流程（問姓名）

        # === 規格：已綁定完成者 → 直接放行；其他一律先要電話 ===
        # === 規格：已綁定完成者 → 直接放行；若已確認手機但缺姓名 → 直接補姓名；其餘才走電話 consent ===

        if user:
            user_fields = user.get("user_fields") or {}
            phone_raw = (user.get("phone") or "").strip()
            phone_digits = normalize_phone(phone_raw)
            name = (user.get("name") or "").strip()
            profile_status = user_fields.get(ZENDESK_UF_PROFILE_STATUS_KEY)

            phone_ok = (len(phone_digits) == 10 and phone_digits.startswith("09"))
            name_ok = is_valid_name(name)

            # ✅ Case 1：已經有 phone（已確認）但 name 需要補（need_name / placeholder）
            if phone_ok and (not name_ok or profile_status == PROFILE_STATUS_NEED_NAME):
                PENDING_REGISTRATIONS[line_user_id] = {
                    "step": "ask_name_after_phone",
                    "zendesk_user_id": user.get("id"),
                    "phone": phone_digits,
                }
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="系統中已有您的資料（手機已確認），請輸入您的真實姓名（全名）：")]
                    )
                )
                return

            # ✅ Case 2：已綁定完成者 → 直接放行
            if is_binding_complete(user, line_user_id):
                info_text = (
                    f"{name or '貴賓'} 您好，系統中已有您的資料：\n"
                    f"手機：{phone_raw or '（已留存）'}\n\n"
                    "請選擇要預約的日期範圍："
                )
                reply_date_range_buttons(event, info_text)
                return

        # ✅ Case 3：其餘（查不到、沒有 phone、未綁、等等）→ 才走 consent → ask_phone
        PENDING_REGISTRATIONS[line_user_id] = {"step": "wait_consent_phone"}

        reply_consent_input(
            line_bot_api=line_bot_api,
            event=event,
            title="線上預約",
            text=(
                "第一次使用線上約診，請先輸入您的手機號碼以查詢身分。\n"
                "按下「好的，我要開始輸入」後再輸入手機。"
            ),
            ok_data="CONSENT_PHONE",
            cancel_data="CANCEL_FLOW",
        )
        return



        # # ❗其他全部情況：一律先問手機
        # PENDING_REGISTRATIONS[line_user_id] = {
        #     "step": "ask_phone",
        # }

        # reply_text = (
        #     "為了確認您的身分，請先輸入手機號碼（格式：09xxxxxxxx）：\n\n"
        #     "如需取消，請輸入「取消建檔」"
        # )

        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token,
        #         messages=[TextMessage(text=reply_text)]
        #     )
        # )
        # return
        # ❗其他全部情況：先送「同意輸入手機」按鈕（不直接打開 ask_phone）
        # === 新朋友：Zendesk 查不到這個 external_id ===
        # reply_consent_input(
        #     line_bot_api=line_bot_api,
        #     event=event,
        #     title="建立個人資料",
        #     text=(
        #         f"{display_name} 您好，歡迎使用線上預約服務。\n"
        #         "接下來需要您輸入姓名與手機以完成建檔。\n"
        #         "按下「好的，我要開始輸入」後再輸入姓名。"
        #     ),
        #     ok_data="CONSENT_NEW_NAME",
        #     cancel_data="CANCEL_FLOW",
        # )
        # return





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
                        # 關鍵：從 Zendesk User 物件中取得 ID
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
                    # 傳入 Zendesk 客戶 ID 給 Bookings API 函式 (讓它能繼續傳給 Zendesk Ticket 函式)
                    zendesk_customer_id=zendesk_customer_id,
                    line_display_name=line_display_name,
                    line_user_id=line_user_id,
                )
                appt_id = created.get("id", "（沒有取得 ID）")
                # ===== DEBUG：強制走 notes 兜底（只在本機測試用）=====
                if FORCE_ZD_ID_FROM_NOTES:
                    app.logger.info("[debug] FORCE_ZD_ID_FROM_NOTES=1 -> ignore zendesk_customer_id and recover from notes")
                    zendesk_customer_id = None


                try:
                        booking_id = created.get("id")
                        if not booking_id:
                            app.logger.error(
                                "[handle_message] Bookings 預約建立成功，但沒有取得 booking id，無法建立 Zendesk ticket"
                            )
                        else:
                            # 如果當下 zendesk_customer_id 沒拿到，就從 serviceNotes 抽 [ZD_USER]
                            zid = None
                            zid_source=None
                            if zendesk_customer_id:
                                try:
                                    zid = int(zendesk_customer_id)
                                    zid_source = "param"
                                except ValueError:
                                    app.logger.error(
                                        f"[handle_message] Zendesk User ID 不是整數: {zendesk_customer_id}，改用 serviceNotes 取Zendesk User ID"
                                    )
                                    zid = None
                                    zid_source=None

                            #2) 再用 serviceNotes recover
                            if not zid:
                                recovered = extract_zd_user_id_from_service_notes(created.get("serviceNotes"))
                                if recovered:
                                    zid = recovered
                                    zid_source = "notes"
                                    app.logger.info(f"[handle_message] 從 serviceNotes 取得 Zendesk User ID: {zid}")

                            # 3) 決定要不要建票
                            if not zid:
                                app.logger.warning(
                                    "[handle_message] 未取得 Zendesk User ID（含 serviceNotes），跳過建立預約 Ticket 流程。"
                                )
                            else:
                                # 用使用者剛選的本地時間組一個 datetime，當作門診時間
                                app.logger.info(f"[ticket][zid] source={zid_source} zid={zid} booking_id={booking_id}")
                                local_start_dt = datetime.strptime(
                                    f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                                )

                                ticket_result = create_zendesk_appointment_ticket(
                                    booking_id=booking_id,
                                    local_start_dt=local_start_dt,
                                    zendesk_customer_id=zid,
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

    # === fallback：使用者直接輸入手機，但尚未進入任何流程 ===
    if uid:
        digits = normalize_phone(text)
        if len(digits) == 10 and digits.startswith("09") and uid not in PENDING_REGISTRATIONS:
            app.logger.info(f"[fallback-phone] uid={uid} digits={digits}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請先點選「線上約診」，並按下「好的，我要開始輸入」後再輸入手機喔。")],
                )
            )
            return

    # === 其他訊息（最後 default 回覆） ===
    app.logger.info("非線上約診相關指令，請聯繫客服")
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="我目前只支援線上約診流程喔～請輸入「線上約診」開始。")],
        )
    )
    return




@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data or ""
    line_user_id = getattr(event.source, "user_id", None)

    app.logger.info(f"[POSTBACK] uid={line_user_id} data={data}")

    # ===== 新增：全域取消（任何時候都能取消）=====
    if data == "CANCEL_FLOW":
        cleared = clear_pending_state(line_user_id)
        app.logger.info(f"[CANCEL_FLOW] uid={line_user_id} cleared={cleared}")
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="已為您取消流程。需要預約請再輸入「線上約診」。")]
            )
        )
        return

    # ===== 新增：同意開始輸入手機 =====
    if data == "CONSENT_PHONE":
        if not line_user_id:
            return
        enter_input_step(
            line_bot_api=line_bot_api,
            pending_dict=PENDING_REGISTRATIONS,
            event=event,
            line_user_id=line_user_id,
            step="ask_phone",
            prompt_text="好的，請輸入您的手機號碼（09xxxxxxxx）：",
        )
        return
    
    if data == "CONSENT_NAME_AFTER_PHONE":
        if not line_user_id:
            return

        state = get_state(line_user_id)
        step = state.get("step")

        # 允許兩種狀態：
        # 1) wait_consent_name_after_phone：按同意後才開始輸入
        # 2) ask_name_after_phone：代表已經進入輸入狀態了（就不要再改 state，只提示他輸入）
        if step not in {"wait_consent_name_after_phone", "ask_name_after_phone"}:
            app.logger.warning(
                f"[CONSENT_NAME_AFTER_PHONE] bad_state uid={line_user_id} state={state}"
            )
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="流程狀態異常，請重新輸入「線上約診」。")]
                )
            )
            return

        # 已經在 ask_name_after_phone，就只提醒輸入姓名，不要重設 state
        if step == "ask_name_after_phone":
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="好的，請直接輸入您的真實姓名（全名）：")]
                )
            )
            return

        # step == wait_consent_name_after_phone → 正常進入輸入
        zendesk_user_id = state.get("zendesk_user_id")
        phone = state.get("phone")

        enter_input_step(
            line_bot_api=line_bot_api,
            pending_dict=PENDING_REGISTRATIONS,
            event=event,
            line_user_id=line_user_id,
            step="ask_name_after_phone",
            prompt_text="好的，請輸入您的真實姓名（全名）：",
            extra_state={
                "zendesk_user_id": zendesk_user_id,
                "phone": phone,
            },
        )
        return
    
    if data == "CONSENT_NAME_AFTER_PHONE":
        if not line_user_id:
            return

        state = PENDING_REGISTRATIONS.get(line_user_id) or {}

        # ✅ fallback：state 不見也能重建（避免 bad_state）
        if not state:
            try:
                count, user = search_zendesk_user_by_line_id(line_user_id, retries=1)
            except Exception as e:
                app.logger.error(f"[CONSENT_NAME_AFTER_PHONE][fallback] search failed uid={line_user_id} err={e}")
                user = None

            if user:
                state = {
                    "step": "wait_consent_name_after_phone",  # 讓你下面流程吃得到
                    "zendesk_user_id": user.get("id"),
                    "phone": normalize_phone(user.get("phone") or ""),
                }
                PENDING_REGISTRATIONS[line_user_id] = state
                app.logger.info(
                    f"[CONSENT_NAME_AFTER_PHONE][fallback] rebuilt uid={line_user_id} "
                    f"user_id={state.get('zendesk_user_id')} phone={state.get('phone')}"
                )
            else:
                app.logger.warning(f"[CONSENT_NAME_AFTER_PHONE] fallback_not_found uid={line_user_id}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="找不到您的資料，請重新輸入「線上約診」。")]
                    )
                )
                return

        step = state.get("step")

        if step not in {"wait_consent_name_after_phone", "ask_name_after_phone"}:
            app.logger.warning(f"[CONSENT_NAME_AFTER_PHONE] bad_state uid={line_user_id} state={state}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="流程狀態異常，請重新輸入「線上約診」。")]
                )
            )
            return

        if step == "ask_name_after_phone":
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="好的，請直接輸入您的真實姓名（全名）：")]
                )
            )
            return

        zendesk_user_id = state.get("zendesk_user_id")
        phone = state.get("phone")

        enter_input_step(
            line_bot_api=line_bot_api,
            pending_dict=PENDING_REGISTRATIONS,
            event=event,
            line_user_id=line_user_id,
            step="ask_name_after_phone",
            prompt_text="好的，請輸入您的真實姓名（全名）：",
            extra_state={"zendesk_user_id": zendesk_user_id, "phone": phone},
        )
        return




    # ===== 約診 postback 邏輯 =====

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

    # === fallback：使用者直接輸入手機，但尚未進入任何流程 ===



@app.route("/cron/run-reminder", methods=["GET"])
def cron_run_reminder():
    days_str = request.args.get("days")  # Ex: "?days=1"
    custom_days = None
    if days_str is not None:
        try:
            custom_days = int(days_str)
        except ValueError:
            custom_days = None

    count = run_reminder_check(days_before=custom_days)
    return {"status": "ok", "processed": count}, 200


@app.route("/demo/voice-call")
def demo_voice_call():
    phone = "0988000000"
    name = "王小明"

    trigger_voice_demo(phone, name)

    return "Voice demo triggered.", 200


@app.route("/demo/enqueue-voice-call")
def demo_enqueue_voice_call():
    """
    Demo：手動丟一筆「外撥任務」進 Redis queue，交給 worker_voice.py 處理。

    目前 task 裡的資料是寫死的，
    未來正式版會從 Booking / Zendesk 撈資料動態塞進去。
    """
    task = {
        "phone": "0903891615",         # 測試時可以改成你自己的手機
        "patient_name": "王小明",
        "appointments": [
            {
                "booking_id": "DEMO-BOOKING-ID-123",
                "local_time": "2025-12-20 09:30",
                "service_name": "一般門診",
            }
        ],
        "zendesk_ticket_id": 1053,
        "line_user_id": "Uxxxxxxxx",
        "reminder_type": "D2",
    }

    job = voice_call_queue.enqueue(
        process_voice_call_task,
        task,
    )

    app.logger.info(f"[VOICE DEMO] 已 enqueue 外撥 job_id={job.id}, task={task}")

    return {
        "status": "queued",
        "job_id": job.id,
        "task": task,
    }, 200



@app.route("/demo/enqueue-voice-call-group")
def demo_enqueue_voice_call_group():
    # Demo：同一人同一天的多張票，只打一通
    line_user_id = "Uxxxx"
    appt_date_str = "2025-12-17"
    ticket_ids = [1123,1124]   # 先放你測試用的

    job = voice_call_queue.enqueue(
        "flows_voice_calls.process_voice_call_group",
        line_user_id,
        appt_date_str,
        ticket_ids
    )
    return {
        "job_id": job.id,
        "status": job.get_status(),
        "line_user_id": line_user_id,
        "appt_date": appt_date_str,
        "ticket_ids": ticket_ids
    }, 200


#外撥回寫
@app.route("/webhook/livehub", methods=["POST"])
def webhook_livehub():
    data = request.get_json(silent=True) or {}
    app.logger.info(f"[livehub_webhook] received: {data}")

    try:
        handle_livehub_webhook(data)
    except Exception as e:
        app.logger.error(f"[livehub_webhook] handle failed: {e}")

    return jsonify({"status": "ok"}), 200

#demo zendesk串到copilot
@app.route("/demo/enqueue-voice-from-zendesk")
def demo_enqueue_voice_from_zendesk():
    # 例：/demo/enqueue-voice-from-zendesk?ticketIds=1123,1124
    ticket_ids_str = request.args.get("ticketIds", "")
    ticket_ids = []
    for x in ticket_ids_str.split(","):
        x = x.strip()
        if x.isdigit():
            ticket_ids.append(int(x))

    if not ticket_ids:
        return {"error": "missing ticketIds, e.g. ?ticketIds=1123,1124"}, 400

    # 這裡先假設同一人同一天（你目前設計就是 group）
    line_user_id = "U_demo"  # 先不靠 line_user_id 也沒關係
    appt_date_str = "unknown"  # 先讓 worker 從 ticket 取日期

    job = voice_call_queue.enqueue(
        "flows_voice_calls.process_voice_call_demo_from_zendesk",
        line_user_id,
        appt_date_str,
        ticket_ids
    )

    return {"status": "queued", "job_id": job.id, "ticket_ids": ticket_ids}, 200



@app.route("/cron/run-voice-reminder", methods=["GET"])
def cron_run_voice_reminder():
    # 預設一天前 D1
    days = int(request.args.get("days", "1"))
    result = build_voice_groups_and_enqueue(days=days)
    return result, 200





# 本機用5001，Azure則用賦予的port
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
