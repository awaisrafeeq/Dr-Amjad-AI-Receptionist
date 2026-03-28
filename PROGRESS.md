# MedCenter Volta - Client Feature Implementation Progress

**Project:** AI Receptionist Feature Updates  
**Client Request Date:** March 27, 2026  
**Status:** In Progress  

---

## Overview

This document tracks all tasks required to implement the new client features for the MedCenter Volta AI Reception System. Tasks are organized by priority and broken down into simple, actionable items.

---

## Priority Levels

- 🔴 **CRITICAL** - Must be completed first, blocks other functionality
- 🟡 **HIGH** - Important for proper system operation
- 🟢 **MEDIUM** - Enhancement features
- ⚪ **LOW** - Nice to have improvements

---

## Task List

### 🔴 CRITICAL TASKS (Priority 1)

#### Task 1.1: Add ePaad Appointment Type ID to Booking Payload
**Status:** ✅ COMPLETED  
**Files:** `utils/rtmt.py`, `utils/helpers.py`  
**Description:** 
- Added `epaad_appointmenttype_id` field to EPAAD API request
- Support IDs: 61 (15 min), 63 (20 min), 65 (30 min)
- Updated `book_appointment` tool definition to include appointment type parameter

#### Task 1.2: Add Visit Reason Collection to Conversation Flow
**Status:** ✅ COMPLETED  
**Files:** `utils/helpers.py`, `utils/rtmt.py`  
**Description:**
- Added `visit_reason` parameter to `book_appointment` tool
- AI asks: "Was ist der Grund für Ihren Termin?"
- Visit reason stored in booking payload

#### Task 1.3: Implement Appointment Duration Selection Logic
**Status:** ✅ COMPLETED  
**Files:** `utils/rtmt.py`  
**Description:**
- Logic implemented to select correct appointment type ID based on visit reason complexity:
  - ID 61: One simple issue
  - ID 63: Two issues
  - ID 65: Three+ issues, new patient, referred patient
- If uncertain, defaults to longer duration (ID 65)

#### Task 1.4: Update System Prompt with New Strict Rules
**Status:** ⬜ NOT STARTED  
**Files:** `system_prompt.md`  
**Description:**
- Replace current prompt with new strict version
- Add 10 priority rules section
- Include appointment duration logic in prompt
- Update doctor names with proper German titles
- Add exact opening sentence requirement
- Add "never discuss diagnosis" strict rule
- Add matched caller handling rules

---

### 🟡 HIGH PRIORITY TASKS (Priority 2)

#### Task 2.1: Add Hardcoded Opening Greeting in Code
**Status:** ⬜ NOT STARTED  
**Files:** `utils/rtmt.py`  
**Description:**
- Hardcode German opening in code (not just prompt):
  - "MedCenter Volta, Sie sprechen mit Kaya, der digitalen Assistentin. Wie kann ich Ihnen behilflich sein?"
- Add English variant for language switching:
  - "Hello, you have reached MedCenter Volta. My name is Kaya, your digital assistant. How may I help you?"
- Trigger greeting after session initialization

#### Task 2.2: Add Backend Phonebook Matching Logic
**Status:** ⬜ NOT STARTED  
**Files:** `utils/rtmt.py`, `utils/session_manager.py`  
**Description:**
- Check if caller phone number matches phonebook
- If matched AND caller name matches → set `matchedCaller = true`
- Skip unnecessary questions for matched callers:
  - Don't ask for name again
  - Don't ask for gender
  - Don't ask for address/email unless needed
  - Only ask: reason for visit, DOB (for booking)
- Store match status in session for reference

#### Task 2.3: Add Doctor List to Config
**Status:** ⬜ NOT STARTED  
**Files:** `config.py`  
**Description:**
- Create doctor list in config:
  - Dr. Mallisho
  - Dr. Lumpp
  - Dr. Keser
  - Dr. Osterwalder
- Add German titles mapping:
  - Herr Dr. Mallisho
  - Herr Dr. Lumpp
  - Frau Dr. Keser
  - Herr Dr. Osterwalder
- Enable doctor-specific routing lookup

---

### 🟢 MEDIUM PRIORITY TASKS (Priority 3)

#### Task 3.1: Add Diagnosis Word Filter for TTS Output
**Status:** ⬜ NOT STARTED  
**Files:** `utils/rtmt.py`  
**Description:**
- Create filter to block diagnosis words before TTS
- Block ICD codes, medical condition names
- Replace with safe fallback message if detected
- Log filtered content for review

#### Task 3.2: Add Doctor-Specific Email Routing
**Status:** ⬜ NOT STARTED  
**Files:** `utils/email_service.py`, `config.py`  
**Description:**
- Add doctor email mapping in config
- If prescription request → route to treating doctor
- Default routing to `medcentervolta@hin.ch`
- Add email subject line with request type

#### Task 3.3: Improve Turn Detection / Prevent Interruptions
**Status:** ⬜ NOT STARTED  
**Files:** `utils/helpers.py`  
**Description:**
- Review current VAD settings:
  - `threshold`: 0.8
  - `silence_duration_ms`: 1000
- Test and adjust if AI still interrupts
- Consider increasing silence duration

---

### ⚪ LOW PRIORITY TASKS (Priority 4)

#### Task 4.1: Add Call Summary Generation
**Status:** ⬜ NOT STARTED  
**Files:** `utils/session_manager.py`  
**Description:**
- Generate summary after call ends:
  - Caller category
  - Matched/unmatched status
  - Main request
  - Urgency level
  - Doctor requested
  - Appointment details if booked
- Include summary in email

#### Task 4.2: Add Transcript Language Detection
**Status:** ⬜ NOT STARTED  
**Files:** `utils/rtmt.py`  
**Description:**
- Auto-detect caller language from transcription
- Switch language if needed
- Log language for reporting

---

## Already Implemented (Verified)

| Feature | Status | Location |
|---------|--------|----------|
| Phonebook lookup | ✅ COMPLETE | `phonebook_lookup.py` |
| Add patient to phonebook | ✅ COMPLETE | `add_patient()` method |
| Upload phonebook to blob | ✅ COMPLETE | `save_phonebook_to_blob()` |
| Transcript email service | ✅ COMPLETE | `email_service.py` |
| Transcript extraction | ✅ COMPLETE | `helpers.py` |
| Call end email trigger | ✅ COMPLETE | `acs_call_events_routes.py` |
| Phonebook integration in booking | ✅ COMPLETE | `rtmt.py` |
| Barge-in handling | ✅ COMPLETE | `rtmt.py` |
| Basic privacy rules | ✅ COMPLETE | `system_prompt.md` |
| Doctor names in prompt | ✅ COMPLETE | `system_prompt.md` |

---

## Implementation Notes

### Environment Variables Required
```bash
# Existing (already set)
ENABLE_INTERNAL_PHONEBOOK_LOOKUP=true
PHONEBOOK_BLOB_CONTAINER=
PHONEBOOK_BLOB_NAME=
AZURE_BLOB_CONN=
EMAIL_DEFAULT_RECIPIENT=medcentervolta@hin.ch

# New (to be added)
DOCTOR_EMAILS_JSON={"mallisho":"...","lumpp":"...","keser":"...","osterwalder":"..."}
```

### Testing Checklist
- [ ] Test appointment booking with ID 61 (15 min)
- [ ] Test appointment booking with ID 63 (20 min)
- [ ] Test appointment booking with ID 65 (30 min)
- [ ] Test matched caller flow (fewer questions)
- [ ] Test unmatched caller flow (full questions)
- [ ] Test German opening greeting
- [ ] Test diagnosis word filtering
- [ ] Test email routing to default and doctors

---

## Progress Summary

**Total Tasks:** 14  
**Completed:** 0  
**In Progress:** 0  
**Remaining:** 14  

**Last Updated:** March 27, 2026
