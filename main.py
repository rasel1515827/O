"""
Orangecarrier -> Telegram bridge with cookie login check
"""
import sys, types
sys.modules['imghdr'] = types.ModuleType('imghdr')
import os, time, json, re, requests, sqlite3
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import InputFile, Bot

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
OC_SESSION_COOKIE = os.getenv("OC_SESSION_COOKIE")  # e.g. "laravel_session=abcd123; XSRF-TOKEN=xyz"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "15"))

BASE_URL = "https://www.orangecarrier.com"
LIVE_CALLS_PATH = "/live/calls"

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("❌ BOT_TOKEN and TARGET_CHAT_ID must be set.")

# 🔹 Cookie ফাইল থেকেও পড়া (যদি env এ না থাকে)
cookie_path = Path("/tmp/orangecarrier_data/oc_cookie.txt")
if not OC_SESSION_COOKIE and cookie_path.exists():
    OC_SESSION_COOKIE = cookie_path.read_text().strip()

# ================ PATHS ==================
DATA_DIR = Path("/tmp/orangecarrier_data")
VOICES_DIR = DATA_DIR / "voices"
DATA_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "seen.sqlite"
conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT)")
conn.commit()

# ================ HELPERS =================
def is_seen(item_id):
    cur.execute("SELECT 1 FROM seen WHERE id=?", (item_id,))
    return cur.fetchone() is not None

def mark_seen(item_id):
    try:
        cur.execute("INSERT INTO seen (id, first_seen) VALUES (?, ?)", (item_id, datetime.now().isoformat()))
        conn.commit()
    except Exception:
        pass

def get_session():
    s = requests.Session()
    if OC_SESSION_COOKIE:
        s.headers.update({
            "Cookie": OC_SESSION_COOKIE,
            "User-Agent": "Mozilla/5.0"
        })
    return s

def check_login(session):
    try:
        r = session.get(BASE_URL + "/dashboard", timeout=15)
        if "Logout" in r.text or "Dashboard" in r.text:
            return True
        return False
    except Exception:
        return False

AUDIO_RX = re.compile(r"https?://[^\s'\"<>]+(?:\.mp3|\.ogg|\.m4a)", re.IGNORECASE)

def fetch_live_items(session):
    url = BASE_URL + LIVE_CALLS_PATH
    try:
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            print("Live calls HTTP", r.status_code)
            return []

        # Prevent scraping login page
        if "Please Enter a valid Password" in r.text or "Sign Up" in r.text or "Forgot Password" in r.text:
            print("⚠️ Login page detected, skipping fetch.")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        blocks = soup.find_all(["div","li","p"])
        parsed, seen_texts = [], set()
        for b in blocks:
            txt = b.get_text(" ", strip=True)
            if len(txt) < 10:
                continue
            aud = None
            for m in AUDIO_RX.findall(str(b)):
                aud = m
                break
            key = (aud or "") + "|" + txt[:120]
            if key in seen_texts:
                continue
            seen_texts.add(key)
            parsed.append({"id": key, "text": txt, "audio": aud})
        return parsed
    except Exception as e:
        print("fetch_live_items error:", e)
        return []

def download_file(session, url, dest):
    try:
        r = session.get(url, stream=True, timeout=40)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print("Download failed:", e)
        return False

# ================ TELEGRAM =================
bot = Bot(token=BOT_TOKEN)

def send_to_telegram(item, audio_path=None):
    body = f"🔔 New call item\n{item.get('text','')[:800]}"
    try:
        if audio_path and Path(audio_path).exists():
            with open(audio_path, "rb") as f:
                bot.send_audio(chat_id=TARGET_CHAT_ID, audio=InputFile(f), caption=body)
        else:
            bot.send_message(chat_id=TARGET_CHAT_ID, text=body)
    except Exception as e:
        print("Telegram send failed:", e)

# ================ MAIN LOOP =================
def main_loop():
    session = get_session()
    bot.send_message(chat_id=TARGET_CHAT_ID, text="🚀 Bot started... Checking OrangeCarrier login...")

    # ❌ যদি cookie না থাকে তাহলে কিছুই করবে না
    if not OC_SESSION_COOKIE:
        bot.send_message(chat_id=TARGET_CHAT_ID, text="⚠️ No cookie found! Skipping OrangeCarrier data fetch.")
        print("No cookie found. Waiting for cookie...")
        while True:
            time.sleep(60)
        return

    # ✅ cookie থাকলে লগইন চেক
    if check_login(session):
        bot.send_message(chat_id=TARGET_CHAT_ID, text="✅ OrangeCarrier login successful.")
    else:
        bot.send_message(chat_id=TARGET_CHAT_ID, text="❌ OrangeCarrier not logged in or cookie expired.")
        while True:
            time.sleep(60)
        return

    print("Polling every", POLL_INTERVAL, "seconds...")
    while True:
        try:
            items = fetch_live_items(session)
            if not items:
                time.sleep(POLL_INTERVAL)
                continue
            for it in items:
                iid = it.get("id")
                if is_seen(iid):
                    continue
                mark_seen(iid)
                audio_path = None
                if it.get("audio"):
                    aurl = it["audio"]
                    if aurl.startswith("/"):
                        aurl = BASE_URL + aurl
                    fname = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.mp3"
                    dest = VOICES_DIR / fname
                    if download_file(session, aurl, dest):
                        audio_path = str(dest)
                send_to_telegram(it, audio_path)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Loop error:", e)
            time.sleep(POLL_INTERVAL)

# 🔹 Telegram /login কমান্ড যোগ করা
from telegram.ext import Updater, CommandHandler

def login_command(update, context):
    app_url = os.getenv("APP_URL", "https://worker-production-d4ba.up.railway.app")
    update.message.reply_text(
        f"🔐 Login to OrangeCarrier:\n👉 {app_url}/login\n\n"
        "After logging in, the bot will automatically save your cookie."
    )

# ==================== BOT STARTUP ====================
updater = Updater(BOT_TOKEN)
dp = updater.dispatcher
dp.add_handler(CommandHandler("login", login_command))
updater.start_polling()
print("🤖 Telegram bot is running...")

# 🔹 Flask সার্ভার যোগ করা (cookie সেভ করার জন্য)
from flask import Flask, request, redirect
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ OrangeCarrier Bridge Bot is running."

@app.route('/login')
def login_page():
    # OrangeCarrier এর লগইন পেজে রিডাইরেক্ট করবে
    return redirect("https://www.orangecarrier.com/login")

@app.route('/save_cookie', methods=['POST'])
def save_cookie():
    data = request.get_json(force=True)
    cookie = data.get("cookie")
    if not cookie:
        return {"error": "No cookie received"}, 400
    cookie_path = Path("/tmp/orangecarrier_data/oc_cookie.txt")
    cookie_path.write_text(cookie.strip())
    return {"status": "Cookie saved successfully"}

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

threading.Thread(target=run_flask, daemon=True).start()

# 🔹 Main loop চালানো
if __name__ == "__main__":
    print("Starting bridge...")
    main_loop()
