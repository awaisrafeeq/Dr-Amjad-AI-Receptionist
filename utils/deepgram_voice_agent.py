import asyncio
import base64
import json
import logging
import os
import unicodedata
from datetime import date, datetime, timedelta, timezone
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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


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

Privacy and phonebook:
- Never say "I found your information", "your data is on file", "that matches our records", "no record was found", or anything similar.
- After the caller confirms first and last name, call resolve_phonebook_identity silently.
- Use verified_patient_fields from resolve_phonebook_identity silently.
- Ask ONLY for fields listed in missing_required_fields. If date of birth or address is not listed as missing, do not ask for it again.
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
7. Ask only missing required fields: date of birth and address. Address can be one combined question: street, house number, postal code, city.
8. Ask preferred doctor. Call get_available_doctors before checking slots.
9. If caller wants earliest/next available, call get_next_available_slot. Otherwise call get_available_slots for a chosen date.
10. Offer one appointment option first and ask if it works.
11. Only call book_appointment after the caller confirms the slot and required fields are available.
12. After booking, briefly confirm the appointment details. Do not say the booking reference.
13. Ask if anything else is needed. After goodbye, call terminate_call.

Function rules:
- get_available_doctors: use before any slot lookup.
- get_available_slots/get_next_available_slot: use real availability only.
- book_appointment: use only after caller confirms the offered slot.
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

    async def forward_messages(self, ws: WebSocket, is_acs_audio_stream: bool, session_id: Optional[str] = None):
        api_key = config.get("deepgram_api_key")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is required when VOICE_AGENT_PROVIDER=deepgram")

        if not is_acs_audio_stream:
            raise RuntimeError("Deepgram Voice Agent bridge currently supports ACS audio streams only")

        headers = {"Authorization": f"Token {api_key}"}
        agent_url = config.get("deepgram_agent_url") or "wss://agent.deepgram.com/v1/agent/converse"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            logger.info("[DEEPGRAM AGENT] Connecting: %s", agent_url)
            async with http_session.ws_connect(agent_url, headers=headers, heartbeat=20) as dg_ws:
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

        await dg_ws.send_json(self._settings_payload())

        while True:
            msg = await dg_ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "SettingsApplied":
                    return
                if data.get("type") == "Error":
                    raise RuntimeError(f"Deepgram settings error: {data}")
                logger.debug("[DEEPGRAM AGENT] init event: %s", data.get("type"))
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("Deepgram websocket closed before SettingsApplied")

    def _settings_payload(self) -> Dict[str, Any]:
        input_sample_rate = int(config.get("deepgram_sample_rate") or 16000)
        output_sample_rate = int(config.get("deepgram_agent_output_sample_rate") or input_sample_rate)
        language_hints = [
            value.strip()
            for value in (config.get("deepgram_agent_language_hints") or "").split(",")
            if value.strip()
        ]

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
        if config.get("deepgram_agent_language"):
            listen_provider["language"] = config["deepgram_agent_language"]
        if language_hints:
            listen_provider["language_hints"] = language_hints

        prompt = _build_agent_prompt(self.system_message)
        logger.info(
            "[DEEPGRAM AGENT] Settings model=%s speak=%s input_sample_rate=%s output_sample_rate=%s prompt_chars=%s",
            listen_provider["model"],
            self.current_speak_model,
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
                await dg_ws.send_bytes(base64.b64decode(audio_b64))

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
        try:
            await session_manager.log_transcription(
                session_id=session_id,
                speaker=speaker,
                utterance_text=text,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            logger.info("[TRANSCRIPT] %s: %s", speaker, text[:80])
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

    def _detect_requested_language(self, text: str, data: Dict[str, Any]) -> Optional[str]:
        lower = text.strip().lower()
        if not lower:
            return None

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
                else "Ask only for the fields listed in missing_required_fields, one question at a time."
            ),
            "missing_fields_policy": (
                "Never tell the caller whether information was found or not. "
                "Use verified_patient_fields silently. Ask ONLY for fields listed in missing_required_fields."
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
        logger.info("[DEEPGRAM DOCTORS] valid=%s", sorted(self.valid_calendar_ids))
        return _json_dumps(doctors)

    async def _tool_get_available_slots(self, args: Dict[str, Any]) -> str:
        calendar_id = int(args.get("calendar_id") or 0)
        if not self._calendar_is_valid(calendar_id):
            return self._calendar_error(calendar_id)

        target_date = datetime.strptime(args.get("date"), "%Y-%m-%d").date()
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

        start_date = datetime.strptime(args.get("start_date"), "%Y-%m-%d").date() if args.get("start_date") else _today_ch()
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
        return _json_dumps(sanitized)

    async def _tool_book_appointment(self, args: Dict[str, Any], session_id: Optional[str]) -> str:
        calendar_id = int(args.get("calendar_id") or 0)
        if not self._calendar_is_valid(calendar_id):
            return self._calendar_error(calendar_id)

        first_name = (args.get("patient_first_name") or "").strip()
        last_name = (args.get("patient_last_name") or "").strip()
        if not _is_latin_name(first_name) or not _is_latin_name(last_name):
            return _json_dumps({"status": "error", "message": "Patient name must use Latin characters. Ask the caller to spell it."})

        verified = self.verified_patient_fields or {}

        phone = args.get("patient_phone") or self._caller_phone(session_id)
        dob = _normalize_dob_for_epaad(args.get("patient_dob") or verified.get("patient_dob"))

        visit_reason = args.get("visit_reason") or "AI Booking"
        comment = args.get("comment") or ""
        full_comment = visit_reason if not comment or comment == visit_reason else f"{visit_reason} | {comment}"
        city = args.get("city") or verified.get("city") or ""
        event = {
            "startDateTime": args.get("slot_iso"),
            "epaad_appointmenttype_id": int(os.getenv("EPAAD_DEFAULT_APPOINTMENT_TYPE_ID", "61")),
            "comment": full_comment,
            "patient": {
                "firstName": first_name,
                "lastName": last_name,
                "birthDate": dob,
                "gender": args.get("patient_gender") or verified.get("patient_gender") or "other",
                "address": {
                    "street": args.get("street") or verified.get("street") or "",
                    "streetNumber": args.get("street_number") or verified.get("street_number") or "",
                    "zipCode": args.get("zip_code") or verified.get("zip_code") or "",
                    "city": city,
                    "state": "BS" if "basel" in city.lower() else "",
                    "country": "CH",
                },
                "privatePhoneNumber": phone,
                "mobilePhoneNumber": phone,
                "email": args.get("patient_email") or verified.get("patient_email") or "",
            },
        }
        result = await epaad_client.create_event(calendar_id, event)
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
