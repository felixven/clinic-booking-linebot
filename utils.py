from linebot.v3.messaging import (
    ReplyMessageRequest,
    TemplateMessage,
    ButtonsTemplate,
    PostbackAction,
    TextMessage,
)

from line_client import line_bot_api
from config import (
    PENDING_REGISTRATIONS,
    CONFIRM_OPEN_DAYS_BEFORE, # 原本 2，現在 +1
    CANCEL_DEADLINE_DAYS_BEFORE,
    ZENDESK_UF_LINE_USER_ID_KEY,
    ZENDESK_UF_PROFILE_STATUS_KEY,
    PROFILE_STATUS_COMPLETE
    )

from patient_core import normalize_phone

from datetime import datetime, timedelta, date

from config import is_valid_name

from flask import current_app as app

from state_store import clear_state


def parse_ticket_ids(raw_ticket_ids):
    """
    將各種可能型態的 ticketIds 轉成 list[int]

    支援：
    - [1140, 1139]
    - ["1140", "1139"]
    - "1140,1139"
    - "1140"
    - None / "" / 不合法 → []
    """
    if not raw_ticket_ids:
        return []

    # case 1: list / tuple / set
    if isinstance(raw_ticket_ids, (list, tuple, set)):
        ids = []
        for x in raw_ticket_ids:
            try:
                ids.append(int(x))
            except Exception:
                continue
        return ids

    # case 2: string "1140,1139"
    if isinstance(raw_ticket_ids, str):
        parts = raw_ticket_ids.split(",")
        ids = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except Exception:
                continue
        return ids

    # fallback
    return []


def reply_consent_input(*, line_bot_api, event, title: str, text: str, ok_data: str, cancel_data: str = "CANCEL_FLOW"):
    """
    回覆一張「同意開始輸入 / 取消」的按鈕卡。
    - 使用 PostbackAction
    - display_text="" → 不顯示使用者文字
    """
    buttons = ButtonsTemplate(
        title=title,
        text=text,
        actions=[
            PostbackAction(label="好的，我要開始輸入", data=ok_data),
            PostbackAction(label="取消預約", data=cancel_data),
        ],
    )
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TemplateMessage(alt_text=title, template=buttons)],
        )
    )

def enter_input_step(*, line_bot_api, pending_dict: dict, event, line_user_id: str, step: str, prompt_text: str, extra_state: dict | None = None):
    """
    進入某個輸入 step，並立刻回一則文字提示「可以開始輸入」。
    """
    state = {"step": step}
    if extra_state:
        state.update(extra_state)
    pending_dict[line_user_id] = state

    app.logger.info(f"[enter_input_step] uid={line_user_id} step={step} extra={bool(extra_state)}")

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=prompt_text)],
        )
    )

# def clear_pending_state(line_user_id: str) -> bool:
#     """
#     清除使用者的暫存流程狀態（目前只處理 PENDING_REGISTRATIONS）。
#     回傳：是否真的有清到東西
#     """
#     if not line_user_id:
#         return False
#     existed = line_user_id in PENDING_REGISTRATIONS
#     PENDING_REGISTRATIONS.pop(line_user_id, None)
#     return existed


def clear_pending_state(line_user_id):
    return clear_state(line_user_id)

def to_local_date(dt: datetime) -> date:
    # 專案如果已有 parse_booking_datetime_to_local() 就用那個轉好再取 .date()
    return dt.date()

def can_confirm(appt_local_dt: datetime, today: date | None = None) -> bool:
    today = today or date.today()  # 伺服器在台灣就OK；不然你就用台灣時區的 today
    appt_date = to_local_date(appt_local_dt)
    return today >= (appt_date - timedelta(days=CONFIRM_OPEN_DAYS_BEFORE))

def can_cancel(appt_local_dt: datetime, today: date | None = None) -> bool:
    today = today or date.today()
    appt_date = to_local_date(appt_local_dt)
    return today <= (appt_date - timedelta(days=CANCEL_DEADLINE_DAYS_BEFORE))

def is_binding_complete(user: dict, line_user_id: str) -> bool:
    """
    規格：已綁定完成者（才可直接進預約）
    必須同時滿足：
    - external_id == line_user_id
    - user_fields.line_user_id == line_user_id
    - profile_status == complete
    - 姓名有效（不可為 未填姓名/貴賓 等 placeholder）
    - 手機有效（09xxxxxxxx）
    """
    if not user or not line_user_id:
        return False

    ext = (user.get("external_id") or "").strip()
    ufs = user.get("user_fields") or {}
    uf_line = (ufs.get(ZENDESK_UF_LINE_USER_ID_KEY) or "").strip()
    profile = (ufs.get(ZENDESK_UF_PROFILE_STATUS_KEY) or "").strip()

    name = (user.get("name") or "").strip()
    phone = normalize_phone(user.get("phone") or "")

    if ext != line_user_id:
        return False
    if uf_line != line_user_id:
        return False
    if profile != PROFILE_STATUS_COMPLETE:
        return False

    # 姓名必須是有效姓名（會擋 未填姓名/貴賓 等）
    if not is_valid_name(name):
        return False

    # 手機必須存在且為 09xxxxxxxx
    if not (len(phone) == 10 and phone.startswith("09")):
        return False

    return True





