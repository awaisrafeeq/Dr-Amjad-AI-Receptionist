# Ambiguities & Clarification Questions

## 1. Doctor List & EPAAD Calendar Configuration

### Ambiguity
Specifies four doctors (Dr. Mallisho, Dr. Lumpp, Dr. Keser, Dr. Osterwalder) with proper German titles. However, we only have one set of EPAAD credentials in the current configuration.

### Current Implementation
- Added doctor list to `config` with German titles
- Configured `DOCTOR_EMAILS_JSON` environment variable for email routing
- **Calendar IDs are set to `None` for all doctors**

### Questions for Client

**Q1.1: EPAAD Calendar Setup**
- Do each of the four doctors have separate EPAAD calendars with unique Calendar IDs?
- Or is there a single shared calendar for all doctors?
- If separate, please provide the Calendar ID for each doctor:
  - Dr. Mallisho: `calendar_id = ?`
  - Dr. Lumpp: `calendar_id = ?`
  - Dr. Keser: `calendar_id = ?`
  - Dr. Osterwalder: `calendar_id = ?`

**Q1.2: Doctor Email Addresses**
For (doctor-specific email routing), we need email addresses for prescription/certificate routing:
- Dr. Mallisho email: ?
- Dr. Lumpp email: ?
- Dr. Keser email: ?
- Dr. Osterwalder email: ?

### Impact if Not Clarified
Without Calendar IDs, the system cannot route appointments to specific doctor calendars. All appointments may go to a default calendar or fail.

---

## 2. Phonebook Matching Behavior

### Ambiguity
Implements automatic phonebook lookup when a call comes in. If caller is matched, we skip name and date of birth questions.

### Questions for Client

**Q2.1: Multiple Matches**
If phone number matches multiple patients (family members), how should system behave?
- Option A: Ask "Are you [Name1] or [Name2]?"
- Option B: Proceed with most recent appointment's patient
- Option C: Always ask for name confirmation


---

## 3. Turn Detection Configuration

### Ambiguity
implements a delay after speech stops to prevent AI from interrupting the caller.

### Current Implementation
- `TURN_DETECTION_DELAY = 0.8` (800 milliseconds)
- AI waits 800ms after caller stops speaking before responding
- If caller speaks again during this delay, AI continues waiting

### Questions for Client

**Q3.1: Barge-in Behavior**
Should callers be able to interrupt Kaya while she is speaking?
- Current: Yes (barge-in enabled with response.cancel)
- Alternative: No (wait for Kaya to finish)

---

## 4. Email Routing Triggers

### Ambiguity
routes emails to specific doctors based on transcript content (prescriptions, certificates).

### Current Implementation
Routes to doctor if caller mentions:
- Prescription keywords: "rezept", "medikament", "tabletten", etc.
- Certificate keywords: "attest", "krankmeldung", "arbeitsunfähig", etc.

### Questions for Client

**Q4.1: Routing Logic Validation**
Are these the correct triggers for doctor-specific routing?
- Prescription requests → Doctor email
- Sick certificates → Doctor email
- Regular appointments → Default email (medcentervolta@hin.ch)

