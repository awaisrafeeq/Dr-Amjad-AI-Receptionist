# MedCenter Volta AI Receptionist – System Documentation

This document outlines the architecture and execution flow of the MedCenter Volta AI Reception Assistant.

## 1. System Overview
The MedCenter Volta AI is a real-time voice assistant designed to handle medical reception tasks, primarily managing appointment bookings, rescheduling, and general practice inquiries. It integrates telephony, state-of-the-art language processing, and a medical practice management API.

## 2. Core Architecture
The system consists of four primary layers:

### A. Telephony Layer (Azure Communication Services - ACS)
-   **Function**: Manages incoming and outgoing phone calls.
-   **Audio Stream**: Streams raw PCM16 audio bi-directionally via WebSockets.
-   **Events**: Captures call initiation, hangup, and DTMF (if needed).

### B. Intelligence Layer (OpenAI Realtime API)
-   **Model**: GPT-4o Realtime (Voice: `shimmer`).
-   **Function**: Processes speech and generates natural-sounding vocal responses with very low latency.
-   **Capabilities**: 
    -   Natural Language Understanding (NLU).
    -   Direct tool/function calling for dynamic actions.
    -   Context-aware memory during the session.

### C. Backend Bridge (rtmt.py - Middle Tier)
-   **Function**: Acts as the "orchestrator" connecting ACS to OpenAI.
-   **Key Responsibilities**:
    -   **VAD (Voice Activity Detection)**: Determines when a user is speaking vs. when the agent should speak.
    -   **Tool Execution**: Intercepts function calls from OpenAI (e.g., `get_available_slots`) and calls the appropriate backend API.
    -   **Knowledge Injection**: Searches the practice’s Knowledge Base (RAG) and feeds relevant info into the AI’s context.
    -   **Session Logging**: Records transcripts and events to Azure Cosmos DB.

### D. Practice Management Layer (EPaaD API)
-   **Function**: The source of truth for doctor schedules and patient data.
-   **Endpoints**: Used for fetching calendars, checking availability, and creating/cancelling events.

---

## 3. The Interaction Flow

### Phase 1: Call Initiation & Greeting
1.  **Trigger**: User calls the practice number.
2.  **Connection**: ACS triggers a callback to the [app.py](file:///c:/Users/Umar/Downloads/office/azure-ai-caller-main/azure-ai-caller-main/app.py), which initializes a session via [rtmt.py](file:///c:/Users/Umar/Downloads/office/azure-ai-caller-main/azure-ai-caller-main/utils/rtmt.py).
3.  **Initial Greeting**: Within 2 seconds of connection, the AI greets the caller:
    > *"Guten Tag, Sie haben das MedCenter Volta erreicht. Mein Name ist Kaya, Ihre digitale Assistentin. Wie kann ich Ihnen behilflich sein?"*
4.  **Language Check**: By default, the AI speaks German. It only switches to English, Turkish, etc., if the user explicitly asks for it.

### Phase 2: Identification & Privacy
1.  **Phonebook Lookup**: The system attempts to match the caller's phone number against the internal telephone book.
2.  **Smart Information Reuse**: 
    -   If the caller's Name and DOB are found, the AI skips asking for them and proceeds to assist.
    -   The AI **never** asks for gender (inferred automatically).
    -   The AI never reveals sensitive internal notes from the phonebook.

### Phase 3: Appointment Booking (The Core Loop)
If the user wants to book an appointment, the AI follows a strict tool-driven protocol:
1.  **Doctor Selection**: `get_available_doctors` identifies the provider.
2.  **Availability Check**: `get_available_slots` retrieves real-time availability for the chosen date.
3.  **Data Collection**: If the patient is not in the phonebook, the AI asks for:
    -   First/Last Name
    -   Date of Birth (DOB)
    -   Phone Number (Country Code included)
    -   Address (Street, City, Zip)
4.  **Booking Execution**: `book_appointment` submits the final payload to EPaaD.
5.  **Confirmation**: The AI repeats the **Booking Reference Key** clearly for the user to note down.

### Phase 4: Call Termination
1.  **Final Sign-off**: Once the user is satisfied, the AI says its goodbye.
2.  **Automatic Hangup**: The AI calls the `terminate_call` tool, which triggers an immediate hangup of the ACS phone line. This prevents background noise from keeping the call active.

---

## 4. Technical Safeguards & Settings
-   **Voice Activity Detection (VAD)**: Set to a 1000ms silence threshold to prevent the bot from interrupting users during natural pauses.
-   **Inactivity Monitor**: If a user is silent for more than 60 seconds, the agent will politely prompt them. If silent for 5 minutes, it will safely disconnect.
-   **Noise Filter**: Short phantom transcriptions (under 2 characters) are ignored to maintain a clean conversation flow.

## 5. Logging & Audit
-   **Local Logs**: `logs/app_YYYYMMDD.log` contains technical API responses and flow details.
-   **Cosmos DB**: Stores full call summaries, duration markers, and time-stamped transcripts for later review by practice staff.
