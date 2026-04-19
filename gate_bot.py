"""
Telegram bot to open a Broadlink RF gate over LAN.

Features:
  - Persistent menu keyboard ("Open" and "Request access") always visible
  - Text triggers still work (backward compatible)
  - Access requests are sent only to the ADMIN (first user in whitelist).
    Admin receives inline buttons to approve or reject.
  - Whitelist stored in JSON so external scripts can read/edit it.
  - Per-user rate limiting.
  - Broadcast notification to the other authorized users when the gate opens.

Requirements:
    pip install broadlink requests
"""

import broadlink, base64, json, os, time, requests, threading
from datetime import datetime


# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN = "PUT-YOUR-BOT-TOKEN-HERE"

# Paths relative to this script (safe when launched as a service)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# JSON file with the whitelist of authorized chat IDs
AUTH_FILE  = os.path.join(_SCRIPT_DIR, "authorized_users.json")
CODES_FILE = os.path.join(_SCRIPT_DIR, "broadlink_codes.json")
LOG_FILE   = os.path.join(_SCRIPT_DIR, "gate_bot.log")

# Initial whitelist (used only the first time the bot runs,
# when authorized_users.json does not yet exist).
# The FIRST id is the admin: only the admin receives access requests.
# Find your chat id by messaging @userinfobot on Telegram.
INITIAL_AUTHORIZED = [
    123456789,   # ADMIN — first user receives access requests
    # 987654321, # other authorized users
]

# Text triggers that open the gate (case-insensitive)
KEYWORDS = ["open", "open gate", "gate", "🚪", "apri", "cancello"]

# Menu labels (exact button text)
MENU_OPEN    = "🚪 Open"
MENU_REQUEST = "🔑 Request access"

# Code name inside broadlink_codes.json
GATE_CODE_NAME = "gate_open"

# Rate limit: max openings per minute per user
RATE_LIMIT_PER_MIN = 3


# ── STATE ─────────────────────────────────────────────────────
_broadlink = None
_broadlink_lock = threading.Lock()
_auth_lock = threading.Lock()
_last_opens = {}   # chat_id → list of timestamps (for rate limiting)

# Pending access requests: req_id → {chat_id, user}
# req_id is sent as callback_data in the admin's inline buttons.
_pending_requests = {}
_pending_lock = threading.Lock()
_next_req_id = 1


# ── LOGGING ───────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── AUTHORIZED USERS (shared JSON) ────────────────────────────
def load_authorized():
    """Load the list of authorized chat IDs. Creates the file on first run."""
    with _auth_lock:
        if not os.path.exists(AUTH_FILE):
            _save_authorized_unlocked(INITIAL_AUTHORIZED)
            return list(INITIAL_AUTHORIZED)
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return list(data.get("users", []))
            return list(data)
        except Exception as e:
            log(f"ERROR reading {AUTH_FILE}: {e}")
            return list(INITIAL_AUTHORIZED)


def _save_authorized_unlocked(user_ids):
    """Internal: save without acquiring the lock (caller must hold it)."""
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": list(user_ids)}, f, indent=2)
    except Exception as e:
        log(f"ERROR writing {AUTH_FILE}: {e}")


def save_authorized(user_ids):
    with _auth_lock:
        _save_authorized_unlocked(user_ids)


def add_authorized(chat_id):
    """Append a chat_id to the end of the list. Preserves admin (first)."""
    users = load_authorized()
    if chat_id in users:
        return
    users.append(chat_id)
    save_authorized(users)
    log(f"✓ chat_id {chat_id} added to {AUTH_FILE}")


def is_authorized(chat_id):
    return chat_id in load_authorized()


def get_admin():
    """Return the FIRST chat_id in the list (admin). None if list is empty."""
    users = load_authorized()
    return users[0] if users else None


# ── BROADLINK ─────────────────────────────────────────────────
def get_broadlink():
    """Connect to the Broadlink device (cached, re-auths if needed)."""
    global _broadlink
    with _broadlink_lock:
        if _broadlink is None:
            log("Discovering Broadlink on the network...")
            devs = broadlink.discover(timeout=5)
            if not devs:
                raise Exception("No Broadlink device found on the LAN")
            _broadlink = devs[0]
            _broadlink.auth()
            log(f"Broadlink connected: {_broadlink.type} @ {_broadlink.host[0]}")
        return _broadlink


def open_gate():
    """Send the RF code to open the gate. Returns True on success."""
    if not os.path.exists(CODES_FILE):
        log(f"ERROR: {CODES_FILE} not found")
        return False
    with open(CODES_FILE) as f:
        codes = json.load(f)
    if GATE_CODE_NAME not in codes:
        log(f"ERROR: code '{GATE_CODE_NAME}' not found in {CODES_FILE}")
        return False

    packet = base64.b64decode(codes[GATE_CODE_NAME])
    try:
        d = get_broadlink()
        d.send_data(packet)
        return True
    except Exception as e:
        log(f"ERROR sending RF: {e}")
        # reset cache so next call re-discovers
        with _broadlink_lock:
            global _broadlink
            _broadlink = None
        return False


# ── TELEGRAM API ──────────────────────────────────────────────
TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def tg_send(chat_id, text, reply_markup=None):
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        requests.post(f"{TG_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        log(f"ERROR Telegram send: {e}")


def tg_answer_callback(callback_id, text=""):
    try:
        requests.post(f"{TG_URL}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text},
                      timeout=10)
    except Exception as e:
        log(f"ERROR Telegram answerCallback: {e}")


def tg_edit_message(chat_id, message_id, text):
    try:
        requests.post(f"{TG_URL}/editMessageText",
                      json={"chat_id": chat_id, "message_id": message_id,
                            "text": text, "parse_mode": "HTML"},
                      timeout=10)
    except Exception as e:
        log(f"ERROR Telegram edit: {e}")


def tg_set_commands():
    """Register the bot's slash-commands list.

    Telegram shows this as the blue "Menu" button next to the message input
    (server-side UI, cannot be hidden by the user).
    """
    try:
        commands = [
            {"command": "menu",    "description": "Show the keyboard menu"},
            {"command": "open",    "description": "Open the gate"},
            {"command": "request", "description": "Request access"},
            {"command": "help",    "description": "Show help"},
        ]
        r = requests.post(
            f"{TG_URL}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
        if r.json().get("ok"):
            log(f"✓ Commands registered: {[c['command'] for c in commands]}")
        else:
            log(f"⚠ setMyCommands failed: {r.json()}")
    except Exception as e:
        log(f"ERROR setMyCommands: {e}")


def tg_set_menu_button():
    """Force the 'Menu' button to show the commands list for every chat."""
    try:
        r = requests.post(
            f"{TG_URL}/setChatMenuButton",
            json={"menu_button": {"type": "commands"}},
            timeout=10,
        )
        if r.json().get("ok"):
            log("✓ Menu button set to 'commands'")
        else:
            log(f"⚠ setChatMenuButton failed: {r.json()}")
    except Exception as e:
        log(f"ERROR setChatMenuButton: {e}")


def tg_get_updates(offset):
    try:
        r = requests.get(
            f"{TG_URL}/getUpdates",
            params={
                "offset": offset,
                "timeout": 30,
                # allowed_updates also enables inline button callbacks
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=40,
        )
        return r.json().get("result", [])
    except Exception as e:
        log(f"ERROR Telegram poll: {e}")
        return []


# ── KEYBOARDS ─────────────────────────────────────────────────
def keyboard_main():
    """Persistent keyboard shown to authorized users."""
    return {
        "keyboard": [
            [{"text": MENU_OPEN}],
            [{"text": MENU_REQUEST}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def keyboard_request_only():
    """Keyboard shown to non-authorized users (only the request button)."""
    return {
        "keyboard": [[{"text": MENU_REQUEST}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def inline_approve_reject(req_id):
    """Inline buttons for the admin to approve/reject an access request."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{req_id}"},
            {"text": "❌ Reject",  "callback_data": f"reject:{req_id}"},
        ]]
    }


# ── RATE LIMITING ─────────────────────────────────────────────
def rate_limit_ok(chat_id):
    now = time.time()
    timestamps = _last_opens.get(chat_id, [])
    timestamps = [t for t in timestamps if now - t < 60]
    if len(timestamps) >= RATE_LIMIT_PER_MIN:
        return False
    timestamps.append(now)
    _last_opens[chat_id] = timestamps
    return True


# ── OPEN GATE FLOW ────────────────────────────────────────────
def do_open(chat_id, user):
    """Rate-checked open + broadcast notification."""
    if not rate_limit_ok(chat_id):
        log(f"⚠ Rate limit hit for {user} (chat_id: {chat_id})")
        tg_send(chat_id, "⏱ Too many requests. Wait a minute.",
                reply_markup=keyboard_main())
        return False

    log(f"🚪 OPEN requested by {user} (chat_id: {chat_id})")
    tg_send(chat_id, "🔓 Opening gate...", reply_markup=keyboard_main())

    if open_gate():
        tg_send(chat_id, "✅ Gate opened!", reply_markup=keyboard_main())
        log("✓ Gate opened")

        # Broadcast to the OTHER authorized users
        t = datetime.now().strftime("%H:%M")
        msg = f"🚪 Gate opened by <b>{user}</b> at {t}"
        for other_id in load_authorized():
            if other_id != chat_id:
                tg_send(other_id, msg)
        return True
    else:
        tg_send(chat_id, "❌ Error. Check the log.",
                reply_markup=keyboard_main())
        return False


# ── ACCESS REQUESTS ──────────────────────────────────────────
def handle_access_request(chat_id, user):
    """Send access request to the admin (first user in the whitelist)."""
    admin = get_admin()
    if admin is None:
        tg_send(chat_id, "❌ No admin configured.")
        return

    if chat_id == admin:
        tg_send(chat_id, "ℹ You're the admin.",
                reply_markup=keyboard_main())
        return

    if is_authorized(chat_id):
        tg_send(chat_id, "✅ You're already authorized.",
                reply_markup=keyboard_main())
        return

    # Register the pending request
    global _next_req_id
    with _pending_lock:
        req_id = _next_req_id
        _next_req_id += 1
        _pending_requests[req_id] = {"chat_id": chat_id, "user": user}

    log(f"📨 Access request from {user} (chat_id: {chat_id}) → admin {admin}")

    tg_send(chat_id,
            "📨 Request sent to the administrator.\n"
            "You'll be notified once it's approved.")

    tg_send(admin,
            f"🔑 <b>New access request</b>\n\n"
            f"User: <b>{user}</b>\n"
            f"Chat ID: <code>{chat_id}</code>\n\n"
            f"Authorize them to open the gate?",
            reply_markup=inline_approve_reject(req_id))


def handle_access_decision(callback):
    """Handle the admin's click on Approve/Reject."""
    chat_id  = callback["from"]["id"]
    data     = callback.get("data", "")
    cb_id    = callback["id"]
    msg      = callback.get("message", {})
    msg_id   = msg.get("message_id")

    # Only the admin can decide
    if chat_id != get_admin():
        tg_answer_callback(cb_id, "⛔ Only the admin can approve.")
        return

    try:
        action, req_id_str = data.split(":", 1)
        req_id = int(req_id_str)
    except Exception:
        tg_answer_callback(cb_id, "Invalid callback.")
        return

    with _pending_lock:
        req = _pending_requests.pop(req_id, None)

    if not req:
        tg_answer_callback(cb_id, "Request expired or already handled.")
        if msg_id:
            tg_edit_message(chat_id, msg_id,
                            "⚠ Request already handled or expired.")
        return

    target_id = req["chat_id"]
    target_user = req["user"]

    if action == "approve":
        add_authorized(target_id)
        tg_answer_callback(cb_id, f"✓ Authorized: {target_user}")
        if msg_id:
            tg_edit_message(
                chat_id, msg_id,
                f"✅ <b>Authorized</b>\n"
                f"User: {target_user}\n"
                f"Chat ID: <code>{target_id}</code>\n"
                f"(saved to <code>{os.path.basename(AUTH_FILE)}</code>)"
            )
        tg_send(target_id,
                "🎉 <b>Access granted!</b>\n"
                "You can now open the gate using the menu below.",
                reply_markup=keyboard_main())
    else:  # reject
        tg_answer_callback(cb_id, f"✗ Rejected: {target_user}")
        if msg_id:
            tg_edit_message(
                chat_id, msg_id,
                f"❌ <b>Rejected</b>\n"
                f"User: {target_user}\n"
                f"Chat ID: <code>{target_id}</code>"
            )
        tg_send(target_id, "⛔ Your request was rejected.")


# ── MESSAGE HANDLER ───────────────────────────────────────────
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user = (msg["chat"].get("username")
            or msg["chat"].get("first_name") or "?")
    text = msg.get("text", "").strip()
    text_low = text.lower()

    # "Request access" is open to NON-authorized users too
    if text == MENU_REQUEST or text_low in ("/request", "request access"):
        handle_access_request(chat_id, user)
        return

    # Authorization check for everything else
    if not is_authorized(chat_id):
        log(f"⚠ Access DENIED from {user} (chat_id: {chat_id}) text: '{text}'")
        tg_send(chat_id,
                "⛔ You're not authorized.\n"
                f"Your chat id is: <code>{chat_id}</code>\n\n"
                "Use the button below to request access.",
                reply_markup=keyboard_request_only())
        return

    # /open → open the gate directly
    if text_low == "/open":
        do_open(chat_id, user)
        return

    # /start /help /menu → (re)show menu
    if text_low in ("/start", "/help", "/menu", "menu", "menù"):
        tg_send(chat_id,
                "🚪 <b>Gate Bot</b>\n\n"
                "Use the menu below, the blue <b>Menu</b> button,\n"
                "or type one of these:\n"
                + "\n".join(f"• <code>{k}</code>" for k in KEYWORDS)
                + "\n\nIf the keyboard disappears, send /menu to bring it back.",
                reply_markup=keyboard_main())
        return

    # Open button or text keyword
    if text == MENU_OPEN or any(k in text_low for k in KEYWORDS):
        do_open(chat_id, user)
        return

    # Anything else: just re-show the menu
    tg_send(chat_id,
            "Use the menu below 👇 (send /menu if it disappears)",
            reply_markup=keyboard_main())


# ── MAIN LOOP ─────────────────────────────────────────────────
def main():
    log("═" * 50)
    log("Gate Bot starting")
    log("═" * 50)

    # Ensure the whitelist file exists
    log(f"Authorized users file: {AUTH_FILE}")
    if not os.path.exists(AUTH_FILE):
        log(f"  → file does NOT exist, creating with {len(INITIAL_AUTHORIZED)} initial users")
        save_authorized(INITIAL_AUTHORIZED)
        if os.path.exists(AUTH_FILE):
            log("  ✓ file created")
        else:
            log("  ✗ PROBLEM: file was not created (permissions?)")
    else:
        log("  → file exists, using it")

    users = load_authorized()
    log(f"Authorized users: {len(users)} → {users}")
    log(f"Admin (first in list): {users[0] if users else 'NONE'}")

    # Pre-connect to the Broadlink
    try:
        get_broadlink()
    except Exception as e:
        log(f"⚠ Broadlink not reachable at startup: {e}")
        log("  (will retry on first command)")

    # Verify bot token
    try:
        r = requests.get(f"{TG_URL}/getMe", timeout=10).json()
        if r.get("ok"):
            bot = r["result"]
            log(f"Bot: @{bot['username']} ({bot['first_name']})")
        else:
            log(f"✗ getMe failed: {r}")
            return
    except Exception as e:
        log(f"✗ Cannot reach Telegram: {e}")
        return

    # Register slash-commands + force the Menu button to show them
    tg_set_commands()
    tg_set_menu_button()

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
                        log(f"ERROR message handler: {e}")
                elif "callback_query" in upd:
                    try:
                        handle_access_decision(upd["callback_query"])
                    except Exception as e:
                        log(f"ERROR callback handler: {e}")
        except KeyboardInterrupt:
            log("Shutdown requested.")
            break
        except Exception as e:
            log(f"ERROR in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()