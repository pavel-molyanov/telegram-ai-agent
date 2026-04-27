"""Deepgram async wrapper for voice message transcription."""

from __future__ import annotations

import asyncio
import logging

from deepgram import AsyncDeepgramClient

from telegram_bot.core.config import Settings

logger = logging.getLogger(__name__)

_TRANSCRIPTION_TIMEOUT_SEC = 30


class TranscriptionError(Exception):
    """Raised when transcription fails."""


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        self._enabled = bool(settings.deepgram_api_key)
        self._client = (
            AsyncDeepgramClient(api_key=settings.deepgram_api_key) if self._enabled else None
        )

    async def transcribe(self, audio_data: bytes) -> str:
        """Transcribe audio bytes via Deepgram Nova-3.

        Returns transcript text (may be empty for non-speech audio).
        Raises TranscriptionError on API or network failures or if Deepgram is not configured.
        """
        if not self._enabled or self._client is None:
            raise TranscriptionError("Deepgram API key not configured")
        try:
            response = await asyncio.wait_for(
                self._client.listen.v1.media.transcribe_file(
                    request=audio_data,
                    model="nova-3",
                    language="ru",
                    smart_format=True,
                ),
                timeout=_TRANSCRIPTION_TIMEOUT_SEC,
            )
        except TimeoutError:
            logger.warning("Deepgram transcription timed out after %ds", _TRANSCRIPTION_TIMEOUT_SEC)
            raise TranscriptionError("Deepgram transcription timed out") from None
        except Exception as exc:
            logger.warning("Deepgram API error: %s", exc)
            raise TranscriptionError(f"Deepgram API error: {exc}") from exc

        channels = response.results.channels
        if not channels or not channels[0].alternatives:
            logger.info("Transcription done, empty response (silence or corrupted audio)")
            return ""

        transcript: str = channels[0].alternatives[0].transcript
        logger.info("Transcription done, length=%d chars", len(transcript))
        return transcript
