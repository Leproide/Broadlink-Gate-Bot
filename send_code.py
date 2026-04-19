"""
Send a previously-learned IR/RF code through a Broadlink RM device.

Usage:
    python send_code.py <code_name>
    python send_code.py --list
"""

import broadlink, base64, json, os, sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CODES_FILE = os.path.join(_SCRIPT_DIR, "broadlink_codes.json")


def load_codes():
    if not os.path.exists(CODES_FILE):
        print(f"✗ {CODES_FILE} not found. Run learn_code.py first.")
        sys.exit(1)
    with open(CODES_FILE) as f:
        return json.load(f)


def connect():
    devs = broadlink.discover(timeout=5)
    if not devs:
        print("✗ No Broadlink device found on the LAN")
        sys.exit(2)
    d = devs[0]
    d.auth()
    return d


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]
    codes = load_codes()

    if arg in ("--list", "-l"):
        print(f"\nCodes in {CODES_FILE}:")
        for name in sorted(codes):
            print(f"  • {name}")
        print(f"\nTotal: {len(codes)}")
        return

    if arg not in codes:
        print(f"✗ Code '{arg}' not found.")
        print(f"Available: {', '.join(sorted(codes))}")
        sys.exit(1)

    packet = base64.b64decode(codes[arg])
    d = connect()
    d.send_data(packet)
    print(f"✓ Sent '{arg}'")


if __name__ == "__main__":
    main()
