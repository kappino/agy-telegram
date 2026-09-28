"""
Unit tests for MediaHandler (media_handler.py).
Verifies detection, validation, download mock, and multimodal prompt composition.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agy_telegram.config import MediaConfig
from agy_telegram.core.media_handler import MediaHandler, DEFAULT_CAPTION_FALLBACK


class TestMediaHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.config = MediaConfig(upload_dir="uploads", max_image_size_mb=5)
        self.handler = MediaHandler(config=self.config, workspace=self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_image_message(self):
        """Verifies identification of photo and valid image document messages."""
        msg_photo = MagicMock()
        msg_photo.photo = [MagicMock()]
        msg_photo.document = None
        self.assertTrue(self.handler.is_image_message(msg_photo))

        msg_doc_png = MagicMock()
        msg_doc_png.photo = []
        msg_doc_png.document.mime_type = "image/png"
        msg_doc_png.document.file_name = "shot.png"
        self.assertTrue(self.handler.is_image_message(msg_doc_png))

        msg_doc_pdf = MagicMock()
        msg_doc_pdf.photo = []
        msg_doc_pdf.document.mime_type = "application/pdf"
        msg_doc_pdf.document.file_name = "doc.pdf"
        self.assertFalse(self.handler.is_image_message(msg_doc_pdf))

    async def test_process_media_message_photo_success(self):
        """Processes a valid photo message with caption and returns properly formatted prompt."""
        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_photo_1 = MagicMock(file_id="thumb_id", file_size=1024)
        mock_photo_2 = MagicMock(file_id="high_res_id", file_size=2048)

        mock_message = MagicMock()
        mock_message.photo = [mock_photo_1, mock_photo_2]
        mock_message.document = None
        mock_message.caption = "Risolvi questo bug nell'UI"
        mock_message.get_bot.return_value = mock_bot

        prompt, saved_path, err = await self.handler.process_media_message(mock_message)

        self.assertIsNone(err)
        self.assertIsNotNone(prompt)
        self.assertIsNotNone(saved_path)
        self.assertTrue(saved_path.name.endswith(".png"))
        self.assertIn("Risolvi questo bug nell'UI", prompt)
        self.assertIn(str(saved_path.relative_to(self.workspace)), prompt)
        mock_bot.get_file.assert_awaited_once_with("high_res_id")
        self.assertTrue(mock_file.download_to_drive.called)

    async def test_process_media_message_default_caption(self):
        """Falls back to default Italian caption when no caption is provided."""
        mock_file = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_photo = MagicMock(file_id="photo_id", file_size=1024)
        mock_message = MagicMock()
        mock_message.photo = [mock_photo]
        mock_message.document = None
        mock_message.caption = ""
        mock_message.get_bot.return_value = mock_bot

        prompt, saved_path, err = await self.handler.process_media_message(mock_message)

        self.assertIsNone(err)
        self.assertIn(DEFAULT_CAPTION_FALLBACK, prompt)

    async def test_process_media_message_file_too_large(self):
        """Rejects media exceeding max_image_size_mb."""
        mock_photo = MagicMock(file_id="huge_id", file_size=10 * 1024 * 1024)  # 10 MB > 5 MB limit
        mock_message = MagicMock()
        mock_message.photo = [mock_photo]
        mock_message.document = None

        prompt, saved_path, err = await self.handler.process_media_message(mock_message)

        self.assertIsNotNone(err)
        self.assertIsNone(prompt)
        self.assertIsNone(saved_path)
        self.assertIn("dimensione massima", err)
