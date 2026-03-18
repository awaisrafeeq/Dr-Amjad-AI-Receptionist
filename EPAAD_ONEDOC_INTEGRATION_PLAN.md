---
description: End-to-end plan to integrate ePaad/OneDoc appointment APIs into the MedCenter Volta calling agent (decision-based, easily tailored after client answers)
---

# ePaad/OneDoc Integration Plan (End-to-End)

## 0) Goal
Enable the calling agent to:
- Offer available appointment times ("free slots")
- Create appointment requests
- (Optionally) cancel/reschedule appointments
- Handle multiple doctors/calendars
- Provide multilingual conversation (caller language)

**Important constraint from spec:** `CreateEvent` creates an **appointment request**, not a confirmed appointment. The agent must communicate this clearly.

## 1) Known inputs (already confirmed)
- **Hostname:** `mcv.epaad.ch`
- **Auth:** `POST /api/v1/authenticate` → Bearer token
- **Calendar example:** Dr. Mallisho, Calendar-ID `4`
- **Timezone:** `Europe/Zurich`

### Client-confirmed operating rules
- **Opening hours (practice):**
  - Mon–Wed, Fri: `08:00–12:00` and `13:30–17:30`
  - Thu: `08:00–12:00`
- **Availability endpoint:** none (must calculate)
- **Token refresh:** assume refresh every ~60 minutes (or on 401)

## 2) Decision points (tailor after client replies)
These answers shape the integration. Keep this section updated.

### D1) Doctors ↔ calendars mapping
- **Client answer:** multiple doctors exist and each has its respective calendar.
- **Current state:** only Calendar-ID `4` is activated in OneDoc; other calendars (e.g., `5`, `6`) exist but are not activated yet.
- **Implementation:**
  - Use `GET /api/v1/onedoc/calendars` as the authoritative source of “active” calendars.
  - Until all doctors are activated, support a temporary config list for extra calendar IDs:
    - `EPAAD_EXTRA_CALENDAR_IDS="5,6"`
    - These will show up as `activated=false` and `Unknown Doctor (Calendar X)` until OneDoc activation.

### D2) Availability source
- **Client answer:** no availability endpoint → compute from `events` + `appointment_blocks`.

### D3) Working hours + slot duration
- **Client answer:** opening hours are defined out-of-band (see above). Use those hours for availability computation.
- **Slot size:** default to 15 minutes (configurable).

### D4) Appointment request status
- **Client answer:** no clear status mechanism known.
- **Implementation:** treat as "request submitted".
- **Optional upgrade (later):** implement change polling via `/onedoc/events/changes` if client confirms semantics.

### D5) Cancel/reschedule without `onedoc_key`
- **Client answer:** not sure; likely a booking id or `onedoc_key`.
- **Implementation:**
  - If the booking was created by our bot, we store `onedoc_key` and can cancel.
  - If caller has neither `onedoc_key` nor another supported identifier, fall back to manual staff follow-up.

### D6) Rate limits + token lifetime
- Add client-side caching/backoff and token refresh logic.

## 3) Architecture overview (recommended)

### 3.1 Server-side API client (required)
Create a dedicated module `utils/epaad_client.py` that:
- Stores credentials from environment
- Performs `authenticate()` and caches the token in memory
- Refreshes token on 401/invalid token and proactively refreshes at ~60 minutes
- Provides typed wrappers:
  - `get_calendars()`
  - `get_events(calendar_id, from_dt, until_dt, type=None)`
  - `create_event(calendar_id, payload)`
  - `delete_event(calendar_id, onedoc_key)`
  - `get_event(calendar_id, event_id, type)`
  - (optional) changes endpoints for background sync

**Implementation detail:** use `aiohttp` (matches project) and keep timeouts/retries bounded.

### 3.2 Availability/slot engine (if D2=no endpoint)
Module `utils/availability.py`:
- Input: date range, `events` (appointments) + `appointment_blocks`
- Config: opening hours per weekday (client-confirmed), breaks, slot size minutes, lead time
- Output: list of available intervals and/or discrete start times

Recommended approach:
- Normalize all times into timezone-aware `datetime` (Europe/Zurich)
- Build busy intervals
- Subtract from working windows
- Produce candidate slots (e.g., 15-min grid)

**Time-of-day filters (for conversation):**
- `morning`: 08:00–12:00
- `afternoon`: 13:30–17:30
- `today`: restrict from “now + lead_time”
- `tomorrow`: full windows

If the caller says a vague time (“morning/afternoon”), filter the computed free slots to that window.

### 3.3 Persistence / audit trail (Cosmos DB)
Store key events for traceability:
- `booking_request_created`: caller, calendar_id, startDateTime, patient data, `onedoc_key`, call/session id
- `booking_request_failed`: error details
- `cancel_request_created`: with `onedoc_key` when available
- `manual_followup_required`: if no lookup possible

This can be done via existing `session_manager`/Cosmos logic.

## 4) Conversation design (call flow)

### 4.1 Intents to support
- **FindAvailability**: "I need an appointment" + preferred date/time
- **BookAppointment**: confirm slot + collect patient details
- **CancelAppointment**: cancel existing (if supported)
- **RescheduleAppointment**: cancel + rebook (only if supported)

### 4.4 Professional voice call flow (booking)
This is the target production flow.

1. **Call pickup**
   - Agent answers and greets (default greeting), then switches to the caller’s language.

2. **Understand request**
   - If user wants an appointment: proceed.

3. **Doctor selection**
   - Ask: “Which doctor would you like to see?”
   - Match to an active calendar (from `GET /onedoc/calendars`).
   - If doctor not found, offer a short list of available doctors.

4. **Date preference**
   - Ask: “When do you need it?”
   - Parse: specific date, “tomorrow”, “next week”, etc.

5. **Time-of-day preference**
   - Ask: “What time works best: morning or afternoon?”
   - If user gives a specific time: treat as a narrow window around that time.

6. **Fetch & compute free slots**
   - Call `get_events(calendar_id, fromDateTime, untilDateTime)` for the relevant day/range.
   - Compute availability inside opening hours.
   - Filter to the requested time-of-day window.
   - Present 3–5 slot options.

7. **Slot confirmation**
   - User chooses a slot.
   - Confirm: date/time + doctor + location.

8. **Collect patient details**
   - First name, last name
   - Date of birth
   - Gender
   - Phone + email
   - Reason/comment
   - Ask for confirmation of spelling/summary.

9. **Create appointment request**
   - Call `CreateEvent` with the chosen `startDateTime` and patient fields.
   - If API returns `onedoc_key` (or any id), store it and read it back to the caller.
   - Confirm as “request submitted” (unless client later confirms real-time confirmation semantics).

10. **Close**
   - Provide next steps (clinic confirmation) and end call politely.

### 4.5 Professional voice call flow (reschedule/update)
Given the spec’s constraints, treat reschedule as: cancel + create new request.

1. Ask for the booking identifier (`onedoc_key`) if the caller has it.
2. If not available:
   - If client later provides a lookup endpoint, use it.
   - Otherwise collect details and route to manual staff follow-up.
3. If `onedoc_key` is available and cancel is supported:
   - Cancel existing (request cancellation)
   - Repeat booking flow to create a new request.

### 4.2 Required patient fields (per client/spec)
Minimal:
- `firstName`, `lastName`, `birthDate`, `gender`
- `mobilePhoneNumber` and/or `email`
- `comment` (reason)
Optional address:
- `street`, `streetNumber`, `zipCode`, `city`, `state`, `country`

### 4.3 Multilingual requirements
- Always respond in caller language.
- Use STT auto-detect (already configured).
- If caller mixes languages, follow the most recent user language.

## 5) Realtime integration strategy (tool-like pattern)

### 5.1 Recommended: server-executed tools
The model should not "pretend" to book. It should request a tool call.

**Pattern:**
1. Model emits a structured JSON command in text (e.g., `{"tool":"appointments.search","args":{...}}`).
2. Server detects it, calls ePaad API, then injects tool result back via `response.create`.

Tools to implement:
- `appointments.list_doctors_or_calendars`
- `appointments.search_availability`
- `appointments.create_request`
- `appointments.cancel_request`

### 5.2 Fallback: rule-based server intent detection
Use only if tool pattern is too heavy initially. Less reliable.

## 6) End-to-end flows

### Flow A: Caller asks for availability
1. Ask doctor (if multiple) else select default calendar
2. Ask preferred date/time window
3. Compute availability:
   - D2=yes endpoint → call availability endpoint
   - D2=no endpoint → call `get_events` + compute free slots
4. Offer 3-5 options

### Flow B: Caller confirms a slot (booking request)
1. Collect patient fields
2. Call `POST /calendars/{id}/events` with `{ event: ... }`
3. Store returned `onedoc_key`
4. Confirm to caller:
   - "Request submitted" and next steps

### Flow C: Caller wants to cancel
1. If we have `onedoc_key` (from our DB):
   - call `DELETE /events/{onedoc_key}`
2. If we do not have `onedoc_key`:
   - If D5=search exists: search then cancel
   - Else: collect details and mark `manual_followup_required`

## 7) Background sync (optional but useful)
If the client wants near-real-time confirmation:
- Use `GET /onedoc/events/changes` and `GET /onedoc/events/last-change-id`
- Store last processed change id
- Apply updates and optionally notify staff/caller

## 8) Environment variables to add
Add to `.env` (names can be adjusted):
- `EPAAD_BASE_URL=https://mcv.epaad.ch`
- `EPAAD_USERNAME=...`
- `EPAAD_PASSWORD=...`
- `EPAAD_DEFAULT_CALENDAR_ID=4`
- `EPAAD_TIMEZONE=Europe/Zurich`

If multiple calendars:
- `EPAAD_CALENDAR_MAP_JSON={"dr_mallisho":4,"dr_x":5}`

If computing availability:
- `EPAAD_SLOT_MINUTES=15`
- `EPAAD_OPENING_HOURS_JSON={"mon":[["08:00","12:00"],["13:30","17:30"]],"tue":[["08:00","12:00"],["13:30","17:30"]],"wed":[["08:00","12:00"],["13:30","17:30"]],"thu":[["08:00","12:00"]],"fri":[["08:00","12:00"],["13:30","17:30"]]}`

## 9) Testing plan

### 9.1 API tests (scripted)
- Authenticate
- Get calendars
- Get events in range
- Create request (test patient)
- Delete request (if allowed)

### 9.2 Voice tests
- Ask availability (German/English)
- Confirm booking
- Barge-in (caller interrupts)
- Silence handling
- Cancellation path

## 10) Implementation checklist (what to build next)
1. `utils/epaad_client.py` (async client + token cache)
2. `utils/availability.py` (if needed)
3. `routers/appointments_routes.py` for debug endpoints (optional)
4. Tool protocol in Realtime layer (recommended)
5. Cosmos logging for all booking/cancel requests

## 12) Implemented backend endpoints (current)
These FastAPI endpoints are now available for the calling-agent integration:

- `GET /appointments/doctors`
  - Returns calendars (doctors) from OneDoc + optional unactivated calendar IDs from `EPAAD_EXTRA_CALENDAR_IDS`.
- `POST /appointments/slots`
  - Input: `calendar_id`, `day` (YYYY-MM-DD), `time_of_day` (morning/afternoon/any), `slot_minutes`
  - Output: list of discrete free slot start times (computed from events within opening hours).
- `POST /appointments/create`
  - Creates an appointment request (`CreateEvent`). Returns API response (expect `onedoc_key`).
- `POST /appointments/cancel`
  - Cancels by `onedoc_key` (`DeleteEvent` endpoint in spec; creates cancellation request).
- `POST /appointments/event`
  - Gets a single event by `event_id` and `type`.
- `POST /appointments/reschedule`
  - Performs cancel + create (since `PUT` is not available in spec).
- `GET /appointments/changes`
  - Returns change feed (`afterChangeId` optional).
- `GET /appointments/last-change-id`
  - Returns latest change id.

## 11) Open questions to resolve (copy/paste)
- Multiple doctors → multiple calendars? How to map?
- Any availability endpoint?
- Working hours + slot duration rules source?
- Request status/confirmation mechanism?
- Search/lookup endpoint for cancel/reschedule without onedoc_key?
- Rate limits + token lifetime?
