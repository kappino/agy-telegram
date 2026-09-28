#!/usr/bin/env python3
"""
CLI helper to send proactive push notifications to Telegram via the Sentinel socket.
"""

import argparse
import json
import socket
import sys

from pathlib import Path

def main():
    default_sock = "/run/agy-telegram/sentinel.sock" if Path("/run/agy-telegram").is_dir() else "/tmp/agy-sentinel.sock"

    parser = argparse.ArgumentParser(description="Send instant push alerts to agy-telegram")
    parser.add_argument("--title", "-t", default="Antigravity Sentinel", help="Notification title")
    parser.add_argument("--message", "-m", required=True, help="Notification message body")
    parser.add_argument("--level", "-l", choices=["info", "warning", "alert"], default="info", help="Severity level")
    parser.add_argument("--socket", "-s", default=default_sock, help="Path to sentinel socket")

    args = parser.parse_args()

    payload = {
        "title": args.title,
        "message": args.message,
        "level": args.level,
    }

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(args.socket)
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        res = client.recv(4096)
        client.close()

        if not res:
            print("Error: Empty response from Sentinel server", file=sys.stderr)
            sys.exit(1)

        try:
            resp_data = json.loads(res.decode("utf-8"))
            if resp_data.get("status") != "delivered":
                print(f"Error: Sentinel server rejected notification: {resp_data}", file=sys.stderr)
                sys.exit(1)
        except json.JSONDecodeError as jde:
            print(f"Error: Malformed JSON response from Sentinel server: {jde}", file=sys.stderr)
            sys.exit(1)

        print("Notification sent to Telegram successfully.")
    except FileNotFoundError:
        print(f"Socket not found at {args.socket}. Ensure agy-telegram daemon is running.", file=sys.stderr)
        sys.exit(1)
    except socket.timeout:
        print(f"Error: Connection to {args.socket} timed out after 5.0s", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error sending notification: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
