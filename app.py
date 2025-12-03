from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    MessageAction,
    CarouselTemplate,
    CarouselColumn,
    LocationMessage,
    PostbackAction,  
)

from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    PostbackEvent,
)

from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()
import certifi
import os
import requests
import base64
import json


app = Flask(__name__)

@app.route("/line-booking", methods=["GET"])
def health_check():
    return "OK", 200


# ======================================
#  一、共用設定 & Helper 函數區
# ======================================

# ======== LINE 基本設定 ========
configuration = Configuration(
    access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
)
configuration.ssl_ca_cert = certifi.where()

api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)

handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET")) 

# ======== Booking 相關資料 ========
BOOKING_DEMO_SERVICE_ID = os.getenv("BOOKING_DEMO_SERVICE_ID")
BOOKING_DEMO_STAFF_ID = os.getenv("BOOKING_DEMO_STAFF_ID")
BOOKING_BUSINESS_ID = os.getenv("BOOKING_BUSINESS_ID") 

# ======== MS Graph Booking Token 相關 ========
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")

# ===================== Zendesk 設定 =====================
ZENDESK_SUBDOMAIN = "con-nwdemo" 
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL") or "tech_support@newwave.tw"
ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")  

# ===================== Zendesk 自訂欄位 ID =====================
ZENDESK_CF_BOOKING_ID = 14459987905295          # Booking ID (Text)
ZENDESK_CF_APPOINTMENT_DATE = 14460045495695    # Appointment Date (Date)
ZENDESK_CF_APPOINTMENT_TIME = 14460068239631    # Appointment Time (Text)
ZENDESK_CF_REMINDER_STATE = 14460033600271      # Reminder State (Dropdown)
ZENDESK_CF_REMINDER_ATTEMPTS = 14460034088591   # Reminder Attempts (Number)
ZENDESK_CF_LAST_CALL_ID = 14460059835279        # Last Call Id (備用)

ZENDESK_APPOINTMENT_FORM_ID=14460691929743

ZENDESK_REMINDER_STATE_CANCELLED = "已取消預約"


# ======== 預約時段相關設定（之後要改時段只改這裡） ========
SLOT_START = "09:00"             # 看診起始時間（第一個）
SLOT_END = "21:00"               # 看診結束時間（最後一個）
SLOT_INTERVAL_MINUTES = 30       # 每一格 slot 間隔（目前半小時）
APPOINTMENT_DURATION_MINUTES = 30  # 實際預約時長（要跟 Bookings duration 對齊）
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]# 禮拜幾


# ======== 診所資料（ ========
CLINIC_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763351663/benyamin-bohlouli-B_sK_xgzwVA-unsplash_n6jy9m.jpg"
CLINIC_NAME = "中醫診所"
CLINIC_ADDRESS = "臺中市西屯區青海路二段242之32號"
CLINIC_LAT = 24.1718527355441
CLINIC_LNG = 120.64402133835931


# 線上預約用的共用圖片
WEEK_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763314182/pulse_ultzw0.jpg"

# serviceNotes 裡當「確認」的標記字串
CONFIRM_NOTE_KEYWORD = "Confirmed via LINE"

# 暫存「首次建檔」流程的狀態（key = line_user_id）
PENDING_REGISTRATIONS = {}

# ======== DEMO 患者資料 ========
DEMO_CUSTOMER_NAME = "陳女士"
DEMO_CUSTOMER_EMAIL = "test@example.com"
DEMO_CUSTOMER_PHONE = "0912345678"



# ======================================
#  二、業務流程（Business Flows）函數區
# ======================================

# ======== 跟 Entra 拿 Microsoft Graph 的 access token ========

def get_graph_token():
    tenant_id = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET")

    if not tenant_id or not client_id or not client_secret:
        raise Exception(
            "GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET 有缺，先到終端機 export")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    resp = requests.post(url, data=data)
    app.logger.info(
        f"GRAPH TOKEN STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()
    return resp.json()["access_token"]

# ===================== Zendesk Helper：用 line_user_id 查使用者 =====================

def _build_zendesk_headers() -> tuple[str, dict]:
    
    """
    回傳 (base_url, headers)
    """
    base_url: str = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com"
    auth_str: str = f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}"
    auth_bytes: bytes = auth_str.encode("utf-8")
    auth_header: str = base64.b64encode(auth_bytes).decode("utf-8")

    headers: dict = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/json",
    }
    return base_url, headers

def create_zendesk_user(line_user_id: str, name: str, phone: str):
    """
    建立 Zendesk end-user，並寫入 user_fields.line_user_id。

    流程：
      1. 先檢查是否已有此 line_user_id 的使用者 → 有則直接回傳
      2. 若沒有 → 建立新的 user（含 name / phone / user_fields.line_user_id）
    """
    if not line_user_id:
        app.logger.warning("[create_zendesk_user] 缺少 line_user_id，略過建立 Zendesk user")
        return None

    # 1) 先搜是否已有使用者
    try:
        count, existing_user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[create_zendesk_user] 搜尋 line_user_id 時發生錯誤: {e}")
        existing_user = None

    if existing_user:
        app.logger.info(
            f"[create_zendesk_user] 已存在對應的 Zendesk user, id={existing_user.get('id')}"
        )
        return existing_user

    # 2) 沒有舊資料 → 建立新 user
    base_url, headers = _build_zendesk_headers()  # ⬅️ 新版！統一認證

    url = f"{base_url}/api/v2/users.json"

    # Field key 要和 Zendesk user field 一致（line_user_id）
    payload = {
        "user": {
            "name": name,
            "role": "end-user",
            "phone": phone,
            "verified": True,  # 讓使用者不會 pending verification
            "user_fields": {
                "line_user_id": line_user_id
            }
        }
    }

    app.logger.info(
        f"[create_zendesk_user] 建立新 Zendesk user, name={name}, phone={phone}, line_user_id={line_user_id}"
    )

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[create_zendesk_user] 呼叫 Zendesk API 建立 user 失敗: {e}")
        return None

    data = resp.json()
    user = data.get("user") or {}

    app.logger.info(f"[create_zendesk_user] 建立成功, id={user.get('id')}")
    return user


def search_zendesk_user_by_line_id(line_user_id: str):
    """
    給一個 LINE userId，去 Zendesk 搜尋 user_fields.line_user_id = 這個值 的使用者。

    回傳：
        - count: 幾筆 (int)
        - user: 若 count == 1，回傳那一個 dict，否則 None
    """
    if not line_user_id:
        return 0, None

    # 共用 helper 拿 base_url + headers
    base_url, headers = _build_zendesk_headers()
    search_url: str = f"{base_url}/api/v2/search.json"

    # query 語法：type:user line_user_id:<xxx>
    params: dict = {
        "query": f"type:user line_user_id:{line_user_id}"
    }

    try:
        resp = requests.get(search_url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"Zendesk 搜尋失敗: {e}")
        return 0, None

    data: dict = resp.json()
    count: int = data.get("count", 0)
    results: list = data.get("results") or []

    if count == 1 and results:
        return count, results[0]
    else:
        # 0 筆 或 >1 筆（應該不會 >1）
        return count, None
    




# =========================================================================
#  Zendesk 核心功能：預約 Ticket 建立
# =========================================================================
def create_zendesk_appointment_ticket(
    booking_id: str,
    local_start_dt: datetime,
    zendesk_customer_id: int,
    customer_name: str,
    booking_service_name: str = "一般門診",
):
    """
    在 Zendesk 內建立一個新的 Ticket，作為預約確認提醒的排程觸發點。
    """
    # 先處理時間相關（不用在這裡組 base_url 了）
    try:
        duration_minutes: int = APPOINTMENT_DURATION_MINUTES
        local_end_dt: datetime = local_start_dt + timedelta(minutes=duration_minutes)
    except NameError as e:
        app.logger.error(
            f"Zendesk 全域變數未定義 (例如 {e})，無法建立 Ticket。"
            "請檢查 APPOINTMENT_DURATION_MINUTES。"
        )
        return None
    except Exception:
        app.logger.warning(
            "APPOINTMENT_DURATION_MINUTES 定義有誤或缺失，使用預設 30 分鐘計算結束時間。"
        )
        local_end_dt: datetime = local_start_dt + timedelta(minutes=30)

    # 共用 helper 拿 base_url + headers
    base_url, headers = _build_zendesk_headers()
    url: str = f"{base_url}/api/v2/tickets.json"

    # ====== 1. 組 subject / body ======
    ticket_subject: str = (
        f"【預約提醒】{customer_name}，將於 "
        f"{local_start_dt.strftime('%Y/%m/%d %H:%M')} 看診"
    )

    ticket_body: str = (
        "這是由 LINE Bot 自動建立的預約提醒 Ticket。\n"
        "請在 **預約日期前 3 天** 確認此 Ticket 狀態。\n\n"
        "--- 預約資料 ---\n"
        f"Bookings ID: {booking_id}\n"
        f"客戶 ID (Zendesk): {zendesk_customer_id}\n"
        f"預約時間: {local_start_dt.strftime('%Y/%m/%d %H:%M')}  ～ "
        f"{local_end_dt.strftime('%H:%M')}\n"
        f"服務項目: {booking_service_name}\n\n"
        "--- 提醒流程 ---\n"
        "如果到期時，Bookings 備註內『尚未』顯示 'Confirmed via LINE'，"
        "則需要通知 LINE Bot 進行回呼確認。"
    )

    # ====== 2. custom_fields ======
    appt_date_str: str = local_start_dt.strftime("%Y-%m-%d")
    appt_time_str: str = local_start_dt.strftime("%H:%M")

    custom_fields = [
        {"id": ZENDESK_CF_BOOKING_ID, "value": booking_id},
        {"id": ZENDESK_CF_APPOINTMENT_DATE, "value": appt_date_str},
        {"id": ZENDESK_CF_APPOINTMENT_TIME, "value": appt_time_str},
        {"id": ZENDESK_CF_REMINDER_STATE, "value": "pending"},
        {"id": ZENDESK_CF_REMINDER_ATTEMPTS, "value": 0},
        {"id": ZENDESK_CF_LAST_CALL_ID, "value": ""},
    ]

    payload: dict = {
        "ticket": {
            # ✅ 指定使用「預約專用 Form」
            "ticket_form_id": ZENDESK_APPOINTMENT_FORM_ID,
            "subject": ticket_subject,
            "comment": {"body": ticket_body},
            "requester_id": zendesk_customer_id,
            "status": "pending",
            "tags": ["line_bot_appointment", "pending_confirmation", "booking_sync"],
            "custom_fields": custom_fields,
        }
    }

    # ====== 3. 呼叫 Zendesk API ======
    try:
        app.logger.info(
            f"ZENDESK TICKET PAYLOAD: {json.dumps(payload, ensure_ascii=False)}"
        )
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        ticket = resp.json().get("ticket", {})
        ticket_id: int = ticket.get("id")
        app.logger.info(f"Zendesk Ticket 建立成功，ID: {ticket_id}")
        return resp.json()
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"Zendesk Ticket 建立失敗，HTTP 錯誤: {e.response.status_code}")
        app.logger.error(f"Zendesk 錯誤回應: {e.response.text}")
        return None
    except Exception as e:
        app.logger.error(f"Zendesk Ticket 建立過程中發生未知錯誤: {e}")
        return None
    
def find_zendesk_ticket_by_booking_id(booking_id):
    """
    給一個 Bookings appointment 的 booking_id，
    到 Zendesk 找對應的 Ticket（看 custom_field_XXXXX 裡的值）。

    回傳：
        - 有找到：回傳那一筆 ticket (dict)
        - 沒找到：回傳 None
    """
    if not booking_id:
        app.logger.warning("[find_zendesk_ticket_by_booking_id] 缺少 booking_id，略過搜尋")
        return None

    base_url, headers = _build_zendesk_headers()

    # 這裡用 custom_field_<ticket_field_id>:<value> 的新寫法
    # ZENDESK_CF_BOOKING_ID 是你的 ticket field id（例如 14459987905295）
    field_key = "custom_field_%s" % ZENDESK_CF_BOOKING_ID

    # booking_id 裡面有 = 等字元，包成雙引號比較安全
    query = 'type:ticket %s:"%s"' % (field_key, booking_id)

    search_url = "%s/api/v2/search.json" % base_url
    params = {"query": query}

    try:
        resp = requests.get(search_url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[find_zendesk_ticket_by_booking_id] 呼叫 Zendesk Search 失敗: {e}")
        return None

    data = resp.json()
    results = data.get("results") or []
    count = data.get("count", 0)

    app.logger.info(
        "[find_zendesk_ticket_by_booking_id] STATUS=%s, URL=%s, count=%s"
        % (resp.status_code, resp.url, count)
    )

    if not results:
        app.logger.info(
            "[find_zendesk_ticket_by_booking_id] 找不到 booking_id=%s 的 ticket" % booking_id
        )
        return None

    if len(results) > 1:
        app.logger.warning(
            "[find_zendesk_ticket_by_booking_id] 找到多筆 booking_id=%s 的 ticket，先取第一筆 id=%s"
            % (booking_id, results[0].get("id"))
        )

    return results[0]


    
# def find_zendesk_ticket_by_booking_id(booking_id):
#     """
#     用 Booking ID 在 Zendesk 找對應的 ticket。
#     - 找到：回傳該 ticket (dict)
#     - 找不到：回傳 None
#     """
#     if not booking_id:
#         app.logger.warning("[find_zendesk_ticket_by_booking_id] 缺 booking_id，直接回 None")
#         return None

#     base_url, headers = _build_zendesk_headers()
#     search_url = f"{base_url}/api/v2/search.json"

#     # ⚠️ 這裡的 cf_booking_id 要對應你 Zendesk Ticket Field 的「field key」
#     query = f"type:ticket cf_booking_id:{booking_id}"
#     params = {"query": query}

#     try:
#         resp = requests.get(search_url, headers=headers, params=params, timeout=10)
#         app.logger.info(
#             f"[find_zendesk_ticket_by_booking_id] STATUS={resp.status_code}, URL={resp.url}"
#         )
#         resp.raise_for_status()
#     except Exception as e:
#         app.logger.error(f"[find_zendesk_ticket_by_booking_id] 呼叫 Zendesk API 失敗: {e}")
#         return None

#     data = resp.json()
#     results = data.get("results") or []
#     count = data.get("count", 0)

#     # 沒找到
#     if count == 0:
#         app.logger.info(
#             f"[find_zendesk_ticket_by_booking_id] 找不到 booking_id={booking_id} 的 ticket"
#         )
#         return None

#     # 多筆 → 你應該只會有一筆，但如果有，先取第一筆
#     if count > 1:
#         app.logger.warning(
#             f"[find_zendesk_ticket_by_booking_id] booking_id={booking_id} 命中了 {count} 筆，取第一筆"
#         )

#     return results[0]


def mark_zendesk_ticket_confirmed(ticket_id: int):
    """
    使用者完成「確認回診」後，更新對應的 Zendesk ticket：

      - 將 reminder_state 改成 success
      - 將 ticket 狀態改成 solved

    Args:
        ticket_id: Zendesk ticket id
    """
    if not ticket_id:
        app.logger.warning("[mark_zendesk_ticket_confirmed] 缺少 ticket_id")
        return

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    payload = {
        "ticket": {
            "status": "solved",
            "custom_fields": [
                {
                    "id": ZENDESK_CF_REMINDER_STATE,
                    "value": "success"
                }
            ]
        }
    }

    app.logger.info(
        f"[mark_zendesk_ticket_confirmed] 更新 ticket_id={ticket_id}, payload="
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(
            f"[mark_zendesk_ticket_confirmed] 更新成功 ticket_id={ticket_id}"
        )
    except Exception as e:
        app.logger.error(f"[mark_zendesk_ticket_confirmed] 更新失敗: {e}")


def mark_zendesk_ticket_cancelled(ticket_id: int):
    """
    使用者「取消約診」後，更新該 ticket 狀態：

      - reminder_state 改成cancelled）
      - ticket 狀態改成 solved

    Args:
        ticket_id: Zendesk ticket id
    """
    if not ticket_id:
        app.logger.warning("[mark_zendesk_ticket_cancelled] 缺少 ticket_id")
        return

    base_url, headers = _build_zendesk_headers()
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"

    payload = {
        "ticket": {
            "status": "solved",
            "custom_fields": [
                {
                    "id": ZENDESK_CF_REMINDER_STATE,
                    "value": "cancelled"
                }
            ]
        }
    }

    app.logger.info(
        f"[mark_zendesk_ticket_cancelled] 更新 ticket_id={ticket_id}, payload="
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        app.logger.info(
            f"[mark_zendesk_ticket_cancelled] 更新成功 ticket_id={ticket_id}"
        )
    except Exception as e:
        app.logger.error(f"[mark_zendesk_ticket_cancelled] 更新失敗: {e}")




# def create_zendesk_appointment_ticket(
#     booking_id: str,
#     local_start_dt: datetime,
#     zendesk_customer_id: int, 
#     customer_name: str,
#     booking_service_name: str = "一般門診",
# ): 
#     """
#     在 Zendesk 內建立一個新的 Ticket，作為預約確認提醒的排程觸發點。
    
#     Args:
#         booking_id: Microsoft Bookings 的 appointment ID (字串)。
#         local_start_dt: 預約的台北時間 (datetime 物件)。
#         zendesk_customer_id: 該客戶在 Zendesk 內的 ID (Requester ID，整數)。
#         customer_name: 客戶姓名 (字串)。
#         booking_service_name: 預約服務名稱 (字串)。

#     Returns:
#         成功建立的 Ticket JSON (字典)，失敗返回 None。
#     """
#     # 檢查必要的全域變數是否存在
#     try:
#         # 使用 ZENDESK_SUBDOMAIN 和 ZENDESK_API_TOKEN
#         base_url: str = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com"
#         # 確保 APPOINTMENT_DURATION_MINUTES 存在
#         duration_minutes: int = APPOINTMENT_DURATION_MINUTES
        
#         # 預先計算結束時間
#         local_end_dt: datetime = local_start_dt + timedelta(minutes=duration_minutes)
#     except NameError as e:
#         app.logger.error(f"Zendesk 全域變數未定義 (例如 {e})，無法建立 Ticket。請檢查 ZENDESK_SUBDOMAIN 或 APPOINTMENT_DURATION_MINUTES。")
#         return None
#     except Exception:
#         # 如果 APPOINTMENT_DURATION_MINUTES 有問題，使用預設值
#         app.logger.warning("APPOINTMENT_DURATION_MINUTES 定義有誤或缺失，使用預設 30 分鐘計算結束時間。")
#         local_end_dt: datetime = local_start_dt + timedelta(minutes=30)
    
    
#     url: str = f"{base_url}/api/v2/tickets.json"

#     # 使用您函式中的認證方式 (使用 ZENDESK_EMAIL / ZENDESK_API_TOKEN)
#     auth_str: str = f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}"
#     auth_bytes: bytes = auth_str.encode("utf-8")
#     auth_header: str = base64.b64encode(auth_bytes).decode("utf-8")

#     headers: dict = {
#         "Authorization": f"Basic {auth_header}",
#         "Content-Type": "application/json",
#     }
    
#     # 1. 建立 Ticket 內容
#     ticket_subject: str = f"【預約提醒】{customer_name}，將於 {local_start_dt.strftime('%Y/%m/%d %H:%M')} 看診"
#     ticket_body: str = (
#         f"這是一個由 LINE Bot 自動建立的預約提醒 Ticket。\n"
#         f"🚨 請在 **預約日期前 3 天** 確認此 Ticket 狀態。\n\n"
#         f"--- 預約細節 ---\n"
#         f"Bookings ID: {booking_id}\n"
#         f"客戶 ID (Zendesk): {zendesk_customer_id}\n"
#         f"預約時間: {local_start_dt.strftime('%Y/%m/%d %H:%M')} (UTC+8) - {local_end_dt.strftime('%H:%M')}\n"
#         f"服務項目: {booking_service_name}\n\n"
#         f"--- 提醒流程 ---\n"
#         f"如果到期時，Bookings 備註內『尚未』包含 'Confirmed via LINE'，"
#         f"則需要手動或透過 Zendesk Trigger 通知 LINE Bot 進行回呼確認。"
#     )

#     payload: dict = {
#         "ticket": {
#             "subject": ticket_subject,
#             "comment": {
#                 "body": ticket_body,
#             },
#             # 這是關鍵：將 Ticket 歸屬於該 Zendesk Customer ID
#             "requester_id": zendesk_customer_id,
#             # 初始狀態設為 Pending，代表待處理/待確認
#             "status": "pending",
#             # 設定 Tag，方便 Zendesk Trigger 識別這是 LINE Bot 預約提醒
#             "tags": ["line_bot_appointment", "pending_confirmation", "booking_sync"],
#         }
#     }

#     # 2. 呼叫 Zendesk API
#     try:
#         resp = requests.post(url, headers=headers, json=payload, timeout=10)
#         resp.raise_for_status()  # 處理 HTTP 錯誤
#         ticket_id: int = resp.json().get('ticket', {}).get('id')
#         app.logger.info(f"Zendesk Ticket 建立成功，ID: {ticket_id}")
#         return resp.json()
#     except requests.exceptions.HTTPError as e:
#         # 使用 app.logger 記錄錯誤
#         app.logger.error(f"Zendesk Ticket 建立失敗，HTTP 錯誤: {e.response.status_code}")
#         app.logger.error(f"Zendesk 錯誤回應: {e.response.text}")
#         return None
#     except Exception as e:
#         # 使用 app.logger 記錄其他錯誤
#         app.logger.error(f"Zendesk Ticket 建立過程中發生未知錯誤: {e}")
#         return None
    

    
# --- 輔助函式：取得指定日期所有預約 (實際呼叫 Graph API) ---
def list_appointments_for_date(date_str: str) -> list:
    """
    從 Bookings 取得指定日期 (台北時間, YYYY-MM-DD) 的所有預約列表。
    回傳: 預約列表 (list of dict)
    """
    token: str = get_graph_token()
    business_id: str = os.environ.get("BOOKING_BUSINESS_ID") or BOOKING_BUSINESS_ID

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID，請檢查環境變數。")

    # 1. 計算 UTC 範圍 (將台北時間 T+08:00 轉換為 UTC)
    try:
        # 台北時間 (UTC+8) 的 00:00:00
        local_start_dt: datetime = datetime.strptime(f"{date_str} 00:00:00", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        app.logger.error(f"日期格式錯誤，請使用 YYYY-MM-DD: {date_str}")
        return []

    local_end_dt: datetime = local_start_dt + timedelta(days=1)

    # 轉為 UTC 時間 (減 8 小時)
    utc_start_dt: datetime = local_start_dt - timedelta(hours=8)
    utc_end_dt: datetime = local_end_dt - timedelta(hours=8)

    # 格式化為 Graph API 要求的 ISO 格式
    start_time: str = utc_start_dt.isoformat() + "Z"
    end_time: str = utc_end_dt.isoformat() + "Z"

    # 2. 呼叫 calendarView API
    url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/calendarView"

    headers: dict = {
        "Authorization": f"Bearer {token}"
    }

    params: dict = {
        "start": start_time,
        "end": end_time
    }

    # 執行 API 呼叫
    resp = requests.get(url, headers=headers, params=params)
    app.logger.info(
        f"CALENDAR VIEW STATUS: {resp.status_code}, URL: {resp.url}")

    resp.raise_for_status()

    # calendarView 回傳的結果已經是該日期範圍內 (UTC+8) 的預約
    return resp.json().get("value", [])
    
# def list_appointments_for_date(date_str):
#     """
#     取得某一天的所有預約
#     """
#     token = get_graph_token()
#     business_id = os.environ.get("BOOKING_BUSINESS_ID")

#     if not business_id:
#         raise Exception("缺 BOOKING_BUSINESS_ID，先到終端機 export")

#     url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"

#     headers = {
#         "Authorization": f"Bearer {token}"
#     }

#     resp = requests.get(url, headers=headers)
#     app.logger.info(
#         f"APPOINTMENTS STATUS: {resp.status_code}, BODY: {resp.text}")

#     resp.raise_for_status()

#     all_appts = resp.json().get("value", [])

#     result = []
#     for a in all_appts:
#         start_info = a.get("startDateTime", {})
#         start_dt_str = start_info.get("dateTime")
#         if not start_dt_str:
#             continue

#         try:
#             s = start_dt_str
#             if s.endswith("Z"):
#                 s = s[:-1]
#             s = s.split(".")[0]
#             utc_dt = datetime.fromisoformat(s)
#         except Exception as e:
#             app.logger.error(
#                 f"解讀 startDateTime 失敗: {start_dt_str}, error: {e}")
#             continue

#         # 轉成台北時間（UTC+8）'YYYY-MM-DD'
#         local_dt = utc_dt + timedelta(hours=8)
#         local_date_str = local_dt.date().isoformat()

#         if local_date_str == date_str:
#             result.append(a)

#     return result

def list_appointments_for_range(start_local: datetime, end_local: datetime):
    """
    一次從 Bookings 抓「某個時間範圍內」所有 appointments。

    傳入的 start_local / end_local 是「台北時間（naive）」，
    我們會轉成 UTC 後呼叫 Graph API：
    GET /solutions/bookingBusinesses/{business_id}/appointments?
        startDateTime=...&endDateTime=...

    回傳：list[dict]（appointments 清單）
    """
    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")
    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    # 先把台北時間（UTC+8）轉成 UTC 時間
    start_utc = start_local - timedelta(hours=8)
    end_utc = end_local - timedelta(hours=8)

    # 轉成 ISO 格式，補上 Z
    start_iso = start_utc.replace(microsecond=0).isoformat() + "Z"
    end_iso = end_utc.replace(microsecond=0).isoformat() + "Z"

    url = (
        f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
        f"?startDateTime={start_iso}&endDateTime={end_iso}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"LIST APPTS RANGE STATUS: {resp.status_code}, BODY: {resp.text[:500]}"
    )
    resp.raise_for_status()

    data = resp.json()
    # 通常 Graph 會把結果放在 value 裡
    return data.get("value", [])


# DEMO 測試的
def get_next_upcoming_appointment_for_demo():
    """
    取得患者「最近一筆未來的約診」。（DEMO）
    - startDateTime > 現在
    - 只看 Bookings 裡 customerEmailAddress == DEMO_CUSTOMER_EMAIL 的預約
    - 如果沒有符合條件，回傳 (None, None)
    - 如果有，回傳 (appointment_dict, local_start_dt)
    """
    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID，請在終端機 export")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"APPOINTMENTS (for upcoming demo) STATUS: {resp.status_code}, BODY: {resp.text}")
    resp.raise_for_status()

    all_appts = resp.json().get("value", [])

    now_local = datetime.now()
    best_appt = None
    best_local_start = None

    for a in all_appts:
        # 如果 Bookings 有 isCancelled 之類的欄位，可以在這裡排除
        if a.get("isCancelled") is True:
            continue

        # 只看 DEMO 患者的預約（用 email 過濾）
        customer_email = (a.get("customerEmailAddress") or "").lower()
        if customer_email != DEMO_CUSTOMER_EMAIL.lower():
            continue

        start_info = a.get("startDateTime", {})
        local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
        if not local_dt:
            continue

        # 只看未來的預約
        if local_dt <= now_local:
            continue

        # 找最近的一筆（時間最早）
        if best_local_start is None or local_dt < best_local_start:
            best_local_start = local_dt
            best_appt = a

    return best_appt, best_local_start

def normalize_phone(phone: str) -> str:
    """
    將電話號碼轉成統一格式，用來比對：
    - 只留數字
    - 把 886 開頭的改成 0 開頭（例如 +8869xxxx → 09xxxx）
    """
    if not phone:
        return ""

    # 只留數字
    digits = "".join(ch for ch in phone if ch.isdigit())

    # 處理台灣號碼：+8869xxx or 8869xxx → 09xxx
    if digits.startswith("8869"):
        digits = "0" + digits[3:]  # 8869xxxxxxxx → 09xxxxxxxx

    return digits


def get_appointment_by_id(appt_id: str):
    """
    用 Bookings appointment id 取得單一預約資訊。
    回傳 (appointment_dict, local_start_dt)；
    找不到或解析失敗則回 (None, None)。
    """
    if not appt_id:
        return None, None

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.get(url, headers=headers)
    app.logger.info(
        f"GET APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

    if resp.status_code == 404:
        # 已被刪除或不存在
        return None, None

    resp.raise_for_status()
    appt = resp.json()

    app.logger.info(f"APPOINTMENT KEYS: {list(appt.keys())}")
    app.logger.info(
        f"APPT NOTES FIELDS: serviceNotes={appt.get('serviceNotes')}, "
        f"customerNotes={appt.get('customerNotes')}"
    )

    start_info = appt.get("startDateTime", {})
    local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
    if not local_dt:
        return None, None

    return appt, local_dt


def cancel_booking_appointment(appt_id: str):
    """
    DEMO 版：直接呼叫 DELETE 取消 Bookings appointment。
    （正式版如果要改成「標記取消」也可以，改這裡就好。）
    """
    if not appt_id:
        raise Exception("cancel_booking_appointment: appt_id 為空")

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}"
    }

    resp = requests.delete(url, headers=headers)
    app.logger.info(
        f"DELETE APPOINTMENT {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")

    # 204 No Content / 200 / 202
    if resp.status_code not in (200, 202, 204):
        resp.raise_for_status()


def update_booking_service_notes(appt_id: str, notes_text: str):
    """
    將指定 appointment 的 serviceNotes 更新為 notes_text。(診所／工作人員可以看的備註)
    """
    if not appt_id:
        raise Exception("update_booking_service_notes: appt_id 為空")

    token = get_graph_token()
    business_id = os.environ.get("BOOKING_BUSINESS_ID")

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    url = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments/{appt_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "serviceNotes": notes_text
    }

    resp = requests.patch(url, headers=headers, json=payload)
    app.logger.info(
        f"PATCH APPT SERVICE NOTES {appt_id} STATUS: {resp.status_code}, BODY: {resp.text}")
    resp.raise_for_status()


def parse_booking_datetime_to_local(start_dt_str: str) -> datetime | None:
    """
    將 Bookings 的 startDateTime.dateTime (UTC) 字串轉成「台北時間 datetime」。
    例如 "2025-11-20T06:00:00.0000000Z" → 2025-11-20 14:00:00 (UTC+8)
    """
    if not start_dt_str:
        return None

    try:
        s = start_dt_str
        if s.endswith("Z"):
            s = s[:-1]
        s = s.split(".")[0]
        utc_dt = datetime.fromisoformat(s)
    except Exception as e:
        app.logger.error(
            f"解讀 Bookings dateTime 失敗: {start_dt_str}, error: {e}")
        return None

    # 轉成台北時間（UTC+8）
    local_dt = utc_dt + timedelta(hours=8)
    return local_dt


def get_available_slots_for_date(date_str: str) -> list:
    """
    回傳指定日期「可預約」的時段列表，例如：
    ["09:00", "09:30", "10:00", ...]
    規則：SLOT_START–SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘，排除當天已被預約的開始時段。
    """
    appts: list = list_appointments_for_date(date_str)

    booked_times: set = set()
    for appt in appts:
        start_info: dict = appt.get("startDateTime", {})
        # "2025-11-20T06:00:00.0000000Z"
        start_dt_str: str = start_info.get("dateTime")
        if not start_dt_str:
            continue

        try:
            s: str = start_dt_str
            if s.endswith("Z"):
                s = s[:-1]
            s = s.split(".")[0]
            utc_dt: datetime = datetime.fromisoformat(s)
        except Exception as e:
            app.logger.error(
                f"解讀 startDateTime 失敗（get_available_slots）：{start_dt_str}, error: {e}")
            continue

        local_dt: datetime = utc_dt + timedelta(hours=8)
        hhmm: str = local_dt.strftime("%H:%M")  # 例如 "14:00"
        booked_times.add(hhmm)

    # SLOT_START ~ SLOT_END，每 SLOT_INTERVAL_MINUTES 分鐘一格
    # 這裡假設日期是今天，只取時間部分
    start_dt_only: datetime = datetime.strptime(SLOT_START, "%H:%M").replace(year=2000, month=1, day=1)
    end_dt_only: datetime = datetime.strptime(SLOT_END, "%H:%M").replace(year=2000, month=1, day=1)


    slots: list = []
    cur: datetime = start_dt_only
    while cur <= end_dt_only:
        hhmm: str = cur.strftime("%H:%M")
        if hhmm not in booked_times:
            slots.append(hhmm)
        cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    return slots

def create_booking_appointment(
    date_str: str,
    time_str: str,
    customer_name: str,
    customer_phone: str,
    zendesk_customer_id: str, # <--- 修正為 str
    line_display_name: str = None,
    line_user_id: str = None,
):
    """
    建立一筆 Bookings 預約。
    - 改用真實病患資料（Zendesk 的姓名＋手機）
    - customerName：姓名 +（LINE 名稱）→ 例如：王凱文（Kevin）
    - serviceNotes：第一行寫入 [LINE_USER] <line_user_id>，方便後續排程／查詢
    
    並在成功後，自動建立 Zendesk Ticket 進行提醒排程。
    回傳: 建立的預約 dict。
    """

    token: str = get_graph_token()
    business_id: str = os.environ.get("BOOKING_BUSINESS_ID") or BOOKING_BUSINESS_ID 

    if not business_id:
        raise Exception("缺 BOOKING_BUSINESS_ID")

    # --- 1. 準備 Bookings Payload (邏輯與您的原始碼一致) ---
    local_str: str = f"{date_str} {time_str}:00"
    local_dt: datetime = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S") # 預約的台北時間 (UTC+8)

    # Bookings API 使用 UTC 台北時間 - 8 小時
    utc_dt: datetime = local_dt - timedelta(hours=8)
    utc_iso: str = utc_dt.isoformat() + "Z"

    # 要寫進 Bookings 的姓名
    if line_display_name:
        booking_customer_name: str = f"{customer_name}（{line_display_name}）"
    else:
        booking_customer_name: str = customer_name

    # 預先組好 serviceNotes
    service_notes_lines: list = []
    if line_user_id:
        service_notes_lines.append(f"[LINE_USER] {line_user_id}")
    service_notes: str = "\n".join(service_notes_lines) if service_notes_lines else None

    # URL 和 Duration 常數
    url: str = f"https://graph.microsoft.com/v1.0/solutions/bookingBusinesses/{business_id}/appointments"
    duration: int = APPOINTMENT_DURATION_MINUTES 

    payload: dict = {
        "customerName": booking_customer_name,
        "customerEmailAddress": None,
        "customerPhone": customer_phone,
        "serviceId": BOOKING_DEMO_SERVICE_ID,
        "serviceName": "一般門診",
        "startDateTime": { "dateTime": utc_iso, "timeZone": "UTC" },
        "endDateTime": {
            "dateTime": (utc_dt + timedelta(minutes=duration)).isoformat() + "Z",
            "timeZone": "UTC",
        },
        "priceType": "free",
        "price": 0.0,
        "smsNotificationsEnabled": False,
        "staffMemberIds": [BOOKING_DEMO_STAFF_ID],
        "maximumAttendeesCount": 1,
        "filledAttendeesCount": 1,
    }

    # 有內容時才塞 serviceNotes
    if service_notes:
        payload["serviceNotes"] = service_notes

    headers: dict = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # --- 2. 建立 Bookings 預約 ---
    resp = requests.post(url, headers=headers, json=payload)
    app.logger.info(f"CREATE APPT STATUS: {resp.status_code}, BODY: {resp.text}")

    resp.raise_for_status()
    created_booking: dict = resp.json()
    
    # --- 3. 整合功能：呼叫 Zendesk Ticket 建立 (在 Bookings 成功後) ---
    # 這裡檢查 zendesk_customer_id 是否存在，並將其從 str 轉換為 int
    if zendesk_customer_id:
        try:
            zendesk_id_int: int = int(zendesk_customer_id)
        except ValueError:
            app.logger.error(f"Zendesk User ID 無法轉換為整數: {zendesk_customer_id}，跳過建立 Ticket 流程。")
            return created_booking

        booking_id: str = created_booking.get("id")
        if not booking_id:
            app.logger.error("Bookings 預約建立成功，但未取得 Bookings ID，無法建立 Zendesk Ticket。")
        else:
            ticket_result: dict = create_zendesk_appointment_ticket(
                booking_id=booking_id,
                local_start_dt=local_dt, 
                zendesk_customer_id=zendesk_id_int, # 傳入 int
                customer_name=customer_name,
            )
            if ticket_result:
                app.logger.info(f"Zendesk Ticket ID: {ticket_result.get('ticket', {}).get('id')}")
            else:
                app.logger.error("Zendesk Ticket 建立失敗。")
    else:
        app.logger.warning("未取得 Zendesk User ID，跳過建立預約 Ticket 流程。")


    return created_booking


def get_days_until(local_dt: datetime) -> int:
    """
    傳入「台北時間的預約起始 datetime」，回傳「距離今天還有幾天」（用日曆天數）。
    例：今天 12/10，預約 12/13 → 回傳 3。
    """
    today = datetime.now().date()
    appt_date = local_dt.date()
    return (appt_date - today).days


def build_slots_carousel(date_str: str, slots: list[str]) -> TemplateMessage:
    """
    將某一天的可預約時段變成 LINE CarouselTemplate。
    slots 例如：["09:00", "09:30", "10:00", ...]
    每個 column 要固定 3 個 actions才符合 LINE 要求。
    """
    columns = []
    BUTTONS_PER_COLUMN = 3

    for i in range(0, len(slots), BUTTONS_PER_COLUMN):
        chunk = slots[i:i+BUTTONS_PER_COLUMN]

        actions = []
        for idx in range(BUTTONS_PER_COLUMN):
            if idx < len(chunk):
                # 真正有時段的按鈕
                time_str = chunk[idx]
                msg_text = f"我想預約 {date_str} {time_str}"
                actions.append(
                    MessageAction(
                        label=time_str,
                        text=msg_text,
                    )
                )
            else:
                # 用「空白按鈕」補滿，避免不同 column actions 數量不同
                actions.append(
                    MessageAction(
                        label="　",  # 全形空白，看起來像空格
                        text="請選擇上方有時間的按鈕",
                    )
                )

        # 可以不用顯示
        col_index = (i // BUTTONS_PER_COLUMN) + 1
        columns.append(
            CarouselColumn(
                # title=f"{date_str}（第 {col_index} 組）",
                title=f"{date_str}",
                text="請選擇看診時段",
                actions=actions,
            )
        )

    return TemplateMessage(
        alt_text=f"{date_str} 可預約時段",
        template=CarouselTemplate(columns=columns),
    )

def get_future_appointments_for_line_user(line_user_id: str, max_days: int = 30) -> list[tuple[datetime, dict]]:
    """
    取得指定 LINE 使用者從「現在起 ~ 未來 max_days 天內」的所有預約（已排序）。

    回傳：
        [(local_start_dt, appt_dict), ...]
    若找不到 / 發生錯誤，回傳 []。
    """

    matched: list[tuple[datetime, dict]] = []

    # ① 先從 Zendesk 找 user，拿 phone 當備援 key
    try:
        count, zd_user = search_zendesk_user_by_line_id(line_user_id)
    except Exception as e:
        app.logger.error(f"[get_future_for_line] 用 line_user_id 查 Zendesk user 失敗: {e}")
        return []

    if not zd_user:
        app.logger.info(f"[get_future_for_line] line_user_id={line_user_id} 在 Zendesk 中查無使用者")
        return []

    raw_phone = zd_user.get("phone") or ""
    target_phone = normalize_phone(raw_phone)
    if not target_phone:
        app.logger.info(f"[get_future_for_line] Zendesk user 沒有 phone，之後僅用 [LINE_USER] 比對")
        target_phone = ""

    # ② 準備查詢範圍：現在 ~ 未來 max_days 天（台北時間，naive）
    now_local = datetime.now()
    end_local = now_local + timedelta(days=max_days)

    app.logger.info(
        f"[get_future_for_line] 查詢範圍：{now_local} ~ {end_local}, line_user_id={line_user_id}"
    )

    try:
        appts = list_appointments_for_range(now_local, end_local)
    except Exception as e:
        app.logger.error(f"[get_future_for_line] list_appointments_for_range 失敗: {e}")
        return []

    app.logger.info(
        f"[get_future_for_line] 範圍內共取得 {len(appts)} 筆 appointments"
    )

    for appt in appts:
        appt_phone = normalize_phone(appt.get("customerPhone") or "")
        service_notes = appt.get("serviceNotes") or ""

        # ③ 比對條件：
        #    - phone 完全一致，或
        #    - serviceNotes 有 [LINE_USER] 且包含 line_user_id
        matched_by_phone = (target_phone and appt_phone and appt_phone == target_phone)
        matched_by_line_id = (
            line_user_id
            and "[LINE_USER]" in service_notes
            and line_user_id in service_notes
        )

        if not (matched_by_phone or matched_by_line_id):
            continue

        # ④ 解析 startDateTime → 先當 UTC，再 +8 小時變台北時間（naive）
        start_info = appt.get("startDateTime") or {}
        start_str = start_info.get("dateTime")
        if not start_str:
            continue

        try:
            # 常見格式："2025-11-25T07:00:00Z" 或 "2025-11-25T07:00:00+00:00"
            cleaned = start_str.replace("Z", "")
            dt_utc = datetime.fromisoformat(cleaned)
            if dt_utc.tzinfo is not None:
                dt_utc = dt_utc.replace(tzinfo=None)
        except Exception:
            app.logger.warning(f"[get_future_for_line] 無法解析 startDateTime: {start_str}")
            continue

        local_start = dt_utc + timedelta(hours=8)

        # 只考慮「現在之後」的約診（同一天但時間已過就跳過）
        if local_start < now_local:
            continue

        matched.append((local_start, appt))

    if not matched:
        app.logger.info("[get_future_for_line] 找不到符合條件的預約")
        return []

    # ⑤ 依照時間排序（由近到遠）
    matched.sort(key=lambda x: x[0])
    app.logger.info(f"[get_future_for_line] 共 {len(matched)} 筆屬於該 LINE 使用者的 future 預約")
    return matched

# version 4
def flow_query_next_appointment(event, text: str):
    """
    約診查詢 Flow：
    改用 line_user_id + Zendesk phone 過濾 Bookings，
    顯示「這位 LINE 使用者」的所有 future 預約（Carousel）。
    """
    # 先拿 LINE userId
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    try:
        if line_user_id:
            matched_list = get_future_appointments_for_line_user(line_user_id)
        else:
            matched_list = []
    except Exception as e:
        app.logger.error(f"查詢約診失敗: {e}")
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="約診查詢失敗，請稍後再試")]
            )
        )
        return

    # ① 沒有任何他的 future 預約，引導去線上約診（沿用原本行為）
    if not matched_list:
        buttons_template = ButtonsTemplate(
            title="目前沒有約診紀錄",
            text="若需預約看診，請點擊「線上預約」。",
            actions=[
                MessageAction(
                    label="線上約診",
                    text="線上約診"
                ),
            ],
        )

        template_message = TemplateMessage(
            alt_text="沒有約診紀錄",
            template=buttons_template
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[template_message]
            )
        )
        return

    # ② 有 future 預約 → 組成 Carousel
    columns: list[CarouselColumn] = []

    # LINE Carousel 最多 10 個 column，超過先截斷並記 log
    if len(matched_list) > 10:
        app.logger.info(
            f"[flow_query_next_appointment] 預約筆數 {len(matched_list)} 超過 10，僅顯示前 10 筆"
        )
        matched_list = matched_list[:10]

    for local_start, appt in matched_list:
        days_left = get_days_until(local_start)

        display_date = local_start.strftime("%Y/%m/%d")
        display_time = local_start.strftime("%H:%M")

        customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
        appt_id = appt.get("id", "")

        service_notes = appt.get("serviceNotes") or ""
        is_confirmed = CONFIRM_NOTE_KEYWORD in service_notes

        # Title：日期 + 時間
        title = f"{display_date} {display_time}"

        actions = []

        # ②-0 若已在 LINE 確認過 → 顯示「已確認」版本，兩個 action 也都要存在
        if is_confirmed:
            text_body = f"{customer_name}\n已完成回診確認，請準時報到。"
            # 第一顆：無動作按鈕（白按鈕）
            actions.append(
                PostbackAction(
                    label="　",       # 全形空白（看起來像空白按鈕）
                    data="NOOP",      # 不會觸發任何後端事件
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # ②-1 距離看診 >= 3 天 → 可取消
        elif days_left >= 3:
            text_body = f"{customer_name}\n距離看診還有 {days_left} 天，可取消。"
            actions.append(
                PostbackAction(
                    label="取消約診",
                    data=f"CANCEL_APPT:{appt_id}",
                    display_text="取消約診",
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # ②-2 距離看診 < 3 天 → 不能取消，只能確認
        else:
            text_body = f"{customer_name}\n距離看診少於三天，可回診確認。"
            actions.append(
                PostbackAction(
                    label="確認回診",
                    data=f"CONFIRM_APPT:{appt_id}",
                    display_text="確認回診",
                )
            )
            actions.append(
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置",
                )
            )

        # 防呆：確保每個 column 至少有兩個 actions（符合 LINE Carousel 規則）
        while len(actions) < 2:
            actions.append(
                MessageAction(
                    label="約診查詢",
                    text="約診查詢",
                )
            )

        # 防呆：LINE 規格 text 要有內容
        if not text_body:
            text_body = customer_name

        column = CarouselColumn(
            title=title,
            text=text_body,
            actions=actions,
        )
        columns.append(column)

    carousel = CarouselTemplate(columns=columns)
    template_message = TemplateMessage(
        alt_text="您的門診預約列表",
        template=carousel
    )

    # 前面加一段說明文字
    intro_text = (
        f"共找到 {len(columns)} 筆未來門診預約：\n"
        "請在列表中選擇要「確認回診」或「取消約診」的那一筆。"
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=intro_text),
                template_message
            ]
        )
    )
    return

def get_next_upcoming_appointment_for_line_user(line_user_id: str, max_days: int = 30):
    """
    依照 LINE userId 找「未來最近一筆」屬於他的預約。

    ✅ 現在內部改成呼叫 get_future_appointments_for_line_user，
      但對外行為不變：回傳 (appt, local_start) 或 (None, None)
    """
    matched = get_future_appointments_for_line_user(line_user_id, max_days=max_days)

    if not matched:
        return None, None

    local_start, appt = matched[0]
    app.logger.info(
        f"[get_next_for_line_range] 命中預約 id={appt.get('id')} local_start={local_start}"
    )
    return appt, local_start


def flow_cancel_request(event, text: str):
    """
    Flow：處理「取消約診 {id}」
    - 優先用傳進來的 appt_id
    - 如果沒有帶 id，就用目前這個 LINE 使用者的預約來當目標（不再用 demo 全診所那種）
    """
    parts = text.split()
    appt_id = parts[1] if len(parts) >= 2 else ""

    # 先拿 LINE userId（用於沒帶 id 的 fallback）
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    # ① 沒帶 id → 用這個 LINE 使用者自己的最近一筆 future 預約
    if not appt_id:
        if not line_user_id:
            # 理論上不會發生，但防呆一下
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="暫時無法取得您的身分，請稍後再試或重新點選「約診查詢」。")]
                )
            )
            return

        appt, local_start = get_next_upcoming_appointment_for_line_user(line_user_id)

    # ② 有帶 id → 直接依 id 查那一筆
    else:
        appt, local_start = get_appointment_by_id(appt_id)

    # ③ 找不到可取消的約診
    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到可取消的約診，請先使用「約診查詢」。")]
            )
        )
        return

    # ④ 判斷距離看診日
    days_left = get_days_until(local_start)
    if days_left < 3:
        msg = (
            "距離看診日已少於三天，無法透過 LINE 取消約診。\n"
            "如有特殊狀況請致電診所。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # ⑤ 組畫面
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
    appt_id = appt.get("id", "")

    detail_text = (
        "您即將取消以下約診：\n"
        f"姓名：{customer_name}\n"
        f"看診時間：{display_date} {display_time}\n\n"
        "確定要取消嗎？"
    )

    buttons_template = ButtonsTemplate(
        title="確認取消約診",
        text="請選擇是否取消本次約診。",
        actions=[
            # 這裡我們已經改成 PostbackAction 了，如果你還沒改可以先保留舊版
            PostbackAction(
                label="確認取消",
                data=f"CANCEL_CONFIRM:{appt_id}",
                display_text="確認取消",
            ),
            PostbackAction(
                label="保留約診",
                data="CANCEL_KEEP",
                display_text="保留約診",
            ),
        ],
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=detail_text),
                TemplateMessage(alt_text="確認取消約診", template=buttons_template),
            ]
        )
    )
    return

def flow_confirm_cancel(event, text: str):
    """
    Flow：處理「確認取消 {id}」
    規則：
    - 只允許看診日前 ≥ 3 天取消
    - 成功取消 Bookings 後，同步把對應的 Zendesk ticket 標記為「取消 / 不需再提醒」
    """
    parts = text.split()
    appt_id = parts[1] if len(parts) >= 2 else ""

    # 先拿 LINE userId（如果之後想支援「沒帶 id 的取消」，可以用這個做 fallback）
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    if not appt_id:
        # 目前 UI 設計理論上一定會帶 id，這裡先保守處理
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="要取消的資訊不完整，請重新透過「約診查詢」進行操作。")]
            )
        )
        return

    # 再查一次這筆約診（避免早就被改時間或取消）
    appt, local_start = get_appointment_by_id(appt_id)
    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到這筆約診，請重新查詢。")]
            )
        )
        return

    days_left = get_days_until(local_start)
    if days_left < 3:
        msg = (
            "距離看診日已少於三天，無法透過 LINE 取消約診。\n"
            "如有特殊狀況請電話聯繫診所。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # 真的取消（DELETE Bookings appointment）
    try:
        cancel_booking_appointment(appt_id)
    except Exception as e:
        app.logger.error(f"取消預約失敗: {e}")
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="取消時發生錯誤，請稍後再試")]
            )
        )
        return

    # --- 同步更新 Zendesk ticket：這筆 booking 已經取消，不用再提醒 ---
    booking_id = appt.get("id") or appt_id
    if booking_id:
        try:
            ticket = find_zendesk_ticket_by_booking_id(booking_id)
            if ticket:
                ticket_id = ticket.get("id")
                mark_zendesk_ticket_cancelled(ticket_id)
            else:
                app.logger.info(
                    f"[flow_confirm_cancel] 找不到對應 booking_id={booking_id} 的 ticket，略過同步。"
                )
        except Exception as e:
            app.logger.error(f"[flow_confirm_cancel] 更新 Zendesk ticket 失敗: {e}")
    else:
        app.logger.warning("[flow_confirm_cancel] 這筆 appt 沒有 id，無法同步 Zendesk ticket")

    # === 回覆給使用者 ===
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME

    msg = (
        "已為您取消以下約診：\n"
        f"姓名：{customer_name}\n"
        f"時間：{display_date} {display_time}"
    )

    buttons_template = ButtonsTemplate(
        title="需要重新約診嗎？",
        text="如需重新預約請點選「線上約診」。",
        actions=[
            MessageAction(label="線上約診", text="線上約診"),
        ],
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[
                TextMessage(text=msg),
                TemplateMessage(alt_text="約診已取消", template=buttons_template),
            ]
        )
    )
    return


def flow_confirm_visit(event, text: str):
    """
    Flow：處理「確認回診 {id}」
    規則：
    - 只允許看診日前 < 3 天確認
    - serviceNotes 已含 CONFIRM_NOTE_KEYWORD → 不再 PATCH，只回「已確認」
    - 第一次確認時，寫入一行 `Confirmed via LINE on ...`
    並同步更新 Zendesk Ticket 狀態（success + solved）
    """
    parts = text.split(maxsplit=1)
    appt_id = parts[1].strip() if len(parts) >= 2 else ""

    # 先拿 LINE userId（給「沒帶 id」的 fallback 用）
    line_user_id = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id = event.source.user_id

    # 沒帶 id → 用這個 LINE 使用者的最近一筆 future 預約
    if not appt_id:
        if not line_user_id:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="暫時無法取得您的身分，請稍後再試或重新點選「約診查詢」。")]
                )
            )
            return
        appt, local_start = get_next_upcoming_appointment_for_line_user(line_user_id)
    else:
        appt, local_start = get_appointment_by_id(appt_id)

    if not appt or not local_start:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="找不到需要確認的約診，請先使用「約診查詢」確認預約狀態。")]
            )
        )
        return

    days_left = get_days_until(local_start)
    display_date = local_start.strftime("%Y/%m/%d")
    display_time = local_start.strftime("%H:%M")
    customer_name = appt.get("customerName") or DEMO_CUSTOMER_NAME
    appt_id = appt.get("id", "")

    # ① 太早確認（≥ 3 天） → 擋掉
    if days_left >= 3:
        msg = (
            "目前距離看診日仍大於三天，暫不開放線上確認回診。\n"
            "可於看診前三天內再透過 LINE 進行確認。"
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
        return

    # ② 看這筆約診是不是已經 Confirm 過
    service_notes = appt.get("serviceNotes") or ""
    already_confirmed = (CONFIRM_NOTE_KEYWORD in service_notes)

    # 已確認 → 不再 PATCH，只回提示＋位置按鈕
    if already_confirmed:
        detail_text = (
            "您已完成回診確認\n"
            f"姓名：{customer_name}\n"
            f"看診時間：{display_date} {display_time}\n"
            "\n如需導航，可點選下方「查詢診所位置」。"
        )
        detail_message = TextMessage(text=detail_text)

        buttons_template = ButtonsTemplate(
            title="回診資訊確認",
            text="預約已確認，如需導航請點選下方。",
            actions=[
                MessageAction(
                    label="查詢診所位置",
                    text="查詢診所位置"
                ),
            ],
        )

        template_message = TemplateMessage(
            alt_text="已確認回診資訊",
            template=buttons_template
        )

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[detail_message, template_message]
            )
        )
        return  # ⬅ 一定要 return，避免下面再 PATCH

    # ③ 尚未確認 → 這裡才會真的 PATCH，一次寫入 Confirmed via LINE
    now_local = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    new_line = f"{CONFIRM_NOTE_KEYWORD} on {now_local} (UTC+8)"

    if service_notes:
        merged_notes = service_notes + "\n" + new_line
    else:
        merged_notes = new_line

    # 先試著更新 Bookings 備註（失敗只記 log，不擋流程）
    try:
        update_booking_service_notes(appt_id, merged_notes)
    except Exception as e:
        app.logger.error(f"更新 Bookings 備註失敗: {e}")
        # 寫備註失敗不影響使用者體驗，只記 log

    # --- 同步更新 Zendesk ticket 狀態 ---
    booking_id = appt.get("id")
    if booking_id:
        try:
            ticket = find_zendesk_ticket_by_booking_id(booking_id)
            if ticket:
                ticket_id = ticket.get("id")
                mark_zendesk_ticket_confirmed(ticket_id)
            else:
                app.logger.info(
                    f"[flow_confirm_visit] 找不到對應 booking_id={booking_id} 的 ticket，略過同步。"
                )
        except Exception as e:
            app.logger.error(f"[flow_confirm_visit] 更新 Zendesk ticket 失敗: {e}")
    else:
        app.logger.warning("[flow_confirm_visit] 這筆 appt 沒有 id，無法同步 Zendesk ticket")

    # ====== 回 LINE 提醒文字＋位置導航按鈕 ======
    detail_text = (
        "回診提醒：\n"
        f"姓名：{customer_name}\n"
        f"看診時間：{display_date} {display_time}\n"
        "\n請於門診開始前 10 分鐘至診所報到。"
    )
    detail_message = TextMessage(text=detail_text)

    buttons_template = ButtonsTemplate(
        title="回診資訊已確認",
        text="如需導航至診所，請點選下方按鈕。",
        actions=[
            MessageAction(
                label="查詢診所位置",
                text="查詢診所位置",
            ),
        ],
    )

    template_message = TemplateMessage(
        alt_text="回診資訊確認",
        template=buttons_template
    )

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[detail_message, template_message]
        )
    )
    return

# ========= Webhook 入口 =========

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
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
    text = event.message.text.strip()
    app.logger.info(f"收到使用者訊息: {text}")

    # === 0. 檢查是否處於首次建檔流程 ===
    line_user_id_for_state = None
    if event.source and hasattr(event.source, "user_id"):
        line_user_id_for_state = event.source.user_id

        # === -1. 使用者主動中斷建檔流程 ===
    if text == "取消建檔":
        if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
            del PENDING_REGISTRATIONS[line_user_id_for_state]
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="已為您取消建檔流程，謝謝。"
                    )]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="目前沒有正在進行的建檔流程。\n如需開始建檔，請輸入「測試身分」。"
                    )]
                )
            )
        return


    if line_user_id_for_state and line_user_id_for_state in PENDING_REGISTRATIONS:
        state = PENDING_REGISTRATIONS[line_user_id_for_state]
        step = state.get("step")


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

        # 0-2. 問手機
        elif step == "ask_phone":
            phone_raw = text.strip()
            digits = "".join(ch for ch in phone_raw if ch.isdigit())

            if not (len(digits) == 10 and digits.startswith("09")):
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="手機格式不正確，請以 09xxxxxxxx 格式重新輸入。")]
                    )
                )
                return

            name = state.get("name") or "未填姓名"

            # 呼叫 Zendesk API 建使用者
            user = create_zendesk_user(line_user_id_for_state, name, digits)
            if not user:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="建立病患資料時發生問題，請稍後再試。")]
                    )
                )
                return

            # 成功 → 清除狀態
            del PENDING_REGISTRATIONS[line_user_id_for_state]

            info_text = (
                "已為您完成基本資料建檔\n"
                f"姓名：{name}\n"
                f"手機：{digits}\n\n"
                "接下來請選擇要預約的日期範圍："
            )

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
                        TemplateMessage(alt_text="線上預約時段選擇", template=buttons_template)
                    ]
                )
            )
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

    # === 預約 YYYY-MM-DD：顯示 Carousel ===
    elif text.startswith("預約 "):
        date_str = text.replace("預約", "").strip()
        try:
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                reply_msg = TextMessage(text=f"{date_str} 沒有可預約時段")
            else:
                reply_msg = build_slots_carousel(date_str, available_slots)
        except Exception as e:
            app.logger.error(f"取得可預約時段失敗: {e}")
            reply_msg = TextMessage(text="取得可預約時段失敗，請稍後再試")

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[reply_msg]
            )
        )
        return

    # === ① 線上約診：先判斷 Zendesk 有沒有這個病患 ===
        # === 1. 線上約診入口（正式給病患用） ===
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

        # 1-3 沒找到 → 視為新病患，啟動首次建檔流程
        if count == 0:
            # 先試著從 LINE 拿暱稱來打招呼
            try:
                profile = line_bot_api.get_profile(user_id=line_user_id)
                display_name = getattr(profile, "display_name", None) or "您好"
            except Exception as e:
                app.logger.error(f"取得 LINE Profile 失敗: {e}")
                display_name = "您好"

            # 記錄這個 user 正在「問姓名」這個 step
            PENDING_REGISTRATIONS[line_user_id] = {
                "step": "ask_name",
                "display_name": display_name,
            }

            reply_text = (
                f"{display_name} 您好，歡迎使用線上約診服務。\n"
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

        # 1-4 已有一筆 → 老病患，直接帶出 Zendesk 資料 + 預約按鈕
        elif count == 1 and user:
            name = user.get("name") or "貴賓"
            phone = user.get("phone") or "（未留電話）"

            info_text = (
                f"{name} 您好，系統中已有您的資料：\n"
                f"手機：{phone}\n\n"
                "請選擇要預約的日期範圍："
            )

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
                    ]
                )
            )
            return

        # 1-5 保險：理論上不會發生（同一個 line_user_id 對到多筆）
        else:
            warn_text = (
                f"系統偵測到 {count} 筆使用相同 LINE ID 的病患資料，"
                "請聯繫診所人員協助處理。"
            )
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=warn_text)]
                )
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


    # === ② 我要預約本週 ===
    elif text == "我要預約本週":
        today = datetime.now()
        weekday = today.weekday()
        monday = today - timedelta(days=weekday)
        saturday = monday + timedelta(days=5)

        # 從「明天」開始，到本週六為止
        start_date = today + timedelta(days=1)
        candidate_dates = []
        cur = start_date
        while cur.date() <= saturday.date():
            candidate_dates.append(cur.date())
            cur += timedelta(days=1)

        columns = []
        for d in candidate_dates:
            date_str = d.isoformat()
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                continue

            mmdd = d.strftime("%m/%d")
            weekday_label = WEEKDAY_ZH[d.weekday()]
            title = f"週{weekday_label}（{mmdd}）"

            columns.append(
                CarouselColumn(
                    title=title,
                    text="點擊查看可預約時段。",
                    actions=[
                        MessageAction(
                            label="查看可預約時段",
                            text=f"預約 {date_str}"
                        )
                    ]
                )
            )

        if not columns:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="本週目前沒有可預約的日期")]
                )
            )
            return

        carousel = CarouselTemplate(columns=columns)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="本週可預約日期列表",
                        template=carousel
                    )
                ]
            )
        )
        return

    # === ③ 我要預約下週 ===
    elif text == "我要預約下週":
        today = datetime.now()
        weekday = today.weekday()
        monday = today - timedelta(days=weekday)
        next_monday = monday + timedelta(days=7)
        next_saturday = next_monday + timedelta(days=5)

        candidate_dates = []
        cur = next_monday
        while cur.date() <= next_saturday.date():
            candidate_dates.append(cur.date())
            cur += timedelta(days=1)

        columns = []
        for d in candidate_dates:
            date_str = d.isoformat()
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                continue

            mmdd = d.strftime("%m/%d")
            weekday_label = WEEKDAY_ZH[d.weekday()]
            title = f"週{weekday_label}（{mmdd}）"

            columns.append(
                CarouselColumn(
                    title=title,
                    text="點擊查看可預約時段。",
                    actions=[
                        MessageAction(
                            label="查看這天時段",
                            text=f"預約 {date_str}"
                        )
                    ]
                )
            )

        if not columns:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="下週目前沒有可預約的日期")]
                )
            )
            return

        carousel = CarouselTemplate(columns=columns)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="下週可預約日期列表",
                        template=carousel
                    )
                ]
            )
        )
        return

    # === ③-1 其他時間：詢問兩週後 / 三週後 ===
    elif text == "其他日期":
        buttons_template = ButtonsTemplate(
            title="其他日期預約",
            text="請選擇要預約的週次：",
            actions=[
                MessageAction(label="兩週後", text="我要預約兩週後"),
                MessageAction(label="三週後", text="我要預約三週後"),
            ],
        )
        template_message = TemplateMessage(
            alt_text="其他日期預約選擇",
            template=buttons_template
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[template_message]
            )
        )
        return

    # === ③-2 我要預約兩週後 ===
    elif text == "我要預約兩週後":
        today = datetime.now()
        weekday = today.weekday()
        monday = today - timedelta(days=weekday)

        two_weeks_monday = monday + timedelta(days=14)
        two_weeks_saturday = two_weeks_monday + timedelta(days=5)

        candidate_dates = []
        cur = two_weeks_monday
        while cur.date() <= two_weeks_saturday.date():
            candidate_dates.append(cur.date())
            cur += timedelta(days=1)

        columns = []
        for d in candidate_dates:
            date_str = d.isoformat()
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                continue

            mmdd = d.strftime("%m/%d")
            weekday_label = WEEKDAY_ZH[d.weekday()]
            title = f"週{weekday_label}（{mmdd}）"

            columns.append(
                CarouselColumn(
                    title=title,
                    text="點擊查看可預約時段。",
                    actions=[
                        MessageAction(
                            label="查看這天時段",
                            text=f"預約 {date_str}"
                        )
                    ]
                )
            )

        if not columns:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="兩週後目前沒有可預約的日期")]
                )
            )
            return

        carousel = CarouselTemplate(columns=columns)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="兩週後可預約日期列表",
                        template=carousel
                    )
                ]
            )
        )
        return

    # === ③-3 我要預約三週後 ===
    elif text == "我要預約三週後":
        today = datetime.now()
        weekday = today.weekday()
        monday = today - timedelta(days=weekday)

        three_weeks_monday = monday + timedelta(days=21)
        three_weeks_saturday = three_weeks_monday + timedelta(days=5)

        candidate_dates = []
        cur = three_weeks_monday
        while cur.date() <= three_weeks_saturday.date():
            candidate_dates.append(cur.date())
            cur += timedelta(days=1)

        columns = []
        for d in candidate_dates:
            date_str = d.isoformat()
            available_slots = get_available_slots_for_date(date_str)
            if not available_slots:
                continue

            mmdd = d.strftime("%m/%d")
            weekday_label = WEEKDAY_ZH[d.weekday()]
            title = f"週{weekday_label}（{mmdd}）"

            columns.append(
                CarouselColumn(
                    title=title,
                    text="點擊查看可預約時段。",
                    actions=[
                        MessageAction(
                            label="查看這天時段",
                            text=f"預約 {date_str}"
                        )
                    ]
                )
            )

        if not columns:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="三週後目前沒有可預約的日期")]
                )
            )
            return

        carousel = CarouselTemplate(columns=columns)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TemplateMessage(
                        alt_text="三週後可預約日期列表",
                        template=carousel
                    )
                ]
            )
        )
        return


    # === ④ 我想預約 YYYY-MM-DD HH:MM ===
    elif text.startswith("我想預約"):
        payload = text.replace("我想預約", "").strip()
        parts = payload.split()

        if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
            date_str, time_str = parts
            display_date = date_str.replace("-", "/")

            buttons_template = ButtonsTemplate(
                title="預約確認",
                text=f"您選擇的時段是：\n{display_date} {time_str}\n\n是否確認預約？",
                actions=[
                    MessageAction(label="確認預約", text=f"確認預約 {date_str} {time_str}"),
                    MessageAction(label="取消", text="取消預約流程")
                ]
            )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TemplateMessage(alt_text="預約確認", template=buttons_template)]
                )
            )
            return

        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請用格式：我想預約 YYYY-MM-DD HH:MM")]
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

            # ① 先拿 LINE userId
            line_user_id = None
            if event.source and hasattr(event.source, "user_id"):
                line_user_id = event.source.user_id

            # ② 預設先用 DEMO（避免真的炸掉）
            customer_name = DEMO_CUSTOMER_NAME
            customer_phone = DEMO_CUSTOMER_PHONE
            line_display_name = None
            # 初始化 Zendesk 客戶 ID
            zendesk_customer_id = None 

            # ③ 如果拿得到 line_user_id，就去 Zendesk 找 user
            if line_user_id:
                try:
                    zd_count, zd_user = search_zendesk_user_by_line_id(line_user_id)
                    if zd_user:
                        # Zendesk 裡的 name / phone
                        zd_name = zd_user.get("name") or customer_name
                        zd_phone = zd_user.get("phone") or customer_phone
                        customer_name = zd_name
                        customer_phone = zd_phone
                        # 🚨 關鍵：從 Zendesk User 物件中取得 ID
                        zendesk_customer_id = zd_user.get("id")

                except Exception as e:
                    app.logger.error(f"用 line_user_id 查 Zendesk user 失敗: {e}")

                # ④ 再嘗試拿 LINE 顯示名稱（例如 Kevin）
                try:
                    profile = line_bot_api.get_profile(line_user_id)
                    if profile and hasattr(profile, "display_name"):
                        line_display_name = profile.display_name
                except Exception as e:
                    app.logger.error(f"取得 LINE profile 失敗: {e}")

            # ⑤ 呼叫新的 create_booking_appointment（會寫入 LINE_USER 到 serviceNotes）
            try:
                created = create_booking_appointment(
                    date_str=date_str,
                    time_str=time_str,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    # 🚨 傳入 Zendesk 客戶 ID 給 Bookings API 函式 (讓它能繼續傳給 Zendesk Ticket 函式)
                    zendesk_customer_id=zendesk_customer_id, 
                    line_display_name=line_display_name,
                    line_user_id=line_user_id,
                )

                appt_id = created.get("id", "（沒有取得 ID）")
                display_date = date_str.replace("-", "/")

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
    # elif text.startswith("確認預約"):
    #     payload = text.replace("確認預約", "").strip()
    #     parts = payload.split()

    #     if len(parts) == 2 and parts[0].count("-") == 2 and ":" in parts[1]:
    #         date_str, time_str = parts

    #         # ① 先拿 LINE userId
    #         line_user_id = None
    #         if event.source and hasattr(event.source, "user_id"):
    #             line_user_id = event.source.user_id

    #         # ② 預設先用 DEMO（避免真的炸掉）
    #         customer_name = DEMO_CUSTOMER_NAME
    #         customer_phone = DEMO_CUSTOMER_PHONE
    #         line_display_name = None

    #         # ③ 如果拿得到 line_user_id，就去 Zendesk 找 user
    #         if line_user_id:
    #             try:
    #                 zd_count, zd_user = search_zendesk_user_by_line_id(line_user_id)
    #                 if zd_user:
    #                     # Zendesk 裡的 name / phone
    #                     zd_name = zd_user.get("name") or customer_name
    #                     zd_phone = zd_user.get("phone") or customer_phone
    #                     customer_name = zd_name
    #                     customer_phone = zd_phone
    #             except Exception as e:
    #                 app.logger.error(f"用 line_user_id 查 Zendesk user 失敗: {e}")

    #             # ④ 再嘗試拿 LINE 顯示名稱（例如 Kevin）
    #             try:
    #                 profile = line_bot_api.get_profile(line_user_id)
    #                 if profile and hasattr(profile, "display_name"):
    #                     line_display_name = profile.display_name
    #             except Exception as e:
    #                 app.logger.error(f"取得 LINE profile 失敗: {e}")

    #         # ⑤ 呼叫新的 create_booking_appointment（會寫入 LINE_USER 到 serviceNotes）
    #         try:
    #             created = create_booking_appointment(
    #                 date_str=date_str,
    #                 time_str=time_str,
    #                 customer_name=customer_name,
    #                 customer_phone=customer_phone,
    #                 line_display_name=line_display_name,
    #                 line_user_id=line_user_id,
    #             )

    #             appt_id = created.get("id", "（沒有取得 ID）")
    #             display_date = date_str.replace("-", "/")

    #             # 這裡顯示給病患看的姓名，沿用 booking_customer_name 的邏輯
    #             if line_display_name:
    #                 display_name = f"{customer_name}（{line_display_name}）"
    #             else:
    #                 display_name = customer_name

    #             detail_text = (
    #                 "已為您完成預約，請準時報到。\n"
    #                 f"姓名：{display_name}\n"
    #                 f"時段：{display_date} {time_str}"
    #             )

    #             buttons_template = ButtonsTemplate(
    #                 title="診所位置",
    #                 text="如需導航，請點選下方按鈕。",
    #                 actions=[
    #                     MessageAction(label="位置導航", text="查詢診所位置")
    #                 ],
    #             )

    #             line_bot_api.reply_message(
    #                 ReplyMessageRequest(
    #                     reply_token=event.reply_token,
    #                     messages=[
    #                         TextMessage(text=detail_text),
    #                         TemplateMessage(
    #                             alt_text="診所位置導航",
    #                             template=buttons_template,
    #                         ),
    #                     ],
    #                 )
    #             )
    #             return

    #         except Exception as e:
    #             app.logger.error(f"建立 Bookings 預約失敗: {e}")
    #             reply_text = "未成功預約，請重新操作"

    #     else:
    #         reply_text = "格式：確認預約 YYYY-MM-DD HH:MM"

    #     line_bot_api.reply_message(
    #         ReplyMessageRequest(
    #             reply_token=event.reply_token,
    #             messages=[TextMessage(text=reply_text)],
    #         )
    #     )
    #     return
    
    # === 約診查詢 ===
    elif text == "約診查詢":
        return flow_query_next_appointment(event, text)

    # === ⑤-1 取消約診 ===
    elif text.startswith("取消約診"):
        return flow_cancel_request(event, text)

    # === ⑤-2 確認取消 ===
    elif text.startswith("確認取消"):
        return flow_confirm_cancel(event, text)

    # === ⑦ 確認回診 ===
    elif text.startswith("確認回診"):
        return flow_confirm_visit(event, text)

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

    # === 其他訊息 ===
    else:
        app.logger.info("非線上約診相關指令，請聯繫客服")

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data or ""
    app.logger.info(f"收到 Postback data: {data}")

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


# 本機用5001，Azure則用賦予的port
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
