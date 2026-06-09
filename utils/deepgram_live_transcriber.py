"""Deepgram live transcription bridge for ACS audio streams.

This module is intentionally SDK-free so deployment does not depend on a
specific Deepgram Python SDK version. It streams raw ACS PCM frames to
Deepgram over WebSocket and emits finalized customer transcripts.
"""

import asyncio
import base64
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

TranscriptCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class DeepgramLiveTranscriber:
    """Small async WebSocket client for Deepgram streaming STT."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        sample_rate: int,
        encoding: str,
        language: Optional[str],
        language_hint: Optional[str],
        on_transcript: TranscriptCallback,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.language = language
        self.language_hint = language_hint
        self.on_transcript = on_transcript
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=400)
        self._closed = asyncio.Event()
        self._last_final = ""
        self.connected = False
        self.failed = False

    @classmethod
    def from_env(cls, on_transcript: TranscriptCallback) -> Optional["DeepgramLiveTranscriber"]:
        api_key = os.getenv("DEEPGRAM_API_KEY")
        provider = os.getenv("TRANSCRIPTION_PROVIDER", "").strip().lower()
        enabled = os.getenv("DEEPGRAM_LIVE_TRANSCRIPTION_ENABLED", "").strip().lower()
        if not api_key or (provider != "deepgram" and enabled not in {"1", "true", "yes", "on"}):
            return None

        return cls(
            api_key=api_key,
            model=os.getenv("DEEPGRAM_LIVE_MODEL", "flux-general-multi"),
            sample_rate=int(os.getenv("DEEPGRAM_SAMPLE_RATE", "16000")),
            encoding=os.getenv("DEEPGRAM_ENCODING", "linear16"),
            language=os.getenv("DEEPGRAM_LANGUAGE") or None,
            language_hint=os.getenv("DEEPGRAM_LANGUAGE_HINT") or None,
            on_transcript=on_transcript,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def queue_acs_audio(self, acs_message: Dict[str, Any]) -> None:
        """Queue a base64 ACS AudioData frame for Deepgram."""
        try:
            payload = acs_message.get("audioData", {}).get("data")
            if not payload:
                return
            audio = base64.b64decode(payload)
            if self._audio_queue.full():
                try:
                    self._audio_queue.get_nowait()
                    self._audio_queue.task_done()
                    logger.warning("[DEEPGRAM] Dropped stale audio frame due to full queue")
                except asyncio.QueueEmpty:
                    pass
            await self._audio_queue.put(audio)
        except Exception as exc:
            logger.debug(f"[DEEPGRAM] Could not queue audio: {exc}")

    async def close(self) -> None:
        self._closed.set()
        try:
            await self._audio_queue.put(None)
        except Exception:
            pass

    def _url(self) -> str:
        is_flux = self.model.startswith("flux-")
        base_url = "wss://api.deepgram.com/v2/listen" if is_flux else "wss://api.deepgram.com/v1/listen"
        params: Dict[str, Any] = {
            "model": self.model,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
        }

        if is_flux:
            if self.language_hint:
                params["language_hint"] = self.language_hint
        else:
            params.update(
                {
                    "interim_results": "true",
                    "punctuate": "true",
                    "smart_format": "true",
                    "utterances": "true",
                    "vad_events": "true",
                    "endpointing": "500",
                }
            )
            if self.language:
                params["language"] = self.language

        return f"{base_url}?{urlencode(params)}"

    async def run(self) -> None:
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(self._url(), headers=headers, heartbeat=15) as ws:
                    self.connected = True
                    self.failed = False
                    logger.info(f"[DEEPGRAM] Connected live transcription model={self.model}")
                    sender = asyncio.create_task(self._send_audio(ws))
                    receiver = asyncio.create_task(self._receive_transcripts(ws))
                    done, pending = await asyncio.wait(
                        [sender, receiver],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in done:
                        exc = task.exception()
                        if exc:
                            raise exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed = True
            logger.error(f"[DEEPGRAM] Live transcription stopped: {exc}")
        finally:
            self.connected = False

    async def _send_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._closed.is_set():
            try:
                item = await asyncio.wait_for(self._audio_queue.get(), timeout=8)
            except asyncio.TimeoutError:
                await ws.send_str(json.dumps({"type": "KeepAlive"}))
                continue

            try:
                if item is None:
                    await ws.send_str(json.dumps({"type": "CloseStream"}))
                    return
                await ws.send_bytes(item)
            finally:
                self._audio_queue.task_done()

    async def _receive_transcripts(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        transcript = self._extract_transcript(data)
        if not transcript:
            return

        is_final = bool(
            data.get("is_final")
            or data.get("speech_final")
            or data.get("type") in {"TurnInfo", "UtteranceEnd"}
        )
        if not is_final:
            return

        normalized = " ".join(transcript.split())
        if not normalized or normalized == self._last_final:
            return
        self._last_final = normalized
        await self.on_transcript(normalized, data)

    def _extract_transcript(self, data: Dict[str, Any]) -> str:
        direct = data.get("transcript")
        if isinstance(direct, str):
            return direct.strip()

        channel = data.get("channel")
        if isinstance(channel, dict):
            alternatives = channel.get("alternatives") or []
            if alternatives and isinstance(alternatives[0], dict):
                return str(alternatives[0].get("transcript") or "").strip()

        return ""
