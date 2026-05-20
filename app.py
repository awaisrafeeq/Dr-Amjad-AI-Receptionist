import os
import logging
import sys
import importlib.util
from datetime import datetime
from logging.handlers import RotatingFileHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
import uvicorn
from contextlib import asynccontextmanager

from utils.session_manager import session_manager


def _load_router_module(module_name: str):
    module_path = os.path.join(APP_DIR, "routers", f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load router module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from routers import (acs_call_events_router, document_router, appointments_router)
except ModuleNotFoundError:
    acs_call_events_router = _load_router_module("acs_call_events_routes").router
    document_router = _load_router_module("document_routes").router
    appointments_router = _load_router_module("appointments_routes").router


# Setup logging to both console and file
# Use /home/logs for Azure App Service (persistent storage), fallback to /tmp/logs if not writable
log_dir = "/home/logs" if os.path.exists("/home") else os.path.join(os.path.dirname(__file__), "logs")

# Try to create the logs directory, fallback to /tmp/logs if it fails
try:
    os.makedirs(log_dir, exist_ok=True)
    # Test if directory is writable
    test_file = os.path.join(log_dir, ".write_test")
    with open(test_file, "w") as f:
        f.write("test")
    os.remove(test_file)
except (OSError, PermissionError, FileNotFoundError):
    log_dir = "/tmp/logs"
    os.makedirs(log_dir, exist_ok=True)
    print(f"Warning: Could not use /home/logs, falling back to {log_dir}")

log_file = os.path.join(log_dir, f"app_{datetime.now().strftime('%Y%m%d')}.log")

# Create formatter
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# File handler (10MB per file, keep 5 backup files)
file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler],
    force=True
)

logger = logging.getLogger(__name__)
logger.info(f"Logging to file: {log_file}")

# Suppress noisy third-party loggers
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("azure.cosmos").setLevel(logging.WARNING)
logging.getLogger("azure.core").setLevel(logging.WARNING)
logging.getLogger("azure.communication").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize services on startup and cleanup on shutdown."""
    # Startup
    logger.info("Initializing Azure Storage Logger and Session Manager...")
    try:
        await session_manager.initialize()
        logger.info("Services initialized successfully!")
    except Exception as e:
        logger.error(f"Failed to initialize services: {e}")
        # The instruction implies enabling the session manager, so we should not continue without it.
        # Re-raising the exception or letting it propagate will prevent the app from starting if initialization fails.
        raise # Re-raise the exception to prevent the app from starting without session manager
    
    yield

# FastAPI app
app = FastAPI(
    title="Hospital Reception Bot",
    description="AI backend for hospital reception bot",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False, 
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Endpoints ---

@app.get("/")
async def root():
    return {"status": "Hospital Reception Agent is running"}
    
app.include_router(acs_call_events_router)
app.include_router(document_router)
app.include_router(appointments_router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=port,
        reload=True 
    )
