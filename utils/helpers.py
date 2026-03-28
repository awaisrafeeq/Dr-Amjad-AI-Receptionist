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
                    "prefix_padding_ms": 200,
                    "silence_duration_ms": 600
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
                        "description": "Book an appointment for a patient. Use phonebook info for name/DOB if available. DO NOT ask for gender. Determine appointment duration based on visit reason complexity (one issue=15min, two issues=20min, three+ issues/new patient=30min).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "calendar_id": {"type": "integer", "description": "The calendar ID of the doctor."},
                                "slot_iso": {"type": "string", "description": "The exact ISO datetime slot chosen by the patient."},
                                "patient_first_name": {"type": "string"},
                                "patient_last_name": {"type": "string"},
                                "patient_dob": {"type": "string", "description": "Patient's Date of Birth in YYYY-MM-DD format."},
                                "patient_phone": {"type": "string", "description": "Patient's phone number with country code (e.g. +41...)"},
                                "patient_gender": {"type": "string", "enum": ["male", "female", "other"], "description": "Patient's gender. Detect automatically from the caller's voice (male vs female voice characteristics). Do NOT ask the patient. Use 'male' for clearly male voices, 'female' for clearly female voices, 'other' only when voice is completely ambiguous."},
                                "patient_email": {"type": "string", "description": "Patient's email address for appointment confirmation. Ask the patient for it if not already known from the phonebook."},
                                "street": {"type": "string", "description": "Patient's street name."},
                                "street_number": {"type": "string", "description": "Patient's house/street number."},
                                "zip_code": {"type": "string", "description": "Patient's zip code / postal code."},
                                "city": {"type": "string", "description": "Patient's city."},
                                "visit_reason": {"type": "string", "description": "The reason for the visit as described by the patient. Used to determine appointment duration."},
                                "comment": {"type": "string", "description": "Additional notes or comments for the appointment."}
                            },
                            "required": [
                                "calendar_id", "slot_iso", "patient_first_name", "patient_last_name", 
                                "patient_dob", "patient_phone", "street", "street_number", "zip_code", "city", "visit_reason"
                            ]
                        }
                    },
                    {
                        "type": "function",
                        "name": "get_available_doctors",
                        "description": "Get the list of available doctors at MedCenter Volta. Returns all doctors with their names and German titles.",
                        "parameters": {
                            "type": "object",
                            "properties": {}
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


# --- Diagnosis Word Filter for TTS Output ---

# List of diagnosis-related keywords and patterns to filter
DIAGNOSIS_KEYWORDS = [
    # Common medical conditions (German)
    "diabetes", "diabetis", "bluthochdruck", "hypertonie", "bluthochdruck",
    "asthma", "copd", "chronisch", "herzinfarkt", "schlaganfall", "krebs",
    "tumor", "carcinom", "carcinoma", "metastas", "depression", "angst",
    "psychose", "bipolar", "schizophrenie", "demenz", "alzheimer",
    "parkinson", "epilepsie", "multipler sklerose", "ms", "rheuma",
    "arthritis", "arthrose", "osteoporose", "gicht", "migräne",
    "clusterkopfschmerz", "kopfschmerz", "schwindel", "tinitus",
    "niereninsuffizienz", "leberzirrhose", "hepatitis", "zirrhose",
    "bluthochdruck", "herzinsuffizienz", "koronare", "koronarsyndrom",
    "ischämie", "thrombose", "embolie", "aneurysma", "varizen",
    "gallensteine", "nierensteine", "prostata", "schilddrüsen",
    "überfunktion", "unterfunktion", "hiv", "aids", "hepatitis",
    
    # Common medical conditions (English)
    "diabetes", "hypertension", "asthma", "copd", "stroke", "heart attack",
    "cancer", "tumor", "carcinoma", "metastasis", "depression", "anxiety",
    "psychosis", "bipolar", "schizophrenia", "dementia", "alzheimer",
    "parkinson", "epilepsy", "multiple sclerosis", "ms", "rheumatoid",
    "arthritis", "osteoporosis", "gout", "migraine", "headache",
    "vertigo", "tinnitus", "kidney failure", "cirrhosis", "hepatitis",
    "coronary", "ischemia", "thrombosis", "embolism", "aneurysm",
    "varicose", "gallstones", "kidney stones", "prostate", "thyroid",
    "hyperthyroidism", "hypothyroidism", "hiv", "aids",
]

# ICD code patterns (e.g., A00-Z99, E11, I10, etc.)
ICD_CODE_PATTERNS = [
    r'\b[A-Z]\d{2}\b',           # A00, B99, etc.
    r'\b[A-Z]\d{2}\.\d{1,2}\b',  # A00.0, E11.9, etc.
    r'\bICD[\s-]?\d{0,2}\b',     # ICD, ICD-10, ICD10
    r'\bICD[\s-]?\d{0,2}[-\s]?[A-Z]\d{2,4}\b',  # ICD-10-E11, ICD10 E11
]

# Sensitive data patterns
SENSITIVE_PATTERNS = [
    r'\bpatienten[-\s]?nr\.?\s*:?\s*\d+',  # Patienten-Nr: 12345
    r'\bversicherten[-\s]?nr\.?\s*:?\s*\d+',  # Versicherten-Nr
    r'\bkv[-\s]?nr\.?\s*:?\s*\d+',  # KV-Nr
    r'\bmatrikel[-\s]?nr\.?\s*:?\s*\d+',  # Matrikel-Nr
]

# Compile regex patterns
import re
ICD_REGEX = [re.compile(pattern, re.IGNORECASE) for pattern in ICD_CODE_PATTERNS]
SENSITIVE_REGEX = [re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_PATTERNS]


def filter_diagnosis_words(text: str) -> tuple[str, bool]:
    """
    Filter diagnosis words, ICD codes, and sensitive medical information from text.
    
    Args:
        text: Input text to filter
        
    Returns:
        Tuple of (filtered_text, was_filtered)
    """
    if not text:
        return text, False
    
    original_text = text
    was_filtered = False
    text_lower = text.lower()
    
    # Check for diagnosis keywords
    for keyword in DIAGNOSIS_KEYWORDS:
        if keyword in text_lower:
            was_filtered = True
            break
    
    # Check for ICD codes
    if not was_filtered:
        for pattern in ICD_REGEX:
            if pattern.search(text):
                was_filtered = True
                break
    
    # Check for sensitive patterns
    if not was_filtered:
        for pattern in SENSITIVE_REGEX:
            if pattern.search(text):
                was_filtered = True
                break
    
    # If filtered, return safe fallback message
    if was_filtered:
        # Log the filtering (but don't expose the filtered content)
        logger = logging.getLogger(__name__)
        logger.warning(f"[DIAGNOSIS_FILTER] Filtered content detected and blocked from TTS")
        
        # Return a safe German fallback
        return "Diese Information kann ich telefonisch nicht weitergeben.", True
    
    return original_text, False


# Export for use in other modules
__all__ = [
    'transform_acs_to_openai_format',
    'transform_openai_to_acs_format', 
    'extract_transcription_from_openai_message',
    'load_prompt_from_markdown',
    'filter_diagnosis_words',
]
