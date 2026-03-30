import aiohttp
import asyncio
import base64
import json
from json import JSONDecodeError
from typing import Any, Optional, List, Dict, Tuple
from fastapi import WebSocket
from utils.helpers import transform_acs_to_openai_format, transform_openai_to_acs_format, load_prompt_from_markdown, extract_transcription_from_openai_message, filter_diagnosis_words
from config import get_config
import asyncio
import logging
from difflib import SequenceMatcher
# Import here to avoid circular imports
from utils.session_manager import session_manager
from utils.document_utils import document_processor
from utils.epaad_client import epaad_client
from utils.availability import compute_free_slots, default_opening_hours

try:
    from langdetect import detect as detect_lang
except Exception:  # pragma: no cover
    detect_lang = None

import os
import re
from datetime import date, datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

config = get_config()
logger = logging.getLogger(__name__)

class RTMiddleTier:
    
    endpoint: str
    deployment: str
    api_version: str
    key: str
    
    selected_voice: str
    system_message: Optional[str]
    
    model: Optional[str] = None
    system_message: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    disable_audio: Optional[bool] = None
    
    def __init__(self):
        self.endpoint = config["azure_openai_endpoint"]
        self.deployment = config["azure_openai_deployment"]
        self.api_version = config["azure_openai_api_version"]
        self.key = config["azure_openai_key"]

        self.selected_voice = "shimmer"
        self.system_message = load_prompt_from_markdown("system_prompt.md")
        self._doctor_cache: Dict[int, str] = {}  # calendar_id -> doctor_name, avoids extra GET /calendars after booking

    
    async def forward_messages(self, ws: WebSocket, is_acs_audio_stream: bool, session_id: Optional[str] = None):
        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            headers = {
                "api-key": self.key,
                "OpenAI-Beta": "realtime=v1",
            }
            ws_url = f"/openai/realtime?api-version={self.api_version}&deployment={self.deployment}"
            
            try:
                # logger.info("Connecting to Azure OpenAI Realtime: %s%s", self.endpoint, ws_url)
                async with session.ws_connect(ws_url, headers=headers) as target_ws:
                    # logger.info("Connected to Azure OpenAI Realtime")

                    loop = asyncio.get_running_loop()
                    session_initialized = asyncio.Event()
                    greeting_sent = asyncio.Event()
                    call_end_requested = asyncio.Event()
                    session_confirmed = asyncio.Event()

                    last_user_activity_ts = loop.time()
                    last_prompt_stage = 0
                    detected_conversation_language: Optional[str] = None

                    last_kb_context: Optional[str] = None

                    suppress_agent_audio = False
                    cancel_sent_for_current_turn = False
                    response_active = False


                    # Native Tool Calling Handlers will be here

                    async def wait_for_response_idle(timeout: float = 2.0) -> bool:
                        """Wait for any active response to finish or be cancelled."""
                        nonlocal response_active
                        if not response_active:
                            return True
                        
                        _start = loop.time()
                        while response_active and (loop.time() - _start) < timeout:
                            await asyncio.sleep(0.05)
                        
                        # Even if flag is finally false, give OpenAI a tiny buffer to be truly "ready"
                        if not response_active:
                            await asyncio.sleep(0.1)
                        return not response_active

                    async def send_assistant_prompt(instructions: str) -> None:
                        nonlocal response_active
                        if response_active:
                            try:
                                await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                await wait_for_response_idle(timeout=1.5)
                            except Exception as e:
                                logger.error(f"Failed to cancel active response: {e}")

                        try:
                            await target_ws.send_str(
                                json.dumps(
                                    {
                                        "type": "response.create",
                                        "response": {
                                            "modalities": ["audio", "text"],
                                            "instructions": instructions,
                                        },
                                    }
                                )
                            )
                        except Exception:
                            logger.exception("Failed to send response.create")

                    async def send_initial_greeting() -> None:
                        if not is_acs_audio_stream:
                            return
                        try:
                            await asyncio.wait_for(session_initialized.wait(), timeout=10)
                        except asyncio.TimeoutError:
                            logger.warning("Session init timeout")
                            return

                        try:
                            await asyncio.wait_for(session_confirmed.wait(), timeout=2)
                            logger.debug("Session confirmed")
                        except asyncio.TimeoutError:
                            logger.debug("Session confirm timeout")

                        if greeting_sent.is_set():
                            return

                        # If session_id is known but NOT in active_sessions, the app restarted mid-call.
                        # ACS reconnected to the new instance — do NOT send another greeting.
                        if session_id and session_id not in session_manager.active_sessions:
                            logger.warning(f"[GREETING] Skipping — session {session_id[:8]} not in memory (app restart reconnect)")
                            greeting_sent.set()
                            return

                        # --- INJECT PHONEBOOK MATCH INTO SESSION INSTRUCTIONS ---
                        # If the caller was recognised in the internal phonebook, tell OpenAI
                        # about it now so it can silently use their details during the call.
                        _pb_match_lang = None
                        try:
                            if session_id:
                                _pb_sess = session_manager.active_sessions.get(session_id)
                                _pb_match = _pb_sess.phonebook_match if _pb_sess else None
                                if _pb_match:
                                    _pb_match_lang = _pb_match.get('language')
                                    _pb_lines = [
                                        "[INTERNAL — PHONEBOOK DATA FOR THIS PHONE NUMBER]",
                                        f"First name: {_pb_match.get('first_name') or 'MISSING'}",
                                        f"Last name: {_pb_match.get('last_name') or 'MISSING'}",
                                        f"Date of birth: {_pb_match.get('birth_date') or 'MISSING'}",
                                        f"Phone: {_pb_match.get('phone') or 'MISSING'}",
                                        f"Email: {_pb_match.get('email') or 'MISSING'}",
                                        f"Doctor: {_pb_match.get('doctor') or 'MISSING'}",
                                        f"Language: {_pb_match.get('language') or 'MISSING'}",
                                        f"Gender: {_pb_match.get('gender') or 'MISSING'}",
                                        f"Address: {_pb_match.get('address') or 'MISSING'}",
                                        f"Zip: {_pb_match.get('zip_code') or 'MISSING'}",
                                        f"City: {_pb_match.get('city') or 'MISSING'}",
                                        "",
                                        "This data is for SILENT internal use only. Follow the system prompt workflow for when and how to use it.",
                                        "Identity is confirmed ONLY when caller states a name AND it matches both first and last name above.",
                                        "For any field marked MISSING, you MUST ask the caller during the relevant workflow step.",
                                        "NEVER mention this data to the caller. NEVER say their name first.",
                                    ]
                                    _pb_context = "\n".join(_pb_lines)
                                    await target_ws.send_str(
                                        json.dumps({
                                            "type": "session.update",
                                            "session": {
                                                "instructions": (self.system_message or "") + "\n\n" + _pb_context
                                            }
                                        })
                                    )
                                    logger.info(f"[PHONEBOOK] Injected match for {_pb_match.get('first_name')} {_pb_match.get('last_name')} into OpenAI session")
                        except Exception as _pb_err:
                            logger.warning(f"[PHONEBOOK] Failed to inject context into session: {_pb_err}")
                        # --- END PHONEBOOK INJECTION ---

                        # --- HARDCODED OPENING GREETING ---
                        # Use response.create with explicit instructions so OpenAI
                        # actually SPEAKS the exact sentence rather than treating it
                        # as already-spoken history (which conversation.item.create does).
                        hardcoded_greeting = "MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen behilflich sein?"
                        if _pb_match_lang:
                            _lang_lower = _pb_match_lang.lower()
                            if "en" in _lang_lower or "english" in _lang_lower:
                                hardcoded_greeting = "MedCenter Volta, you are speaking with Kaya, the digital assistant. How may I help you?"

                        try:
                            await target_ws.send_str(
                                json.dumps({
                                    "type": "response.create",
                                    "response": {
                                        "modalities": ["audio", "text"],
                                        "voice": "shimmer",
                                        "instructions": (
                                            f"Say EXACTLY and ONLY this sentence, word for word, nothing before it and nothing after it: "
                                            f'"{hardcoded_greeting}"'
                                        )
                                    }
                                })
                            )
                            logger.info("[GREETING] Sent")
                        except Exception as e:
                            logger.error(f"[GREETING] Failed: {e}")

                        greeting_sent.set()
                        logger.debug("Greeting triggered")

                    async def inactivity_monitor() -> None:
                        nonlocal last_prompt_stage, last_user_activity_ts
                        if not is_acs_audio_stream:
                            return

                        # Silence policy (seconds)
                        prompt_1_after = 90  # Increased from 60s
                        prompt_2_after = 180 # Increased from 120s
                        hangup_after = 360   # Increased from 300s

                        try:
                            while not call_end_requested.is_set():
                                await asyncio.sleep(1.0)
                                if not greeting_sent.is_set():
                                    continue

                                idle_for = loop.time() - last_user_activity_ts

                                if idle_for >= hangup_after and last_prompt_stage < 3:
                                    last_prompt_stage = 3
                                    await send_assistant_prompt(
                                        "It seems we got disconnected or you are not available. "
                                        "I will end the call now. Please call MedCenter Volta again anytime. Goodbye."
                                    )
                                    call_end_requested.set()
                                    return

                                if idle_for >= prompt_2_after and last_prompt_stage < 2:
                                    last_prompt_stage = 2
                                    await send_assistant_prompt(
                                        "Are you still there? If you need help with an appointment or general information, "
                                        "please tell me what you need."
                                    )
                                    continue

                                if idle_for >= prompt_1_after and last_prompt_stage < 1:
                                    last_prompt_stage = 1
                                    await send_assistant_prompt(
                                        "Are you still there? How may I assist you today?"
                                    )
                                    continue

                        except asyncio.CancelledError:
                            return
                        except Exception:
                            logger.exception("Inactivity monitor error")
                            return
                    async def from_client_to_server():
                        nonlocal session_id
                        try:
                            async for msg in ws.iter_text():
                                try:
                                    data = json.loads(msg)
                                except JSONDecodeError:
                                    logger.debug("Non-JSON from ACS (ignored)")
                                    continue

                                # Once the AI has said goodbye and requested hangup,
                                # stop forwarding any more caller audio to OpenAI.
                                # This prevents the AI from generating new responses
                                # (e.g. reverting to German) after the call is over.
                                if call_end_requested.is_set():
                                    continue

                                if isinstance(data, dict):
                                    kind = data.get("kind")
                                    
                                    # If session_id not known yet, resolve from metadata
                                    if not session_id and kind == "metadata":
                                        call_conn_id = data.get("metadata", {}).get("callConnectionId")
                                        if call_conn_id:
                                            session_obj = session_manager.get_session_by_call_connection_id(call_conn_id)
                                            if session_obj:
                                                session_id = session_obj.session_id
                                                logger.debug(f"Resolved session {session_id[:8]}...")

                                    if kind == "AudioData":
                                        last_user_activity_ts = loop.time()

                                if is_acs_audio_stream:
                                    data = transform_acs_to_openai_format(data, self.model, self.system_message, self.temperature, self.max_tokens, self.disable_audio, self.selected_voice)
                                if data:
                                    if isinstance(data, dict) and data.get("type") == "session.update":
                                        session_initialized.set()
                                    await target_ws.send_str(json.dumps(data))
                        except asyncio.CancelledError:
                            logger.debug("Client→server cancelled")
                            return
                        except Exception as e:
                            logger.exception("Client→server error")
                            return
                            
                    # --- TURN DETECTION CONFIGURATION ---
                    # Delay after speech stops before AI can respond (seconds)
                    TURN_DETECTION_DELAY = 0.0  # OpenAI server_vad silence_duration_ms=600 already handles turn timing
                    speech_stop_time = 0.0
                    unsuppress_scheduled = False  # Flag to prevent duplicate scheduling
                    # --- END TURN DETECTION CONFIG ---

                    async def from_server_to_client():
                        nonlocal last_user_activity_ts, last_prompt_stage, session_id, detected_conversation_language
                        nonlocal suppress_agent_audio, cancel_sent_for_current_turn, response_active, speech_stop_time, unsuppress_scheduled
                        try:
                            async for msg in target_ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    original_data = json.loads(msg.data)
                                    event_type = original_data.get("type")
                                    # Log only essential conversation events (not every message)
                                    event_type = original_data.get("type")
                                    
                                    # Log error details — suppress known harmless errors as DEBUG
                                    if event_type == "error":
                                        _err_code = original_data.get("error", {}).get("code", "")
                                        if _err_code == "response_cancel_not_active":
                                            logger.debug(f"[OPENAI] response_cancel_not_active (expected)")
                                        else:
                                            logger.error(f"[OPENAI ERROR] {_err_code}: {original_data.get('error', {}).get('message', 'Unknown error')}")
                                    
                                    # Track response state (debug only)
                                    if event_type == "response.created":
                                        response_active = True
                                        logger.debug("Response created")
                                    elif event_type in ("response.done", "response.cancelled"):
                                        response_active = False
                                        logger.debug("Response ended")
                                    
                                    elif event_type == "session.updated":
                                        logger.debug("Session updated — transcription active")
                                        session_confirmed.set()

                                    elif event_type == "response.function_call_arguments.done":
                                        _call_id = original_data.get("call_id")
                                        _func_name = original_data.get("name")
                                        try:
                                            _fn_args = json.loads(original_data.get("arguments", "{}"))
                                        except json.JSONDecodeError:
                                            _fn_args = {}

                                        # Run all API work in a background task so the event loop stays
                                        # unblocked — audio frames from OpenAI continue to be forwarded
                                        # to ACS while the function executes, eliminating silence gaps.
                                        async def _run_function_call(_call_id, _func_name, _args):
                                            nonlocal detected_conversation_language
                                            _result = ""

                                            # Send brief holding message for slow functions so caller never hears silence
                                            if _func_name in ("get_available_slots", "book_appointment", "get_available_doctors"):
                                                await asyncio.sleep(0.15)  # yield so response.done is processed first
                                                if not response_active:
                                                    try:
                                                        await target_ws.send_str(json.dumps({
                                                            "type": "response.create",
                                                            "response": {
                                                                "modalities": ["audio", "text"],
                                                                "tool_choice": "none",
                                                                "max_output_tokens": 40,
                                                                "instructions": (
                                                                    f"Say EXACTLY ONE short sentence: 'One moment please.' "
                                                                    f"in the language the caller is speaking (currently '{detected_conversation_language or 'de'}'). "
                                                                    "Then STOP. Say NOTHING else. Do NOT list anything. Do NOT guess results."
                                                                )
                                                            }
                                                        }))
                                                        logger.info(f"[HOLDING] {_func_name}")
                                                    except Exception as __he:
                                                        logger.debug(f"[HOLDING] Could not send: {__he}")

                                            if _func_name == "get_available_doctors":
                                                __doctors = []
                                                try:
                                                    __calendars = await epaad_client.get_calendars()
                                                    for __cal in __calendars or []:
                                                        __prof = __cal.get("professional") or {}
                                                        __first = (__prof.get("firstName") or "").strip()
                                                        __last = (__prof.get("lastName") or "").strip()
                                                        __name = f"Dr. {__first} {__last}".strip() or "Unknown Doctor"
                                                        try:
                                                            __doctors.append({"calendar_id": int(__cal.get("id")), "doctor_name": __name})
                                                        except Exception:
                                                            pass

                                                    __extra = (os.getenv("EPAAD_EXTRA_CALENDAR_IDS") or "").strip()
                                                    # Cache calendar_id -> name so _phonebook_update skips an extra GET /calendars
                                                    for __d in __doctors:
                                                        self._doctor_cache[__d["calendar_id"]] = __d["doctor_name"]
                                                    if __extra:
                                                        for __part in __extra.split(","):
                                                            if __part.strip():
                                                                try:
                                                                    __cal_id = int(__part.strip())
                                                                    if not any(__d["calendar_id"] == __cal_id for __d in __doctors):
                                                                        __doctors.append({"calendar_id": __cal_id, "doctor_name": f"Unknown Doctor (Calendar {__cal_id})"})
                                                                except Exception:
                                                                    pass

                                                    _result = json.dumps(__doctors)
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_doctors: {__e}")
                                                    _result = json.dumps({"error": str(__e)})

                                            elif _func_name == "get_available_slots":
                                                try:
                                                    __cal_id = _args.get("calendar_id")
                                                    __target_date_str = _args.get("date")
                                                    __tod = _args.get("time_of_day", "any")
                                                    __target_day = date.fromisoformat(__target_date_str)
                                                    __start_dt = datetime.combine(__target_day, datetime.min.time())
                                                    __end_dt = datetime.combine(__target_day, datetime.max.time()).replace(microsecond=0)

                                                    __events = await epaad_client.get_events(
                                                        calendar_id=__cal_id,
                                                        from_dt=__start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        until_dt=__end_dt.strftime("%Y-%m-%dT%H:%M:%S")
                                                    )

                                                    __slots = compute_free_slots(
                                                        events=__events or [],
                                                        target_date=__target_day,
                                                        opening_hours=default_opening_hours(),
                                                        slot_minutes=15,
                                                        time_of_day=__tod,
                                                        now_dt=datetime.now(ZoneInfo("Europe/Zurich")),
                                                    )

                                                    __slot_iso = [__s.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S") for __s in __slots][:10]
                                                    _result = json.dumps({"available_slots": __slot_iso})
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_slots: {__e}")
                                                    _result = json.dumps({"error": str(__e)})

                                            elif _func_name == "book_appointment":
                                                try:
                                                    __dob = _args.get("patient_dob", "")
                                                    if len(__dob) == 10:
                                                        __dob += "T00:00:00"
                                                    __phone = _args.get("patient_phone")
                                                    __visit_reason = _args.get("visit_reason", "")
                                                    __comment = _args.get("comment", "")

                                                    # --- APPOINTMENT TYPE SELECTION LOGIC ---
                                                    __appointment_type_id = 61
                                                    __visit_lower = __visit_reason.lower() if __visit_reason else ""
                                                    __comment_lower = __comment.lower() if __comment else ""
                                                    __combined_text = __visit_lower + " " + __comment_lower

                                                    __issue_indicators = [
                                                        "und", "and", "sowie", "auch", "außerdem", "zusätzlich",
                                                        "another", "other", "also", "plus", "besides", "moreover"
                                                    ]
                                                    __issue_count = sum(1 for __ind in __issue_indicators if __ind in __combined_text)

                                                    __new_patient_keywords = [
                                                        "neu", "new patient", "newly", "erstmalig", "neupatient",
                                                        "erstbesuch", "first visit", "neu bei", "new to"
                                                    ]
                                                    __is_new_patient = any(__kw in __combined_text for __kw in __new_patient_keywords)

                                                    __referral_keywords = [
                                                        "überweisung", "referral", "überwiesen", "referred",
                                                        "zuweisung", "zuweisender", "transfer"
                                                    ]
                                                    __is_referred = any(__kw in __combined_text for __kw in __referral_keywords)

                                                    if __is_new_patient or __is_referred or __issue_count >= 2:
                                                        __appointment_type_id = 65
                                                        logger.info(f"[BOOKING] Type 65 (30 min)")
                                                    elif __issue_count == 1:
                                                        __appointment_type_id = 63
                                                        logger.info(f"[BOOKING] Type 63 (20 min)")
                                                    else:
                                                        __appointment_type_id = 61
                                                        logger.info(f"[BOOKING] Type 61 (15 min)")
                                                    # --- END APPOINTMENT TYPE SELECTION ---

                                                    __f_name = _args.get("patient_first_name")
                                                    __l_name = _args.get("patient_last_name")
                                                    if __f_name == "[REDACTED]" or __l_name == "[REDACTED]":
                                                        logging.getLogger("utils.rtmt").warning(f"[BOOKING WARN] Model sent [REDACTED] for name.")

                                                    # Map gender: prefer AI-detected voice gender, then phonebook fallback
                                                    __gender = (_args.get("patient_gender") or "").lower()
                                                    if __gender not in ["male", "female"]:
                                                        __gender = "other"
                                                        try:
                                                            __caller_session = session_manager.active_sessions.get(session_id)
                                                            __caller_phone = __caller_session.participants[0]["phone_number"] if __caller_session and __caller_session.participants else None
                                                            if __caller_phone:
                                                                from utils.phonebook_lookup import get_phonebook_lookup
                                                                __pb_lookup = get_phonebook_lookup()
                                                                if __pb_lookup:
                                                                    __pb_match = __pb_lookup.lookup_by_phone(__caller_phone)
                                                                    if __pb_match and __pb_match.gender:
                                                                        __g = __pb_match.gender.strip().lower()
                                                                        if __g in ("m", "männlich", "male", "maennlich"):
                                                                            __gender = "male"
                                                                        elif __g in ("w", "f", "weiblich", "female", "frau"):
                                                                            __gender = "female"
                                                                        logger.info(f"[GENDER] From phonebook: '{__gender}'")
                                                        except Exception as __ge:
                                                            logger.warning(f"[GENDER] Phonebook gender lookup failed: {__ge}")

                                                    __city = _args.get("city", "")
                                                    __state = "BS" if "basel" in __city.lower() else ""

                                                    __full_comment = __visit_reason
                                                    if __comment and __comment != __visit_reason:
                                                        __full_comment = f"{__visit_reason} | {__comment}"

                                                    __event = {
                                                        "startDateTime": _args.get("slot_iso"),
                                                        "epaad_appointmenttype_id": __appointment_type_id,
                                                        "comment": __full_comment or "AI Booking",
                                                        "patient": {
                                                            "firstName": __f_name,
                                                            "lastName": __l_name,
                                                            "birthDate": __dob,
                                                            "gender": __gender,
                                                            "address": {
                                                                "street": _args.get("street", ""),
                                                                "streetNumber": _args.get("street_number", ""),
                                                                "zipCode": _args.get("zip_code", ""),
                                                                "city": __city,
                                                                "state": __state,
                                                                "country": "CH"
                                                            },
                                                            "privatePhoneNumber": __phone,
                                                            "mobilePhoneNumber": __phone,
                                                            "email": _args.get("patient_email", "")
                                                        }
                                                    }
                                                    _res = await epaad_client.create_event(_args.get("calendar_id"), __event)
                                                    __onedoc_key = _res.get("onedoc_key") or _res.get("onedocKey") or _res.get("key") if isinstance(_res, dict) else None
                                                    if __onedoc_key:
                                                        logger.info(f"[BOOKING SUCCESS] {_args.get('slot_iso')[:10]} ref:{__onedoc_key[:8]}...")
                                                        _result = json.dumps({"status": "success", "booking_reference": __onedoc_key, "instruction": "DO NOT speak the booking_reference to the caller."})

                                                        # --- PHONEBOOK INTEGRATION (fire-and-forget) ---
                                                        # Capture values from outer scope into local names
                                                        # to avoid Python name-mangling issues inside nested closures.
                                                        _pb_phone = __phone
                                                        _pb_f_name = __f_name
                                                        _pb_l_name = __l_name
                                                        _pb_dob = __dob
                                                        _pb_gender_raw = __gender
                                                        _pb_args = dict(_args)  # shallow copy
                                                        _pb_session_id = session_id

                                                        async def _phonebook_update():
                                                            nonlocal detected_conversation_language
                                                            try:
                                                                from utils.phonebook_lookup import get_phonebook_lookup, save_phonebook_to_blob
                                                                pb_lookup = get_phonebook_lookup()
                                                                if not pb_lookup:
                                                                    return

                                                                # Get ACS incoming caller number
                                                                acs_num = _pb_phone
                                                                try:
                                                                    upd_sess = session_manager.active_sessions.get(_pb_session_id)
                                                                    if upd_sess and upd_sess.participants:
                                                                        acs_num = upd_sess.participants[0].get("phone_number") or _pb_phone
                                                                except Exception:
                                                                    pass

                                                                # --- Build patient_data (used for BOTH add and update) ---
                                                                pb_gender = "M" if _pb_gender_raw == "male" else ("W" if _pb_gender_raw == "female" else "")

                                                                birth_raw = _pb_dob[:10] if _pb_dob else ""
                                                                birth_fmt = ""
                                                                if birth_raw:
                                                                    try:
                                                                        birth_fmt = datetime.strptime(birth_raw, "%Y-%m-%d").strftime("%m/%d/%Y")
                                                                    except Exception:
                                                                        birth_fmt = birth_raw

                                                                lang_map = {
                                                                    "de": "Deutsch", "en": "Englisch", "fr": "Französisch",
                                                                    "it": "Italienisch", "sq": "Albanisch", "tr": "Türkisch",
                                                                    "ar": "Arabisch", "ru": "Russisch", "es": "Spanisch",
                                                                    "pt": "Portugiesisch", "pl": "Polnisch", "hr": "Kroatisch",
                                                                    "sr": "Serbisch", "bs": "Bosnisch", "ro": "Rumänisch",
                                                                    "nl": "Niederländisch", "uk": "Ukrainisch",
                                                                    "ko": "Koreanisch", "zh": "Chinesisch", "ja": "Japanisch",
                                                                    "fa": "Persisch", "ku": "Kurdisch", "so": "Somali",
                                                                }
                                                                pb_lang = lang_map.get(detected_conversation_language or "de", "Deutsch")

                                                                st = _pb_args.get("street", "")
                                                                st_no = _pb_args.get("street_number", "")
                                                                pb_address = f"{st} {st_no}".strip() if st else st_no

                                                                patient_data = {
                                                                    "first_name": _pb_f_name,
                                                                    "last_name": _pb_l_name,
                                                                    "birth_date": birth_fmt,
                                                                    "phone": _pb_phone,
                                                                    "mobile": acs_num,
                                                                    "gender": pb_gender,
                                                                    "email": _pb_args.get("patient_email", ""),
                                                                    "zip_code": _pb_args.get("zip_code", ""),
                                                                    "city": _pb_args.get("city", ""),
                                                                    "address": pb_address,
                                                                    "doctor": "",
                                                                    "comment": _pb_args.get("comment") or "AI Booking",
                                                                    "language": pb_lang,
                                                                }
                                                                logger.info(f"[PHONEBOOK] Built patient_data: {patient_data}")

                                                                # Look up doctor name — use in-memory cache first to avoid extra GET /calendars
                                                                try:
                                                                    _cal_id_int = int(_pb_args.get("calendar_id") or 0)
                                                                    if _cal_id_int and _cal_id_int in self._doctor_cache:
                                                                        patient_data["doctor"] = self._doctor_cache[_cal_id_int]
                                                                    elif _cal_id_int:
                                                                        cals = await epaad_client.get_calendars()
                                                                        for cal in cals or []:
                                                                            if int(cal.get("id")) == _cal_id_int:
                                                                                prof = cal.get("professional") or {}
                                                                                pf = (prof.get("firstName") or "").strip()
                                                                                pl = (prof.get("lastName") or "").strip()
                                                                                patient_data["doctor"] = f"Dr. {pf} {pl}".strip() if (pf or pl) else ""
                                                                                break
                                                                except Exception:
                                                                    pass

                                                                # --- Add or Update ---
                                                                # Rule: update ONLY when phone + BOTH first AND last name all match.
                                                                # If phone matches but the name is different, this is a different person
                                                                # sharing the same number — add a NEW entry instead of overwriting.
                                                                exact_match = pb_lookup.lookup_by_phone_and_name(acs_num, _pb_f_name, _pb_l_name)
                                                                if exact_match:
                                                                    logger.info(f"[PHONEBOOK] Exact match (phone+name) — updating {_pb_f_name} {_pb_l_name}")
                                                                    updated = await asyncio.to_thread(pb_lookup.update_patient, acs_num, patient_data, _pb_f_name, _pb_l_name)
                                                                    if updated:
                                                                        uploaded = await asyncio.to_thread(save_phonebook_to_blob, pb_lookup.xlsx_path)
                                                                        if uploaded:
                                                                            logger.info(f"[PHONEBOOK] Updated {_pb_f_name} {_pb_l_name} locally and in Blob")
                                                                        else:
                                                                            logger.warning("[PHONEBOOK] Updated local, blob upload failed")
                                                                    else:
                                                                        logger.warning("[PHONEBOOK] Failed to update existing patient")
                                                                else:
                                                                    # No exact match (new person or different name on same number) → add new row
                                                                    logger.info(f"[PHONEBOOK] No exact match — adding new entry for {_pb_f_name} {_pb_l_name}")
                                                                    added = await asyncio.to_thread(pb_lookup.add_patient, patient_data)
                                                                    if added:
                                                                        uploaded = await asyncio.to_thread(save_phonebook_to_blob, pb_lookup.xlsx_path)
                                                                        if uploaded:
                                                                            logger.info(f"[PHONEBOOK] Added {_pb_f_name} {_pb_l_name}")
                                                                        else:
                                                                            logger.warning("[PHONEBOOK] Added local, blob failed")
                                                                    else:
                                                                        logger.warning("[PHONEBOOK] Failed to add")
                                                            except Exception as pe:
                                                                logger.warning(f"[PHONEBOOK] Error during phonebook integration: {pe}")
                                                        asyncio.create_task(_phonebook_update())
                                                        # --- END PHONEBOOK INTEGRATION ---

                                                    else:
                                                        _result = json.dumps({"status": "success", "message": "Appointment booked but no reference generated."})
                                                except Exception as __e:
                                                    logger.error(f"Error in book_appointment: {__e}")
                                                    _result = json.dumps({"status": "error", "message": str(__e)})

                                            elif _func_name == "terminate_call":
                                                logger.info("[CALL END] AI requested hangup")
                                                call_end_requested.set()
                                                _result = json.dumps({"status": "success", "message": "Terminating call now."})
                                                
                                                # Actually hang up the ACS call
                                                try:
                                                    _session = session_manager.active_sessions.get(session_id)
                                                    if _session and _session.call_connection_id:
                                                        from utils.acs import acs_caller
                                                        await acs_caller.hang_up(_session.call_connection_id)
                                                        logger.info("[CALL END] ACS hangup sent")
                                                except Exception as _hangup_err:
                                                    logger.error(f"[CALL END] Hangup failed: {_hangup_err}")

                                            elif _func_name == "search_knowledge_base":
                                                try:
                                                    __query = _args.get("query", "")
                                                    _results = await document_processor.search_knowledge_base(query=__query, k=5)
                                                    if not _results:
                                                        _result = json.dumps({"status": "no_results_found", "message": "No information found in the knowledge base."})
                                                    else:
                                                        __blocks = []
                                                        for __r in _results:
                                                            __content = (__r.get("content") or "").strip()
                                                            if __content:
                                                                __blocks.append(__content)
                                                        _result = json.dumps({"status": "success", "information": "\n\n".join(__blocks)[:2000]})
                                                except Exception as __e:
                                                    logger.error(f"Error in search_knowledge_base: {__e}")
                                                    _result = json.dumps({"status": "error", "message": str(__e)})

                                            # Cancel any still-active holding response before sending tool output.
                                            # This prevents the AI from saying "no slots available" (guess)
                                            # then "actually I see slots" (real data) — the contradiction problem.
                                            if _func_name != "terminate_call":
                                                if response_active:
                                                    try:
                                                        await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                                        logger.debug(f"[TOOL] Cancelled holding response before submitting {_func_name} result")
                                                    except Exception:
                                                        pass
                                                        await wait_for_response_idle(timeout=1.5)

                                            # Submit function output back to OpenAI
                                            await target_ws.send_str(json.dumps({
                                                "type": "conversation.item.create",
                                                "item": {
                                                    "type": "function_call_output",
                                                    "call_id": _call_id,
                                                    "output": _result
                                                }
                                            }))

                                            # Trigger agent to respond (if not terminating)
                                            if not call_end_requested.is_set():
                                                # Ensure any previous response (like 'One moment please') is dead
                                                await wait_for_response_idle(timeout=1.5)
                                                await target_ws.send_str(json.dumps({
                                                    "type": "response.create"
                                                }))

                                        asyncio.create_task(_run_function_call(_call_id, _func_name, _fn_args))

                                    if event_type == "input_audio_buffer.speech_started":
                                        # If the call is ending, ignore any new speech events
                                        if call_end_requested.is_set():
                                            continue
                                        last_user_activity_ts = loop.time()
                                        last_prompt_stage = 0
                                        suppress_agent_audio = True

                                        # Always try to cancel — even if response_active is False,
                                        # audio may still be buffered in ACS playout queue.
                                        if not cancel_sent_for_current_turn:
                                            cancel_sent_for_current_turn = True
                                            try:
                                                await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                                logger.info("[BARGE-IN] Cancelled")
                                                response_active = False
                                            except Exception:
                                                logger.debug("[BARGE-IN] No active response")

                                        # Flush ACS playout buffer by sending StopAudio
                                        try:
                                            await ws.send_text(json.dumps({
                                                "kind": "StopAudio"
                                            }))
                                            logger.debug("[BARGE-IN] StopAudio sent")
                                        except Exception as _se:
                                            logger.debug(f"[BARGE-IN] StopAudio failed: {_se}")

                                    if event_type in (
                                        "input_audio_buffer.speech_stopped",
                                        "input_audio_buffer.committed",
                                    ):
                                        # --- TURN DETECTION: Unsuppress in background to avoid blocking the message loop ---
                                        # Only schedule if not already scheduled (prevents duplicate from both events firing)
                                        if not unsuppress_scheduled:
                                            unsuppress_scheduled = True
                                            _speech_stop_ts = loop.time()
                                            logger.info(f"[TURN] Speech stopped, wait {TURN_DETECTION_DELAY}s")
                                            async def _unsuppress_after_delay(_ts=_speech_stop_ts):
                                                nonlocal suppress_agent_audio, cancel_sent_for_current_turn, unsuppress_scheduled
                                                await asyncio.sleep(TURN_DETECTION_DELAY)
                                                if loop.time() - _ts >= TURN_DETECTION_DELAY:
                                                    suppress_agent_audio = False
                                                    cancel_sent_for_current_turn = False
                                                    unsuppress_scheduled = False
                                                    logger.debug("[TURN] AI can respond")
                                            asyncio.create_task(_unsuppress_after_delay())
                                        # --- END TURN DETECTION ---

                                    if suppress_agent_audio and event_type == "response.audio.delta":
                                        continue
                                    
                                    # Extract transcription data if available
                                    try:
                                        transcription_data = extract_transcription_from_openai_message(original_data)
                                        if transcription_data:
                                            last_user_activity_ts = loop.time()
                                            last_prompt_stage = 0
                                            # Log transcription asynchronously (don't block message flow)
                                            if session_id:
                                                try:
                                                    await session_manager.log_transcription(
                                                        session_id=session_id,
                                                        speaker=transcription_data.get("speaker", "unknown"),
                                                        utterance_text=transcription_data.get("utterance_text", ""),
                                                        timestamp=transcription_data.get("timestamp")
                                                    )
                                                    speaker = transcription_data.get("speaker")
                                                    text = transcription_data.get("utterance_text", "")[:50]
                                                    logger.info(f"[TRANSCRIPT] {speaker}: {text}...")
                                                except Exception as e:
                                                    logger.error(f"[TRANSCRIPT ERROR] {e}")

                                            if transcription_data.get("speaker") == "customer":
                                                utterance = transcription_data.get("utterance_text", "").strip()
                                                # Skip very short / noise transcriptions
                                                if len(utterance) <= 2:
                                                    logger.debug(f"[NOISE] Skipped: '{utterance}'")
                                                else:
                                                    # Run language detection (CPU bound) in a thread to avoid blocking main loop
                                                    if detect_lang and len(utterance) > 12:  # only detect on reasonable length
                                                        try:
                                                            _lang = await asyncio.to_thread(detect_lang, utterance)
                                                            if _lang and _lang != detected_conversation_language:
                                                                detected_conversation_language = _lang
                                                                logger.debug(f"[LANG] Detected change to: {_lang}")
                                                        except Exception:
                                                            pass
                                    except Exception as e:
                                        logger.debug(f"Transcription processing error: {e}")
                                    
                                    # Transform for ACS if needed
                                    if is_acs_audio_stream:
                                        data = transform_openai_to_acs_format(original_data)
                                    else:
                                        data = original_data
                                        
                                    if data:
                                        await ws.send_text(json.dumps(data))
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    logger.error(f"WebSocket error: {target_ws.exception()}")
                                    break
                        except asyncio.CancelledError:
                            logger.debug("Server→client cancelled")
                            return
                        except Exception as e:
                            logger.exception("Server→client error")
                            return

                    try:
                        bg_tasks = [
                            asyncio.create_task(send_initial_greeting()),
                            asyncio.create_task(inactivity_monitor()),
                        ]

                        forward_tasks = [
                            asyncio.create_task(from_client_to_server()),
                            asyncio.create_task(from_server_to_client()),
                        ]

                        # Exit when either forwarding task completes (disconnect/error). Background tasks should
                        # not terminate the call loop.
                        done, pending = await asyncio.wait(
                            forward_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        
                        # Cancel any remaining tasks
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                        for task in bg_tasks:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                                
                    except asyncio.CancelledError:
                        logger.debug("Forward messages cancelled")
                        raise

                    logger.info(f"[WEBSOCKET] Closed (code={getattr(target_ws, 'close_code', None)})")
                        
            except asyncio.CancelledError:
                logger.debug("WebSocket session cancelled")
                raise
            except aiohttp.WSServerHandshakeError as e:
                logger.error(f"OpenAI handshake failed: {getattr(e, 'status', None)} - {e}")
                raise
            except Exception as e:
                logger.exception("forward_messages error")
                raise
                
rtmt = RTMiddleTier()
