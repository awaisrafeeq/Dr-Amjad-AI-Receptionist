# MedCenter Volta AI Receptionist

An intelligent voice-enabled receptionist system for medical practices. Built with **FastAPI**, **Azure Communication Services**, and **Azure OpenAI GPT-4o Realtime API**.

## Overview

This application provides a fully autonomous AI receptionist named "Kaya" that handles inbound phone calls for medical practices. It can:

- **Answer incoming phone calls** with a natural-sounding voice
- **Book, cancel, and reschedule appointments** via EPAAD/OneDoc API integration
- **Recognize returning patients** through phonebook lookup
- **Handle prescription refills and medical certificates** (request forwarding)
- **Switch languages** (German default + 8 other languages)
- **Send call transcripts** via email to staff and doctors
- **Log all call data** to Cosmos DB for analytics

## Architecture

```
┌─────────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│   Phone Call    │────▶│  Azure Communication│────▶│  FastAPI Backend │
│   (Inbound)     │     │     Services        │     │   (this app)     │
└─────────────────┘     └─────────────────────┘     └────────┬─────────┘
                                                              │
                                    ┌─────────────────────────┼─────────────────────────┐
                                    │                         │                         │
                                    ▼                         ▼                         ▼
                          ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
                          │ Azure OpenAI     │    │ EPAAD/OneDoc API │    │ Azure Cosmos DB  │
                          │ GPT-4o Realtime  │    │ (Appointments)   │    │ (Call Logging)   │
                          └──────────────────┘    └──────────────────┘    └──────────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend Framework | FastAPI + Uvicorn |
| AI Voice Model | Azure OpenAI GPT-4o Realtime Preview |
| Telephony | Azure Communication Services |
| Appointment System | EPAAD/OneDoc API |
| Database | Azure Cosmos DB |
| File Storage | Azure Blob Storage |
| Email | Azure Communication Email |
| Search | Azure AI Search |

## Project Structure

```
azure-ai-caller/
├── app.py                          # FastAPI application entry point
├── config.py                       # Environment configuration & validation
├── requirements.txt                # Python dependencies
├── system_prompt.md                # AI personality & behavior instructions
│
├── routers/
│   ├── acs_call_events_routes.py   # ACS webhooks & WebSocket handler
│   ├── appointments_routes.py      # REST API for appointment operations
│   ├── document_routes.py          # Document upload & RAG endpoints
│   └── __init__.py                 # Router exports
│
└── utils/
    ├── acs.py                      # Azure Communication Services wrapper
    ├── rtmt.py                     # Real-Time Middle Tier (OpenAI bridge)
    ├── session_manager.py          # Call session tracking & logging
    ├── epaad_client.py             # EPAAD/OneDoc API client
    ├── availability.py             # Appointment slot calculation
    ├── phonebook_lookup.py         # Patient recognition via XLSX
    ├── email_service.py            # Transcript email delivery
    ├── azure_storage_logger.py     # Cosmos DB logging
    ├── helpers.py                  # Audio format transformations
    └── document_utils.py           # Document processing for RAG
```

## Key Components

### 1. RTMiddleTier (`utils/rtmt.py`)
The core bridge between ACS audio streams and Azure OpenAI Realtime API:
- Handles bidirectional audio streaming (PCM24_K_MONO format)
- Implements function calling for appointment operations
- Manages conversation state & inactivity monitoring (auto-hangup after 3 min silence)
- Performs phonebook lookup injection for recognized callers
- Controls turn detection with 0.3s delay filtering

### 2. SessionManager (`utils/session_manager.py`)
Tracks call lifecycle and persists data:
- Creates sessions on incoming calls
- Logs all ACS events to Cosmos DB
- Stores transcriptions with speaker attribution
- Sends transcripts via email on call completion
- Tracks phonebook matches for caller identification

### 3. EPAAD Client (`utils/epaad_client.py`)
Integrates with the practice management system:
- Authenticates with EPAAD API (JWT token with 59-min TTL)
- Fetches calendars/doctors and available slots
- Creates, cancels, and reschedules appointments
- Implements retry logic for transient failures

### 4. Phonebook Lookup (`utils/phonebook_lookup.py`)
Patient recognition system:
- Loads patient data from XLSX stored in Azure Blob
- Normalizes phone numbers for matching
- Injects matched patient data into AI context
- Supports adding new patients post-booking

## Environment Variables

Required environment variables (see `.env.example`):

### Azure OpenAI
```env
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_KEY=your-key
AZURE_OPENAI_DEPLOYMENT=gpt-4o-realtime-preview
AZURE_OPENAI_API_VERSION=2024-10-01-preview
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
```

### Azure Communication Services
```env
ACS_CONNECTION_STRING=endpoint=https://...;accesskey=...
ACS_SOURCE_NUMBER=+1234567890
COGNITIVE_SERVICE_ENDPOINT=https://your-cognitive-service.cognitiveservices.azure.com/
```

### Infrastructure
```env
DEVTUNNEL_ID=your-tunnel-id.devtunnels.ms
AZURE_COSMOS_URI=https://your-db.documents.azure.com:443/
AZURE_COSMOS_KEY=your-key
AZURE_COSMOS_DB_NAME=AzureCallingAppDB
AZURE_BLOB_CONN=DefaultEndpointsProtocol=https;...
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_KEY=your-key
```

### EPAAD Integration
```env
EPAAD_BASE_URL=https://mcv.epaad.ch
EPAAD_USERNAME=your-username
EPAAD_PASSWORD=your-password
EPAAD_TIMEZONE=Europe/Zurich
```

### Email Configuration
```env
EMAIL_DEFAULT_RECIPIENT=frontdesk@example.com,backup@example.com
DOCTOR_EMAILS_JSON={"mallisho":"mallisho@example.com",...}
```

## API Endpoints

### Health & Documentation
- `GET /` - Health check
- `GET /docs` - Swagger UI
- `GET /redoc` - ReDoc documentation

### ACS Integration
- `POST /acs/incoming` - Incoming call webhook (EventGrid)
- `POST /acs/callbacks/{contextId}` - Call lifecycle callbacks
- `WS /realtime-acs` - Audio streaming WebSocket

### Appointments
- `GET /appointments/doctors` - List available doctors
- `POST /appointments/slots` - Find available appointment slots
- `POST /appointments/create` - Book an appointment
- `POST /appointments/cancel` - Cancel an appointment
- `POST /appointments/reschedule` - Reschedule an appointment
- `POST /appointments/inspect-events` - Debug: view calendar events

### Internal
- `POST /appointments/internal/add-to-phonebook` - Add new patient to phonebook

## AI Behavior (System Prompt)

The AI follows a strict multi-layer behavioral protocol defined in `system_prompt.md`:

1. **Core Philosophy**: Listen first, act second. Never assume caller intent.
2. **Constraints**: Never speak diagnoses, never reveal internal data, never say caller's name first.
3. **Opening**: Always start in German: *"MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen behilflich sein?"*
4. **Language**: Default German, switch only on explicit request (supports 8 languages).
5. **Workflows**: Structured state machine for appointments, prescriptions, certificates, and general inquiries.

### Workflow: Appointment Booking
1. Identify purpose
2. Collect name & match against phonebook
3. Gather missing demographics (DOB, email, address, insurance)
4. Get available doctors
5. Find available slots
6. Confirm booking details
7. Create appointment
8. Close call

## Data Flow

### Incoming Call Flow
1. **ACS Incoming Call** → EventGrid triggers `POST /acs/incoming`
2. **Session Creation** → `session_manager.create_session()` logs to Cosmos DB
3. **Phonebook Lookup** → Match caller number to patient record
4. **Answer Call** → ACS answers with media streaming enabled
5. **WebSocket Upgrade** → ACS connects to `WS /realtime-acs`
6. **Audio Bridge** → `RTMiddleTier` forwards audio to/from OpenAI
7. **Greeting** → AI speaks opening line (German)
8. **Conversation** → Function calls triggered for appointments
9. **Call End** → Transcript email sent, session logged

### Appointment Booking Flow
1. Caller requests appointment
2. AI calls `get_available_doctors()` function
3. Caller selects doctor
4. AI calls `get_available_slots(calendar_id, date)`
5. Caller selects slot
6. AI confirms all details
7. AI calls `book_appointment()` with patient data
8. Appointment created in EPAAD

## Logging & Monitoring

All call data is logged to Azure Cosmos DB:

- **Call Metadata**: Participants, duration, status, phonebook match
- **Transcriptions**: Speaker-attributed conversation text
- **Events**: ACS lifecycle events (CallConnected, CallDisconnected, etc.)
- **Function Calls**: AI tool invocations and results

Local logs: `/home/logs/app_YYYYMMDD.log` (10MB rotation, 5 backups)

## Deployment

### Azure App Service
1. Create Azure App Service (Linux, Python 3.11)
2. Configure all environment variables in App Settings
3. Deploy via GitHub Actions or ZIP deployment
4. Configure ACS webhook URL pointing to `/acs/incoming`
5. Enable WebSocket support in App Service configuration

### DevTunnel (Local Development)
```bash
# Start devtunnel
devtunnel host -p 8001

# Set DEVTUNNEL_ID environment variable
export DEVTUNNEL_ID=your-tunnel-id.devtunnels.ms

# Run locally
uvicorn app:app --host 0.0.0.0 --port 8001
```

## Testing

Test scripts in `/test/` directory:
- `epaad_book.py` - Test appointment creation
- `epaad_cancel.py` - Test appointment cancellation
- `epaad_free_slots.py` - Test slot availability
- `verify_booking_platform.py` - End-to-end verification

Run individual tests:
```bash
python test/epaad_free_slots.py
```

## Security Considerations

1. **Data Privacy**: Patient data never exposed to AI until identity confirmed
2. **No Diagnoses**: AI cannot speak, infer, or repeat medical conditions
3. **Encrypted Storage**: All PHI stored encrypted in Cosmos DB
4. **Access Control**: EPAAD API credentials isolated in environment variables
5. **Audit Trail**: Complete call transcripts retained for compliance

## License

Private - MedCenter Volta Internal Use

## Support

For deployment assistance or bug reports, contact the development team.
