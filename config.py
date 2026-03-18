import os
from typing import Dict, Any
from dotenv import load_dotenv


load_dotenv()

# --- Load Environment Variables ---
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
COGNITIVE_SERVICE_ENDPOINT = os.getenv("COGNITIVE_SERVICE_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_API_TYPE = os.getenv("AZURE_OPENAI_API_TYPE")

AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

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
        "azure_search_endpoint": AZURE_SEARCH_ENDPOINT,
        "azure_search_key": AZURE_SEARCH_KEY,
        "azure_blob_conn": AZURE_BLOB_CONN,
        "azure_blob_account_key": AZURE_BLOB_ACCOUNT_KEY,
        "azure_cosmos_uri": AZURE_COSMOS_URI,
        "azure_cosmos_key": AZURE_COSMOS_KEY,
        "azure_cosmos_db_name": AZURE_COSMOS_DB_NAME,
    }