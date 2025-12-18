# flows_voice_scheduler.py
from datetime import datetime, timedelta
from collections import defaultdict
from flask import current_app as app

from zendesk_core import (
    search_zendesk_tickets_for_voice_reminder,
    _get_ticket_cf_value,
    ZENDESK_CF_APPOINTMENT_DATE,
)

from config import (
    ZENDESK_REMINDER_STATE_QUEUED,
)

from queue_core import voice_call_queue


def build_voice_groups_and_enqueue(days: int = 1) -> dict:
    """
    真實版 D1：
    1) 撈 Zendesk pending tickets
    2) 篩出 appointment_date = 今天 + days
    3) 依 (requester_id, appointment_date) 分組
    4) 每組 enqueue 一通 voice job（process_voice_call_group_from_zendesk）
    """
    target_date = (datetime.now().date() + timedelta(days=days)).strftime("%Y-%m-%d")
    app.logger.info("[VOICE CRON] target_date=%s (days=%s)", target_date, days)

    tickets = search_zendesk_tickets_for_voice_reminder(
        state=ZENDESK_REMINDER_STATE_QUEUED
    ) or []

    app.logger.info("[VOICE CRON] pending candidates=%s", len(tickets))

    # key = (requester_id, appointment_date) → value = [ticket_id...]
    groups = defaultdict(list)

    for t in tickets:
        tid = t.get("id")
        requester_id = t.get("requester_id")

        appt_date = _get_ticket_cf_value(t, ZENDESK_CF_APPOINTMENT_DATE, "") or ""
        appt_date = str(appt_date).strip()

        if not tid or not requester_id or not appt_date:
            continue

        # 只打「一天前」那天的票
        if appt_date != target_date:
            continue

        groups[(int(requester_id), appt_date)].append(int(tid))

    app.logger.info("[VOICE CRON] groups_to_call=%s", len(groups))

    enqueued = 0
    details = []

    for (requester_id, appt_date), ticket_ids in groups.items():
        # line_user_id 這階段先不用依賴它；給 placeholder 即可
        line_user_id = "U_auto"

        job = voice_call_queue.enqueue(
            "flows_voice_calls.process_voice_call_group",
            line_user_id,
            appt_date,
            ticket_ids,
        )

        enqueued += 1
        details.append(
            {
                "requester_id": requester_id,
                "appointment_date": appt_date,
                "ticket_ids": ticket_ids,
                "job_id": job.id,
            }
        )

        app.logger.info(
            "[VOICE CRON] enqueued job_id=%s requester_id=%s date=%s tickets=%s",
            job.id, requester_id, appt_date, ticket_ids
        )

    return {
        "target_date": target_date,
        "pending_candidates": len(tickets),
        "groups": len(groups),
        "enqueued": enqueued,
        "details": details[:20],  # 避免回太長
    }
