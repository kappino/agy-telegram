"""
Main Telegram Bot Application wiring all modules together (v2.0 - Production Grade).
Features:
- Isolated per-turn state management (TurnContext) to prevent race conditions & orphaned messages
- Deterministic, solid human-in-the-loop permission buttons (Approve / Reject)
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
import shlex
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
from agy_telegram.core.approval import ApprovalManager, Decision, PendingApproval
from agy_telegram.core.diff_preview import (
    generate_diff_for_tool,
    format_diff_for_telegram,
    extract_latest_tool_call_from_transcript,
)
from agy_telegram.core.media_handler import MediaHandler

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
    watcher_task: Optional[asyncio.Task] = None


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
        self.approval_mgr = ApprovalManager(
            auto_patterns=config.approval.auto_approve_patterns,
            hard_deny_patterns=config.approval.hard_deny_patterns,
            timeout_seconds=config.approval.timeout_seconds,
            fallback_action=config.approval.fallback_action,
        )
        self.media_handler = MediaHandler(
            config=config.media,
            workspace=Path(config.agent.default_workspace),
        )
        self.lock = asyncio.Lock()
        self.user_locks: Dict[int, asyncio.Lock] = {}
        self.app: Optional[Application] = None

        # State isolation per user / turn
        self.active_turns: Dict[int, TurnContext] = {}


    def get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Returns the serialization lock for a given user."""
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        return self.user_locks[user_id]

    def is_authorized(self, update: Update) -> bool:
        if not update.effective_chat or update.effective_chat.type != "private":
            logger.warning(
                f"Interaction rejected: non-private chat type '{getattr(update.effective_chat, 'type', None)}'"
            )
            return False
        if not update.effective_user:
            return False
        user_id = update.effective_user.id
        if user_id not in self.config.telegram.allowed_users:
            logger.warning(f"Access denied to unauthorized user: {user_id}")
            return False
        return True

    async def reply_safe(self, update: Update, text: str, reply_markup=None, is_html: bool = False):
        html_text = text if is_html else markdown_to_telegram_html(text)
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
                logger.warning(f"HTML send error ({e}), falling back to plain text.")
                await update.effective_message.reply_text(chunk, reply_markup=markup)

    async def broadcast_to_users(self, text: str, reply_markup=None):
        """Sends a message to all authorized users."""
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
                        logger.error(f"Broadcast error to {user_id}: {e}")

    # -------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------

    def get_quick_keyboard(self) -> ReplyKeyboardMarkup:
        keyboard = [
            [KeyboardButton("📊 Status"), KeyboardButton("📈 Usage")],
            [KeyboardButton("🤖 Model"), KeyboardButton("✍️ Auto-Edit")],
            [KeyboardButton("🆕 New"), KeyboardButton("🔄 Resume")],
            [KeyboardButton("🛑 Abort"), KeyboardButton("ℹ️ Help")],
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
            "• `/model` - View and switch the active model\n"
            "• `/autoedit` - Toggle automatic approval for file edits\n"
            "• `/mode` - Select execution mode (accept-edits, default, plan)\n"
            "• `/usage` - Context token usage and remaining quota\n"
            "• `/new` - Reset session with clean context\n"
            "• `/resume` - List and resume conversations by title\n"
            "• `/abort` - Send Ctrl+C interrupt signal\n"
            "• `/status` - Host and resource diagnostics\n"
            "• `/help` - Operation manual\n\n"
            "💬 *Any plain text message will be forwarded directly into the active console.*"
        )
        await self.reply_safe(update, msg, reply_markup=self.get_quick_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        msg = (
            "📖 *agy-telegram Operational Manual*\n\n"
            "🔹 *Text messages*: Injected instantly into active console prompt.\n"
            "🔹 *Command approval*: When the CLI requests confirmation, inline Approve/Reject buttons appear.\n"
            "🔹 `/model`: View or switch LLM model dynamically.\n"
            "🔹 `/autoedit`: Toggle auto-approval for file edits (`accept-edits`).\n"
            "🔹 `/mode`: Switch between Auto-Edit (`accept-edits`), Standard (`default`), or Planning (`plan`).\n"
            "🔹 `/usage`: View used tokens, remaining context, and step counts.\n"
            "🔹 `/new`: Start a fresh session clearing previous context.\n"
            "🔹 `/resume`: List previous sessions by title and restore context.\n"
            "🔹 `/status`: Run host resource diagnostics.\n"
            "🔹 `/abort`: Send `Ctrl+C` interrupt to active terminal."
        )
        await self.reply_safe(update, msg)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Starts a clean session."""
        if not self.is_authorized(update):
            return

        user_id = update.effective_user.id
        turn_ctx = self.active_turns.get(user_id)
        if turn_ctx:
            self.approval_mgr.resolve(turn_ctx.turn_id)
            turn_ctx.is_completed = True
            turn_ctx.is_prompt_active = False
            if turn_ctx.watcher_task and not turn_ctx.watcher_task.done():
                turn_ctx.watcher_task.cancel()
            if turn_ctx.status_msg:

                try:
                    await turn_ctx.status_msg.delete()
                except Exception:
                    pass
            self.active_turns.pop(user_id, None)

        self.session_mgr.set_active_session(None)
        if self.is_tmux_mode:
            await self.tmux_mirror.stop_turn_monitoring()
            await self.tmux_mirror.send_input("/new", press_enter=True)

        msg = (
            "🆕 <b>New Session Initialized</b>\n\n"
            "Previous context has been archived and reset.\n\n"
            "<i>You can send your new instruction now.</i>"
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def cmd_autoedit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggles autoedit (accept-edits) mode for file modifications."""
        if not self.is_authorized(update):
            return

        current = get_current_mode()
        new_mode = "default" if current == "accept-edits" else "accept-edits"
        success = set_current_mode(new_mode)
        if not success:
            await self.reply_safe(update, "❌ *Failed to update mode in settings.json.*")
            return

        if self.is_tmux_mode:
            await self.tmux_mirror.send_input(f"/mode {new_mode}", press_enter=True)

        if new_mode == "accept-edits":
            msg = (
                "✍️ <b>Auto-Edit ENABLED (accept-edits)</b>\n\n"
                "• File modifications (<code>replace_file_content</code>, <code>write_to_file</code>) "
                "will be <b>automatically approved</b>.\n"
                "• Shell commands still require manual human confirmation."
            )
        else:
            msg = (
                "🛡️ <b>Standard Mode Restored (default)</b>\n\n"
                "• All actions (file modifications and shell commands) require manual approval."
            )

        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays and switches execution mode (accept-edits, default, plan)."""
        if not self.is_authorized(update):
            return

        if context.args and len(context.args) > 0:
            target = context.args[0].lower().strip()
            valid_slugs = [slug for slug, _, _ in AVAILABLE_MODES]
            if target in valid_slugs:
                success = set_current_mode(target)
                if not success:
                    await self.reply_safe(update, "❌ *Failed to update mode in settings.json.*")
                    return
                if self.is_tmux_mode:
                    await self.tmux_mirror.send_input(f"/mode {target}", press_enter=True)
                await self.reply_safe(update, f"✅ *Mode set to:* `{target}`")
                return
            else:
                await self.reply_safe(update, f"⚠️ Invalid mode `{target}`. Available options: `{', '.join(valid_slugs)}`")
                return

        curr = get_current_mode()
        buttons = []
        for slug, label, desc in AVAILABLE_MODES:
            prefix = "👉 " if slug == curr else "🔘 "
            buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"set_mode:{slug}")])

        markup = InlineKeyboardMarkup(buttons)
        text = (
            "⚙️ <b>Select Execution Mode</b>\n\n"
            f"Current mode: <b>{html.escape(curr)}</b>\n\n"
            "• <b>Auto-Edit:</b> auto-approve file changes, prompt for shell commands.\n"
            "• <b>Standard:</b> confirm both file changes and commands.\n"
            "• <b>Plan Only:</b> read and plan only; no changes executed.\n\n"
            "<i>Tap an option to switch:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lists recent sessions by title and allows resuming them (like the CLI /resume)."""
        if not self.is_authorized(update):
            return

        sessions = await asyncio.to_thread(self.session_mgr.list_recent_sessions, limit=6)
        if not sessions:
            await self.reply_safe(update, "ℹ️ No archived sessions found.")
            return

        buttons = []
        for s in sessions:
            title_text = s.get("display_title") or s.get("title") or s.get("preview") or "Untitled session"
            if len(title_text) > 38:
                title_text = title_text[:35] + "..."
            dt = s.get("timestamp") or ""
            dt_badge = f" ({dt[-5:]})" if len(dt) >= 5 else ""
            btn_text = f"💬 {title_text}{dt_badge}"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"resume:{s['id']}")])

        markup = InlineKeyboardMarkup(buttons)
        text = (
            "🔄 <b>Resume Antigravity Session</b>\n\n"
            "<i>Select a previous conversation to restore context:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for /resume."""
        await self.cmd_resume(update, context)

    async def cmd_abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        user_id = update.effective_user.id
        turn_ctx = self.active_turns.get(user_id)
        if turn_ctx:
            self.approval_mgr.resolve(turn_ctx.turn_id)
            turn_ctx.is_completed = True
            turn_ctx.is_prompt_active = False
            if turn_ctx.watcher_task and not turn_ctx.watcher_task.done():
                turn_ctx.watcher_task.cancel()
            if turn_ctx.status_msg:

                try:
                    await turn_ctx.status_msg.delete()
                except Exception:
                    pass
            self.active_turns.pop(user_id, None)

        if self.is_tmux_mode:
            await self.tmux_mirror.stop_turn_monitoring()
            await self.tmux_mirror.send_raw_key("C-c")
            await self.reply_safe(update, "🛑 Sent `Ctrl+C` interrupt to terminal.")
        else:
            self.driver.abort_current_task()
            await self.reply_safe(update, "🛑 Interrupt signal sent to agent.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Executes safe universal or custom host diagnostics."""
        if not self.is_authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)

        allowed_status_binaries = {"uptime", "free", "df", "uname", "top", "systemctl", "vmstat", "iostat"}
        custom_cmd = self.config.agent.status_command

        if custom_cmd:
            tokens = shlex.split(custom_cmd.strip())
            bin_name = Path(tokens[0]).name if tokens else ""
            if bin_name not in allowed_status_binaries:
                logger.warning(f"cmd_status rejected: binary '{bin_name}' is not in allowlist")
                await self.reply_safe(
                    update,
                    f"⚠️ <b>Unauthorized status binary:</b> <code>{html.escape(bin_name)}</code>\n"
                    f"Allowed binaries: <code>{', '.join(sorted(allowed_status_binaries))}</code>",
                    is_html=True,
                )
                return
            proc = await asyncio.create_subprocess_exec(
                *tokens,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            # Default static and safe host diagnostic script
            proc = await asyncio.create_subprocess_exec(
                "sh",
                "-c",
                "echo '=== Host Status & Resources ===' && uptime && echo '' && free -h 2>/dev/null || free && echo '' && df -h / 2>/dev/null || df -h",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            out = stdout.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            proc.kill()
            out = "⚠️ Status command timed out (exceeded 10s)."

        await self.reply_safe(update, f"📊 *System Status*\n```text\n{out}\n```")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Displays current model or sets it directly if passed as argument."""
        if not self.is_authorized(update):
            return

        current_model = get_current_model()

        if context.args and len(context.args) > 0:
            query_arg = " ".join(context.args).lower().strip()
            target_model = None
            target_slug = None
            for slug, display in AVAILABLE_MODELS:
                if query_arg in slug.lower() or query_arg in display.lower():
                    target_model = display
                    target_slug = slug
                    break

            if target_model:
                success = set_current_model(target_model)
                if not success:
                    await self.reply_safe(update, "❌ *Failed to update model in settings.json.*")
                    return
                if self.is_tmux_mode and target_slug:
                    await self.tmux_mirror.send_input(f"/model {target_slug}", press_enter=True)
                await self.reply_safe(update, f"✅ *Model updated successfully.*\nActive: `{target_model}`")
                return
            else:
                await self.reply_safe(
                    update,
                    f"⚠️ Model not found matching `{query_arg}`.\nUse `/model` without arguments to see available options.",
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
            "🤖 <b>Antigravity Model Selection</b>\n\n"
            f"Currently active model:\n<b>{html.escape(current_model)}</b>\n\n"
            "<i>Tap a model to switch dynamically:</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reports token usage, context saturation, and turn metrics."""
        if not self.is_authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)

        current_model = get_current_model()
        current_mode = get_current_mode()
        latest_transcript = self.transcript_watcher.get_latest_transcript_path()
        conv_id = latest_transcript.parent.parent.name if latest_transcript else "N/A"

        stats = await asyncio.to_thread(compute_session_usage, latest_transcript, current_model)
        msg = format_usage_html(stats, conv_id, current_mode)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    # -------------------------------------------------------------
    # Inline Callback Query Handler
    # -------------------------------------------------------------

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return

        # Enforce callback authorization
        if not self.is_authorized(update):
            logger.warning(f"Callback rejected from unauthorized user: {update.effective_user.id}")
            try:
                await query.answer("⛔ Access denied.", show_alert=True)
            except Exception:
                pass
            return

        user_id = update.effective_user.id

        # 1. Model Selection
        if query.data.startswith("set_model:"):
            chosen_model = query.data.split("set_model:", 1)[1]
            success = set_current_model(chosen_model)
            if success:
                if self.is_tmux_mode:
                    target_slug = None
                    for slug, display in AVAILABLE_MODELS:
                        if display == chosen_model or slug == chosen_model:
                            target_slug = slug
                            break
                    if target_slug:
                        await self.tmux_mirror.send_input(f"/model {target_slug}", press_enter=True)
                try:
                    await query.edit_message_text(
                        f"✅ <b>Model updated successfully</b>\n"
                        f"Active: <code>{html.escape(chosen_model)}</code>\n\n"
                        f"<i>Subsequent prompts will use this model.</i>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                await query.answer("Model updated.")

            else:
                await query.edit_message_text("❌ <i>Failed to update model in settings.json.</i>", parse_mode=ParseMode.HTML)
                await query.answer("Error updating model.", show_alert=True)
            return

        # 2. Mode Selection (Auto-Edit, Default, Plan)
        if query.data.startswith("set_mode:"):
            chosen_mode = query.data.split("set_mode:", 1)[1]
            success = set_current_mode(chosen_mode)
            if success:
                if self.is_tmux_mode:
                    await self.tmux_mirror.send_input(f"/mode {chosen_mode}", press_enter=True)
                try:
                    await query.edit_message_text(
                        f"✅ <b>Mode set to:</b> <code>{html.escape(chosen_mode)}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                await query.answer("Mode updated.")
            else:
                await query.edit_message_text("❌ <i>Failed to update mode in settings.json.</i>", parse_mode=ParseMode.HTML)
                await query.answer("Error updating mode.", show_alert=True)
            return

        # 3. Resume Session
        if query.data.startswith("resume:"):
            conv_id = query.data.split(":", 1)[1]
            self.session_mgr.set_active_session(conv_id)
            if self.is_tmux_mode:
                await self.tmux_mirror.send_input(f"/resume {conv_id}", press_enter=True)

            # Retrieve last message from transcript
            last_interaction = await asyncio.to_thread(self.session_mgr.get_last_interaction, conv_id)
            last_msg_notice = ""
            if last_interaction:
                role, content = last_interaction
                role_icon = "🤖" if role == "PLANNER_RESPONSE" else "👤"
                role_label = "Agent" if role == "PLANNER_RESPONSE" else "User"

                formatted_content = markdown_to_telegram_html(content.strip())
                # Telegram blockquote expandable allows tapping to expand full message
                # Keep inside safe limits for inline query edit (3200 chars), or fallback if massive
                if len(formatted_content) > 3200:
                    formatted_content = formatted_content[:3180] + "..."

                last_msg_notice = (
                    f"\n\n<b>Last interaction ({role_icon} {role_label}):</b>\n"
                    f"<blockquote expandable>{formatted_content}</blockquote>"
                )

            resumed_html = (
                f"✅ <b>Session resumed:</b> <code>{html.escape(conv_id)}</code>"
                f"{last_msg_notice}\n\n"
                f"<i>Ready for new instructions.</i>"
            )

            try:
                await query.edit_message_text(
                    resumed_html,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                try:
                    await query.message.reply_text(resumed_html, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            await query.answer("Session resumed.")
            return

        # 4. Command Approval / Rejection (with turn_id validation)
        if query.data.startswith("tmux_key:"):
            parts = query.data.split(":")
            key = parts[1]
            target_turn_id = parts[2] if len(parts) > 2 else None

            # Cancel active timeout timer for this turn
            if target_turn_id:
                self.approval_mgr.resolve(target_turn_id)

            turn_ctx = self.active_turns.get(user_id)

            # Prevent stale button execution
            if not turn_ctx or (target_turn_id and turn_ctx.turn_id != target_turn_id):
                stale_text = "⏳ <i>This permission prompt has expired. Turn is no longer active.</i>"
                try:
                    # In python-telegram-bot, edit_message_text is the standard unless explicitly a document/photo message
                    await query.edit_message_text(stale_text, parse_mode=ParseMode.HTML)
                except Exception:
                    try:
                        await query.edit_message_caption(stale_text, parse_mode=ParseMode.HTML)
                    except Exception as e:
                        logger.debug(f"Failed to edit stale message: {e}")
                await query.answer("⏳ Request expired.", show_alert=True)
                return

            if not turn_ctx.is_prompt_active:
                await query.answer("ℹ️ No command awaiting confirmation.", show_alert=True)
                return

            turn_ctx.is_prompt_active = False

            await self.tmux_mirror.send_raw_key(key)
            await self.tmux_mirror.send_raw_key("Enter")

            action_name = "Approved" if key == "1" else "Rejected"
            action_text = f"⚡ <b>Command {action_name}</b> (Sent to console)\n💭 <i>Processing...</i>"
            try:
                await query.edit_message_text(action_text, parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    await query.edit_message_caption(action_text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            turn_ctx.status_msg = query.message
            turn_ctx.prompt_msg = None
            await query.answer(f"Command {action_name}.")


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

        # Exact lookup for quick keyboard actions
        quick_action_map = {
            "📊 Status": self.cmd_status,
            "📈 Usage": self.cmd_usage,
            "🤖 Model": self.cmd_model,
            "✍️ Auto-Edit": self.cmd_autoedit,
            "✍️ Autoedit": self.cmd_autoedit,
            "🆕 New": self.cmd_new,
            "🔄 Resume": self.cmd_resume,
            "🗂️ Sessions": self.cmd_sessions,
            "🛑 Abort": self.cmd_abort,
            "ℹ️ Help": self.cmd_help,
        }

        if cleaned_cmd in quick_action_map:
            await quick_action_map[cleaned_cmd](update, context)
            return

        await self._dispatch_turn(update, context, user_text)

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles incoming photos and documents (screenshots/images) from Telegram."""
        if not self.is_authorized(update):
            return

        msg = update.effective_message
        if not msg:
            return

        await update.effective_chat.send_action(ChatAction.TYPING)
        prompt_str, saved_path, err = await self.media_handler.process_media_message(msg)
        if err:
            await self.reply_safe(update, f"⚠️ <b>Media Error:</b> {html.escape(err)}", is_html=True)
            return

        if not prompt_str:
            return

        logger.info(f"Processed media message successfully, saved at {saved_path}. Dispatching turn.")
        await self._dispatch_turn(update, context, prompt_str)

    async def _dispatch_turn(self, update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_text: str):
        """Dispatches an interaction turn either via Tmux mirroring or direct AgyDriver."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        self.mirror_log.log("USER", prompt_text)

        # Per-user serialization to prevent race conditions on overlapping turns
        async with self.get_user_lock(user_id):
            prev_turn = self.active_turns.get(user_id)
            if prev_turn and prev_turn.is_prompt_active:
                await update.effective_message.reply_text(
                    "⚠️ <b>Pending Authorization Request</b>\n\n"
                    "A command is currently awaiting your explicit approval or rejection.\n"
                    "Please tap <b>Approve</b> or <b>Reject</b> above (or send <code>/abort</code>) before sending new instructions.",
                    parse_mode=ParseMode.HTML,
                )
                return

            # Explicit cleanup of previous turn and orphan watcher cancellation
            if prev_turn and not prev_turn.is_completed:
                prev_turn.is_completed = True
                self.approval_mgr.resolve(prev_turn.turn_id)
                if prev_turn.watcher_task and not prev_turn.watcher_task.done():
                    prev_turn.watcher_task.cancel()
                    logger.debug(f"Cancelled orphan watcher task from previous turn: {prev_turn.turn_id}")
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
                # Check if Tmux session exists
                if not await self.tmux_mirror.check_session_exists():
                    turn_ctx.is_completed = True
                    if self.active_turns.get(user_id) == turn_ctx:
                        self.active_turns.pop(user_id, None)
                    target = self.tmux_mirror.target
                    sess_name = target.split(":")[0] if ":" in target else target
                    err_msg = (
                        f"⚠️ <b>Tmux session not found!</b>\n\n"
                        f"Configured target: <code>{html.escape(target)}</code>\n\n"
                        f"<b>How to proceed:</b>\n"
                        f"1. Start a tmux session on the server with:\n"
                        f"   <code>tmux new -s {html.escape(sess_name)} agy</code>\n"
                        f"2. Or set <code>mode = 'driver'</code> in your <code>config.toml</code> to run without tmux."
                    )
                    await update.effective_message.reply_text(err_msg, parse_mode=ParseMode.HTML)
                    return

                active_conv = self.session_mgr.get_active_session()
                prev_transcript = self.transcript_watcher.get_latest_transcript_path(conv_id=active_conv)
                prev_offset = (
                    self.transcript_watcher.get_current_offset(prev_transcript)
                    if prev_transcript and prev_transcript.is_file()
                    else 0
                )

                await update.effective_chat.send_action(ChatAction.TYPING)
                await self.tmux_mirror.send_input(prompt_text, press_enter=True)

                latest_transcript, start_offset = await self.transcript_watcher.await_active_transcript_and_offset(
                    conv_id=active_conv,
                    prev_transcript_path=prev_transcript,
                    prev_offset=prev_offset,
                    timeout=6.0,
                )

                if latest_transcript:
                    try:
                        detected_conv_id = latest_transcript.parent.parent.name
                        self.session_mgr.set_active_session(detected_conv_id)
                    except Exception:
                        pass
                else:
                    turn_ctx.is_completed = True
                    await self.tmux_mirror.stop_turn_monitoring()
                    if self.active_turns.get(user_id) == turn_ctx:
                        self.active_turns.pop(user_id, None)
                    await update.effective_message.reply_text(
                        "⚠️ <b>Transcript log not found</b>\n\n"
                        "Unable to locate the active Antigravity session transcript after 6 seconds.\n"
                        "Ensure `agy` is running in your tmux session.",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                status_msg = await update.effective_message.reply_text(
                    "💭 <b>Processing...</b>",
                    parse_mode=ParseMode.HTML,
                )
                turn_ctx.status_msg = status_msg


                last_status_edit_time = 0.0

                async def on_status_update(status_text: str):
                    nonlocal last_status_edit_time
                    if turn_ctx.is_completed or turn_ctx.is_prompt_active:
                        return
                    # Throttle to 1.5s to avoid Telegram HTTP 429 rate limiting
                    now = time.monotonic()
                    if now - last_status_edit_time < 1.5:
                        return
                    if turn_ctx.status_msg:
                        try:
                            await turn_ctx.status_msg.edit_text(status_text, parse_mode=ParseMode.HTML)
                            last_status_edit_time = now
                            return
                        except Exception:
                            pass
                    try:
                        turn_ctx.status_msg = await update.effective_message.reply_text(
                            status_text, parse_mode=ParseMode.HTML
                        )
                        last_status_edit_time = now
                    except Exception:
                        pass

                async def on_interim_response(interim_text: str):
                    if turn_ctx.is_completed or turn_ctx.is_prompt_active:
                        return
                    clean_text = interim_text.strip()
                    if not clean_text:
                        return
                    formatted = markdown_to_telegram_html(clean_text)
                    if len(formatted) > 3000:
                        formatted = formatted[:2980] + "..."
                    status_body = (
                        f"💬 <b>Agent:</b>\n"
                        f"<blockquote expandable>{formatted}</blockquote>\n\n"
                        f"⏳ <i>Background task in progress...</i>"
                    )
                    if turn_ctx.status_msg:
                        try:
                            await turn_ctx.status_msg.edit_text(status_body, parse_mode=ParseMode.HTML)
                            return
                        except Exception:
                            pass
                    try:
                        turn_ctx.status_msg = await update.effective_message.reply_text(
                            status_body, parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.debug(f"Failed to send interim response: {e}")

                async def on_turn_prompt(cmd_requested: str, options: List[Tuple[str, str]]):
                    if turn_ctx.is_completed:
                        return

                    # 1. Smart Approval Evaluation
                    decision = self.approval_mgr.evaluate(cmd_requested)
                    clean_cmd = html.escape(cmd_requested)

                    if decision == Decision.AUTO_APPROVE:
                        logger.info(f"Auto-approving safe diagnostic command: {cmd_requested}")
                        await self.tmux_mirror.send_raw_key("1")
                        await self.tmux_mirror.send_raw_key("Enter")
                        if turn_ctx.status_msg:
                            try:
                                await turn_ctx.status_msg.edit_text(
                                    f"⚡ <b>Auto-Approved Diagnostic Command</b>\n"
                                    f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>\n"
                                    f"💭 <i>Executing automatically...</i>",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass
                        return

                    if decision == Decision.HARD_DENY:
                        logger.warning(f"Hard-denying dangerous command by policy: {cmd_requested}")
                        # Reject key: default from second option or dynamically match from parsed options
                        reject_key = options[1][0] if len(options) > 1 else "2"
                        for opt_key, opt_label in options:
                            if any(w in opt_label.lower() for w in ("reject", "deny", "cancel", "no")):
                                reject_key = opt_key
                                break
                        await self.tmux_mirror.send_raw_key(reject_key)
                        await self.tmux_mirror.send_raw_key("Enter")
                        if turn_ctx.status_msg:
                            try:
                                await turn_ctx.status_msg.edit_text(
                                    f"🚫 <b>Command Blocked by Security Policy</b>\n"
                                    f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>\n"
                                    f"⚠️ <i>Execution rejected immediately.</i>",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass
                        return

                    # 2. Decision == Decision.MANUAL: Operator confirmation required
                    turn_ctx.is_prompt_active = True
                    turn_ctx.active_prompt_cmd = cmd_requested

                    # Inspect transcript to synthesize pre-execution unified diff if applicable
                    diff_text = None
                    tool_info = extract_latest_tool_call_from_transcript(latest_transcript)
                    if tool_info:
                        tool_name, tool_args = tool_info
                        diff_text = generate_diff_for_tool(
                            tool_name=tool_name,
                            args=tool_args,
                            workspace=Path(self.config.agent.default_workspace),
                        )

                    buttons = [
                        [
                            InlineKeyboardButton(opt_label, callback_data=f"tmux_key:{opt_key}:{turn_ctx.turn_id}")
                            for opt_key, opt_label in options
                        ]
                    ]
                    markup = InlineKeyboardMarkup(buttons)

                    timeout_sec = self.config.approval.timeout_seconds
                    prompt_html = (
                        "⚠️ <b>Command Authorization Request</b>\n"
                        f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>\n"
                        f"⏱️ <i>Timeout: {timeout_sec}s (Fallback: {self.config.approval.fallback_action})</i>"
                    )

                    inline_diff, patch_file = (None, None)
                    if diff_text:
                        inline_diff, patch_file = format_diff_for_telegram(diff_text)

                    sent_msg: Optional[Message] = None

                    if patch_file:
                        # Diff is large (>3000 chars): send as .patch document attachment
                        if turn_ctx.status_msg:
                            try:
                                await turn_ctx.status_msg.delete()
                                turn_ctx.status_msg = None
                            except Exception:
                                pass
                        try:
                            sent_msg = await update.effective_chat.send_document(
                                document=patch_file,
                                caption=prompt_html,
                                parse_mode=ParseMode.HTML,
                                reply_markup=markup,
                            )
                            turn_ctx.prompt_msg = sent_msg
                        except Exception as ex:
                            logger.error(f"Error sending diff patch document: {ex}")
                    else:
                        if inline_diff:
                            prompt_html += f"\n\n<b>Proposed Diff Preview:</b>\n{markdown_to_telegram_html(inline_diff)}"

                        if turn_ctx.status_msg:
                            try:
                                await turn_ctx.status_msg.edit_text(
                                    prompt_html,
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=markup,
                                )
                                sent_msg = turn_ctx.status_msg
                            except Exception:
                                pass

                        if not sent_msg:
                            try:
                                sent_msg = await update.effective_chat.send_message(
                                    prompt_html,
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=markup,
                                )
                                turn_ctx.prompt_msg = sent_msg
                            except Exception as ex:
                                logger.error(f"Error sending authorization prompt: {ex}")

                    # Callback for timeout expiration
                    async def handle_approval_timeout(pending: PendingApproval, fallback_action: str):
                        if turn_ctx.is_completed or not turn_ctx.is_prompt_active:
                            return
                        turn_ctx.is_prompt_active = False

                        if fallback_action == "abort":
                            await self.tmux_mirror.send_raw_key("C-c")
                            action_desc = "Aborted (Ctrl+C)"
                        else:
                            # Default fallback is reject
                            fallback_key = options[1][0] if len(options) > 1 else "2"
                            for opt_key, opt_label in options:
                                if any(w in opt_label.lower() for w in ("reject", "deny", "cancel", "no")):
                                    fallback_key = opt_key
                                    break
                            await self.tmux_mirror.send_raw_key(fallback_key)
                            await self.tmux_mirror.send_raw_key("Enter")
                            action_desc = "Rejected"

                        notice_text = (
                            f"⏱️ <b>Authorization Timed Out ({timeout_sec}s)</b>\n"
                            f"<pre><code class=\"language-bash\">{clean_cmd}</code></pre>\n"
                            f"Action applied: <b>{action_desc}</b>"
                        )
                        msg_to_edit = turn_ctx.prompt_msg or turn_ctx.status_msg
                        if msg_to_edit:
                            try:
                                if getattr(msg_to_edit, "document", None) or getattr(msg_to_edit, "caption", None):
                                    await msg_to_edit.edit_caption(notice_text, parse_mode=ParseMode.HTML)
                                else:
                                    await msg_to_edit.edit_text(notice_text, parse_mode=ParseMode.HTML)
                            except Exception as e:
                                logger.debug(f"Failed to edit timed out message: {e}")

                    # Arm timeout in ApprovalManager
                    msg_id = sent_msg.message_id if sent_msg else 0
                    self.approval_mgr.register_pending(
                        turn_id=turn_ctx.turn_id,
                        chat_id=chat_id,
                        message_id=msg_id,
                        command=cmd_requested,
                        on_timeout=handle_approval_timeout,
                    )

                async def on_final_response(final_text: str):
                    turn_ctx.is_completed = True
                    turn_ctx.is_prompt_active = False
                    self.approval_mgr.resolve(turn_ctx.turn_id)

                    await self.tmux_mirror.stop_turn_monitoring()

                    if turn_ctx.status_msg:
                        try:
                            await turn_ctx.status_msg.delete()
                        except Exception:
                            pass
                        turn_ctx.status_msg = None

                    self.mirror_log.log("AGY", final_text)
                    await self.reply_safe(update, final_text)
                    # Remove only if current registered turn is still this one
                    if self.active_turns.get(user_id) == turn_ctx:
                        self.active_turns.pop(user_id, None)

                await self.tmux_mirror.start_turn_monitoring(on_prompt=on_turn_prompt)

                async def run_turn_watcher():
                    try:
                        await self.transcript_watcher.watch_turn(
                            transcript_path=latest_transcript,
                            start_offset=start_offset,
                            on_status=on_status_update,
                            on_final=on_final_response,
                            on_interim=on_interim_response,
                            timeout_seconds=self.config.agent.timeout_seconds,
                        )
                    except asyncio.CancelledError:
                        pass
                    except asyncio.TimeoutError:
                        if not turn_ctx.is_completed:
                            turn_ctx.is_completed = True
                            self.approval_mgr.resolve(turn_ctx.turn_id)
                            await self.tmux_mirror.stop_turn_monitoring()
                            if turn_ctx.status_msg:
                                try:
                                    await turn_ctx.status_msg.delete()
                                except Exception:
                                    pass
                                turn_ctx.status_msg = None
                            if self.active_turns.get(user_id) == turn_ctx:
                                self.active_turns.pop(user_id, None)
                            await update.effective_message.reply_text(
                                f"⏱️ <b>Task Timed Out</b>\n\nExecution exceeded {self.config.agent.timeout_seconds}s limit.",
                                parse_mode=ParseMode.HTML,
                            )
                    except Exception as exc:
                        logger.error("Critical error in watcher task: %s", exc, exc_info=True)
                        if not turn_ctx.is_completed:
                            turn_ctx.is_completed = True
                            self.approval_mgr.resolve(turn_ctx.turn_id)
                            await self.tmux_mirror.stop_turn_monitoring()
                            if turn_ctx.status_msg:
                                try:
                                    await turn_ctx.status_msg.delete()
                                except Exception:
                                    pass
                                turn_ctx.status_msg = None
                            if self.active_turns.get(user_id) == turn_ctx:
                                self.active_turns.pop(user_id, None)
                            await update.effective_message.reply_text(
                                f"⚠️ <b>Error in background watcher:</b> <code>{html.escape(str(exc))}</code>",
                                parse_mode=ParseMode.HTML,
                            )

                watcher_task = asyncio.create_task(run_turn_watcher())
                turn_ctx.watcher_task = watcher_task
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

                        response = stdout if stdout else (stderr or "✅ *(No output)*")
                        self.mirror_log.log("AGY", response)
                        await self.reply_safe(update, response)
                    except Exception as e:
                        stop_typing = True
                        typing_task.cancel()
                        await self.reply_safe(update, f"⚠️ Internal error: `{e}`")
                    finally:
                        turn_ctx.is_completed = True
                        self.approval_mgr.resolve(turn_ctx.turn_id)
                        if self.active_turns.get(user_id) == turn_ctx:
                            self.active_turns.pop(user_id, None)

    # -------------------------------------------------------------
    # Proactive Sentinel Alert Handler
    # -------------------------------------------------------------

    async def handle_sentinel_alert(self, payload: dict):
        title = payload.get("title", "Sentinel Alert")
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
        logger.info("Initializing agy-telegram bot (v2.0)...")
        self.app = Application.builder().token(self.config.telegram.bot_token).build()

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("model", self.cmd_model))
        self.app.add_handler(CommandHandler("usage", self.cmd_usage))
        self.app.add_handler(CommandHandler("new", self.cmd_new))
        self.app.add_handler(CommandHandler("autoedit", self.cmd_autoedit))
        self.app.add_handler(CommandHandler("mode", self.cmd_mode))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        self.app.add_handler(CommandHandler("abort", self.cmd_abort))
        self.app.add_handler(CommandHandler("status", self.cmd_status))

        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
                self.handle_message,
            )
        )
        self.app.add_handler(
            MessageHandler(
                (filters.PHOTO | (filters.Document.IMAGE & ~filters.COMMAND)) & ~filters.UpdateType.EDITED_MESSAGE,
                self.handle_media,
            )
        )

        if self.config.sentinel.enabled:
            self.sentinel.register_callback(self.handle_sentinel_alert)
            await self.sentinel.start()

        if self.is_tmux_mode:
            await self.tmux_mirror.start_monitor()

        logger.info("Starting Telegram polling...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            logger.info("Stopping agy-telegram...")
            if self.is_tmux_mode:
                await self.tmux_mirror.stop_monitor()
            await self.sentinel.stop()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
