import aiohttp
import asyncio
import base64
import json
from json import JSONDecodeError

# Try to use orjson for faster JSON parsing (10-20x faster, C-based)
try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    orjson = None
    _HAS_ORJSON = False
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
from utils.availability import build_next_available_recommendation, build_slot_recommendation, compute_free_slots, default_opening_hours

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
        self._prompt_path = "system_prompt.md"
        self._prompt_mtime: float = 0.0
        self.system_message = self._load_prompt()
        self._doctor_cache: Dict[int, str] = {}  # calendar_id -> doctor_name, avoids extra GET /calendars after booking

        # Preload phonebook at startup to avoid first-call delay
        self._preload_phonebook()

    def _preload_phonebook(self) -> None:
        """Preload phonebook lookup at startup to warm the cache."""
        try:
            from utils.phonebook_lookup import get_phonebook_lookup
            pb = get_phonebook_lookup()
            if pb:
                pb.load()
                logger.info(
                    f"[PHONE BOOK] Preloaded {pb._entry_count} entries across {len(pb._index)} phone variants"
                )
            else:
                logger.warning("[PHONE BOOK] Not available at startup")
        except Exception as e:
            logger.warning(f"[PHONE BOOK] Preload skipped: {e}")

    def _load_prompt(self) -> Optional[str]:
        """Load system prompt from file and track its modification time."""
        try:
            mtime = os.path.getmtime(self._prompt_path)
            self._prompt_mtime = mtime
            return load_prompt_from_markdown(self._prompt_path)
        except Exception as e:
            logger.error(f"[PROMPT] Failed to load {self._prompt_path}: {e}")
            return self.system_message  # keep existing if reload fails

    def _refresh_prompt_if_changed(self) -> None:
        """Reload system prompt only if the file has been modified since last load."""
        try:
            mtime = os.path.getmtime(self._prompt_path)
            if mtime > self._prompt_mtime:
                new_prompt = load_prompt_from_markdown(self._prompt_path)
                self.system_message = new_prompt
                self._prompt_mtime = mtime
                logger.info("[PROMPT] Reloaded (file changed)")
        except Exception:
            pass  # keep existing prompt on any error

    
    async def forward_messages(self, ws: WebSocket, is_acs_audio_stream: bool, session_id: Optional[str] = None):
        self._refresh_prompt_if_changed()
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
                    response_idle_event = asyncio.Event()
                    response_idle_event.set()  # Start in idle state

                    last_user_activity_ts = loop.time()
                    last_prompt_stage = 0
                    detected_conversation_language: Optional[str] = None

                    last_kb_context: Optional[str] = None

                    suppress_agent_audio = False
                    cancel_sent_for_current_turn = False
                    response_active = False
                    hangup_sent = False  # Guard against double hangup
                    _dynamic_tasks: list = []  # tracks fire-and-forget tasks for cleanup

                    # Per-session doctor validation: get_available_slots is blocked
                    # until get_available_doctors has been called and returned valid IDs.
                    _valid_calendar_ids: set = set()  # populated by get_available_doctors

                    # Cache calendars for this session to avoid duplicate API calls
                    _session_calendars: Optional[list] = None

                    # Transcription batching queue for async logging
                    _transcription_batch: list = []
                    _transcription_flush_task: Optional[asyncio.Task] = None

                    # Native Tool Calling Handlers will be here

                    async def wait_for_response_idle(timeout: float = 2.0) -> bool:
                        """Wait for any active response to finish or be cancelled using event-based waiting."""
                        nonlocal response_active
                        if not response_active and response_idle_event.is_set():
                            return True
                        
                        try:
                            await asyncio.wait_for(response_idle_event.wait(), timeout=timeout)
                            return True
                        except asyncio.TimeoutError:
                            return not response_active

                    def _json_dumps(obj) -> str:
                        """Use orjson if available for faster serialization."""
                        if _HAS_ORJSON:
                            return orjson.dumps(obj).decode('utf-8')
                        return json.dumps(obj)

                    def _resolved_phonebook_match() -> Optional[Dict[str, Any]]:
                        if not session_id:
                            return None
                        _sess = session_manager.active_sessions.get(session_id)
                        return _sess.phonebook_match if _sess else None

                    async def send_assistant_prompt(instructions: str) -> None:
                        nonlocal response_active
                        if response_active:
                            try:
                                await target_ws.send_str(_json_dumps({"type": "response.cancel"}))
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

                    async def _do_acs_hangup(reason: str) -> bool:
                        """Centralized ACS hangup with full diagnostic logging.
                        Returns True if hangup was sent, False otherwise."""
                        nonlocal hangup_sent
                        _tag = f"[HANGUP:{reason}]"
                        if hangup_sent:
                            logger.info(f"{_tag} Skipping — hangup already sent for session={session_id[:8] if session_id else '?'}")
                            return True
                        logger.info(f"{_tag} Attempting ACS hangup for session={session_id}")

                        if not session_id:
                            logger.error(f"{_tag} FAILED — session_id is None, cannot hang up")
                            return False

                        _sess = session_manager.active_sessions.get(session_id)
                        if not _sess:
                            logger.error(f"{_tag} FAILED — session {session_id[:8]} not found in active_sessions")
                            return False

                        _conn_id = _sess.call_connection_id
                        if not _conn_id:
                            logger.error(
                                f"{_tag} FAILED — session {session_id[:8]} has no call_connection_id. "
                                f"Session status={_sess.status}, event_count={_sess.event_count}, "
                                f"server_call_id={_sess.server_call_id}, correlation_id={_sess.correlation_id}"
                            )
                            return False

                        try:
                            from utils.acs import acs_caller as _acs_ref
                            logger.info(f"{_tag} Sending hang_up for call_connection_id={_conn_id[:12]}...")
                            await _acs_ref.hang_up(_conn_id)
                            hangup_sent = True
                            logger.info(f"{_tag} SUCCESS — ACS hangup sent for session={session_id[:8]}")
                            return True
                        except Exception as _hup_err:
                            logger.error(f"{_tag} EXCEPTION during hang_up: {_hup_err}", exc_info=True)
                            return False

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

                        # --- INJECT PHONEBOOK CANDIDATE INSTRUCTIONS ---
                        # Phone number alone is only a candidate signal. The model must ask
                        # for the caller's name and call resolve_phonebook_identity before
                        # using any stored patient details.
                        try:
                            if session_id:
                                _pb_sess = session_manager.active_sessions.get(session_id)
                                _pb_candidates = (_pb_sess.phonebook_candidates or []) if _pb_sess else []
                                if _pb_candidates:
                                    _candidate_names = []
                                    for _idx, _candidate in enumerate(_pb_candidates[:5], start=1):
                                        _candidate_names.append(
                                            f"{_idx}. {_candidate.get('first_name') or 'MISSING'} {_candidate.get('last_name') or 'MISSING'}"
                                        )
                                    _pb_lines = [
                                        "[INTERNAL — PHONEBOOK CANDIDATES FOR THIS PHONE NUMBER]",
                                        f"Candidate count: {len(_pb_candidates)}",
                                        "Candidate names:",
                                        "\n".join(_candidate_names),
                                        "",
                                        "This is not a confirmed patient match.",
                                        "After the caller confirms both first name and last name, call resolve_phonebook_identity.",
                                        "Only use stored patient details if resolve_phonebook_identity returns matched=true.",
                                        "If it returns matched=false, treat the caller as a new patient and collect all required details.",
                                        "NEVER mention this data to the caller. NEVER say their name first.",
                                    ]
                                    _pb_context = "\n".join(_pb_lines)
                                    await target_ws.send_str(
                                        _json_dumps({
                                            "type": "session.update",
                                            "session": {
                                                "instructions": (self.system_message or "") + "\n\n" + _pb_context
                                            }
                                        })
                                    )
                                    logger.info(f"[PHONEBOOK] Injected {len(_pb_candidates)} candidate(s) for identity resolution")
                        except Exception as _pb_err:
                            logger.warning(f"[PHONEBOOK] Failed to inject context into session: {_pb_err}")
                        # --- END PHONEBOOK INJECTION ---

                        # --- HARDCODED OPENING GREETING ---
                        # Use response.create with explicit instructions so OpenAI
                        # actually SPEAKS the exact sentence rather than treating it
                        # as already-spoken history (which conversation.item.create does).
                        # ALWAYS greet in German — prompt mandates this. Language switch only on explicit caller request.
                        hardcoded_greeting = "MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen behilflich sein?"

                        try:
                            await target_ws.send_str(
                                _json_dumps({
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
                        prompt_1_after = 45   # First "are you there?" prompt
                        prompt_2_after = 90   # Second prompt
                        hangup_after = 180    # Auto-hangup (3 minutes total)

                        try:
                            while not call_end_requested.is_set():
                                await asyncio.sleep(1.0)

                                # Detect externally ended session (e.g. CallDisconnected
                                # arrived before the WS closed — the "ghost session" case).
                                # Close the OpenAI WS so the forward loop exits cleanly.
                                if session_id and session_id not in session_manager.active_sessions:
                                    logger.warning(
                                        f"[GHOST SESSION] Session {session_id[:8]} no longer in active_sessions "
                                        f"— closing OpenAI WebSocket to stop ghost processing"
                                    )
                                    call_end_requested.set()
                                    try:
                                        await target_ws.close()
                                    except Exception:
                                        pass
                                    return

                                if not greeting_sent.is_set():
                                    continue

                                idle_for = loop.time() - last_user_activity_ts

                                _lang = detected_conversation_language or "de"
                                _lang_instruction = f"Respond in the language the caller is speaking (currently '{_lang}'). "

                                if idle_for >= hangup_after and last_prompt_stage < 3:
                                    last_prompt_stage = 3
                                    logger.info(f"[INACTIVITY] {idle_for:.0f}s idle — initiating hangup for session={session_id}")
                                    await send_assistant_prompt(
                                        f"{_lang_instruction}"
                                        "Tell the caller it seems we got disconnected or they are not available, "
                                        "you will end the call now, and they can call MedCenter Volta again anytime. Say goodbye."
                                    )
                                    call_end_requested.set()
                                    # Wait for goodbye audio to play before hanging up (reduced from 5s to 2s)
                                    await asyncio.sleep(2.0)
                                    _hung = await _do_acs_hangup("inactivity")
                                    if not _hung:
                                        logger.error("[INACTIVITY] Hangup failed — call may remain connected")
                                    return

                                if idle_for >= prompt_2_after and last_prompt_stage < 2:
                                    last_prompt_stage = 2
                                    await send_assistant_prompt(
                                        f"{_lang_instruction}"
                                        "Ask the caller if they are still there, and if they need help with an appointment or general information."
                                    )
                                    continue

                                if idle_for >= prompt_1_after and last_prompt_stage < 1:
                                    last_prompt_stage = 1
                                    await send_assistant_prompt(
                                        f"{_lang_instruction}"
                                        "Ask the caller if they are still there and how you may assist them today."
                                    )
                                    continue

                        except asyncio.CancelledError:
                            logger.info("[INACTIVITY] Monitor cancelled (call likely ended externally)")
                            return
                        except Exception:
                            logger.exception("Inactivity monitor error")
                            return

                    async def _flush_transcriptions():
                        """Flush batched transcriptions to database every 2 seconds."""
                        nonlocal _transcription_batch, _transcription_flush_task
                        while _transcription_batch:
                            batch = _transcription_batch[:10]  # Process up to 10 at a time
                            _transcription_batch = _transcription_batch[10:]
                            for item in batch:
                                try:
                                    await session_manager.log_transcription(**item)
                                except Exception as e:
                                    logger.error(f"[TRANSCRIPT BATCH ERROR] {e}")
                            await asyncio.sleep(0.1)  # Small delay between batches
                        _transcription_flush_task = None

                    async def from_client_to_server():
                        nonlocal session_id
                        try:
                            async for msg in ws.iter_text():
                                try:
                                    data = json.loads(msg) if not _HAS_ORJSON else orjson.loads(msg)
                                except (JSONDecodeError, Exception):
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
                                    await target_ws.send_str(_json_dumps(data))
                        except asyncio.CancelledError:
                            logger.debug("Client→server cancelled")
                            return
                        except Exception as e:
                            logger.exception("Client→server error")
                            return
                            
                    # --- TURN DETECTION CONFIGURATION ---
                    # Delay after speech stops before AI can respond (seconds)
                    TURN_DETECTION_DELAY = 0.3  # Wait 0.3s after speech stops to filter phantom transcriptions
                    speech_stop_time = 0.0
                    unsuppress_scheduled = False  # Flag to prevent duplicate scheduling
                    # --- END TURN DETECTION CONFIG ---

                    async def from_server_to_client():
                        nonlocal last_user_activity_ts, last_prompt_stage, session_id, detected_conversation_language
                        nonlocal suppress_agent_audio, cancel_sent_for_current_turn, response_active, speech_stop_time, unsuppress_scheduled
                        nonlocal _transcription_flush_task
                        try:
                            async for msg in target_ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    original_data = json.loads(msg.data) if not _HAS_ORJSON else orjson.loads(msg.data)
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
                                        response_idle_event.clear()
                                        logger.debug("Response created")
                                    elif event_type in ("response.done", "response.cancelled"):
                                        response_active = False
                                        response_idle_event.set()
                                        logger.debug("Response ended")
                                    
                                    elif event_type == "session.updated":
                                        logger.debug("Session updated — transcription active")
                                        session_confirmed.set()

                                    elif event_type == "response.function_call_arguments.done":
                                        _call_id = original_data.get("call_id")
                                        _func_name = original_data.get("name")
                                        logger.info(f"[FUNCTION CALL] OpenAI invoked: {_func_name} (call_id={_call_id}, session={session_id})")
                                        try:
                                            _args_str = original_data.get("arguments", "{}")
                                            _fn_args = orjson.loads(_args_str) if _HAS_ORJSON else json.loads(_args_str)
                                        except (json.JSONDecodeError, Exception):
                                            _fn_args = {}

                                        # Run all API work in a background task so the event loop stays
                                        # unblocked — audio frames from OpenAI continue to be forwarded
                                        # to ACS while the function executes, eliminating silence gaps.
                                        async def _run_function_call(_call_id, _func_name, _args):
                                            nonlocal detected_conversation_language
                                            _result = ""

                                            # Send brief holding message for slow functions so caller never hears silence
                                            if _func_name in ("get_available_slots", "get_next_available_slot", "book_appointment", "get_available_doctors"):
                                                # Removed sleep(0.15) - yield not needed with proper event handling
                                                if not response_active:
                                                    try:
                                                        await target_ws.send_str(_json_dumps({
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

                                            async def _get_cached_calendars():
                                                """Get calendars with session-level caching to avoid duplicate API calls."""
                                                nonlocal _session_calendars
                                                if _session_calendars is None:
                                                    _session_calendars = await epaad_client.get_calendars()
                                                return _session_calendars

                                            if _func_name == "resolve_phonebook_identity":
                                                try:
                                                    __first = (_args.get("patient_first_name") or "").strip()
                                                    __last = (_args.get("patient_last_name") or "").strip()
                                                    __resolution = session_manager.resolve_phonebook_identity(
                                                        session_id=session_id,
                                                        first_name=__first,
                                                        last_name=__last,
                                                    )
                                                    logger.info(
                                                        "[PHONEBOOK] Identity resolution session=%s matched=%s candidate_count=%s",
                                                        session_id,
                                                        __resolution.get("matched"),
                                                        __resolution.get("candidate_count"),
                                                    )
                                                    _result = _json_dumps(__resolution)
                                                except Exception as __e:
                                                    logger.error(f"Error in resolve_phonebook_identity: {__e}")
                                                    _result = _json_dumps({"matched": False, "is_new_patient": False, "status": "error", "message": str(__e)})

                                            elif _func_name == "get_available_doctors":
                                                __doctors = []
                                                try:
                                                    __calendars = await _get_cached_calendars()
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

                                                    # Store valid calendar IDs for this session so
                                                    # get_available_slots can validate against them.
                                                    _valid_calendar_ids.clear()
                                                    for __d in __doctors:
                                                        _valid_calendar_ids.add(__d["calendar_id"])
                                                    logger.info(f"[DOCTORS] Valid calendar IDs for session: {_valid_calendar_ids}")

                                                    _result = _json_dumps(__doctors)
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_doctors: {__e}")
                                                    _result = _json_dumps({"error": str(__e)})

                                            elif _func_name == "get_available_slots":
                                                try:
                                                    __cal_id = _args.get("calendar_id")
                                                    __target_date_str = _args.get("date")
                                                    __tod = _args.get("time_of_day", "any")

                                                    # --- ENFORCE: get_available_doctors must be called first ---
                                                    if not _valid_calendar_ids:
                                                        logger.warning(f"[SLOTS BLOCKED] get_available_slots called without prior get_available_doctors. calendar_id={__cal_id}, session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": "You must call get_available_doctors first before checking slots. "
                                                            "Ask the caller which doctor they prefer, then call get_available_doctors to get the list, "
                                                            "let the caller choose, and only then call get_available_slots with the correct calendar_id."
                                                        })
                                                        raise ValueError("blocked: doctors not fetched")

                                                    try:
                                                        __cal_id_int = int(__cal_id)
                                                    except (TypeError, ValueError):
                                                        __cal_id_int = None

                                                    if __cal_id_int not in _valid_calendar_ids:
                                                        logger.warning(f"[SLOTS BLOCKED] Invalid calendar_id={__cal_id}. Valid IDs: {_valid_calendar_ids}. session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": f"calendar_id {__cal_id} is not valid. "
                                                            f"Valid calendar IDs from get_available_doctors are: {sorted(_valid_calendar_ids)}. "
                                                            "Please use one of these IDs based on the doctor the caller selected."
                                                        })
                                                        raise ValueError("blocked: invalid calendar_id")
                                                    # --- END ENFORCEMENT ---

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
                                                    __recommendation = build_slot_recommendation(
                                                        slots=__slots,
                                                        target_date=__target_day,
                                                        requested_time_of_day=__tod,
                                                    )
                                                    __recommendation["available_slots"] = __slot_iso
                                                    _result = _json_dumps(__recommendation)
                                                except ValueError:
                                                    # Validation block (doctors not fetched / invalid ID)
                                                    # _result was already set above, just pass through
                                                    pass
                                                except Exception as __e:
                                                    logger.error(f"Error in get_available_slots: {__e}")
                                                    _result = _json_dumps({"error": str(__e)})

                                            elif _func_name == "get_next_available_slot":
                                                try:
                                                    __cal_id = _args.get("calendar_id")
                                                    __tod = _args.get("time_of_day", "any")
                                                    if __tod not in ("morning", "afternoon", "any"):
                                                        __tod = "any"

                                                    # Keep the production default predictable and bounded.
                                                    try:
                                                        __search_window_days = int(_args.get("search_window_days") or 14)
                                                    except (TypeError, ValueError):
                                                        __search_window_days = 14
                                                    __search_window_days = max(1, min(__search_window_days, 14))

                                                    # --- ENFORCE: get_available_doctors must be called first ---
                                                    if not _valid_calendar_ids:
                                                        logger.warning(f"[NEXT SLOT BLOCKED] get_next_available_slot called without prior get_available_doctors. calendar_id={__cal_id}, session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": "You must call get_available_doctors first before checking next available slots. "
                                                            "Ask the caller which doctor they prefer, then call get_available_doctors to get the list, "
                                                            "let the caller choose, and only then call get_next_available_slot with the correct calendar_id."
                                                        })
                                                        raise ValueError("blocked: doctors not fetched")

                                                    try:
                                                        __cal_id_int = int(__cal_id)
                                                    except (TypeError, ValueError):
                                                        __cal_id_int = None

                                                    if __cal_id_int not in _valid_calendar_ids:
                                                        logger.warning(f"[NEXT SLOT BLOCKED] Invalid calendar_id={__cal_id}. Valid IDs: {_valid_calendar_ids}. session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": f"calendar_id {__cal_id} is not valid. "
                                                            f"Valid calendar IDs from get_available_doctors are: {sorted(_valid_calendar_ids)}. "
                                                            "Please use one of these IDs based on the doctor the caller selected."
                                                        })
                                                        raise ValueError("blocked: invalid calendar_id")
                                                    # --- END ENFORCEMENT ---

                                                    __now_dt = datetime.now(ZoneInfo("Europe/Zurich"))
                                                    __start_date_arg = (_args.get("start_date") or "").strip()
                                                    if __start_date_arg:
                                                        try:
                                                            __start_day = date.fromisoformat(__start_date_arg)
                                                        except ValueError:
                                                            __start_day = __now_dt.date()
                                                        if __start_day < __now_dt.date():
                                                            __start_day = __now_dt.date()
                                                    else:
                                                        __start_day = __now_dt.date()

                                                    __end_day = __start_day + timedelta(days=__search_window_days - 1)
                                                    __start_dt = datetime.combine(__start_day, datetime.min.time())
                                                    __end_dt = datetime.combine(__end_day, datetime.max.time()).replace(microsecond=0)

                                                    __events = await epaad_client.get_events(
                                                        calendar_id=__cal_id_int,
                                                        from_dt=__start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        until_dt=__end_dt.strftime("%Y-%m-%dT%H:%M:%S")
                                                    )

                                                    __opening_hours = default_opening_hours()
                                                    __all_slots = []
                                                    __working_days_count = 0
                                                    for __day_offset in range(__search_window_days):
                                                        __day = __start_day + timedelta(days=__day_offset)
                                                        if __opening_hours.windows_by_weekday.get(__day.weekday(), []):
                                                            __working_days_count += 1

                                                        __day_slots = compute_free_slots(
                                                            events=__events or [],
                                                            target_date=__day,
                                                            opening_hours=__opening_hours,
                                                            slot_minutes=15,
                                                            time_of_day=__tod,
                                                            now_dt=__now_dt,
                                                        )
                                                        __all_slots.extend(__day_slots)

                                                    __recommendation = build_next_available_recommendation(
                                                        slots=__all_slots,
                                                        start_date=__start_day,
                                                        search_window_days=__search_window_days,
                                                        requested_time_of_day=__tod,
                                                        searched_working_days_count=__working_days_count,
                                                    )
                                                    __recommendation["available_slots"] = [
                                                        __s.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
                                                        for __s in sorted(__all_slots)[:10]
                                                    ]
                                                    logger.info(
                                                        f"[NEXT SLOT] calendar_id={__cal_id_int}, time_of_day={__tod}, "
                                                        f"window={__search_window_days}d, slots={len(__all_slots)}"
                                                    )
                                                    _result = _json_dumps(__recommendation)
                                                except ValueError:
                                                    # Validation block (doctors not fetched / invalid ID / bad date)
                                                    pass
                                                except Exception as __e:
                                                    logger.error(f"Error in get_next_available_slot: {__e}")
                                                    _result = _json_dumps({"error": str(__e)})

                                            elif _func_name == "book_appointment":
                                                try:
                                                    # --- ENFORCE: validate calendar_id before booking ---
                                                    __book_cal_id = _args.get("calendar_id")
                                                    try:
                                                        __book_cal_id_int = int(__book_cal_id)
                                                    except (TypeError, ValueError):
                                                        __book_cal_id_int = None

                                                    if not _valid_calendar_ids:
                                                        logger.warning(f"[BOOKING BLOCKED] book_appointment called without prior get_available_doctors. calendar_id={__book_cal_id}, session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": "You must call get_available_doctors first to get valid doctor calendar IDs before booking. "
                                                            "Ask the caller which doctor they prefer, call get_available_doctors, then proceed."
                                                        })
                                                        raise ValueError("blocked: doctors not fetched")

                                                    if __book_cal_id_int not in _valid_calendar_ids:
                                                        logger.warning(f"[BOOKING BLOCKED] Invalid calendar_id={__book_cal_id}. Valid IDs: {_valid_calendar_ids}. session={session_id}")
                                                        _result = _json_dumps({
                                                            "error": f"calendar_id {__book_cal_id} is not valid. "
                                                            f"Valid calendar IDs are: {sorted(_valid_calendar_ids)}. "
                                                            "Use the correct ID for the doctor the caller chose."
                                                        })
                                                        raise ValueError("blocked: invalid calendar_id")
                                                    # --- END ENFORCEMENT ---

                                                    # Resolve identity before autofill. Existing patient requires
                                                    # caller phone + first name + last name to all match.
                                                    __f_name = _args.get("patient_first_name")
                                                    __l_name = _args.get("patient_last_name")
                                                    if __f_name == "[REDACTED]" or __l_name == "[REDACTED]":
                                                        logging.getLogger("utils.rtmt").warning(f"[BOOKING WARN] Model sent [REDACTED] for name.")

                                                    try:
                                                        if session_id and __f_name and __l_name and not _resolved_phonebook_match():
                                                            __resolution = session_manager.resolve_phonebook_identity(
                                                                session_id=session_id,
                                                                first_name=__f_name,
                                                                last_name=__l_name,
                                                            )
                                                            logger.info(
                                                                "[PHONEBOOK] Booking-time identity resolution matched=%s candidate_count=%s",
                                                                __resolution.get("matched"),
                                                                __resolution.get("candidate_count"),
                                                            )
                                                    except Exception as __resolve_err:
                                                        logger.warning(f"[PHONEBOOK] Booking-time identity resolution failed: {__resolve_err}")

                                                    # --- AUTO-FILL FROM PHONEBOOK ---
                                                    # If OpenAI didn't provide data (marked MISSING or empty), use the resolved match only.
                                                    try:
                                                        __pb_match = _resolved_phonebook_match()
                                                        if __pb_match:
                                                            # Auto-fill DOB
                                                            if not _args.get("patient_dob") or _args.get("patient_dob") in ("MISSING", ""):
                                                                if __pb_match.get("birth_date") and __pb_match.get("birth_date") not in ("MISSING", ""):
                                                                    _args["patient_dob"] = __pb_match["birth_date"]
                                                                    logger.info(f"[BOOKING AUTO-FILL] DOB from resolved phonebook match: {__pb_match['birth_date']}")
                                                            # Auto-fill street/address
                                                            if not _args.get("street") or _args.get("street") in ("MISSING", ""):
                                                                if __pb_match.get("address") and __pb_match.get("address") not in ("MISSING", ""):
                                                                    _args["street"] = __pb_match["address"]
                                                                    logger.info(f"[BOOKING AUTO-FILL] Address from resolved phonebook match: {__pb_match['address']}")
                                                            # Auto-fill zip_code
                                                            if not _args.get("zip_code") or _args.get("zip_code") in ("MISSING", ""):
                                                                if __pb_match.get("zip_code") and __pb_match.get("zip_code") not in ("MISSING", ""):
                                                                    _args["zip_code"] = __pb_match["zip_code"]
                                                                    logger.info(f"[BOOKING AUTO-FILL] Zip from resolved phonebook match: {__pb_match['zip_code']}")
                                                            # Auto-fill city
                                                            if not _args.get("city") or _args.get("city") in ("MISSING", ""):
                                                                if __pb_match.get("city") and __pb_match.get("city") not in ("MISSING", ""):
                                                                    _args["city"] = __pb_match["city"]
                                                                    logger.info(f"[BOOKING AUTO-FILL] City from resolved phonebook match: {__pb_match['city']}")
                                                            # Auto-fill email
                                                            if not _args.get("patient_email") or _args.get("patient_email") in ("MISSING", ""):
                                                                if __pb_match.get("email") and __pb_match.get("email") not in ("MISSING", ""):
                                                                    _args["patient_email"] = __pb_match["email"]
                                                                    logger.info(f"[BOOKING AUTO-FILL] Email from resolved phonebook match: {__pb_match['email']}")
                                                    except Exception as __auto_err:
                                                        logger.warning(f"[BOOKING AUTO-FILL] Failed to auto-fill from phonebook: {__auto_err}")
                                                    # --- END AUTO-FILL ---

                                                    __dob = _args.get("patient_dob", "")
                                                    if len(__dob) == 10:
                                                        __dob += "T00:00:00"
                                                    __phone = _args.get("patient_phone")
                                                    # Ensure we always have the real caller phone number
                                                    try:
                                                        __caller_sess = session_manager.active_sessions.get(session_id)
                                                        if __caller_sess and __caller_sess.participants:
                                                            __acs_phone = __caller_sess.participants[0].get("phone_number")
                                                            if __acs_phone:
                                                                if not __phone or __phone in ("MISSING", "+41", "+41..."):
                                                                    __phone = __acs_phone
                                                    except Exception:
                                                        pass
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

                                                    # Map gender: prefer AI-detected voice gender, then phonebook fallback
                                                    __gender = (_args.get("patient_gender") or "").lower()
                                                    if __gender not in ["male", "female"]:
                                                        __gender = "other"
                                                        try:
                                                            __pb_match = _resolved_phonebook_match()
                                                            if __pb_match and __pb_match.get("gender"):
                                                                __g = __pb_match["gender"].strip().lower()
                                                                if __g in ("m", "männlich", "male", "maennlich"):
                                                                    __gender = "male"
                                                                elif __g in ("w", "f", "weiblich", "female", "frau"):
                                                                    __gender = "female"
                                                                logger.info(f"[GENDER] From resolved phonebook match: '{__gender}'")
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
                                                        _result = _json_dumps({"status": "success", "booking_reference": __onedoc_key, "instruction": "DO NOT speak the booking_reference to the caller. Confirm the appointment details briefly, then ask if there is anything else you can help with. Only call terminate_call after the caller confirms they have no further questions and you have said goodbye."})

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

                                                                # Look up doctor name — use in-memory cache first, then session cache
                                                                try:
                                                                    _cal_id_int = int(_pb_args.get("calendar_id") or 0)
                                                                    if _cal_id_int and _cal_id_int in self._doctor_cache:
                                                                        patient_data["doctor"] = self._doctor_cache[_cal_id_int]
                                                                    elif _cal_id_int:
                                                                        # Use session-cached calendars to avoid extra API call
                                                                        cals = await _get_cached_calendars()
                                                                        for cal in cals or []:
                                                                            if int(cal.get("id")) == _cal_id_int:
                                                                                prof = cal.get("professional") or {}
                                                                                pf = (prof.get("firstName") or "").strip()
                                                                                pl = (prof.get("lastName") or "").strip()
                                                                                patient_data["doctor"] = f"Dr. {pf} {pl}".strip() if (pf or pl) else ""
                                                                                break
                                                                except Exception:
                                                                    pass

                                                                # --- Add only (never overwrite existing patient) ---
                                                                # Rule: if phone + first + last name all match → patient already
                                                                # exists, skip. Only add when this is a genuinely new person.
                                                                exact_match = pb_lookup.lookup_by_phone_and_name(acs_num, _pb_f_name, _pb_l_name)
                                                                if exact_match:
                                                                    logger.info(f"[PHONEBOOK] Patient already exists — skipping for {_pb_f_name} {_pb_l_name}")
                                                                else:
                                                                    # New person (or different name on same number) → add new row
                                                                    logger.info(f"[PHONEBOOK] New patient — adding entry for {_pb_f_name} {_pb_l_name}")
                                                                    added = await asyncio.to_thread(pb_lookup.add_patient, patient_data)
                                                                    if not added:
                                                                        logger.warning("[PHONEBOOK] Failed to add new patient")
                                                            except Exception as pe:
                                                                logger.warning(f"[PHONEBOOK] Error during phonebook integration: {pe}")
                                                        _dynamic_tasks.append(asyncio.create_task(_phonebook_update()))
                                                        # --- END PHONEBOOK INTEGRATION ---

                                                        # --- AUTO-HANGUP FALLBACK ---
                                                        # If the AI fails to call terminate_call after booking,
                                                        # auto-hangup after 60 seconds so the call doesn't hang.
                                                        # Longer timeout since AI now asks "anything else?" before ending.
                                                        async def _auto_hangup_fallback():
                                                            await asyncio.sleep(60)
                                                            if not call_end_requested.is_set():
                                                                logger.warning(f"[AUTO-HANGUP] 60s passed after booking — terminate_call was NOT called. session={session_id}")
                                                                call_end_requested.set()
                                                                _hung = await _do_acs_hangup("auto_hangup_post_booking")
                                                                if not _hung:
                                                                    logger.error("[AUTO-HANGUP] Fallback hangup FAILED — call may remain connected")
                                                            else:
                                                                logger.debug("[AUTO-HANGUP] call_end_requested already set — skipping (terminate_call was called)")
                                                        _dynamic_tasks.append(asyncio.create_task(_auto_hangup_fallback()))
                                                        # --- END AUTO-HANGUP FALLBACK ---

                                                    else:
                                                        _result = _json_dumps({"status": "success", "message": "Appointment booked but no reference generated."})
                                                except ValueError:
                                                    # Validation block (doctors not fetched / invalid ID)
                                                    # _result was already set above, just pass through
                                                    pass
                                                except Exception as __e:
                                                    logger.error(f"Error in book_appointment: {__e}")
                                                    _result = _json_dumps({"status": "error", "message": str(__e)})

                                            elif _func_name == "terminate_call":
                                                logger.info(f"[CALL END] AI requested terminate_call — session={session_id}")
                                                call_end_requested.set()
                                                _result = _json_dumps({"status": "success", "message": "Terminating call now."})

                                                # Log session state at the moment of terminate
                                                _tc_sess = session_manager.active_sessions.get(session_id)
                                                if _tc_sess:
                                                    logger.info(
                                                        f"[CALL END] Session state: call_connection_id={_tc_sess.call_connection_id}, "
                                                        f"status={_tc_sess.status}, event_count={_tc_sess.event_count}, "
                                                        f"server_call_id={_tc_sess.server_call_id}"
                                                    )
                                                else:
                                                    logger.error(f"[CALL END] Session {session_id} NOT FOUND in active_sessions at terminate_call time!")

                                                # Wait for the agent's goodbye audio to finish playing (reduced from 4s to 1.5s)
                                                logger.info("[CALL END] Waiting 1.5s for goodbye audio to finish...")
                                                await asyncio.sleep(1.5)

                                                # Actually hang up the ACS call
                                                _hung = await _do_acs_hangup("terminate_call")
                                                if not _hung:
                                                    logger.error("[CALL END] terminate_call hangup FAILED — call may remain connected")

                                            elif _func_name == "store_insurance_card_number":
                                                __card = _args.get("card_number", "").strip()
                                                # Remove any spaces/dashes for validation
                                                __card_digits = re.sub(r'[\s\-]', '', __card)
                                                if __card_digits and session_id:
                                                    # Validate: must start with 807 and be exactly 20 digits
                                                    if not __card_digits.startswith("807"):
                                                        _result = _json_dumps({"status": "error", "message": "Invalid card number — it must start with 807. Please ask the caller to check the number on the front of their card and try again."})
                                                    elif len(__card_digits) != 20:
                                                        _result = _json_dumps({"status": "error", "message": f"Invalid card number — it must be exactly 20 digits. The number provided has {len(__card_digits)} digits. Please ask the caller to re-read the complete number."})
                                                    elif not __card_digits.isdigit():
                                                        _result = _json_dumps({"status": "error", "message": "Invalid card number — it must contain only digits. Please ask the caller to re-read the number."})
                                                    else:
                                                        __sess = session_manager.active_sessions.get(session_id)
                                                        if __sess:
                                                            __sess.insurance_card_number = __card_digits
                                                            logger.info(f"[INSURANCE] Stored card number for session {session_id[:8]}")
                                                        _result = _json_dumps({"status": "success", "message": "Insurance card number stored for documentation."})
                                                else:
                                                    _result = _json_dumps({"status": "error", "message": "No card number provided or no active session."})

                                            elif _func_name == "search_knowledge_base":
                                                try:
                                                    __query = _args.get("query", "")
                                                    _results = await document_processor.search_knowledge_base(query=__query, k=5)
                                                    if not _results:
                                                        _result = _json_dumps({"status": "no_results_found", "message": "No information found in the knowledge base."})
                                                    else:
                                                        __blocks = []
                                                        for __r in _results:
                                                            __content = (__r.get("content") or "").strip()
                                                            if __content:
                                                                __blocks.append(__content)
                                                        _result = _json_dumps({"status": "success", "information": "\n\n".join(__blocks)[:2000]})
                                                except Exception as __e:
                                                    logger.error(f"Error in search_knowledge_base: {__e}")
                                                    _result = _json_dumps({"status": "error", "message": str(__e)})

                                            # Cancel any still-active holding response before sending tool output.
                                            # This prevents the AI from saying "no slots available" (guess)
                                            # then "actually I see slots" (real data) — the contradiction problem.
                                            if _func_name != "terminate_call":
                                                if response_active:
                                                    try:
                                                        await target_ws.send_str(_json_dumps({"type": "response.cancel"}))
                                                        logger.debug(f"[TOOL] Cancelled holding response before submitting {_func_name} result")
                                                    except Exception:
                                                        pass
                                                # Always wait for response to finish before submitting tool output
                                                await wait_for_response_idle(timeout=2.0)

                                            # Submit function output back to OpenAI
                                            logger.info(f"[FUNCTION RESULT] Sending output for {_func_name} (call_id={_call_id}): {_result[:120]}")
                                            await target_ws.send_str(_json_dumps({
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
                                                logger.debug(f"[FUNCTION RESULT] Sending response.create after {_func_name}")
                                                await target_ws.send_str(_json_dumps({
                                                    "type": "response.create"
                                                }))
                                            else:
                                                logger.info(f"[FUNCTION RESULT] Skipping response.create — call_end_requested is set (func={_func_name})")

                                        _dynamic_tasks.append(asyncio.create_task(_run_function_call(_call_id, _func_name, _fn_args)))

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
                                            _dynamic_tasks.append(asyncio.create_task(_unsuppress_after_delay()))
                                        # --- END TURN DETECTION ---

                                    if suppress_agent_audio and event_type == "response.audio.delta":
                                        continue
                                    
                                    # Extract transcription data if available
                                    try:
                                        transcription_data = extract_transcription_from_openai_message(original_data)
                                        if transcription_data:
                                            last_user_activity_ts = loop.time()
                                            last_prompt_stage = 0
                                            # Log transcription asynchronously with batching
                                            if session_id:
                                                try:
                                                    # Add to batch queue for background processing
                                                    _transcription_batch.append({
                                                        "session_id": session_id,
                                                        "speaker": transcription_data.get("speaker", "unknown"),
                                                        "utterance_text": transcription_data.get("utterance_text", ""),
                                                        "timestamp": transcription_data.get("timestamp")
                                                    })
                                                    # Schedule flush if not already running
                                                    if _transcription_flush_task is None:
                                                        _transcription_flush_task = asyncio.create_task(_flush_transcriptions())
                                                    # Log transcript locally (fast, non-blocking)
                                                    speaker = transcription_data.get("speaker")
                                                    text = transcription_data.get("utterance_text", "")[:50]
                                                    logger.info(f"[TRANSCRIPT] {speaker}: {text}...")
                                                except Exception as e:
                                                    logger.error(f"[TRANSCRIPT ERROR] {e}")

                                            # Detect language from BOTH agent and customer responses
                                            if transcription_data.get("speaker") in ("agent", "customer"):
                                                text = transcription_data.get("utterance_text", "").strip().lower()
                                                # Check for explicit language switch phrases first
                                                if any(phrase in text for phrase in ["speak english", "in english", "switch to english", "we can speak english"]):
                                                    if detected_conversation_language != "en":
                                                        detected_conversation_language = "en"
                                                        logger.info(f"[LANG] Explicit switch to English detected from {transcription_data.get('speaker')}")
                                                # Also detect from German phrases
                                                elif any(phrase in text for phrase in ["deutsch", "auf deutsch", "auf deutsch sprechen"]):
                                                    if detected_conversation_language != "de":
                                                        detected_conversation_language = "de"
                                                        logger.info(f"[LANG] Explicit switch to German detected from {transcription_data.get('speaker')}")
                                                # Fall back to langdetect for longer utterances (increased from 15 to 30)
                                                elif detect_lang and len(text) > 30:
                                                    try:
                                                        _lang = await asyncio.to_thread(detect_lang, text)
                                                        if _lang and _lang in ("en", "de") and _lang != detected_conversation_language:
                                                            detected_conversation_language = _lang
                                                            logger.debug(f"[LANG] Detected from {transcription_data.get('speaker')}: {_lang}")
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
                                        await ws.send_text(_json_dumps(data))
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

                        # Cancel dynamically-created tasks (function calls, phonebook updates, turn detection)
                        for task in _dynamic_tasks:
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass

                        # Ensure the ACS call is hung up so CallDisconnected event fires
                        logger.info(f"[CLEANUP] Forward loop ended — ensuring ACS call is hung up. session={session_id}, call_end_requested={call_end_requested.is_set()}")
                        _hung = await _do_acs_hangup("forward_loop_cleanup")
                        if not _hung:
                            logger.warning("[CLEANUP] Cleanup hangup returned False — call may already be disconnected or session missing")

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
