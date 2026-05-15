import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("UPSTOX_ACCESS_TOKEN")

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json"
}

# Test 1 — fetch profile
print("Testing Upstox connection...")
r = requests.get("https://api.upstox.com/v2/user/profile", headers=headers)
data = r.json()

if r.status_code == 200:
    print(f"✅ Connected as: {data['data']['user_name']}")
    print(f"   Email: {data['data']['email']}")
    print(f"   Broker: {data['data']['broker']}")
else:
    print(f"❌ Failed: {data}")

# Test 2 — fetch funds
print("\nFetching funds...")
r = requests.get("https://api.upstox.com/v2/user/get-funds-and-margin", headers=headers)
data = r.json()

if r.status_code == 200:
    equity = data['data']['equity']
    print(f"✅ Available margin: ₹{equity['available_margin']}")
    print(f"   Used margin:      ₹{equity['used_margin']}")
else:
    print(f"❌ Funds fetch failed: {data}")
