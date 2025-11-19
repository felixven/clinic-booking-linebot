import os
import requests

def get_graph_token():
    tenant = os.getenv("GRAPH_TENANT_ID")
    client_id = os.getenv("GRAPH_CLIENT_ID")
    client_secret = os.getenv("GRAPH_CLIENT_SECRET")

    print("ENV CHECK:")
    print("  GRAPH_TENANT_ID:", tenant)
    print("  GRAPH_CLIENT_ID:", client_id)
    print("  GRAPH_CLIENT_SECRET is set:", client_secret is not None)

    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }

    resp = requests.post(url, data=data)
    print("TOKEN RESPONSE STATUS:", resp.status_code)
    print("TOKEN RESPONSE BODY:", resp.text)

    data = resp.json()
    if "access_token" not in data:
        raise Exception("無法取得 access_token")
    return data["access_token"]

def list_booking_businesses():
    token = get_graph_token()
    url = "https://graph.microsoft.com/v1.0/solutions/bookingBusinesses"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    print("BUSINESS LIST STATUS:", resp.status_code)
    print("BUSINESS LIST BODY:", resp.text)

if __name__ == "__main__":
    list_booking_businesses()
