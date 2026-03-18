import json
from datetime import datetime, timezone
from openai.types.beta.realtime import (InputAudioBufferAppendEvent, SessionUpdateEvent)
from openai.types.beta.realtime.session_update_event import Session, SessionTurnDetection
from typing import Any, Literal, Optional
import logging

logger = logging.getLogger(__name__)

def transform_acs_to_openai_format(msg_data: Any, model: Optional[str], system_message: Optional[str], temperature: Optional[float], max_tokens: Optional[int], disable_audio: Optional[bool], voice: str) -> InputAudioBufferAppendEvent | SessionUpdateEvent | Any | None:
    """
    Transforms websocket message data from Azure Communication Services (ACS) to the OpenAI Realtime API format.
    Args:
        msg_data_json (str): The JSON string containing the ACS message data.
    Returns:
        Optional[str]: The transformed message in the OpenAI Realtime API format
    This is needed to plug the Azure Communication Services audio stream into the OpenAI Realtime API.
    Both APIs have different message formats, so this function acts as a bridge between them.
    This method decides, if the given message is relevant for the OpenAI Realtime API, and if so, it is transformed to the OpenAI Realtime API format.
    """
    
    """Convert ACS audio events -> OpenAI Realtime format."""
    
    oai_message: Any = None
    
    if msg_data["kind"] == "AudioMetadata":
        
        oai_message = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": 'server_vad',
                    "threshold": 0.6,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 1000 # Increased from 500ms to give more time to talk
                },
                "input_audio_transcription": {
                  "model": "whisper-1", 
                },
                "input_audio_noise_reduction": {
                    "type": "near_field"  
                },
                "tools": [
                    {
                        "type": "function",
                        "name": "get_available_doctors",
                        "description": "Get a list of available doctors and their calendar IDs. Use this before checking availability.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    },
                    {
                        "type": "function",
                        "name": "get_available_slots",
                        "description": "Get a list of available appointment slots for a specific doctor on a specific date.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "calendar_id": {"type": "integer", "description": "The calendar ID of the doctor."},
                                "date": {"type": "string", "description": "The date in YYYY-MM-DD format."},
                                "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"], "description": "The preferred time of day."}
                            },
                            "required": ["calendar_id", "date"]
                        }
                    },
                    {
                        "type": "function",
                        "name": "book_appointment",
                        "description": "Book an appointment for a patient. Use phonebook info for name/DOB if available. DO NOT ask for gender.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "calendar_id": {"type": "integer", "description": "The calendar ID of the doctor."},
                                "slot_iso": {"type": "string", "description": "The exact ISO datetime slot chosen by the patient."},
                                "patient_first_name": {"type": "string"},
                                "patient_last_name": {"type": "string"},
                                "patient_dob": {"type": "string", "description": "Patient's Date of Birth in YYYY-MM-DD format."},
                                "patient_phone": {"type": "string", "description": "Patient's phone number with country code (e.g. +41...)"},
                                "patient_gender": {"type": "string", "enum": ["male", "female", "other"], "description": "Patient's gender (male, female, other). Default to 'other' or infer from voice. DO NOT ASK."},
                                "patient_email": {"type": "string", "description": "Patient's email address (optional).", "default": ""},
                                "street": {"type": "string", "description": "Patient's street name."},
                                "street_number": {"type": "string", "description": "Patient's house/street number."},
                                "zip_code": {"type": "string", "description": "Patient's zip code / postal code."},
                                "city": {"type": "string", "description": "Patient's city."},
                                "comment": {"type": "string", "description": "Reason for the appointment."}
                            },
                            "required": [
                                "calendar_id", "slot_iso", "patient_first_name", "patient_last_name", 
                                "patient_dob", "patient_phone", "street", "street_number", "zip_code", "city", "comment"
                            ]
                        }
                    },
                    {
                        "type": "function",
                        "name": "terminate_call",
                        "description": "End the call and hang up the phone after saying goodbye.",
                        "parameters": {
                            "type": "object",
                            "properties": {}
                        }
                    }
                ],
                "tool_choice": "auto"
            }
        }
        if system_message is not None:
            oai_message["session"]["instructions"] = system_message
        if temperature is not None:
            oai_message["session"]["temperature"] = temperature
        if max_tokens is not None:
            oai_message["session"]["max_response_output_tokens"] = max_tokens
        if disable_audio is not None:
            oai_message["session"]["disable_audio"] = disable_audio


    elif msg_data["kind"] == "AudioData":
        oai_message = {
            "type": "input_audio_buffer.append",
            "audio": msg_data["audioData"]["data"]
        }

    return oai_message
        

def transform_openai_to_acs_format(msg_data: Any) -> Optional[Any]:
    """
    Transforms websocket message data from the OpenAI Realtime API format into the Azure Communication Services (ACS) format.
    Args:
        msg_data_json (str): The JSON string containing the message data from the OpenAI Realtime API.
    Returns:
        Optional[str]: A JSON string containing the transformed message in ACS format, or None if the message type is not handled.
    This is needed to plug the OpenAI Realtime API audio stream into Azure Communication Services.
    Both APIs have different message formats, so this function acts as a bridge between them.
    This method decides, if the given message is relevant for the ACS, and if so, it is transformed to the ACS format.
    """
    
    acs_message = None
    
    # Message from the OpenAI Realtime API with audio data.
    # Transform the message to the Azure Communication Services format.
    if msg_data["type"] == "response.audio.delta":
        acs_message = {
            "kind": "AudioData",
            "audioData": {
                "data": msg_data["delta"]
            }
        }
    # Message from the OpenAI Realtime API detecting, that the user starts speaking and interrupted the AI.
    # In this case, we don't want to send the unplayed audio buffer to the client anymore and clear the buffer audio.
    # Buffered audio is audio data that has been sent to Azure Communication Services, but not yet played by the client.
    if msg_data["type"] == "input_audio_buffer.speech_started":
        logger.info("VAD detected: User started speaking")
        acs_message = None

    return acs_message

def extract_transcription_from_openai_message(msg_data: Any) -> Optional[dict]:
    """
    Extract transcription data from OpenAI Realtime API messages.
    
    Args:
        msg_data: Message data from OpenAI Realtime API
        
    Returns:
        Dictionary with transcription data or None if no transcription found
    """
    transcription_data = None
    # Use high-precision timestamp to avoid conflicts
    current_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    
    logger.info(f"[TRANSCRIPTION EXTRACT] Processing message type: {msg_data.get('type')}")
    
    if msg_data.get("type") == "input_audio_buffer.speech_started":
        logger.info("[TRANSCRIPTION EXTRACT] User started speaking")
    elif msg_data.get("type") == "input_audio_buffer.committed":
        logger.info("[TRANSCRIPTION EXTRACT] User stopped speaking")
    elif msg_data.get("type") == "conversation.item.input_audio_transcription.completed":
        transcript = msg_data.get("transcript", "")
        logger.info(f"[TRANSCRIPTION EXTRACT] Transcription completed: '{transcript}'")
        if transcript:
            transcription_data = {
                "speaker": "customer",
                "utterance_text": transcript,
                "timestamp": current_time,
            }
            logger.info(f"[TRANSCRIPTION EXTRACT] User said: {transcript}")
    elif msg_data.get("type") == "response.audio_transcript.done":
        transcript = msg_data.get("transcript", "")
        logger.info(f"[TRANSCRIPTION EXTRACT] Agent transcript: '{transcript}'")
        if transcript:
            transcription_data = {
                "speaker": "agent",
                "utterance_text": transcript,
                "timestamp": current_time
            }
            logger.info(f"[TRANSCRIPTION EXTRACT] Agent said: {transcript}")
    
    elif msg_data.get("type") == "response.content_part.added":
        logger.debug("Agent is speaking...")
    elif msg_data.get("type") == "response.done":
        logger.debug("Agent stopped speaking")
    
    return transcription_data

def load_prompt_from_markdown(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        prompt = file.read()
    return prompt

