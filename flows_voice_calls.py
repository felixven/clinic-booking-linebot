# flows_voice_calls.py
import os
import requests
from flask import current_app as app  # 不要 import app.py
import base64

from zendesk_core import get_zendesk_ticket_by_id, get_zendesk_user_by_id, extract_phone_from_zendesk_user


LIVEHUB_BASE_URL = os.getenv("LIVEHUB_BASE_URL", "https://livehub.audiocodes.io")
LIVEHUB_DIALOUT_PATH = "/api/v1/actions/dialout"
LIVEHUB_BOT_ID = os.getenv("LIVEHUB_BOT_ID")
LIVEHUB_NOTIFY_URL = os.getenv("LIVEHUB_NOTIFY_URL")
LIVEHUB_CALLER = os.getenv("LIVEHUB_CALLER_NUMBER", "+886437005750")
LIVEHUB_API_KEY = os.getenv("LIVEHUB_API_KEY")


def _build_livehub_headers():
    headers = {
        "Content-Type": "application/json",
    }

    if LIVEHUB_API_KEY:
        # Basic auth: base64("apiKey:")
        token = base64.b64encode(f"{LIVEHUB_API_KEY}:".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"

    return headers


#單筆/測試用的func
def process_voice_call_task(task: dict):
    if not LIVEHUB_BOT_ID or not LIVEHUB_NOTIFY_URL:
        app.logger.error("[VOICE JOB] 缺 bot ID 或 notifyUrl")
        return

    phone = task.get("phone")
    if not phone:
        app.logger.warning(f"[VOICE JOB] 缺 phone，略過 task={task}")
        return

    target = f"tel:{phone}"

    appointments = task.get("appointments") or []
    first_appt = appointments[0] if appointments else {}

    metadata = {
        "bookingId": first_appt.get("booking_id"),
        "ticketId": task.get("zendesk_ticket_id"),
        "lineUserId": task.get("line_user_id"),
        "reminderType": task.get("reminder_type"),
        "patientName": task.get("patient_name"),
        "appointmentTime": first_appt.get("local_time"),
        "serviceName": first_appt.get("service_name"),
    }

    payload = {
        "bot": LIVEHUB_BOT_ID,
        "notifyUrl": LIVEHUB_NOTIFY_URL,
        "machineDetection": "disconnect",
        "voicemailEndTimeoutSec": 20,
        "target": target,
        "caller": LIVEHUB_CALLER,
        "metadata": metadata,
    }

    url = LIVEHUB_BASE_URL + LIVEHUB_DIALOUT_PATH
    headers = _build_livehub_headers()

    app.logger.info(f"[VOICE JOB] 呼叫 Live Hub: {payload}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[VOICE JOB] dialout 失敗: {e}")
        return

    try:
        data = resp.json()
    except Exception:
        data = resp.text

    app.logger.info(f"[VOICE JOB] Live Hub 回應: {data}")

def process_voice_call_group(line_user_id: str, appt_date_str: str, ticket_ids: list[int]):
    """
    群組外撥（正式版 v1）：
    - 同一個人同一天：只打一通
    - metadata 帶 ticketIds，讓 webhook 回來可以更新整組
    - Zendesk 更新（attempts/date/note）留給 webhook 處理
    """
    if not LIVEHUB_BOT_ID or not LIVEHUB_NOTIFY_URL:
        app.logger.error("[VOICE GROUP] 缺 bot ID 或 notifyUrl")
        return

    if not ticket_ids:
        app.logger.warning("[VOICE GROUP] ticket_ids 為空，略過")
        return

    # 1) 用第一張 ticket 找 requester → phone/name
    first_ticket_id = int(ticket_ids[0])
    ticket = get_zendesk_ticket_by_id(first_ticket_id)
    if not ticket:
        app.logger.error(f"[VOICE GROUP] 取不到 ticket: {first_ticket_id}")
        return

    requester_id = ticket.get("requester_id")
    if not requester_id:
        app.logger.error(f"[VOICE GROUP] ticket_id={first_ticket_id} 缺 requester_id")
        return

    user = get_zendesk_user_by_id(int(requester_id))
    patient_name = (user or {}).get("name") or "貴賓"
    phone = extract_phone_from_zendesk_user(user)

    if not phone:
        app.logger.error(f"[VOICE GROUP] requester_id={requester_id} 找不到 phone，無法外撥 ticket_ids={ticket_ids}")
        return

    target = f"tel:{phone}"

    # 2) metadata：最關鍵是 ticketIds（整組）
    metadata = {
        "lineUserId": line_user_id,
        "apptDate": appt_date_str,
        "ticketIds": [int(x) for x in ticket_ids],
        "patientName": patient_name,
        "phone": phone,
    }

    payload = {
        "bot": LIVEHUB_BOT_ID,
        "notifyUrl": LIVEHUB_NOTIFY_URL,
        "machineDetection": "disconnect",
        "voicemailEndTimeoutSec": 20,
        "target": target,
        "caller": LIVEHUB_CALLER,
        "metadata": metadata,
    }

    url = LIVEHUB_BASE_URL + LIVEHUB_DIALOUT_PATH
    headers = _build_livehub_headers()

    app.logger.info(f"[VOICE GROUP] dialout payload={payload}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json() if resp.headers.get("content-type","").startswith("application/json") else resp.text
        app.logger.info(f"[VOICE GROUP] LiveHub response={data}")
    except Exception as e:
        app.logger.error(f"[VOICE GROUP] dialout 失敗: {e}")
        return
