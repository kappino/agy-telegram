"""
Main Telegram Bot Application wiring all modules together.
Supports both Direct Driver Mode and Live Bidirectional Tmux Mirror Mode.
"""

import asyncio
import html
import logging
from typing import Optional, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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

logger = logging.getLogger("agy_telegram.bot")

class AgyTelegramBot:
    def __init__(self, config: AppConfig):
        self.config = config
        self.driver = AgyDriver(
            executable=config.agent.executable,
            default_workspace=config.agent.default_workspace,
            default_model=config.agent.default_model,
            default_effort=config.agent.default_effort,
            timeout=config.agent.timeout_seconds,
        )
        self.session_mgr = SessionManager()
        self.sentinel = SentinelServer(socket_path=config.sentinel.socket_path)
        self.mirror_log = TerminalMirror(log_file=config.mirror.log_file)
        self.tmux_mirror = TmuxMirror(target_session=config.mirror.target_session)
        self.is_tmux_mode = (config.mirror.mode == "tmux")
        self.transcript_watcher = TranscriptWatcher()
        self.lock = asyncio.Lock()
        self.app: Optional[Application] = None

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
    # Tmux Callbacks (Live Output -> Telegram)
    # -------------------------------------------------------------

    async def on_tmux_output(self, text: str):
        """Invocato quando la sessione agy in tmux produce nuovo testo (registra nel log per diagnostica)."""
        self.mirror_log.log("TMUX_AGY", text)

    async def on_tmux_prompt(self, cmd_requested: str, prompt_sig: str):
        """Invocato quando agy chiede autorizzazione (mostra pulsanti di approvazione per il comando specifico)."""
        logger.info(f"Rilevata richiesta autorizzazione per: {cmd_requested}")
        buttons = [
            [
                InlineKeyboardButton("✅ Approva", callback_data="tmux_key:1"),
                InlineKeyboardButton("❌ Rifiuta", callback_data="tmux_key:4"),
            ]
        ]
        markup = InlineKeyboardMarkup(buttons)
        clean_cmd = html.escape(cmd_requested)
        prompt_html = (
            "⚠️ <b>Richiesta Autorizzazione Comando</b>\n"
            f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>"
        )

        self.is_prompt_active = True

        # Se abbiamo un messaggio di stato attivo in chat, aggiorniamo DIRETTAMENTE quello
        if hasattr(self, "current_status_msg") and self.current_status_msg:
            try:
                await self.current_status_msg.edit_text(
                    prompt_html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return
            except Exception as e:
                logger.warning(f"Impossibile modificare il messaggio di stato per il prompt: {e}")

        # Fallback: invia a tutti gli utenti autorizzati e memorizza il messaggio di stato
        if self.app:
            for user_id in self.config.telegram.allowed_users:
                try:
                    msg = await self.app.bot.send_message(
                        chat_id=user_id,
                        text=prompt_html,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                    self.current_status_msg = msg
                except Exception as e:
                    logger.error(f"Errore invio prompt autorizzazione a {user_id}: {e}")

    # -------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------

    def get_quick_keyboard(self) -> ReplyKeyboardMarkup:
        keyboard = [
            [KeyboardButton("📊 Status"), KeyboardButton("🛑 Abort"), KeyboardButton("ℹ️ Help")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        mode_desc = "🖥️ *Tmux Mirror Mode (1-to-1 Live Console)*" if self.is_tmux_mode else "🤖 *Stand-alone Driver Mode*"
        msg = (
            "🚀 *Antigravity Mobile Cockpit (agy-telegram)*\n\n"
            f"• *Mode:* {mode_desc}\n"
            f"• *Target Terminal:* `{self.config.mirror.target_session}`\n\n"
            "Quick Commands:\n"
            "• `/abort` - Send Ctrl+C interrupt to terminal\n"
            "• `/status` - Diagnostic host resources and system health\n"
            "• `/help` - Operational manual & shortcuts\n\n"
            "💬 *Any message you send is typed directly into your active agent console!*"
        )
        await self.reply_safe(update, msg, reply_markup=self.get_quick_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        msg = (
            "📖 *Manuale Operativo agy-telegram (Tmux Mirror)*\n\n"
            "🔹 *Messaggi di testo*: Vengono scritti direttamente nel prompt dell'agente in terminale e inviati con Invio.\n"
            "🔹 *Pulsanti di approvazione*: Se agy richiede permessi nel terminale, compaiono i bottoni ✅ / ❌ direttamente qui su Telegram.\n"
            "🔹 `/abort`: Invia una combinazione di interruzione (`Ctrl+C`) alla sessione terminale.\n"
            "🔹 `/status`: Esegue la diagnostica hypervisor e risorse."
        )
        await self.reply_safe(update, msg)

    async def cmd_abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        if self.is_tmux_mode:
            await self.tmux_mirror.send_raw_key("C-c")
            await self.reply_safe(update, "🛑 Inviato `Ctrl+C` alla sessione terminale.")
        else:
            self.driver.abort_current_task()
            await self.reply_safe(update, "🛑 Interruzione inviata all'agente.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        status_cmd = "aegis-ops status 2>/dev/null || (echo '--- System Resources ---' && uptime && free -h && df -h /)"
        proc = await asyncio.create_subprocess_shell(
            status_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode("utf-8", errors="replace").strip()
        await self.reply_safe(update, f"📊 *System Status*\n```text\n{out}\n```")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not query.data:
            return

        if query.data.startswith("tmux_key:"):
            key = query.data.split(":", 1)[1]
            self.is_prompt_active = False
            await self.tmux_mirror.send_raw_key(key)
            await self.tmux_mirror.send_raw_key("Enter")
            action_name = "Approva" if key == "1" else "Rifiuta"
            try:
                await query.edit_message_text(
                    f"⚡ <b>Azione inviata: {action_name}</b> (Invio alla console...)\n💭 <i>Elaborazione in corso...</i>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        elif query.data.startswith("resume:"):
            conv_id = query.data.split(":", 1)[1]
            self.session_mgr.set_active_session(conv_id)
            await query.edit_message_text(f"✅ Sessione agganciata: <code>{conv_id}</code>", parse_mode=ParseMode.HTML)

    # -------------------------------------------------------------
    # Conversational Message Handler
    # -------------------------------------------------------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return

        user_text = update.effective_message.text
        if not user_text:
            return

        # Route quick keyboard actions
        cleaned_cmd = user_text.strip()
        if "Status" in cleaned_cmd:
            await self.cmd_status(update, context)
            return
        elif "Abort" in cleaned_cmd:
            await self.cmd_abort(update, context)
            return
        elif "Help" in cleaned_cmd:
            await self.cmd_help(update, context)
            return

        self.mirror_log.log("USER", user_text)

        if self.is_tmux_mode:
            # Cattura punto di partenza del transcript prima di inviare il comando
            latest_transcript = self.transcript_watcher.get_latest_transcript_path()
            start_line = 0
            if latest_transcript and latest_transcript.is_file():
                try:
                    with open(latest_transcript, "r", encoding="utf-8") as f:
                        start_line = len(f.readlines())
                except Exception:
                    pass

            await update.effective_chat.send_action(ChatAction.TYPING)
            # Invia il comando al terminale tmux
            await self.tmux_mirror.send_input(user_text, press_enter=True)

            # Invia subito un messaggio effimero di stato in chat
            status_msg = await update.effective_message.reply_text(
                "💭 <b>Elaborazione in corso...</b>",
                parse_mode=ParseMode.HTML,
            )
            self.current_status_msg = status_msg

            async def on_status_update(status_text: str):
                # Se è attivo un prompt di autorizzazione con bottoni, non sovrascriverlo con lo status
                if getattr(self, "is_prompt_active", False):
                    return
                try:
                    await status_msg.edit_text(status_text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

            async def on_final_response(final_text: str):
                self.is_prompt_active = False
                self.current_status_msg = None
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                self.mirror_log.log("AGY", final_text)
                await self.reply_safe(update, final_text)

            if latest_transcript:
                asyncio.create_task(
                    self.transcript_watcher.watch_turn(
                        transcript_path=latest_transcript,
                        start_line=start_line,
                        on_status=on_status_update,
                        on_final=on_final_response,
                    )
                )
        else:
            # Modalità driver autonomo
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
        logger.info("Inizializzazione agy-telegram bot...")
        self.app = Application.builder().token(self.config.telegram.bot_token).build()

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("abort", self.cmd_abort))
        self.app.add_handler(CommandHandler("status", self.cmd_status))

        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        if self.config.sentinel.enabled:
            self.sentinel.register_callback(self.handle_sentinel_alert)
            await self.sentinel.start()

        if self.is_tmux_mode:
            self.tmux_mirror.register_callbacks(
                on_output=self.on_tmux_output,
                on_prompt=self.on_tmux_prompt,
            )
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
