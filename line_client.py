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

api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)

handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
