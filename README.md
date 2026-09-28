# agy-telegram 🚀

[![CI Quality Gate](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agy-telegram.svg?color=blue)](https://pypi.org/project/agy-telegram/)
[![Python Versions](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![OS Support](https://img.shields.io/badge/OS-Linux%20%7C%20macOS%20%7C%20WSL2-orange)](https://github.com/kappino/agy-telegram)

> **The Open-Source Mobile Cockpit & Gateway for Google Antigravity (`agy`)**

Control your local AI coding and systems agent directly from Telegram with zero latency and full terminal parity. Designed for developers and engineers who want complete oversight of their local or cloud agent on their mobile device without exposing SSH or opening incoming firewall ports.

---

## ✨ Key Capabilities

- 🔒 **Zero Inbound Attack Surface**: Operates entirely via HTTPS long-polling. No inbound ports to open, no public webhooks or reverse proxies required.
- 🛡️ **Strict 1-to-1 User Whitelist**: Silently drops any message originating from unauthorized Telegram user IDs.
- ⚡ **Instant Input Injection (0ms Latency)**: Bypasses slow simulated key typing by directly utilizing `tmux` copy buffers (`set-buffer` & `paste-buffer`), injecting multi-thousand-word prompts instantaneously.
- 🚦 **Human-in-the-Loop Interactive Approvals**:
  - Automatically captures critical tool executions (`run_command`, destructive file actions, package managers).
  - Presents interactive inline buttons (`[ ✅ Approve ]` and `[ ❌ Reject ]`) directly in your chat.
- 💭 **Unified Dynamic Status Message**:
  - Maintains a clean single status message per turn: `Elaborating...` ➔ `Action: Executing command` with code preview ➔ Approval Prompt.
  - Dynamically cleans up after completion to ensure a clutter-free chat history.
- 📄 **Structured JSONL Audit Watcher**:
  - Tails the native audit log (`transcript.jsonl`) instead of scraping raw ANSI virtual terminal panes, eliminating VT100 control codes, screen artifacts, and truncation bugs.
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

    subgraph Host System (Linux / macOS Server or PC)
        subgraph agy-telegram Daemon
            BOT[Telegram Bot Core (AsyncIO)]
            TW[JSONL Transcript Stream Watcher]
            TM[Tmux Mirror Engine]
            SENTINEL[Unix Socket IPC Listener]
        end

        subgraph Local Agent Environment
            TMUX[Tmux Session: main:0.0]
            AGY[Antigravity CLI (agy)]
            AUDIT[transcript.jsonl Audit Log]
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
    CRON --> NOTIFY_CLI
    NOTIFY_CLI -->|Unix Socket| SENTINEL
    SENTINEL -->|Push Alert| BOT
```

## 💻 Platform Compatibility & Requirements

- **Linux**: Fully supported natively (Ubuntu, Debian, Arch, Fedora, RHEL, Proxmox containers). Native `systemd` service generation supported.
- **macOS**: Fully supported natively (install `tmux` via Homebrew: `brew install tmux`).
- **Windows**: Supported via **WSL2 (Windows Subsystem for Linux)**. Running `agy-telegram` inside WSL2 provides 100% feature parity including Tmux buffer paste and Unix domain sockets.
  > *Note on Native Windows (cmd/PowerShell)*: Native Windows does not provide `tmux` or POSIX Unix Domain Sockets (`AF_UNIX`). Windows users should run the agent and `agy-telegram` inside WSL2 or use Docker.

---

## 🚀 Quickstart

### 1. Prerequisites

- Python 3.10+
- `tmux` & `git`
- Google Antigravity (`agy` CLI) installed and accessible in your `$PATH`

### 2. Installation

Clone and install `agy-telegram` in editable mode or into a virtual environment:

```bash
git clone https://github.com/kappino/agy-telegram.git
cd agy-telegram
pip install -e .
```

This installs two global CLI binaries:
- `agy-telegram`: Daemon runtime and control manager.
- `agy-notify`: Client CLI for dispatching proactive push notifications.

### 3. Configuration

Create a configuration file at `/etc/agy-telegram/config.toml` (or in `~/.config/agy-telegram/config.toml`):

```toml
[telegram]
bot_token = "YOUR_BOT_TOKEN_FROM_BOTFATHER"
allowed_users = [123456789]  # Your numeric Telegram user ID

[agent]
executable = "agy"
default_workspace = "/path/to/your/project"
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

> **Tip**: You can also configure parameters via environment variables (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`).

### 4. Running the Gateway

#### Interactive / Foreground Mode
```bash
agy-telegram start
```

#### Running as a Background Systemd Service (Linux)
```bash
# Generate and install systemd unit
agy-telegram service install
systemctl daemon-reload
systemctl enable --now agy-telegram
```

---

## 📱 Mobile Commands & Operations

| Command | Action |
| :--- | :--- |
| `/start` | Initializes conversation and displays connection diagnostics |
| `/status` | Fetches local host metrics (load average, memory, disk usage) |
| `/abort` | Sends an interrupt signal (`Ctrl+C`) to the active terminal session |
| `/help` | Displays operational manual and keyboard shortcuts |
| `*Free Text*` | Any plain text sent in chat is instantly pasted into the active agent terminal |

---

## 🔔 Proactive Push Alerts (`agy-notify`)

You can send notifications directly to your Telegram chat from any local bash script or cron job without needing to import Python libraries:

```bash
# Standard Information Notification
agy-notify --level info --title "Build Finished" --message "Release v1.2.0 bundle generated successfully."

# Warning Notification
agy-notify --level warning --title "High Memory Usage" --message "RAM utilization exceeded 85%."

# Critical Alert Notification
agy-notify --level alert --title "Service Outage" --message "Web server daemon crashed unexpectedly."
```

---

## 🛠️ Development & Testing

Run unit tests and linters:

```bash
pip install -e ".[dev]"
pytest tests/
```

---

## 🛡️ License

MIT License. Open source and built for the Antigravity community.
