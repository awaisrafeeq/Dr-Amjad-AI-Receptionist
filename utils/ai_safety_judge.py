"""AI-based safety review for live call transcripts.

This module intentionally uses a separate chat model instead of keyword lists so
urgent or incoherent calls can be flagged even when wording, language, or accent
varies.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SafetyAssessment:
    should_handoff: bool
    reason: str
    urgency: str
    confidence: float
    office_summary: str
    caller_message: str

    @classmethod
    def safe_default(cls) -> "SafetyAssessment":
        return cls(
            should_handoff=False,
            reason="normal",
            urgency="unknown",
            confidence=0.0,
            office_summary="",
            caller_message="",
        )


class AISafetyJudge:
    """Classify recent call context for emergency/manual-review escalation."""

    def __init__(self) -> None:
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self.api_key = os.getenv("AZURE_OPENAI_KEY")
        self.api_version = (
            os.getenv("AZURE_OPENAI_SAFETY_API_VERSION")
            or os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION")
            or os.getenv("AZURE_OPENAI_API_VERSION")
        )
        self.deployment = (
            os.getenv("AZURE_OPENAI_SAFETY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        )

    def _configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.api_version and self.deployment)

    async def assess(self, transcript: List[Dict[str, Any]]) -> SafetyAssessment:
        if not self._configured():
            logger.warning("[SAFETY JUDGE] Azure OpenAI config missing; skipping")
            return SafetyAssessment.safe_default()

        compact = self._format_transcript(transcript)
        if not compact.strip():
            return SafetyAssessment.safe_default()

        try:
            from openai import AsyncAzureOpenAI

            client = AsyncAzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
            )
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.deployment,
                    temperature=0,
                    max_tokens=260,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a safety and call-quality classifier for a medical reception voice assistant. "
                                "Assess ONLY the transcript. Do not diagnose. Do not infer facts that are not stated. "
                                "Flag for handoff when the caller appears medically urgent, incoherent/confused, stuck in repeated misunderstanding, "
                                "or asks for human/manual follow-up. Be conservative for emergency: if clearly urgent, set urgency to emergency or same_day. "
                                "Return only valid JSON with keys: should_handoff boolean, reason string, urgency string, confidence number 0-1, "
                                "office_summary string, caller_message string. "
                                "Allowed reasons: normal, urgent_medical, confused_or_incoherent, repeated_misunderstanding, caller_requests_staff, other. "
                                "Allowed urgency: emergency, same_day, this_week, routine, unknown."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Recent transcript:\n{compact}",
                        },
                    ],
                ),
                timeout=8,
            )
            content = (response.choices[0].message.content or "").strip()
            data = self._parse_json(content)
            assessment = SafetyAssessment(
                should_handoff=bool(data.get("should_handoff", False)),
                reason=self._clean_choice(
                    data.get("reason"),
                    {"normal", "urgent_medical", "confused_or_incoherent", "repeated_misunderstanding", "caller_requests_staff", "other"},
                    "other",
                ),
                urgency=self._clean_choice(
                    data.get("urgency"),
                    {"emergency", "same_day", "this_week", "routine", "unknown"},
                    "unknown",
                ),
                confidence=self._safe_float(data.get("confidence")),
                office_summary=str(data.get("office_summary") or "").strip(),
                caller_message=str(data.get("caller_message") or "").strip(),
            )
            if assessment.reason == "normal":
                assessment.should_handoff = False
            return assessment
        except Exception as exc:
            logger.warning(f"[SAFETY JUDGE] Assessment failed: {exc}")
            return SafetyAssessment.safe_default()

    def _format_transcript(self, transcript: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for entry in transcript[-12:]:
            speaker = str(entry.get("speaker") or "unknown").strip()
            text = re.sub(r"\s+", " ", str(entry.get("utterance_text") or "")).strip()
            if text:
                lines.append(f"{speaker}: {text[:500]}")
        return "\n".join(lines)

    def _parse_json(self, content: str) -> Dict[str, Any]:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if match:
                return json.loads(match.group(0))
            raise

    def _clean_choice(self, value: Any, allowed: set[str], default: str) -> str:
        text = str(value or "").strip().lower()
        return text if text in allowed else default

    def _safe_float(self, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0


ai_safety_judge = AISafetyJudge()
