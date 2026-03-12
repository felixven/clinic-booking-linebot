import requests
import json, uuid, time
import sys
import threading


from flask import Flask, request, abort, jsonify
from flask_app import app
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError

from linebot.v3.messaging import (
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    MessageAction,
    LocationMessage,
    QuickReply,
    QuickReplyItem,
)

from linebot.v3.messaging.models import (
    ButtonsTemplate,
    PostbackAction,
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
    extract_zd_user_id_from_service_notes,
    get_available_acu_slots_for_date,
    is_acu_slot_available,
    has_existing_clinic_period_booking,
    get_available_clinic_slots_for_session
    
)


from zendesk_core import (
    search_zendesk_user_by_line_id,
    upsert_zendesk_user_basic_profile,
    create_zendesk_appointment_ticket,
    search_zendesk_users_by_phone,
    _build_zendesk_headers,
    run_fail_queued_tickets,
    check_acupuncture_eligibility_from_zendesk
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
    build_clinic_session_buttons,
    pick_first_available_clinic_time
)

from flows_reminders import (
    run_reminder_check,
    # run_reminder_check_direct
)

from queue_core import voice_call_queue
from flows_voice_calls import process_voice_call_task

from flows_voice_scheduler import build_voice_groups_and_enqueue

from utils import (
    reply_consent_input,
    enter_input_step,
    clear_pending_state,
    is_binding_complete,
    reply_acu_terms_buttons
)

from state_store import get_state, set_state, clear_state, acquire_lock

from dedupe_store import check_and_mark_webhook

from line_send import send_line


# Demo 測試用
from voice_demo import trigger_voice_demo

from flows_voice_webhook import handle_livehub_webhook


FORCE_ZD_ID_FROM_NOTES = os.environ.get("FORCE_ZD_ID_FROM_NOTES", "0") == "1"

app = Flask(__name__)


@app.route("/line-booking", methods=["GET"])
def health_check():
    return "OK", 200


from config import (
    ZENDESK_UF_LINE_USER_ID_KEY,
    ZENDESK_UF_PROFILE_STATUS_KEY,
    PROFILE_STATUS_NEED_PHONE,
    PROFILE_STATUS_COMPLETE,
    PROFILE_STATUS_NEED_NAME,
    is_valid_name,
    CLINIC_IMAGE_URL,
    CLINIC_NAME,
    CLINIC_ADDRESS,
    CLINIC_LAT,
    CLINIC_LNG,
    WEEK_IMAGE_URL,
    PENDING_REGISTRATIONS,
    DEMO_CUSTOMER_NAME,
    DEMO_CUSTOMER_PHONE,
    ACU_SLOTS,
    BOOKINGS_SERVICE_ACU_BED1_ID,
    BOOKINGS_SERVICE_ACU_BED2_ID,
    BOOKINGS_BUSINESS_CLINIC_ID,
    BOOKINGS_BUSINESS_ACU_ID,
    BOOKINGS_DEMO_SERVICE_ID,
    BOOKINGS_BUSINESS_ACU_BED1_ID,
    BOOKINGS_BUSINESS_ACU_BED2_ID,
    BOOKINGS_SERVICE_ACU_BED_ID,
    ACU_STAFF_BED1_ID,
    ACU_STAFF_BED2_ID,
    ZENDESK_APPT_CATEGORY_ACU_BED1,
    ZENDESK_APPT_CATEGORY_ACU_BED2,
    ZENDESK_APPT_CATEGORY_CLINIC
)


from line_send import send_line, start_loading_animation

def reply_booking_type_buttons(event, info_text: str):
    buttons_template = ButtonsTemplate(
        title="線上約診",
        text="請選擇要預約的項目：",
        thumbnail_image_url=WEEK_IMAGE_URL,
        actions=[
            MessageAction(label="預約看診", text="預約看診"),
            MessageAction(label="預約針灸", text="預約針灸"),
        ],
    )

    send_line(
        line_bot_api,
        event,
        messages=[
            TextMessage(text=info_text),
            TemplateMessage(alt_text="預約項目選擇", template=buttons_template),
        ],
        label="reply_booking_type_buttons",
        timeout=(3, 12),
        push_timeout=(3, 10),
    )

# 按照state更改title
def reply_date_range_buttons(event, info_text: str):
    # 取得 line_user_id
    line_user_id = getattr(getattr(event, "source", None), "user_id", None)

    # 從 state 拿 booking_type（預設 clinic）
    state = get_state(line_user_id) or {}
    booking_type = (state.get("booking_type") or "clinic").strip()

    # 動態標題
    if booking_type == "acupuncture":
        title = "預約針灸"
        alt_text = "針灸預約日期範圍選擇"
        template_text = "請選擇要預約針灸的日期範圍："
    else:
        title = "預約內科門診"
        alt_text = "內科門診預約日期範圍選擇"
        template_text = "請選擇要預約門診的日期範圍："

    buttons_template = ButtonsTemplate(
        title=title,
        text=template_text,  #動態顯示預約項目
        thumbnail_image_url=WEEK_IMAGE_URL,
        actions=[
            MessageAction(label="本週", text="我要預約本週"),
            MessageAction(label="下週", text="我要預約下週"),
            MessageAction(label="其他日期", text="其他日期"),
        ],
    )

    send_line(
        line_bot_api,
        event,
        messages=[
            TextMessage(text=info_text),
            TemplateMessage(alt_text=alt_text, template=buttons_template),  
        ],
        label="reply_date_range_buttons",
        timeout=(3, 12),
        push_timeout=(3, 10),
    )


# 上一版未按照state更改title的版本
# def reply_date_range_buttons(event, info_text: str):
#     buttons_template = ButtonsTemplate(
#         title="線上約診",
#         text="請選擇要預約的日期範圍：",
#         thumbnail_image_url=WEEK_IMAGE_URL,
#         actions=[
#             MessageAction(label="本週", text="我要預約本週"),
#             MessageAction(label="下週", text="我要預約下週"),
#             MessageAction(label="其他日期", text="其他日期"),
#         ],
#     )

#     send_line(
#         line_bot_api,
#         event,
#         messages=[
#             TextMessage(text=info_text),
#             TemplateMessage(alt_text="線上預約時段選擇", template=buttons_template),
#         ],
#         label="reply_date_range_buttons",
#         timeout=(3, 12),  # reply 最多等 12 秒就認定怪
#         push_timeout=(3, 10),
#         # timeout 不傳 -> 用 line_send.py 預設 (3,30)
#     )

def reply_confirm_appt_buttons(*, event, date_str: str, time_str: str):
    display_date = date_str.replace("-", "/")
    title = "確認預約"
    text = f"請確認是否預約：\n{display_date} {time_str}"

    buttons = ButtonsTemplate(
        title=title,
        text=text,
        actions=[
            MessageAction(label="確認預約", text=f"確認預約 {date_str} {time_str}"),
            MessageAction(label="重新選日期", text="我要預約本週"),
        ],
    )

    send_line(
        line_bot_api,
        event,
        messages=[TemplateMessage(alt_text="確認預約", template=buttons)],
        label="clinic_confirm_buttons",
    )



# def reply_date_range_buttons(event, info_text: str):
#     buttons_template = ButtonsTemplate(
#         title="線上約診",
#         text="請選擇要預約的日期範圍：",
#         thumbnail_image_url=WEEK_IMAGE_URL,
#         actions=[
#             MessageAction(label="本週", text="我要預約本週"),
#             MessageAction(label="下週", text="我要預約下週"),
#             MessageAction(label="其他日期", text="其他日期"),
#         ],
#     )

#     line_bot_api.reply_message(
#         ReplyMessageRequest(
#             reply_token=event.reply_token,
#             messages=[
#                 TextMessage(text=info_text),
#                 TemplateMessage(
#                     alt_text="線上預約時段選擇",
#                     template=buttons_template
#                 ),
#             ],
#         )
#     )

def _bg_handle_line_webhook(*, body: str, signature: str, req_id: str):
    """
    背景處理 handler.handle，但補齊 Flask 的 app/request context，
    避免 Working outside of application context / request context。
    """
    t0 = time.time()

    # 先在背景入口就把 evt_id/msg_id/ts 印出來（避免還沒進 handler 就炸掉還看不到）
    evt_id = None
    msg_id = None
    evt_ts = None
    try:
        payload = json.loads(body or "{}")
        events = payload.get("events") or []
        if events:
            e0 = events[0]
            evt_id = e0.get("webhookEventId")
            evt_ts = e0.get("timestamp")
            msg = e0.get("message") or {}
            msg_id = msg.get("id")
    except Exception as e:
        print(
            f"[CB_BG_TRACE_PARSE_FAIL][{req_id}] err={repr(e)}",
            file=sys.stderr,
            flush=True,
        )

    print(
        f"[CB_BG_START][{req_id}] evt_id={evt_id} msg_id={msg_id} ts={evt_ts} body_len={len(body or '')}",
        file=sys.stderr,
        flush=True,
    )

    try:
        with app.app_context():
            with app.test_request_context(
                "/callback",
                method="POST",
                data=body,
                headers={"X-Line-Signature": signature},
            ):
                try:
                    handler.handle(body, signature)
                except InvalidSignatureError:
                    print(f"[CB_BAD_SIG][{req_id}]", file=sys.stderr, flush=True)
                except Exception as e:
                    print(
                        f"[CB_HANDLE_ERROR][{req_id}] evt_id={evt_id} msg_id={msg_id} err={repr(e)}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        f"[CB_AFTER_HANDLE][{req_id}] evt_id={evt_id} msg_id={msg_id}",
                        file=sys.stderr,
                        flush=True,
                    )

    except Exception as e:
        # 如果連 context 都炸了，至少要看到
        print(
            f"[CB_BG_CRASH][{req_id}] evt_id={evt_id} msg_id={msg_id} err={repr(e)}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        print(
            f"[CB_BG_DONE][{req_id}] evt_id={evt_id} msg_id={msg_id} elapsed={time.time()-t0:.3f}s",
            file=sys.stderr,
            flush=True,
        )





# ========= Webhook 入口 =========

@app.route("/callback", methods=["POST"])
def callback():

    t0 = time.time()
    print(f"[CB_HIT] ts={t0}", file=sys.stderr, flush=True)

    # signature = request.headers["X-Line-Signature"]
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        print("[CB_ERR] Missing X-Line-Signature", file=sys.stderr, flush=True)
        return "Missing X-Line-Signature", 400

    body = request.get_data(as_text=True) or ""

    print(
        f"[CB_GOT] sig={bool(signature)} body_len={len(body)}",
        file=sys.stderr,
        flush=True,
    )

    # --- Debug Trace(最小追蹤變數) ---
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

    print(
        f"[CB_TRACE][{req_id}] evt_id={evt_id} msg_id={msg_id} ts={evt_ts}",
        file=sys.stderr,
        flush=True,
    )

    app.logger.info(
        f"[TRACE][{req_id}] incoming webhook "
        f"evt_id={evt_id} msg_id={msg_id} ts={evt_ts} "
        f"len_body={len(body)}"
    )
    # --- END TRACE ---

    # ===== Webhook 入口去重（handler.handle 之前）=====
    decision = check_and_mark_webhook(
        evt_id=evt_id,
        msg_id=msg_id,
        evt_ts=evt_ts,
        body=body,
        ttl_sec=6 * 60 * 60,  # 6小時
    )

    # 4) dedupe 結果用 print（這個最關鍵）
    if decision is False:
        # print(f"[DEDUPE_HIT][{req_id}] evt_id={evt_id} msg_id={msg_id}", flush=True)
        print(
            f"[DEDUPE_HIT][{req_id}] evt_id={evt_id} msg_id={msg_id}",
            file=sys.stderr,
            flush=True,
        )
        return "OK"
    elif decision is None:
        # print(
        #     f"[DEDUPE_FAIL_OPEN][{req_id}] evt_id={evt_id} msg_id={msg_id}", flush=True
        # )
        print(
            f"[DEDUPE_FAIL_OPEN][{req_id}] evt_id={evt_id} msg_id={msg_id}",
            file=sys.stderr,
            flush=True,
        )
    else:
        # print(f"[DEDUPE_PASS][{req_id}] evt_id={evt_id} msg_id={msg_id}", flush=True)
        print(
            f"[DEDUPE_PASS][{req_id}] evt_id={evt_id} msg_id={msg_id}",
            file=sys.stderr,
            flush=True,
        )

    # 5) handler.handle 前後各加入追蹤點（知道到底卡在 handle 前或 handle 內）
    print(f"[CB_BEFORE_HANDLE][{req_id}]", file=sys.stderr, flush=True)
    app.logger.info("Request body: " + body)

    # 把 handler.handle 丟到背景跑，callback 先回 200 避免 LINE request_timeout
    def _run_bg():
        try:
            _bg_handle_line_webhook(body=body, signature=signature, req_id=req_id)
        finally:
            # 看背景處理總耗時（包含 handler.handle）
            print(
                f"[CB_DONE][{req_id}] elapsed={time.time()-t0:.3f}s",
                file=sys.stderr,
                flush=True,
            )

    threading.Thread(target=_run_bg, daemon=True).start()

    # 先回應 LINE（秒回）
    print(
        f"[CB_ACK][{req_id}] elapsed={time.time()-t0:.3f}s",
        file=sys.stderr,
        flush=True,
    )
    return "OK"


# ======================================
#  LINE Event Handlers 區/訊息處理
# ======================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    evt_id = getattr(event, "webhook_event_id", None) or getattr(
        event, "webhookEventId", None
    )
    msg_id = getattr(event.message, "id", None)
    ts = getattr(event, "timestamp", None)
    uid = None
    if event.source and hasattr(event.source, "user_id"):
        uid = event.source.user_id

    print(
        f"[HANDLE] evt_id={evt_id} msg_id={msg_id} ts={ts} uid={uid} text={text}",
        flush=True,)
    

    print(f"[HANDLE_ENTER] uid={uid} text={text}", flush=True)



    # === 0. 檢查是否處於首次建檔流程 ===
    line_user_id_for_state = None

    if event.source and hasattr(event.source, "user_id"):
        line_user_id_for_state = event.source.user_id
        

    # === -1. 使用者主動中斷建檔流程 ===
    if text in {"取消建檔", "取消流程", "取消", "取消預約流程"}:

        cleared = clear_pending_state(line_user_id_for_state)
        app.logger.info(f"[取消建檔] uid={line_user_id_for_state} cleared={cleared}")

        if cleared:
            msg = "已為您取消預約流程，謝謝。"
        else:
            msg = "目前沒有正在進行的流程。\n如需預約看診，請輸入「線上約診」。"

        start_loading_animation(line_bot_api, uid, loading_seconds=30, timeout=(1, 2))

        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text=msg)],
            label="cancel_flow_result",
        )
        return

    state = get_state(line_user_id_for_state) if line_user_id_for_state else {}
    step = state.get("step")
    print(f"[STATE] uid={line_user_id_for_state} step={step} state={state}", flush=True)

    # === 非流程中：未知輸入先給短 loading，避免空等 ===
    if line_user_id_for_state and not step:
        known_commands = {
            "線上約診",
            "約診查詢",
            "取消預約",
            "取消預約流程",
            "診所資訊",
            "衛教資訊",
            "查看地圖位置",
            "查詢診所位置",
            "測試token",
            "測試身分",
            "取消建檔",
            "取消流程",
            "取消",
        }
        known_prefix = (
            "查 ",
            "預約 ",
            "我想預約",
            "確認預約",
            "取消約診",
            "確認取消",
            "確認回診",
        )

        if text and (text in known_commands or text.startswith(known_prefix)):
            pass
        else:
            start_loading_animation(
                line_bot_api,
                line_user_id_for_state,
                loading_seconds=10,
                timeout=(1, 2),
            )


    if line_user_id_for_state and step:
        # ===== 流程中保護：避免把「線上約診/取消預約...」當成姓名或手機 =====
        flow_commands = {
            "線上約診",
            "約診查詢",
            "取消預約",
            "取消預約流程",
            "查詢診所位置",
            "我要預約本週",
            "我要預約下週",
            "我要預約兩週後",
            "我要預約三週後",
            "其他日期",
            "測試token",
            "測試身分",
            "診所資訊",
            "查看地圖位置",
        }

        is_command = (
            text in flow_commands
            or text.startswith("查 ")
            or text.startswith("預約 ")
            or text.startswith("我想預約")
            or text.startswith("確認預約")
            or text.startswith("取消約診")
            or text.startswith("確認取消")
            or text.startswith("確認回診")
        )

        # Step: pick_booking_type 允許「預約看診 / 預約針灸」跳過上鎖的流程
        if step == "pick_booking_type" and text in {"預約看診", "預約針灸"}:
            is_command = False

        if is_command:
            start_loading_animation(
                line_bot_api,
                line_user_id_for_state,
                loading_seconds=30,
                timeout=(1, 2),
            )
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(
                        text="您目前正在填寫資料中。\n如要取消請按「取消」或輸入「取消」。"
                    )
                ],
                label="already_in_flow_warning",
            )
            return


        # ===== 等待同意：使用者若直接輸入，不要 reset，提示他們先按按鈕 =====
        if step in {
            "wait_consent_new_name",
            "wait_consent_name_after_phone",
            "wait_consent_phone",
            "wait_consent_acu_terms",  
        }:

            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))

            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(
                        text="請先按下方按鈕「開始輸入」後再輸入。若要取消請輸入「取消」。"
                    )
                ],
                label="consent",
            )
            return
        
        # Step: pick_booking_type（只允許在這一步點「預約看診 / 預約針灸」）
        # 0-x. 選擇預約項目（看診 / 針灸）
        if step == "pick_booking_type":

            if text == "預約看診":
                # 清掉 step，進入看診流程（原本的流程）
                state["step"] = None
                state["booking_type"] = "clinic"
                set_state(line_user_id_for_state, state)

                start_loading_animation(
                    line_bot_api,
                    line_user_id_for_state,
                    loading_seconds=15,
                    timeout=(1, 2),
                )
                reply_date_range_buttons(event, "請選擇要預約的日期範圍：")
                return

            if text == "預約針灸":
                # 這裡就不再掉到外面的 elif text == "預約針灸"了
                print(
                    f"[BOOK_TYPE] enter uid={line_user_id_for_state} text={text} state={state}",
                    flush=True,
                )
                start_loading_animation(
                    line_bot_api,
                    line_user_id_for_state,
                    loading_seconds=15,
                    timeout=(1, 2),
                )
                # 先查是不是已建檔病患
                ok_patient = is_registered_patient(line_user_id_for_state)
                print(
                    f"[BOOK_TYPE] is_registered uid={line_user_id_for_state} ok={ok_patient}",
                    flush=True,
                )

                if not ok_patient:
                    
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="若您是初診，請先到診所掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。"
                            )
                        ],
                        label="acupuncture_invalid_patient",
                    )
                    return

                # === 針灸資格 1/2/3 缺一不可 ===
                ok_acu, missing, zd_user = check_acupuncture_eligibility_from_zendesk(
                    line_user_id_for_state
                )

                print(
                    f"[BOOK_TYPE][ACU_ELIG] uid={line_user_id_for_state} ok={ok_acu} "
                    f"missing={missing} zd_user_id={(zd_user or {}).get('id')}",
                    flush=True,
                )

                if not ok_acu:
                    # 查詢失敗 / 找不到資料 / 或缺資格 → 都在這邊擋
                    if "後端查詢病患資料失敗" in (missing or []):
                        print(
                            f"[ACU_ELIG] ineligible uid={line_user_id_for_state} missing={missing}",
                            flush=True,
                        )

                        send_line(
                            line_bot_api,
                            event,
                            messages=[
                                TextMessage(
                                    text="後端查詢病患資料發生錯誤，請稍後再試。"
                                )
                            ],
                            label="acupuncture_eligibility_lookup_failed",
                        )
                        return
                    
                    #組拒絕約針灸文案
                    header = "很抱歉，目前您尚未符合針灸線上預約資格（需同時符合以下條件）："
                    details = (
                        "\n".join([f"・{x}" for x in (missing or [])])
                        or "・（請聯繫診所查詢是否符合線上預約資格）"
                    )
                    footer = "如有疑問請聯繫診所協助確認，謝謝您。"
                    msg = f"{header}\n{details}\n\n{footer}"

                    # clear_state(line_user_id_for_state)
                    state["step"] = "pick_booking_type"
                    set_state(line_user_id_for_state, state)
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text=msg + "\n\n您可以改為預約看診，或取消本次預約流程：",
                                quick_reply=QuickReply(
                                    items=[
                                        QuickReplyItem(
                                            action=MessageAction(label="預約看診", text="預約看診")
                                        ),
                                        QuickReplyItem(
                                            action=MessageAction(label="取消預約", text="取消預約")
                                        ),
                                    ]
                                ),
                            )
                        ],
                        label="acupuncture_ineligible_with_buttons",
                    )
                    return

                # 走到這裡 = 已是老客 + 針灸資格 OK → 先出同意書/針灸說明的buttons，再進入選日期
                #（避免使用者跳過同意事項直接選時段）
                state["step"] = "wait_consent_acu_terms"
                state["booking_type_pending"] = "acupuncture"
                set_state(line_user_id_for_state, state)

                start_loading_animation(
                    line_bot_api,
                    line_user_id_for_state,
                    loading_seconds=15,
                    timeout=(1, 2),
                )

                reply_acu_terms_buttons(line_bot_api, event)
                return
                        
            if text == "取消預約":
                clear_state(line_user_id_for_state)
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="已取消本次操作。如需重新預約，請輸入「線上約診」。")],
                    label="pick_booking_type_cancel",
                )
                return


            # 其他文字：提示使用者點按鈕
            start_loading_animation(
                line_bot_api,
                line_user_id_for_state,
                loading_seconds=10,
                timeout=(1, 2),
            )
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(text="請點選按鈕選擇要預約的項目：預約看診 / 預約針灸")
                ],
                label="pick_booking_type_hint",
            )
            return

        

        # 0-1. 問姓名（此流程在最新討論的預約流程中已無使用）
        if step == "ask_name":
            name = text.strip()
            if not name:
                start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="姓名不能空白，請再次輸入您的姓名。"
                        )
                    ],
                    label="name_cannot_blank",
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
                        app.logger.warning(
                            "[handle_message] 寫入 Zendesk 姓名失敗，但仍繼續問手機"
                        )
                except Exception as e:
                    app.logger.error(
                        f"[handle_message] 更新 Zendesk user 姓名失敗: {e}"
                    )
                    # 不中斷流程，繼續問手機

            state["name"] = name
            state["step"] = "ask_phone"
            set_state(line_user_id_for_state, state)

            reply_text = f"{name} 您好，請輸入您的手機號碼（格式：09xxxxxxxx）："

            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(
                        text=reply_text
                    )
                ],
                label="enter_phone_number",
            )
            return

        # 0-1.5 問姓名（手機已經有了，補姓名用）
        elif step == "ask_name_after_phone":

            print(f"[ASK_NAME_AFTER_PHONE] enter uid={uid} text={text} state={state}", flush=True)

            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2))

            name = text.strip()
            if not is_valid_name(name):
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="請輸入您的真實姓名（不可空白）。"
                        )
                    ],
                    label="name_cannot_blank",
                )
                return

            zendesk_user_id = state.get("zendesk_user_id")
            if not zendesk_user_id:
                # 保守：如果意外沒有 user_id，就回到問手機重新走
                state["step"] = "ask_phone"
                set_state(line_user_id_for_state, state)
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="資料狀態異常，請重新輸入手機號碼（09xxxxxxxx）："
                        )
                    ],
                    label="phone_number_system_error",
                )
                return

            phone = (state.get("phone") or "").strip()
            # 更新 Zendesk：name + profile_status=complete（手機已經有了）
            base_url, headers = _build_zendesk_headers()
            url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"
            
            payload = {
                "user": {
                    "name": name,
                    "phone": phone,
                    "external_id": line_user_id_for_state,
                    "user_fields": {
                        ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
                        ZENDESK_UF_PROFILE_STATUS_KEY: (
                            PROFILE_STATUS_COMPLETE
                            if is_valid_name(name)
                            else PROFILE_STATUS_NEED_NAME
                        ),
                    },
                }
            }

            try:
                print(f"[ASK_NAME_AFTER_PHONE] before_put uid={uid} zid={zendesk_user_id} phone={phone} name={name}", flush=True)
                resp = requests.put(url, headers=headers, json=payload, timeout=10)
                print(f"[ASK_NAME_AFTER_PHONE] after_put uid={uid} status={resp.status_code}", flush=True)
                app.logger.info(
                    f"[ask_name_after_phone][PUT] status={resp.status_code} body={resp.text[:300]}"
                )
                resp.raise_for_status()
            except Exception as e:
                print(f"[ASK_NAME_AFTER_PHONE] put EXC uid={uid} zid={zendesk_user_id} err={repr(e)}", flush=True)
                app.logger.error(
                    f"[ask_name_after_phone] 更新 Zendesk 姓名失敗 user_id={zendesk_user_id}: {e}"
                )
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="更新姓名時發生問題，請稍後再試。"
                        )
                    ],
                    label="name_system_error",
                )
                return

            
            print(f"[ASK_NAME_AFTER_PHONE] before_clear uid={line_user_id_for_state} state={state}", flush=True)
            
            # 成功 → 清狀態 → 進入選日期範圍（跟原本完成建檔一致）
            clear_state(line_user_id_for_state)

            st_after = get_state(line_user_id_for_state) or {}
            print(f"[ASK_NAME_AFTER_PHONE] after_clear uid={line_user_id_for_state} state_now={st_after}", flush=True)

            phone_display = state.get("phone") or "（已留存）"
            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{phone_display}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

            print(f"[ASK_NAME_AFTER_PHONE] before_reply_date_range uid={line_user_id_for_state}", flush=True)
            reply_date_range_buttons(event, info_text)
            print(f"[ASK_NAME_AFTER_PHONE] after_reply_date_range uid={line_user_id_for_state}", flush=True)
            return

        elif step == "confirm_name_after_claim":
            # 期待：姓名正確 / 我要修改姓名

            print(f"[CONFIRM_CLAIM] enter uid={line_user_id_for_state} text={text} state={state}", flush=True)

            if text not in {"姓名正確", "我要修改姓名"}:
                start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="請點選按鈕：姓名正確 / 我要修改姓名")],
                    label="confirm_name_after_claim_invalid_choice",
                )
                return


            print(f"[CONFIRM_CLAIM] before_bind uid={line_user_id_for_state} zd_user_id={state.get('zendesk_user_id')} phone={state.get('phone')}", flush=True)

            zendesk_user_id = state.get("zendesk_user_id")
            phone = (state.get("phone") or "").strip()
            found_name = (state.get("found_name") or "").strip()

            if not zendesk_user_id:
                clear_state(line_user_id_for_state)
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始。")
                    ],
                    label="confirm_name_after_claim_missing_user",
                )
                return

            # 使用者選「我要修改姓名」→ 直接進入補姓名
            if text == "我要修改姓名":
                start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
                state["step"] = "ask_name_after_phone"
                # ask_name_after_phone 會負責把 name + phone + external_id 一次寫入 Zendesk
                set_state(line_user_id_for_state, state)

                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="請輸入您要更新的真實姓名（全名）：")],
                    label="confirm_name_after_claim_to_ask_name",
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
                        ZENDESK_UF_PROFILE_STATUS_KEY: (
                            PROFILE_STATUS_COMPLETE
                            if is_valid_name(found_name)
                            else PROFILE_STATUS_NEED_NAME
                        ),
                    },
                }
            }

            # 如果 state 有 phone，就一起補上（不然 Zendesk 有些資料會留空）
            if phone:
                payload["user"]["phone"] = phone

            start_loading_animation(
                line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2)
            )

            try:
                resp = requests.put(url, headers=headers, json=payload, timeout=10)
                app.logger.info(
                    f"[confirm_name_after_claim][PUT] status={resp.status_code} body={resp.text[:300]}"
                )
                resp.raise_for_status()
            except Exception as e:
                app.logger.error(
                    f"[confirm_name_after_claim] bind failed user_id={zendesk_user_id}: {e}"
                )
                # line_bot_api.reply_message(
                #     ReplyMessageRequest(
                #         reply_token=event.reply_token,
                #         messages=[TextMessage(text="綁定資料時發生問題，請稍後再試。")],
                #     )
                # )
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="綁定資料時發生問題，請稍後再試。")],
                    label="data_biding_error",
                )
                return

            # clear_state(line_user_id_for_state)
            state["step"] = "pick_booking_type"
            state["zendesk_user_id"] = zendesk_user_id
            set_state(line_user_id_for_state, state)

            info_text = (
                f"{found_name or '貴賓'} 您好，已為您完成身分綁定。\n"
                f"手機：{phone or '（已確認）'}\n\n"
                "請選擇要預約的日期範圍："
            )

            print(f"[CONFIRM_CLAIM] before_reply uid={line_user_id_for_state}", flush=True)
            start_loading_animation(
                line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2)
            )

            reply_booking_type_buttons(event, info_text)
            return

        elif step == "ask_name_for_multi_claim":
            name = text.strip()

            candidates = state.get("candidates") or []
            phone = state.get("phone") or ""
            mode = (state.get("mode") or "").strip()

            # already_bound：不做姓名格式檢查，直接拿來比對；比對結果最後都導客服
            if mode != "already_bound":
                if not is_valid_name(name):
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="請輸入您的真實姓名（全名），以便確認資料。"
                            )
                        ],
                        label="ask_name_for_multi_claim_invalid_name",
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
                    clear_state(line_user_id_for_state)
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="此手機號碼已綁定其他帳號，請聯繫診所協助處理。"
                            )
                        ],
                        label="ask_name_for_multi_claim_already_bound",
                    )
                    return

                found = matched[0]
                found_name = (found.get("name") or "").strip()

                # 姓名 placeholder → 直接補姓名
                if not is_valid_name(found_name):
                    set_state(
                        line_user_id_for_state,
                        {
                            "step": "ask_name_after_phone",
                            "zendesk_user_id": found.get("id"),
                            "phone": phone,
                            "found_name": found_name,
                        },
                    )
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="已確認您的手機，請輸入您的真實姓名（全名）："
                            )
                        ],
                        label="ask_name_for_multi_claim_placeholder_to_ask_name",
                    )
                    return

                # 姓名有效 → 進入確認姓名
                set_state(
                    line_user_id_for_state,
                    {
                        "step": "confirm_name_after_claim",
                        "zendesk_user_id": found.get("id"),
                        "phone": phone,
                        "found_name": found_name,
                    },
                )

                buttons_template = ButtonsTemplate(
                    title="確認姓名",
                    text=f"我們找到您的資料：\n姓名：{found_name}\n手機：{phone}\n\n姓名是否正確？",
                    actions=[
                        MessageAction(label="正確", text="姓名正確"),
                        MessageAction(label="我要修改", text="我要修改姓名"),
                    ],
                )
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TemplateMessage(alt_text="確認姓名", template=buttons_template)
                    ],
                    label="ask_name_for_multi_claim_confirm_buttons",
                )
                return

            if len(matched) == 0:
                if mode == "already_bound":
                    clear_state(line_user_id_for_state)
                    # line_bot_api.reply_message(
                    #     ReplyMessageRequest(
                    #         reply_token=event.reply_token,
                    #         messages=[
                    #             TextMessage(
                    #                 text="此手機號碼已綁定其他帳號，請聯繫診所客服協助處理。"
                    #             )
                    #         ],
                    #     )
                    # )
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="此手機號碼已綁定其他帳號，請聯繫診所客服協助處理。"
                            )
                        ],
                        label="phone_multi_user_error",
                    )
                    return

                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="找不到符合此姓名的資料。請確認後重新輸入姓名，或聯繫診所協助。"
                        )
                    ],
                    label="ask_name_for_multi_claim_no_match",
                )
                return

            # matched > 1：同手機+同姓名仍多筆，只能擋
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(text="此姓名仍對應多筆資料，請聯繫診所協助確認。")
                ],
                label="ask_name_for_multi_claim_multi_match",
            )
            return

        # 0-2. 問手機
        elif step == "ask_phone":
            phone_raw = text.strip()
            digits = normalize_phone(phone_raw)

            print(f"[ASK_PHONE] enter uid={line_user_id_for_state} phone_raw={phone_raw} digits={digits}", flush=True)


            if not (len(digits) == 10 and digits.startswith("09")):

                print(f"[ASK_PHONE] invalid uid={line_user_id_for_state} digits={digits}", flush=True)

                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="手機格式不正確，請以 09xxxxxxxx 格式重新輸入。"
                        )
                    ],
                    label="ask_phone_invalid_format",
                )
                return

            print(f"[ASK_PHONE] valid uid={line_user_id_for_state} digits={digits} -> start_loading", flush=True)
            start_loading_animation(
                line_bot_api,
                line_user_id_for_state,
                loading_seconds=30,
                timeout=(1, 2),
            )

            # === A路線：先用手機找 Zendesk seed 老客，做認領 ===
            # 只有在「此 LINE 尚未綁定」時才做認領，避免老用戶更新資料時誤觸

            print(f"[ASK_PHONE] before_search_by_line uid={line_user_id_for_state}", flush=True)
            try:
                # 先查這個 LINE ID 在 Zendesk 有沒有已綁定資料
                bound_count, bound_user = search_zendesk_user_by_line_id(
                    line_user_id_for_state, retries=1
                )
                print(f"[ASK_PHONE] after_search_by_line uid={line_user_id_for_state} bound_user={'Y' if bound_user else 'N'}", flush=True)
            except Exception as e:
                print(f"[ASK_PHONE] search_by_line EXC uid={line_user_id_for_state} err={repr(e)}", flush=True)
                app.logger.error(f"[ask_phone][claim] search by line_id failed: {e}")
                bound_user = None


            # ===== Guard：若此 LINE 已有綁定中的 user（不論 complete/need_name），不允許更換手機去認領別人 =====
            # 目的：避免「先留了一支手機（或半成品）→ 下一次輸入另一支手機」造成搶綁與資料錯亂

            if bound_user:
                # 如果 bound_user 存在：代表「這個 LINE 已經綁到某個 Zendesk user」
                ufs = bound_user.get("user_fields") or {}
                bound_phone = normalize_phone(bound_user.get("phone") or "")
                bound_profile = (ufs.get(ZENDESK_UF_PROFILE_STATUS_KEY) or "").strip()

                # 若 Zendesk 已留 phone，且使用者輸入的 digits 與既有 phone 不同 → 直接擋
                if bound_phone and bound_phone != digits:
                    start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=5, timeout=(1, 2))
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="此帳號已綁定其他手機號碼，系統不允許線上更換。請聯繫診所協助處理。"
                            )
                        ],
                        label="ask_phone_block_change_phone",
                    )
                    return

                # 若 profile_status 不是 complete（例如 need_name）→ 直接導向補姓名（不要走認領）
                bound_name = (bound_user.get("name") or "").strip()
                if bound_profile != PROFILE_STATUS_COMPLETE or (
                    not is_valid_name(bound_name)
                ):
                    set_state(
                        line_user_id_for_state,
                        {
                            "step": "wait_consent_name_after_phone",
                            "zendesk_user_id": bound_user.get("id"),
                            "phone": (bound_phone or digits),
                        },
                    )

                    reply_consent_input(
                        line_bot_api=line_bot_api,
                        event=event,
                        title="填寫基本資料",
                        text="我們已確認您的手機。為完成資料綁定，請先填寫您的真實姓名（全名）。\n點擊「開始輸入」後再輸入姓名。",
                        ok_data="CONSENT_NAME_AFTER_PHONE",
                        cancel_data="CANCEL_FLOW",
                    )
                    return
 
                reply_date_range_buttons(
                    event, "已確認您的姓名和手機，請選擇要預約的日期範圍："
                )
                return

            if not bound_user:
                try:
                    candidates = search_zendesk_users_by_phone(
                        digits
                    )  # 放在 zendesk_core
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

                    # Case 1：姓名是 placeholder → 直接補姓名（要同意開關）
                    if not is_valid_name(found_name):
                        set_state(
                            line_user_id_for_state,
                            {
                                "step": "wait_consent_name_after_phone",
                                "zendesk_user_id": found.get("id"),
                                "phone": digits,
                            },
                        )

                        reply_consent_input(
                            line_bot_api=line_bot_api,
                            event=event,
                            title="填寫姓名",
                            text="已找到您的資料（手機已確認）。\n為完成身分綁定，請補上您的真實姓名（全名）。\n點擊「開始輸入」後再輸入姓名。",
                            ok_data="CONSENT_NAME_AFTER_PHONE",
                            cancel_data="CANCEL_FLOW",
                        )
                        return

                    # Case 2：姓名有效 → 進入「確認姓名是否正確」的按鈕
                    set_state(
                        line_user_id_for_state,
                        {
                            "step": "confirm_name_after_claim",
                            "zendesk_user_id": found.get("id"),
                            "phone": digits,
                            "found_name": found_name,
                        },
                    )

                    buttons_template = ButtonsTemplate(
                        title="確認姓名",
                        text=f"{found_name} 您好，\n手機：{digits}\n\n以上是您的個人資料，請問是否正確？",
                        actions=[
                            MessageAction(label="正確", text="姓名正確"),
                            MessageAction(label="我要修改", text="我要修改姓名"),
                        ],
                    )

                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TemplateMessage(
                                alt_text="確認姓名", template=buttons_template
                            )
                        ],
                        label="confirm_name_after_claim",
                    )

                    return

                if len(unbound) > 1:
                    # 進入「多筆資料 → 輸入姓名縮小範圍」
                    set_state(
                        line_user_id_for_state,
                        {
                            "step": "ask_name_for_multi_claim",
                            "phone": digits,
                            "candidates": [
                                {"id": u.get("id"), "name": u.get("name") or ""}
                                for u in unbound
                            ],
                        },
                    )

                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="此手機號碼已有資料。為了確認身分，請輸入您的姓名（全名）："
                            )
                        ],
                        label="ask_name_for_multi_claim",
                    )
                    return

                # candidates 有資料但都已綁 external_id（可能是本人就綁定，也可能被別人綁走）
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
                            set_state(
                                line_user_id_for_state,
                                {
                                    "step": "ask_name_after_phone",
                                    "zendesk_user_id": found.get("id"),
                                    "phone": found_phone,
                                    "found_name": found_name,
                                },
                            )
                            
                            send_line(
                                line_bot_api,
                                event,
                                messages=[
                                    TextMessage(
                                        text="已確認您的手機，請輸入您的真實姓名（全名）："
                                    )
                                ],
                                label="ask_name_after_phone",
                            )
                            return

                        
                        # 姓名完整 → 直接放行預約
                        reply_date_range_buttons(
                            event, f"{found_name} 您好，\n請選擇要預約的日期範圍："
                        )
                        return

                    # 不是綁到自己（或多筆混雜）→ 依規格：先輸入姓名比對，失敗才叫客服
                    set_state(
                        line_user_id_for_state,
                        {
                            "step": "ask_name_for_multi_claim",
                            "phone": digits,
                            "candidates": [
                                {
                                    "id": u.get("id"),
                                    "name": u.get("name") or "",
                                    "external_id": (u.get("external_id") or ""),
                                }
                                for u in candidates
                            ],
                            "mode": "already_bound",  # 用來讓後續分支知道這是「已綁走」情境
                        },
                    )
                    start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
                    send_line(
                        line_bot_api,
                        event,
                        messages=[
                            TextMessage(
                                text="此手機號碼已有資料。為了確認身分，請輸入您的姓名（全名）："
                            )
                        ],
                        label="ask_name_for_multi_claim",
                    )
                    return

            # === 若沒有找到可認領的 seed 老客,才進入原本的新朋友流程 ===
            name = state.get("name") or "未填姓名"
            # === 若沒有找到可認領的 seed 老客 ===
            # 若入口是「線上約診（只允許既有病患綁定）」→ 不走新朋友建檔，直接回覆初診文案
            if state.get("bind_only") is True:
                clear_state(line_user_id_for_state)
                start_loading_animation(
                    line_bot_api,
                    line_user_id_for_state,
                    loading_seconds=15,
                    timeout=(1, 2),
                )
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。"
                        )
                    ],
                    label="bind_only_no_profile",
                )
                return
            
            profile_status_value = (
                PROFILE_STATUS_COMPLETE
                if is_valid_name(name)
                else PROFILE_STATUS_NEED_NAME
            )

            # 寫進 Zendesk：phone + profile_status=complete
            user = None
            zendesk_user_id = state.get("zendesk_user_id")

            # 優先：直接更新剛剛那一筆（不靠 search）
            if line_user_id_for_state and zendesk_user_id:
                base_url, headers = _build_zendesk_headers()
                app.logger.info(
                    f"[ask_phone] will update zendesk_user_id={zendesk_user_id} line_user_id={line_user_id_for_state}"
                )
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
                    app.logger.info(
                        f"[ask_phone][PUT] status={resp.status_code} body={resp.text[:300]}"
                    )
                    resp.raise_for_status()
                    user = (resp.json() or {}).get("user")
                    app.logger.info(
                        f"[ask_phone] 更新 Zendesk user_id={zendesk_user_id} 成功"
                    )
                except Exception as e:
                    app.logger.error(
                        f"[ask_phone] 更新 Zendesk user_id={zendesk_user_id} 失敗: {e}"
                    )
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
                    app.logger.error(
                        f"[handle_message] 更新 Zendesk user 手機失敗: {e}"
                    )
                    user = None

            if not user:
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="建立病患資料時發生問題，請稍後再試。")],
                    label="create_profile_failed",
                )
                return

            # 成功 → 清除state狀態
            # 不看 flow：只要姓名無效（含 未填姓名）→ 補姓名（要同意開關）
            if not is_valid_name(name):
                state["zendesk_user_id"] = user.get("id") or state.get(
                    "zendesk_user_id"
                )
                state["phone"] = digits
                state["step"] = "wait_consent_name_after_phone"
                set_state(line_user_id_for_state, state)

                start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))

                reply_consent_input(
                    line_bot_api=line_bot_api,
                    event=event,
                    title="填寫姓名",
                    text="手機已確認。\n為完成身分綁定，請補上您的真實姓名（全名）。\n點擊「開始輸入」後再輸入姓名。",
                    ok_data="CONSENT_NAME_AFTER_PHONE",
                    cancel_data="CANCEL_FLOW",
                )
                return

            # 姓名有效 → 清狀態 → 放行選日期範圍
            clear_state(line_user_id_for_state)

            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))

            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{digits}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

            reply_booking_type_buttons(event, info_text)
            return

        # 0-3. 例外 step → reset
        else:
            clear_state(line_user_id_for_state)
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始流程。")
                ],
                label="state_reset",
            )
            return


    # === 測試：從後端跟 Entra 拿 Graph token（使用者用不到） ===
    if text == "測試token":
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)

        # 先給 loading（不吃 reply token；timeout 設短避免卡 webhook）
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )

        try:
            token = get_graph_token()
            app.logger.info(f"GRAPH ACCESS TOKEN (HEAD): {token[:30]}...")
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="成功取得 Graph token")],
                label="test_graph_token_ok",
            )
        except Exception as e:
            app.logger.error(f"Graph token 申請失敗: {e}")
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="Graph token 申請失敗，請稍後再試")],
                label="test_graph_token_fail",
            )
        return

    # === 查詢某天預約（內部不公開流程） ===
    if text.startswith("查 "):
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)

        # 先給 loading（因為下面會打 Graph / Bookings 查詢）
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )

        parts = text.split()
        if len(parts) >= 2:
            date_str = parts[1].strip()
            try:
                appts = list_appointments_for_date(date_str)
                reply_text = f"{date_str} 有 {len(appts)} 筆預約"
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text=reply_text)],
                    label="query_appts_ok",
                )
            except Exception as e:
                app.logger.error(f"查預約失敗: {e}")
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="查預約失敗，請稍後再試")],
                    label="query_appts_fail",
                )
        else:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="請輸入：查 YYYY-MM-DD，例：查 2025-01-15")],
                label="query_appts_bad_args",
            )
        return

    # === 預約 YYYY-MM-DD：顯示 Carousel（限制三週內＋需已建檔）
    elif text.startswith("預約 "):
        date_str = text.replace("預約", "").strip()

        # 取得 LINE userId
        line_user_id = None
        if event.source and hasattr(event.source, "user_id"):
            line_user_id = event.source.user_id

        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))


        # 1. 檢查是否已有 Zendesk 病患資料（避免未建檔客戶亂預約）
        if not is_registered_patient(line_user_id):
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(
                        text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。"
                    )
                ],
                label="ask_register_before_booking",
            )
            return

        # 2. 驗證日期（格式正確／三週內／非過去）
        ok, msg = validate_appointment_date(date_str)
        if not ok:

            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text=msg)],
                label="validate_appointment_date",
            )
            return

        # 3. 通過檢查才真的去查某天的時段
        try:
            # 依 booking_type 分流（沒值就當看診）
            state = get_state(line_user_id) or {}
            booking_type = (state.get("booking_type") or "clinic").strip()

            if booking_type == "acupuncture":
                available_slots = get_available_acu_slots_for_date(date_str)
                if not available_slots:
                    reply_msg = TextMessage(text=f"{date_str} 沒有可預約時段")
                else:
                    reply_msg = build_slots_carousel(date_str, available_slots)

            else:
                # 門診：只選早/晚，不顯示 HH:MM
                # 規則：沒有名額 / 該日無晚診 → 不要顯示該按鈕

                actions = []

                # 早診是否還有名額（只做存在判斷，不回傳 HH:MM 給病患）
                t_morning = pick_first_available_clinic_time(
                    date_str=date_str,
                    period="morning",
                    business_id=BOOKINGS_BUSINESS_CLINIC_ID,
                )
                if t_morning:
                    actions.append(MessageAction(label="早診", text=f"門診早 {date_str}"))

                # 晚診是否還有名額
                t_evening = pick_first_available_clinic_time(
                    date_str=date_str,
                    period="evening",
                    business_id=BOOKINGS_BUSINESS_CLINIC_ID,
                )
                if t_evening:
                    actions.append(MessageAction(label="晚診", text=f"門診晚 {date_str}"))

                if not actions:
                    reply_msg = TextMessage(text=f"{date_str} 門診目前無可預約時段，請改選其他日期。")
                else:
                    tpl = ButtonsTemplate(
                        title="選擇內科門診時段",
                        text=f"{date_str}\n請選擇要預約早診或晚診：",
                        actions=actions,
                    )
                    reply_msg = TemplateMessage(alt_text="選擇門診早/晚", template=tpl)

        except Exception as e:
            app.logger.error(f"取得可預約時段失敗: {e}")
            reply_msg = TextMessage(text="取得可預約時段失敗，請稍後再試")

        # 回傳 Carousel 或是錯誤訊息
        send_line(
            line_bot_api,
            event,
            messages=[reply_msg],
            label="booking_slots_reply",
        )
        return
    

    # === ① 線上約診：先判斷 Zendesk 有沒有這個病患 ===
    elif text == "線上約診":
        # 1-1 取得 LINE userId
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        # line_user_id = None
        # if event.source and hasattr(event.source, "user_id"):
        #     line_user_id = event.source.user_id

        if not line_user_id:
            # 聊天一定有 user_id，這裡只是保險用的
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text="找不到 LINE userId，請改用 1 對 1 聊天測試。")
                    ],
                )
            )
            return

        start_loading_animation(
            line_bot_api,
            line_user_id,
            loading_seconds=30,
            timeout=(1, 2),  # 訊息快點回傳回來，不卡住 webhook
        )

        # 1-2 先到 Zendesk 查這個 line_user_id 是否已建檔
        try:
            print(f"[FLOW] before_zendesk uid={uid} t={time.time()}", flush=True)
            count, user = search_zendesk_user_by_line_id(line_user_id, retries=1)
            print(
                f"[FLOW] after_zendesk uid={uid} t={time.time()} count={count}",
                flush=True,
            )
        except Exception as e:
            app.logger.error(f"查詢 Zendesk 使用者失敗: {e}")
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="後端查詢病患資料發生錯誤，請稍後再試。")],
                label="zendesk_search_failed",
            )
            return

        app.logger.info(
            f"[線上約診][debug] line_user_id={line_user_id} count={count} "
            f"user_none={user is None} user_id={(user or {}).get('id')} "
            f"uf_line={((user or {}).get('user_fields') or {}).get(ZENDESK_UF_LINE_USER_ID_KEY)} "
            f"profile_status={((user or {}).get('user_fields') or {}).get(ZENDESK_UF_PROFILE_STATUS_KEY)} "
            f"name={(user or {}).get('name')} phone={(user or {}).get('phone')}"
        )

        # === 已綁定完成者 → 直接放行；若已確認手機但缺姓名 → 直接補姓名；其餘才走電話 consent ===
        if user:
            user_fields = user.get("user_fields") or {}
            phone_raw = (user.get("phone") or "").strip()
            phone_digits = normalize_phone(phone_raw)
            name = (user.get("name") or "").strip()
            profile_status = user_fields.get(ZENDESK_UF_PROFILE_STATUS_KEY)

            phone_ok = len(phone_digits) == 10 and phone_digits.startswith("09")
            name_ok = is_valid_name(name)

            # Case 1：已經有 phone（已確認）但 name 需要補（need_name / placeholder）
            if phone_ok and (not name_ok or profile_status == PROFILE_STATUS_NEED_NAME):
                # set_state(line_user_id_for_state, {
                set_state(
                    line_user_id,
                    {
                        "step": "ask_name_after_phone",
                        "zendesk_user_id": user.get("id"),
                        "phone": phone_digits,
                    },
                )
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                        TextMessage(
                            text="系統中已有您的資料（手機已確認），請輸入您的真實姓名（全名）："
                        )
                    ],
                    label="ask_name_after_phone",
                )
                return

            # Case 2：已綁定完成者 → 先選「預約看診 / 預約針灸」
            if is_binding_complete(user, line_user_id):
                info_text = (
                    f"{name or '貴賓'} 您好，診所已有您的資料：\n"
                    f"手機：{phone_raw or '（系統已有您的手機號碼）'}\n\n"
                    "請問您想預約什麼項目？"
                )

                # 記下這次入口使用者（之後針灸會查條件/看診直接放行）
                set_state(line_user_id, {
                    "step": "pick_booking_type",
                    "zendesk_user_id": user.get("id"),
                })

                reply_booking_type_buttons(event, info_text)
                return

        #以下舊流程：Case 3：其餘（查不到、沒有 phone、未綁、等等）→ 才走 consent → ask_phone（目前最新版本已經不會叫患者先留姓名了）
        # set_state(line_user_id, {"step": "wait_consent_phone"})
        set_state(line_user_id, {"step": "wait_consent_phone", "bind_only": True}) # "bind_only": True 就是不進入新朋友留資料流程
        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
        reply_consent_input(
            line_bot_api=line_bot_api,
            event=event,
            title="線上約診",
            text=(
                "第一次使用線上約診，請先輸入您的手機號碼以查詢身分。\n"
                "點擊「開始輸入」後再輸入手機。"
            ),
            ok_data="CONSENT_PHONE",
            cancel_data="CANCEL_FLOW",
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
                    messages=[
                        TextMessage(text="找不到 LINE userId，請改用 1 對 1 聊天測試。")
                    ],
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
                    messages=[
                        TextMessage(text="後端查詢病患資料發生錯誤，請稍後再試。")
                    ],
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
                    messages=[TextMessage(text=reply_text)],
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

        # 3. 把狀態記在 state 裡，進入 ask_name 流程
        set_state(
            line_user_id_for_state,
            {
                "step": "ask_name",
                "display_name": display_name,
            },
        )

        reply_text = (
            f"{display_name} 您好，歡迎使用線上預約服務。\n"
            "請先完成基本資料建檔再使用本服務。\n\n"
            "請輸入您的姓名（全名）："
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token, messages=[TextMessage(text=reply_text)]
            )
        )
        return
    # === 測試身分 END ===

    elif text == "預約看診":
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if not line_user_id:
            print("[BOOK_TYPE] missing line_user_id", flush=True)
            return

        state = get_state(line_user_id) or {}
        print(f"[BOOK_TYPE] enter uid={line_user_id} text={text} state={state}", flush=True)

        # 入口就擋：未建檔不要進流程
        ok_patient = is_registered_patient(line_user_id)
        print(f"[BOOK_TYPE] is_registered_patient uid={line_user_id} ok={ok_patient}", flush=True)

        if not ok_patient:
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")
                ],
                label="clinic_entry_require_registered",
            )
            return

        # 保險：不是從 pick_booking_type 來的也沒關係，照樣可走
        state["booking_type"] = "clinic"
        set_state(line_user_id, state)

        start_loading_animation(line_bot_api, line_user_id, loading_seconds=15, timeout=(1, 2))
        reply_date_range_buttons(event, "請選擇要預約的日期範圍：")
        return



    # === ②-1 其他日期：再提供兩週後／三週後選項 ===
    elif text == "其他日期":
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[WEEK_PICK] enter uid={line_user_id} text={text}", flush=True)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )

        buttons_template = ButtonsTemplate(
            title="選擇其他日期",
            text="請選擇要預約的日期範圍：",
            thumbnail_image_url=WEEK_IMAGE_URL,
            actions=[
                MessageAction(label="兩週後", text="我要預約兩週後"),
                MessageAction(label="三週後", text="我要預約三週後"),
            ],
        )
        send_line(
            line_bot_api,
            event,
            messages=[
               TemplateMessage(alt_text="選擇其他日期", template=buttons_template)
            ],
            label="other_dates",
        )
        return

    # === ② 我要預約本週 ===
    elif text == "我要預約本週":

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[WEEK_PICK] enter uid={line_user_id} text={text}", flush=True)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )

        show_dates_for_week(0, event, line_user_id)
        return

    # === ③ 我要預約下週 ===
    elif text == "我要預約下週":
    
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[WEEK_PICK] enter uid={line_user_id} text={text}", flush=True)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )
        show_dates_for_week(1, event, line_user_id)
        return

    # === ③-2 我要預約兩週後 ===
    elif text == "我要預約兩週後":

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[WEEK_PICK] enter uid={line_user_id} text={text}", flush=True)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )
        show_dates_for_week(2, event, line_user_id)
        return

    # === ③-3 我要預約三週後 ===
    elif text == "我要預約三週後":

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[WEEK_PICK] enter uid={line_user_id} text={text}", flush=True)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )
        show_dates_for_week(3, event,line_user_id)
        return

    # === 我想預約 YYYY-MM-DD HH:MM（需限制三週內＋需已建檔） ===
    elif text.startswith("我想預約"):
        print(f"[INTENT_BOOK] enter uid={uid} evt_id={evt_id} text={text}", flush=True)
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

            print(f"[INTENT_BOOK] parsed uid={line_user_id} date={date_str} time={time_str}", flush=True)

            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30)

            # 1. 檢查是否已有 Zendesk 病患資料（避免未建檔亂預約）
            print(f"[CONFIRM_APPT] before_is_registered uid={line_user_id}", flush=True)
            ok_patient = is_registered_patient(line_user_id)
            print(f"[CONFIRM_APPT] after_is_registered uid={line_user_id} ok={ok_patient}", flush=True)

            if not ok_patient:
                send_line(
                    line_bot_api,
                    event,
                    messages=[
                    TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")
                    ],
                    label="invalid_patient",
                )
                return

            # 2. 日期驗證（三週內／非過去）
            ok, msg = validate_appointment_date(date_str)
            print(f"[CONFIRM_APPT] validate_date uid={line_user_id} ok={ok} msg={msg}", flush=True)

            print(f"[INTENT_BOOK] validate_date uid={line_user_id} ok={ok} msg={msg}", flush=True)

            if not ok:
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text=msg)],
                    label="validate_appointment_date",
                )
                return

            # 2-1. 依 booking_type 檢查該時間是否是「該類型」可選時段（防止暴力輸入）
            state = get_state(line_user_id) or {}
            booking_type = (state.get("booking_type") or "clinic").strip()

            if booking_type == "acupuncture":
                # 必須在固定表內 + 當下仍可預約（避免已被佔）
                if not is_acu_slot_available(date_str, time_str):
                    send_line(
                        line_bot_api,
                        event,
                        messages=[TextMessage(text="很抱歉，您選擇的時段已滿或無法預約，請重新選擇其他時段。")],
                        label="acu_time_invalid",
                    )
                    return

            # 3. 通過檢查，顯示「預約確認」按鈕（此處只是確認，不會直接預約）
            buttons_template = ButtonsTemplate(
                title="預約確認",
                text=f"您選擇的時段是：\n{display_date} {time_str}\n\n是否確認預約？",
                actions=[
                    MessageAction(
                        label="確認預約", text=f"確認預約 {date_str} {time_str}"
                    ),
                    MessageAction(label="取消", text="取消預約流程"),
                ],
            )

            print(f"[INTENT_BOOK] before_send_confirm uid={line_user_id} date={date_str} time={time_str}", flush=True)

            send_line(
                line_bot_api,
                event,
                messages=[TemplateMessage(alt_text="預約確認", template=buttons_template)],
                label="booking_confirm",
            )
            print(f"[INTENT_BOOK] after_send_confirm uid={line_user_id}", flush=True)

            return

        # 格式不正確 → 直接提示
        else:
            # line_bot_api.reply_message(
            #     ReplyMessageRequest(
            #         reply_token=event.reply_token,
            #         messages=[TextMessage(text="請用格式：我想預約 YYYY-MM-DD HH:MM")],
            #     )
            # )
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="請用格式：我想預約 YYYY-MM-DD HH:MM")],
                label="wrong_format",
            )
            return

    # === 使用者取消預約流程（我想預約 → 預約確認 → 取消） ===
    elif text == "取消預約流程":
        print(f"[CANCEL_BOOK_FLOW] enter uid={uid} evt_id={evt_id}", flush=True)

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        cleared = clear_pending_state(line_user_id)
        print(f"[CANCEL_BOOK_FLOW] uid={line_user_id} cleared={cleared}", flush=True)

        buttons_template = ButtonsTemplate(
            title="已經取消約診流程",
            text="若需預約看診，請點擊「線上約診」。",
            actions=[MessageAction(label="線上約診", text="線上約診")],
        )

        send_line(
            line_bot_api,
            event,
            messages=[TemplateMessage(alt_text="已取消預約流程", template=buttons_template)],
            label="cancelled",
        )
        return

    
    elif text.startswith("門診早 ") or text.startswith("門診晚 "):
        parts = text.split()
        if len(parts) != 2:
            send_line(line_bot_api, event, messages=[TextMessage(text="格式錯誤，請重新選擇門診時段。")], label="clinic_period_bad_format")
            return

        cmd, date_str = parts[0], parts[1]
        if cmd == "門診早":
            period = "morning"
        elif cmd == "門診晚":
            period = "evening"
        else:
            period = None
        # cmd, date_str = parts[0], parts[1]
        # period = "morning" if cmd == "門診早" else "evening"

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if not line_user_id:
            send_line(line_bot_api, event, messages=[TextMessage(text="系統未取得您的帳號資訊，請稍後再試。")], label="clinic_period_missing_uid")
            return

        # 防繞過：手動輸入「門診早/晚」也要已建檔
        if not is_registered_patient(line_user_id):
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")],
                label="clinic_period_require_registered",
            )
            return
        
        if has_existing_clinic_period_booking(line_user_id, date_str, period):
            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(
                        text="您已預約此時段門診，當天請準時到診所報到。\n\n若要查看已預約內容，請點選下方按鈕。",
                        quick_reply=QuickReply(
                            items=[
                                QuickReplyItem(
                                    action=MessageAction(label="約診查詢", text="約診查詢")
                                ),
                            ]
                        ),
                    )
                ],
                label="clinic_period_duplicate",
            )
            return

        start_loading_animation(line_bot_api, line_user_id, loading_seconds=15, timeout=(1, 2))


        # 只取一次（隱藏用）
        time_str = pick_first_available_clinic_time(
            date_str=date_str,
            period=period,
            business_id=BOOKINGS_BUSINESS_CLINIC_ID,
        )
        if not time_str:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text=f"{date_str} 此時段已滿，請改選其他日期或時段。")],
                label="clinic_period_full",
            )
            return

        # 存 state（門診建立預約要靠 postback step 控制）
        state = get_state(line_user_id) or {}
        state["booking_type"] = "clinic"
        state["step"] = "wait_confirm_clinic_period"
        state["clinic_period"] = period
        state["clinic_date"] = date_str
        set_state(line_user_id, state)

        display_date = date_str.replace("-", "/")
        period_label = "早診" if period == "morning" else "晚診"

        # postback data 夾帶 HH:MM（只給後端用）
        # data = f"CLINIC_CONFIRM|{date_str}|{time_str}|{period}"

        # buttons = ButtonsTemplate(
        #     title="確認預約",
        #     text=f"請確認是否預約：\n{display_date}（{period_label}）",
        #     actions=[
        #         PostbackAction(label="確認預約", data=f"CLINIC_CONFIRM|{date}|{period}",),
        #         MessageAction(label="重新選擇", text=f"預約 {date_str}"),
        #     ],
        # )
        data = f"CLINIC_CONFIRM|{date_str}|{time_str}|{period}"

        buttons = ButtonsTemplate(
            title="確認預約",
            text=f"請確認是否預約：\n{display_date}（{period_label}）",
            actions=[
                PostbackAction(label="確認預約", data=data),  # 用 data
                MessageAction(label="取消", text="取消預約流程")
            ],
        )

        send_line(
            line_bot_api,
            event,
            messages=[TemplateMessage(alt_text="確認預約", template=buttons)],
            label="clinic_period_confirm_card",
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

            evt_id_safe = locals().get("evt_id") or getattr(event, "webhook_event_id", None) or "NA"
            
            print(f"[CONFIRM_APPT] parsed uid={line_user_id} date={date_str} time={time_str}", flush=True)

            if not line_user_id:
                print(f"[CONFIRM_APPT] missing_uid evt_id={evt_id_safe} date={date_str} time={time_str}", flush=True)
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="系統未取得您的帳號資訊，請稍後再試或重新開啟對話。")],
                    label="confirm_appt_missing_uid",
                )
                return

            print(f"[CONFIRM_APPT] enter uid={line_user_id} evt_id={evt_id_safe} date={date_str} time={time_str}", flush=True)

            state = get_state(line_user_id) or {}
            booking_type = (state.get("booking_type") or "clinic").strip()
            
            bed = None
            print(f"[CONFIRM_APPT] type={booking_type} bed={bed}", flush=True)
            
            if booking_type == "acupuncture":
                wd = datetime.strptime(date_str, "%Y-%m-%d").date().weekday()
                bed = (ACU_SLOTS.get(wd) or {}).get(time_str)  # bed1 / bed2

                if not bed:
                    send_line(
                        line_bot_api,
                        event,
                        messages=[TextMessage(text="此時段不是針灸可預約時段，請重新選擇。")],
                        label="acu_invalid_time",
                    )
                    return

                lock_key = f"confirm:acupuncture:{line_user_id}:{date_str}:{time_str}:{bed}"
            else:
                lock_key = f"confirm:clinic:{line_user_id}:{date_str}:{time_str}"

            # bed = None
            # if booking_type == "acupuncture":
            #     wd = datetime.strptime(date_str, "%Y-%m-%d").date().weekday()
            #     bed = (ACU_SLOTS.get(wd) or {}).get(time_str)  # bed1 / bed2

            #     if not bed:
            #         send_line(
            #             line_bot_api,
            #             event,
            #             messages=[TextMessage(text="此時段不是針灸可預約時段，請重新選擇。")],
            #             label="acu_invalid_time",
            #         )
            #         return

            #     lock_key = f"confirm:acupuncture:{line_user_id}:{date_str}:{time_str}:{bed}"
            # else:
            #     lock_key = f"confirm:clinic:{line_user_id}:{date_str}:{time_str}"

            print(f"[CONFIRM_APPT] before_lock uid={line_user_id} key={lock_key}", flush=True)
            lock_ok = acquire_lock(lock_key, ttl_sec=30)
            print(f"[CONFIRM_APPT] after_lock uid={line_user_id} ok={lock_ok}", flush=True)

            if not lock_ok:
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="正在建立預約中，請勿重複點擊～\n若未收到預約成功訊息，請稍後再試或「約診查詢」。")],
                    label="duplication",
                )
                return

            # ④ 檢查該時段目前是否仍可預約（防止暴力輸入或已被別人搶走）
            t0 = time.time()

            if booking_type == "acupuncture":
                ok_slot = is_acu_slot_available(date_str, time_str)
            else:
                ok_slot = is_slot_available(date_str, time_str, business_id=BOOKINGS_BUSINESS_CLINIC_ID)


            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30)

            # ② 檢查是否已在 Zendesk 建檔（防止未建檔暴力確認）
            if not is_registered_patient(line_user_id):
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")],
                    label="cancelled",
                )
                return

            # ③ 檢查日期是否合法（三週內／非過去）
            ok, msg = validate_appointment_date(date_str)
            print(f"[CONFIRM_APPT] validate_date uid={line_user_id} ok={ok} msg={msg}", flush=True)

            if not ok:
                # line_bot_api.reply_message(
                #     ReplyMessageRequest(
                #         reply_token=event.reply_token,
                #         messages=[TextMessage(text=msg)],
                #     )
                # )
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text=msg)],
                    label="validate_appointment_date",
                )
                return


            # ④ 檢查該時段目前是否仍可預約（防止暴力輸入或已被別人搶走）
            t0 = time.time()

            if booking_type == "acupuncture":
                ok_slot = is_acu_slot_available(date_str, time_str)
            else:
                ok_slot = is_slot_available(date_str, time_str, business_id=BOOKINGS_BUSINESS_CLINIC_ID)

            print(f"[CONFIRM_APPT] slot_check uid={line_user_id} type={booking_type} ok={ok_slot} elapsed={time.time()-t0:.3f}s", flush=True)
            print(f"[CONFIRM_APPT] slot_check uid={line_user_id} date={date_str} time={time_str} type={booking_type} ok={ok_slot}", flush=True)


            # if not is_slot_available(date_str, time_str):
            if not ok_slot:
                send_line(
                    line_bot_api,
                    event,
                    messages=[TextMessage(text="很抱歉，您選擇的時段已滿或無法預約，請重新選擇其他時段。")],
                    label="date_full",
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
                    print(f"[CONFIRM_APPT] before_search_zd uid={line_user_id}", flush=True)
                    zd_count, zd_user = search_zendesk_user_by_line_id(line_user_id)
                    if zd_user:
                        # Zendesk 裡的 name / phone
                        zd_name = zd_user.get("name") or customer_name
                        zd_phone = zd_user.get("phone") or customer_phone
                        customer_name = zd_name
                        customer_phone = zd_phone
                        # 關鍵：從 Zendesk User 物件中取得 ID
                        zendesk_customer_id = zd_user.get("id")
                    print(f"[CONFIRM_APPT] after_search_zd uid={line_user_id} found={'Y' if zd_user else 'N'}", flush=True)


                except Exception as e:
                    app.logger.error(f"用 line_user_id 查 Zendesk user 失敗: {e}")

                # ⑦ 再嘗試拿 LINE 顯示名稱（例如 Kevin）
                print(f"[CONFIRM_APPT] before_get_profile uid={line_user_id}", flush=True)

                try:
                    print(f"[CONFIRM_APPT] before_get_profile uid={line_user_id}", flush=True)
                    profile = line_bot_api.get_profile(line_user_id)
                    if profile and hasattr(profile, "display_name"):
                        line_display_name = profile.display_name
                        print(f"[CONFIRM_APPT] after_get_profile uid={line_user_id} display={line_display_name}", flush=True)
                    else:
                        print(f"[CONFIRM_APPT] after_get_profile uid={line_user_id} display=None", flush=True)
                except Exception as e:
                    print(f"[CONFIRM_APPT] get_profile EXC uid={line_user_id} err={repr(e)}", flush=True)
                    app.logger.error(f"取得 LINE profile 失敗: {e}")

            # ⑧ 呼叫新的 create_booking_appointment（會寫入 LINE_USER 到 serviceNotes）

            print(f"[CONFIRM_APPT] before_create uid={line_user_id} evt_id={evt_id} date={date_str} time={time_str}", flush=True)

            # ⑧-0 若是針灸：依固定表決定床位 → 對應 serviceId

            staff_member_ids = None

            # bed 已在前面鎖的地方算過，這裡直接使用
            if booking_type == "acupuncture":
                if not bed:
                    send_line(
                        line_bot_api,
                        event,
                        messages=[TextMessage(text="此時段不是針灸可預約時段，請重新選擇。")],
                        label="acu_invalid_time",
                    )
                    return

                business_id = BOOKINGS_BUSINESS_ACU_ID
                service_id = BOOKINGS_SERVICE_ACU_BED_ID

                if bed == "bed1":
                    staff_member_ids = [ACU_STAFF_BED1_ID]
                    appt_category = ZENDESK_APPT_CATEGORY_ACU_BED1
                elif bed == "bed2":
                    staff_member_ids = [ACU_STAFF_BED2_ID]
                    appt_category = ZENDESK_APPT_CATEGORY_ACU_BED2
                else:
                    send_line(
                        line_bot_api,
                        event,
                        messages=[TextMessage(text="針灸床位設定異常，請聯繫診所協助。")],
                        label="acu_bed_config_error",
                    )
                    return
            
            else:
                service_id = BOOKINGS_DEMO_SERVICE_ID
                business_id = BOOKINGS_BUSINESS_CLINIC_ID

            print(f"[ACU_CREATE] uid={line_user_id} type={booking_type} business_id={business_id} service_id={service_id} staff={staff_member_ids} date={date_str} time={time_str}", flush=True)  
            try:
                created = create_booking_appointment(
                    date_str=date_str,
                    time_str=time_str,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    zendesk_customer_id=zendesk_customer_id,
                    line_display_name=line_display_name,
                    line_user_id=line_user_id,
                    business_id=business_id,                 # 新增
                    service_id=service_id,                   # 針灸固定 service / 內科用原本的 service（若有）
                    staff_member_ids=staff_member_ids,       # 針灸帶床位 staff
                )
                bid = created.get("id") if isinstance(created, dict) else None
                print(f"[CONFIRM_APPT] after_create uid={line_user_id} booking_id={bid} created_type={type(created).__name__}", flush=True)

                appt_id = created.get("id", "（沒有取得 ID）")
                # ===== DEBUG：強制走 notes 兜底（只在本機測試用）=====
                if FORCE_ZD_ID_FROM_NOTES:
                    app.logger.info(
                        "[debug] FORCE_ZD_ID_FROM_NOTES=1 -> ignore zendesk_customer_id and recover from notes"
                    )
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
                        zid_source = None
                        if zendesk_customer_id:
                            try:
                                zid = int(zendesk_customer_id)
                                zid_source = "param"
                            except ValueError:
                                app.logger.error(
                                    f"[handle_message] Zendesk User ID 不是整數: {zendesk_customer_id}，改用 serviceNotes 取Zendesk User ID"
                                )
                                zid = None
                                zid_source = None

                        # 2) 再用 serviceNotes recover
                        if not zid:
                            recovered = extract_zd_user_id_from_service_notes(
                                created.get("serviceNotes")
                            )
                            if recovered:
                                zid = recovered
                                zid_source = "notes"
                                app.logger.info(
                                    f"[handle_message] 從 serviceNotes 取得 Zendesk User ID: {zid}"
                                )

                        # 3) 決定要不要建票
                        if not zid:
                            app.logger.warning(
                                "[handle_message] 未取得 Zendesk User ID（含 serviceNotes），跳過建立預約 Ticket 流程。"
                            )
                        else:
                            # 用使用者剛選的本地時間組一個 datetime，當作門診時間
                            app.logger.info(
                                f"[ticket][zid] source={zid_source} zid={zid} booking_id={booking_id}"
                            )
                            local_start_dt = datetime.strptime(
                                f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                            )

                            ticket_result = create_zendesk_appointment_ticket(
                                booking_id=booking_id,
                                local_start_dt=local_start_dt,
                                zendesk_customer_id=zid,
                                customer_name=customer_name,
                                booking_service_name="針灸",
                                appt_category=appt_category,
                                booking_type="acupuncture",
                                business_id=business_id,
                                line_user_id=line_user_id,
                                bed=bed,
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
                    f"時段：{display_date} {time_str}\n\n"
                    "請在三天前回到 LINE 上確認您是否會如期前來。"
                )

                buttons_template = ButtonsTemplate(
                    title="診所位置",
                    text="如需導航，請點選下方按鈕。",
                    actions=[MessageAction(label="位置導航", text="查詢診所位置")],
                )

                print(f"[CONFIRM_APPT] before_send_success uid={line_user_id} booking_id={bid}", flush=True)
                send_line(
                    line_bot_api, event,
                    messages=[
                        TextMessage(text=detail_text),
                        TemplateMessage(alt_text="診所位置導航", template=buttons_template),
                    ],
                    label="confirm_appt_success",
                    timeout=(3, 10),
                    push_timeout=(3, 10),
                )
                print(f"[CONFIRM_APPT] after_send_success uid={line_user_id}", flush=True)

                return

            except Exception as e:
                print(f"[CONFIRM_APPT] create EXC uid={line_user_id} err={repr(e)}", flush=True)
                app.logger.error(f"建立 Bookings 預約失敗: {e}")
                reply_text = "未成功預約，請重新操作"

        else:
            reply_text = "格式：確認預約 YYYY-MM-DD HH:MM"

        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token,
        #         messages=[TextMessage(text=reply_text)],
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text=reply_text)],
            label="reply_text",
        )
        return

    # === 約診查詢 ===
    elif text == "約診查詢":
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)

        # 立刻顯示 loading
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )
        return flow_query_next_appointment(event, text)

    # === ⑤-1 取消約診 ===
    elif text.startswith("取消約診"):
        return flow_cancel_request(event, text)

    elif text.startswith("確認取消"):
        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token,
        #         messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")],
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")],
            label="need_check_appointment_cancel",
        )
        return

    # === ⑦ 確認回診 ===
    elif text.startswith("確認回診"):
        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token,
        #         messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")],
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text="請先點選「約診查詢」確認約診狀態。")],
            label="need_check_appointment_confirm",
        )
        return

    # === 查詢診所位置 ===
    elif text == "查詢診所位置":
        location_message = LocationMessage(
            title=CLINIC_NAME,
            address=CLINIC_ADDRESS,
            latitude=CLINIC_LAT,
            longitude=CLINIC_LNG,
        )
        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token, messages=[location_message]
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[location_message],
            label="location_message",
        )
        return

    # === 診所資訊 ===
    elif text == "診所資訊":
        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(
                line_bot_api,
                line_user_id,
                loading_seconds=30,
                timeout=(1, 2),
            )
        short_text = f"地址：{CLINIC_ADDRESS}\n點擊下方查看地圖位置"

        clinic_info_template = ButtonsTemplate(
            thumbnail_image_url=CLINIC_IMAGE_URL,
            title=CLINIC_NAME,
            text=short_text,
            actions=[MessageAction(label="查看地圖位置", text="查看地圖位置")],
        )

        opening_hours_message = TextMessage(
            text=(
                "門診時間：\n"
                "週一：9:00 ～ 12:30 | 16:30 ~ 20:00\n"
                "週二：9:00 ～ 12:30 | 16:30 ~ 20:00\n"
                "週三：休診\n"
                "週四：9:00 ～ 12:30 | 16:30 ~ 20:00\n"
                "週五：9:00 ～ 12:30\n"
                "週六：9:00 ～ 12:30\n"
                "週日：休診"
            )
        )

        location_message = LocationMessage(
            title=CLINIC_NAME,
            address=CLINIC_ADDRESS,
            latitude=CLINIC_LAT,
            longitude=CLINIC_LNG,
        )

        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token,
        #         messages=[
        #             TemplateMessage(alt_text="診所資訊", template=clinic_info_template),
        #             opening_hours_message,
        #             location_message,
        #         ],
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[
                TemplateMessage(alt_text="診所資訊", template=clinic_info_template),
                opening_hours_message,
                location_message,
            ],
            label="clinic_info_bundle",
        )

        return

    # === 查看地圖位置 ===
    elif text == "查看地圖位置":
        location_message = LocationMessage(
            title=CLINIC_NAME,
            address=CLINIC_ADDRESS,
            latitude=CLINIC_LAT,
            longitude=CLINIC_LNG,
        )
        # line_bot_api.reply_message(
        #     ReplyMessageRequest(
        #         reply_token=event.reply_token, messages=[location_message]
        #     )
        # )
        send_line(
            line_bot_api,
            event,
            messages=[location_message],
            label="clinic_location_only",
        )

        return

    # === fallback：使用者直接輸入手機，但尚未進入任何流程 ===
    if uid:
        digits = normalize_phone(text)
        # ✅ Redis：若目前沒有任何 pending state，才走 fallback-phone
        st = get_state(uid)
        print(f"[fallback-phone] check uid={uid} digits={digits} has_state={'Y' if (st and st.get('step')) else 'N'}", flush=True)
        if (
            len(digits) == 10
            and digits.startswith("09")
            and (not st or not st.get("step"))
        ):
            app.logger.info(f"[fallback-phone] uid={uid} digits={digits}")
            print(f"[fallback-phone] uid={uid} digits={digits} st={st}", flush=True)

            send_line(
                line_bot_api,
                event,
                messages=[
                    TextMessage(text="請先點選「線上約診」，並點擊「開始輸入」後再輸入手機。")
                ],
                label="fallback_phone",
                timeout=(3, 10),
                push_timeout=(3, 10),
            )

            return

    # === 其他訊息（最後 default 回覆） ===
    app.logger.info("非線上約診相關指令，請真人回覆")
    send_line(
        line_bot_api,
        event,
        messages=[TextMessage(text="非「線上約診」相關訊息，請稍候由專人為您回覆喔！")],
        label="remind_message",
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
                messages=[
                    TextMessage(text="已為您取消流程。需要預約請再輸入「線上約診」。")
                ],
            )
        )
        return

    # ===== 新增：同意開始輸入手機 =====
    if data == "CONSENT_PHONE":

        if not line_user_id:
            return

        print(f"[PB] CONSENT_PHONE enter uid={line_user_id}", flush=True)

        state = get_state(line_user_id) or {}
        step = (state.get("step") or "").strip()

        print(f"[PB] CONSENT_PHONE before_enter step={step} state={state}", flush=True)

        # 只允許在「等同意輸入手機 / 已在輸入手機」時推進，避免舊卡片把流程拉回去
        if step and step not in {"wait_consent_phone", "ask_phone"}:

            print(f"[PB] CONSENT_PHONE blocked step={step}", flush=True)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="您目前正在填寫其他資料。如需重新開始，請輸入「取消」。")],
                )
            )
            return

        print(f"[PB] CONSENT_PHONE calling enter_input_step uid={line_user_id}", flush=True)

        start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        enter_input_step(
            line_bot_api=line_bot_api,
            event=event,
            line_user_id=line_user_id,
            step="ask_phone",
            prompt_text="好的，請輸入您的手機號碼（09xxxxxxxx）：",
        )

        # 進一步確認 set_state 後 step 是否真的變 ask_phone
        state2 = get_state(line_user_id) or {}
        step2 = (state2.get("step") or "").strip()
        print(f"[PB] CONSENT_PHONE after_enter step={step2} state={state2}", flush=True)
        return

    # ===== 同意開始輸入姓名（手機已確認後補姓名）=====
    if data == "CONSENT_NAME_AFTER_PHONE":
        if not line_user_id:
            return

        print(f"[PB] CONSENT_NAME_AFTER_PHONE enter uid={line_user_id} data={data}", flush=True)

        state = get_state(line_user_id) or {}

        print(f"[PB] CONSENT_NAME_AFTER_PHONE state step={(state.get('step') or '').strip()} state={state}", flush=True)

        # state 不見也能重建（避免 bad_state / Redis TTL / worker restart）
        if not state:
            try:
                count, user = search_zendesk_user_by_line_id(line_user_id, retries=1)
            except Exception as e:
                app.logger.error(
                    f"[CONSENT_NAME_AFTER_PHONE][fallback] search failed uid={line_user_id} err={e}"
                )
                user = None

            if user:
                state = {
                    "step": "wait_consent_name_after_phone",
                    "zendesk_user_id": user.get("id"),
                    "phone": normalize_phone(user.get("phone") or ""),
                }
                set_state(line_user_id, state)
                app.logger.info(
                    f"[CONSENT_NAME_AFTER_PHONE][fallback] rebuilt uid={line_user_id} "
                    f"user_id={state.get('zendesk_user_id')} phone={state.get('phone')}"
                )
            else:
                app.logger.warning(
                    f"[CONSENT_NAME_AFTER_PHONE] fallback_not_found uid={line_user_id}"
                )
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(text="找不到您的資料，請重新輸入「線上約診」。")
                        ],
                    )
                )
                return

        step = (state.get("step") or "").strip()

        # 允許兩種狀態：
        # 1) wait_consent_name_after_phone：按同意後才開始輸入
        # 2) ask_name_after_phone：代表已進入輸入狀態了（不要重設 state，只提醒他輸入）
        if step not in {"wait_consent_name_after_phone", "ask_name_after_phone"}:
            app.logger.warning(
                f"[CONSENT_NAME_AFTER_PHONE] bad_state uid={line_user_id} state={state}"
            )
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text="流程狀態異常，請重新輸入「線上約診」。")
                    ],
                )
            )
            return

        # 已經在 ask_name_after_phone：只提醒輸入姓名，不要重設 state
        if step == "ask_name_after_phone":
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text="好的，請直接輸入您的真實姓名（全名）：")
                    ],
                )
            )
            return

        # step == wait_consent_name_after_phone → 正常進入輸入
        zendesk_user_id = state.get("zendesk_user_id")
        phone = state.get("phone")

        print(f"[PB] CONSENT_NAME_AFTER_PHONE proceed uid={line_user_id} -> enter_input_step ask_name_after_phone", flush=True)

        # 不新增 loading（保留原本的樣子）
        start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        enter_input_step(
            line_bot_api=line_bot_api,
            event=event,
            line_user_id=line_user_id,
            step="ask_name_after_phone",
            prompt_text="好的，請輸入您的真實姓名（全名）：",
            extra_state={"zendesk_user_id": zendesk_user_id, "phone": phone},
        )
        return

    # ===== 門診早/晚：確認預約（用 postback，病患看不到時間）=====
    if data.startswith("CLINIC_CONFIRM|"):
        if not line_user_id:
            print("[PB] CLINIC_CONFIRM missing line_user_id", flush=True)
            return

        parts = data.split("|")
        if len(parts) != 4:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="資料格式異常，請重新從「線上約診」開始。")],
                label="clinic_confirm_bad_data",
            )
            return

        _, date_str, time_str, period = parts

        state = get_state(line_user_id) or {}
        step = (state.get("step") or "").strip()
        print(f"[PB] CLINIC_CONFIRM state step={step} state={state}", flush=True)

        if step != "wait_confirm_clinic_period":
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="此按鈕已過期，請重新從「線上約診」開始。")],
                label="clinic_confirm_step_mismatch",
            )
            return
        
        # 防重複點擊 / 防重複建立（與「確認預約」共用同邏輯）
        lock_key = f"confirm:clinic:{line_user_id}:{date_str}:{time_str}"
        print(f"[PB] CLINIC_CONFIRM before_lock uid={line_user_id} key={lock_key}", flush=True)
        lock_ok = acquire_lock(lock_key, ttl_sec=30)
        print(f"[PB] CLINIC_CONFIRM after_lock uid={line_user_id} ok={lock_ok}", flush=True)

        if not lock_ok:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="正在建立預約中，請勿重複點擊～\n若未收到預約成功訊息，請稍後再試或「約診查詢」。")],
                label="clinic_confirm_duplication",
            )
            return

        start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        if not is_registered_patient(line_user_id):
            clear_state(line_user_id)
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")],
                label="clinic_confirm_invalid_patient",
            )
            return

        ok, msg = validate_appointment_date(date_str)
        if not ok:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text=msg)],
                label="clinic_confirm_invalid_date",
            )
            return

        # 防搶位：最後一刻再檢查 slot
        ok_slot = is_slot_available(date_str, time_str, business_id=BOOKINGS_BUSINESS_CLINIC_ID)
        if not ok_slot:
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="很抱歉，該時段剛剛已額滿，請重新選擇其他日期或時段。")],
                label="clinic_confirm_slot_taken",
            )
            return

        # 取 Zendesk user + LINE display（沿用confirm 預約那段邏輯）
        customer_name = DEMO_CUSTOMER_NAME
        customer_phone = DEMO_CUSTOMER_PHONE
        line_display_name = None
        zendesk_customer_id = None

        try:
            zd_count, zd_user = search_zendesk_user_by_line_id(line_user_id)
            if zd_user:
                customer_name = zd_user.get("name") or customer_name
                customer_phone = zd_user.get("phone") or customer_phone
                zendesk_customer_id = zd_user.get("id")
        except Exception as e:
            app.logger.error(f"[CLINIC_CONFIRM] search_zendesk_user_by_line_id failed: {e}")

        try:
            profile = line_bot_api.get_profile(line_user_id)
            if profile and hasattr(profile, "display_name"):
                line_display_name = profile.display_name
        except Exception as e:
            app.logger.error(f"[CLINIC_CONFIRM] get_profile failed: {e}")

        # 建 Bookings（門診 business/service 用現有常數）
        business_id = BOOKINGS_BUSINESS_CLINIC_ID
        service_id = BOOKINGS_DEMO_SERVICE_ID  # 目前門診用這個（若有正式 service_id 就換掉）
        staff_member_ids = None

        created = create_booking_appointment(
            date_str=date_str,
            time_str=time_str,
            customer_name=customer_name,
            customer_phone=customer_phone,
            zendesk_customer_id=zendesk_customer_id,
            line_display_name=line_display_name,
            line_user_id=line_user_id,
            business_id=business_id,
            service_id=service_id,
            staff_member_ids=staff_member_ids,
        )

        if not isinstance(created, dict) or not created.get("id"):
            app.logger.error(f"[CLINIC_CONFIRM] create_booking_appointment failed created={created}")
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="預約建立失敗，請稍後再試或改選其他時段。")],
                label="clinic_confirm_create_failed",
            )
            return

        booking_id = created.get("id")
        appt_category = ZENDESK_APPT_CATEGORY_CLINIC

        # 必做：建 Zendesk ticket（沿用原本 recover notes 的邏輯）
        try:
            zid = None
            if zendesk_customer_id:
                try:
                    zid = int(zendesk_customer_id)
                except ValueError:
                    zid = None

            if not zid:
                recovered = extract_zd_user_id_from_service_notes(created.get("serviceNotes"))
                if recovered:
                    zid = recovered

            if booking_id and zid:
                local_start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                create_zendesk_appointment_ticket(
                    booking_id=booking_id,
                    local_start_dt=local_start_dt,
                    zendesk_customer_id=zid,
                    customer_name=customer_name,
                    booking_service_name="門診",
                    appt_category=appt_category,
                    booking_type="clinic",
                    business_id=BOOKINGS_BUSINESS_CLINIC_ID,
                    line_user_id=line_user_id,
                    clinic_period=period,
                )
        except Exception as e:
            app.logger.error(f"[CLINIC_CONFIRM] create_zendesk_ticket failed (non-blocking): {e}")

        # ✅ 回覆病患：門診不得顯示 HH:MM
        display_date = date_str.replace("-", "/")
        period_label = "早診" if period == "morning" else "晚診"

        display_name = customer_name
        if line_display_name:
            display_name = f"{customer_name}（{line_display_name}）"

        detail_text = (
            "已為您完成預約，請準時報到。\n"
            f"姓名：{display_name}\n"
            f"時段：{display_date}（{period_label}）\n\n"
            "請在三天前回到 LINE 上確認您是否會如期前來。"
        )

        buttons_template = ButtonsTemplate(
            title="診所位置",
            text="如需導航，請點選下方按鈕。",
            actions=[MessageAction(label="位置導航", text="查詢診所位置")],
        )

        clear_state(line_user_id)

        send_line(
            line_bot_api,
            event,
            messages=[
                TextMessage(text=detail_text),
                TemplateMessage(alt_text="診所位置導航", template=buttons_template),
            ],
            label="clinic_confirm_success",
            timeout=(3, 10),
            push_timeout=(3, 10),
        )
        return

        # 原本放在這裡的 print 永遠跑不到（因為上面已 return）
        # 把它搬到最後的 else，讓它真的會印。

    # ===== 針灸同意書：同意後繼續 =====
    if data == "CONSENT_ACU_TERMS":

        if not line_user_id:
            return

        print(f"[PB] CONSENT_ACU_TERMS enter uid={line_user_id} data={data}", flush=True)

        state = get_state(line_user_id) or {}
        step = (state.get("step") or "").strip()

        print(f"[PB] CONSENT_ACU_TERMS state step={step} state={state}", flush=True)

        # 只允許在對的 step 才推進，避免舊卡片把流程拉回來
        if step != "wait_consent_acu_terms":
            # 不新增 loading（保留原本的）
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=10, timeout=(1, 2))
            send_line(
                line_bot_api,
                event,
                messages=[TextMessage(text="此按鈕已逾時或流程已變更，請重新輸入「線上約診」。")],
                label="acu_terms_wrong_step",
            )
            return

        # 同意後：正式進入針灸預約流程 → 進日期範圍
        state["step"] = None
        state["booking_type"] = "acupuncture"
        state.pop("booking_type_pending", None)
        set_state(line_user_id, state)

        # 不新增 loading（保留原本的）
        start_loading_animation(line_bot_api, line_user_id, loading_seconds=15, timeout=(1, 2))
        reply_date_range_buttons(event, "已同意針灸注意事項。\n請選擇要預約的日期範圍：")
        return

  # ===== 約診 postback 邏輯 =====

    # 0) NOOP（已確認的卡片用空白按鈕）
    if data == "NOOP":
        return

    # 1) 確認回診（新格式）
    elif data.startswith("CONFIRM_APPT|"):
        _, biz, appt_id = data.split("|", 2)
        appt_id = appt_id.strip()
        biz = biz.strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"確認回診 {appt_id}"
        return flow_confirm_visit(event, fake_text, business_id=biz)

    # 1-legacy) 確認回診（舊格式）
    elif data.startswith("CONFIRM_APPT:"):
        appt_id = data.split(":", 1)[1].strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"確認回診 {appt_id}"
        return flow_confirm_visit(event, fake_text)  # 舊格式 → flow 內 fallback 雙試

    # 2) 取消約診（第一階段：跳出確認取消畫面）（新格式）
    elif data.startswith("CANCEL_APPT|"):
        _, biz, appt_id = data.split("|", 2)
        appt_id = appt_id.strip()
        biz = biz.strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"取消約診 {appt_id}"
        return flow_cancel_request(event, fake_text, business_id=biz)

    # 2-legacy) 取消約診（第一階段）（舊格式）
    elif data.startswith("CANCEL_APPT:"):
        appt_id = data.split(":", 1)[1].strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"取消約診 {appt_id}"
        return flow_cancel_request(event, fake_text)

    # 3) 確認取消（第二階段：真的取消）（新格式）
    elif data.startswith("CANCEL_CONFIRM|"):
        _, biz, appt_id = data.split("|", 2)
        appt_id = appt_id.strip()
        biz = biz.strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"確認取消 {appt_id}"
        return flow_confirm_cancel(event, fake_text, business_id=biz)

    # 3-legacy) 確認取消（舊格式）
    elif data.startswith("CANCEL_CONFIRM:"):
        appt_id = data.split(":", 1)[1].strip()

        line_user_id = getattr(getattr(event, "source", None), "user_id", None)
        if line_user_id:
            start_loading_animation(line_bot_api, line_user_id, loading_seconds=30, timeout=(1, 2))

        fake_text = f"確認取消 {appt_id}"
        return flow_confirm_cancel(event, fake_text)  # 舊格式 → flow 內 fallback 雙試

    # 4) 保留約診
    elif data == "CANCEL_KEEP":
        send_line(
            line_bot_api,
            event,
            messages=[TextMessage(text="已為您保留原本的約診，謝謝。")],
            label="cancel_keep",
        )
        return

    # 其他沒處理到的 Postback
    else:
        app.logger.warning(f"未處理的 Postback data: {data}")
        uid = getattr(getattr(event, "source", None), "user_id", None)
        print(f"[POSTBACK] unhandled uid={uid} data={data}", flush=True)
        return

    # === fallback：使用者直接輸入手機，但尚未進入任何流程 ===


#     # === fallback：使用者直接輸入手機，但尚未進入任何流程 ===


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


# 外撥回寫
@app.route("/webhook/livehub", methods=["POST"])
def webhook_livehub():
    data = request.get_json(silent=True) or {}
    app.logger.info(f"[livehub_webhook] received: {data}")

    try:
        handle_livehub_webhook(data)
    except Exception as e:
        app.logger.error(f"[livehub_webhook] handle failed: {e}")

    return jsonify({"status": "ok"}), 200



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


@app.route("/cron/run-reminder-fail", methods=["GET"])
def cron_run_reminder_fail():
    days = int(request.args.get("days", "1"))
    min_attempts = int(request.args.get("min_attempts", "3"))
    dry_run = request.args.get("dry_run", "0") in ("1", "true", "True", "yes", "Y")

    result = run_fail_queued_tickets(
        days=days, min_attempts=min_attempts, dry_run=dry_run
    )
    return result, 200


@app.get("/ping")
def ping():
    return {"ok": True, "ts": time.time()}, 200

#替換區
        # # Case 3：查無此人，不是診所既有的病患，不開放初診線上預約
        # print(f"[FLOW] no_zendesk_user uid={uid} line_user_id={line_user_id}", flush=True)
        # start_loading_animation(
        #     line_bot_api,
        #     line_user_id,
        #     loading_seconds=15,
        #     timeout=(1, 2),
        # )

        # send_line(
        #     line_bot_api,
        #     event,
        #     messages=[TextMessage(text="若您是初診，請先至現場掛號完成資料建檔；建檔後即可使用線上預約服務，謝謝您。")],
        #     label="new_patient_blocked",
        # )


    # if data == "CONSENT_NAME_AFTER_PHONE":
    #     if not line_user_id:
    #         return

    #     state = get_state(line_user_id)
    #     step = state.get("step")

    #     # 允許兩種狀態：
    #     # 1) wait_consent_name_after_phone：按同意後才開始輸入
    #     # 2) ask_name_after_phone：代表已經進入輸入狀態了（就不要再改 state，只提示他輸入）
    #     if step not in {"wait_consent_name_after_phone", "ask_name_after_phone"}:
    #         app.logger.warning(
    #             f"[CONSENT_NAME_AFTER_PHONE] bad_state uid={line_user_id} state={state}"
    #         )
    #         line_bot_api.reply_message(
    #             ReplyMessageRequest(
    #                 reply_token=event.reply_token,
    #                 messages=[TextMessage(text="流程狀態異常，請重新輸入「線上約診」。")]
    #             )
    #         )
    #         return

    #     # 已經在 ask_name_after_phone，就只提醒輸入姓名，不要重設 state
    #     if step == "ask_name_after_phone":
    #         line_bot_api.reply_message(
    #             ReplyMessageRequest(
    #                 reply_token=event.reply_token,
    #                 messages=[TextMessage(text="好的，請直接輸入您的真實姓名（全名）：")]
    #             )
    #         )
    #         return

    #     # step == wait_consent_name_after_phone → 正常進入輸入
    #     zendesk_user_id = state.get("zendesk_user_id")
    #     phone = state.get("phone")

    #     enter_input_step(
    #         line_bot_api=line_bot_api,
    #         event=event,
    #         line_user_id=line_user_id,
    #         step="ask_name_after_phone",
    #         prompt_text="好的，請輸入您的真實姓名（全名）：",
    #         extra_state={
    #             "zendesk_user_id": zendesk_user_id,
    #             "phone": phone,
    #         },
    #     )
    #     return

    # if data == "CONSENT_NAME_AFTER_PHONE":
    #     if not line_user_id:
    #         return

    #     state = get_state(line_user_id)

    #     # ✅ fallback：state 不見也能重建（避免 bad_state）
    #     if not state:
    #         try:
    #             count, user = search_zendesk_user_by_line_id(line_user_id, retries=1)
    #         except Exception as e:
    #             app.logger.error(f"[CONSENT_NAME_AFTER_PHONE][fallback] search failed uid={line_user_id} err={e}")
    #             user = None

    #         if user:
    #             state = {
    #                 "step": "wait_consent_name_after_phone",  # 讓下面流程吃得到
    #                 "zendesk_user_id": user.get("id"),
    #                 "phone": normalize_phone(user.get("phone") or ""),
    #             }
    #             set_state(line_user_id, state)
    #             app.logger.info(
    #                 f"[CONSENT_NAME_AFTER_PHONE][fallback] rebuilt uid={line_user_id} "
    #                 f"user_id={state.get('zendesk_user_id')} phone={state.get('phone')}"
    #             )
    #         else:
    #             app.logger.warning(f"[CONSENT_NAME_AFTER_PHONE] fallback_not_found uid={line_user_id}")
    #             line_bot_api.reply_message(
    #                 ReplyMessageRequest(
    #                     reply_token=event.reply_token,
    #                     messages=[TextMessage(text="找不到您的資料，請重新輸入「線上約診」。")]
    #                 )
    #             )
    #             return

    #     step = state.get("step")

    #     if step not in {"wait_consent_name_after_phone", "ask_name_after_phone"}:
    #         app.logger.warning(f"[CONSENT_NAME_AFTER_PHONE] bad_state uid={line_user_id} state={state}")
    #         line_bot_api.reply_message(
    #             ReplyMessageRequest(
    #                 reply_token=event.reply_token,
    #                 messages=[TextMessage(text="流程狀態異常，請重新輸入「線上約診」。")]
    #             )
    #         )
    #         return

    #     if step == "ask_name_after_phone":
    #         line_bot_api.reply_message(
    #             ReplyMessageRequest(
    #                 reply_token=event.reply_token,
    #                 messages=[TextMessage(text="好的，請直接輸入您的真實姓名（全名）：")]
    #             )
    #         )
    #         return

    #     zendesk_user_id = state.get("zendesk_user_id")
    #     phone = state.get("phone")

    #     enter_input_step(
    #         line_bot_api=line_bot_api,
    #         event=event,
    #         line_user_id=line_user_id,
    #         step="ask_name_after_phone",
    #         prompt_text="好的，請輸入您的真實姓名（全名）：",
    #         extra_state={"zendesk_user_id": zendesk_user_id, "phone": phone},
    #     )
    #     return

            # lock_key = f"confirm:{line_user_id}:{date_str}:{time_str}"
            # print(f"[CONFIRM_APPT] before_lock uid={line_user_id} key={lock_key}", flush=True)
            # lock_ok = acquire_lock(lock_key, ttl_sec=30)
            # print(f"[CONFIRM_APPT] after_lock uid={line_user_id} ok={lock_ok}", flush=True)
            # if not lock_ok:
            #     # line_bot_api.reply_message(
            #     #     ReplyMessageRequest(
            #     #         reply_token=event.reply_token,
            #     #         messages=[
            #     #             TextMessage(
            #     #                 text="正在建立預約中，請勿重複點擊～\n若未收到預約成功訊息，請稍後再試或「約診查詢」。"
            #     #             )
            #     #         ],
            #     #     )
            #     # )
            #     send_line(
            #         line_bot_api,
            #         event,
            #         messages=[TextMessage(text="正在建立預約中，請勿重複點擊～\n若未收到預約成功訊息，請稍後再試或「約診查詢」。")],
            #         label="duplication",
            #     )
            #     return