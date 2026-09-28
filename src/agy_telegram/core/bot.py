"""
Main Telegram Bot Application wiring all modules together (v2.0 - Production Grade).
Features:
- Isolated per-turn state management (TurnContext) to prevent race conditions & orphaned messages
- Deterministic, solid human-in-the-loop permission buttons (Approva / Rifiuta)
- /model command to view and switch models dynamically
- /usage command to monitor context tokens, conversation steps, and quotas
- /new command to start a clean session
- /autoedit & /mode commands to toggle and set execution modes (accept-edits, default, plan)
- /sessions command to list and resume previous conversations
- Universal status command with customizable status_command support
- Tmux session healthcheck with actionable user guidance
- True O(1) transcript-driven turn lifecycle with automatic monitoring cleanup
- Zero 24/7 idle CPU polling
"""

import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Message,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from agy_telegram.config import AppConfig
from agy_telegram.core.formatter import split_text, markdown_to_telegram_html
from agy_telegram.core.agy_driver import AgyDriver
from agy_telegram.core.session import SessionManager
from agy_telegram.core.tmux_mirror import TmuxMirror
from agy_telegram.sentinel.server import SentinelServer
from agy_telegram.utils.terminal import TerminalMirror
from agy_telegram.core.transcript_watcher import TranscriptWatcher
from agy_telegram.core.settings_manager import (
    AVAILABLE_MODELS,
    AVAILABLE_MODES,
    get_current_model,
    set_current_model,
    get_current_mode,
    set_current_mode,
)
from agy_telegram.core.usage import compute_session_usage, format_usage_html

logger = logging.getLogger("agy_telegram.bot")


@dataclass
class TurnContext:
    turn_id: str
    user_id: int
    chat_id: int
    status_msg: Optional[Message] = None
    prompt_msg: Optional[Message] = None
    is_prompt_active: bool = False
    active_prompt_cmd: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    is_completed: bool = False


class AgyTelegramBot:
    def __init__(self, config: AppConfig):
        self.config = config
        self.driver = AgyDriver(
            executable=config.agent.executable,
            default_workspace=config.agent.default_workspace,
            default_model=config.agent.default_model,
            default_effort=config.agent.default_effort,
            timeout=config.agent.timeout_seconds,
            skip_permissions=config.agent.skip_permissions,
        )
        self.session_mgr = SessionManager()
        self.sentinel = SentinelServer(socket_path=config.sentinel.socket_path)
        self.mirror_log = TerminalMirror(log_file=config.mirror.log_file)
        self.tmux_mirror = TmuxMirror(
            target_session=config.mirror.target_session,
            check_interval=config.mirror.check_interval_seconds,
        )
        self.is_tmux_mode = (config.mirror.mode == "tmux")
        self.transcript_watcher = TranscriptWatcher()
        self.lock = asyncio.Lock()
        self.app: Optional[Application] = None

        # Isolamento di stato per utente / turno
        self.active_turns: Dict[int, TurnContext] = {}

    def is_authorized(self, update: Update) -> bool:
        if not update.effective_user:
            return False
        user_id = update.effective_user.id
        if user_id not in self.config.telegram.allowed_users:
            logger.warning(f"Accesso negato all'utente non autorizzato: {user_id}")
            return False
        return True

    async def reply_safe(self, update: Update, text: str, reply_markup=None):
        html_text = markdown_to_telegram_html(text)
        chunks = split_text(html_text)
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            try:
                await update.effective_message.reply_text(
                    chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except Exception as e:
                logger.warning(f"Errore invio HTML ({e}), fallback a plain text.")
                await update.effective_message.reply_text(chunk, reply_markup=markup)

    async def broadcast_to_users(self, text: str, reply_markup=None):
        """Invia un messaggio a tutti gli utenti autorizzati."""
        if not self.app:
            return
        html_text = markdown_to_telegram_html(text)
        chunks = split_text(html_text)
        for user_id in self.config.telegram.allowed_users:
            for i, chunk in enumerate(chunks):
                markup = reply_markup if i == len(chunks) - 1 else None
                try:
                    await self.app.bot.send_message(
                        chat_id=user_id,
                        text=chunk,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                except Exception:
                    try:
                        await self.app.bot.send_message(
                            chat_id=user_id,
                            text=chunk,
                            reply_markup=markup,
                        )
                    except Exception as e:
                        logger.error(f"Errore broadcast a {user_id}: {e}")

    # -------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------

    def get_quick_keyboard(self) -> ReplyKeyboardMarkup:
        keyboard = [
            [KeyboardButton("📊 Status"), KeyboardButton("📈 Usage")],
            [KeyboardButton("🤖 Model"), KeyboardButton("✍️ Auto-Edit")],
            [KeyboardButton("🆕 New"), KeyboardButton("🛑 Abort")],
            [KeyboardButton("ℹ️ Help")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        mode_desc = "🖥️ *Tmux Mirror Mode (1-to-1 Live Console)*" if self.is_tmux_mode else "🤖 *Stand-alone Driver Mode*"
        current_m = get_current_model()
        current_exec_mode = get_current_mode()
        msg = (
            "🚀 *Antigravity Mobile Cockpit (agy-telegram)*\n\n"
            f"• *Mode:* {mode_desc}\n"
            f"• *Active Model:* `{current_m}`\n"
            f"• *Execution Mode:* `{current_exec_mode}`\n"
            f"• *Target Terminal:* `{self.config.mirror.target_session}`\n\n"
            "Quick Commands:\n"
            "• `/model` - Visualizza e cambia il modello attivo\n"
            "• `/autoedit` - Attiva/disattiva approvazione automatica file edits\n"
            "• `/mode` - Seleziona modalità operativa (accept-edits, default, plan)\n"
            "• `/usage` - Statistiche token contesto e quota rimanente\n"
            "• `/new` - Inizializza una nuova sessione pulita\n"
            "• `/sessions` - Elenco e ripristino sessioni recenti\n"
            "• `/abort` - Invia segnale di interruzione Ctrl+C\n"
            "• `/status` - Diagnostica risorse host e sistema\n"
            "• `/help` - Manuale operativo\n\n"
            "💬 *Tutti i messaggi inviati vengono digitati direttamente nella console attiva!*"
        )
        await self.reply_safe(update, msg, reply_markup=self.get_quick_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        msg = (
            "📖 *agy-telegram Operational Manual*\n\n"
            "🔹 *Messaggi di testo*: Inviati istantaneamente nel prompt della console attiva.\n"
            "🔹 *Approvazione comandi*: Quando la CLI chiede conferma, compaiono i bottoni ✅ Approva e ❌ Rifiuta.\n"
            "🔹 `/model`: Visualizza o seleziona il modello LLM (Gemini 3.8/3.6, Claude Sonnet/Opus, ecc.).\n"
            "🔹 `/autoedit`: Abilita l'approvazione automatica delle modifiche ai file (action edit auto-approved).\n"
            "🔹 `/mode`: Cambia modalità tra Auto-Edit (`accept-edits`), Standard (`default`) o Planning (`plan`).\n"
            "🔹 `/usage`: Visualizza il conteggio token usati, la capienza contesto e i token rimasti.\n"
            "🔹 `/new`: Avvia una nuova sessione azzerando il contesto precedente.\n"
            "🔹 `/sessions`: Mostra le ultime conversazioni archiviate per riagganciarle.\n"
            "🔹 `/status`: Esegue la diagnostica risorse host.\n"
            "🔹 `/abort`: Invia una combinazione di interruzione (`Ctrl+C`) alla sessione terminale."
        )
        await self.reply_safe(update, msg)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Avvia una nuova sessione pulita."""
        if not self.is_authorized(update):
            return

        user_id = update.effective_user.id
        turn_ctx = self.active_turns.get(user_id)
        if turn_ctx:
            turn_ctx.is_completed = True
            turn_ctx.is_prompt_active = False
            if turn_ctx.status_msg:
                try:
                    await turn_ctx.status_msg.delete()
                except Exception:
                    pass
            self.active_turns.pop(user_id, None)

        if self.is_tmux_mode:
            await self.tmux_mirror.stop_turn_monitoring()
            await self.tmux_mirror.send_input("/new", press_enter=True)
        else:
            self.session_mgr.set_active_session(None)

        msg = (
            "🆕 <b>Nuova Sessione Inizializzata!</b>\n\n"
            "La conversazione precedente è stata archiviata.\n"
            "Il contesto è ora azzerato e pronto per un nuovo task.\n\n"
            "<i>Puoi inviare la tua nuova istruzione adesso.</i>"
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def cmd_autoedit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Attiva o disattiva la modalità autoedit (accept-edits) per le modifiche ai file."""
        if not self.is_authorized(update):
            return

        current = get_current_mode()
        new_mode = "default" if current == "accept-edits" else "accept-edits"
        set_current_mode(new_mode)

        if self.is_tmux_mode:
            await self.tmux_mirror.send_input(f"/mode {new_mode}", press_enter=True)

        if new_mode == "accept-edits":
            msg = (
                "✍️ <b>Auto-Edit ABILITATO (accept-edits)!</b>\n\n"
                "• Tutte le azioni di scrittura e modifica file (<code>replace_file_content</code>, <code>write_to_file</code>) "
                "saranno <b>approvate automaticamente</b>.\n"
                "• I comandi shell di sistema richiederanno comunque la tua approvazione manuale (Human-in-the-loop) per sicurezza."
            )
        else:
            msg = (
                "🛡️ <b>Modalità Standard Ripristinata (default)</b>\n\n"
                "• Tutte le azioni (sia modifiche file che comandi shell) richiederanno conferma manuale."
            )

        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Visualizza e permette di cambiare la modalità operativa (accept-edits, default, plan)."""
        if not self.is_authorized(update):
            return

        if context.args and len(context.args) > 0:
            target = context.args[0].lower().strip()
            valid_slugs = [slug for slug, _, _ in AVAILABLE_MODES]
            if target in valid_slugs:
                set_current_mode(target)
                if self.is_tmux_mode:
                    await self.tmux_mirror.send_input(f"/mode {target}", press_enter=True)
                await self.reply_safe(update, f"✅ *Modalità impostata su:* `{target}`")
                return
            else:
                await self.reply_safe(update, f"⚠️ Modalità non valida `{target}`. Opzioni disponibili: `{', '.join(valid_slugs)}`")
                return

        curr = get_current_mode()
        buttons = []
        for slug, label, desc in AVAILABLE_MODES:
            prefix = "👉 " if slug == curr else "🔘 "
            buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_mode:{slug}")])

        markup = InlineKeyboardMarkup(buttons)
        text = (
            "⚙️ <b>Selezione Modalità Esecuzione (Agent Mode)</b>\n\n"
            f"Modalità attuale: <b>{html.escape(curr)}</b>\n\n"
            "• <b>Auto-Edit:</b> modifiche file automatiche, conferma solo comandi shell.\n"
            "• <b>Standard:</b> conferma richiesta sia per file che per comandi.\n"
            "• <b>Plan Only:</b> sola ricerca e pianificazione senza modifiche.\n\n"
            "<i>Tocca un'opzione per attivarla:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le sessioni recenti e consente di riagganciarne una."""
        if not self.is_authorized(update):
            return

        sessions = self.session_mgr.list_recent_sessions(limit=6)
        if not sessions:
            await self.reply_safe(update, "ℹ️ Nessuna sessione archiviata trovata nella cronologia.")
            return

        buttons = []
        for s in sessions:
            cid = s["id"][:8]
            prev = s.get("preview") or "Sessione senza titolo"
            dt = s.get("timestamp") or ""
            btn_text = f"🔄 {cid} ({dt}) - {prev[:25]}..."
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"resume:{s['id']}")])

        markup = InlineKeyboardMarkup(buttons)
        text = (
            "🗂️ <b>Sessioni Recenti Antigravity</b>\n\n"
            "<i>Tocca una sessione per riprenderla nella console attiva:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        user_id = update.effective_user.id
        turn_ctx = self.active_turns.get(user_id)
        if turn_ctx:
            turn_ctx.is_completed = True
            turn_ctx.is_prompt_active = False

        if self.is_tmux_mode:
            await self.tmux_mirror.stop_turn_monitoring()
            await self.tmux_mirror.send_raw_key("C-c")
            await self.reply_safe(update, "🛑 Inviato `Ctrl+C` alla sessione terminale.")
        else:
            self.driver.abort_current_task()
            await self.reply_safe(update, "🛑 Interruzione inviata all'agente.")

        if turn_ctx and turn_ctx.status_msg:
            try:
                await turn_ctx.status_msg.delete()
            except Exception:
                pass
            turn_ctx.status_msg = None

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esegue diagnostica di sistema universale o personalizzata."""
        if not self.is_authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)

        # Se l'utente ha configurato status_command usa quello, altrimenti usa comandi POSIX universali
        status_cmd = self.config.agent.status_command or (
            "echo '=== Host Status & Resources ===' && "
            "uptime && echo '' && free -h 2>/dev/null || free && "
            "echo '' && df -h / 2>/dev/null || df -h"
        )

        proc = await asyncio.create_subprocess_shell(
            status_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode("utf-8", errors="replace").strip()
        await self.reply_safe(update, f"📊 *System Status*\n```text\n{out}\n```")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Visualizza il modello corrente o lo imposta direttamente se fornito come argomento."""
        if not self.is_authorized(update):
            return

        current_model = get_current_model()

        if context.args and len(context.args) > 0:
            query_arg = " ".join(context.args).lower().strip()
            target_model = None
            for slug, display in AVAILABLE_MODELS:
                if query_arg in slug.lower() or query_arg in display.lower():
                    target_model = display
                    break

            if target_model:
                set_current_model(target_model)
                await self.reply_safe(update, f"✅ *Modello aggiornato con successo!*\nNuovo modello attivo: `{target_model}`")
                return
            else:
                await self.reply_safe(
                    update,
                    f"⚠️ Modello non trovato per `{query_arg}`.\nUsa `/model` senza argomenti per visualizzare i modelli disponibili.",
                )
                return

        buttons = []
        for slug, display in AVAILABLE_MODELS:
            is_active = (display == current_model)
            prefix = "🔘 " if not is_active else "👉 "
            btn_text = f"{prefix}{display}"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"set_model:{display}")])

        markup = InlineKeyboardMarkup(buttons)
        text = (
            "🤖 <b>Selezione Modello Antigravity</b>\n\n"
            f"Modello attualmente attivo:\n<b>{html.escape(current_model)}</b>\n\n"
            "<i>Tocca un modello per attivarlo istantaneamente:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Calcola e visualizza i token utilizzati, la capienza del contesto e i token rimasti."""
        if not self.is_authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)

        current_model = get_current_model()
        current_mode = get_current_mode()
        latest_transcript = self.transcript_watcher.get_latest_transcript_path()
        conv_id = latest_transcript.parent.parent.name if latest_transcript else "N/D"

        stats = compute_session_usage(latest_transcript, current_model)
        msg = format_usage_html(stats, conv_id, current_mode)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    # -------------------------------------------------------------
    # Inline Callback Query Handler
    # -------------------------------------------------------------

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not query.data or not update.effective_user:
            return

        user_id = update.effective_user.id

        # 1. Selezione Modello
        if query.data.startswith("set_model:"):
            chosen_model = query.data.split("set_model:", 1)[1]
            success = set_current_model(chosen_model)
            if success:
                try:
                    await query.edit_message_text(
                        f"✅ <b>Modello aggiornato con successo!</b>\n"
                        f"Attivo: <code>{html.escape(chosen_model)}</code>\n\n"
                        f"<i>Le prossime richieste utilizzeranno questo modello.</i>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            else:
                await query.edit_message_text("❌ <i>Errore durante l'aggiornamento del modello.</i>", parse_mode=ParseMode.HTML)
            return

        # 2. Selezione Modalità (Auto-Edit, Default, Plan)
        if query.data.startswith("set_mode:"):
            chosen_mode = query.data.split("set_mode:", 1)[1]
            success = set_current_mode(chosen_mode)
            if success:
                if self.is_tmux_mode:
                    await self.tmux_mirror.send_input(f"/mode {chosen_mode}", press_enter=True)
                try:
                    await query.edit_message_text(
                        f"✅ <b>Modalità impostata su:</b> <code>{html.escape(chosen_mode)}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            else:
                await query.edit_message_text("❌ <i>Errore durante l'aggiornamento della modalità.</i>", parse_mode=ParseMode.HTML)
            return

        # 3. Ripristino Sessione (Resume)
        if query.data.startswith("resume:"):
            conv_id = query.data.split(":", 1)[1]
            if self.is_tmux_mode:
                await self.tmux_mirror.send_input(f"/resume {conv_id}", press_enter=True)
            else:
                self.session_mgr.set_active_session(conv_id)
            try:
                await query.edit_message_text(
                    f"✅ <b>Sessione riagganciata:</b> <code>{html.escape(conv_id)}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        # 4. Approvazione / Rifiuto comandi
        if query.data.startswith("tmux_key:"):
            parts = query.data.split(":")
            key = parts[1]

            turn_ctx = self.active_turns.get(user_id)
            if turn_ctx:
                turn_ctx.is_prompt_active = False

            await self.tmux_mirror.send_raw_key(key)
            await self.tmux_mirror.send_raw_key("Enter")

            action_name = "✅ Approvato" if key == "1" else "❌ Rifiutato"
            try:
                await query.edit_message_text(
                    f"⚡ <b>Comando {action_name}</b> (Inviato alla console)\n💭 <i>Elaborazione in corso...</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    # -------------------------------------------------------------
    # Conversational Message Handler
    # -------------------------------------------------------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return

        user_text = update.effective_message.text
        if not user_text:
            return

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        cleaned_cmd = user_text.strip()
        if "Status" in cleaned_cmd:
            await self.cmd_status(update, context)
            return
        elif "Usage" in cleaned_cmd:
            await self.cmd_usage(update, context)
            return
        elif "Model" in cleaned_cmd:
            await self.cmd_model(update, context)
            return
        elif "Auto-Edit" in cleaned_cmd or "Autoedit" in cleaned_cmd:
            await self.cmd_autoedit(update, context)
            return
        elif "New" in cleaned_cmd:
            await self.cmd_new(update, context)
            return
        elif "Sessions" in cleaned_cmd:
            await self.cmd_sessions(update, context)
            return
        elif "Abort" in cleaned_cmd:
            await self.cmd_abort(update, context)
            return
        elif "Help" in cleaned_cmd:
            await self.cmd_help(update, context)
            return

        self.mirror_log.log("USER", user_text)

        # Pulizia del turno precedente
        prev_turn = self.active_turns.get(user_id)
        if prev_turn and not prev_turn.is_completed:
            prev_turn.is_completed = True
            if prev_turn.status_msg:
                try:
                    await prev_turn.status_msg.delete()
                except Exception:
                    pass

        turn_ctx = TurnContext(
            turn_id=uuid.uuid4().hex[:8],
            user_id=user_id,
            chat_id=chat_id,
        )
        self.active_turns[user_id] = turn_ctx

        if self.is_tmux_mode:
            # Controllo esistenza della sessione Tmux
            if not await self.tmux_mirror.check_session_exists():
                turn_ctx.is_completed = True
                self.active_turns.pop(user_id, None)
                target = self.tmux_mirror.target
                sess_name = target.split(":")[0] if ":" in target else target
                err_msg = (
                    f"⚠️ <b>Sessione Tmux non trovata!</b>\n\n"
                    f"Target configurato: <code>{html.escape(target)}</code>\n\n"
                    f"<b>Come procedere:</b>\n"
                    f"1. Avvia una sessione tmux sul server con:\n"
                    f"   <code>tmux new -s {html.escape(sess_name)} agy</code>\n"
                    f"2. Oppure imposta <code>mode = 'driver'</code> nel tuo <code>config.toml</code> per eseguire senza tmux."
                )
                await update.effective_message.reply_text(err_msg, parse_mode=ParseMode.HTML)
                return

            latest_transcript = self.transcript_watcher.get_latest_transcript_path()
            start_offset = 0
            if latest_transcript and latest_transcript.is_file():
                start_offset = self.transcript_watcher.get_current_offset(latest_transcript)

            await update.effective_chat.send_action(ChatAction.TYPING)
            await self.tmux_mirror.send_input(user_text, press_enter=True)

            status_msg = await update.effective_message.reply_text(
                "💭 <b>Elaborazione in corso...</b>",
                parse_mode=ParseMode.HTML,
            )
            turn_ctx.status_msg = status_msg

            async def on_status_update(status_text: str):
                if turn_ctx.is_completed or turn_ctx.is_prompt_active:
                    return
                if turn_ctx.status_msg:
                    try:
                        await turn_ctx.status_msg.edit_text(status_text, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass

            async def on_turn_prompt(cmd_requested: str, options: List[Tuple[str, str]]):
                if turn_ctx.is_completed:
                    return
                turn_ctx.is_prompt_active = True
                turn_ctx.active_prompt_cmd = cmd_requested

                buttons = [
                    [
                        InlineKeyboardButton("✅ Approva", callback_data=f"tmux_key:1:{turn_ctx.turn_id}"),
                        InlineKeyboardButton("❌ Rifiuta", callback_data=f"tmux_key:4:{turn_ctx.turn_id}"),
                    ]
                ]
                markup = InlineKeyboardMarkup(buttons)
                clean_cmd = html.escape(cmd_requested)
                prompt_html = (
                    "⚠️ <b>Richiesta Autorizzazione Comando</b>\n"
                    f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>"
                )

                if turn_ctx.status_msg:
                    try:
                        await turn_ctx.status_msg.edit_text(
                            prompt_html,
                            parse_mode=ParseMode.HTML,
                            reply_markup=markup,
                        )
                        return
                    except Exception:
                        pass

                try:
                    new_msg = await update.effective_chat.send_message(
                        prompt_html,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                    turn_ctx.prompt_msg = new_msg
                except Exception as ex:
                    logger.error(f"Errore invio prompt autorizzazione: {ex}")

            async def on_final_response(final_text: str):
                turn_ctx.is_completed = True
                turn_ctx.is_prompt_active = False

                await self.tmux_mirror.stop_turn_monitoring()

                if turn_ctx.status_msg:
                    try:
                        await turn_ctx.status_msg.delete()
                    except Exception:
                        pass
                    turn_ctx.status_msg = None

                self.mirror_log.log("AGY", final_text)
                await self.reply_safe(update, final_text)
                self.active_turns.pop(user_id, None)

            await self.tmux_mirror.start_turn_monitoring(on_prompt=on_turn_prompt)

            if latest_transcript:
                asyncio.create_task(
                    self.transcript_watcher.watch_turn(
                        transcript_path=latest_transcript,
                        start_offset=start_offset,
                        on_status=on_status_update,
                        on_final=on_final_response,
                    )
                )
        else:
            await update.effective_chat.send_action(ChatAction.TYPING)
            async with self.lock:
                stop_typing = False

                async def keep_typing():
                    while not stop_typing:
                        try:
                            await update.effective_chat.send_action(ChatAction.TYPING)
                        except Exception:
                            pass
                        await asyncio.sleep(4)

                typing_task = asyncio.create_task(keep_typing())
                try:
                    code, stdout, stderr = await self.driver.execute_prompt(
                        prompt=user_text,
                        conversation_id=self.session_mgr.get_active_session(),
                        workspace=self.config.agent.default_workspace,
                        model=self.config.agent.default_model,
                    )
                    stop_typing = True
                    typing_task.cancel()

                    response = stdout if stdout else (stderr or "✅ *(Nessun output)*")
                    self.mirror_log.log("AGY", response)
                    await self.reply_safe(update, response)
                except Exception as e:
                    stop_typing = True
                    typing_task.cancel()
                    await self.reply_safe(update, f"⚠️ Errore interno: `{e}`")
                finally:
                    turn_ctx.is_completed = True
                    self.active_turns.pop(user_id, None)

    # -------------------------------------------------------------
    # Proactive Sentinel Alert Handler
    # -------------------------------------------------------------

    async def handle_sentinel_alert(self, payload: dict):
        title = payload.get("title", "Allerta Sentinel")
        msg = payload.get("message", "")
        lvl = payload.get("level", "info")

        emoji = "ℹ️"
        if lvl == "warning":
            emoji = "⚠️"
        elif lvl == "alert":
            emoji = "🚨"

        formatted = f"{emoji} *{title}*\n\n{msg}"
        await self.broadcast_to_users(formatted)

    # -------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------

    async def run(self):
        logger.info("Inizializzazione agy-telegram bot (v2.0)...")
        self.app = Application.builder().token(self.config.telegram.bot_token).build()

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("model", self.cmd_model))
        self.app.add_handler(CommandHandler("usage", self.cmd_usage))
        self.app.add_handler(CommandHandler("new", self.cmd_new))
        self.app.add_handler(CommandHandler("autoedit", self.cmd_autoedit))
        self.app.add_handler(CommandHandler("mode", self.cmd_mode))
        self.app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        self.app.add_handler(CommandHandler("abort", self.cmd_abort))
        self.app.add_handler(CommandHandler("status", self.cmd_status))

        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        if self.config.sentinel.enabled:
            self.sentinel.register_callback(self.handle_sentinel_alert)
            await self.sentinel.start()

        if self.is_tmux_mode:
            await self.tmux_mirror.start_monitor()

        logger.info("Avvio polling Telegram...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            logger.info("Arresto agy-telegram...")
            if self.is_tmux_mode:
                await self.tmux_mirror.stop_monitor()
            await self.sentinel.stop()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
