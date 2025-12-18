from dotenv import load_dotenv
load_dotenv()
import os

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
# Profile 狀態判斷
PROFILE_STATUS_EMPTY = "empty"
PROFILE_STATUS_NEED_PHONE = "need_phone"
PROFILE_STATUS_COMPLETE = "complete"

ZENDESK_UF_LINE_USER_ID = 14416308078351
ZENDESK_UF_LINE_USER_ID_KEY = "line_user_id"
ZENDESK_UF_PROFILE_STATUS_KEY = "profile_status"

ZENDESK_CF_BOOKING_ID = 14459987905295          # Booking ID (Text)
ZENDESK_CF_APPOINTMENT_DATE = 14460045495695    # Appointment Date (Date)
ZENDESK_CF_APPOINTMENT_TIME = 14460068239631    # Appointment Time (Text)
ZENDESK_CF_REMINDER_STATE = 14460033600271      # Reminder State (Dropdown)
ZENDESK_CF_REMINDER_ATTEMPTS = 14460034088591   # Reminder Attempts (Number)
ZENDESK_CF_LAST_CALL_ID = 14460059835279        # Last Call Id (備用)
ZENDESK_CF_LAST_VOICE_ATTEMPT_DATE=14623920927375 

ZENDESK_APPOINTMENT_FORM_ID=14460691929743

ZENDESK_REMINDER_STATE_PENDING = "待提醒"
ZENDESK_REMINDER_STATE_QUEUED = "已排入外撥"
ZENDESK_REMINDER_STATE_SUCCESS="已成功提醒"
ZENDESK_REMINDER_STATE_FAILED="提醒失敗"
ZENDESK_REMINDER_STATE_CANCELLED = "已取消預約"

# 距離看診幾天前要發提醒（正式版可能是 3，測試可以先改）
REMINDER_DAYS_BEFORE = int(os.environ.get("REMINDER_DAYS_BEFORE", "3"))



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
DEMO_CUSTOMER_NAME = "LINE 使用者"
DEMO_CUSTOMER_EMAIL = "test@example.com"
DEMO_CUSTOMER_PHONE = "0912345678"

DEMO_FAIL_TICKET_ID_NO_RQ = os.getenv("DEMO_FAIL_TICKET_ID_NO_RQ")
DEMO_FAIL_TICKET_ID_RQ = os.getenv("DEMO_FAIL_TICKET_ID_RQ")

print(f"[CONFIG DEMO] NO_RQ={DEMO_FAIL_TICKET_ID_NO_RQ!r}, RQ={DEMO_FAIL_TICKET_ID_RQ!r}")
