"""AI safety classifier for realtime telephone calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AISafetyDecision:
    is_emergency: bool = False
    is_unclear: bool = False
    is_noise_or_hallucination: bool = False
    caller_requested_human: bool = False
    conversation_quality: str = "good"
    can_continue_workflow: bool = True
    should_escalate: bool = False
    confidence: float = 0.0
    reason: str = ""
    requested_language: Optional[str] = None
    detected_language: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AIToolValidation:
    allowed: bool = True
    reason: str = ""
    message: str = ""
    confidence: float = 0.0


class AISafetyJudge:
    """Uses the configured Azure OpenAI chat deployment as a safety judge."""

    def __init__(self):
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self.api_key = os.getenv("AZURE_OPENAI_KEY")
        self.api_version = os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION") or os.getenv("AZURE_OPENAI_API_VERSION")
        self.deployment = (
            os.getenv("AZURE_OPENAI_SAFETY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
        )
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.api_key and self.api_version and self.deployment)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncAzureOpenAI

            self._client = AsyncAzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
            )
        return self._client

    def _parse_json(self, content: str) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    async def _chat_json(self, messages: List[Dict[str, str]], max_tokens: int, timeout: float) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            logger.warning("[AI SAFETY] Azure OpenAI safety config missing; judge disabled")
            return None

        try:
            client = self._get_client()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.deployment,
                    temperature=0,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    messages=messages,
                ),
                timeout=timeout,
            )
            content = response.choices[0].message.content or ""
            return self._parse_json(content)
        except Exception as exc:
            logger.warning(f"[AI SAFETY] Judge call failed: {exc}")
            return None

    async def classify_turn(
        self,
        utterance: str,
        language: str,
        recent_customer_texts: List[str],
        last_agent_text: str = "",
    ) -> Optional[AISafetyDecision]:
        text = (utterance or "").strip()
        if not text:
            return None

        data = await self._chat_json(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a safety classifier for a realtime medical reception phone assistant. "
                        "Classify only the caller's latest utterance in context. Do not continue the conversation. "
                        "Be conservative: if the utterance is likely ASR noise, garbled, random, contradictory, or too unclear for a safe workflow, mark it unclear/noise. "
                        "If the caller may need urgent medical attention, mark emergency. "
                        "If the caller asks for staff/human/transfer/callback, mark caller_requested_human. "
                        "Return only valid JSON with keys: is_emergency, is_unclear, is_noise_or_hallucination, "
                        "caller_requested_human, conversation_quality, can_continue_workflow, should_escalate, confidence, reason. "
                        "Also include requested_language and detected_language as ISO-like codes when clear: de, en, fr, it, es, tr, ar, ku, or null. "
                        "requested_language must be set only when the caller explicitly asks to use a language. "
                        "conversation_quality must be one of: good, weak, poor, unsafe."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_language": language,
                            "last_agent_text": last_agent_text,
                            "recent_customer_texts": recent_customer_texts[-5:],
                            "latest_customer_utterance": text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=260,
            timeout=5.0,
        )
        if not data:
            return None

        return AISafetyDecision(
            is_emergency=bool(data.get("is_emergency")),
            is_unclear=bool(data.get("is_unclear")),
            is_noise_or_hallucination=bool(data.get("is_noise_or_hallucination")),
            caller_requested_human=bool(data.get("caller_requested_human")),
            conversation_quality=str(data.get("conversation_quality") or "good").lower(),
            can_continue_workflow=bool(data.get("can_continue_workflow", True)),
            should_escalate=bool(data.get("should_escalate")),
            confidence=float(data.get("confidence") or 0.0),
            reason=str(data.get("reason") or ""),
            requested_language=data.get("requested_language"),
            detected_language=data.get("detected_language"),
        )

    async def validate_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        recent_customer_texts: List[str],
        identity_resolved: bool,
        has_phonebook_match: bool,
        workflow_blocked: bool,
    ) -> Optional[AIToolValidation]:
        data = await self._chat_json(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You validate tool calls for a medical reception AI before execution. "
                        "Do not rely on memorized keywords. Judge whether the supplied structured arguments are safe, clear, and consistent with the recent caller turns. "
                        "Block if the AI appears to be guessing names, symptoms, identity, appointment slot, doctor, DOB, or booking details. "
                        "For booking, require confirmed identity and enough real patient/slot information. "
                        "Return only valid JSON with keys: allowed, reason, message, confidence. "
                        "If blocked, message must be a short instruction the assistant can follow as one clarification question."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tool_name": tool_name,
                            "arguments": args or {},
                            "recent_customer_texts": recent_customer_texts[-5:],
                            "identity_resolved": identity_resolved,
                            "has_phonebook_match": has_phonebook_match,
                            "workflow_blocked": workflow_blocked,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=240,
            timeout=5.0,
        )
        if not data:
            return None

        return AIToolValidation(
            allowed=bool(data.get("allowed", True)),
            reason=str(data.get("reason") or ""),
            message=str(data.get("message") or ""),
            confidence=float(data.get("confidence") or 0.0),
        )


ai_safety_judge = AISafetyJudge()
