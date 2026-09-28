"""
Media and screenshot ingestion module for agy-telegram.
Handles Telegram photo and document uploads, validates sizes, and formats multimodal prompts.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple
from telegram import Message

from agy_telegram.config import MediaConfig

logger = logging.getLogger("agy_telegram.media")

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_CAPTION_FALLBACK = (
    "Analizza questo screenshot/immagine e identifica eventuali problemi o passaggi successivi."
)


class MediaHandler:
    def __init__(self, config: MediaConfig, workspace: Optional[Path] = None):
        self.config = config
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.upload_dir = self.workspace / self.config.upload_dir

    def is_image_message(self, message: Message) -> bool:
        """Determines if the message contains an image photo or image document."""
        if bool(message.photo):
            return True

        if message.document:
            mime = (message.document.mime_type or "").lower()
            if mime.startswith("image/"):
                return True
            file_name = (message.document.file_name or "").lower()
            ext = Path(file_name).suffix
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                return True

        return False

    async def process_media_message(
        self,
        message: Message,
    ) -> Tuple[Optional[str], Optional[Path], Optional[str]]:
        """
        Validates, downloads, and composes prompt string from a media message.
        Returns: (prompt_string, saved_file_path, error_message).
        """
        if not self.is_image_message(message):
            return None, None, "Messaggio privo di immagine supportata."

        self.upload_dir.mkdir(parents=True, exist_ok=True)

        target_file_id: Optional[str] = None
        file_ext = ".png"

        if message.photo:
            # Highest resolution photo is last in list
            best_photo = message.photo[-1]
            target_file_id = best_photo.file_id
            file_ext = ".png"
            file_size = best_photo.file_size or 0
        elif message.document:
            target_file_id = message.document.file_id
            orig_name = message.document.file_name or "image.png"
            file_ext = Path(orig_name).suffix.lower()
            if file_ext not in ALLOWED_IMAGE_EXTENSIONS:
                file_ext = ".png"
            file_size = message.document.file_size or 0
        else:
            return None, None, "Nessun media valido rilevato."

        # Size validation
        max_bytes = self.config.max_image_size_mb * 1024 * 1024
        if file_size > max_bytes:
            return (
                None,
                None,
                f"L'immagine supera la dimensione massima consentita di {self.config.max_image_size_mb} MB.",
            )

        ts = time.strftime("%Y%m%d_%H%M%S")
        rand_id = uuid.uuid4().hex[:8]
        dest_filename = f"img_{ts}_{rand_id}{file_ext}"
        dest_path = self.upload_dir / dest_filename

        try:
            tg_file = await message.get_bot().get_file(target_file_id)
            await tg_file.download_to_drive(custom_path=str(dest_path))
            logger.info(f"Downloaded Telegram media to {dest_path}")
        except Exception as e:
            logger.error(f"Failed to download Telegram media {target_file_id}: {e}")
            return None, None, f"Errore durante il download dell'immagine: {e}"

        # Caption extraction or fallback
        instruction = (message.caption or "").strip()
        if not instruction:
            instruction = DEFAULT_CAPTION_FALLBACK

        try:
            rel_path = dest_path.relative_to(self.workspace)
        except ValueError:
            rel_path = dest_path

        prompt = f"[Screenshot caricato]: {rel_path}\nIstruzione: {instruction}"
        return prompt, dest_path, None
