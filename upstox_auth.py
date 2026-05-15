import os
import requests
import webbrowser
import threading
from flask import Flask, request
from dotenv import load_dotenv, set_key

load_dotenv()

API_KEY = os.getenv("UPSTOX_API_KEY")
API_SECRET = os.getenv("UPSTOX_API_SECRET")
REDIRECT_URI = "http://127.0.0.1:3000"
ENV_FILE = ".env"

app = Flask(__name__)
auth_done = threading.Event()


@app.route("/")
def callback():
    code = request.args.get("code")
    if not code:
        return "❌ No auth code received.", 400

    try:
        # Exchange auth code for access token via direct HTTP
        response = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"
            },
            data={
                "code": code,
                "client_id": API_KEY,
                "client_secret": API_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code"
            }
        )

        data = response.json()

        if "access_token" not in data:
            print(f"\n❌ Token exchange failed: {data}")
            return f"❌ Failed: {data}", 500

        access_token = data["access_token"]

        # Save to .env automatically
        set_key(ENV_FILE, "UPSTOX_ACCESS_TOKEN", access_token)
        print(f"\n✅ Access token saved to .env!")
        print(f"Token preview: {access_token[:20]}...")

        auth_done.set()
        return "✅ Authenticated! Close this tab and return to terminal.", 200

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return f"❌ Error: {e}", 500


def open_browser():
    auth_url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?client_id={API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
    )
    print(f"\n🌐 Opening Upstox login in browser...")
    print(f"If browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)


if __name__ == "__main__":
    print("=" * 50)
    print("   Upstox OAuth - Daily Token Generator")
    print("=" * 50)

    timer = threading.Timer(1.5, open_browser)
    timer.start()

    print("🔄 Starting local server on port 3000...")
    threading.Thread(
        target=lambda: app.run(port=3000, debug=False, use_reloader=False)
    ).start()

    auth_done.wait()
    print("🎉 Authentication complete!")
    print("Run python3 main.py to start trading.")
    os._exit(0)
