"""State and deterministic data validation for realtime call safety.

High-level safety classification is handled by the AI safety judge. This module
keeps per-call counters and validates structured fields that should never depend
on model judgment, such as missing required fields and invalid dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def looks_like_name(value: Any) -> bool:
    text = _clean(value)
    if not (2 <= len(text) <= 45):
        return False
    parts = [part for part in re.split(r"[\s\-']+", text) if part]
    if not parts:
        return False
    for part in parts:
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", part):
            return False
    return True


def valid_iso_date(value: Any) -> bool:
    try:
        parsed = date.fromisoformat(_clean(value)[:10])
    except ValueError:
        return False
    return parsed <= date.today()


def valid_future_slot(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(_clean(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.date() >= date.today()


@dataclass
class ValidationResult:
    allowed: bool
    reason: str = ""
    message: str = ""
    confidence: Optional[float] = None
    source: str = "local"


@dataclass
class CallSafetyState:
    unclear_count: int = 0
    weak_turn_count: int = 0
    human_request_count: int = 0
    escalation_sent: bool = False
    emergency_triggered: bool = False
    workflow_blocked: bool = False
    workflow_block_reason: str = ""
    identity_resolved: bool = False
    last_agent_text: str = ""
    last_customer_text: str = ""
    recent_customer_texts: List[str] = field(default_factory=list)

    def record_transcript(self, speaker: str, text: str) -> None:
        clean_text = _clean(text)
        if not clean_text:
            return
        if speaker == "agent":
            self.last_agent_text = clean_text
            return
        if speaker == "customer":
            self.last_customer_text = clean_text
            self.recent_customer_texts.append(clean_text)
            self.recent_customer_texts = self.recent_customer_texts[-5:]

    def apply_ai_decision(self, decision: Optional[Dict[str, Any]]) -> Optional[str]:
        """Update counters from AI Safety Judge output and return an action event."""
        if not decision:
            return None

        reason = _clean(decision.get("reason")) or "AI safety judge requested review."
        is_emergency = bool(decision.get("is_emergency"))
        is_unclear = bool(decision.get("is_unclear"))
        is_noise = bool(decision.get("is_noise_or_hallucination"))
        requested_human = bool(decision.get("caller_requested_human"))
        should_escalate = bool(decision.get("should_escalate"))
        can_continue = decision.get("can_continue_workflow")
        quality = _clean(decision.get("conversation_quality")).lower()

        if is_emergency:
            self.emergency_triggered = True
            self.workflow_blocked = True
            self.workflow_block_reason = reason
            return "emergency"

        if requested_human:
            self.human_request_count += 1
            return "human_requested"

        weak_turn = is_unclear or is_noise or quality in {"poor", "unsafe"} or can_continue is False
        if weak_turn:
            self.unclear_count += 1
            self.weak_turn_count += 1
            if should_escalate or self.unclear_count >= 3 or self.weak_turn_count >= 3:
                self.workflow_blocked = True
                self.workflow_block_reason = reason
                return "quality_threshold"
            return "unclear_input"

        self.unclear_count = 0
        self.weak_turn_count = 0
        return None

    def should_escalate(self) -> Optional[str]:
        if self.emergency_triggered:
            return self.workflow_block_reason or "Emergency red flag detected."
        if self.workflow_blocked:
            return self.workflow_block_reason or "Conversation quality threshold reached."
        if self.human_request_count >= 2:
            return "Caller requested human assistance more than once."
        if self.unclear_count >= 3:
            return "Caller input remained unclear after repeated attempts."
        return None

    def validate_tool_call(self, name: str, args: Dict[str, Any], has_phonebook_match: bool = False) -> ValidationResult:
        if self.workflow_blocked and name not in {"terminate_call", "search_knowledge_base"}:
            return ValidationResult(
                False,
                "workflow_blocked",
                self.workflow_block_reason or "Do not continue the workflow. Tell the caller the practice team will review the request.",
            )

        if name in {"get_available_doctors", "get_available_slots", "get_next_available_slot", "book_appointment"} and not self.identity_resolved:
            return ValidationResult(
                False,
                "identity_required",
                "Do not continue the appointment workflow yet. Ask for and confirm the caller's first and last name, then call resolve_phonebook_identity.",
            )

        if name == "resolve_phonebook_identity":
            first = args.get("patient_first_name")
            last = args.get("patient_last_name")
            if not looks_like_name(first) or not looks_like_name(last):
                return ValidationResult(
                    False,
                    "invalid_identity_name",
                    "The caller name is not clear enough. Ask again for first and last name, then repeat it back for confirmation.",
                )
            return ValidationResult(True)

        if name == "book_appointment":
            required = {
                "slot_iso": "appointment time",
                "patient_first_name": "first name",
                "patient_last_name": "last name",
                "patient_phone": "phone number",
                "visit_reason": "visit reason",
            }
            if not has_phonebook_match:
                required["patient_dob"] = "date of birth"
            missing = [
                label
                for key, label in required.items()
                if not _clean(args.get(key)) or _clean(args.get(key)).upper() == "MISSING"
            ]
            if missing:
                return ValidationResult(
                    False,
                    "missing_booking_fields",
                    f"Do not book yet. Ask the caller for the missing information: {', '.join(missing)}.",
                )
            if not looks_like_name(args.get("patient_first_name")) or not looks_like_name(args.get("patient_last_name")):
                return ValidationResult(False, "invalid_booking_name", "The patient name is unclear. Ask for first and last name again.")
            if _clean(args.get("patient_dob")) and not valid_iso_date(args.get("patient_dob")):
                return ValidationResult(False, "invalid_dob", "The date of birth is missing or invalid. Ask the caller to repeat it.")
            if not valid_future_slot(args.get("slot_iso")):
                return ValidationResult(False, "invalid_slot", "The appointment slot is missing or not a future date. Offer a valid future slot first.")
            return ValidationResult(True)

        return ValidationResult(True)
