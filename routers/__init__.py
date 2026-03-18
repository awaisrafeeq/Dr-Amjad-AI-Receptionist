from .acs_call_events_routes import router as acs_call_events_router
from .document_routes import router as document_router
from .appointments_routes import router as appointments_router

__all__ = [
    "acs_call_events_router",
    "document_router",
    "appointments_router"
]
