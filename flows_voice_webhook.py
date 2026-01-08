# flows_voice_webhook.py
from datetime import datetime
from flask import current_app as app
from zendesk_core import (
    mark_zendesk_ticket_voice_attempted,
    mark_zendesk_ticket_voice_succeeded,
    mark_zendesk_ticket_voice_failed,
)

from utils import(
    parse_ticket_ids
)

def _get_metadata(data: dict) -> dict:
    m = data.get("metadata")
    if isinstance(m, dict):
        return m
    call = data.get("call") or {}
    m2 = call.get("metadata")
    if isinstance(m2, dict):
        return m2
    return {}

def _normalize_ticket_ids(v):
    if not v:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",")]
        out = []
        for p in parts:
            try:
                out.append(int(p))
            except Exception:
                pass
        return out
    try:
        return [int(v)]
    except Exception:
        return []



def _normalize_status(s: str) -> str:
    v = (s or "").strip().lower()
    if v in ("success", "completed", "answered", "ok"):
        return "success"
    if v in ("no_answer", "noanswer", "busy", "failed", "error", "rejected"):
        return "failed"
    return "attempted"


def handle_livehub_webhook(data: dict):
    """
    支援兩種 payload：
    - 新版群組：metadata.ticketIds = [..]
    - 舊版單筆：metadata.ticketId = 123
    """
    call_id = (
        data.get("callId")
        or data.get("call_id")
        or data.get("sessionId")
        or data.get("id")
        or data.get("conversationId")
        or (data.get("call") or {}).get("id")
    )


    call_status = data.get("callStatus") or data.get("status") or data.get("call_status")
    result = _normalize_status(call_status)
    status_lc= (call_status or "").strip().lower()

    if status_lc in ("answered", "ringing", "connecting"):
        app.logger.info(f"[voice_webhook] skip status={call_status} (wait for completed)")
        return

    reason_code = data.get("reasonCode") or ""
    call_duration = data.get("callDuration")  # 可能是 float/int/None

    if call_duration is None:
        call_duration_str = ""
    else:
        call_duration_str = str(call_duration)

    metadata = _get_metadata(data)

    raw_ticket_ids = (
        (metadata.get("ticketIds") if isinstance(metadata, dict) else None)
        or (metadata.get("ticketId") if isinstance(metadata, dict) else None)
        or (metadata.get("zendesk_ticket_id") if isinstance(metadata, dict) else None)
    )

    if not raw_ticket_ids:
        bod = data.get("botOperationData") or {}
        if isinstance(bod, dict):
            raw_ticket_ids = (
                bod.get("ticketIds") or bod.get("ticketids")
                or bod.get("ticketId") or bod.get("ticketid")
            )

    ticket_ids = parse_ticket_ids(raw_ticket_ids)
    if not ticket_ids:
        single = metadata.get("ticketId") or metadata.get("zendesk_ticket_id")
        ticket_ids = parse_ticket_ids(single)


    if not call_id or not ticket_ids:
        app.logger.warning(f"[voice_webhook] missing call_id or ticket_ids, ignore. data={data}")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    for tid in ticket_ids:
        if not tid:
            continue
        try:
            # if result == "success":
            #     mark_zendesk_ticket_voice_succeeded(
            #         ticket_id=int(tid),
            #         call_id=str(call_id),
            #         call_status=str(call_status or ""),
            #         attempted_date=today_str,
            #     )
            # elif result == "failed":
            #     mark_zendesk_ticket_voice_failed(
            #         ticket_id=int(tid),
            #         call_id=str(call_id),
            #         call_status=str(call_status or ""),
            #         attempted_date=today_str,
            #     )
            # else:
            #     mark_zendesk_ticket_voice_attempted(
            #         ticket_id=int(tid),
            #         call_id=str(call_id),
            #         call_status=str(call_status or ""),
            #         attempted_date=today_str,
            #     )
             mark_zendesk_ticket_voice_attempted(
                    ticket_id=int(tid),
                    call_id=str(call_id),
                    call_status=f"{call_status or ''}|{result}".strip("|"),
                    attempted_date=today_str,
                    reason_code=reason_code,
                    call_duration=call_duration_str,
                )
        except Exception as e:
            app.logger.error(f"[voice_webhook] update ticket failed tid={tid}: {e}")
