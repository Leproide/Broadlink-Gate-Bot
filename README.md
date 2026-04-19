# Broadlink Gate Bot

Local LAN control for Broadlink RM-series remotes, plus a Telegram bot to open an RF gate from anywhere — all **without** the Broadlink cloud.

## What it does

Three small Python scripts that work together:

- **`learn_code.py`** — captures an IR or RF code from a physical remote (gate, TV, AC, etc.) and saves it locally in `broadlink_codes.json`.
- **`send_code.py`** — replays a saved code over LAN. Fully offline after learning.
- **`gate_bot.py`** — a Telegram bot that opens the gate when an authorized user presses a menu button or sends a keyword, and notifies the other authorized users that the gate was opened. Non-authorized users can request access; the admin approves or rejects with one tap.

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

### 3. Run the Telegram gate bot

Edit `gate_bot.py` and set at the top of the file:

- `TELEGRAM_TOKEN` — your bot token from [@BotFather](https://t.me/BotFather)
- `INITIAL_AUTHORIZED` — the initial whitelist. **The first chat_id in the list is the admin** and is the only user who receives access requests. Get your chat id from [@userinfobot](https://t.me/userinfobot).
- `KEYWORDS` — text messages that also trigger the gate (`open`, `apri`, `🚪`, …)
- `GATE_CODE_NAME` — the key in `broadlink_codes.json` that holds the gate code

Then:

```bash
python gate_bot.py
```

**How the bot works:**

- On first launch the bot creates `authorized_users.json` using `INITIAL_AUTHORIZED` as seed. From then on, **the JSON file is the source of truth** — the list can be read or modified by other scripts without restarting the bot.
- Authorized users always see a persistent keyboard with two buttons: **🚪 Open** and **🔑 Request access**. They can also simply type any of the `KEYWORDS`.
- Non-authorized users see only **🔑 Request access**. When they press it, only the admin (first user in the JSON list) receives a notification with inline **Approve / Reject** buttons.
- On approval, the new user's chat_id is appended to `authorized_users.json` and they receive an instant notification with the full menu.
- When any user opens the gate, the other authorized users get a broadcast notification with who opened it and at what time.

### `authorized_users.json` format

```json
{
  "users": [
    1363844,
    1650718330,
    987654321
  ]
}
```

The first entry is always the admin. You can edit this file manually or from other tools — the bot reads it fresh on every check.

### Security considerations

A gate is a physical-security control. Treat the bot token and chat-id whitelist accordingly:

- Only users in `authorized_users.json` can trigger the gate. Everyone else gets a refusal and the attempt is logged.
- A rate limit (`RATE_LIMIT_PER_MIN`, default 3/min per user) prevents abuse.
- All open attempts — successful and denied — are written to `gate_bot.log` with timestamps.
- Never commit your bot token or `broadlink_codes.json` to git. The included `.gitignore` already excludes them (along with `authorized_users.json`).

### Running the bot as a service

**Linux (systemd)**:

```ini
# /etc/systemd/system/gate-bot.service
[Unit]
Description=Broadlink Gate Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/user/broadlink-gate-bot
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
nssm start GateBot
```

All paths inside `gate_bot.py` are resolved relative to the script's own location, so the bot works correctly even when launched as a service or from a different working directory.

## Troubleshooting

| Symptom                                            | Likely cause / Fix                                                                                   |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `AuthenticationError` at startup                   | Device is in cloud mode — unlock via Broadlink app (see above)                                       |
| Frequency sweep "finds" instantly without pressing | Ambient RF noise or stale library — make sure `broadlink` is ≥ 0.19                                  |
| Frequency found but code capture times out         | Move remote closer to Broadlink, try shorter button press, replace remote battery                    |
| Code captured but gate doesn't open                | Make sure the remote is a **fixed-code** type. Rolling-code remotes (KeeLoq etc.) cannot be replayed |
| Broadlink not discovered                           | Device and PC must be on the same subnet. Check no AP isolation / IoT VLAN                           |
| Menu keyboard doesn't appear in Telegram           | Force-close and reopen the Telegram app; send `/start` to the bot to resync                          |
| `authorized_users.json` not created                | Check write permissions in the script directory. The bot logs the absolute path at startup           |

## Rolling-code remotes

If your gate uses a rolling code (the transmitted code changes every press — common on newer remotes), replay attacks are intentionally blocked by the receiver. This package cannot control such remotes. Alternatives:

- Wire a small relay directly to the gate's internal "open" button (e.g. a Shelly 1)
- Add a fixed-code remote to the receiver's memory and use that
- Swap the receiver for one with an API / cloud-free integration

## Credits

- [`python-broadlink`](https://github.com/mjg59/python-broadlink) by Matthew Garrett and contributors — the library that makes all of this possible.

## License

GPL v2
