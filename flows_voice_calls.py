# flows_voice_calls.py
import os
import requests
from flask import current_app as app  # 不要 import app.py
import base64
import json
from datetime import datetime

from zendesk_core import (
    get_zendesk_ticket_by_id,
    get_zendesk_user_by_id,
    extract_phone_from_zendesk_user,
    mark_zendesk_ticket_queued,
    mark_zendesk_ticket_voice_attempted,
    _get_ticket_cf_value, #demo zendesk 串copilot
)

from config import (
    PROFILE_STATUS_EMPTY,
    PROFILE_STATUS_NEED_PHONE,
    PROFILE_STATUS_COMPLETE,
    ZENDESK_SUBDOMAIN,
    ZENDESK_EMAIL,
    ZENDESK_API_TOKEN,
    ZENDESK_CF_BOOKING_ID,
    ZENDESK_CF_APPOINTMENT_DATE,
    ZENDESK_CF_APPOINTMENT_TIME,
    ZENDESK_CF_REMINDER_STATE,
    ZENDESK_CF_REMINDER_ATTEMPTS,
    ZENDESK_CF_LAST_CALL_ID,
    ZENDESK_UF_LINE_USER_ID_KEY,
    ZENDESK_UF_PROFILE_STATUS_KEY,
    ZENDESK_APPOINTMENT_FORM_ID,
    ZENDESK_REMINDER_STATE_PENDING,
    ZENDESK_REMINDER_STATE_QUEUED,
    ZENDESK_REMINDER_STATE_SUCCESS,
    ZENDESK_REMINDER_STATE_FAILED,
    ZENDESK_REMINDER_STATE_CANCELLED,
    ZENDESK_CF_LAST_VOICE_ATTEMPT_DATE,
    APPOINTMENT_DURATION_MINUTES,
)



LIVEHUB_BASE_URL = os.getenv("LIVEHUB_BASE_URL", "https://livehub.audiocodes.io")
LIVEHUB_DIALOUT_PATH = "/api/v1/actions/dialout"
LIVEHUB_BOT_ID = os.getenv("LIVEHUB_BOT_ID")
LIVEHUB_NOTIFY_URL = os.getenv("LIVEHUB_NOTIFY_URL")
LIVEHUB_CALLER = os.getenv("LIVEHUB_CALLER_NUMBER", "+886437005750")
LIVEHUB_USERNAME = os.getenv("LIVEHUB_USERNAME")
LIVEHUB_PASSWORD = os.getenv("LIVEHUB_PASSWORD")
VOICE_DEMO_MODE = os.getenv("VOICE_DEMO_MODE", "0") == "1"
VOICE_TEST_PHONE = os.getenv("VOICE_TEST_PHONE", "").strip()


def _build_livehub_headers():
    headers = {
        "Content-Type": "application/json",
    }

    # 只印 user + 密碼長度，不印密碼本身
    # 用 print 確保在 RQ worker console 一定看得到
    print(
        "[VOICE AUTH CHECK] user=%r pass_len=%s"
        % (
            LIVEHUB_USERNAME,
            (len(LIVEHUB_PASSWORD) if LIVEHUB_PASSWORD else None),
        )
    )

    if LIVEHUB_USERNAME and LIVEHUB_PASSWORD:
        raw = f"{LIVEHUB_USERNAME}:{LIVEHUB_PASSWORD}"
        token = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"
    else:
        # 用 print 確保可見
        print("[VOICE AUTH] missing LIVEHUB_USERNAME or LIVEHUB_PASSWORD")
        app.logger.error("[VOICE AUTH] missing LIVEHUB_USERNAME or LIVEHUB_PASSWORD")

    return headers




#單筆/測試用的func
def process_voice_call_task(task: dict):
    # 1) 先印 env 狀態（不會洩漏密碼）
    print("[ENV CHECK] BOT_ID=%r NOTIFY_URL=%r USER=%r" % (LIVEHUB_BOT_ID, LIVEHUB_NOTIFY_URL, LIVEHUB_USERNAME))

    # 2) 最基本 gate（缺這兩個一定不能打）
    if not LIVEHUB_BOT_ID or not LIVEHUB_NOTIFY_URL:
        print("[VOICE JOB] MISSING BOT_ID or NOTIFY_URL -> abort")
        app.logger.error("[VOICE JOB] 缺 bot ID 或 notifyUrl")
        return

    phone = (task.get("phone") or "").strip()
    if not phone:
        print("[VOICE JOB] MISSING phone -> abort task=%r" % task)
        app.logger.warning(f"[VOICE JOB] 缺 phone，略過 task={task}")
        return

    target = f"tel:{phone}"

    appointments = task.get("appointments") or []
    first_appt = appointments[0] if appointments else {}

    # 3) Phase1 的 metadata（直接給 Copilot 念的）
    # ===== Phase 1 (Demo A) 最小 metadata：只送 3 個欄位給 Copilot =====
    appointment_date = ""
    if first_appt.get("local_time"):
        appointment_date = str(first_appt.get("local_time"))[:10]  # "YYYY-MM-DD"

    metadata = {
        "patientName": task.get("patient_name") or "貴賓",
        "appointmentDate": appointment_date,
        "appointmentCount": len(appointments),
    }

    # metadata = {
    #     "patientName": task.get("patient_name") or "貴賓",
    #     "appointmentTime": first_appt.get("local_time") or "",
    #     "appointmentDate": (first_appt.get("local_time") or "")[:10],
    #     "appointmentCount": len(appointments),
    #     "ticketId": task.get("zendesk_ticket_id"),
    #     "bookingId": first_appt.get("booking_id"),
    #     "lineUserId": task.get("line_user_id"),
    #     "reminderType": task.get("reminder_type"),
    #     "serviceName": first_appt.get("service_name"),
    # }



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

    # 4) 直接印出「最終送出的 JSON」（你要看的就是這個）
    print("FINAL DIALOUT PAYLOAD =", json.dumps(payload, ensure_ascii=False))

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        # 先印 response（只截前 200 字避免太長）
        print("LIVEHUB RESP =", resp.status_code, (resp.text or "")[:200])
        resp.raise_for_status()
    except Exception as e:
        print("LIVEHUB ERROR =", repr(e))
        app.logger.error(f"[VOICE JOB] dialout 失敗: {e}")
        return

    # 成功才會到這裡
    try:
        data = resp.json()
    except Exception:
        data = resp.text

    print("LIVEHUB OK =", data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
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
    # 0) 外撥前先鎖票（queued）避免重複撥打
    # 這裡會 attempts + 1（因為你的 mark_zendesk_ticket_queued 就是這樣設計）
    for tid in ticket_ids:
        if not tid:
            continue
        try:
            # 用第一張 ticket 的資料可以算 attempts，但你這裡為了最小改動就不帶 ticket
            mark_zendesk_ticket_queued(ticket_id=int(tid), ticket=None)
        except Exception as e:
            app.logger.error(f"[VOICE GROUP] lock queued failed tid={tid}: {e}")


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
    
    # ✅ 測試模式：強制覆蓋電話（避免用假資料）
    if VOICE_DEMO_MODE and VOICE_TEST_PHONE:
        app.logger.warning(
            "[VOICE DEMO] override phone %s -> %s",
            phone,
            VOICE_TEST_PHONE
        )
        phone = VOICE_TEST_PHONE

    if not phone:
        app.logger.error(
            f"[VOICE GROUP] requester_id={requester_id} 找不到 phone，無法外撥 ticket_ids={ticket_ids}"
        )
        return
    app.logger.warning("[VOICE GROUP] final phone=%r target=%r", phone, f"tel:{phone}")


    target = f"tel:{phone}"

    # 2) metadata：最關鍵是 ticketIds（整組）
    metadata = {
        # Copilot 會用的（一定要對齊）
        "patientName": patient_name,
        "appointmentDate": appt_date_str,
        "appointmentCount": len(ticket_ids),

        # 先留著，Copilot 不用
        "lineUserId": line_user_id,
        "ticketIds": ",".join(str(x) for x in ticket_ids),
    }   
    # metadata = {
    #     "lineUserId": line_user_id,
    #     "apptDate": appt_date_str,
    #     "ticketIds": [int(x) for x in ticket_ids],
    #     "patientName": patient_name,
    #     "phone": phone,
    # }

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

    app.logger.info(
        "[VOICE] FINAL dialout payload = %s",
        json.dumps(payload, ensure_ascii=False)
    )

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        app.logger.warning("[VOICE GROUP] LiveHub status=%s body=%s", resp.status_code, (resp.text or "")[:300])
        resp.raise_for_status()
        data = resp.json() if resp.headers.get("content-type","").startswith("application/json") else resp.text
        app.logger.info(f"[VOICE GROUP] LiveHub response={data}")
    except Exception as e:
        app.logger.error(f"[VOICE GROUP] dialout 失敗: {e}")

        # dialout 失敗也要回寫 Zendesk（避免隔天重跑一直打）
        today_str = datetime.now().strftime("%Y-%m-%d")
        for tid in ticket_ids:
            if not tid:
                continue
            try:
                mark_zendesk_ticket_voice_attempted(
                    ticket_id=int(tid),
                    call_id="",
                    call_status="dialout_failed",
                    attempted_date=today_str,
                )
            except Exception as ee:
                app.logger.error(f"[VOICE GROUP] update failed tid={tid}: {ee}")
        return
    
#demo zendesk串到copilot
def process_voice_call_demo_from_zendesk(line_user_id: str, appt_date_str: str, ticket_ids: list[int]):
    """
    真實版 v1（最小可用）：
    - 用 ticketIds 找 requester → user → name/phone
    - appointmentDate 從 ticket custom field 拿
    - appointmentCount = ticketIds 數量
    - metadata 對齊 Copilot 的 dialoutMetadata schema（patientName/appointmentDate/appointmentCount）
    """

    app.logger.info("[VOICE ZD] ENTER ticket_ids=%s appt_date_str=%s", ticket_ids, appt_date_str)

    if not LIVEHUB_BOT_ID or not LIVEHUB_NOTIFY_URL:
        app.logger.error("[VOICE ZD] missing LIVEHUB_BOT_ID or LIVEHUB_NOTIFY_URL")
        return

    if not ticket_ids:
        app.logger.warning("[VOICE ZD] empty ticket_ids, skip")
        return

    try:
        # 1) 第一張 ticket → requester → user
        first_ticket_id = int(ticket_ids[0])
        ticket = get_zendesk_ticket_by_id(first_ticket_id)
        if not ticket:
            app.logger.error("[VOICE ZD] cannot fetch ticket first_ticket_id=%s", first_ticket_id)
            return

        requester_id = ticket.get("requester_id")
        if not requester_id:
            app.logger.error("[VOICE ZD] ticket_id=%s missing requester_id", first_ticket_id)
            return

        user = get_zendesk_user_by_id(int(requester_id))
        patient_name = (user or {}).get("name") or "貴賓"

        phone = extract_phone_from_zendesk_user(user)

        # 2) ✅ Demo 模式才允許覆蓋電話
        if VOICE_DEMO_MODE and VOICE_TEST_PHONE:
            app.logger.warning("[VOICE DEMO] override phone %s -> %s", phone, VOICE_TEST_PHONE)
            phone = VOICE_TEST_PHONE

        if not phone:
            app.logger.error("[VOICE ZD] requester_id=%s no phone, skip ticket_ids=%s", requester_id, ticket_ids)
            return

        # 3) appointmentDate：優先從 ticket CF 拿，拿不到就 fallback appt_date_str
        appointment_date = _get_ticket_cf_value(ticket, ZENDESK_CF_APPOINTMENT_DATE, "") or ""
        appointment_date = appointment_date.strip()
        if not appointment_date or appointment_date == "unknown":
            appointment_date = (appt_date_str or "").strip() or datetime.now().strftime("%Y-%m-%d")

        # 4) appointmentCount：ticketIds 數量
        appointment_count = len([x for x in ticket_ids if x])

        # 5) metadata（Copilot 目前只用這三個也 OK）
        metadata = {
            "patientName": patient_name,
            "appointmentDate": appointment_date,
            "appointmentCount": appointment_count,
            # ✅ 先帶著，不會影響你現階段播報；未來 webhook 回寫會用
            "ticketIds": [int(x) for x in ticket_ids if x],
        }

        payload = {
            "bot": LIVEHUB_BOT_ID,
            "notifyUrl": LIVEHUB_NOTIFY_URL,
            "machineDetection": "disconnect",
            "voicemailEndTimeoutSec": 20,
            "target": f"tel:{phone}",
            "caller": LIVEHUB_CALLER,
            "metadata": metadata,
        }

        url = LIVEHUB_BASE_URL + LIVEHUB_DIALOUT_PATH
        headers = _build_livehub_headers()

        app.logger.info("[VOICE ZD] FINAL dialout payload=%s", json.dumps(payload, ensure_ascii=False))

        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        app.logger.info("[VOICE ZD] LiveHub resp=%s %s", resp.status_code, (resp.text or "")[:200])
        resp.raise_for_status()

    except Exception as e:
        app.logger.exception("[VOICE ZD] failed: %s", e)
        return