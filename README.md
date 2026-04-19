# Broadlink Gate Bot

Local LAN control for Broadlink RM-series remotes, plus a Telegram bot to open an RF gate from anywhere — all **without** the Broadlink cloud.

## What it does

Three small Python scripts that work together:

- **`learn_code.py`** — captures an IR or RF code from a physical remote (gate, TV, AC, etc.) and saves it locally in `broadlink_codes.json`.
- **`send_code.py`** — replays a saved code over LAN. Fully offline after learning.
- **`gate_bot.py`** — a Telegram bot that opens the gate when an authorized user presses a menu button or sends a keyword, and notifies the other authorized users that the gate was opened. Non-authorized users can request access; the admin approves, rejects, lists, or removes users with one tap.

Everything runs on your own machine/home server. The Broadlink device talks to your script directly over UDP on your LAN. No Broadlink cloud account is queried at runtime.

## Why not just use the Broadlink app?

The official Broadlink app works, but:

- It routes commands through Broadlink's servers even when your phone and the device are on the same WiFi.
- Recent firmware locks the device in "cloud mode", preventing any local access until you explicitly unlock it.
- Automating anything (e.g. "open the gate when I send a Telegram message") requires third-party integrations and a cloud round-trip.

Local control fixes all of that: commands fire in <200 ms, your setup keeps working when the internet is down, and you own the data.

## Hardware tested

| Device                | Model ID | Protocols           | Notes                               |
| --------------------- | -------- | ------------------- | ----------------------------------- |
| **Broadlink RM4 Pro** | `0x649b` | IR + RF 433/315 MHz | Confirmed working with this package |

Other RM-series devices supported by `python-broadlink` should work too (RM mini, RM mini 3, RM Pro, RM4 mini, RM4C mini, RM4C Pro…). IR-only models (mini) obviously can't control an RF gate.

## Prerequisites

- Python 3.9+
- A Broadlink RM device **on the same LAN** as the machine running the scripts
- For the gate bot: a Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

```bash
git clone https://github.com/<you>/broadlink-gate-bot.git
cd broadlink-gate-bot
pip install -r requirements.txt
```

## Unlocking a cloud-locked device

If your device was added via the latest Broadlink app, it's probably locked. Unlock it once:

1. Open the Broadlink app, tap your device
2. Go to device **Settings** → toggle off **Lock device** (or equivalent)
3. Close the app

Verify by running `learn_code.py` — if it connects and authenticates, you're good.

If there's no unlock toggle, factory-reset the device (hold the reset button ~6s until the LED blinks fast), reconfigure only the WiFi via the Broadlink app, then immediately close the app without adding any remotes. From that point, manage everything from these scripts.

## Usage

### 1. Learn a code

For an RF gate remote:

```bash
python learn_code.py
```

You'll be prompted for a name (e.g. `gate_open`). The script runs a two-step RF capture:

1. **Frequency sweep** — hold the remote button down continuously, close to the Broadlink (<30 cm), until the frequency is identified.
2. **Code capture** — release, then press the button once briefly.

For IR codes, the process is similar but single-step (no frequency sweep).

Codes are stored in `broadlink_codes.json`:

```json
{
  "gate_open": "sMCwBGibBgAAAfkP...base64..."
}
```

### 2. Send a saved code

```bash
python send_code.py gate_open
```

This reads `broadlink_codes.json` and transmits the saved code. Purely local, <200 ms latency.

### 3. Configure the Telegram bot

#### 3.1 — Bot token (never hardcode it!)

The bot loads the token from **one of these sources**, in priority order:

1. `GATEBOT_TOKEN` environment variable
2. `.token` file in the bot's directory (single line, no quotes, no trailing spaces)
3. the `TELEGRAM_TOKEN` constant at the top of `gate_bot.py` (default: `"PUT-YOUR-BOT-TOKEN-HERE"` — the bot refuses to start if this is left unchanged)

**Recommended** (so it stays out of git):

```bash
# Linux / macOS
echo "123456:AABB...your-token..." > .token

# Windows PowerShell
Set-Content -Path .token -Value "123456:AABB...your-token..." -NoNewline
```

`.token` is already in `.gitignore`.

If you accidentally commit or share your token, **revoke it immediately** via `/revoke` on [@BotFather](https://t.me/BotFather) and generate a new one.

#### 3.2 — Initial whitelist and admin

Edit `gate_bot.py` and set at the top of the file:

- `INITIAL_AUTHORIZED` — the initial whitelist used only on first run. **The first chat_id in the list is the admin** and is the only user who receives access requests and can manage users. Get your chat id from [@userinfobot](https://t.me/userinfobot).
- `KEYWORDS` — text messages that also trigger the gate (`open`, `apri`, `🚪`, …)
- `GATE_CODE_NAME` — the key in `broadlink_codes.json` that holds the gate code

#### 3.3 — Optional: pin the Broadlink device

To avoid LAN broadcast discovery on every reconnect (and prevent rogue devices on the network), set:

```python
BROADLINK_IP  = "192.168.1.139"         # direct connect, no broadcast
BROADLINK_MAC = "25:3e:f1:a7:df:24"     # optional: hard MAC match
```

- **IP only** — connects directly to that IP (~200 ms vs 5 s broadcast)
- **IP + MAC** — as above, but rejects the connection if the MAC doesn't match (strong anti-spoof)
- **MAC only** — does a generic discovery and filters by MAC (useful if IP changes via DHCP)
- **Both `None`** — original behavior: takes the first Broadlink found on the LAN

#### 3.4 — Run

```bash
python gate_bot.py
```

## How the bot works

- On first launch the bot creates `authorized_users.json` using `INITIAL_AUTHORIZED` as seed. From then on, **the JSON file is the source of truth** — the list can be read or modified by other scripts without restarting the bot.
- Authorized users always see a persistent keyboard with two buttons: **🚪 Open** and **🔑 Request access**. They can also simply type any of the `KEYWORDS`.
- A blue **Menu** button next to the message input exposes the bot's slash commands (`/menu`, `/open`, `/request`, `/users`, `/help`) and is always available even if the reply keyboard gets hidden. The bot registers these commands automatically via `setMyCommands` / `setChatMenuButton` on every startup, so there's nothing to configure manually with @BotFather.
- Non-authorized users see only **🔑 Request access**. When they press it, only the admin receives a notification with inline **Approve / Reject** buttons. Requests expire automatically after 15 minutes (`ACCESS_REQUEST_TTL`).
- On approval, the new user's chat_id and username are appended to `authorized_users.json` and they receive an instant notification with the full menu.
- When any user opens the gate, the other authorized users get a broadcast notification with who opened it and at what time.

### Admin commands

Only the admin (first user in `authorized_users.json`) can use these:

| Command                       | What it does                                                                                                                                                                            |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/users`                      | Shows all authorized users with their username and chat_id, plus a **🗑 remove** button for each non-admin user. Removal is two-step with a confirm prompt; removed users are notified. |
| Approve/Reject inline buttons | When another user presses **🔑 Request access**, the admin gets a message with ✅ / ❌ buttons to decide.                                                                                 |

Usernames in the JSON are refreshed automatically every time a user sends a message, so the list always reflects their current Telegram handle.

### `authorized_users.json` format

```json
{
  "users": [
    {"id": 1765834, "username": "alice"},
    {"id": 1234458623, "username": "bob"},
    {"id": 917455621, "username": null}
  ]
}
```

The first entry is always the admin. `username` can be `null` if it's not known yet (it fills in on the user's first message). You can edit this file manually or from other tools — the bot reads it fresh on every check.

The old format (`{"users": [1363844, 987654321]}` — plain integer list) is still read correctly and migrated silently to the new format on the first write.

## Security considerations

A gate is a physical-security control. This package includes several hardening measures:

- **Whitelist enforcement** — every inbound action (message or inline button) is checked against `authorized_users.json`. Denied attempts are logged.
- **Rate limit** — `RATE_LIMIT_PER_MIN` (default 3/min per user) prevents abuse.
- **Access-request TTL** — pending requests auto-expire after 15 minutes so an admin can't accidentally approve a forgotten request weeks later. One pending request per user at a time.
- **Private-chat only** — the bot ignores messages from groups, channels, or any non-private chat.
- **HTML injection hardening** — all user-supplied text (first names, usernames) is HTML-escaped before being sent back in HTML-formatted messages, so a user with a crafted first name like `</b>ADMIN<b>` can't spoof tags.
- **Token scrubbing in logs** — if a `requests` exception leaks the Telegram API URL, the token is redacted to `***TOKEN***` before the line is written to `gate_bot.log`.
- **Token via env var or file** — the token is never required to live in the source code; it's loaded from `GATEBOT_TOKEN` or from `.token` (both excluded from git).
- **Optional Broadlink pinning** — IP and/or MAC can be hardcoded to prevent a rogue Broadlink on the LAN from being picked up.
- **Audit log** — all open attempts, authorizations, removals, and denied attempts are timestamped in `gate_bot.log`.
- **Don't commit secrets** — the included `.gitignore` excludes `.token`, `broadlink_codes.json`, `authorized_users.json`, and `gate_bot.log`.

## Running the bot as a service

**Linux (systemd)**:

```ini
# /etc/systemd/system/gate-bot.service
[Unit]
Description=Broadlink Gate Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/user/broadlink-gate-bot
Environment="GATEBOT_TOKEN=your-token-here"
ExecStart=/usr/bin/python3 gate_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gate-bot
```

**Windows (NSSM)**:

Download [NSSM](https://nssm.cc/), then:

```powershell
nssm install GateBot "C:\Python313\python.exe" "C:\path\to\gate_bot.py"
nssm set GateBot AppDirectory "C:\path\to"
nssm set GateBot AppEnvironmentExtra "GATEBOT_TOKEN=your-token-here"
nssm start GateBot
```

All paths inside `gate_bot.py` are resolved relative to the script's own location, so the bot works correctly even when launched as a service or from a different working directory.

## Troubleshooting

| Symptom                                            | Likely cause / Fix                                                                                                                                                    |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AuthenticationError` at startup                   | Device is in cloud mode — unlock via Broadlink app (see above)                                                                                                        |
| Frequency sweep "finds" instantly without pressing | Ambient RF noise or stale library — make sure `broadlink` is ≥ 0.19                                                                                                   |
| Frequency found but code capture times out         | Move remote closer to Broadlink, try shorter button press, replace remote battery                                                                                     |
| Code captured but gate doesn't open                | Make sure the remote is a **fixed-code** type. Rolling-code remotes (KeeLoq etc.) cannot be replayed                                                                  |
| Broadlink not discovered                           | Device and PC must be on the same subnet. Check no AP isolation / IoT VLAN. Consider pinning `BROADLINK_IP`                                                           |
| `TELEGRAM_TOKEN is not configured` at startup      | Set the env var `GATEBOT_TOKEN` or create a `.token` file next to the script                                                                                          |
| Reply keyboard doesn't appear in Telegram          | Send `/menu` or tap the blue **Menu** button next to the input field. If still missing, close the chat and reopen it; the reply keyboard is a per-chat client setting |
| `authorized_users.json` not created                | Check write permissions in the script directory. The bot logs the absolute path at startup                                                                            |

## Rolling-code remotes

If your gate uses a rolling code (the transmitted code changes every press — common on newer remotes), replay attacks are intentionally blocked by the receiver. This package cannot control such remotes. Alternatives:

- Wire a small relay directly to the gate's internal "open" button (e.g. a Shelly 1)
- Add a fixed-code remote to the receiver's memory and use that
- Swap the receiver for one with an API / cloud-free integration

## Credits

- [`python-broadlink`](https://github.com/mjg59/python-broadlink) by Matthew Garrett and contributors — the library that makes all of this possible.

## Screenshot

### Admin side:

<img width="569" height="1280" alt="1_GateBot" src="https://github.com/user-attachments/assets/22d9b917-8a3e-49ae-8e54-ee2caa3312d5" />

<img width="575" height="1280" alt="GateBot_Grant" src="https://github.com/user-attachments/assets/1ea3308e-1628-4ca2-85d6-02e49977f838" />

### Users side:

<img width="576" height="1280" alt="GateBot_Request" src="https://github.com/user-attachments/assets/44f03509-8763-4bcb-9d60-c21e360caf72" />

## License

GPL v2
