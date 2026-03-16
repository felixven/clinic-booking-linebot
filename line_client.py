# line_client.py
import os
import certifi

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
)

# === LINE 基本設定 ===
configuration = Configuration(
    access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
)
configuration.ssl_ca_cert = certifi.where()

configuration.connection_timeout = 5   # 連線最多等 5 秒
configuration.read_timeout = 5         # 讀 response 最多等 5 秒


api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)

def _mask_secret(s: str | None) -> str:
    if not s:
        return "None"
    if len(s) <= 8:
        return s
    return f"{s[:4]}...{s[-4:]}"

_init_secret = os.getenv("LINE_CHANNEL_SECRET")
print(f"[LINE_CLIENT_INIT] secret={_mask_secret(_init_secret)}", flush=True)

_init_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
print(f"[LINE_CLIENT_INIT] token={_mask_secret(_init_token)}", flush=True)

handler = WebhookHandler(_init_secret)

# handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
