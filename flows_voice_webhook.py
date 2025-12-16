# flows_voice_webhook.py
from datetime import datetime
from flask import current_app as app
from zendesk_core import mark_zendesk_ticket_voice_attempted

def handle_livehub_webhook(data: dict):
    """
    支援兩種 payload：
    - 新版群組：metadata.ticketIds = [..]
    - 舊版單筆：metadata.ticketId = 123
    """
    call_id = data.get("callId") or data.get("call_id") or data.get("sessionId")
    call_status = data.get("callStatus") or data.get("status") or data.get("call_status")

    metadata = data.get("metadata") or {}

    ticket_ids = metadata.get("ticketIds")
    if not ticket_ids:
        # fallback：單筆 payload
        single = metadata.get("ticketId") or metadata.get("zendesk_ticket_id")
        ticket_ids = [single] if single else []

    if not call_id or not ticket_ids:
        app.logger.warning(f"[voice_webhook] missing call_id or ticket_ids, ignore. data={data}")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    for tid in ticket_ids:
        if not tid:
            continue
        try:
            mark_zendesk_ticket_voice_attempted(
                ticket_id=int(tid),
                call_id=str(call_id),
                call_status=str(call_status or ""),
                attempted_date=today_str,
            )
        except Exception as e:
            app.logger.error(f"[voice_webhook] update ticket failed tid={tid}: {e}")
