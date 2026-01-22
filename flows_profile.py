# flows/profile_flow.py
import requests

from state_store import get_state, set_state, clear_state


def handle_profile_flow(
    *,
    line_bot_api,
    event,
    text: str,
    line_user_id_for_state: str,
    state: dict,
    step: str,
    # ===== 由 app.py 注入，避免 circular import =====
    start_loading_animation,
    send_line,
    reply_consent_input,
    reply_date_range_buttons,
    normalize_phone,
    is_valid_name,
    is_binding_complete,
    upsert_zendesk_user_basic_profile,
    search_zendesk_user_by_line_id,
    search_zendesk_users_by_phone,
    _build_zendesk_headers,
    # 常數也用注入（先不搬家）
    PROFILE_STATUS_COMPLETE: str,
    PROFILE_STATUS_NEED_NAME: str,
    PROFILE_STATUS_NEED_PHONE: str,
    ZENDESK_UF_LINE_USER_ID_KEY: str,
    ZENDESK_UF_PROFILE_STATUS_KEY: str,
) -> bool:
    """
    只處理「建檔/認領/綁定」這段 step machine。
    回傳：
      True  -> 已處理（app.py 直接 return）
      False -> 不屬於此 flow（讓 app.py 繼續處理其他指令）
    """

    # 沒有 uid 或沒有 step 就不是這支處理
    if not line_user_id_for_state or not step:
        return False

    # ===== 流程中保護：避免把指令當成姓名或手機 =====
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

    if is_command:
        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2))
        send_line(
            line_bot_api,
            event,
            messages=[
                # 這裡用你原本訊息
                __import__("linebot.v3.messaging").v3.messaging.TextMessage(
                    text="您目前正在填寫資料中。\n如要取消請按「取消」或輸入「取消建檔」。"
                )
            ],
            label="already_in_flow_warning",
        )
        return True

    # ===== 等待同意：使用者若直接輸入，不要 reset，提示按按鈕 =====
    if step in {
        "wait_consent_new_name",
        "wait_consent_name_after_phone",
        "wait_consent_phone",
    }:
        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
        send_line(
            line_bot_api,
            event,
            messages=[
                __import__("linebot.v3.messaging").v3.messaging.TextMessage(
                    text="請先按下方按鈕「好的，我要開始輸入」後再輸入。若要取消請輸入「取消」。"
                )
            ],
            label="consent",
        )
        return True

    # 0-1. 問姓名
    if step == "ask_name":
        name = text.strip()
        if not name:
            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
            send_line(
                line_bot_api,
                event,
                messages=[
                    __import__("linebot.v3.messaging").v3.messaging.TextMessage(
                        text="姓名不能空白，請再次輸入您的姓名。"
                    )
                ],
                label="name_cannot_blank",
            )
            return True

        # 先把姓名寫進 Zendesk，同時標記 profile_status = need_phone
        try:
            user = upsert_zendesk_user_basic_profile(
                line_user_id=line_user_id_for_state,
                name=name,
                phone=None,
                profile_status=PROFILE_STATUS_NEED_PHONE,
            )
            if user and user.get("id"):
                state["zendesk_user_id"] = user.get("id")
        except Exception:
            # 不中斷流程，繼續問手機
            pass

        state["name"] = name
        state["step"] = "ask_phone"
        set_state(line_user_id_for_state, state)

        reply_text = f"{name} 您好，請輸入您的手機號碼（格式：09xxxxxxxx）："
        send_line(
            line_bot_api,
            event,
            messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text=reply_text)],
            label="enter_phone_number",
        )
        return True

    # 0-1.5 問姓名（手機已經有了，補姓名用）
    elif step == "ask_name_after_phone":
        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2))

        name = text.strip()
        if not is_valid_name(name):
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="請輸入您的真實姓名（不可空白）。")],
                label="name_cannot_blank",
            )
            return True

        zendesk_user_id = state.get("zendesk_user_id")
        if not zendesk_user_id:
            # 保守：如果意外沒有 user_id，就回到問手機重新走
            state["step"] = "ask_phone"
            set_state(line_user_id_for_state, state)
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="資料狀態異常，請重新輸入手機號碼（09xxxxxxxx）：")],
                label="phone_number_system_error",
            )
            return True

        phone = (state.get("phone") or "").strip()
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
                        PROFILE_STATUS_COMPLETE if is_valid_name(name) else PROFILE_STATUS_NEED_NAME
                    ),
                },
            }
        }

        try:
            resp = requests.put(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception:
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="更新姓名時發生問題，請稍後再試。")],
                label="name_system_error",
            )
            return True

        # 成功 → 清狀態 → 進入選日期範圍
        clear_state(line_user_id_for_state)

        phone_display = state.get("phone") or "（已留存）"
        info_text = (
            "已為您完成基本資料建檔\n"
            f"姓名：{name}\n"
            f"手機：{phone_display}\n\n"
            "接下來請選擇要預約的日期範圍："
        )
        reply_date_range_buttons(event, info_text)
        return True

    elif step == "confirm_name_after_claim":
        if text not in {"姓名正確", "我要修改姓名"}:
            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="請點選按鈕：姓名正確 / 我要修改姓名")],
                label="confirm_name_after_claim_invalid_choice",
            )
            return True

        zendesk_user_id = state.get("zendesk_user_id")
        phone = (state.get("phone") or "").strip()
        found_name = (state.get("found_name") or "").strip()

        if not zendesk_user_id:
            clear_state(line_user_id_for_state)
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始。")],
                label="confirm_name_after_claim_missing_user",
            )
            return True

        # 使用者選「我要修改姓名」→ 直接進入補姓名
        if text == "我要修改姓名":
            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
            state["step"] = "ask_name_after_phone"
            set_state(line_user_id_for_state, state)
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="請輸入您要更新的真實姓名（全名）：")],
                label="confirm_name_after_claim_to_ask_name",
            )
            return True

        # 使用者選「姓名正確」→ 只做綁定（external_id / user_fields），不改名
        base_url, headers = _build_zendesk_headers()
        url = f"{base_url}/api/v2/users/{zendesk_user_id}.json"
        payload = {
            "user": {
                "external_id": line_user_id_for_state,
                "user_fields": {
                    ZENDESK_UF_LINE_USER_ID_KEY: line_user_id_for_state,
                    ZENDESK_UF_PROFILE_STATUS_KEY: (
                        PROFILE_STATUS_COMPLETE if is_valid_name(found_name) else PROFILE_STATUS_NEED_NAME
                    ),
                },
            }
        }
        if phone:
            payload["user"]["phone"] = phone

        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2))
        try:
            resp = requests.put(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception:
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="綁定資料時發生問題，請稍後再試。")],
                label="data_biding_error",
            )
            return True

        clear_state(line_user_id_for_state)

        info_text = (
            f"{found_name or '貴賓'} 您好，已為您完成身分綁定。\n"
            f"手機：{phone or '（已確認）'}\n\n"
            "請選擇要預約的日期範圍："
        )
        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
        reply_date_range_buttons(event, info_text)
        return True

    elif step == "ask_name_for_multi_claim":
        name = text.strip()
        candidates = state.get("candidates") or []
        phone = state.get("phone") or ""
        mode = (state.get("mode") or "").strip()

        if mode != "already_bound":
            if not is_valid_name(name):
                send_line(
                    line_bot_api,
                    event,
                    messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="請輸入您的真實姓名（全名），以便確認資料。")],
                    label="ask_name_for_multi_claim_invalid_name",
                )
                return True

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
                    messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此手機號碼已綁定其他帳號，請聯繫診所協助處理。")],
                    label="ask_name_for_multi_claim_already_bound",
                )
                return True

            found = matched[0]
            found_name = (found.get("name") or "").strip()

            # placeholder → 直接補姓名
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
                    messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="已確認您的手機，請輸入您的真實姓名（全名）：")],
                    label="ask_name_for_multi_claim_placeholder_to_ask_name",
                )
                return True

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

            ButtonsTemplate = __import__("linebot.v3.messaging").v3.messaging.ButtonsTemplate
            MessageAction = __import__("linebot.v3.messaging").v3.messaging.MessageAction
            TemplateMessage = __import__("linebot.v3.messaging").v3.messaging.TemplateMessage

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
                messages=[TemplateMessage(alt_text="確認姓名", template=buttons_template)],
                label="ask_name_for_multi_claim_confirm_buttons",
            )
            return True

        if len(matched) == 0:
            if mode == "already_bound":
                clear_state(line_user_id_for_state)
                send_line(
                    line_bot_api,
                    event,
                    messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此手機號碼已綁定其他帳號，請聯繫診所客服協助處理。")],
                    label="phone_multi_user_error",
                )
                return True

            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="找不到符合此姓名的資料。請確認後重新輸入姓名，或聯繫診所協助。")],
                label="ask_name_for_multi_claim_no_match",
            )
            return True

        send_line(
            line_bot_api,
            event,
            messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此姓名仍對應多筆資料，請聯繫診所協助確認。")],
            label="ask_name_for_multi_claim_multi_match",
        )
        return True

    # 0-2. 問手機
    elif step == "ask_phone":
        phone_raw = text.strip()
        digits = normalize_phone(phone_raw)

        if not (len(digits) == 10 and digits.startswith("09")):
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="手機格式不正確，請以 09xxxxxxxx 格式重新輸入。")],
                label="ask_phone_invalid_format",
            )
            return True

        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=30, timeout=(1, 2))

        # A：先看此 LINE 是否已綁
        try:
            bound_count, bound_user = search_zendesk_user_by_line_id(line_user_id_for_state, retries=1)
        except Exception:
            bound_user = None

        if bound_user:
            ufs = bound_user.get("user_fields") or {}
            bound_phone = normalize_phone(bound_user.get("phone") or "")
            bound_profile = (ufs.get(ZENDESK_UF_PROFILE_STATUS_KEY) or "").strip()

            if bound_phone and bound_phone != digits:
                start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=5, timeout=(1, 2))
                send_line(
                    line_bot_api,
                    event,
                    messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此帳號已綁定其他手機號碼，系統不允許線上更換。請聯繫診所協助處理。")],
                    label="ask_phone_block_change_phone",
                )
                return True

            bound_name = (bound_user.get("name") or "").strip()
            if bound_profile != PROFILE_STATUS_COMPLETE or (not is_valid_name(bound_name)):
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
                    text="我們已確認您的手機。為完成資料綁定，請先填寫您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                    ok_data="CONSENT_NAME_AFTER_PHONE",
                    cancel_data="CANCEL_FLOW",
                )
                return True

            reply_date_range_buttons(event, "已確認您的身分，請選擇要預約的日期範圍：")
            return True

        # B：用手機找 seed 老客（只允許 external_id 空白的）
        try:
            candidates = search_zendesk_users_by_phone(digits)
        except Exception:
            candidates = []

        unbound = []
        for u in candidates:
            ext = (u.get("external_id") or "").strip()
            if not ext:
                unbound.append(u)

        if len(unbound) == 1:
            found = unbound[0]
            found_name = (found.get("name") or "").strip()

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
                    text="已找到您的資料（手機已確認）。\n為完成身分綁定，請補上您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                    ok_data="CONSENT_NAME_AFTER_PHONE",
                    cancel_data="CANCEL_FLOW",
                )
                return True

            set_state(
                line_user_id_for_state,
                {
                    "step": "confirm_name_after_claim",
                    "zendesk_user_id": found.get("id"),
                    "phone": digits,
                    "found_name": found_name,
                },
            )

            ButtonsTemplate = __import__("linebot.v3.messaging").v3.messaging.ButtonsTemplate
            MessageAction = __import__("linebot.v3.messaging").v3.messaging.MessageAction
            TemplateMessage = __import__("linebot.v3.messaging").v3.messaging.TemplateMessage

            buttons_template = ButtonsTemplate(
                title="確認姓名",
                text=f"我們找到您的資料：\n姓名：{found_name}\n手機：{digits}\n\n姓名是否正確？",
                actions=[
                    MessageAction(label="正確", text="姓名正確"),
                    MessageAction(label="我要修改", text="我要修改姓名"),
                ],
            )
            send_line(
                line_bot_api,
                event,
                messages=[TemplateMessage(alt_text="確認姓名", template=buttons_template)],
                label="confirm_name_after_claim",
            )
            return True

        if len(unbound) > 1:
            set_state(
                line_user_id_for_state,
                {
                    "step": "ask_name_for_multi_claim",
                    "phone": digits,
                    "candidates": [{"id": u.get("id"), "name": u.get("name") or ""} for u in unbound],
                },
            )
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此手機號碼已有資料。為了確認身分，請輸入您的姓名（全名）：")],
                label="ask_name_for_multi_claim",
            )
            return True

        # candidates 有資料但都已綁 external_id：如果是綁到自己就放行，不然走 already_bound 模式
        if candidates and len(unbound) == 0:
            mine = []
            for u in candidates:
                ext = (u.get("external_id") or "").strip()
                if ext and ext == line_user_id_for_state:
                    mine.append(u)

            if len(mine) == 1:
                found = mine[0]
                found_name = (found.get("name") or "").strip()
                found_phone = normalize_phone(found.get("phone") or digits)

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
                        messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="已確認您的手機，請輸入您的真實姓名（全名）：")],
                        label="ask_name_after_phone",
                    )
                    return True

                reply_date_range_buttons(event, f"{found_name} 您好，\n請選擇要預約的日期範圍：")
                return True

            set_state(
                line_user_id_for_state,
                {
                    "step": "ask_name_for_multi_claim",
                    "phone": digits,
                    "candidates": [
                        {"id": u.get("id"), "name": u.get("name") or "", "external_id": (u.get("external_id") or "")}
                        for u in candidates
                    ],
                    "mode": "already_bound",
                },
            )
            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="此手機號碼已有資料。為了確認身分，請輸入您的姓名（全名）：")],
                label="ask_name_for_multi_claim",
            )
            return True

        # === 沒找到可認領的 seed 老客 → 走原本「新朋友」邏輯（你現在其實也在用）===
        name = state.get("name") or "未填姓名"
        profile_status_value = PROFILE_STATUS_COMPLETE if is_valid_name(name) else PROFILE_STATUS_NEED_NAME

        user = None
        zendesk_user_id = state.get("zendesk_user_id")

        if zendesk_user_id:
            base_url, headers = _build_zendesk_headers()
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
                resp.raise_for_status()
                user = (resp.json() or {}).get("user")
            except Exception:
                user = None

        if not user:
            try:
                user = upsert_zendesk_user_basic_profile(
                    line_user_id=line_user_id_for_state,
                    name=name,
                    phone=digits,
                    profile_status=profile_status_value,
                )
            except Exception:
                user = None

        if not user:
            send_line(
                line_bot_api,
                event,
                messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="建立病患資料時發生問題，請稍後再試。")],
                label="create_profile_failed",
            )
            return True

        if not is_valid_name(name):
            state["zendesk_user_id"] = user.get("id") or state.get("zendesk_user_id")
            state["phone"] = digits
            state["step"] = "wait_consent_name_after_phone"
            set_state(line_user_id_for_state, state)

            start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
            reply_consent_input(
                line_bot_api=line_bot_api,
                event=event,
                title="填寫姓名",
                text="手機已確認。\n為完成身分綁定，請補上您的真實姓名（全名）。\n按下「好的，我要開始輸入」後再輸入姓名。",
                ok_data="CONSENT_NAME_AFTER_PHONE",
                cancel_data="CANCEL_FLOW",
            )
            return True

        clear_state(line_user_id_for_state)

        start_loading_animation(line_bot_api, line_user_id_for_state, loading_seconds=15, timeout=(1, 2))
        info_text = (
            "已為您完成基本資料建檔\n"
            f"姓名：{name}\n"
            f"手機：{digits}\n\n"
            "接下來請選擇要預約的日期範圍："
        )
        reply_date_range_buttons(event, info_text)
        return True

    # 0-3. 例外 step → reset
    else:
        clear_state(line_user_id_for_state)
        send_line(
            line_bot_api,
            event,
            messages=[__import__("linebot.v3.messaging").v3.messaging.TextMessage(text="資料狀態異常，請重新輸入「線上約診」開始流程。")],
            label="state_reset",
        )
        return True
