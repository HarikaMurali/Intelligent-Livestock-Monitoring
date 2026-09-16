import os
import requests
from dotenv import load_dotenv

load_dotenv()

def notify(title, msg, user_ids):
    app_id = os.getenv("ONESIGNAL_APP_ID")
    api_key = os.getenv("ONESIGNAL_REST_API_KEY")

    if not app_id or not api_key:
        print("[OneSignal Error] Missing ONESIGNAL_APP_ID or ONESIGNAL_REST_API_KEY in .env!")
        return

    if isinstance(user_ids, str):
        user_ids = [user_ids]

    payload = {
        "app_id": app_id,
        "include_aliases": {
            "external_id": user_ids
        },
        "headings": {"en": title},
        "contents": {"en": msg},
        "target_channel": "push"
    }

    try:
        res = requests.post(
            "https://onesignal.com/api/v1/notifications",
            headers={
                "Authorization": f"Key {api_key}",  # Note: OneSignal REST API v16 uses 'Key <token>' or 'Basic <token>'
                "Content-Type": "application/json; charset=utf-8"
            },
            json=payload,
            timeout=5
        )
        print(f"[OneSignal API Response {res.status_code}]: {res.text}")
    except Exception as e:
        print(f"[OneSignal Request Failed]: {e}")

notify("Alert", "This is a test notification from the cattle monitoring system.", ["cattle","cattle_1","cattle_2","cattle_3"])