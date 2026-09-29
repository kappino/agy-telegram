"""
Unit tests for File Commands, Mobile Quick Keyboard, and Watchdog Investigation Actions.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agy_telegram.config import (
    AppConfig,
    TelegramConfig,
    AgentConfig,
    SentinelConfig,
    MirrorConfig,
)
from agy_telegram.core.bot import AgyTelegramBot


def make_test_config(allowed_user_id: int = 12345, workspace: str = ".") -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(bot_token="123456:dummy_token", allowed_users=[allowed_user_id]),
        agent=AgentConfig(default_workspace=workspace),
        sentinel=SentinelConfig(enabled=False, watchdog_enabled=False),
        mirror=MirrorConfig(mode="tmux", target_session="test:0.0"),
    )


def make_mock_update(user_id: int, chat_id: int = 999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.effective_chat.send_message = AsyncMock()
    update.effective_chat.send_action = AsyncMock()
    update.effective_chat.send_document = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    update.message = update.effective_message
    return update


class TestFileCommandsAndKeyboard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = 12345
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.bot = AgyTelegramBot(make_test_config(allowed_user_id=self.user_id, workspace=str(self.workspace)))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_quick_keyboard_structure(self):
        """Verifies persistent quick keyboard includes required buttons."""
        kb = self.bot.get_quick_keyboard()
        self.assertTrue(kb.resize_keyboard)
        self.assertTrue(kb.is_persistent)

        button_texts = [btn.text for row in kb.keyboard for btn in row]
        self.assertTrue(any("Status" in b for b in button_texts))
        self.assertTrue(any("Modello" in b for b in button_texts))
        self.assertTrue(any("Resume" in b for b in button_texts))
        self.assertTrue(any("File" in b for b in button_texts))
        self.assertTrue(any("Modalità" in b for b in button_texts))
        self.assertTrue(any("Interrompi" in b for b in button_texts))

    async def test_cmd_get_nonexistent_file(self):
        """Verifies cmd_get reports error when file does not exist."""
        mock_update = make_mock_update(self.user_id)
        mock_update.message.text = "/get nonexistent.txt"

        mock_context = MagicMock()
        mock_context.args = ["nonexistent.txt"]

        await self.bot.cmd_get(mock_update, mock_context)

        mock_update.effective_chat.send_message.assert_awaited_once()
        args, _ = mock_update.effective_chat.send_message.await_args
        self.assertIn("non trovato", args[0])

    async def test_cmd_get_file_too_large(self):
        """Verifies files over 50MB are rejected with informative message."""
        huge_file = self.workspace / "huge.bin"
        huge_file.write_bytes(b"dummy")

        mock_update = make_mock_update(self.user_id)
        mock_update.message.text = f"/get {huge_file.name}"

        mock_context = MagicMock()
        mock_context.args = [str(huge_file.name)]

        # Mock stat to return 55MB file size
        fake_stat = os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 1000, 1000, 55 * 1024 * 1024, 0, 0, 0))
        with patch.object(Path, "stat", return_value=fake_stat):
            await self.bot.cmd_get(mock_update, mock_context)

        mock_update.effective_chat.send_message.assert_awaited_once()
        args, _ = mock_update.effective_chat.send_message.await_args
        self.assertIn("File troppo grande", args[0])

    async def test_cmd_get_valid_file(self):
        """Verifies valid file sends document to Telegram chat."""
        sample_file = self.workspace / "output.txt"
        sample_file.write_text("Hello from Antigravity!")

        mock_update = make_mock_update(self.user_id)
        mock_context = MagicMock()
        mock_context.args = ["output.txt"]

        await self.bot.cmd_get(mock_update, mock_context)

        mock_update.effective_chat.send_action.assert_awaited_once()
        mock_update.effective_chat.send_document.assert_awaited_once()
        kwargs = mock_update.effective_chat.send_document.await_args.kwargs
        self.assertEqual(kwargs["filename"], "output.txt")

    async def test_cmd_files_lists_workspace_files(self):
        """Verifies /files displays inline buttons for workspace files."""
        f1 = self.workspace / "script1.py"
        f1.write_text("print(1)")
        f2 = self.workspace / "report.md"
        f2.write_text("# Report")

        mock_update = make_mock_update(self.user_id)
        await self.bot.cmd_files(mock_update, MagicMock())

        mock_update.effective_message.reply_text.assert_awaited_once()
        args, kwargs = mock_update.effective_message.reply_text.await_args
        self.assertIn("File", args[0])
        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertGreaterEqual(len(reply_markup.inline_keyboard), 2)

    async def test_callback_dl_file_serves_cached_path(self):
        """Verifies dl_file callback delivers requested file."""
        sample_file = self.workspace / "generated.py"
        sample_file.write_text("a = 1")
        btn_key = "abc123"
        self.bot.file_download_cache[btn_key] = str(sample_file)

        mock_update = make_mock_update(self.user_id)
        mock_query = MagicMock()
        mock_query.data = f"dl_file:{btn_key}"
        mock_query.from_user.id = self.user_id
        mock_query.answer = AsyncMock()
        mock_query.message = mock_update.effective_message
        mock_update.callback_query = mock_query

        await self.bot.handle_callback(mock_update, MagicMock())

        mock_query.answer.assert_awaited_once()
        mock_update.effective_chat.send_document.assert_awaited_once()

    async def test_callback_investigate_submits_to_agent(self):
        """Verifies clicking investigate sends action prompt to Antigravity."""
        alert_id = "alert42"
        action = "Verifica il container 105 su Proxmox"
        self.bot.sentinel_actions[alert_id] = action

        mock_update = make_mock_update(self.user_id)
        mock_query = MagicMock()
        mock_query.data = f"investigate:{alert_id}"
        mock_query.from_user.id = self.user_id
        mock_query.answer = AsyncMock()
        mock_query.edit_message_reply_markup = AsyncMock()
        mock_query.message = mock_update.effective_message
        mock_update.callback_query = mock_query

        self.bot.tmux_mirror.check_session_exists = AsyncMock(return_value=True)
        self.bot.tmux_mirror.send_input = AsyncMock()

        await self.bot.handle_callback(mock_update, MagicMock())

        mock_query.answer.assert_awaited_once()
        mock_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.bot.tmux_mirror.send_input.assert_awaited_once_with(action, press_enter=True)
