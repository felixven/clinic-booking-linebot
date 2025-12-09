from flask import current_app as app

from line_client import line_bot_api

from linebot.v3.messaging import (
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    MessageAction,
)
from linebot.v3.webhooks import MessageEvent

from patient_core import (
    create_zendesk_user,
    search_zendesk_user_by_line_id,
)

from config import WEEK_IMAGE_URL

def handle_cancel_registration(
    event: MessageEvent,
    text: str,
    line_user_id_for_state: str | None,
    pending_registrations: dict,
) -> bool:
    """
    處理「取消建檔」邏輯。
    回傳 True 表示這個函式已經處理並回覆使用者，呼叫端可以 return。
    """
    ...


def handle_pending_registration_steps(
    event: MessageEvent,
    text: str,
    line_user_id_for_state: str | None,
    pending_registrations: dict,
) -> bool:
    """
    處理 PENDING_REGISTRATIONS 狀態機（ask_name / ask_phone）。
    回傳 True 表示已處理。
    """
    ...


def flow_online_booking_entry(
    event: MessageEvent,
    text: str,
    pending_registrations: dict,
) -> bool:
    """
    處理「線上約診」正式入口（老病患 / 新病患啟動建檔）。
    回傳 True 表示已處理。
    """
    ...


def flow_test_identity(
    event: MessageEvent,
    text: str,
    pending_registrations: dict,
) -> bool:
    """
    處理「測試身分」指令。
    回傳 True 表示已處理。
    """
    ...
