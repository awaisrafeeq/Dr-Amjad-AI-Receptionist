import aiohttp
import asyncio
import json
from json import JSONDecodeError
from typing import Any, Optional, List, Dict, Tuple
from fastapi import WebSocket
from utils.helpers import transform_acs_to_openai_format, transform_openai_to_acs_format, load_prompt_from_markdown, extract_transcription_from_openai_message
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
from datetime import date, datetime, timedelta

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
                        nonlocal last_kb_context
                        if not user_text:
                            return

                        user_lang = _detect_language(user_text)

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
                        prompt_1_after = 60  # Increased from 30s
                        prompt_2_after = 120 # Increased from 60s
                        hangup_after = 300   # Increased from 120s

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
                        try:
                            async for msg in ws.iter_text():
                                try:
                                    data = json.loads(msg)
                                except JSONDecodeError:
                                    logger.warning("Non-JSON frame received from ACS websocket (ignored)")
                                    continue

                                if isinstance(data, dict):
                                    logger.debug("ACS -> received kind: %s", data.get("kind"))

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
                            
                    async def from_server_to_client():
                        nonlocal last_user_activity_ts, last_prompt_stage
                        nonlocal suppress_agent_audio, cancel_sent_for_current_turn, response_active
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
                                        suppress_agent_audio = False
                                        cancel_sent_for_current_turn = False
                                        last_user_activity_ts = loop.time()
                                        logger.debug("Response created - response_active=True, flags reset")
                                    elif event_type == "response.done":
                                        response_active = False
                                        suppress_agent_audio = False
                                        cancel_sent_for_current_turn = False
                                        logger.debug("Response ended - response_active=False, flags reset")
                                    
                                    elif event_type == "session.updated":
                                        logger.info("[SESSION] OpenAI confirmed session.updated — transcription active")
                                        session_confirmed.set()

                                    elif event_type == "response.function_call_arguments.done":
                                        logger.info(f"[FUNCTION CALL] Received: {original_data}")
                                        call_id = original_data.get("call_id")
                                        func_name = original_data.get("name")
                                        try:
                                            args = json.loads(original_data.get("arguments", "{}"))
                                        except json.JSONDecodeError:
                                            args = {}
                                            
                                        result = ""
                                        
                                        if func_name == "get_available_doctors":
                                            doctors = []
                                            try:
                                                calendars = await epaad_client.get_calendars()
                                                for cal in calendars or []:
                                                    prof = cal.get("professional") or {}
                                                    first = (prof.get("firstName") or "").strip()
                                                    last = (prof.get("lastName") or "").strip()
                                                    name = f"Dr. {first} {last}".strip() or "Unknown Doctor"
                                                    try:
                                                        doctors.append({"calendar_id": int(cal.get("id")), "doctor_name": name})
                                                    except Exception:
                                                        pass
                                                
                                                extra = (os.getenv("EPAAD_EXTRA_CALENDAR_IDS") or "").strip()
                                                if extra:
                                                    for part in extra.split(","):
                                                        if part.strip():
                                                            try:
                                                                cal_id = int(part.strip())
                                                                if not any(d["calendar_id"] == cal_id for d in doctors):
                                                                    doctors.append({"calendar_id": cal_id, "doctor_name": f"Unknown Doctor (Calendar {cal_id})"})
                                                            except Exception:
                                                                pass
                                                                
                                                result = json.dumps(doctors)
                                            except Exception as e:
                                                logger.error(f"Error in get_available_doctors: {e}")
                                                result = json.dumps({"error": str(e)})
                                                
                                        elif func_name == "get_available_slots":
                                            try:
                                                cal_id = args.get("calendar_id")
                                                target_date_str = args.get("date")
                                                tod = args.get("time_of_day", "any")
                                                target_day = date.fromisoformat(target_date_str)
                                                start_dt = datetime.combine(target_day, datetime.min.time())
                                                end_dt = datetime.combine(target_day, datetime.max.time()).replace(microsecond=0)
                                                
                                                events = await epaad_client.get_events(
                                                    calendar_id=cal_id,
                                                    from_dt=start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                                                    until_dt=end_dt.strftime("%Y-%m-%dT%H:%M:%S")
                                                )
                                                
                                                slots = compute_free_slots(
                                                    events=events or [],
                                                    target_date=target_day,
                                                    opening_hours=default_opening_hours(),
                                                    slot_minutes=15,
                                                    time_of_day=tod,
                                                    now_dt=datetime.now(),
                                                )
                                                
                                                slot_iso = [s.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S") for s in slots][:10]
                                                result = json.dumps({"available_slots": slot_iso})
                                            except Exception as e:
                                                logger.error(f"Error in get_available_slots: {e}")
                                                result = json.dumps({"error": str(e)})
                                                
                                        elif func_name == "book_appointment":
                                            try:
                                                dob = args.get("patient_dob", "")
                                                if len(dob) == 10:
                                                    dob += "T00:00:00"

                                                phone = args.get("patient_phone")

                                                event = {
                                                    "startDateTime": args.get("slot_iso"),
                                                    "comment": args.get("comment") or "AI Booking",
                                                    "patient": {
                                                        "firstName": args.get("patient_first_name"),
                                                        "lastName": args.get("patient_last_name"),
                                                        "birthDate": dob,
                                                        "gender": args.get("patient_gender", "other"),
                                                        "address": {
                                                            "street": args.get("street", ""),
                                                            "streetNumber": args.get("street_number", ""),
                                                            "zipCode": args.get("zip_code", ""),
                                                            "city": args.get("city", ""),
                                                            "state": "",
                                                            "country": "CH"  # Default to Switzerland as per test scripts
                                                        },
                                                        "privatePhoneNumber": phone,
                                                        "mobilePhoneNumber": phone,
                                                        "email": args.get("patient_email", "")
                                                    }
                                                }
                                                res = await epaad_client.create_event(args.get("calendar_id"), event)
                                                onedoc_key = res.get("onedoc_key") or res.get("onedocKey") or res.get("key") if isinstance(res, dict) else None
                                                if onedoc_key:
                                                    logger.info(f"[BOOKING SUCCESS] Slot: {args.get('slot_iso')}, Reference: {onedoc_key}")
                                                    result = json.dumps({"status": "success", "booking_reference": onedoc_key})
                                                else:
                                                    result = json.dumps({"status": "success", "message": "Appointment booked but no reference generated."})
                                            except Exception as e:
                                                logger.error(f"Error in book_appointment: {e}")
                                                result = json.dumps({"status": "error", "message": str(e)})

                                        elif func_name == "terminate_call":
                                            logger.info("AI requested to terminate call.")
                                            call_end_requested.set()
                                            result = json.dumps({"status": "success", "message": "Terminating call now."})

                                        # Send function output back to the server
                                        await target_ws.send_str(json.dumps({
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": call_id,
                                                "output": result
                                            }
                                        }))
                                        
                                        # Trigger agent to respond (if not terminating)
                                        if not call_end_requested.is_set():
                                            await target_ws.send_str(json.dumps({
                                                "type": "response.create"
                                            }))

                                    if event_type == "input_audio_buffer.speech_started":
                                        last_user_activity_ts = loop.time()
                                        last_prompt_stage = 0
                                        suppress_agent_audio = True

                                        if not cancel_sent_for_current_turn and response_active:
                                            cancel_sent_for_current_turn = True
                                            try:
                                                await target_ws.send_str(json.dumps({"type": "response.cancel"}))
                                                logger.info("Barge-in: sent response.cancel")
                                                response_active = False  # Response is now cancelled
                                            except Exception:
                                                logger.exception("Barge-in: failed to send response.cancel")

                                    if event_type in (
                                        "input_audio_buffer.speech_stopped",
                                        "input_audio_buffer.committed",
                                    ):
                                        suppress_agent_audio = False
                                        cancel_sent_for_current_turn = False

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
                                                await session_manager.log_transcription(
                                                    session_id=session_id,
                                                    speaker=transcription_data.get("speaker", "unknown"),
                                                    utterance_text=transcription_data.get("utterance_text", ""),
                                                    timestamp=transcription_data.get("timestamp")
                                                )

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
