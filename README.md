# agy-telegram

[![CI](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/kappino/agy-telegram/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agy-telegram.svg?color=blue)](https://pypi.org/project/agy-telegram/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Bridge bidirezionale tra Telegram e la CLI di Google Antigravity (`agy`). Consente di interagire con l'agente locale tramite una sessione `tmux` attiva, approvare o rifiutare comandi via bottoni inline e ricevere notifiche proattive da script locali.

---

## Architettura

```mermaid
flowchart LR
    subgraph Client
        TG[Telegram Mobile / Desktop]
    end

    subgraph Host
        BOT[agy-telegram daemon]
        TMUX[tmux session: main:0.0]
        AGY[Antigravity CLI]
        LOG[transcript.jsonl]
        SOCK[(/tmp/agy-sentinel.sock)]
    end

    TG <-->|HTTPS Long Polling| BOT
    BOT <-->|tmux buffer paste / send-keys| TMUX
    TMUX <--> AGY
    AGY -->|Event stream| LOG
    LOG -->|Byte-offset tail O(1)| BOT
    SOCK -->|IPC Unix Socket mode 0600| BOT
```

### Principi di Progettazione
- **Zero porte in ascolto esterno**: Comunicazione solo via HTTPS long polling in uscita verso le API Telegram.
- **Autorizzazione rigida**: Filtro sugli ID utente Telegram ammessi su tutti i messaggi e callback inline.
- **Input istantaneo via Tmux Buffer**: I comandi lunghi vengono iniettati tramite `set-buffer` e `paste-buffer`, evitando la digitazione simulata tasto per tasto.
- **Tailing O(1)**: Il monitoraggio dei passaggi dell'agente legge i delta del file `transcript.jsonl` tracciando l'offset in byte, senza rileggere l'intero file.
- **IPC Locale Sicuro**: Socket UNIX locale `/tmp/agy-sentinel.sock` con permessi `0600` e verifica delle credenziali kernel (`SO_PEERCRED`) per prevenire spoofing o privilege escalation.

---

## Requisiti

- Python 3.10+
- Linux (supporto per systemd), macOS o WSL2
- `tmux` installato e disponibile nel `PATH`
- Google Antigravity CLI (`agy`) configurato sul sistema

---

## Installazione

```bash
git clone https://github.com/kappino/agy-telegram.git
cd agy-telegram
pip install -e .
```

Il pacchetto fornisce due comandi eseguibili:
- `agy-telegram`: Demone principale per la gestione del bridge Telegram.
- `agy-notify`: Utility CLI per inviare notifiche push via socket locale.

---

## Configurazione

Crea la directory di configurazione e copia il file di esempio:

```bash
mkdir -p ~/.config/agy-telegram
cp config.example.toml ~/.config/agy-telegram/config.toml
```

Configura `~/.config/agy-telegram/config.toml`:

```toml
[telegram]
bot_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
allowed_users = [123456789] # ID numerico Telegram (da @userinfobot)

[agent]
executable = "agy"
default_workspace = "."
default_model = "gemini-3.8-flash"
default_effort = "high"
timeout_seconds = 600

[sentinel]
enabled = true
socket_path = "/tmp/agy-sentinel.sock"

[mirror]
enabled = true
mode = "tmux"
target_session = "main:0.0"
log_file = "/tmp/agy-telegram-chat.log"
check_interval_seconds = 1.0
```

> **Variabili d'ambiente alternative**: È possibile configurare le credenziali tramite `TELEGRAM_BOT_TOKEN` e `TELEGRAM_ALLOWED_USER_ID` in un file `.env` locale o di sistema.

---

## Utilizzo

### 1. Avvia Antigravity in una sessione tmux

```bash
tmux new -s main "agy"
```

### 2. Avvia il demone agy-telegram

```bash
agy-telegram start
```

### 3. Invia messaggi da Telegram

Apri la chat del bot su Telegram e invia `/start`. Qualsiasi messaggio inviato in chat verrà inoltrato direttamente nella console attiva di Antigravity.

---

## Comandi Disponibili

| Comando | Descrizione |
|---|---|
| `/start` | Mostra lo stato del bridge e la tastiera rapida. |
| `/status` | Esegue la diagnostica delle risorse host (carico, memoria, disco). |
| `/model` | Mostra il modello LLM attivo e permette di cambiarlo al volo. |
| `/usage` | Statistiche sui token consumati, capienza del contesto e passi del turno. |
| `/autoedit` | Attiva o disattiva l'auto-approvazione delle modifiche ai file (`accept-edits`). |
| `/mode` | Imposta la modalità operativa (`accept-edits`, `default`, `plan`). |
| `/new` | Inizializza una nuova sessione azzerando il contesto precedente. |
| `/sessions` | Elenca le ultime sessioni archiviate per riprenderle. |
| `/abort` | Invia un segnale di interruzione `Ctrl+C` al terminale. |
| `/help` | Guida rapida all'uso. |

Quando l'agente richiede conferma per un'operazione (ad esempio l'esecuzione di un comando di sistema), compaiono i pulsanti inline **Approva** e **Rifiuta**.

---

## Notifiche Push da Script Locali (`agy-notify`)

Qualsiasi script bash, job cron o pipeline locale può inviare notifiche push immediate a Telegram senza dipendenze esterne:

```bash
# Info
agy-notify --level info --title "Backup" --message "Backup database completato con successo."

# Warning
agy-notify --level warning --title "Memoria" --message "Utilizzo RAM superiore all'85%."

# Alert critico
agy-notify --level alert --title "Servizio Offline" --message "Il servizio nginx non risponde."
```

---

## Esecuzione con Systemd

Per eseguire il demone come servizio di sistema in background:

```bash
sudo cp systemd/agy-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agy-telegram
sudo systemctl status agy-telegram
```

La unit systemd include direttive di sandboxing di processo (`PrivateTmp=true`, `ProtectSystem=full`, `ProtectHome=read-only`, `RuntimeDirectory=agy-telegram`).

---

## Test e Sviluppo

Installazione delle dipendenze di test:

```bash
pip install -e ".[dev]"
```

Esecuzione dei test:

```bash
python3 -m unittest discover tests
```

---

## Licenza

Distribuito sotto licenza [MIT](LICENSE).
