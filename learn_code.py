"""
learn_code.py
Captures an IR or RF code from a physical remote and saves it to
broadlink_codes.json for later replay via send_code.py or gate_bot.py.

Usage:
    python learn_code.py
"""

import broadlink
import time
import base64
import json
import os
import sys

CODES_FILE = "broadlink_codes.json"


def discover_device():
    print("Looking for Broadlink device on the LAN...")
    devices = broadlink.discover(timeout=5)
    if not devices:
        print("No Broadlink device found. Make sure it is on the same LAN.")
        sys.exit(1)
    d = devices[0]
    d.auth()
    print(f"Connected to {d.type} @ {d.host[0]}\n")
    return d


def save_code(name: str, code_b64: str):
    codes = {}
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE) as f:
            codes = json.load(f)
    codes[name] = code_b64
    with open(CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2)
    print(f"\nSaved '{name}' to {CODES_FILE}")


def learn_rf(device):
    """Two-step RF capture: frequency sweep, then code capture."""
    name = input("Name for this code (e.g. 'gate_open'): ").strip() or "gate"

    print("\n── Step 1/2: Frequency sweep ──")
    print("  When the LED turns on, HOLD the remote button DOWN")
    print("  (close to the Broadlink, <30 cm) until the frequency is found.")
    input("  Press ENTER to start...")

    device.sweep_frequency()
    print("\n  LED on → HOLD the button NOW!")

    frequency = None
    for i in range(30):
        time.sleep(1)
        try:
            found, freq = device.check_frequency()
            if found:
                frequency = freq
                print(f"\n  Frequency locked: {freq:.2f} MHz (after {i+1}s)")
                break
            print(f"  [{i+1:2d}s] scanning... current={freq:.2f} MHz")
        except Exception as e:
            print(f"  Error: {e}")
            break

    if not frequency:
        print("\n  Frequency not identified. Tips:")
        print("    - Move the remote closer to the Broadlink")
        print("    - Press harder / longer")
        print("    - Check remote battery")
        try:
            device.cancel_sweep_frequency()
        except Exception:
            pass
        sys.exit(1)

    print("\n  Release the button now.")
    time.sleep(2)

    print(f"\n── Step 2/2: Code capture @ {frequency:.2f} MHz ──")
    input("  Press ENTER, then press the remote button ONCE briefly...")

    device.find_rf_packet(frequency)
    print("\n  LED on → press the button ONCE now!")

    packet = None
    for i in range(20):
        time.sleep(1)
        try:
            packet = device.check_data()
            if packet:
                break
        except broadlink.exceptions.ReadError:
            pass
        except broadlink.exceptions.StorageError:
            pass
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {e}")
        print(f"  [{i+1:2d}s] waiting for button press...")

    if not packet:
        print("\n  No code captured. Please retry.")
        sys.exit(1)

    code_b64 = base64.b64encode(packet).decode()
    print(f"\n  Captured {len(packet)} bytes")
    save_code(name, code_b64)
    print(f"\nDone! Try it now:  python send_code.py {name}")


def learn_ir(device):
    """Single-step IR capture."""
    name = input("Name for this code (e.g. 'tv_power'): ").strip() or "code"

    print("\n── IR capture ──")
    input("  Press ENTER, then point the remote at the Broadlink and press a button...")

    device.enter_learning()
    print("\n  LED on → press the remote button now!")

    packet = None
    for i in range(20):
        time.sleep(1)
        try:
            packet = device.check_data()
            if packet:
                break
        except broadlink.exceptions.ReadError:
            pass
        except broadlink.exceptions.StorageError:
            pass
        print(f"  [{i+1:2d}s] waiting...")

    if not packet:
        print("\n  No code captured. Please retry.")
        sys.exit(1)

    code_b64 = base64.b64encode(packet).decode()
    print(f"\n  Captured {len(packet)} bytes")
    save_code(name, code_b64)
    print(f"\nDone! Try it now:  python send_code.py {name}")


def main():
    device = discover_device()

    print("What type of code do you want to learn?")
    print("  1) RF  (gate remote, doorbell, garage door, etc.)")
    print("  2) IR  (TV, AC, audio, etc.)")
    choice = input("Choice [1/2]: ").strip()

    if choice == "2":
        learn_ir(device)
    else:
        learn_rf(device)


if __name__ == "__main__":
    main()
