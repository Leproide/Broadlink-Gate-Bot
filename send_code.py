"""
send_code.py
Replays a code previously saved by learn_code.py.
Fully offline — talks to the Broadlink device over LAN.

Usage:
    python send_code.py <code_name>
"""

import broadlink
import base64
import json
import sys
import os

CODES_FILE = "broadlink_codes.json"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <code_name>")
        if os.path.exists(CODES_FILE):
            with open(CODES_FILE) as f:
                codes = json.load(f)
            print(f"\nAvailable codes: {list(codes.keys())}")
        sys.exit(1)

    name = sys.argv[1]

    if not os.path.exists(CODES_FILE):
        print(f"{CODES_FILE} not found. Run learn_code.py first.")
        sys.exit(1)

    with open(CODES_FILE) as f:
        codes = json.load(f)

    if name not in codes:
        print(f"Code '{name}' not found. Available: {list(codes.keys())}")
        sys.exit(1)

    devices = broadlink.discover(timeout=5)
    if not devices:
        print("Broadlink device not found on the LAN.")
        sys.exit(1)

    d = devices[0]
    d.auth()

    packet = base64.b64decode(codes[name])
    d.send_data(packet)
    print(f"Sent '{name}' ({len(packet)} bytes)")


if __name__ == "__main__":
    main()
