"""
gate_bot.py
Telegram bot that opens an RF gate via a Broadlink RM device.

Authorized users can open the gate by sending a keyword. When anyone
opens the gate, all OTHER authorized users get a notification with the
username and timestamp.

Setup:
    1. Learn the gate code:        python learn_code.py
    2. Create a Telegram bot via   @BotFather     → TELEGRAM_TOKEN
    3. Find your chat_id via       @userinfobot   → AUTHORIZED_CHAT_IDS
    4. Edit the CONFIG section below
    5. Run:                        python gate_bot.py
"""

import broadlink
import base64
import json
import os
import time
import requests
import threading
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────

TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"

# Chat IDs allowed to open the gate. IMPORTANT: keep this short and audited.
# Unauthorized attempts are logged to gate_bot.log.
AUTHORIZED_CHAT_IDS = [
    123456789,   # owner
    # 987654321, # family member
]

# Keywords that trigger the gate (case-insensitive, substring match)
KEYWORDS = ["open", "gate", "apri", "cancello", "🚪"]

# Key in broadlink_codes.json to send when triggered
GATE_CODE_NAME = "gate_open"

# Max gate openings per minute, per user (abuse protection)
RATE_LIMIT_PER_MIN = 3

# Files
CODES_FILE = "broadlink_codes.json"
LOG_FILE   = "gate_bot.log"

# ── STATE ─────────────────────────────────────────────────────
_broadlink = None
_broadlink_lock = threading.Lock()
_last_opens = {}   # chat_id -> [timestamps]


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Broadlink connection ──────────────────────────────────────

def get_broadlink():
    """Return a cached, authenticated Broadlink device, reconnecting if needed."""
    global _broadlink
    with _broadlink_lock:
        if _broadlink is None:
            log("Discovering Broadlink device...")
            devs = broadlink.discover(timeout=5)
            if not devs:
                raise Exception("Broadlink not found on LAN")
            _broadlink = devs[0]
            _broadlink.auth()
            log(f"Broadlink connected: {_broadlink.type} @ {_broadlink.host[0]}")
        return _broadlink


def open_gate() -> bool:
    """Send the saved RF code to open the gate. Returns True on success."""
    global _broadlink
    if not os.path.exists(CODES_FILE):
        log(f"ERROR: {CODES_FILE} not found")
        return False
    with open(CODES_FILE) as f:
        codes = json.load(f)
    if GATE_CODE_NAME not in codes:
        log(f"ERROR: code '{GATE_CODE_NAME}' not in {CODES_FILE}")
        return False

    packet = base64.b64decode(codes[GATE_CODE_NAME])
    try:
        d = get_broadlink()
        d.send_data(packet)
        return True
    except Exception as e:
        log(f"ERROR sending RF: {e}")
        with _broadlink_lock:
            _broadlink = None   # force reconnect next time
        return False


# ── Telegram ─────────────────────────────────────────────────

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def tg_send(chat_id: int, text: str):
    try:
        requests.post(
            f"{TG_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"ERROR Telegram send: {e}")


def tg_get_updates(offset: int):
    try:
        r = requests.get(
            f"{TG_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=40,
        )
        return r.json().get("result", [])
    except Exception as e:
        log(f"ERROR Telegram poll: {e}")
        return []


# ── Rate limiting ────────────────────────────────────────────

def rate_limit_ok(chat_id: int) -> bool:
    now = time.time()
    timestamps = [t for t in _last_opens.get(chat_id, []) if now - t < 60]
    if len(timestamps) >= RATE_LIMIT_PER_MIN:
        _last_opens[chat_id] = timestamps
        return False
    timestamps.append(now)
    _last_opens[chat_id] = timestamps
    return True


# ── Message handler ──────────────────────────────────────────

def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user    = msg["chat"].get("username") or msg["chat"].get("first_name") or "?"
    text    = msg.get("text", "").strip().lower()

    # Authorization check
    if chat_id not in AUTHORIZED_CHAT_IDS:
        log(f"DENIED from {user} (chat_id: {chat_id}) text: '{text}'")
        tg_send(chat_id,
                "⛔ You are not authorized.\n"
                f"Your chat_id is: <code>{chat_id}</code>")
        return

    # Help command
    if text in ("/start", "/help"):
        tg_send(chat_id,
                "🚪 <b>Gate Bot</b>\n\n"
                "Send any of these keywords to open the gate:\n"
                + "\n".join(f"• <code>{k}</code>" for k in KEYWORDS))
        return

    # Keyword match (substring)
    if not any(k in text for k in KEYWORDS):
        return

    # Rate limit
    if not rate_limit_ok(chat_id):
        log(f"RATE LIMIT for {user} (chat_id: {chat_id})")
        tg_send(chat_id, "⏱ Too many requests. Wait a minute.")
        return

    # Trigger gate
    log(f"OPEN requested by {user} (chat_id: {chat_id})")
    tg_send(chat_id, "🔓 Opening gate...")

    if open_gate():
        tg_send(chat_id, "✅ Gate opened!")
        log("Gate opened successfully")

        # Broadcast to OTHER authorized users
        when = datetime.now().strftime("%H:%M")
        notice = f"🚪 Gate opened by <b>{user}</b> at {when}"
        for other_id in AUTHORIZED_CHAT_IDS:
            if other_id != chat_id:
                tg_send(other_id, notice)
    else:
        tg_send(chat_id, "❌ Error. Check the log.")


# ── Main loop ────────────────────────────────────────────────

def main():
    log("=" * 50)
    log("Gate Bot starting")
    log("=" * 50)

    # Warm up the Broadlink connection
    try:
        get_broadlink()
    except Exception as e:
        log(f"WARNING: Broadlink unreachable at start: {e}")
        log("  (will retry on first command)")

    # Verify bot token
    try:
        r = requests.get(f"{TG_URL}/getMe", timeout=10).json()
        if r.get("ok"):
            bot = r["result"]
            log(f"Bot: @{bot['username']} ({bot['first_name']})")
        else:
            log(f"FATAL getMe error: {r}")
            return
    except Exception as e:
        log(f"FATAL cannot reach Telegram: {e}")
        return

    if not AUTHORIZED_CHAT_IDS or AUTHORIZED_CHAT_IDS == [123456789]:
        log("WARNING: AUTHORIZED_CHAT_IDS is not configured.")
        log("  Send a message to the bot — your chat_id will be logged.")
        log("  Add it to AUTHORIZED_CHAT_IDS and restart.")

    log("Listening for messages...")

    offset = 0
    while True:
        try:
            updates = tg_get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"])
                    except Exception as e:
                        log(f"ERROR in handler: {e}")
        except KeyboardInterrupt:
            log("Shutdown requested.")
            break
        except Exception as e:
            log(f"ERROR in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
