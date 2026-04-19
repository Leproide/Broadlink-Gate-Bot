"""
Telegram bot to open a Broadlink RF gate over LAN.

Features:
  - Persistent menu keyboard ("Open" and "Request access") always visible
  - Text triggers still work (backward compatible)
  - Access requests are sent only to the ADMIN (first user in whitelist).
    Admin receives inline buttons to approve or reject.
  - Admin commands to list and remove authorized users.
  - Whitelist stored in JSON so external scripts can read/edit it.
  - Per-user rate limiting.
  - Broadcast notification to the other authorized users when the gate opens.
  - Security hardening: HTML escape, token scrubbing in logs, access-request
    TTL, private-chat only, optional Broadlink IP/MAC pinning.

Requirements:
    pip install broadlink requests
"""

import broadlink, base64, json, os, time, requests, threading, html
from datetime import datetime


# ── CONFIG ────────────────────────────────────────────────────
# Paths are always relative to this script (safe when launched as a service).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load the bot token from an environment variable or a `.token` file (priority),
# with a hardcoded placeholder as last-resort fallback.
# NEVER commit a real token to git: it is effectively the key to your gate.
TELEGRAM_TOKEN = (
    os.environ.get("GATEBOT_TOKEN")
    or (open(os.path.join(_SCRIPT_DIR, ".token")).read().strip()
        if os.path.exists(os.path.join(_SCRIPT_DIR, ".token"))
        else None)
    or "PUT-YOUR-BOT-TOKEN-HERE"
)

# Optional: pin the Broadlink device by IP and/or MAC.
# - If BROADLINK_IP is set: direct probe, no UDP broadcast on the LAN.
# - If BROADLINK_MAC is also set: MAC mismatch = hard error (anti rogue-device).
# - Both None: generic UDP discovery (original behavior).
BROADLINK_IP  = None   # e.g. "192.168.1.139"
BROADLINK_MAC = None   # e.g. "25:3e:f1:a7:df:24"

# JSON file with the whitelist of authorized users
AUTH_FILE  = os.path.join(_SCRIPT_DIR, "authorized_users.json")
CODES_FILE = os.path.join(_SCRIPT_DIR, "broadlink_codes.json")
LOG_FILE   = os.path.join(_SCRIPT_DIR, "gate_bot.log")

# Initial whitelist (used only the first time the bot runs,
# when authorized_users.json does not yet exist).
# The FIRST id is the admin: only the admin receives access requests
# and can manage users through /users.
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

# Access request TTL (seconds). Stale requests are auto-dropped.
ACCESS_REQUEST_TTL = 15 * 60   # 15 minutes


# ── STATE ─────────────────────────────────────────────────────
_broadlink = None
_broadlink_lock = threading.Lock()
_auth_lock = threading.Lock()
_last_opens = {}   # chat_id → list of timestamps (for rate limiting)

# Pending access requests: req_id → {chat_id, user, ts}
_pending_requests = {}
_pending_lock = threading.Lock()
_next_req_id = 1


# ── LOGGING ───────────────────────────────────────────────────
def _scrub(text):
    """Strip the bot token from log lines (requests exceptions leak the URL)."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "PUT-YOUR-BOT-TOKEN-HERE":
        return str(text)
    return str(text).replace(TELEGRAM_TOKEN, "***TOKEN***")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {_scrub(msg)}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def h(text):
    """HTML-escape for user-controlled content in parse_mode=HTML messages."""
    return html.escape(str(text) if text is not None else "", quote=False)


# ── AUTHORIZED USERS (shared JSON) ────────────────────────────
# New format:     {"users": [{"id": 123, "username": "foo"}, ...]}
# Backward compat: {"users": [123, 456]}  or plain list  [123, 456]

def _normalize_user(u):
    """Normalize a user entry (int or dict) to dict {id, username}."""
    if isinstance(u, int):
        return {"id": u, "username": None}
    if isinstance(u, dict) and "id" in u:
        return {
            "id": int(u["id"]),
            "username": u.get("username"),
        }
    return None


def load_authorized():
    """Load authorized users from JSON. Returns list of dict {id, username}.
    Creates the file on first run."""
    with _auth_lock:
        if not os.path.exists(AUTH_FILE):
            initial = [{"id": uid, "username": None} for uid in INITIAL_AUTHORIZED]
            _save_authorized_unlocked(initial)
            return initial
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("users", []) if isinstance(data, dict) else data
            users = [_normalize_user(u) for u in raw]
            return [u for u in users if u and u["id"]]
        except Exception as e:
            log(f"ERROR reading {AUTH_FILE}: {e}")
            return [{"id": uid, "username": None} for uid in INITIAL_AUTHORIZED]


def _save_authorized_unlocked(users):
    """Internal: save without acquiring the lock (caller must hold it)."""
    normalized = [_normalize_user(u) for u in users]
    normalized = [u for u in normalized if u and u["id"]]
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": normalized}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"ERROR writing {AUTH_FILE}: {e}")


def save_authorized(users):
    with _auth_lock:
        _save_authorized_unlocked(users)


def get_authorized_ids():
    """Return only the IDs (for broadcast and is_authorized)."""
    return [u["id"] for u in load_authorized()]


def add_authorized(chat_id, username=None):
    """Append a user, or just update their username if already present."""
    users = load_authorized()
    found = False
    for u in users:
        if u["id"] == chat_id:
            if username and u.get("username") != username:
                u["username"] = username
            found = True
            break
    if not found:
        users.append({"id": chat_id, "username": username})
        log(f"✓ chat_id {chat_id} (@{username or '?'}) added to {AUTH_FILE}")
    save_authorized(users)


def remove_authorized(chat_id):
    """Remove a user. Returns True if it was removed."""
    users = load_authorized()
    new_users = [u for u in users if u["id"] != chat_id]
    if len(new_users) == len(users):
        return False
    save_authorized(new_users)
    log(f"✓ chat_id {chat_id} REMOVED from {AUTH_FILE}")
    return True


def update_username_if_known(chat_id, username):
    """Silently refresh the stored username of an authorized user."""
    if not username:
        return
    users = load_authorized()
    changed = False
    for u in users:
        if u["id"] == chat_id and u.get("username") != username:
            u["username"] = username
            changed = True
            break
    if changed:
        save_authorized(users)


def is_authorized(chat_id):
    return chat_id in get_authorized_ids()


def get_admin():
    """Return the FIRST authorized chat_id (admin). None if the list is empty."""
    users = load_authorized()
    return users[0]["id"] if users else None


def format_user(u):
    """Format for messages: '@username (id)' or just '(id)'.
    Username is always HTML-escaped (Telegram limits it to [A-Za-z0-9_]
    but we escape defensively)."""
    if u.get("username"):
        return f"@{h(u['username'])} <code>{u['id']}</code>"
    return f"<code>{u['id']}</code>"


# ── BROADLINK ─────────────────────────────────────────────────
def get_broadlink():
    """Connect to the Broadlink device (cached, re-auths if needed).

    - If BROADLINK_IP is set: direct probe (no LAN broadcast).
    - Else: generic UDP discovery + optional MAC filter.
    """
    global _broadlink
    with _broadlink_lock:
        if _broadlink is None:

            # Path 1: direct connection when IP is known
            if BROADLINK_IP:
                log(f"Connecting directly to Broadlink {BROADLINK_IP}...")
                try:
                    # We still need one targeted probe to get MAC/devtype.
                    devs = broadlink.discover(
                        timeout=5,
                        discover_ip_address=BROADLINK_IP,
                    )
                    if not devs:
                        raise Exception(f"No response from {BROADLINK_IP}")
                    d = devs[0]
                    if BROADLINK_MAC:
                        actual_mac = d.mac.hex(":").lower()
                        if actual_mac != BROADLINK_MAC.lower():
                            raise Exception(
                                f"MAC mismatch: expected {BROADLINK_MAC}, got {actual_mac}"
                            )
                    _broadlink = d
                except Exception as e:
                    raise Exception(f"Direct Broadlink connect failed: {e}")

            # Path 2: generic discovery
            else:
                log("Discovering Broadlink on the network...")
                devs = broadlink.discover(timeout=5)
                if not devs:
                    raise Exception("No Broadlink device found on the LAN")

                if BROADLINK_MAC:
                    filtered = [
                        d for d in devs
                        if d.mac.hex(":").lower() == BROADLINK_MAC.lower()
                    ]
                    if not filtered:
                        raise Exception(
                            f"No Broadlink with MAC={BROADLINK_MAC}"
                        )
                    _broadlink = filtered[0]
                else:
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
            {"command": "users",   "description": "Manage users (admin)"},
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

        # Broadcast to the OTHER authorized users (username HTML-escaped)
        t = datetime.now().strftime("%H:%M")
        msg = f"🚪 Gate opened by <b>{h(user)}</b> at {t}"
        for other_id in get_authorized_ids():
            if other_id != chat_id:
                tg_send(other_id, msg)
        return True
    else:
        tg_send(chat_id, "❌ Error. Check the log.",
                reply_markup=keyboard_main())
        return False


# ── ACCESS REQUESTS ──────────────────────────────────────────
def _purge_expired_requests():
    """Drop pending access requests older than ACCESS_REQUEST_TTL."""
    now = time.time()
    with _pending_lock:
        expired = [rid for rid, req in _pending_requests.items()
                   if now - req.get("ts", now) > ACCESS_REQUEST_TTL]
        for rid in expired:
            req = _pending_requests.pop(rid, None)
            if req:
                log(f"⏰ Access request expired: {req.get('user')} "
                    f"(chat_id: {req.get('chat_id')})")


def handle_access_request(chat_id, user):
    """Send access request to the admin (first user in the whitelist)."""
    _purge_expired_requests()

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

    # One pending request at a time per chat_id
    with _pending_lock:
        for req in _pending_requests.values():
            if req["chat_id"] == chat_id:
                tg_send(chat_id,
                        "📨 You already have a pending request.")
                return

    # Register the pending request
    global _next_req_id
    with _pending_lock:
        req_id = _next_req_id
        _next_req_id += 1
        _pending_requests[req_id] = {
            "chat_id": chat_id,
            "user": user,
            "ts": time.time(),
        }

    log(f"📨 Access request from {user} (chat_id: {chat_id}) → admin {admin}")

    tg_send(chat_id,
            "📨 Request sent to the administrator.\n"
            "You'll be notified once it's approved.\n"
            f"<i>The request expires in {ACCESS_REQUEST_TTL // 60} minutes.</i>")

    tg_send(admin,
            f"🔑 <b>New access request</b>\n\n"
            f"User: <b>{h(user)}</b>\n"
            f"Chat ID: <code>{chat_id}</code>\n\n"
            f"Authorize them to open the gate?",
            reply_markup=inline_approve_reject(req_id))


def handle_access_decision(callback):
    """Handle the admin's click on Approve/Reject."""
    _purge_expired_requests()

    chat_id  = callback["from"]["id"]
    data     = callback.get("data", "")
    cb_id    = callback["id"]
    msg      = callback.get("message", {})
    msg_id   = msg.get("message_id")

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
        add_authorized(target_id, target_user)
        tg_answer_callback(cb_id, f"✓ Authorized: {target_user}")
        if msg_id:
            tg_edit_message(
                chat_id, msg_id,
                f"✅ <b>Authorized</b>\n"
                f"User: {h(target_user)}\n"
                f"Chat ID: <code>{target_id}</code>\n"
                f"(saved to <code>{h(os.path.basename(AUTH_FILE))}</code>)"
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
                f"User: {h(target_user)}\n"
                f"Chat ID: <code>{target_id}</code>"
            )
        tg_send(target_id, "⛔ Your request was rejected.")


# ── USER MANAGEMENT (admin only) ─────────────────────────────
def handle_list_users(chat_id):
    """Show the user list with inline remove buttons (admin only)."""
    if chat_id != get_admin():
        tg_send(chat_id, "⛔ Only the admin can manage users.",
                reply_markup=keyboard_main())
        return

    users = load_authorized()
    if not users:
        tg_send(chat_id, "No authorized users.")
        return

    lines = ["👥 <b>Authorized users</b>\n"]
    for i, u in enumerate(users):
        tag = "👑 ADMIN" if i == 0 else f"   {i}."
        lines.append(f"{tag} {format_user(u)}")

    buttons = []
    for u in users:
        if u["id"] == chat_id:
            continue  # admin cannot remove themselves
        label = f"🗑 @{u['username']}" if u.get("username") else f"🗑 {u['id']}"
        buttons.append([{"text": label, "callback_data": f"remove:{u['id']}"}])

    if buttons:
        lines.append("\nTap a user to remove them:")
        reply_markup = {"inline_keyboard": buttons}
    else:
        lines.append("\n<i>No removable users (admin only).</i>")
        reply_markup = None

    tg_send(chat_id, "\n".join(lines), reply_markup=reply_markup)


def handle_remove_request(callback):
    """Admin clicked '🗑 Remove' → ask for confirmation."""
    chat_id = callback["from"]["id"]
    cb_id   = callback["id"]
    msg     = callback.get("message", {})
    msg_id  = msg.get("message_id")
    data    = callback.get("data", "")

    if chat_id != get_admin():
        tg_answer_callback(cb_id, "⛔ Admin only.")
        return

    try:
        target_id = int(data.split(":", 1)[1])
    except Exception:
        tg_answer_callback(cb_id, "Invalid callback.")
        return

    target_user = None
    for u in load_authorized():
        if u["id"] == target_id:
            target_user = u
            break
    if target_user is None:
        tg_answer_callback(cb_id, "User not found.")
        return

    tg_answer_callback(cb_id)
    confirm_markup = {"inline_keyboard": [[
        {"text": "✅ Yes, remove",
         "callback_data": f"confirm_remove:{target_id}"},
        {"text": "❌ Cancel",
         "callback_data": "cancel_remove"},
    ]]}
    if msg_id:
        tg_edit_message(
            chat_id, msg_id,
            f"⚠️ <b>Confirm removal</b>\n\n"
            f"Really remove {format_user(target_user)}?\n"
            f"They will no longer be able to open the gate."
        )
        tg_send(chat_id, "Confirm?", reply_markup=confirm_markup)


def handle_remove_confirm(callback):
    """Admin confirmed → remove the user."""
    chat_id = callback["from"]["id"]
    cb_id   = callback["id"]
    msg     = callback.get("message", {})
    msg_id  = msg.get("message_id")
    data    = callback.get("data", "")

    if chat_id != get_admin():
        tg_answer_callback(cb_id, "⛔ Admin only.")
        return

    try:
        target_id = int(data.split(":", 1)[1])
    except Exception:
        tg_answer_callback(cb_id, "Invalid callback.")
        return

    target_user = None
    for u in load_authorized():
        if u["id"] == target_id:
            target_user = u
            break

    if remove_authorized(target_id):
        tg_answer_callback(cb_id, "Removed")
        name = format_user(target_user) if target_user else f"<code>{target_id}</code>"
        if msg_id:
            tg_edit_message(chat_id, msg_id, f"✅ Removed: {name}")
        tg_send(target_id,
                "⚠️ Your access to the gate has been revoked by the admin.")
    else:
        tg_answer_callback(cb_id, "User already removed")
        if msg_id:
            tg_edit_message(chat_id, msg_id, "ℹ️ User not found or already removed.")


def handle_remove_cancel(callback):
    """Admin canceled the removal."""
    chat_id = callback["from"]["id"]
    cb_id   = callback["id"]
    msg     = callback.get("message", {})
    msg_id  = msg.get("message_id")

    if chat_id != get_admin():
        tg_answer_callback(cb_id, "⛔ Admin only.")
        return

    tg_answer_callback(cb_id, "Canceled")
    if msg_id:
        tg_edit_message(chat_id, msg_id, "❌ Removal canceled.")


# ── CALLBACK ROUTER ──────────────────────────────────────────
def handle_callback(callback):
    """Dispatch inline-button callbacks by data prefix."""
    data = callback.get("data", "")
    if data.startswith("approve:") or data.startswith("reject:"):
        handle_access_decision(callback)
    elif data.startswith("remove:"):
        handle_remove_request(callback)
    elif data.startswith("confirm_remove:"):
        handle_remove_confirm(callback)
    elif data == "cancel_remove":
        handle_remove_cancel(callback)
    else:
        tg_answer_callback(callback["id"], "")


# ── MESSAGE HANDLER ───────────────────────────────────────────
def handle_message(msg):
    # Reject messages from groups, channels or any non-private chat.
    # The bot is designed for 1-to-1 conversations only.
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        log(f"⚠ Ignored message from non-private chat "
            f"(type={chat.get('type')}, id={chat.get('id')})")
        return

    chat_id = chat["id"]
    user = (chat.get("username") or chat.get("first_name") or "?")
    text = msg.get("text", "").strip()
    text_low = text.lower()

    # "Request access" is open to non-authorized users too
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

    # Silently refresh username if it changed on Telegram
    update_username_if_known(chat_id, chat.get("username"))

    # /users → admin-only user management
    if text_low in ("/users", "/admin"):
        handle_list_users(chat_id)
        return

    # /open → open the gate directly
    if text_low == "/open":
        do_open(chat_id, user)
        return

    # /start /help /menu → (re)show menu
    if text_low in ("/start", "/help", "/menu", "menu"):
        tg_send(chat_id,
                "🚪 <b>Gate Bot</b>\n\n"
                "Use the menu below, the blue <b>Menu</b> button,\n"
                "or type one of these:\n"
                + "\n".join(f"• <code>{h(k)}</code>" for k in KEYWORDS)
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

    # Hard check: is the token actually configured?
    if TELEGRAM_TOKEN == "PUT-YOUR-BOT-TOKEN-HERE" or not TELEGRAM_TOKEN:
        log("✗ ERROR: TELEGRAM_TOKEN is not configured.")
        log("   Set the GATEBOT_TOKEN environment variable")
        log("   or create a .token file in the script directory.")
        return

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
    log(f"Admin (first in list): {users[0]['id'] if users else 'NONE'}")

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

    # Register slash commands + force the Menu button to show them
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
                        handle_callback(upd["callback_query"])
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
