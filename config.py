from dotenv import load_dotenv
load_dotenv()
import os

# ======== Booking 相關資料 ========
BOOKINGS_DEMO_SERVICE_ID = os.getenv("BOOKINGS_DEMO_SERVICE_ID")
BOOKINGS_DEMO_STAFF_ID = os.getenv("BOOKINGS_DEMO_STAFF_ID")

# === Bookings business ids（針灸分床）===
BOOKINGS_BUSINESS_CLINIC_ID = os.getenv("BOOKINGS_BUSINESS_CLINIC_ID") 
BOOKINGS_BUSINESS_ACU_ID    = os.getenv("BOOKINGS_BUSINESS_ACU_ID")  # 針灸那個 business

BOOKINGS_BUSINESS_ACU_BED1_ID = os.getenv("BOOKINGS_BUSINESS_ACU_BED1_ID") 
BOOKINGS_BUSINESS_ACU_BED2_ID = os.getenv("BOOKINGS_BUSINESS_ACU_BED2_ID") 

BOOKINGS_SERVICE_ACU_BED_ID=os.getenv("BOOKINGS_SERVICE_ACU_BED_ID") 
# === Bookings service ids（針灸分床）===
BOOKINGS_SERVICE_ACU_BED1_ID = os.getenv("BOOKINGS_SERVICE_ACU_BED1_ID") 
# 針灸（一床）
BOOKINGS_SERVICE_ACU_BED2_ID = os.getenv("BOOKINGS_SERVICE_ACU_BED2_ID") 
 # 針灸（二床）

ACU_STAFF_BED1_ID=os.getenv("ACU_STAFF_BED1_ID")
ACU_STAFF_BED2_ID=os.getenv("ACU_STAFF_BED2_ID")

# ======== MS Graph Booking Token 相關 ========
GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")

# ===================== Zendesk 設定 =====================
ZENDESK_SUBDOMAIN = "longyin" 
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL") or "tech_support@newwave.tw"
ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")  

# ===================== Zendesk 自訂欄位 ID =====================
# Profile 狀態判斷
PROFILE_STATUS_EMPTY = "empty"
PROFILE_STATUS_NEED_PHONE = "need_phone"
PROFILE_STATUS_NEED_NAME = "need_name"
PROFILE_STATUS_COMPLETE = "complete"
PLACEHOLDER_NAMES = {"未填姓名", "貴賓"}
def is_valid_name(name: str) -> bool:
    if not name:
        return False
    s = str(name).strip()

    if not s:
        return False
    if s in INVALID_NAME_PLACEHOLDERS:
        return False
    if len(s) < 2:
        return False

    return True
ZENDESK_UF_LINE_USER_ID = 54043434990489
ZENDESK_UF_LINE_USER_ID_KEY = "line_user_id"
ZENDESK_UF_PROFILE_STATUS_KEY = "profile_status"
ZENDESK_UF_ACU_OK_1_INTERNAL_MED_PATIENT = 54581133333145   # 針灸資格1：內科患者
ZENDESK_UF_ACU_OK_1_INTERNAL_MED_PATIENT_KEY = "clinic_acu_internal_patient"
ZENDESK_UF_ACU_OK_2_SEEN_WITHIN_3_MONTHS = 54581125805849   # 針灸資格2：近三個月看過診
ZENDESK_UF_ACU_OK_2_SEEN_WITHIN_3_MONTHS_KEY = "clinic_acu_seen_within_3_months"
ZENDESK_UF_ACU_OK_3_DOCTOR_APPROVED = 54581179112089   # 針灸資格3：醫師評估OK
ZENDESK_UF_ACU_OK_3_DOCTOR_APPROVED_KEY = "clinic_acu_doctor_approved"

ZENDESK_CF_BOOKING_ID = 54043597648537          # Booking ID (Text)
ZENDESK_CF_APPOINTMENT_DATE = 54043618333593    # Appointment Date (Date)
ZENDESK_CF_APPOINTMENT_TIME = 54043605240601    # Appointment Time (Text)
ZENDESK_CF_REMINDER_STATE = 54043678550809      # Reminder State (Dropdown)
ZENDESK_CF_REMINDER_ATTEMPTS = 54043616442009   # Reminder Attempts (Numeric)
ZENDESK_CF_LAST_CALL_ID = 54043719367065        # Last Call Id (text)
ZENDESK_CF_LAST_VOICE_ATTEMPT_DATE=14623920927375 

ZENDESK_APPOINTMENT_FORM_ID=54043900272281

ZENDESK_REMINDER_STATE_PENDING = "待提醒"
ZENDESK_REMINDER_STATE_QUEUED = "已排入外撥"
ZENDESK_REMINDER_STATE_SUCCESS="已成功提醒"
ZENDESK_REMINDER_STATE_FAILED="提醒失敗"
ZENDESK_REMINDER_STATE_CANCELLED = "已取消預約"


# ===================== Zendesk 設定（NewWave） =====================
# ZENDESK_SUBDOMAIN = "con-nwdemo" 
# ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL") or "tech_support@newwave.tw"
# ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")  

# ZENDESK_UF_LINE_USER_ID = 14416308078351
# ZENDESK_UF_LINE_USER_ID_KEY = "line_user_id"
# ZENDESK_UF_PROFILE_STATUS_KEY = "profile_status"

# ZENDESK_CF_BOOKING_ID = 14459987905295          # Booking ID (Text)
# ZENDESK_CF_APPOINTMENT_DATE = 14460045495695    # Appointment Date (Date)
# ZENDESK_CF_APPOINTMENT_TIME = 14460068239631    # Appointment Time (Text)
# ZENDESK_CF_REMINDER_STATE = 14460033600271      # Reminder State (Dropdown)
# ZENDESK_CF_REMINDER_ATTEMPTS = 14460034088591   # Reminder Attempts (Number)
# ZENDESK_CF_LAST_CALL_ID = 14460059835279        # Last Call Id (備用)
# ZENDESK_CF_LAST_VOICE_ATTEMPT_DATE=14623920927375 

# ZENDESK_APPOINTMENT_FORM_ID=14460691929743

# ZENDESK_REMINDER_STATE_PENDING = "待提醒"
# ZENDESK_REMINDER_STATE_QUEUED = "已排入外撥"
# ZENDESK_REMINDER_STATE_SUCCESS="已成功提醒"
# ZENDESK_REMINDER_STATE_FAILED="提醒失敗"
# ZENDESK_REMINDER_STATE_CANCELLED = "已取消預約"


# 距離看診幾天前要發提醒（正式版可能是 3，測試可以先改）
REMINDER_DAYS_BEFORE = int(os.environ.get("REMINDER_DAYS_BEFORE", "3"))

# ======== 預約時段相關設定（之後要改時段只改這裡） ========
CLOSED_WEEKDAYS = {2, 6}  # 週三、週日
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]
SLOT_MINUTES = 20
CLINIC_MORNING_START = "09:00"
CLINIC_MORNING_END   = "12:20"
CLINIC_EVENING_START = "16:30"
CLINIC_EVENING_END   = "19:50"


SLOT_INTERVAL_MINUTES = SLOT_MINUTES
APPOINTMENT_DURATION_MINUTES = SLOT_MINUTES

MORNING_START = "09:00"
MORNING_END   = "12:20"

AFTERNOON_START = "16:30"
AFTERNOON_END   = "19:50"

FRI_MORNING_START = MORNING_START
FRI_MORNING_END   = MORNING_END

SAT_MORNING_START = MORNING_START
SAT_MORNING_END   = MORNING_END



# ===== Clinic session config =====
# 週一=0 ... 週日=6
CLINIC_OPEN = {
    0: {"am": True,  "pm": True},
    1: {"am": True,  "pm": True},
    2: {"am": False, "pm": False},  # 週三休
    3: {"am": True,  "pm": True},
    4: {"am": True,  "pm": False},  # 週五只有早
    5: {"am": True,  "pm": False},  # 週六只有早
    6: {"am": False, "pm": False},  # 週日休
}

# ✅ 人數上限（兩種做法擇一）
# A) 直接設上限（最符合你說的“config調整”）
CLINIC_CAPACITY = {"am": 20, "pm": 20}

# 或 B) 不寫死上限，改用「時段窗 / slot分鐘」自動算出總格數（更不容易忘記改）
# （要上限時再套 min(自動格數, 手動上限)）



ACU_SLOTS = {
    0: {  # Mon
        "08:50": "bed1",
        "09:20": "bed2",
        "09:50": "bed1",
        "10:20": "bed2",
        "11:20": "bed1",
        "16:20": "bed1",
        "16:50": "bed2",
        "17:20": "bed1",
        "18:20": "bed1",
        "18:50": "bed2",
    },
    1: {  # Tue
        "08:50": "bed1",
        "09:20": "bed2",
        "09:50": "bed1",
        "10:20": "bed2",
        "11:20": "bed1",
        "16:20": "bed1",
        "16:50": "bed2",
        "17:20": "bed1",
        "18:20": "bed1",
        "18:50": "bed2",
    },
    3: {  # Thu
        "08:50": "bed1",
        "09:20": "bed2",
        "09:50": "bed1",
        "10:20": "bed2",
        "11:20": "bed1",
        "16:20": "bed1",
        "16:50": "bed2",
        "17:20": "bed1",
        "18:20": "bed1",
        "18:50": "bed2",
    },
    4: {  # Fri（只有早診）
        "08:50": "bed1",
        "09:20": "bed2",
        "09:50": "bed1",
        "10:20": "bed2",
        "11:20": "bed1",
    },
    5: {  # Sat（例外：全一床）
        "08:50": "bed1",
        "09:50": "bed1",
        "10:20": "bed1",
        "11:20": "bed1",
    },
}

# 由 ACU_SLOTS 自動產生「當天可顯示的時間列表」
# （排序後固定顯示順序）
ACU_SLOTS_BY_WEEKDAY = {
    wd: sorted(times.keys())
    for wd, times in ACU_SLOTS.items()
}


# 針灸相關 serviceId（你之後若分床1/床2，就把兩個都放進去）
ACU_SERVICE_IDS = {
    BOOKINGS_SERVICE_ACU_BED1_ID,
    BOOKINGS_SERVICE_ACU_BED2_ID
}

# ======== 診所資料（ ========
CLINIC_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763351663/benyamin-bohlouli-B_sK_xgzwVA-unsplash_n6jy9m.jpg"
CLINIC_NAME = "龍吟中醫診所"
CLINIC_ADDRESS = "臺中市西屯區青海路二段242之32號"
CLINIC_LAT = 24.1718527355441
CLINIC_LNG = 120.64402133835931


# 線上預約用的共用圖片
WEEK_IMAGE_URL = "https://res.cloudinary.com/drbhr7kmb/image/upload/v1763314182/pulse_ultzw0.jpg"

# serviceNotes 裡當「確認」的標記字串
CONFIRM_NOTE_KEYWORD = "Confirmed via LINE"

# 暫存「首次建檔」流程的狀態（key = line_user_id）
PENDING_REGISTRATIONS = {}

CONFIRM_OPEN_DAYS_BEFORE = 3  # 原本 2，現在 +1
CANCEL_DEADLINE_DAYS_BEFORE = 4  # 原本 3，現在 +1

INVALID_NAME_PLACEHOLDERS = {
    "未填姓名", "貴賓", "您好", "病患", "訪客", "客人",
    "unknown", "Unknown", "N/A", "NA", "-", "—", "null", "None", "（未填）"
}


# ======== DEMO 患者資料 ========
DEMO_CUSTOMER_NAME = "LINE 使用者"
DEMO_CUSTOMER_EMAIL = "test@example.com"
DEMO_CUSTOMER_PHONE = "0912345678"
