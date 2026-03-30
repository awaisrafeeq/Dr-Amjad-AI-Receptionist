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
                
                # --- PHONEBOOK LOOKUP ---
                phonebook_match = None
                matched_caller = False
                try:
                    from utils.phonebook_lookup import get_phonebook_lookup
                    lookup = get_phonebook_lookup()
                    if lookup:
                        phonebook_match = lookup.lookup_by_phone(caller_id)
                        if phonebook_match:
                            matched_caller = True
                            logger.info(f"[PHONEBOOK] Match: {phonebook_match.first_name} {phonebook_match.last_name}")
                        else:
                            logger.debug(f"[PHONEBOOK] No match for {caller_id}")
                    else:
                        logger.debug(f"[PHONEBOOK] Lookup disabled")
                except Exception as pb_error:
                    logger.warning(f"[PHONEBOOK] Lookup error: {pb_error}")
                # --- END PHONEBOOK LOOKUP ---
                
                # create a new session ID for this call
                try:
                    event_payload = event.data
                    if isinstance(event_payload, str):
                        event_payload = json.loads(event_payload)
                    
                    # Add phonebook match info to session data
                    session_data = {
                        **event_payload,
                        "matched_caller": matched_caller,
                        "phonebook_info": phonebook_match.to_dict() if phonebook_match else None
                    }
                    
                    session_id = await session_manager.create_session(session_data, event.event_type)
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
                
                await session_manager.log_event(
                    session_id=current_session_id,
                    event_data=generic_event
                )
            
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
                
                # End the session
                await session_manager.end_session(
                    session_id=contextId,
                    event_data=generic_event
                )
                
                # Send transcript email
                try:
                    await session_manager.send_transcript_email(
                        session_id=contextId,
                        caller_phone=caller_id if 'caller_id' in locals() else None
                    )
                except Exception as email_error:
                    logger.error(f"Email send error: {email_error}")


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