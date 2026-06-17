import os
from typing import Dict, Any
from dotenv import load_dotenv


load_dotenv()

# --- Doctor Configuration ---
# List of doctors at MedCenter Volta with proper German titles
DOCTORS = [
    {"key": "mallisho", "name": "Dr. Mallisho", "german_title": "Herr Dr. Mallisho", "calendar_id": None},
    {"key": "lumpp", "name": "Dr. Lumpp", "german_title": "Herr Dr. Lumpp", "calendar_id": None},
    {"key": "keser", "name": "Dr. Keser", "german_title": "Frau Dr. Keser", "calendar_id": None},
    {"key": "osterwalder", "name": "Dr. Osterwalder", "german_title": "Herr Dr. Osterwalder", "calendar_id": None},
]

# Doctor email mapping for routing (can be set via environment variable)
# Format: {"mallisho": "mallisho@example.com", "lumpp": "lumpp@example.com", ...}
DOCTOR_EMAILS_JSON = os.getenv("DOCTOR_EMAILS_JSON", "{}")

try:
    import json
    DOCTOR_EMAILS = json.loads(DOCTOR_EMAILS_JSON)
except:
    DOCTOR_EMAILS = {}

# --- Load Environment Variables ---
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
COGNITIVE_SERVICE_ENDPOINT = os.getenv("COGNITIVE_SERVICE_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_API_TYPE = os.getenv("AZURE_OPENAI_API_TYPE")

AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "openai")
DEEPGRAM_LIVE_TRANSCRIPTION_ENABLED = os.getenv("DEEPGRAM_LIVE_TRANSCRIPTION_ENABLED", "false")
DEEPGRAM_LIVE_MODEL = os.getenv("DEEPGRAM_LIVE_MODEL", "flux-general-multi")
DEEPGRAM_SAMPLE_RATE = os.getenv("DEEPGRAM_SAMPLE_RATE", "24000")
DEEPGRAM_ENCODING = os.getenv("DEEPGRAM_ENCODING", "linear16")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE")
DEEPGRAM_LANGUAGE_HINT = os.getenv("DEEPGRAM_LANGUAGE_HINT")

VOICE_AGENT_PROVIDER = os.getenv("VOICE_AGENT_PROVIDER", "openai_realtime")
DEEPGRAM_AGENT_URL = os.getenv("DEEPGRAM_AGENT_URL", "wss://agent.deepgram.com/v1/agent/converse")
DEEPGRAM_AGENT_LISTEN_MODEL = os.getenv("DEEPGRAM_AGENT_LISTEN_MODEL", "flux-general-multi")
DEEPGRAM_AGENT_LISTEN_VERSION = os.getenv("DEEPGRAM_AGENT_LISTEN_VERSION", "v2")
DEEPGRAM_AGENT_LANGUAGE = os.getenv("DEEPGRAM_AGENT_LANGUAGE")
DEEPGRAM_AGENT_LANGUAGE_HINTS = os.getenv("DEEPGRAM_AGENT_LANGUAGE_HINTS", "de,en,fr,it,es")
DEEPGRAM_AGENT_ALLOW_UNSUPPORTED_LANGUAGE_HINTS = os.getenv("DEEPGRAM_AGENT_ALLOW_UNSUPPORTED_LANGUAGE_HINTS", "false")
DEEPGRAM_AGENT_KEYTERMS = os.getenv("DEEPGRAM_AGENT_KEYTERMS", "")
DEEPGRAM_AGENT_ASR_OPTIMIZATION_ENABLED = os.getenv("DEEPGRAM_AGENT_ASR_OPTIMIZATION_ENABLED", "true")
DEEPGRAM_AGENT_LANGUAGE_LOCK_ENABLED = os.getenv("DEEPGRAM_AGENT_LANGUAGE_LOCK_ENABLED", "true")
DEEPGRAM_AGENT_RUNTIME_LISTEN_UPDATES_ENABLED = os.getenv("DEEPGRAM_AGENT_RUNTIME_LISTEN_UPDATES_ENABLED", "false")
DEEPGRAM_AGENT_MAX_KEYTERMS = os.getenv("DEEPGRAM_AGENT_MAX_KEYTERMS", "80")
DEEPGRAM_AGENT_EOT_THRESHOLD = os.getenv("DEEPGRAM_AGENT_EOT_THRESHOLD", "0.8")
DEEPGRAM_AGENT_EAGER_EOT_THRESHOLD = os.getenv("DEEPGRAM_AGENT_EAGER_EOT_THRESHOLD", "0.5")
DEEPGRAM_AGENT_SPEAK_MODEL = os.getenv("DEEPGRAM_AGENT_SPEAK_MODEL", "aura-2-elara-de")
DEEPGRAM_AGENT_SPEAK_MODEL_DE = os.getenv("DEEPGRAM_AGENT_SPEAK_MODEL_DE", DEEPGRAM_AGENT_SPEAK_MODEL)
DEEPGRAM_AGENT_SPEAK_MODEL_EN = os.getenv("DEEPGRAM_AGENT_SPEAK_MODEL_EN", "aura-2-thalia-en")
DEEPGRAM_AGENT_OUTPUT_SAMPLE_RATE = os.getenv("DEEPGRAM_AGENT_OUTPUT_SAMPLE_RATE", "24000")
DEEPGRAM_AGENT_THINK_PROVIDER = os.getenv("DEEPGRAM_AGENT_THINK_PROVIDER", "open_ai")
DEEPGRAM_AGENT_THINK_MODEL = os.getenv("DEEPGRAM_AGENT_THINK_MODEL", "gpt-4o-mini")
DEEPGRAM_AGENT_THINK_TEMPERATURE = os.getenv("DEEPGRAM_AGENT_THINK_TEMPERATURE", "0.3")
DEEPGRAM_AGENT_PROMPT_MAX_CHARS = os.getenv("DEEPGRAM_AGENT_PROMPT_MAX_CHARS", "12000")

ACS_SOURCE_NUMBER = os.getenv("ACS_SOURCE_NUMBER")
ACS_CONNECTION_STRING = os.getenv("ACS_CONNECTION_STRING")
DEVTUNNEL_ID = os.getenv("DEVTUNNEL_ID")
ACS_CALLBACK_PATH = f"{DEVTUNNEL_ID}/acs/callbacks"
ACS_MEDIA_STREAMING_WS = f"wss://{DEVTUNNEL_ID.replace('https://', '')}/realtime-acs"

AZURE_BLOB_CONN = os.getenv("AZURE_BLOB_CONN")
AZURE_BLOB_ACCOUNT_KEY = os.getenv("AZURE_BLOB_ACCOUNT_KEY")

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")

AZURE_COSMOS_URI = os.getenv("AZURE_COSMOS_URI")
AZURE_COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY")
AZURE_COSMOS_DB_NAME = os.getenv("AZURE_COSMOS_DB_NAME")

ENV_VALIDATION_ERRORS = []

if not AZURE_OPENAI_KEY:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_KEY environment variable is required but not set")
if not AZURE_OPENAI_ENDPOINT:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_ENDPOINT environment variable is required but not set")
if not AZURE_OPENAI_DEPLOYMENT:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_DEPLOYMENT environment variable is required but not set")
if not ACS_CONNECTION_STRING:
    ENV_VALIDATION_ERRORS.append("ACS_CONNECTION_STRING environment variable is required but not set")
if not COGNITIVE_SERVICE_ENDPOINT:
    ENV_VALIDATION_ERRORS.append("COGNITIVE_SERVICE_ENDPOINT environment variable is required but not set")
if not ACS_SOURCE_NUMBER:
    ENV_VALIDATION_ERRORS.append("ACS_SOURCE_NUMBER environment variable is required but not set")
if not DEVTUNNEL_ID:
    ENV_VALIDATION_ERRORS.append("DEVTUNNEL_ID environment variable is required but not set")
if not AZURE_OPENAI_API_VERSION:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_API_VERSION environment variable is required but not set")
if not AZURE_OPENAI_API_TYPE:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_API_TYPE environment variable is required but not set")
if not ACS_MEDIA_STREAMING_WS:
    ENV_VALIDATION_ERRORS.append("ACS_MEDIA_STREAMING_WEBSOCKET_PATH environment variable is required but not set")
if not ACS_CALLBACK_PATH:
    ENV_VALIDATION_ERRORS.append("ACS_CALLBACK_PATH environment variable is required but not set")
if not AZURE_OPENAI_EMBEDDING_DEPLOYMENT:
    ENV_VALIDATION_ERRORS.append("AZURE_OPENAI_EMBEDDING_DEPLOYMENT environment variable is required but not set")
if not AZURE_SEARCH_ENDPOINT:
    ENV_VALIDATION_ERRORS.append("AZURE_SEARCH_ENDPOINT environment variable is required but not set")
if not AZURE_SEARCH_KEY:
    ENV_VALIDATION_ERRORS.append("AZURE_SEARCH_KEY environment variable is required but not set")
if not AZURE_BLOB_CONN:
    ENV_VALIDATION_ERRORS.append("AZURE_BLOB_CONN environment variable is required but not set")
if not AZURE_BLOB_ACCOUNT_KEY:
    ENV_VALIDATION_ERRORS.append("AZURE_BLOB_ACCOUNT_KEY environment variable is required but not set")
if not AZURE_COSMOS_URI:
    ENV_VALIDATION_ERRORS.append("AZURE_COSMOS_URI environment variable is required but not set")
if not AZURE_COSMOS_KEY:
    ENV_VALIDATION_ERRORS.append("AZURE_COSMOS_KEY environment variable is required but not set")
if not AZURE_COSMOS_DB_NAME:
    ENV_VALIDATION_ERRORS.append("AZURE_COSMOS_DB_NAME environment variable is required but not set")

def validate_environment():
    """Log missing environment variables as warnings, do not raise errors."""
    if ENV_VALIDATION_ERRORS:
        warning_message = "Missing required environment variables:\n" + "\n".join(f"- {error}" for error in ENV_VALIDATION_ERRORS)
        import warnings
        warnings.warn(warning_message)

def get_config() -> Dict[str, Any]:
    """Get configuration dictionary."""
    validate_environment() 
    
    return {
        "acs_connection_string": ACS_CONNECTION_STRING,
        "cognitive_service_endpoint": COGNITIVE_SERVICE_ENDPOINT,
        "acs_source_number": ACS_SOURCE_NUMBER,
        "acs_callback_path": ACS_CALLBACK_PATH,
        "acs_media_streaming_ws": ACS_MEDIA_STREAMING_WS,
        "azure_openai_endpoint": AZURE_OPENAI_ENDPOINT,
        "azure_openai_deployment": AZURE_OPENAI_DEPLOYMENT,
        "azure_openai_key": AZURE_OPENAI_KEY,
        "azure_openai_api_version": AZURE_OPENAI_API_VERSION,
        "azure_openai_api_type": AZURE_OPENAI_API_TYPE,
        "devtunnel_id": DEVTUNNEL_ID,
        "azure_openai_embedding_deployment": AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        "deepgram_api_key": DEEPGRAM_API_KEY,
        "transcription_provider": TRANSCRIPTION_PROVIDER,
        "deepgram_live_transcription_enabled": DEEPGRAM_LIVE_TRANSCRIPTION_ENABLED,
        "deepgram_live_model": DEEPGRAM_LIVE_MODEL,
        "deepgram_sample_rate": DEEPGRAM_SAMPLE_RATE,
        "deepgram_encoding": DEEPGRAM_ENCODING,
        "deepgram_language": DEEPGRAM_LANGUAGE,
        "deepgram_language_hint": DEEPGRAM_LANGUAGE_HINT,
        "voice_agent_provider": VOICE_AGENT_PROVIDER,
        "deepgram_agent_url": DEEPGRAM_AGENT_URL,
        "deepgram_agent_listen_model": DEEPGRAM_AGENT_LISTEN_MODEL,
        "deepgram_agent_listen_version": DEEPGRAM_AGENT_LISTEN_VERSION,
        "deepgram_agent_language": DEEPGRAM_AGENT_LANGUAGE,
        "deepgram_agent_language_hints": DEEPGRAM_AGENT_LANGUAGE_HINTS,
        "deepgram_agent_allow_unsupported_language_hints": DEEPGRAM_AGENT_ALLOW_UNSUPPORTED_LANGUAGE_HINTS,
        "deepgram_agent_keyterms": DEEPGRAM_AGENT_KEYTERMS,
        "deepgram_agent_asr_optimization_enabled": DEEPGRAM_AGENT_ASR_OPTIMIZATION_ENABLED,
        "deepgram_agent_language_lock_enabled": DEEPGRAM_AGENT_LANGUAGE_LOCK_ENABLED,
        "deepgram_agent_runtime_listen_updates_enabled": DEEPGRAM_AGENT_RUNTIME_LISTEN_UPDATES_ENABLED,
        "deepgram_agent_max_keyterms": DEEPGRAM_AGENT_MAX_KEYTERMS,
        "deepgram_agent_eot_threshold": DEEPGRAM_AGENT_EOT_THRESHOLD,
        "deepgram_agent_eager_eot_threshold": DEEPGRAM_AGENT_EAGER_EOT_THRESHOLD,
        "deepgram_agent_speak_model": DEEPGRAM_AGENT_SPEAK_MODEL,
        "deepgram_agent_speak_model_de": DEEPGRAM_AGENT_SPEAK_MODEL_DE,
        "deepgram_agent_speak_model_en": DEEPGRAM_AGENT_SPEAK_MODEL_EN,
        "deepgram_agent_output_sample_rate": DEEPGRAM_AGENT_OUTPUT_SAMPLE_RATE,
        "deepgram_agent_think_provider": DEEPGRAM_AGENT_THINK_PROVIDER,
        "deepgram_agent_think_model": DEEPGRAM_AGENT_THINK_MODEL,
        "deepgram_agent_think_temperature": DEEPGRAM_AGENT_THINK_TEMPERATURE,
        "deepgram_agent_prompt_max_chars": DEEPGRAM_AGENT_PROMPT_MAX_CHARS,
        "azure_search_endpoint": AZURE_SEARCH_ENDPOINT,
        "azure_search_key": AZURE_SEARCH_KEY,
        "azure_blob_conn": AZURE_BLOB_CONN,
        "azure_blob_account_key": AZURE_BLOB_ACCOUNT_KEY,
        "azure_cosmos_uri": AZURE_COSMOS_URI,
        "azure_cosmos_key": AZURE_COSMOS_KEY,
        "azure_cosmos_db_name": AZURE_COSMOS_DB_NAME,
    }
