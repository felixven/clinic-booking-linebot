import requests

def trigger_voice_demo(phone_number: str, patient_name: str):
    """
    Demo：從後端觸發外撥流程（現在是假的，不打電話）。
    未來有 ACS 後，只要把下面的 fake call 換成真正的 API 呼叫。
    """

    payload = {
        "phoneNumber": phone_number,
        "patientName": patient_name
    }

    # 假的 URL（之後會換成 Power Automate / ACS）
    fake_url = "https://example.com/voice-demo"

    print("[Voice Demo] Sending payload to cloud:")
    print(payload)

    try:
        resp = requests.post(fake_url, json=payload)
        print(f"[Voice Demo] Cloud responded: {resp.status_code}")
    except Exception as e:
        print(f"[Voice Demo] Failed to send demo request: {e}")
