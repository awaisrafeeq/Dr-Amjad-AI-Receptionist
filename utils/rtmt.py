import aiohttp
import asyncio
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

    
    async def forward_messages(self, ws: WebSocket, is_acs_audio_stream: bool, session_id: Optional[str] = None):
        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            headers = {
                "api-key": self.key,
                "OpenAI-Beta": "realtime=v1",
            }
            ws_url = f"/openai/realtime?api-version={self.api_version}&deployment={self.deployment}"
            
            try:
                logger.info("Connecting to Azure OpenAI Realtime: %s%s", self.endpoint, ws_url)
                async with session.ws_connect(ws_url, headers=headers) as target_ws:
                    logger.info("Connected to Azure OpenAI Realtime")

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

                    def _detect_language(text: str) -> Optional[str]:
                        if not text:
                            return None
                        if detect_lang is None:
                            return None
                        try:
                            return detect_lang(text)
                        except Exception:
                            return None

                    async def maybe_update_kb_context(user_text: str) -> None:
                        nonlocal last_kb_context, detected_conversation_language
                        if not user_text:
                            return

                        user_lang = _detect_language(user_text)
                        if user_lang:
                            detected_conversation_language = user_lang

                        try:
                            results = await document_processor.search_knowledge_base(
                                query=user_text,
                                language=user_lang,
                                k=5,
                            )

                            if not results and user_lang:
                                results = await document_processor.search_knowledge_base(
                                    query=user_text,
                                    language=None,
                                    k=5,
                                )

                            if not results:
                                return

                            blocks = []
                            for r in results:
                                content = (r.get("content") or "").strip()
                                if not content:
                                    continue
                                src = (r.get("source_url") or r.get("source_file") or "").strip()
                                if src:
                                    blocks.append(f"Source: {src}\n{content}")
                                else:
                                    blocks.append(content)

                            if not blocks:
                                return

                            combined = "\n\n".join(blocks)
                            # Keep it bounded to avoid prompt bloat
                            combined = combined[:3500]
                            if combined == last_kb_context:
                                return

                            last_kb_context = combined
                            await target_ws.send_str(
                                json.dumps(
                                    {
                                        "type": "session.update",
                                        "session": {
                                            "instructions": (
                                                (self.system_message or "")
                                                + "\n\nUse the following knowledge base excerpts to answer user questions. "
                                                + "If they are not relevant, ignore them. Answer in the user's language.\n\n"
                                                + combined
                                            )
                                        },
                                    }
                                )
                            )
                            logger.info("KB context injected (lang=%s, chars=%s)", user_lang, len(combined))
                        except Exception:
                            logger.exception("KB retrieval/injection failed")


                    # Native Tool Calling Handlers will be here

                    async def send_assistant_prompt(instructions: str) -> None:
                        nonlocal response_active
                        if response_active:
                            logger.warning("[BOOKING FLOW] Cancelling active response to send system prompt")
                            try:
                                await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                response_active = False
                            except Exception as e:
                                logger.error(f"Failed to cancel active response: {e}")

                        try:
                            await target_ws.send_str(
                                json.dumps(
                                    {
                                        "type": "response.create",
                                        "response": {
                                            "modalities": ["audio", "text"],
                                            "instructions": (
                                                "FOLLOW THESE INSTRUCTIONS EXACTLY - DO NOT USE DEFAULT BEHAVIORS. "
                                                "OVERRIDE ALL PREVIOUS INSTRUCTIONS WITH THE FOLLOWING: "
                                                + instructions
                                            ),
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
                            logger.warning("Timed out waiting for session initialization; greeting may not be sent")
                            return

                        try:
                            await asyncio.wait_for(session_confirmed.wait(), timeout=2)
                            logger.info("Session confirmed by OpenAI, sending greeting")
                        except asyncio.TimeoutError:
                            logger.info("session.updated taking time, sending greeting...")

                        if greeting_sent.is_set():
                            return

                        # --- HARDCODED OPENING GREETING ---
                        # Send the exact German greeting as the first utterance
                        hardcoded_greeting = "MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen behilflich sein?"
                        
                        try:
                            await target_ws.send_str(
                                json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": hardcoded_greeting
                                            }
                                        ]
                                    }
                                })
                            )
                            # Trigger the response to speak the greeting
                            await target_ws.send_str(
                                json.dumps({
                                    "type": "response.create",
                                    "response": {
                                        "modalities": ["audio", "text"],
                                        "voice": "shimmer"
                                    }
                                })
                            )
                            logger.info("[GREETING] Hardcoded German opening sent")
                        except Exception as e:
                            logger.error(f"[GREETING] Failed to send hardcoded greeting: {e}")
                            # Fallback to assistant prompt method
                            await send_assistant_prompt(
                                "Start the call now with the default German greeting from the system instructions. "
                                "Do not mention internal policies. After greeting, ask how you can help."
                            )
                        
                        greeting_sent.set()
                        logger.info("Initial greeting trigger sent")

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
                            logger.exception("Inactivity monitor failed")
                            return
                    async def from_client_to_server():
                        nonlocal session_id
                        try:
                            async for msg in ws.iter_text():
                                try:
                                    data = json.loads(msg)
                                except JSONDecodeError:
                                    logger.warning("Non-JSON frame received from ACS websocket (ignored)")
                                    continue

                                if isinstance(data, dict):
                                    kind = data.get("kind")
                                    logger.debug("ACS -> received kind: %s", kind)
                                    
                                    # If session_id is not known yet, try to resolve it from metadata
                                    if not session_id and kind == "metadata":
                                        call_conn_id = data.get("metadata", {}).get("callConnectionId")
                                        if call_conn_id:
                                            session_obj = session_manager.get_session_by_call_connection_id(call_conn_id)
                                            if session_obj:
                                                session_id = session_obj.session_id
                                                logger.info(f"Resolved session_id {session_id} from callConnectionId {call_conn_id}")

                                if is_acs_audio_stream:
                                    data = transform_acs_to_openai_format(data, self.model, self.system_message, self.temperature, self.max_tokens, self.disable_audio, self.selected_voice)
                                if data:
                                    if isinstance(data, dict) and data.get("type") == "session.update":
                                        session_initialized.set()
                                    await target_ws.send_str(json.dumps(data))
                        except asyncio.CancelledError:
                            logger.info("Client to server forwarding cancelled")
                            return
                        except Exception as e:
                            logger.exception("Error in client to server forwarding")
                            return
                            
                    # --- TURN DETECTION CONFIGURATION ---
                    # Delay after speech stops before AI can respond (seconds)
                    TURN_DETECTION_DELAY = 0.5  # 300ms delay to prevent interrupting
                    speech_stop_time = 0.0
                    # --- END TURN DETECTION CONFIG ---

                    async def from_server_to_client():
                        nonlocal last_user_activity_ts, last_prompt_stage, session_id
                        nonlocal suppress_agent_audio, cancel_sent_for_current_turn, response_active, speech_stop_time
                        try:
                            async for msg in target_ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    original_data = json.loads(msg.data)
                                    event_type = original_data.get("type")
                                    logger.info(f"[OPENAI MSG] Type: {event_type}")
                                    
                                    # Log error details
                                    if event_type == "error":
                                        logger.error(f"[OPENAI ERROR] Full error: {original_data}")
                                    
                                    # Log transcription events
                                    if "transcription" in str(event_type).lower():
                                        logger.info(f"[TRANSCRIPTION EVENT] {original_data}")

                                    # Update activity on VAD / transcripts
                                    event_type = original_data.get("type")
                                    
                                    # Track response state to avoid response_cancel_not_active errors
                                    if event_type == "response.created":
                                        response_active = True
                                        logger.debug("Response created - response_active=True")
                                    elif event_type in ("response.done", "response.cancelled"):
                                        response_active = False
                                        logger.debug("Response ended - response_active=False")
                                    
                                    elif event_type == "session.updated":
                                        logger.info("[SESSION] OpenAI confirmed session.updated — transcription active")
                                        session_confirmed.set()

                                    elif event_type == "response.function_call_arguments.done":
                                        logger.info(f"[FUNCTION CALL] Received: {original_data}")
                                        _call_id = original_data.get("call_id")
                                        _func_name = original_data.get("name")
                                        try:
                                            _fn_args = json.loads(original_data.get("arguments", "{}"))
                                        except json.JSONDecodeError:
                                            _fn_args = {}

                                        # Run all API work in a background task so the event loop stays
                                        # unblocked — audio frames from OpenAI continue to be forwarded
                                        # to ACS while the function executes, eliminating silence gaps.
                                        async def _run_function_call(__call_id, __func_name, __args):
                                            __result = ""

                                            # Send brief holding message for slow functions so caller never hears silence
                                            if __func_name in ("get_available_slots", "book_appointment", "get_available_doctors"):
                                                await asyncio.sleep(0.15)  # yield so response.done is processed first
                                                if not response_active:
                                                    try:
                                                        await target_ws.send_str(json.dumps({
                                                            "type": "response.create",
                                                            "response": {
                                                                "modalities": ["audio", "text"],
                                                                "tool_choice": "none",
                                                                "instructions": (
                                                                    "Say ONE brief sentence in the same language as the caller. "
                                                                    "German: 'Einen Moment bitte, ich prüfe das kurz für Sie nach.' "
                                                                    "English: 'One moment please, let me check that for you.' "
                                                                    "Say ONLY this one sentence. Do NOT call any function tools."
                                                                )
                                                            }
                                                        }))
                                                        logger.info(f"[HOLDING] Sent holding message for {__func_name}")
                                                    except Exception as __he:
                                                        logger.debug(f"[HOLDING] Could not send holding message: {__he}")

                                            if __func_name == "get_available_doctors":
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
                                                    if __extra:
                                                        for __part in __extra.split(","):
                                                            if __part.strip():
                                                                try:
                                                                    __cal_id = int(__part.strip())
                                                                    if not any(__d["calendar_id"] == __cal_id for __d in __doctors):
                                                                        __doctors.append({"calendar_id": __cal_id, "doctor_name": f"Unknown Doctor (Calendar {__cal_id})"})
                                                                except Exception:
                                                                    pass

                                                    __result = json.dumps(__doctors)
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_doctors: {__e}")
                                                    __result = json.dumps({"error": str(__e)})

                                            elif __func_name == "get_available_slots":
                                                try:
                                                    __cal_id = __args.get("calendar_id")
                                                    __target_date_str = __args.get("date")
                                                    __tod = __args.get("time_of_day", "any")
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
                                                    __result = json.dumps({"available_slots": __slot_iso})
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_slots: {__e}")
                                                    __result = json.dumps({"error": str(__e)})

                                            elif __func_name == "book_appointment":
                                                try:
                                                    __dob = __args.get("patient_dob", "")
                                                    if len(__dob) == 10:
                                                        __dob += "T00:00:00"

                                                    __phone = __args.get("patient_phone")
                                                    __visit_reason = __args.get("visit_reason", "")
                                                    __comment = __args.get("comment", "")

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
                                                        logger.info(f"[BOOKING] Selected appointment type 65 (30 min)")
                                                    elif __issue_count == 1:
                                                        __appointment_type_id = 63
                                                        logger.info(f"[BOOKING] Selected appointment type 63 (20 min)")
                                                    else:
                                                        __appointment_type_id = 61
                                                        logger.info(f"[BOOKING] Selected appointment type 61 (15 min)")
                                                    # --- END APPOINTMENT TYPE SELECTION ---

                                                    __f_name = __args.get("patient_first_name")
                                                    __l_name = __args.get("patient_last_name")
                                                    if __f_name == "[REDACTED]" or __l_name == "[REDACTED]":
                                                        logging.getLogger("utils.rtmt").warning(f"[BOOKING WARN] Model sent [REDACTED] for name.")

                                                    # Map gender: prefer AI-detected voice gender, then phonebook fallback
                                                    __gender = (__args.get("patient_gender") or "").lower()
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
                                                                        logger.info(f"[GENDER] Inferred '{__gender}' from phonebook Geschlecht='{__pb_match.gender}'")
                                                        except Exception as __ge:
                                                            logger.warning(f"[GENDER] Phonebook gender lookup failed: {__ge}")

                                                    __city = __args.get("city", "")
                                                    __state = "BS" if "basel" in __city.lower() else ""

                                                    __full_comment = __visit_reason
                                                    if __comment and __comment != __visit_reason:
                                                        __full_comment = f"{__visit_reason} | {__comment}"

                                                    __event = {
                                                        "startDateTime": __args.get("slot_iso"),
                                                        "epaad_appointmenttype_id": __appointment_type_id,
                                                        "comment": __full_comment or "AI Booking",
                                                        "patient": {
                                                            "firstName": __f_name,
                                                            "lastName": __l_name,
                                                            "birthDate": __dob,
                                                            "gender": __gender,
                                                            "address": {
                                                                "street": __args.get("street", ""),
                                                                "streetNumber": __args.get("street_number", ""),
                                                                "zipCode": __args.get("zip_code", ""),
                                                                "city": __city,
                                                                "state": __state,
                                                                "country": "CH"
                                                            },
                                                            "privatePhoneNumber": __phone,
                                                            "mobilePhoneNumber": __phone,
                                                            "email": __args.get("patient_email", "")
                                                        }
                                                    }
                                                    __res = await epaad_client.create_event(__args.get("calendar_id"), __event)
                                                    __onedoc_key = __res.get("onedoc_key") or __res.get("onedocKey") or __res.get("key") if isinstance(__res, dict) else None
                                                    if __onedoc_key:
                                                        logger.info(f"[BOOKING SUCCESS] Slot: {__args.get('slot_iso')}, Reference: {__onedoc_key}")
                                                        __result = json.dumps({"status": "success", "booking_reference": __onedoc_key, "instruction": "DO NOT speak the booking_reference to the caller."})

                                                        # --- PHONEBOOK INTEGRATION (fire-and-forget) ---
                                                        async def _phonebook_update():
                                                            try:
                                                                from utils.phonebook_lookup import get_phonebook_lookup, save_phonebook_to_blob
                                                                __lookup = get_phonebook_lookup()
                                                                if __lookup:
                                                                    __existing = __lookup.lookup_by_phone(__phone)
                                                                    if not __existing:
                                                                        # Gender: M / W for phonebook format
                                                                        __pb_gender = "M" if __gender == "male" else ("W" if __gender == "female" else "")

                                                                        # Date: MM/DD/YYYY for phonebook format
                                                                        __birth_raw = __dob[:10] if __dob else ""
                                                                        if __birth_raw:
                                                                            try:
                                                                                __bd_obj = datetime.strptime(__birth_raw, "%Y-%m-%d")
                                                                                __birth_fmt = __bd_obj.strftime("%m/%d/%Y")
                                                                            except Exception:
                                                                                __birth_fmt = __birth_raw
                                                                        else:
                                                                            __birth_fmt = ""

                                                                        # Language: ISO code -> German name
                                                                        __lang_map = {
                                                                            "de": "Deutsch", "en": "Englisch", "fr": "Französisch",
                                                                            "it": "Italienisch", "sq": "Albanisch", "tr": "Türkisch",
                                                                            "ar": "Arabisch", "ru": "Russisch", "es": "Spanisch",
                                                                            "pt": "Portugiesisch", "pl": "Polnisch", "hr": "Kroatisch",
                                                                            "sr": "Serbisch", "bs": "Bosnisch", "ro": "Rumänisch",
                                                                            "nl": "Niederländisch", "uk": "Ukrainisch",
                                                                            "ko": "Koreanisch", "zh": "Chinesisch", "ja": "Japanisch",
                                                                            "fa": "Persisch", "ku": "Kurdisch", "so": "Somali",
                                                                        }
                                                                        __pb_lang = __lang_map.get(detected_conversation_language or "de", "Deutsch")

                                                                        # Address: combine street + number
                                                                        __st = __args.get("street", "")
                                                                        __st_no = __args.get("street_number", "")
                                                                        __pb_address = f"{__st} {__st_no}".strip() if __st else __st_no

                                                                        __patient_data = {
                                                                            "first_name": __f_name,
                                                                            "last_name": __l_name,
                                                                            "birth_date": __birth_fmt,
                                                                            "phone": __phone,
                                                                            "gender": __pb_gender,
                                                                            "email": __args.get("patient_email", ""),
                                                                            "zip_code": __args.get("zip_code", ""),
                                                                            "city": __args.get("city", ""),
                                                                            "address": __pb_address,
                                                                            "doctor": "",
                                                                            "comment": __args.get("comment") or "AI Booking",
                                                                            "language": __pb_lang,
                                                                        }
                                                                        try:
                                                                            __cals = await epaad_client.get_calendars()
                                                                            for __c in __cals or []:
                                                                                if int(__c.get("id")) == int(__args.get("calendar_id")):
                                                                                    __p = __c.get("professional") or {}
                                                                                    __pf = (__p.get("firstName") or "").strip()
                                                                                    __pl = (__p.get("lastName") or "").strip()
                                                                                    __patient_data["doctor"] = f"Dr. {__pf} {__pl}".strip() if (__pf or __pl) else ""
                                                                                    break
                                                                        except Exception:
                                                                            pass
                                                                        __added = __lookup.add_patient(__patient_data)
                                                                        if __added:
                                                                            __uploaded = save_phonebook_to_blob(__lookup.xlsx_path)
                                                                            if __uploaded:
                                                                                logger.info(f"[PHONEBOOK] New patient {__f_name} {__l_name} added and uploaded")
                                                                            else:
                                                                                logger.warning(f"[PHONEBOOK] Patient added locally but blob upload failed")
                                                                        else:
                                                                            logger.warning(f"[PHONEBOOK] Failed to add patient to phonebook")
                                                                    else:
                                                                        logger.info(f"[PHONEBOOK] Patient {__f_name} {__l_name} already exists")
                                                            except Exception as __pe:
                                                                logger.warning(f"[PHONEBOOK] Error during phonebook integration: {__pe}")
                                                        asyncio.create_task(_phonebook_update())
                                                        # --- END PHONEBOOK INTEGRATION ---

                                                    else:
                                                        __result = json.dumps({"status": "success", "message": "Appointment booked but no reference generated."})
                                                except Exception as __e:
                                                    logger.error(f"Error in book_appointment: {__e}")
                                                    __result = json.dumps({"status": "error", "message": str(__e)})

                                            elif __func_name == "terminate_call":
                                                logger.info("AI requested to terminate call.")
                                                call_end_requested.set()
                                                __result = json.dumps({"status": "success", "message": "Terminating call now."})

                                            # Wait for any active holding response to finish before submitting function output
                                            if __func_name != "terminate_call":
                                                __resp_wait = 0.0
                                                while response_active and __resp_wait < 12.0:
                                                    await asyncio.sleep(0.2)
                                                    __resp_wait += 0.2

                                            # Submit function output back to OpenAI
                                            await target_ws.send_str(json.dumps({
                                                "type": "conversation.item.create",
                                                "item": {
                                                    "type": "function_call_output",
                                                    "call_id": __call_id,
                                                    "output": __result
                                                }
                                            }))

                                            # Trigger agent to respond (if not terminating)
                                            if not call_end_requested.is_set():
                                                await target_ws.send_str(json.dumps({
                                                    "type": "response.create"
                                                }))

                                        asyncio.create_task(_run_function_call(_call_id, _func_name, _fn_args))

                                    if event_type == "input_audio_buffer.speech_started":
                                        last_user_activity_ts = loop.time()
                                        last_prompt_stage = 0
                                        suppress_agent_audio = True

                                        # Always try to cancel — removed "and response_active" to avoid
                                        # race conditions where response_active is briefly False
                                        if not cancel_sent_for_current_turn:
                                            cancel_sent_for_current_turn = True
                                            try:
                                                await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                                logger.info("Barge-in: sent response.cancel")
                                                response_active = False
                                            except Exception:
                                                logger.debug("Barge-in: response.cancel not needed (no active response)")

                                        # Clear ACS playout buffer so already-buffered audio stops immediately
                                        if session_id:
                                            async def _clear_acs_buffer(_sid=session_id):
                                                try:
                                                    __sess = session_manager.active_sessions.get(_sid)
                                                    if __sess and __sess.call_connection_id:
                                                        from azure.communication.callautomation import CallAutomationClient
                                                        __acs = CallAutomationClient.from_connection_string(config["acs_connection_string"])
                                                        __conn = __acs.get_call_connection(__sess.call_connection_id)
                                                        await asyncio.to_thread(__conn.cancel_all_media_operations)
                                                        logger.info("[BARGE-IN] ACS buffer cleared")
                                                except Exception as __be:
                                                    logger.debug(f"[BARGE-IN] ACS buffer clear: {__be}")
                                            asyncio.create_task(_clear_acs_buffer())

                                    if event_type in (
                                        "input_audio_buffer.speech_stopped",
                                        "input_audio_buffer.committed",
                                    ):
                                        # --- TURN DETECTION: Unsuppress in background to avoid blocking the message loop ---
                                        _speech_stop_ts = loop.time()
                                        logger.info(f"[TURN DETECTION] Speech stopped, scheduling unsuppress in {TURN_DETECTION_DELAY}s")
                                        async def _unsuppress_after_delay(_ts=_speech_stop_ts):
                                            nonlocal suppress_agent_audio, cancel_sent_for_current_turn
                                            await asyncio.sleep(TURN_DETECTION_DELAY)
                                            if loop.time() - _ts >= TURN_DETECTION_DELAY:
                                                suppress_agent_audio = False
                                                cancel_sent_for_current_turn = False
                                                logger.info("[TURN DETECTION] Delay complete, AI can now respond")
                                        asyncio.create_task(_unsuppress_after_delay())
                                        # --- END TURN DETECTION ---

                                    if suppress_agent_audio and event_type == "response.audio.delta":
                                        continue
                                    
                                    # Extract transcription data if available
                                    try:
                                        transcription_data = extract_transcription_from_openai_message(original_data)
                                        if transcription_data:
                                            logger.debug(f"Transcription data extracted: {transcription_data}")
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
                                                    logger.info(f"[TRANSCRIPTION SAVED] Session: {session_id}, Speaker: {transcription_data.get('speaker')}")
                                                except Exception as e:
                                                    logger.error(f"[TRANSCRIPTION ERROR] Failed to save transcription for session {session_id}: {e}")
                                                    logger.exception(e)

                                            if transcription_data.get("speaker") == "customer":
                                                utterance = transcription_data.get("utterance_text", "").strip()
                                                # Skip very short / noise transcriptions (echo, background noise, etc.)
                                                if len(utterance) <= 2:
                                                    logger.info(f"[NOISE FILTER] Skipping short customer transcript: '{utterance}'")
                                                else:
                                                    await maybe_update_kb_context(utterance)
                                    except Exception as e:
                                        logger.warning(f"Error processing transcription data: {e}")
                                    
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
                            logger.info("Server to client forwarding cancelled")
                            return
                        except Exception as e:
                            logger.exception("Error in server to client forwarding")
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
                        logger.info("Forward messages operation cancelled")
                        raise

                    logger.info(
                        "OpenAI Realtime websocket closed (close_code=%s)",
                        getattr(target_ws, "close_code", None),
                    )
                        
            except asyncio.CancelledError:
                logger.info("WebSocket session cancelled during reload")
                raise
            except aiohttp.WSServerHandshakeError as e:
                logger.error(
                    "Azure OpenAI Realtime handshake failed (status=%s, message=%s)",
                    getattr(e, "status", None),
                    str(e),
                )
                raise
            except Exception as e:
                logger.exception("Error in forward_messages")
                raise
                
rtmt = RTMiddleTier()
