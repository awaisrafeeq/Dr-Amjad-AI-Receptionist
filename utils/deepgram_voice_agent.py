import asyncio
import array
import base64
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import aiohttp
from azure.communication.callautomation import CallAutomationClient
from fastapi import WebSocket

from config import get_config
from utils.availability import (
    build_next_available_recommendation,
    build_slot_recommendation,
    compute_free_slots,
    default_opening_hours,
)
from utils.document_utils import document_processor
from utils.epaad_client import epaad_client
from utils.helpers import transform_acs_to_openai_format
from utils.session_manager import session_manager

logger = logging.getLogger(__name__)
config = get_config()

_LATIN_NAME_EXTRA_CHARS = set(" -'.`´'")
_SUPPORTED_FLUX_LANGUAGE_HINTS = {"de", "en", "fr", "it", "es", "nl", "pt", "ja", "hi", "ru"}
_BASE_ASR_KEYTERMS = [
    "MedCenter Volta",
    "Kaya",
    "Basel",
    "Lehenmatt",
    "Lehenmattstrasse",
    "Lehenmattstraße",
    "Missionsstrasse",
    "Missionsstraße",
    "Spalenring",
    "Klybeckstrasse",
    "Klybeckstraße",
    "Voltastrasse",
    "Voltastraße",
    "Dornacherstrasse",
    "Dornacherstraße",
    "Gundeldingerstrasse",
    "Gundeldingerstraße",
    "Mallisho",
    "Amjad Mallisho",
    "Keser",
    "Elif Keser",
    "Lumpp",
    "Osterwalder",
    "Termin",
    "Sprechstunde",
    "Hausarzt",
    "Medikament",
    "Rezept",
    "Überweisung",
    "Ueberweisung",
]


class VoiceGenderEstimator:
    """Lightweight caller-audio pitch estimator. It does not store raw audio."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.frame_samples = max(400, int(sample_rate * 0.03))
        self.buffer = bytearray()
        self.pitch_estimates: List[float] = []
        self.max_estimates = 50

    def add_audio(self, raw_audio: bytes) -> None:
        if len(self.pitch_estimates) >= self.max_estimates:
            return
        self.buffer.extend(raw_audio)
        frame_bytes = self.frame_samples * 2
        while len(self.buffer) >= frame_bytes and len(self.pitch_estimates) < self.max_estimates:
            frame = bytes(self.buffer[:frame_bytes])
            del self.buffer[:frame_bytes]
            pitch = self._estimate_pitch(frame)
            if pitch:
                self.pitch_estimates.append(pitch)

    def classification(self) -> str:
        if len(self.pitch_estimates) < 8:
            return "other"
        values = sorted(self.pitch_estimates)
        median = values[len(values) // 2]
        if median < 165:
            return "male"
        if median > 185:
            return "female"
        return "other"

    def confidence(self) -> float:
        if len(self.pitch_estimates) < 8:
            return 0.0
        values = sorted(self.pitch_estimates)
        median = values[len(values) // 2]
        distance = min(abs(median - 165), abs(median - 185))
        return round(min(0.95, 0.45 + (distance / 80)), 2)

    def _estimate_pitch(self, frame: bytes) -> Optional[float]:
        samples = array.array("h")
        samples.frombytes(frame)
        if not samples:
            return None
        mean = sum(samples) / len(samples)
        centered = [sample - mean for sample in samples]
        energy = sum(sample * sample for sample in centered) / len(centered)
        if energy < 90000:
            return None

        min_lag = max(1, int(self.sample_rate / 300))
        max_lag = min(len(centered) // 2, int(self.sample_rate / 75))
        if max_lag <= min_lag:
            return None

        best_lag = 0
        best_score = 0.0
        base_energy = sum(sample * sample for sample in centered)
        if base_energy <= 0:
            return None

        for lag in range(min_lag, max_lag + 1):
            score = 0.0
            for idx in range(len(centered) - lag):
                score += centered[idx] * centered[idx + lag]
            if score > best_score:
                best_score = score
                best_lag = lag

        if not best_lag or best_score / base_energy < 0.25:
            return None
        return self.sample_rate / best_lag


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _csv_values(value: Any) -> List[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _clean_keyterm(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or len(text) < 2:
        return ""
    return text[:80]


def _dedupe_preserve_order(values: List[str], max_items: Optional[int] = None) -> List[str]:
    seen = set()
    cleaned: List[str] = []
    for value in values:
        term = _clean_keyterm(value)
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(term)
        if max_items and len(cleaned) >= max_items:
            break
    return cleaned


def _is_latin_name(value: Optional[str]) -> bool:
    if not value:
        return False
    for char in value.strip():
        if char in _LATIN_NAME_EXTRA_CHARS:
            continue
        if char.isalpha():
            try:
                if "LATIN" not in unicodedata.name(char):
                    return False
            except ValueError:
                return False
            continue
        return False
    return True


def _today_ch() -> date:
    return _now_ch().date()


def _now_ch() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(os.getenv("EPAAD_TIMEZONE", "Europe/Zurich")))
    except Exception:
        return datetime.now()


def _normalize_dob_for_epaad(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        return text
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            continue
    return text


def _parse_spoken_number(value: Any) -> Optional[int]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    compact = re.sub(r"[^a-z]", "", normalized)
    if not compact:
        return None

    german_units = {
        "ein": 1,
        "eins": 1,
        "eine": 1,
        "zwei": 2,
        "drei": 3,
        "vier": 4,
        "funf": 5,
        "fuenf": 5,
        "sechs": 6,
        "sieben": 7,
        "acht": 8,
        "neun": 9,
    }
    german_teens = {
        "zehn": 10,
        "elf": 11,
        "zwolf": 12,
        "zwoelf": 12,
        "dreizehn": 13,
        "vierzehn": 14,
        "funfzehn": 15,
        "fuenfzehn": 15,
        "sechzehn": 16,
        "siebzehn": 17,
        "achtzehn": 18,
        "neunzehn": 19,
    }
    german_tens = {
        "zwanzig": 20,
        "dreissig": 30,
        "dreisig": 30,
        "vierzig": 40,
        "funfzig": 50,
        "fuenfzig": 50,
        "sechzig": 60,
        "siebzig": 70,
        "achtzig": 80,
        "neunzig": 90,
    }

    def parse_german_under_100(part: str) -> Optional[int]:
        if not part:
            return 0
        if part in german_units:
            return german_units[part]
        if part in german_teens:
            return german_teens[part]
        if part in german_tens:
            return german_tens[part]
        if "und" in part:
            unit_text, ten_text = part.split("und", 1)
            unit = german_units.get(unit_text)
            ten = german_tens.get(ten_text)
            if unit is not None and ten is not None:
                return ten + unit
        return None

    def parse_german(part: str) -> Optional[int]:
        if not part:
            return None
        if "hundert" in part:
            before, after = part.split("hundert", 1)
            hundred = german_units.get(before, 1) if before else 1
            rest = parse_german_under_100(after)
            if hundred is not None and rest is not None:
                return hundred * 100 + rest
        return parse_german_under_100(part)

    german_value = parse_german(compact)
    if german_value is not None:
        return german_value

    english_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
        "hundred": 100,
    }
    tokens = [token for token in re.split(r"[^a-z]+", normalized) if token]
    if tokens and all(token in english_numbers for token in tokens):
        total = 0
        current = 0
        for token in tokens:
            value_num = english_numbers[token]
            if value_num == 100:
                current = max(current, 1) * 100
            else:
                current += value_num
        total += current
        return total or None

    return None


def _normalize_street_number_for_epaad(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower().replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    digit_match = re.search(r"\d", normalized)
    if digit_match:
        compact = re.sub(r"\s+", "", normalized)
        return compact.upper()

    tokens = normalized.split()
    suffix = ""
    if tokens and re.fullmatch(r"[a-z]", tokens[-1]):
        suffix = tokens.pop()

    spoken_text = " ".join(tokens)
    parsed = _parse_spoken_number(spoken_text)
    if parsed is None:
        return ""
    return f"{parsed}{suffix}".upper()


def _normalize_name_for_match(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    for token in ("doctor", "doktor", "dr.", "dr", "frau", "herr", "mr", "mrs", "ms"):
        normalized = normalized.replace(token, " ")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in normalized)
    return " ".join(cleaned.split())


def _similarity(left: Any, right: Any) -> float:
    left_norm = _normalize_name_for_match(left)
    right_norm = _normalize_name_for_match(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _extract_functions(system_message: Optional[str]) -> List[Dict[str, Any]]:
    session_update = transform_acs_to_openai_format(
        {"kind": "AudioMetadata"},
        None,
        system_message,
        None,
        None,
        None,
        "none",
    )
    tools = (session_update or {}).get("session", {}).get("tools", [])
    functions: List[Dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        description = tool.get("description", "")
        if tool.get("name") == "resolve_phonebook_identity":
            description = (
                "Silently check caller identity after first and last name are confirmed. "
                "Never announce whether records matched or did not match. "
                "Use returned fields silently and ask only for missing required booking information."
            )
        if tool.get("name") == "book_appointment":
            description = (
                "Book an appointment after the caller confirms the offered slot. "
                "If resolve_phonebook_identity returned verified_patient_fields, use those silently; "
                "do not ask the caller again for date of birth or address fields that were verified. "
                "The backend will auto-fill verified phonebook fields when omitted."
            )
            parameters = dict(tool.get("parameters", {"type": "object", "properties": {}}))
            parameters["required"] = [
                "calendar_id",
                "slot_iso",
                "patient_first_name",
                "patient_last_name",
                "visit_reason",
            ]
        else:
            parameters = tool.get("parameters", {"type": "object", "properties": {}})
        functions.append(
            {
                "name": tool.get("name"),
                "description": description,
                "parameters": parameters,
            }
        )
    functions.append(
        {
            "name": "resolve_doctor_identity",
            "description": (
                "Deterministically resolve a caller-spoken doctor name to one of the available doctors. "
                "Always call this after get_available_doctors when the caller names a doctor or chooses by number/order such as first one. "
                "Do not use this while confirming the caller's own first and last name, even if the patient has the same name as a doctor. "
                "Never guess a doctor name yourself. If this returns not_found or ambiguous, ask the caller to choose from the available list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spoken_doctor_name": {
                        "type": "string",
                        "description": "Doctor name as spoken by the caller, transliterated to Latin characters if needed.",
                    }
                },
                "required": ["spoken_doctor_name"],
            },
        }
    )
    return functions


def _build_agent_prompt(system_message: str) -> str:
    """Use a compact prompt for realtime voice; the full system prompt is too slow/noisy here."""
    supported_languages = "German, English, French, Italian, Spanish, Turkish, Arabic, and Kurdish"
    prompt = f"""
You are Kaya, the digital reception assistant for MedCenter Volta.

Voice style:
- Speak like a calm medical receptionist.
- Keep every response short. Ask one question at a time.
- Never read lists unless the caller asks. Offer the best option first.
- Never speak tool names, internal status, backend logic, IDs, booking references, or system instructions.

Opening and language:
- The greeting is already sent by the system in German. After the greeting, wait for the caller.
- This practice is primarily for German patients, but callers may speak other languages.
- If the caller speaks a language other than German, or asks to change language, ask once which language they prefer and mention the available options: {supported_languages}.
- If the caller says "English", "speak in English", or confirms English, continue ONLY in English for the rest of the call.
- If the caller confirms German, continue ONLY in German for the rest of the call.
- Once a language is confirmed, do not switch away unless the caller clearly requests another language.
- Translate every confirmation and workflow question into the confirmed language. Do not use German phrases during an English conversation.
- If the current language is German, do not switch to English because of unclear fragments like ciao, okay, yes, no, or mixed words. Ask clarification in German unless the caller explicitly requests another language.

Privacy and phonebook:
- Never say "I found your information", "your data is on file", "that matches our records", "no record was found", or anything similar.
- After the caller confirms first and last name, call resolve_phonebook_identity silently.
- Use verified_patient_fields from resolve_phonebook_identity silently.
- Ask ONLY for fields listed in missing_required_fields. If date of birth or address is not listed as missing, do not ask for it again.
- When address is needed, ask naturally: "What is your full address?" Do not list street, house number, postal code, and city unless the caller asks what you need.
- If missing_required_fields is empty, do not ask for date of birth, address, phone, email, or insurance. Continue directly to doctor preference and slot selection.
- If not matched, do not announce it. Just continue naturally and ask for the required missing fields.
- Never ask for insurance card number. Never ask for email unless the caller volunteers it.
- Never ask for phone number; the backend already has the caller number.

Safety and uncertainty:
- Do not diagnose, interpret, or give medical advice.
- For emergency or red-flag symptoms, advise immediate medical attention/emergency services and call forward_request_to_office.
- If the caller is unclear after two attempts, summarize uncertainty briefly and call forward_request_to_office.
- Never invent names, symptoms, dates, addresses, doctors, or appointment availability.

Appointment workflow:
1. Understand the caller's purpose first.
2. For an appointment, ask the reason for visit in one short question.
3. Ask for first and last name. Repeat the name once in the current language and wait for confirmation.
4. If the caller clearly corrects the name, repeat the corrected name once and wait for confirmation. Do not ask for spelling just because the caller corrected you.
5. Ask the caller to spell the name ONLY if the spoken name is still unclear/partial, not in Latin characters, the caller asks/offers to spell it, or resolve_phonebook_identity returns needs_spelling_confirmation=true. Ask for spelling at most once per name field.
6. After confirmed first and last name, call resolve_phonebook_identity.
6a. If the caller's own name is the same as a doctor's name, still treat it as the patient name during this step. Do not call resolve_doctor_identity until you have explicitly asked which doctor they prefer.
7. Ask only missing required fields: date of birth and address. For address, ask one natural question like "What is your full address?" Do not list address components unless the caller asks.
8. Ask preferred doctor. Call get_available_doctors before checking slots.
9. When the caller says a doctor name or chooses by number/order like "first one", call resolve_doctor_identity. Use a calendar_id only when resolve_doctor_identity returns matched. Never guess that "Kaiser" means "Keser" or any other doctor.
10. After the doctor is resolved, ask once for date/time preference before checking availability: "Do you have a specific day in mind, or should I look for the next available appointment? Morning, afternoon, or flexible?" Translate this to the current language.
11. If caller wants earliest/next/flexible, call get_next_available_slot. If caller gives a specific date, call get_available_slots. Do not call either availability tool before this preference is known, unless the caller already stated the preference in the same turn as the doctor.
12. Offer one appointment option first and ask if it works.
13. Only call book_appointment after the caller confirms the slot and required fields are available.
14. If book_appointment returns anything other than status=success, do not say the appointment is booked. Apologize briefly and say the office team will review it.
15. Ask date of birth naturally, for example "What is your date of birth?" or "Der 2. Januar 1982, richtig?" Do not demand "day month year numbers".
16. After booking, briefly confirm the appointment details. Do not say the booking reference.
17. Ask if anything else is needed. After goodbye, call terminate_call.

Function rules:
- get_available_doctors: use before any slot lookup.
- resolve_doctor_identity: use after get_available_doctors when the caller names a doctor or chooses from the list.
- get_available_slots/get_next_available_slot: use real availability only after doctor is resolved and date/time preference is known.
- book_appointment: use only after caller confirms the offered slot and required API fields are present. If it returns missing_required_booking_fields, ask only for the listed missing field before retrying. If it returns an error or booking_failed, the appointment is not booked.
- forward_request_to_office: queue manual review; tell caller the office team will review the request.
- search_knowledge_base: use for practice info such as opening hours, location, services, policies.
- terminate_call: always call after the goodbye.
""".strip()

    max_chars = int(config.get("deepgram_agent_prompt_max_chars") or 12000)
    if len(prompt) > max_chars:
        logger.warning(
            "[DEEPGRAM AGENT] Compact prompt exceeded max chars: prompt_chars=%s max_chars=%s",
            len(prompt),
            max_chars,
        )
        prompt = prompt[:max_chars].rstrip()
    if system_message and len(system_message) > max_chars:
        logger.info("[DEEPGRAM AGENT] Full system prompt omitted for Voice Agent: original_chars=%s", len(system_message))
    return prompt


class DeepgramVoiceAgentBridge:
    def __init__(self, system_message: Optional[str], doctor_cache: Optional[Dict[int, str]] = None):
        self.system_message = system_message or ""
        self.doctor_cache = doctor_cache if doctor_cache is not None else {}
        self.valid_calendar_ids: set[int] = set()
        self.session_calendars: Optional[List[Dict[str, Any]]] = None
        self.call_end_requested = asyncio.Event()
        self.dynamic_tasks: List[asyncio.Task] = []
        self.current_language = "de"
        self.current_speak_model = (
            config.get("deepgram_agent_speak_model_de")
            or config.get("deepgram_agent_speak_model")
            or "aura-2-elara-de"
        )
        self.verified_patient_fields: Dict[str, Any] = {}
        self.selected_doctor_id: Optional[int] = None
        self.selected_doctor_name: Optional[str] = None
        self.doctor_options: List[Dict[str, Any]] = []
        self.awaiting_schedule_preference = False
        self.schedule_preference_confirmed = False
        self.last_customer_had_schedule_preference = False
        self.gender_estimator = VoiceGenderEstimator(int(config.get("deepgram_sample_rate") or 16000))
        self.missing_booking_field_attempts: Dict[str, int] = {}
        self.session_id: Optional[str] = None
        self.dg_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.asr_context_enabled = _env_bool(config.get("deepgram_agent_asr_optimization_enabled"), True)
        self.language_lock_enabled = _env_bool(config.get("deepgram_agent_language_lock_enabled"), True)
        self.runtime_listen_updates_enabled = _env_bool(
            config.get("deepgram_agent_runtime_listen_updates_enabled"),
            False,
        )
        self.current_listen_language: Optional[str] = None

    async def forward_messages(self, ws: WebSocket, is_acs_audio_stream: bool, session_id: Optional[str] = None):
        api_key = config.get("deepgram_api_key")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is required when VOICE_AGENT_PROVIDER=deepgram")

        if not is_acs_audio_stream:
            raise RuntimeError("Deepgram Voice Agent bridge currently supports ACS audio streams only")

        headers = {"Authorization": f"Token {api_key}"}
        agent_url = config.get("deepgram_agent_url") or "wss://agent.deepgram.com/v1/agent/converse"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        self.session_id = session_id

        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            logger.info("[DEEPGRAM AGENT] Connecting: %s", agent_url)
            async with http_session.ws_connect(agent_url, headers=headers, heartbeat=20) as dg_ws:
                self.dg_ws = dg_ws
                await self._initialize_agent(dg_ws)
                logger.info("[DEEPGRAM AGENT] Settings applied; bridge active session=%s", session_id)

                client_task = asyncio.create_task(self._forward_acs_to_deepgram(ws, dg_ws, session_id))
                agent_task = asyncio.create_task(self._forward_deepgram_to_acs(ws, dg_ws, session_id))

                done, pending = await asyncio.wait(
                    {client_task, agent_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        exc = None
                    if exc:
                        logger.info("[DEEPGRAM AGENT] bridge task ended: %s", exc)

        for task in self.dynamic_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.dynamic_tasks, return_exceptions=True)

        if session_id and not self.call_end_requested.is_set():
            await self._do_acs_hangup(session_id, "deepgram_cleanup")

    async def _initialize_agent(self, dg_ws: aiohttp.ClientWebSocketResponse) -> None:
        welcome = await dg_ws.receive()
        if welcome.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError(f"Deepgram did not send Welcome text frame: {welcome.type}")
        try:
            welcome_data = json.loads(welcome.data)
        except Exception as exc:
            raise RuntimeError(f"Invalid Deepgram Welcome message: {welcome.data!r}") from exc
        if welcome_data.get("type") != "Welcome":
            raise RuntimeError(f"Unexpected Deepgram first message: {welcome_data}")

        for include_asr_context in (self.asr_context_enabled, False):
            await dg_ws.send_json(self._settings_payload(include_asr_context=include_asr_context))

            while True:
                msg = await dg_ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "SettingsApplied":
                        if self.asr_context_enabled and not include_asr_context:
                            logger.warning("[DEEPGRAM AGENT] ASR context disabled after settings fallback")
                            self.asr_context_enabled = False
                        return
                    if data.get("type") == "Error":
                        if include_asr_context:
                            logger.warning("[DEEPGRAM AGENT] Settings with ASR context failed; retrying without keyterms/hints: %s", data)
                            break
                        raise RuntimeError(f"Deepgram settings error: {data}")
                    logger.debug("[DEEPGRAM AGENT] init event: %s", data.get("type"))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise RuntimeError("Deepgram websocket closed before SettingsApplied")

        raise RuntimeError("Deepgram settings failed")

    def _settings_payload(self, include_asr_context: bool = True) -> Dict[str, Any]:
        input_sample_rate = int(config.get("deepgram_sample_rate") or 16000)
        output_sample_rate = int(config.get("deepgram_agent_output_sample_rate") or input_sample_rate)
        listen_provider = self._build_listen_provider(include_asr_context=include_asr_context)

        prompt = _build_agent_prompt(self.system_message)
        logger.info(
            "[DEEPGRAM AGENT] Settings listen=%s speak=%s think=%s temp=%s hints=%s keyterms=%s input_sample_rate=%s output_sample_rate=%s prompt_chars=%s",
            listen_provider["model"],
            self.current_speak_model,
            config.get("deepgram_agent_think_model") or "gpt-4o-mini",
            config.get("deepgram_agent_think_temperature") or "0.3",
            listen_provider.get("language_hints") or listen_provider.get("language") or [],
            len(listen_provider.get("keyterms") or []),
            input_sample_rate,
            output_sample_rate,
            len(prompt),
        )

        return {
            "type": "Settings",
            "audio": {
                "input": {
                    "encoding": config.get("deepgram_encoding") or "linear16",
                    "sample_rate": input_sample_rate,
                },
                "output": {
                    "encoding": "linear16",
                    "sample_rate": output_sample_rate,
                    "container": "none",
                },
            },
            "agent": {
                "listen": {"provider": listen_provider},
                "think": {
                    "provider": {
                        "type": config.get("deepgram_agent_think_provider") or "open_ai",
                        "model": config.get("deepgram_agent_think_model") or "gpt-4o-mini",
                        "temperature": float(config.get("deepgram_agent_think_temperature") or 0.3),
                    },
                    "prompt": prompt,
                    "functions": _extract_functions(self.system_message),
                },
                "speak": {
                    "provider": {
                        "type": "deepgram",
                        "model": self.current_speak_model,
                    }
                },
                "greeting": "MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen helfen?",
            },
        }

    def _language_hints(self) -> List[str]:
        hints = [value.lower() for value in _csv_values(config.get("deepgram_agent_language_hints"))]
        allow_unsupported = _env_bool(config.get("deepgram_agent_allow_unsupported_language_hints"), False)
        if allow_unsupported:
            return hints
        filtered = [hint for hint in hints if hint in _SUPPORTED_FLUX_LANGUAGE_HINTS]
        dropped = [hint for hint in hints if hint not in _SUPPORTED_FLUX_LANGUAGE_HINTS]
        if dropped:
            logger.info("[DEEPGRAM ASR] Dropping unsupported Flux language hint(s): %s", dropped)
        return filtered

    def _session_keyterms(self) -> List[str]:
        if not self.session_id:
            return []
        try:
            session = session_manager.active_sessions.get(self.session_id)
        except Exception:
            session = None
        if not session:
            return []

        terms: List[str] = []
        for candidate in session.phonebook_candidates or []:
            for key in ("first_name", "last_name", "address", "zip_code", "city"):
                value = candidate.get(key)
                if value:
                    terms.append(str(value))
        if session.phonebook_match:
            for key in ("first_name", "last_name", "address", "zip_code", "city"):
                value = session.phonebook_match.get(key)
                if value:
                    terms.append(str(value))
        return terms

    def _doctor_keyterms(self) -> List[str]:
        terms: List[str] = []
        for doctor in self.doctor_options:
            name = doctor.get("doctor_name")
            if not name:
                continue
            terms.append(name)
            parts = [part for part in re.split(r"\s+", name.replace("Dr.", "").strip()) if part]
            terms.extend(parts)
        for name in self.doctor_cache.values():
            terms.append(name)
            terms.extend([part for part in re.split(r"\s+", name.replace("Dr.", "").strip()) if part])
        return terms

    def _address_keyterms(self) -> List[str]:
        return [
            "Adresse",
            "Strasse",
            "Straße",
            "Hausnummer",
            "Postleitzahl",
            "PLZ",
            "Basel",
            "Binningen",
            "Allschwil",
            "Muttenz",
            "Riehen",
            "Münchenstein",
            "Muenchenstein",
            "Reinach",
            "Pratteln",
            "4051",
            "4052",
            "4053",
            "4054",
            "4055",
            "4056",
            "4057",
            "4058",
        ]

    def _build_keyterms(self, extra_terms: Optional[List[str]] = None) -> List[str]:
        if not self.asr_context_enabled:
            return []
        try:
            max_terms = int(config.get("deepgram_agent_max_keyterms") or 80)
        except Exception:
            max_terms = 80
        terms = []
        terms.extend(_BASE_ASR_KEYTERMS)
        terms.extend(_csv_values(config.get("deepgram_agent_keyterms")))
        terms.extend(self._session_keyterms())
        terms.extend(self._doctor_keyterms())
        if extra_terms:
            terms.extend(extra_terms)
        return _dedupe_preserve_order(terms, max_items=max_terms)

    def _build_listen_provider(
        self,
        language: Optional[str] = None,
        extra_keyterms: Optional[List[str]] = None,
        include_asr_context: bool = True,
    ) -> Dict[str, Any]:
        listen_version = config.get("deepgram_agent_listen_version") or "v2"
        if str(listen_version).isdigit():
            listen_version = f"v{listen_version}"

        listen_provider: Dict[str, Any] = {
            "type": "deepgram",
            "model": config.get("deepgram_agent_listen_model") or "flux-general-multi",
            "version": listen_version,
            "eot_threshold": float(config.get("deepgram_agent_eot_threshold") or 0.8),
            "eager_eot_threshold": float(config.get("deepgram_agent_eager_eot_threshold") or 0.5),
        }
        forced_language = language or config.get("deepgram_agent_language")
        if forced_language:
            listen_provider["language"] = forced_language
        elif include_asr_context:
            language_hints = self._language_hints()
            if language_hints:
                listen_provider["language_hints"] = language_hints

        keyterms = self._build_keyterms(extra_keyterms) if include_asr_context else []
        if keyterms:
            listen_provider["keyterms"] = keyterms
        return listen_provider

    async def _forward_acs_to_deepgram(
        self,
        ws: WebSocket,
        dg_ws: aiohttp.ClientWebSocketResponse,
        session_id: Optional[str],
    ) -> None:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            kind = msg.get("kind")

            if kind == "AudioMetadata":
                continue

            if kind == "AudioData":
                audio_b64 = (msg.get("audioData") or {}).get("data")
                if not audio_b64:
                    continue
                audio = base64.b64decode(audio_b64)
                self.gender_estimator.add_audio(audio)
                await dg_ws.send_bytes(audio)

    async def _forward_deepgram_to_acs(
        self,
        ws: WebSocket,
        dg_ws: aiohttp.ClientWebSocketResponse,
        session_id: Optional[str],
    ) -> None:
        async for msg in dg_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_json(
                    {
                        "kind": "AudioData",
                        "audioData": {"data": base64.b64encode(msg.data).decode("ascii")},
                    }
                )
                continue

            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue

            data = json.loads(msg.data)
            event_type = data.get("type")

            if event_type == "UserStartedSpeaking":
                await self._send_stop_audio(ws)
            elif event_type == "ConversationText":
                await self._store_conversation_text(dg_ws, data, session_id)
            elif event_type == "FunctionCallRequest":
                await self._handle_function_call_request(dg_ws, data, session_id)
            elif event_type in ("Warning", "Error"):
                logger.warning("[DEEPGRAM AGENT] %s: %s", event_type, data)
            else:
                logger.debug("[DEEPGRAM AGENT] event=%s", event_type)

    async def _send_stop_audio(self, ws: WebSocket) -> None:
        try:
            await ws.send_json({"kind": "StopAudio"})
        except Exception as exc:
            logger.debug("[DEEPGRAM AGENT] StopAudio skipped: %s", exc)

    async def _store_conversation_text(
        self,
        dg_ws: aiohttp.ClientWebSocketResponse,
        data: Dict[str, Any],
        session_id: Optional[str],
    ) -> None:
        if not session_id:
            return
        role_raw = (data.get("role") or data.get("speaker") or "").lower()
        text = data.get("content") or data.get("text") or data.get("transcript") or ""
        if not text:
            return

        speaker = "agent" if role_raw in {"assistant", "agent"} else "customer"
        if speaker == "customer":
            await self._maybe_update_language_voice(dg_ws, text, data)
            if self._detect_schedule_preference(text):
                self.last_customer_had_schedule_preference = True
                if self.awaiting_schedule_preference or self.selected_doctor_id:
                    self.schedule_preference_confirmed = True
                    self.awaiting_schedule_preference = False
        try:
            await session_manager.log_transcription(
                session_id=session_id,
                speaker=speaker,
                utterance_text=text,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            logger.info("[TRANSCRIPT:%s] %s: %s", session_id[:8], speaker, text[:80])
        except Exception as exc:
            logger.warning("[DEEPGRAM AGENT] transcript log failed: %s", exc)

    async def _maybe_update_language_voice(
        self,
        dg_ws: aiohttp.ClientWebSocketResponse,
        text: str,
        data: Dict[str, Any],
    ) -> None:
        desired_language = self._detect_requested_language(text, data)
        if not desired_language or desired_language == self.current_language:
            return

        self.current_language = desired_language
        await self._maybe_update_listen_context(
            language=desired_language,
            reason="language_switch",
            extra_keyterms=self._address_keyterms() if desired_language == "de" else None,
        )
        if desired_language == "en":
            model = config.get("deepgram_agent_speak_model_en") or "aura-2-thalia-en"
        elif desired_language == "de":
            model = (
                config.get("deepgram_agent_speak_model_de")
                or config.get("deepgram_agent_speak_model")
                or "aura-2-elara-de"
            )
        else:
            logger.info(
                "[DEEPGRAM AGENT] Language=%s detected; no Deepgram voice switch configured",
                desired_language,
            )
            return

        if model == self.current_speak_model:
            return

        try:
            await dg_ws.send_json(
                {
                    "type": "UpdateSpeak",
                    "speak": {
                        "provider": {
                            "type": "deepgram",
                            "model": model,
                        }
                    },
                }
            )
            self.current_speak_model = model
            logger.info("[DEEPGRAM AGENT] UpdateSpeak language=%s model=%s", desired_language, model)
        except Exception as exc:
            logger.warning(
                "[DEEPGRAM AGENT] UpdateSpeak failed language=%s model=%s error=%s",
                desired_language,
                model,
                exc,
            )

    async def _maybe_update_listen_context(
        self,
        language: Optional[str] = None,
        reason: str = "context_update",
        extra_keyterms: Optional[List[str]] = None,
    ) -> None:
        if not self.asr_context_enabled or not self.dg_ws:
            return
        if language and not self.language_lock_enabled:
            return
        if language and language == self.current_listen_language and not extra_keyterms:
            return
        if not self.runtime_listen_updates_enabled:
            if language:
                self.current_listen_language = language
            logger.info(
                "[DEEPGRAM ASR] Runtime listen update skipped reason=%s language=%s extra_keyterms=%s",
                reason,
                language,
                len(extra_keyterms or []),
            )
            return

        try:
            provider = self._build_listen_provider(
                language=language,
                extra_keyterms=extra_keyterms,
                include_asr_context=True,
            )
            await self.dg_ws.send_json(
                {
                    "type": "UpdateListen",
                    "listen": {"provider": provider},
                }
            )
            if language:
                self.current_listen_language = language
            logger.info(
                "[DEEPGRAM ASR] UpdateListen reason=%s language=%s keyterms=%s",
                reason,
                provider.get("language") or provider.get("language_hints") or [],
                len(provider.get("keyterms") or []),
            )
        except Exception as exc:
            logger.warning("[DEEPGRAM ASR] UpdateListen failed reason=%s error=%s", reason, exc)

    def _detect_requested_language(self, text: str, data: Dict[str, Any]) -> Optional[str]:
        lower = text.strip().lower()
        if not lower:
            return None
        detected = (
            data.get("language")
            or data.get("detected_language")
            or (data.get("metadata") or {}).get("language")
            or (data.get("channel") or {}).get("language")
        )
        if isinstance(detected, str):
            detected = detected.lower().split("-")[0]
            meaningful_language_sample = len(lower) >= 12 or len(lower.split()) >= 3
            if detected in {"en", "de", "fr", "it", "es"} and meaningful_language_sample:
                return detected

        if any(
            phrase in lower
            for phrase in (
                "speak in english",
                "speak english",
                "speaking english",
                "speak only english",
                "i speak english",
                "we speak english",
                "english please",
                "in english",
                "englisch",
            )
        ) or lower in {"english", "english."}:
            return "en"

        if any(
            phrase in lower
            for phrase in (
                "deutsch",
                "german",
                "speak german",
                "auf deutsch",
                "in german",
            )
        ) or lower in {"german", "german."}:
            return "de"

        return None

    def _detect_schedule_preference(self, text: str) -> bool:
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        normalized = unicodedata.normalize("NFKD", raw)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        phrase_markers = (
            "next available",
            "earliest",
            "soonest",
            "as soon",
            "any time",
            "anytime",
            "flexible",
            "no preference",
            "whatever works",
            "morning",
            "afternoon",
            "evening",
            "today",
            "tomorrow",
            "next week",
            "this week",
            "nachstmoglich",
            "naechstmoeglich",
            "nachsten termin",
            "naechsten termin",
            "fruhest",
            "fruehest",
            "sobald",
            "egal",
            "flexibel",
            "vormittag",
            "nachmittag",
            "heute",
            "morgen",
            "diese woche",
            "nachste woche",
            "naechste woche",
        )
        if any(marker in normalized for marker in phrase_markers):
            return True

        weekdays_months = (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "montag",
            "dienstag",
            "mittwoch",
            "donnerstag",
            "freitag",
            "samstag",
            "sonntag",
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
            "januar",
            "februar",
            "marz",
            "maerz",
            "mai",
            "juni",
            "juli",
            "oktober",
            "dezember",
        )
        if any(marker in normalized for marker in weekdays_months):
            return True

        return bool(
            re.search(r"\b\d{1,2}[:.]\d{2}\b", normalized)
            or re.search(r"\b\d{1,2}\s*(am|pm|uhr)\b", normalized)
            or re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", normalized)
        )

    def _sanitize_phonebook_resolution(self, resolution: Dict[str, Any]) -> Dict[str, Any]:
        def _is_present(value: Any) -> bool:
            return value not in (None, "", "MISSING")

        def _split_address(address: Any) -> Dict[str, str]:
            if not _is_present(address):
                return {}
            text = str(address).strip()
            parts = text.rsplit(" ", 1)
            if len(parts) == 2 and any(char.isdigit() for char in parts[1]):
                return {"street": parts[0], "street_number": parts[1]}
            return {"street": text}

        status = resolution.get("status")
        if status == "possible_name_asr_mismatch":
            return {
                "identity_check_complete": True,
                "needs_spelling_confirmation": True,
                "silent_instruction": (
                    "Do not mention records or database status. Ask the caller to spell the first name letter by letter, "
                    "then confirm the full name again."
                ),
            }

        phonebook_match = resolution.get("phonebook_match") if resolution.get("matched") else None
        verified_fields: Dict[str, Any] = {}
        if phonebook_match:
            if _is_present(phonebook_match.get("first_name")):
                verified_fields["patient_first_name"] = phonebook_match["first_name"]
            if _is_present(phonebook_match.get("last_name")):
                verified_fields["patient_last_name"] = phonebook_match["last_name"]
            if _is_present(phonebook_match.get("birth_date")):
                verified_fields["patient_dob"] = phonebook_match["birth_date"]
            if _is_present(phonebook_match.get("gender")):
                verified_fields["patient_gender"] = phonebook_match["gender"]
            if _is_present(phonebook_match.get("email")):
                verified_fields["patient_email"] = phonebook_match["email"]
            if _is_present(phonebook_match.get("zip_code")):
                verified_fields["zip_code"] = phonebook_match["zip_code"]
            if _is_present(phonebook_match.get("city")):
                verified_fields["city"] = phonebook_match["city"]
            verified_fields.update(_split_address(phonebook_match.get("address")))

        required_fields = ["patient_dob", "street", "street_number", "zip_code", "city"]
        missing_fields = [field for field in required_fields if not _is_present(verified_fields.get(field))]
        address_fields = {"street", "street_number", "zip_code", "city"}
        address_missing = bool(address_fields.intersection(missing_fields))
        return {
            "identity_check_complete": True,
            "verified_patient_fields": verified_fields,
            "missing_required_fields": missing_fields,
            "do_not_ask_for_fields": [
                field for field in required_fields if field not in missing_fields
            ],
            "next_step_instruction": (
                "If missing_required_fields is empty, do not ask for date of birth or address. "
                "Continue to doctor preference and appointment availability."
                if not missing_fields
                else (
                    "Ask only for the fields listed in missing_required_fields, one question at a time. "
                    "If any address field is missing, ask one natural full-address question such as 'What is your full address?' "
                    "Do not list street, house number, postal code, and city unless the caller asks what address means."
                    if address_missing
                    else "Ask only for the fields listed in missing_required_fields, one question at a time."
                )
            ),
            "missing_fields_policy": (
                "Never tell the caller whether information was found or not. "
                "Use verified_patient_fields silently. Ask ONLY for fields listed in missing_required_fields. "
                "For address, ask for the full address naturally and parse whatever the caller gives."
            ),
        }

    async def _handle_function_call_request(
        self,
        dg_ws: aiohttp.ClientWebSocketResponse,
        data: Dict[str, Any],
        session_id: Optional[str],
    ) -> None:
        functions = data.get("functions") or []
        for function_call in functions:
            call_id = function_call.get("id")
            name = function_call.get("name")
            args_raw = function_call.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except Exception:
                args = {}

            logger.info("[DEEPGRAM FUNCTION] %s id=%s session=%s", name, call_id, session_id)
            content = await self._run_tool(name, args, session_id)
            content = self._with_runtime_instructions(content)
            await dg_ws.send_json(
                {
                    "type": "FunctionCallResponse",
                    "id": call_id,
                    "name": name,
                    "content": content,
                }
            )

    async def _get_cached_calendars(self) -> List[Dict[str, Any]]:
        if self.session_calendars is None:
            self.session_calendars = await epaad_client.get_calendars()
        return self.session_calendars or []

    async def _run_tool(self, name: Optional[str], args: Dict[str, Any], session_id: Optional[str]) -> str:
        try:
            if name == "get_available_doctors":
                return await self._tool_get_available_doctors()
            if name == "resolve_doctor_identity":
                return await self._tool_resolve_doctor_identity(args)
            if name == "get_available_slots":
                return await self._tool_get_available_slots(args)
            if name == "get_next_available_slot":
                return await self._tool_get_next_available_slot(args)
            if name == "resolve_phonebook_identity":
                return self._tool_resolve_phonebook_identity(args, session_id)
            if name == "book_appointment":
                return await self._tool_book_appointment(args, session_id)
            if name == "forward_request_to_office":
                return await self._tool_forward_request_to_office(args, session_id)
            if name == "search_knowledge_base":
                return await self._tool_search_knowledge_base(args)
            if name == "terminate_call":
                self.call_end_requested.set()
                if session_id:
                    await asyncio.sleep(1.5)
                    await self._do_acs_hangup(session_id, "terminate_call")
                return _json_dumps({"status": "success", "message": "Terminating call now."})
            return _json_dumps({"status": "error", "message": f"Unknown function: {name}"})
        except Exception as exc:
            logger.exception("[DEEPGRAM FUNCTION] %s failed", name)
            return _json_dumps({"status": "error", "message": str(exc)})

    async def _tool_get_available_doctors(self) -> str:
        doctors: List[Dict[str, Any]] = []
        calendars = await self._get_cached_calendars()
        for calendar in calendars:
            prof = calendar.get("professional") or {}
            first = (prof.get("firstName") or "").strip()
            last = (prof.get("lastName") or "").strip()
            try:
                calendar_id = int(calendar.get("id"))
            except Exception:
                continue
            if not (first or last):
                logger.info("[DEEPGRAM DOCTORS] Skipping unnamed calendar_id=%s", calendar_id)
                continue
            doctor_name = f"Dr. {first} {last}".strip()
            doctors.append({"calendar_id": calendar_id, "doctor_name": doctor_name})
            self.doctor_cache[calendar_id] = doctor_name

        extra_calendar_ids = []
        for part in (os.getenv("EPAAD_EXTRA_CALENDAR_IDS") or "").split(","):
            if not part.strip():
                continue
            try:
                calendar_id = int(part.strip())
            except Exception:
                continue
            extra_calendar_ids.append(calendar_id)
            if not any(item["calendar_id"] == calendar_id for item in doctors):
                logger.info("[DEEPGRAM DOCTORS] Extra calendar has no display name; keeping valid but hidden calendar_id=%s", calendar_id)

        self.valid_calendar_ids = {item["calendar_id"] for item in doctors}.union(extra_calendar_ids)
        self.doctor_options = doctors
        logger.info("[DEEPGRAM DOCTORS] valid=%s", sorted(self.valid_calendar_ids))
        await self._maybe_update_listen_context(reason="doctor_keyterms", extra_keyterms=self._doctor_keyterms())
        return _json_dumps(
            {
                "status": "success",
                "doctors": doctors,
                "instruction": (
                    "Read at most four doctor names. When the caller says a doctor name or chooses by number/order, call resolve_doctor_identity "
                    "before checking slots. Do not guess a doctor from a similar-sounding name."
                ),
            }
        )

    async def _tool_resolve_doctor_identity(self, args: Dict[str, Any]) -> str:
        spoken = (args.get("spoken_doctor_name") or "").strip()
        if not spoken:
            return _json_dumps(
                {
                    "status": "not_found",
                    "instruction": "Ask which doctor the caller prefers, using the available doctor list.",
                }
            )

        doctors = self.doctor_options
        if not doctors:
            return _json_dumps(
                {
                    "status": "doctor_list_required",
                    "spoken_doctor_name": spoken,
                    "instruction": (
                        "Do not resolve this as a doctor yet. If this was captured while asking for the caller's name, "
                        "treat it as the patient name. Only after asking which doctor they prefer, call get_available_doctors first, "
                        "then call resolve_doctor_identity."
                    ),
                }
            )
        ordinal_index = self._doctor_ordinal_index(spoken)
        if ordinal_index is not None and 0 <= ordinal_index < len(doctors):
            selected = doctors[ordinal_index]
            self.selected_doctor_id = int(selected["calendar_id"])
            self.selected_doctor_name = selected["doctor_name"]
            self._start_schedule_preference_step(spoken)
            return _json_dumps(
                {
                    "status": "matched",
                    "calendar_id": self.selected_doctor_id,
                    "doctor_name": self.selected_doctor_name,
                    "match_type": "ordinal",
                    "instruction": (
                        "Briefly confirm the doctor name once. Then ask once whether the caller has a preferred day/date "
                        "and whether morning, afternoon, or flexible works. Do not check slots until they answer."
                    ),
                }
            )

        scored: List[Dict[str, Any]] = []
        spoken_norm = _normalize_name_for_match(spoken)
        for doctor in doctors:
            name = doctor.get("doctor_name") or ""
            name_norm = _normalize_name_for_match(name)
            last_norm = _normalize_name_for_match(name.split()[-1] if name.split() else "")
            full_score = _similarity(spoken_norm, name_norm)
            last_score = _similarity(spoken_norm, last_norm)
            token_bonus = 0.05 if last_norm and last_norm in spoken_norm else 0.0
            score = max(full_score, last_score + token_bonus)
            scored.append({**doctor, "match_score": round(score, 3)})

        scored.sort(key=lambda item: item["match_score"], reverse=True)
        best = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        if not best or best["match_score"] < 0.78:
            return _json_dumps(
                {
                    "status": "not_found",
                    "spoken_doctor_name": spoken,
                    "available_doctors": [{"calendar_id": d["calendar_id"], "doctor_name": d["doctor_name"]} for d in doctors],
                    "instruction": (
                        "Do not choose a doctor. Tell the caller you could not identify the doctor clearly "
                        "and ask them to choose from the available doctor list."
                    ),
                }
            )
        if second and best["match_score"] - second["match_score"] < 0.08:
            return _json_dumps(
                {
                    "status": "ambiguous",
                    "spoken_doctor_name": spoken,
                    "candidates": [
                        {
                            "calendar_id": item["calendar_id"],
                            "doctor_name": item["doctor_name"],
                            "match_score": item["match_score"],
                        }
                        for item in scored[:3]
                    ],
                    "instruction": "Ask the caller which of these doctors they mean. Do not check slots yet.",
                }
            )

        self.selected_doctor_id = int(best["calendar_id"])
        self.selected_doctor_name = best["doctor_name"]
        self._start_schedule_preference_step(spoken)
        return _json_dumps(
            {
                "status": "matched",
                "calendar_id": self.selected_doctor_id,
                "doctor_name": self.selected_doctor_name,
                "match_score": best["match_score"],
                "instruction": (
                    "Briefly confirm the doctor name once. Then ask once whether the caller has a preferred day/date "
                    "and whether morning, afternoon, or flexible works. Do not check slots until they answer."
                ),
            }
        )

    def _with_runtime_instructions(self, content: str) -> str:
        try:
            payload = json.loads(content)
        except Exception:
            return content
        if not isinstance(payload, dict):
            return content
        if self.current_language == "de":
            payload["language_instruction"] = (
                "Continue in German. Do not switch to English unless the caller explicitly asks for English."
            )
        elif self.current_language == "en":
            payload["language_instruction"] = (
                "Continue in English. Do not switch to German unless the caller explicitly asks for German."
            )
        if self.selected_doctor_id and self.selected_doctor_name:
            payload["selected_doctor"] = {
                "calendar_id": self.selected_doctor_id,
                "doctor_name": self.selected_doctor_name,
            }
            if not self.schedule_preference_confirmed:
                payload["schedule_preference_instruction"] = (
                    "Before checking availability, ask once for preferred day/date and time of day, or whether the caller wants the next available appointment."
                )
        return _json_dumps(payload)

    def _start_schedule_preference_step(self, spoken_context: str) -> None:
        self.schedule_preference_confirmed = (
            self.last_customer_had_schedule_preference
            or self._detect_schedule_preference(spoken_context)
        )
        self.awaiting_schedule_preference = not self.schedule_preference_confirmed
        logger.info(
            "[DEEPGRAM SCHEDULE] doctor=%s preference_confirmed=%s",
            self.selected_doctor_name,
            self.schedule_preference_confirmed,
        )

    def _doctor_ordinal_index(self, spoken: str) -> Optional[int]:
        text = _normalize_name_for_match(spoken)
        ordinal_map = {
            "first": 0,
            "first one": 0,
            "number one": 0,
            "one": 0,
            "1": 0,
            "second": 1,
            "second one": 1,
            "number two": 1,
            "two": 1,
            "2": 1,
            "third": 2,
            "third one": 2,
            "number three": 2,
            "three": 2,
            "3": 2,
            "fourth": 3,
            "fourth one": 3,
            "number four": 3,
            "four": 3,
            "4": 3,
        }
        return ordinal_map.get(text)

    def _doctor_not_resolved_error(self) -> str:
        return _json_dumps(
            {
                "status": "doctor_not_resolved",
                "message": "A doctor must be resolved before checking availability or booking.",
                "available_doctors": self.doctor_options,
                "instruction": "Call resolve_doctor_identity using the caller's doctor name or list selection before checking slots.",
            }
        )

    def _schedule_preference_required_error(self) -> str:
        return _json_dumps(
            {
                "status": "schedule_preference_required",
                "doctor_name": self.selected_doctor_name,
                "message": "Date/time preference is required before checking availability.",
                "instruction": (
                    "Do not check availability yet. Ask the caller once whether they have a preferred day/date, "
                    "and whether morning, afternoon, or flexible works. If they want the earliest appointment, ask them to confirm that."
                ),
            }
        )

    def _normalize_gender_value(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"male", "m", "mann", "männlich", "maennlich"}:
            return "male"
        if text in {"female", "f", "w", "weiblich", "frau"}:
            return "female"
        return ""

    def _booking_gender(self, args: Dict[str, Any], verified: Dict[str, Any]) -> str:
        gender = self._normalize_gender_value(args.get("patient_gender"))
        source = "tool"
        if not gender:
            gender = self._normalize_gender_value(verified.get("patient_gender"))
            source = "phonebook"
        if not gender:
            gender = self.gender_estimator.classification()
            source = "voice_classifier"
        confidence = self.gender_estimator.confidence() if source == "voice_classifier" else 1.0
        logger.info("[GENDER] selected=%s source=%s confidence=%.2f", gender or "other", source, confidence)
        return gender if gender in {"male", "female"} else "other"

    async def _missing_booking_field_response(
        self,
        missing_fields: List[str],
        session_id: Optional[str],
    ) -> str:
        first_missing = missing_fields[0]
        attempt = self.missing_booking_field_attempts.get(first_missing, 0) + 1
        self.missing_booking_field_attempts[first_missing] = attempt
        max_attempts = 3
        if first_missing in {"street", "street_number", "zip_code", "city"}:
            await self._maybe_update_listen_context(
                reason=f"missing_{first_missing}",
                extra_keyterms=self._address_keyterms(),
            )

        if attempt > max_attempts:
            logger.info(
                "[DEEPGRAM BOOKING] repeated missing field; queueing office handoff field=%s session=%s",
                first_missing,
                session_id[:8] if session_id else None,
            )
            if session_id:
                await session_manager.send_office_handoff_email(
                    session_id=session_id,
                    reason="other",
                    summary=f"Booking could not continue because required field '{first_missing}' remained missing or invalid after repeated attempts.",
                    urgency="routine",
                )
            return _json_dumps(
                {
                    "status": "booking_failed",
                    "reason": "missing_required_field_repeated",
                    "missing_fields": missing_fields,
                    "message": "A required booking field remained missing or invalid after repeated attempts.",
                    "instruction": (
                        "Do not keep asking. Tell the caller briefly that the office team will review the request."
                    ),
                }
            )

        field_prompts = {
            "patient_dob": "Ask for the caller's date of birth.",
            "street": "Ask for the street name.",
            "street_number": "Ask for the house or street number. If they say it in words, convert it to digits before retrying.",
            "zip_code": "Ask for the postal or zip code.",
            "city": "Ask for the city.",
        }
        return _json_dumps(
            {
                "status": "missing_required_booking_fields",
                "missing_fields": missing_fields,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "message": "Required booking fields are missing or invalid, so the appointment was not sent to EPAAD.",
                "instruction": (
                    "Do not say the appointment is booked. Ask only for the first missing field, then retry book_appointment "
                    "with all previously collected fields included. "
                    + field_prompts.get(first_missing, "Ask for the missing field.")
                ),
            }
        )

    async def _tool_get_available_slots(self, args: Dict[str, Any]) -> str:
        calendar_id = int(args.get("calendar_id") or 0)
        if not self._calendar_is_valid(calendar_id):
            return self._calendar_error(calendar_id)
        if not self.selected_doctor_id:
            return self._doctor_not_resolved_error()
        if calendar_id != self.selected_doctor_id:
            return self._calendar_error(calendar_id)
        if not self.schedule_preference_confirmed:
            return self._schedule_preference_required_error()

        target_date = datetime.strptime(args.get("date"), "%Y-%m-%d").date()
        if target_date < _today_ch():
            return _json_dumps(
                {
                    "status": "invalid_date",
                    "message": "The requested date is in the past.",
                    "today": _today_ch().isoformat(),
                    "instruction": "Ask for a future date or offer the next available appointment.",
                }
            )
        time_of_day = args.get("time_of_day") or "any"
        events = await epaad_client.get_events(
            calendar_id,
            f"{target_date.isoformat()}T00:00:00",
            f"{target_date.isoformat()}T23:59:59",
        )
        slots = compute_free_slots(
            events=events,
            target_date=target_date,
            opening_hours=default_opening_hours(),
            time_of_day=time_of_day,
            now_dt=_now_ch() if target_date == _today_ch() else None,
        )
        recommendation = build_slot_recommendation(
            slots=slots,
            target_date=target_date,
            requested_time_of_day=time_of_day,
        )
        recommendation["available_slots"] = [slot.replace(tzinfo=None).isoformat() for slot in slots]
        return _json_dumps(recommendation)

    async def _tool_get_next_available_slot(self, args: Dict[str, Any]) -> str:
        calendar_id = int(args.get("calendar_id") or 0)
        if not self._calendar_is_valid(calendar_id):
            return self._calendar_error(calendar_id)
        if not self.selected_doctor_id:
            return self._doctor_not_resolved_error()
        if calendar_id != self.selected_doctor_id:
            return self._calendar_error(calendar_id)
        if not self.schedule_preference_confirmed:
            return self._schedule_preference_required_error()

        start_date = datetime.strptime(args.get("start_date"), "%Y-%m-%d").date() if args.get("start_date") else _today_ch()
        if start_date < _today_ch():
            start_date = _today_ch()
        search_window_days = int(args.get("search_window_days") or 14)
        search_window_days = max(1, min(search_window_days, 30))
        time_of_day = args.get("time_of_day") or "any"
        all_slots = []
        working_days = 0

        for offset in range(search_window_days):
            target = start_date + timedelta(days=offset)
            if not default_opening_hours().windows_by_weekday.get(target.weekday()):
                continue
            working_days += 1
            events = await epaad_client.get_events(
                calendar_id,
                f"{target.isoformat()}T00:00:00",
                f"{target.isoformat()}T23:59:59",
            )
            all_slots.extend(
                compute_free_slots(
                    events=events,
                    target_date=target,
                    opening_hours=default_opening_hours(),
                    time_of_day=time_of_day,
                    now_dt=_now_ch() if target == _today_ch() else None,
                )
            )

        recommendation = build_next_available_recommendation(
            slots=all_slots,
            start_date=start_date,
            search_window_days=search_window_days,
            requested_time_of_day=time_of_day,
            searched_working_days_count=working_days,
        )
        return _json_dumps(recommendation)

    def _tool_resolve_phonebook_identity(self, args: Dict[str, Any], session_id: Optional[str]) -> str:
        if not session_id:
            return _json_dumps({"matched": False, "is_new_patient": False, "status": "error", "message": "Session not found."})
        first = (args.get("patient_first_name") or "").strip()
        last = (args.get("patient_last_name") or "").strip()
        if not _is_latin_name(first) or not _is_latin_name(last):
            return _json_dumps(
                {
                    "matched": False,
                    "is_new_patient": False,
                    "status": "name_requires_latin",
                    "message": "Patient first and last names must be transliterated into Latin characters before retrying.",
                }
            )
        resolution = session_manager.resolve_phonebook_identity(session_id, first, last)
        sanitized = self._sanitize_phonebook_resolution(resolution)
        self.verified_patient_fields = dict(sanitized.get("verified_patient_fields") or {})
        logger.info(
            "[PHONEBOOK] Deepgram identity resolution session=%s status=%s matched=%s candidate_count=%s missing_fields=%s",
            session_id[:8] if session_id else None,
            resolution.get("status"),
            resolution.get("matched"),
            resolution.get("candidate_count"),
            sanitized.get("missing_required_fields"),
        )
        if resolution.get("matched") and not sanitized.get("missing_required_fields"):
            session_manager.clear_recoverable_office_handoff(session_id, "confused_or_incoherent")
        return _json_dumps(sanitized)

    async def _tool_book_appointment(self, args: Dict[str, Any], session_id: Optional[str]) -> str:
        calendar_id = int(args.get("calendar_id") or 0)
        if not self._calendar_is_valid(calendar_id):
            return self._calendar_error(calendar_id)
        if not self.selected_doctor_id:
            return self._doctor_not_resolved_error()
        if calendar_id != self.selected_doctor_id:
            return self._calendar_error(calendar_id)

        first_name = (args.get("patient_first_name") or "").strip()
        last_name = (args.get("patient_last_name") or "").strip()
        if not _is_latin_name(first_name) or not _is_latin_name(last_name):
            return _json_dumps({"status": "error", "message": "Patient name must use Latin characters. Ask the caller to spell it."})

        verified = self.verified_patient_fields or {}

        slot_iso = args.get("slot_iso")
        try:
            slot_dt = datetime.fromisoformat(str(slot_iso).replace("Z", "+00:00"))
            if slot_dt.tzinfo is not None:
                slot_dt = slot_dt.astimezone(_now_ch().tzinfo).replace(tzinfo=None)
            if slot_dt < _now_ch().replace(tzinfo=None):
                return _json_dumps(
                    {
                        "status": "booking_failed",
                        "reason": "past_slot",
                        "message": "The selected appointment time is in the past and was not booked.",
                        "instruction": "Do not say the appointment is booked. Offer to check the next available future appointment.",
                    }
                )
        except Exception:
            return _json_dumps(
                {
                    "status": "booking_failed",
                    "reason": "invalid_slot",
                    "message": "The appointment slot was invalid and was not booked.",
                    "instruction": "Do not say the appointment is booked. Check availability again.",
                }
            )

        phone = args.get("patient_phone") or self._caller_phone(session_id)
        dob = _normalize_dob_for_epaad(args.get("patient_dob") or verified.get("patient_dob"))

        visit_reason = args.get("visit_reason") or "AI Booking"
        comment = args.get("comment") or ""
        full_comment = visit_reason if not comment or comment == visit_reason else f"{visit_reason} | {comment}"
        street = (args.get("street") or verified.get("street") or "").strip()
        street_number = _normalize_street_number_for_epaad(
            args.get("street_number") or verified.get("street_number") or ""
        )
        zip_code = re.sub(r"\D", "", str(args.get("zip_code") or verified.get("zip_code") or ""))
        city = args.get("city") or verified.get("city") or ""
        missing_fields = []
        if not dob:
            missing_fields.append("patient_dob")
        if not street:
            missing_fields.append("street")
        if not street_number or not re.search(r"\d", street_number):
            missing_fields.append("street_number")
        if not zip_code:
            missing_fields.append("zip_code")
        if not city:
            missing_fields.append("city")

        if missing_fields:
            logger.info(
                "[DEEPGRAM BOOKING] blocked before EPAAD; missing_fields=%s session=%s",
                missing_fields,
                session_id[:8] if session_id else None,
            )
            return await self._missing_booking_field_response(missing_fields, session_id)

        gender = self._booking_gender(args, verified)
        event = {
            "startDateTime": slot_iso,
            "epaad_appointmenttype_id": int(os.getenv("EPAAD_DEFAULT_APPOINTMENT_TYPE_ID", "61")),
            "comment": full_comment,
            "patient": {
                "firstName": first_name,
                "lastName": last_name,
                "birthDate": dob,
                "gender": gender,
                "address": {
                    "street": street,
                    "streetNumber": street_number,
                    "zipCode": zip_code,
                    "city": city,
                    "state": "BS" if "basel" in city.lower() else "",
                    "country": "CH",
                },
                "privatePhoneNumber": phone,
                "mobilePhoneNumber": phone,
                "email": args.get("patient_email") or verified.get("patient_email") or "",
            },
        }
        try:
            result = await epaad_client.create_event(calendar_id, event)
        except Exception as exc:
            logger.error("[DEEPGRAM BOOKING] failed slot=%s error=%s", slot_iso, exc)
            if session_id:
                await session_manager.send_office_handoff_email(
                    session_id=session_id,
                    reason="other",
                    summary=f"Appointment booking failed in EPAAD after the caller confirmed the slot. Error: {exc}",
                    urgency="routine",
                )
            return _json_dumps(
                {
                    "status": "booking_failed",
                    "message": "The appointment was not booked because the booking system rejected the request.",
                    "instruction": (
                        "Do not say the appointment is booked. Apologize briefly and say the office team will review the request."
                    ),
                }
            )
        onedoc_key = result.get("onedoc_key") or result.get("onedocKey") or result.get("key") if isinstance(result, dict) else None
        if session_id:
            session_manager.mark_appointment_booked(session_id)
        logger.info("[DEEPGRAM BOOKING] success slot=%s ref=%s", args.get("slot_iso"), str(onedoc_key)[:8])
        return _json_dumps(
            {
                "status": "success",
                "booking_reference": onedoc_key,
                "instruction": "Do not speak the booking reference. Confirm appointment details briefly, then ask if anything else is needed.",
            }
        )

    async def _tool_forward_request_to_office(self, args: Dict[str, Any], session_id: Optional[str]) -> str:
        if not session_id:
            return _json_dumps({"status": "error", "message": "Session not found."})
        reason = (args.get("reason") or "other").strip()
        urgency = (args.get("urgency") or "unknown").strip()
        queueable_reasons = {
            "repeated_misunderstanding",
            "confused_or_incoherent",
            "urgent_medical",
            "caller_requests_staff",
        }
        if reason == "caller_requests_staff":
            session = session_manager.active_sessions.get(session_id)
            if session and not session_manager._recent_transcript_has_staff_request(session.recent_transcript):
                logger.info("[OFFICE HANDOFF] Deepgram staff-request handoff ignored without explicit staff request session=%s", session_id[:8])
                return _json_dumps(
                    {
                        "status": "not_queued",
                        "instruction": "Do not say this was forwarded. Continue helping the caller with the appointment flow.",
                    }
                )
        if reason not in queueable_reasons and urgency not in {"emergency", "same_day"}:
            logger.info(
                "[OFFICE HANDOFF] Deepgram routine/other handoff ignored session=%s reason=%s urgency=%s",
                session_id[:8],
                reason,
                urgency,
            )
            return _json_dumps(
                {
                    "status": "not_queued",
                    "instruction": "Do not say this was forwarded. Ask one short clarification question or continue the active workflow.",
                }
            )
        queued = await session_manager.send_office_handoff_email(
            session_id=session_id,
            reason=reason,
            summary=(args.get("summary") or "Manual office review requested.").strip(),
            urgency=urgency,
        )
        return _json_dumps({"status": "queued" if queued else "error", "send_timing": "after_call_end"})

    async def _tool_search_knowledge_base(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        results = await document_processor.search_knowledge_base(query, k=5)
        return _json_dumps({"results": results[:5]})

    def _calendar_is_valid(self, calendar_id: int) -> bool:
        return bool(calendar_id and (not self.valid_calendar_ids or calendar_id in self.valid_calendar_ids))

    def _calendar_error(self, calendar_id: int) -> str:
        return _json_dumps(
            {
                "error": "You must call get_available_doctors first and use one of the returned calendar IDs.",
                "calendar_id": calendar_id,
                "valid_calendar_ids": sorted(self.valid_calendar_ids),
            }
        )

    def _caller_phone(self, session_id: Optional[str]) -> str:
        if not session_id:
            return ""
        session = session_manager.active_sessions.get(session_id)
        if not session or not session.participants:
            return ""
        return session.participants[0].get("phone_number") or ""

    async def _do_acs_hangup(self, session_id: str, reason: str) -> bool:
        session = session_manager.active_sessions.get(session_id)
        if not session or not session.call_connection_id:
            logger.warning("[DEEPGRAM HANGUP:%s] missing session/call_connection_id session=%s", reason, session_id)
            return False

        call_connection_id = session.call_connection_id
        if not await session_manager.begin_hangup(session_id, call_connection_id):
            logger.info("[DEEPGRAM HANGUP:%s] skipping duplicate session=%s", reason, session_id[:8])
            return True

        try:
            client = CallAutomationClient.from_connection_string(config["acs_connection_string"])
            call_conn = client.get_call_connection(call_connection_id)
            call_conn.hang_up(is_for_everyone=True)
            await session_manager.finish_hangup(session_id, call_connection_id, True)
            logger.info("[DEEPGRAM HANGUP:%s] sent session=%s", reason, session_id[:8])
            return True
        except Exception as exc:
            message = str(exc)
            success = any(token in message for token in ("404", "not found", "CallConnectionNotFound"))
            await session_manager.finish_hangup(session_id, call_connection_id, success)
            if success:
                logger.info("[DEEPGRAM HANGUP:%s] call already gone session=%s", reason, session_id[:8])
                return True
            logger.error("[DEEPGRAM HANGUP:%s] failed session=%s error=%s", reason, session_id[:8], exc)
            return False
