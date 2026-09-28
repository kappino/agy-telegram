"""
Entrypoint CLI for agy-telegram daemon and service utilities.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from agy_telegram.config import load_config
from agy_telegram.core.bot import AgyTelegramBot

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("agy_telegram.cli")

def main():
    parser = argparse.ArgumentParser(description="agy-telegram: Mobile Cockpit & Gateway for Google Antigravity")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to run")

    # start command
    start_parser = subparsers.add_parser("start", help="Start the agy-telegram daemon")
    start_parser.add_argument("--config", "-c", help="Path to config.toml")

    # service command
    service_parser = subparsers.add_parser("service", help="Manage systemd service unit")
    service_parser.add_argument("action", choices=["generate", "install"], help="Service action")

    args = parser.parse_args()

    if args.command == "service":
        unit_content = f"""[Unit]
Description=Antigravity Mobile Telegram Cockpit (agy-telegram)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/var/lib/agy-telegram
ExecStart=/usr/local/bin/agy-telegram start
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
RuntimeDirectory=agy-telegram
RuntimeDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""
        if args.action == "generate":
            print(unit_content)
        elif args.action == "install":
            target = Path("/etc/systemd/system/agy-telegram.service")
            target.write_text(unit_content, encoding="utf-8")
            print(f"Service unit installed to {target}")
            print("To enable and start:")
            print("  systemctl daemon-reload")
            print("  systemctl enable --now agy-telegram")
        return

    # Default / start
    try:
        config = load_config(args.config if hasattr(args, "config") else None)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    bot = AgyTelegramBot(config)
    try:
        asyncio.run(bot.run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("agy-telegram shutdown completed.")

if __name__ == "__main__":
    main()
