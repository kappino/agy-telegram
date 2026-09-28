# agy-telegram 🚀

[![CI Quality Gate](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agy-telegram.svg?color=blue)](https://pypi.org/project/agy-telegram/)
[![Python Versions](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![OS Support](https://img.shields.io/badge/OS-Linux%20%7C%20macOS%20%7C%20WSL2-orange)](https://github.com/kappino/agy-telegram)

> **The Open-Source Mobile Cockpit & Gateway for Google Antigravity (`agy`)**

Control your local AI coding and autonomous sysadmin agent directly from Telegram with zero latency and full terminal parity. Designed for developers, DevOps, and engineers who want complete oversight of their local or cloud agent on their mobile device without exposing SSH or opening incoming firewall ports.

---

## ✨ Key Features

- 🔒 **Zero Inbound Attack Surface**: Operates entirely via HTTPS long-polling. No inbound ports to open, no public webhooks or reverse proxies required.
- 🛡️ **Strict User Whitelist**: Silently drops any message originating from unauthorized Telegram user IDs.
- ⚡ **Instant Input Injection (0ms Latency)**: Bypasses simulated key typing by directly utilizing `tmux` copy buffers (`set-buffer` & `paste-buffer`), injecting multi-thousand-word prompts instantaneously.
- 🚦 **Human-in-the-Loop Interactive Approvals**:
  - Automatically captures critical tool executions (`run_command`, destructive file actions, package managers).
  - Presents interactive inline buttons (`[ ✅ Approve ]` and `[ ❌ Reject ]`) directly in your chat.
- 🧠 **Dynamic AI Model Switching (`/model`)**:
  - View the active model and switch models on the fly (Gemini 3.8 Flash, Pro, Thinking, Claude 3.5 Sonnet, etc.) via interactive inline keyboards.
  - Updates configuration dynamically without restarting your agent session.
- 📊 **Real-Time Token & Context Saturation (`/usage`)**:
  - Inspect total input and output token consumption for the active session.
  - Context window saturation gauge with visual progress bar and turn counter.
- ⚡ **Auto-Edit Mode Control (`/autoedit` & `/mode`)**:
  - Toggle between `accept-edits` (automatic file modifications) and `default` review modes right from your phone.
- 🧹 **Session Management (`/new` & `/sessions`)**:
  - Start a clean Antigravity session with `/new` (clearing context while keeping history safe).
  - Inspect active tmux sessions and pane attachments with `/sessions`.
- 💭 **Unified Dynamic Status Message**:
  - Maintains a clean single status message per turn: `Elaborating...` ➔ `Action: Executing command` with code preview ➔ Approval Prompt.
  - Dynamically cleans up after completion to ensure a clutter-free chat history.
- 📄 **Efficient JSONL Audit Watcher**:
  - Efficiently tails the native audit log (`transcript.jsonl`) with persistent byte offset tracking, eliminating screen scraping artifacts, ANSI escapes, and CPU churn.
- 🎨 **Telegram HTML Engine**:
  - Converts agent Markdown into validated Telegram HTML with syntax-highlighted code blocks, expandable quotes, and tap-to-copy code snippets without parse errors.
- 📢 **Proactive Push Notification Hub (`agy-notify`)**:
  - Listens on a local Unix Domain Socket (`/tmp/agy-sentinel.sock`).
  - Allows local scripts, cron jobs, or monitoring systems to push instant alerts to your phone with a single CLI command:
    ```bash
    agy-notify --level alert --title "Backup Failed" --message "Disk space threshold exceeded on /dev/sda1"
    ```

---

## 🏗️ Architecture

```mermaid
flowchart TD
    subgraph Mobile
        TG[Telegram Mobile App]
    end

    subgraph Host System (Linux / macOS / WSL2)
        subgraph agy-telegram Daemon
            BOT[Telegram Bot Core (AsyncIO)]
            TW[JSONL Transcript Stream Watcher]
            TM[Tmux Mirror Engine]
            SM[Settings & Mode Manager]
            SENTINEL[Unix Socket IPC Listener]
        end

        subgraph Local Agent Environment
            TMUX[Tmux Session: main:0.0]
            AGY[Antigravity CLI (agy)]
            AUDIT[transcript.jsonl Audit Log]
            SETTINGS[settings.json Config]
        end

        subgraph Local System & Scripts
            CRON[Cron Jobs / Scripts / Monitors]
            NOTIFY_CLI[agy-notify CLI]
        end
    end

    TG <-->|HTTPS Long Polling| BOT
    BOT <-->|Instant Tmux Buffer Paste| TM
    TM <-->|Bidirectional I/O| TMUX
    TMUX <--> AGY
    AGY -->|Real-time Events| AUDIT
    AUDIT -->|Async Tail Stream| TW
    TW -->|Live Status & Responses| BOT
    BOT <-->|Dynamic Updates| SM
    SM <-->|Read / Write| SETTINGS
    CRON --> NOTIFY_CLI
    NOTIFY_CLI -->|Unix Socket| SENTINEL
    SENTINEL -->|Push Alert| BOT
```

---

## 💻 Platform Compatibility & Requirements

- **Python**: 3.10 or higher.
- **Operating Systems**:
  - **Linux**: Fully supported natively (Ubuntu, Debian, Arch, Fedora, Proxmox VE containers). Native `systemd` service supported.
  - **macOS**: Fully supported natively (`brew install tmux`).
  - **Windows**: Supported via **WSL2 (Windows Subsystem for Linux)**.
- **Dependencies**: `tmux` and Google Antigravity CLI (`agy`) installed and accessible in `$PATH`.

---

## 🚀 Step-by-Step Setup Guide

### 1. Create a Telegram Bot & Retrieve User ID

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts, and copy your **Bot Token** (e.g. `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
3. Search for [@userinfobot](https://t.me/userinfobot) on Telegram and send `/start` to get your numeric **User ID** (e.g. `123456789`).

### 2. Installation

Clone and install `agy-telegram` in editable mode or into a virtual environment:

```bash
git clone https://github.com/kappino/agy-telegram.git
cd agy-telegram
pip install -e .
```

This installs two commands:
- `agy-telegram`: Daemon runtime and control manager.
- `agy-notify`: Client CLI for dispatching proactive push notifications.

### 3. Configuration

Generate your configuration from the provided template:

```bash
mkdir -p ~/.config/agy-telegram
cp config.example.toml ~/.config/agy-telegram/config.toml
```

Edit `~/.config/agy-telegram/config.toml` with your bot token and numeric user ID:

```toml
[telegram]
bot_token = "YOUR_BOT_TOKEN_FROM_BOTFATHER"
allowed_users = [123456789]  # Your Telegram User ID

[agent]
executable = "agy"
default_workspace = "."
default_model = "gemini-3.8-flash"
default_effort = "high"

[sentinel]
enabled = true
socket_path = "/tmp/agy-sentinel.sock"

[mirror]
enabled = true
mode = "tmux"
target_session = "main:0.0"
log_file = "/tmp/agy-telegram-chat.log"
```

> **Alternative (Environment Variables)**: You can also configure via `.env` or environment variables:
> `TELEGRAM_BOT_TOKEN="xxx"` and `TELEGRAM_ALLOWED_USER_ID="123456789"`.

### 4. Start Antigravity in Tmux

`agy-telegram` attaches to an interactive tmux session. Start Antigravity inside tmux:

```bash
tmux new -s main "agy"
```

### 5. Start the Gateway

In another terminal:

```bash
agy-telegram start
```

Now open Telegram, send `/start` to your bot, and start chatting with Antigravity from your phone!

---

## ⚙️ Running as a Systemd Service (Linux)

To run `agy-telegram` 24/7 as a background daemon on Linux:

1. Copy the unit template:
   ```bash
   sudo cp systemd/agy-telegram.service /etc/systemd/system/
   ```
2. Adjust `User=` and paths in `/etc/systemd/system/agy-telegram.service` if needed.
3. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now agy-telegram
   sudo systemctl status agy-telegram
   ```

---

## 📱 Mobile Commands & Operations

| Command | Action |
| :--- | :--- |
| `/start` | Initializes conversation and displays connection diagnostics |
| `/status` | Fetches host metrics (load average, memory, disk usage, active sessions) |
| `/model` | Displays active AI model and provides interactive inline menu to switch models |
| `/usage` | Analyzes session token consumption, context saturation gauge, and turns |
| `/autoedit` | Toggles automatic file modifications (`accept-edits` vs `default`) |
| `/mode` | Displays current mode and interactive menu to switch (`accept-edits`, `default`, `plan`) |
| `/new` | Starts a fresh Antigravity session (sends `/clear` to terminal) |
| `/sessions` | Lists active tmux sessions and shows attached target pane |
| `/abort` | Sends an interrupt signal (`Ctrl+C`) to the active terminal session |
| `/help` | Displays operational manual and quick action keyboard |
| `*Free Text*` | Any plain text sent in chat is instantly pasted into the active agent terminal |

---

## 🔔 Proactive Push Alerts (`agy-notify`)

Send notifications directly to your Telegram chat from any local bash script, backup pipeline, or cron job without needing to import Python libraries:

```bash
# Standard Information Notification
agy-notify --level info --title "Build Finished" --message "Release v1.2.0 bundle generated successfully."

# Warning Notification
agy-notify --level warning --title "High Memory Usage" --message "RAM utilization exceeded 85%."

# Critical Alert Notification
agy-notify --level alert --title "Service Outage" --message "Web server daemon crashed unexpectedly."
```

---

## ❓ Troubleshooting & FAQ

### Tmux session not found: `main:0.0`
- **Symptom**: The bot warns that the target tmux session does not exist.
- **Fix**: Start tmux first: `tmux new -s main "agy"`. If your session is named differently (e.g. `dev`), set `target_session = "dev:0.0"` in `config.toml` or `export AGY_TMUX_SESSION="dev:0.0"`.

### Telegram Conflict Error (HTTP 409)
- **Symptom**: `telegram.error.Conflict: terminated by other getUpdates request`.
- **Fix**: Another instance of the bot is currently running with the same token. Stop other running processes: `killall agy-telegram` or `sudo systemctl stop agy-telegram`.

### Model changes not reflecting immediately
- **Explanation**: `/model` writes directly to Antigravity's active `settings.json`. Antigravity CLI reloads settings on the fly for the next inference call without needing to restart the process.

### Running safely as an unprivileged user
- `agy-telegram` does **not** require root privileges. You can run it under any regular user account with access to `tmux` and `agy`.

---

## 🛠️ Development & Testing

Run unit tests and linters:

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

---

## 🛡️ License

MIT License. See [LICENSE](LICENSE) for details. Built with ❤️ for the Antigravity community.
