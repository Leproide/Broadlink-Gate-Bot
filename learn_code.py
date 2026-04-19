"""
Learn an IR or RF code from a physical remote using a Broadlink RM device.
Saves the captured code in base64 format to broadlink_codes.json.
"""

import broadlink, base64, json, os, sys, time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CODES_FILE = os.path.join(_SCRIPT_DIR, "broadlink_codes.json")


def load_codes():
    if not os.path.exists(CODES_FILE):
        return {}
    with open(CODES_FILE) as f:
        return json.load(f)


def save_codes(codes):
    with open(CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2)


def discover_device():
    print("Discovering Broadlink on the network...")
    devs = broadlink.discover(timeout=5)
    if not devs:
        print("✗ No Broadlink device found. Check that it's on the same LAN.")
        sys.exit(1)
    d = devs[0]
    d.auth()
    print(f"✓ Connected: {d.type} @ {d.host[0]}  (model: 0x{d.devtype:04x})")
    return d


def learn_ir(device):
    print("\nPoint the remote at the Broadlink and press the button...")
    device.enter_learning()
    for _ in range(30):
        time.sleep(1)
        try:
            data = device.check_data()
            if data:
                return data
        except Exception:
            continue
    return None


def learn_rf(device):
    print("\nStep 1/2 — FREQUENCY SWEEP")
    print("Hold the remote button pressed CONTINUOUSLY near the Broadlink (<30cm).")
    print("Keep holding until the frequency is detected.")

    device.sweep_frequency()
    found = False
    for _ in range(30):
        time.sleep(1)
        try:
            if device.check_frequency():
                found = True
                break
        except Exception:
            continue

    if not found:
        print("✗ Frequency not detected. Release the button and try again.")
        try: device.cancel_sweep_frequency()
        except Exception: pass
        return None

    print("✓ Frequency detected")
    print("\nStep 2/2 — CODE CAPTURE")
    print("Release the button, then press it ONCE briefly.")

    device.find_rf_packet()
    for _ in range(30):
        time.sleep(1)
        try:
            data = device.check_data()
            if data:
                return data
        except Exception:
            continue
    return None


def main():
    name = input("Name for this code (e.g. gate_open): ").strip()
    if not name:
        print("Name required.")
        return

    kind = input("Type? [i] IR  [r] RF  (default i): ").strip().lower()
    kind = "rf" if kind == "r" else "ir"

    d = discover_device()
    data = learn_rf(d) if kind == "rf" else learn_ir(d)

    if not data:
        print("✗ Capture failed.")
        return

    b64 = base64.b64encode(data).decode("ascii")
    codes = load_codes()
    codes[name] = b64
    save_codes(codes)

    print(f"\n✓ Code '{name}' saved to {CODES_FILE}  ({len(b64)} chars)")


if __name__ == "__main__":
    main()
