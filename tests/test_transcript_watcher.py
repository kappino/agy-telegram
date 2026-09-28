"""
Unit tests for TranscriptWatcher O(1) tailing and turn lifecycle.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agy_telegram.core.transcript_watcher import TranscriptWatcher


class TestTranscriptWatcher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.transcript_file = Path(self.temp_dir.name) / "transcript.jsonl"
        self.watcher = TranscriptWatcher()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_get_current_offset(self):
        self.assertEqual(self.watcher.get_current_offset(self.transcript_file), 0)
        self.transcript_file.write_text("line 1\nline 2\n", encoding="utf-8")
        self.assertEqual(self.watcher.get_current_offset(self.transcript_file), len("line 1\nline 2\n".encode("utf-8")))

    def test_watch_turn_lifecycle(self):
        """Verifica che i tool steps intermedi non interrompano il watcher e che on_final riceva il testo corretto."""
        statuses = []
        finals = []

        async def dummy_on_status(status_text: str):
            statuses.append(status_text)

        async def dummy_on_final(final_text: str):
            finals.append(final_text)

        # Inizializza file transcript
        self.transcript_file.write_text("", encoding="utf-8")

        async def run_test():
            # Avvia il watcher in background
            watch_task = asyncio.create_task(
                self.watcher.watch_turn(
                    transcript_path=self.transcript_file,
                    start_offset=0,
                    on_status=dummy_on_status,
                    on_final=dummy_on_final,
                    timeout_seconds=5,
                )
            )

            await asyncio.sleep(0.1)

            # Step 1: Thinking
            step1 = json.dumps({"type": "PLANNER_RESPONSE", "thinking": "Thinking about plan..."}) + "\n"
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(step1)

            await asyncio.sleep(0.4)

            # Step 2: Tool execution (con messaggio parziale content che NON deve terminare il turno!)
            step2 = json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "Controllo file...",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls -la", "toolAction": "List files"}}],
            }) + "\n"
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(step2)

            await asyncio.sleep(0.4)

            # Step 3: Tool generic output
            step3 = json.dumps({"type": "GENERIC", "content": "file1.txt\nfile2.txt"}) + "\n"
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(step3)

            await asyncio.sleep(0.4)

            # A questo punto il watcher deve essere ANCORA attivo (finals deve essere vuoto)
            self.assertEqual(len(finals), 0, "Il watcher non deve terminare durante l'esecuzione dei tool!")

            # Step 4: Risposta finale effettiva
            step4 = json.dumps({"type": "PLANNER_RESPONSE", "content": "Operazione completata con successo!"}) + "\n"
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(step4)

            # Attendi completamento naturale del watcher
            await asyncio.wait_for(watch_task, timeout=2.0)

        asyncio.run(run_test())

        self.assertGreaterEqual(len(statuses), 1, "Devono essere stati emessi stati intermedi")
        self.assertEqual(len(finals), 1, "Deve essere stata emessa esattamente una risposta finale")
        self.assertEqual(finals[0], "Operazione completata con successo!")


if __name__ == "__main__":
    unittest.main()
