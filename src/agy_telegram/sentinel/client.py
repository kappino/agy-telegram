#!/usr/bin/env python3
"""
CLI helper to send proactive push notifications to Telegram via the Sentinel socket.
"""

import argparse
import json
import socket
import sys

def main():
    parser = argparse.ArgumentParser(description="Send instant push alerts to agy-telegram")
    parser.add_argument("--title", "-t", default="Sistema Aegis", help="Notification title")
    parser.add_argument("--message", "-m", required=True, help="Notification message body")
    parser.add_argument("--level", "-l", choices=["info", "warning", "alert"], default="info", help="Severity level")
    parser.add_argument("--socket", "-s", default="/tmp/agy-sentinel.sock", help="Path to sentinel socket")

    args = parser.parse_args()

    payload = {
        "title": args.title,
        "message": args.message,
        "level": args.level,
    }

    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(args.socket)
        client.sendall(json.dumps(payload).encode("utf-8"))
        res = client.recv(1024)
        client.close()
        print("✅ Notifica inviata a Telegram con successo!")
    except FileNotFoundError:
        print(f"❌ Socket non trovato in {args.socket}. Assicurati che agy-telegram sia attivo!", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ Errore durante l'invio della notifica: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
