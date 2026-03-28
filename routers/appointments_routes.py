from fastapi import APIRouter, HTTPException
import logging
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional
from datetime import datetime, date, timedelta

from utils.epaad_client import epaad_client
from utils.availability import compute_free_slots, compute_free_intervals, default_opening_hours

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/appointments", tags=["appointments"])


TimeOfDay = Literal["morning", "afternoon", "any"]


class DoctorInfo(BaseModel):
    calendar_id: int
    doctor_name: str
    location_name: Optional[str] = None
    activated: bool


class ListDoctorsResponse(BaseModel):
    doctors: List[DoctorInfo]


class FindSlotsRequest(BaseModel):
    calendar_id: int
    day: str = Field(..., description="YYYY-MM-DD")
    time_of_day: TimeOfDay = "any"
    slot_minutes: int = 15
    debug: bool = False


class SlotItem(BaseModel):
    startDateTime: str


class FindSlotsResponse(BaseModel):
    calendar_id: int
    day: str
    time_of_day: TimeOfDay
    slot_minutes: int
    slots: List[SlotItem]
    debug: Optional[Dict[str, Any]] = None


class InspectEventsRequest(BaseModel):
    calendar_id: int
    day: str = Field(..., description="YYYY-MM-DD")
    limit: int = 20


class VerifyWindowRequest(BaseModel):
    calendar_id: int
    fromDateTime: str = Field(..., description="YYYY-MM-DDTHH:MM:SS")
    untilDateTime: str = Field(..., description="YYYY-MM-DDTHH:MM:SS")
    match_slot_times: Optional[List[str]] = Field(
        default=None,
        description="Optional list of HH:MM times to flag as matches (local time).",
    )
    match_comment_contains: Optional[str] = Field(
        default=None,
        description="Optional substring to search for in event summary/comment when available.",
    )


class CreateRequestBody(BaseModel):
    calendar_id: int
    event: Dict[str, Any]


class CancelRequestBody(BaseModel):
    calendar_id: int
    onedoc_key: str


class GetEventRequest(BaseModel):
    calendar_id: int
    event_id: int
    event_type: Literal["appointment", "appointment_block"] = "appointment"


class RescheduleRequestBody(BaseModel):
    calendar_id: int
    onedoc_key: str
    new_event: Dict[str, Any]


def _parse_date(d: str) -> date:
    try:
        return date.fromisoformat(d)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid 'day' format. Use YYYY-MM-DD")


def _to_iso_local(dt: datetime) -> str:
    # Use local ISO format without timezone suffix (matches API examples)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def _to_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


@router.get("/doctors", response_model=ListDoctorsResponse)
async def list_doctors():
    """List activated calendars from OneDoc API. Unactivated calendars are supported via env config in later steps."""
    logger.info("[API /doctors] Request received")
    try:
        calendars = await epaad_client.get_calendars()
        logger.info(f"[API /doctors] Fetched {len(calendars) if calendars else 0} calendars")
    except Exception as e:
        logger.error(f"[API /doctors ERROR] Failed to fetch calendars: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    doctors: List[DoctorInfo] = []
    for cal in calendars or []:
        professional = cal.get("professional") or {}
        first = (professional.get("firstName") or "").strip()
        last = (professional.get("lastName") or "").strip()
        name = (f"Dr. {first} {last}").strip() if (first or last) else "Unknown Doctor"

        location = cal.get("location") or {}
        loc_name = (location.get("name") or None)

        try:
            cal_id = int(cal.get("id"))
        except Exception:
            continue

        doctors.append(
            DoctorInfo(
                calendar_id=cal_id,
                doctor_name=name,
                location_name=loc_name,
                activated=True,
            )
        )

    # Optional: include unactivated calendar IDs from env
    # EPAAD_EXTRA_CALENDAR_IDS="5,6" -> [5,6]
    import os

    extra = (os.getenv("EPAAD_EXTRA_CALENDAR_IDS") or "").strip()
    if extra:
        for part in extra.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                cal_id = int(part)
            except Exception:
                continue
            if any(d.calendar_id == cal_id for d in doctors):
                continue
            doctors.append(
                DoctorInfo(
                    calendar_id=cal_id,
                    doctor_name=f"Unknown Doctor (Calendar {cal_id})",
                    location_name=None,
                    activated=False,
                )
            )

    return ListDoctorsResponse(doctors=sorted(doctors, key=lambda d: d.calendar_id))


@router.post("/slots", response_model=FindSlotsResponse)
async def find_slots(payload: FindSlotsRequest):
    logger.info(f"[API /slots] Request: calendar_id={payload.calendar_id}, day={payload.day}, tod={payload.time_of_day}")
    target_day = _parse_date(payload.day)

    # Fetch events for that day range (00:00 → 23:59) in local time.
    start_dt = datetime.combine(target_day, datetime.min.time())
    end_dt = datetime.combine(target_day, datetime.max.time()).replace(microsecond=0)

    try:
        events = await epaad_client.get_events(
            calendar_id=payload.calendar_id,
            from_dt=_to_iso_local(start_dt),
            until_dt=_to_iso_local(end_dt),
        )
        logger.info(f"[API /slots] Fetched {len(events) if events else 0} events")
    except Exception as e:
        logger.error(f"[API /slots ERROR] Failed to fetch events: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    now_dt = datetime.now()
    slots = compute_free_slots(
        events=events or [],
        target_date=target_day,
        opening_hours=default_opening_hours(),
        slot_minutes=payload.slot_minutes,
        time_of_day=payload.time_of_day,
        now_dt=now_dt,
    )
    logger.info(f"[API /slots] Computed {len(slots)} free slots")

    debug_payload: Optional[Dict[str, Any]] = None
    if payload.debug:
        # Summarize how many events matched the requested day and what windows we used.
        from utils.availability import parse_epaad_datetime

        events_on_day = 0
        missing_fields = 0
        for ev in events or []:
            try:
                s = parse_epaad_datetime(ev["startDateTime"])
                _ = parse_epaad_datetime(ev["endDateTime"])
            except Exception:
                missing_fields += 1
                continue
            if s.date() == target_day:
                events_on_day += 1

        oh = default_opening_hours().windows_by_weekday.get(target_day.weekday(), [])

        free_intervals = compute_free_intervals(
            events=events or [],
            target_date=target_day,
            opening_hours=default_opening_hours(),
            time_of_day=payload.time_of_day,
            now_dt=now_dt,
        )

        debug_payload = {
            "events_fetched": len(events or []),
            "events_on_day": events_on_day,
            "events_skipped_parse": missing_fields,
            "opening_windows": [[w[0].strftime("%H:%M"), w[1].strftime("%H:%M")] for w in oh],
            "time_of_day": payload.time_of_day,
            "free_intervals": [[_to_hm(s), _to_hm(e)] for s, e in free_intervals],
        }

    return FindSlotsResponse(
        calendar_id=payload.calendar_id,
        day=payload.day,
        time_of_day=payload.time_of_day,
        slot_minutes=payload.slot_minutes,
        slots=[SlotItem(startDateTime=_to_iso_local(s)) for s in slots],
        debug=debug_payload,
    )


@router.post("/inspect-events")
async def inspect_events(payload: InspectEventsRequest):
    logger.info(f"[API /inspect-events] Request: calendar_id={payload.calendar_id}, day={payload.day}")
    """Debug endpoint: fetch raw events for a calendar/day and return a small sanitized subset."""
    target_day = _parse_date(payload.day)
    start_dt = datetime.combine(target_day, datetime.min.time())
    end_dt = datetime.combine(target_day, datetime.max.time()).replace(microsecond=0)

    try:
        events = await epaad_client.get_events(
            calendar_id=payload.calendar_id,
            from_dt=_to_iso_local(start_dt),
            until_dt=_to_iso_local(end_dt),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    out: List[Dict[str, Any]] = []
    for ev in (events or [])[: max(1, min(payload.limit, 200))]:
        start = ev.get("startDateTime")
        end = ev.get("endDateTime")
        # Try to normalize to ISO format if possible
        try:
            from utils.availability import parse_epaad_datetime

            if isinstance(start, str):
                start = _to_iso_local(parse_epaad_datetime(start))
            if isinstance(end, str):
                end = _to_iso_local(parse_epaad_datetime(end))
        except Exception:
            pass

        out.append(
            {
                "startDateTime": start,
                "endDateTime": end,
                "type": ev.get("type"),
                "id": ev.get("id"),
            }
        )

    return {
        "calendar_id": payload.calendar_id,
        "day": payload.day,
        "events_fetched": len(events or []),
        "events_sample": out,
    }


@router.post("/verify-window")
async def verify_window(payload: VerifyWindowRequest):
    """Fetch events in a window and return a small normalized subset plus match flags."""
    try:
        events = await epaad_client.get_events(
            calendar_id=payload.calendar_id,
            from_dt=payload.fromDateTime,
            until_dt=payload.untilDateTime,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Some upstream environments appear to ignore the from/until query params.
    # We therefore enforce window filtering locally for deterministic verification.
    from utils.availability import parse_epaad_datetime
    import os

    try:
        from zoneinfo import ZoneInfo
    except Exception:  # pragma: no cover
        ZoneInfo = None  # type: ignore

    try:
        window_start = datetime.fromisoformat(payload.fromDateTime)
        window_end = datetime.fromisoformat(payload.untilDateTime)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid fromDateTime/untilDateTime format. Use YYYY-MM-DDTHH:MM:SS")

    # Normalize to the practice timezone (NOT the machine's local timezone).
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(os.getenv("EPAAD_TIMEZONE", "Europe/Zurich"))
        except Exception:
            tz = None

    if tz is not None:
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=tz)
        else:
            window_start = window_start.astimezone(tz)

        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=tz)
        else:
            window_end = window_end.astimezone(tz)

    filtered_events: List[Dict[str, Any]] = []
    for ev in events or []:
        start_raw = ev.get("startDateTime")
        if not isinstance(start_raw, str):
            continue
        try:
            sdt = parse_epaad_datetime(start_raw)
        except Exception:
            continue
        if tz is not None:
            if getattr(sdt, "tzinfo", None) is None:
                sdt = sdt.replace(tzinfo=tz)
            else:
                sdt = sdt.astimezone(tz)
        if window_start <= sdt <= window_end:
            filtered_events.append(ev)

    events = filtered_events

    match_times = {t.strip() for t in (payload.match_slot_times or []) if (t or "").strip()}
    # Only match events that contain test comment marker to avoid matching pre-existing real appointments
    needle = "test - new booking at".lower()

    out: List[Dict[str, Any]] = []
    matches = 0

    for ev in events or []:
        start_raw = ev.get("startDateTime")
        end_raw = ev.get("endDateTime")
        etype = ev.get("type")
        ev_id = ev.get("id")
        summary = ev.get("summary") or ev.get("comment") or ""

        start_iso = start_raw
        end_iso = end_raw
        start_hm = None

        try:
            if isinstance(start_raw, str):
                sdt = parse_epaad_datetime(start_raw)
                start_iso = _to_iso_local(sdt)
                start_hm = sdt.strftime("%H:%M")
            if isinstance(end_raw, str):
                end_iso = _to_iso_local(parse_epaad_datetime(end_raw))
        except Exception:
            pass

        is_match = False
        # Only flag as match if time matches AND comment contains our test marker
        if match_times and start_hm and start_hm in match_times:
            if needle in (summary or "").lower():
                is_match = True

        if is_match:
            matches += 1

        out.append(
            {
                "startDateTime": start_iso,
                "endDateTime": end_iso,
                "time": start_hm,
                "type": etype,
                "id": ev_id,
                "summary": summary,
                "match": is_match,
            }
        )

    return {
        "calendar_id": payload.calendar_id,
        "fromDateTime": payload.fromDateTime,
        "untilDateTime": payload.untilDateTime,
        "events_fetched": len(events or []),
        "matches": matches,
        "events": out,
    }


@router.post("/create")
async def create_appointment_request(payload: CreateRequestBody):
    # Conflict check: verify the requested slot is available
    try:
        from utils.availability import parse_epaad_datetime, compute_free_intervals, default_opening_hours
        
        event_start_str = payload.event.get("startDateTime")
        if event_start_str:
            start_dt = parse_epaad_datetime(event_start_str)
            # Fetch events for the day to check for conflicts
            from_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            until_dt = start_dt.replace(hour=23, minute=59, second=59)
            
            existing_events = await epaad_client.get_events(
                calendar_id=payload.calendar_id,
                from_dt=_to_iso_local(from_dt),
                until_dt=_to_iso_local(until_dt),
            )
            
            # Check if requested slot overlaps with any existing event
            for ev in existing_events or []:
                ev_start_str = ev.get("startDateTime")
                ev_end_str = ev.get("endDateTime")
                if not ev_start_str or not ev_end_str:
                    continue
                try:
                    ev_start = parse_epaad_datetime(ev_start_str)
                    ev_end = parse_epaad_datetime(ev_end_str)
                    
                    # Default slot duration: 20 minutes
                    slot_duration = payload.event.get("durationMinutes", 20)
                    requested_end = start_dt + timedelta(minutes=slot_duration)
                    
                    # Check overlap: if requested slot overlaps with existing event
                    if start_dt < ev_end and requested_end > ev_start:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "slot_already_booked",
                                "message": f"The requested slot {event_start_str} is already occupied by another appointment",
                                "existing_event_id": ev.get("id"),
                                "existing_event_start": ev_start_str,
                            }
                        )
                except HTTPException:
                    raise
                except Exception:
                    continue
    except HTTPException:
        raise
    except Exception as e:
        # Log but don't block if availability check fails
        import logging
        logging.getLogger(__name__).warning(f"Availability check failed: {e}")
    
    # Proceed with creation
    try:
        result = await epaad_client.create_event(payload.calendar_id, payload.event)
        onedoc_key = result.get("onedoc_key") or result.get("onedocKey") or result.get("key") if isinstance(result, dict) else None
        logger.info(f"[API /create] SUCCESS! Created event with onedoc_key={onedoc_key}")
        return result
    except Exception as e:
        logger.error(f"[API /create ERROR] Failed to create event: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/cancel")
async def cancel_appointment_request(payload: CancelRequestBody):
    try:
        await epaad_client.delete_event(payload.calendar_id, payload.onedoc_key)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/event")
async def get_single_event(payload: GetEventRequest):
    try:
        return await epaad_client.get_event(
            calendar_id=payload.calendar_id,
            event_id=payload.event_id,
            event_type=payload.event_type,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/reschedule")
async def reschedule(payload: RescheduleRequestBody):
    """Reschedule by creating a cancellation request for existing onedoc_key and creating a new appointment request."""
    try:
        await epaad_client.delete_event(payload.calendar_id, payload.onedoc_key)
        result = await epaad_client.create_event(payload.calendar_id, payload.new_event)
        return {"status": "ok", "new_booking": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/changes")
async def get_changes(afterChangeId: Optional[int] = None):
    try:
        return await epaad_client.get_event_changes(after_change_id=afterChangeId)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/last-change-id")
async def get_last_change_id():
    try:
        return await epaad_client.get_latest_event_change_id()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# Internal endpoint for adding new patients to phonebook (background/async)
class AddToPhonebookRequest(BaseModel):
    first_name: str
    last_name: str
    birth_date: str = Field(..., description="YYYY-MM-DD")
    phone: str
    gender: Optional[str] = "other"
    email: Optional[str] = ""
    doctor: Optional[str] = ""
    comment: Optional[str] = ""
    language: Optional[str] = "Deutsch"


class AddToPhonebookResponse(BaseModel):
    success: bool
    message: str


@router.post("/internal/add-to-phonebook", response_model=AddToPhonebookResponse, tags=["internal"])
async def add_to_phonebook(payload: AddToPhonebookRequest):
    """
    Internal endpoint to add a new patient to the phonebook XLSX.
    Called asynchronously after appointment booking for new patients.
    """
    try:
        from utils.phonebook_lookup import get_phonebook_lookup, save_phonebook_to_blob
        
        lookup = get_phonebook_lookup()
        if not lookup:
            return AddToPhonebookResponse(
                success=False,
                message="Phonebook lookup not enabled"
            )
        
        # Check if patient already exists
        existing = lookup.lookup_by_phone(payload.phone)
        if existing:
            return AddToPhonebookResponse(
                success=False,
                message="Patient already exists in phonebook"
            )
        
        # Prepare patient data
        patient_data = {
            "first_name": payload.first_name,
            "last_name": payload.last_name,
            "birth_date": payload.birth_date,
            "phone": payload.phone,
            "gender": payload.gender,
            "email": payload.email,
            "doctor": payload.doctor,
            "comment": payload.comment,
            "language": payload.language,
        }
        
        # Add to local XLSX
        added = lookup.add_patient(patient_data)
        if not added:
            return AddToPhonebookResponse(
                success=False,
                message="Failed to add patient to phonebook"
            )
        
        # Upload updated XLSX back to Azure Blob
        uploaded = save_phonebook_to_blob(lookup.xlsx_path)
        
        if uploaded:
            logger.info(f"[Phonebook] Added patient {payload.first_name} {payload.last_name} and uploaded to blob")
            return AddToPhonebookResponse(
                success=True,
                message="Patient added to phonebook and uploaded to Azure Blob"
            )
        else:
            logger.warning(f"[Phonebook] Patient added locally but failed to upload to blob")
            return AddToPhonebookResponse(
                success=True,
                message="Patient added to phonebook but blob upload failed (will retry on next call)"
            )
            
    except Exception as e:
        logger.error(f"[Phonebook] Error adding patient: {e}")
        return AddToPhonebookResponse(
            success=False,
            message=f"Error: {str(e)}"
        )
