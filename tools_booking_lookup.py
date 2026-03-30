import re

from bookings_core import list_appointments_for_date, parse_booking_datetime_to_local
from config import BOOKINGS_BUSINESS_ACU_ID, BOOKINGS_BUSINESS_CLINIC_ID
from patient_core import normalize_phone


def _normalize_import_name(name: str | None) -> str:
    return re.sub(r"\s+", "", (name or "").strip()).lower()


def resolve_import_business_id(booking_type: str) -> tuple[str | None, str | None]:
    btype = (booking_type or "").strip().lower()
    if btype == "clinic":
        return BOOKINGS_BUSINESS_CLINIC_ID, "clinic"
    if btype == "acupuncture":
        return BOOKINGS_BUSINESS_ACU_ID, "acupuncture"
    return None, None


def find_booking_candidates_for_import(
    *,
    booking_type: str,
    date_str: str,
    time_str: str,
    phone: str,
    patient_name: str,
) -> tuple[str | None, list[dict]]:
    business_id, normalized_type = resolve_import_business_id(booking_type)
    if not business_id:
        raise ValueError("unsupported_booking_type")

    appts = list_appointments_for_date(date_str, business_id=business_id)
    target_phone = normalize_phone(phone or "")
    target_name = _normalize_import_name(patient_name)
    candidates: list[dict] = []

    for appt in appts:
        start_info = appt.get("startDateTime") or {}
        local_dt = parse_booking_datetime_to_local(start_info.get("dateTime"))
        if not local_dt:
            continue

        appt_time = local_dt.strftime("%H:%M")
        if appt_time != time_str:
            continue

        appt_phone = normalize_phone(appt.get("customerPhone") or "")
        if target_phone and appt_phone != target_phone:
            continue

        appt_name = _normalize_import_name(appt.get("customerName") or "")
        if target_name and appt_name != target_name:
            continue

        candidates.append(
            {
                "booking_id": appt.get("id"),
                "business_id": business_id,
                "booking_type": normalized_type,
                "patient_name": appt.get("customerName") or patient_name,
                "phone": appt.get("customerPhone") or phone,
                "date": local_dt.strftime("%Y-%m-%d"),
                "time": appt_time,
                "service_name": appt.get("serviceName") or "",
            }
        )

    return business_id, candidates


def render_find_booking_id_tool() -> str:
    return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>查詢 Bookings ID</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; max-width: 760px; }
    h1 { font-size: 24px; margin-bottom: 8px; }
    p { color: #444; }
    form { display: grid; gap: 12px; margin-top: 20px; }
    label { display: grid; gap: 6px; font-weight: 600; }
    input, select, button, textarea { font: inherit; padding: 10px 12px; }
    button { width: fit-content; cursor: pointer; }
    .actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .hint { font-size: 14px; color: #666; }
    textarea { min-height: 240px; white-space: pre; }
  </style>
</head>
<body>
  <h1>查詢 Bookings ID</h1>
  <p>給診所內部查既有預約用。輸入條件後，系統會去 Bookings 反查可用的 booking id。</p>
  <form id="booking-id-form">
    <label>預約類型
      <select name="booking_type" required>
        <option value="clinic">門診</option>
        <option value="acupuncture">針灸</option>
      </select>
    </label>
    <label>預約日期
      <input type="date" name="date" required>
    </label>
    <label>預約時間
      <input type="time" name="time" required>
    </label>
    <label>病患手機
      <input type="text" name="phone" placeholder="0912345678">
    </label>
    <label>病患姓名
      <input type="text" name="patient_name" placeholder="王小明">
    </label>
    <p>病患手機或病患姓名至少填一個。</p>
    <div class="actions">
      <button type="submit">查詢 Bookings ID</button>
      <button type="button" id="copy-booking-id">Copy booking_id</button>
      <span id="copy-status" class="hint"></span>
    </div>
    <label>查詢結果
      <textarea id="result" readonly></textarea>
    </label>
  </form>
  <script>
    const form = document.getElementById("booking-id-form");
    const result = document.getElementById("result");
    const copyButton = document.getElementById("copy-booking-id");
    const copyStatus = document.getElementById("copy-status");

    function setCopyStatus(text) {
      copyStatus.textContent = text;
      if (!text) return;
      window.setTimeout(() => {
        if (copyStatus.textContent === text) {
          copyStatus.textContent = "";
        }
      }, 2000);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      result.value = "查詢中...";
      setCopyStatus("");
      const payload = Object.fromEntries(new FormData(form).entries());

      try {
        const resp = await fetch("/api/tools/find-booking-id", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        result.value = JSON.stringify(data, null, 2);
      } catch (err) {
        result.value = JSON.stringify({ success: false, reason: "request_failed", message: String(err) }, null, 2);
      }
    });

    copyButton.addEventListener("click", async () => {
      try {
        const parsed = JSON.parse(result.value || "{}");
        const bookingId = parsed.booking_id;
        if (!bookingId) {
          setCopyStatus("目前沒有可複製的 booking_id");
          return;
        }
        await navigator.clipboard.writeText(bookingId);
        setCopyStatus("已複製 booking_id");
      } catch (err) {
        setCopyStatus("複製失敗，請確認查詢結果格式正確");
      }
    });
  </script>
</body>
</html>
"""
