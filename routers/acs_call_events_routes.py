import logging
from fastapi import Request, WebSocket, APIRouter
from azure.eventgrid import EventGridEvent, SystemEventNames
from fastapi.responses import JSONResponse, PlainTextResponse, Response
import uuid
from urllib.parse import urlencode
from azure.core.messaging import CloudEvent
from utils.acs import acs_caller
from utils.rtmt import rtmt
from utils.session_manager import session_manager
from utils.phonebook_lookup import normalize_phone_variants
import json
from datetime import datetime, timezone
import asyncio
from fastapi import WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)

# --- Setup ACS ---
caller = acs_caller


# --- Setup OpenAI Realtime bridge ---
rtmt = rtmt

# --- Dedup: track recently answered incoming calls to prevent duplicate sessions ---
import time
_recent_incoming_calls: dict[str, float] = {}  # correlation_id -> timestamp
_DEDUP_WINDOW_SECONDS = 5.0

# session_id = None  # REMOVED GLOBAL SESSION ID

@router.post("/acs/incoming", tags=['ACS Call Events'])
async def inbound_call(request: Request):
    """Handles inbound ACS call events."""
    # Handle incoming call events
    try:
        event_data = await request.json()
        logger.debug(f"ACS event received")

        if isinstance(event_data, dict):
            event_data = [event_data]
        elif not isinstance(event_data, list):
            event_data = [event_data]
        
        # EventGrid sends events in an array
        for event_dict in event_data:
            logger.debug(f"Processing: {event_dict.get('eventType', 'unknown')}")
            event = EventGridEvent.from_dict(event_dict)
            
            if event.event_type == SystemEventNames.EventGridSubscriptionValidationEventName:
                logger.debug("Validating subscription")
                validation_code = event.data["validationCode"]
                return JSONResponse(content={"validationResponse": validation_code}, status_code=200)
            
            elif event.event_type == "Microsoft.Communication.IncomingCall":
                incoming_call_context = event.data['incomingCallContext']
                if event.data["from"]["kind"] == "phoneNumber":
                    caller_id = event.data["from"]["phoneNumber"]["value"]
                else:
                    caller_id = event.data["from"]["rawId"]
                logger.info(f"[CALL] Incoming from: {caller_id}")

                # Dedup: skip if we already answered this exact incoming call event recently
                _correlation_id = event.data.get("correlationId", "")
                _server_call_id = event.data.get("serverCallId", "")
                _dedup_key = _correlation_id or _server_call_id or ""
                if _dedup_key:
                    _now = time.time()
                    # Clean old entries
                    _recent_incoming_calls.update({k: v for k, v in _recent_incoming_calls.items() if _now - v < _DEDUP_WINDOW_SECONDS})
                    if _dedup_key in _recent_incoming_calls:
                        logger.warning(f"[CALL] Duplicate incoming call detected (dedup_key={_dedup_key[:16]}...) — skipping")
                        return Response(status_code=200)
                    _recent_incoming_calls[_dedup_key] = _now

                # create a new session ID for this call
                try:
                    event_payload = event.data
                    if isinstance(event_payload, str):
                        event_payload = json.loads(event_payload)
                    
                    # Add phonebook match info to session data
                    # session_data = {
                    #     **event_payload,
                    #     "matched_caller": matched_caller,
                    #     "phonebook_info": phonebook_match.to_dict() if phonebook_match else None
                    # }
                    
                    session_id = await session_manager.create_session(event_payload, event.event_type)
                    # Use session_id as the guid for the callback URL
                    guid = session_id
                except Exception as session_error:
                    logger.error(f"Session creation error: {session_error}")
                    guid = uuid.uuid4()
                    session_id = str(guid)
                
                query_parameters = urlencode({"callerId": caller_id})
                callback_uri = f"{caller.acs_callback_path}/{guid}?{query_parameters}"
                logger.debug(f"Callback: {callback_uri[:60]}...")
                
                await caller.answer_inbound_call(incoming_call_context, callback_uri, session_id)
                
                return Response(status_code=200)

        return Response(status_code=200)

    except Exception as e:
        logger.exception("Error handling inbound call")
        try:
            event = {
                "event_type": "error",
                "details": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            if 'session_id' in locals():
                await session_manager.log_event(
                    session_id=session_id,
                    event_data=event
                )
        except Exception as log_error:
            logger.error(f"Error logging inbound call failure (ignored): {log_error}")

        return Response(status_code=200)

    
@router.post("/acs/callbacks/{contextId}", tags=['ACS Call Events'])
async def handle_callback(contextId: str, request: Request):
    """
    Handle ACS call lifecycle callbacks (CallConnected, RecognizeCompleted, PlayCompleted, etc.).
    """
    try:
        callbacks = await request.json()
        current_session_id = contextId # session_id was used as contextId/guid
        caller_id = request.query_params.get("callerId", "").strip()
        
        if "+" not in caller_id and caller_id:
            caller_id = "+" + caller_id

        for event_dict in callbacks:
            event = CloudEvent.from_dict(event_dict)
            logger.debug(f"Event: {event.type}")
            
            call_connection_id = event.data['callConnectionId']
            
            generic_event = {
                "event_type": event.type,            # e.g., 'CallConnected'
                "callConnectionId": event.data.get("callConnectionId"),
                "serverCallId": event.data.get("serverCallId"),
                "correlationId": event.data.get("correlationId"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": 'active',
                "details": {}                        # store event-specific fields here
            }

            if event.type == "Microsoft.Communication.CallConnected":
                generic_event["details"]["operationContext"] = event.data.get("operationContext")
                logger.info(
                    f"[CALL CONNECTED] session={current_session_id[:8]}, "
                    f"call_connection_id={call_connection_id}"
                )

                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )

                # Verify call_connection_id was stored on session
                _verify_sess = session_manager.get_session(current_session_id)
                if _verify_sess:
                    logger.info(f"[CALL CONNECTED] Verified session.call_connection_id={_verify_sess.call_connection_id}")
                else:
                    logger.error(f"[CALL CONNECTED] Session {current_session_id[:8]} NOT FOUND after log_event!")
            
            elif event.type == "Microsoft.Communication.ParticipantsUpdated":
                generic_event["details"]["participants"] = event.data.get("participants")
                generic_event["details"]["sequenceNumber"] = event.data.get("sequenceNumber")
                
                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )
                
            elif event.type == "Microsoft.Communication.RecognizeCompleted":
                pass

            elif event.type == "Microsoft.Communication.PlayCompleted":
                pass
            
            elif event.type == "Microsoft.Communication.SpeechSynthesisCompleted":
                pass
            
            elif event.type == "Microsoft.Communication.SpeechSynthesisStarted":
                pass
            
            elif event.type == "Microsoft.Communication.RecognizeStarted":
                pass
            
                
            elif event.type == "Microsoft.Communication.MediaStreamingStarted":
                generic_event["details"]["mediaStreamingUpdate"] = event.data.get("mediaStreamingUpdate")
                
                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )
                
            elif event.type == "Microsoft.Communication.MediaStreamingStopped":
                generic_event["details"]["mediaStreamingUpdate"] = event.data.get("mediaStreamingUpdate")
                
                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )
                
            elif event.type == "Microsoft.Communication.MediaStreamingFailed":
                generic_event["details"]["mediaStreamingUpdate"] = event.data.get("mediaStreamingUpdate")
                generic_event["status"] = 'error'
                
                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )
                
            elif event.type == "Microsoft.Communication.CallDisconnected":
                generic_event["details"]["operationContext"] = event.data.get("operationContext")
                generic_event["details"]["resultInformation"] = event.data.get("resultInformation")
                generic_event["status"] = 'disconnected'
                _result_info = event.data.get("resultInformation", {})
                logger.info(
                    f"[CALL DISCONNECTED] session={contextId[:8]}, "
                    f"call_connection_id={call_connection_id}, "
                    f"reason_code={_result_info.get('subCode')}, "
                    f"message={_result_info.get('message', 'N/A')}"
                )

                # Guard: skip if WebSocket handler already started cleanup for this session
                if contextId in session_manager._cleanup_in_progress:
                    logger.info(f"[CALLBACK] Session {contextId[:8]} cleanup already in progress — skipping")
                else:
                    session_manager._cleanup_in_progress.add(contextId)

                    # Send transcript email
                    try:
                        await session_manager.send_transcript_email(
                            session_id=contextId,
                            caller_phone=caller_id if caller_id else None
                        )
                    except Exception as email_error:
                        logger.error(f"Email send error: {email_error}")

                    # THEN end the session
                    await session_manager.end_session(
                        session_id=contextId,
                        event_data=generic_event
                    )


    except Exception as ex:
        logger.exception(f"Event handling error: {ex}")
        event = {
            "event_type": "error",
            "details": str(ex),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        await session_manager.log_event(
            session_id=current_session_id,
            event_data=event
        )
    return Response(status_code=200)   

@router.websocket("/realtime-acs")
async def websocket_handler_acs(websocket: WebSocket):
    """Handles ACS <-> OpenAI Realtime audio streaming."""
    await websocket.accept()
    
    # Get session_id from query parameters
    current_session_id = websocket.query_params.get("session_id")
    if not current_session_id:
        logger.warning("WebSocket connected without session_id!")
    
    try:
        # Add a timeout to prevent hanging connections
        await asyncio.wait_for(
            rtmt.forward_messages(websocket, is_acs_audio_stream=True, session_id=current_session_id),
            timeout=None  # You can set a reasonable timeout like 3600 for 1 hour
        )
    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected")
    except asyncio.CancelledError:
        logger.debug("WebSocket cancelled")
        raise
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass
        # Safety-net cleanup: if CallDisconnected hasn't already handled this session,
        # send transcript email and end the session so it doesn't linger forever.
        if current_session_id and current_session_id in session_manager.active_sessions:
            if current_session_id not in session_manager._cleanup_in_progress:
                session_manager._cleanup_in_progress.add(current_session_id)
                logger.info(f"[WS CLEANUP] Session {current_session_id[:8]} still active — cleaning up")
                try:
                    _sess = session_manager.active_sessions.get(current_session_id)
                    _caller_phone = (
                        _sess.participants[0]["phone_number"]
                        if _sess and _sess.participants else None
                    )
                    await session_manager.send_transcript_email(
                        session_id=current_session_id,
                        caller_phone=_caller_phone,
                    )
                except BaseException as _email_err:
                    # BaseException catches CancelledError too (server shutdown)
                    logger.error(f"[WS CLEANUP] Email error: {_email_err}")
                try:
                    await session_manager.end_session(
                        session_id=current_session_id,
                        event_data={
                            "event_type": "WebSocketDisconnected",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "status": "disconnected",
                        },
                    )
                except BaseException as _end_err:
                    logger.error(f"[WS CLEANUP] End session error: {_end_err}")
    
    # Try to get session context from query parameters or headers
    # session_id = websocket.query_params.get("session_id")
    # call_connection_id = websocket.query_params.get("call_connection_id")
    
    # # Log WebSocket connection
    # if session_id or call_connection_id:
    #     await session_manager.log_event(
    #         session_id=session_id,
    #         call_connection_id=call_connection_id,
    #         event_type="websocket_connected",
    #         event_data={
    #             "endpoint": "/realtime-acs",
    #             "client_info": websocket.client.host if websocket.client else "unknown"
    #         }
    #     )
    
    # try:
    #     await rtmt.forward_messages(websocket, is_acs_audio_stream=True)
    # except Exception as e:
    #     # Log WebSocket errors
    #     if session_id or call_connection_id:
    #         await session_manager.log_event(
    #             session_id=session_id,
    #             call_connection_id=call_connection_id,
    #             event_type="websocket_error",
    #             event_data={
    #                 "error": str(e),
    #                 "endpoint": "/realtime-acs"
    #             }
    #         )
    #     raise
    # finally:
    #     # Log WebSocket disconnection
    #     if session_id or call_connection_id:
    #         await session_manager.log_event(
    #             session_id=session_id,
    #             call_connection_id=call_connection_id,
    #             event_type="websocket_disconnected",
    #             event_data={
    #                 "endpoint": "/realtime-acs"
    #             }
    #         )