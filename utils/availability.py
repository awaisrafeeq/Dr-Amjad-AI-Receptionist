import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Literal, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


TimeOfDay = Literal["morning", "afternoon", "any"]


@dataclass
class OpeningHours:
    # weekday: 0=Mon .. 6=Sun
    windows_by_weekday: Dict[int, List[Tuple[time, time]]]


DEFAULT_TZ = os.getenv("EPAAD_TIMEZONE", "Europe/Zurich")
DEFAULT_SLOT_MINUTES = int(os.getenv("EPAAD_SLOT_MINUTES", "15"))


def default_opening_hours() -> OpeningHours:
    # Client-confirmed practice hours
    # Mon–Wed, Fri: 08:00–12:00 and 13:30–17:30
    # Thu: 08:00–12:00
    windows: Dict[int, List[Tuple[time, time]]] = {
        0: [(time(8, 0), time(12, 0)), (time(13, 30), time(17, 30))],
        1: [(time(8, 0), time(12, 0)), (time(13, 30), time(17, 30))],
        2: [(time(8, 0), time(12, 0)), (time(13, 30), time(17, 30))],
        3: [(time(8, 0), time(12, 0))],
        4: [(time(8, 0), time(12, 0)), (time(13, 30), time(17, 30))],
        5: [],
        6: [],
    }
    return OpeningHours(windows_by_weekday=windows)


def _tzinfo():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(DEFAULT_TZ)
    except Exception:
        return None


def parse_epaad_datetime(dt_str: str) -> datetime:
    # API returns ISO8601 without timezone in examples.
    dt = datetime.fromisoformat(dt_str)
    tz = _tzinfo()
    if tz and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_busy(window: Tuple[datetime, datetime], busy: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    free: List[Tuple[datetime, datetime]] = []
    w_start, w_end = window
    cursor = w_start
    for b_start, b_end in busy:
        if b_end <= cursor:
            continue
        if b_start >= w_end:
            break
        if b_start > cursor:
            free.append((cursor, min(b_start, w_end)))
        cursor = max(cursor, b_end)
        if cursor >= w_end:
            break
    if cursor < w_end:
        free.append((cursor, w_end))
    return [(s, e) for s, e in free if e > s]


def _time_of_day_window(d: date, tod: TimeOfDay) -> Optional[Tuple[datetime, datetime]]:
    tz = _tzinfo()
    if tod == "any":
        return None
    if tod == "morning":
        start_t, end_t = time(8, 0), time(12, 0)
    else:
        start_t, end_t = time(13, 30), time(17, 30)
    s = datetime.combine(d, start_t)
    e = datetime.combine(d, end_t)
    if tz:
        s = s.replace(tzinfo=tz)
        e = e.replace(tzinfo=tz)
    return (s, e)


def compute_free_intervals(
    *,
    events: Iterable[dict],
    target_date: date,
    opening_hours: Optional[OpeningHours] = None,
    time_of_day: TimeOfDay = "any",
    now_dt: Optional[datetime] = None,
) -> List[Tuple[datetime, datetime]]:
    """Return free time intervals (start,end) for a single day."""

    opening_hours = opening_hours or default_opening_hours()
    windows = opening_hours.windows_by_weekday.get(target_date.weekday(), [])
    if not windows:
        return []

    tz = _tzinfo()
    busy: List[Tuple[datetime, datetime]] = []
    for e in events:
        try:
            s = parse_epaad_datetime(e["startDateTime"])
            e_end = parse_epaad_datetime(e["endDateTime"])
        except Exception:
            continue
        if s.date() != target_date:
            continue
        busy.append((s, e_end))

    busy = _merge_intervals(busy)

    free_intervals: List[Tuple[datetime, datetime]] = []
    for w_s_t, w_e_t in windows:
        w_s = datetime.combine(target_date, w_s_t)
        w_e = datetime.combine(target_date, w_e_t)
        if tz:
            w_s = w_s.replace(tzinfo=tz)
            w_e = w_e.replace(tzinfo=tz)

        tod_window = _time_of_day_window(target_date, time_of_day)
        if tod_window:
            w_s = max(w_s, tod_window[0])
            w_e = min(w_e, tod_window[1])
        if w_e <= w_s:
            continue

        free_intervals.extend(_subtract_busy((w_s, w_e), busy))

    if now_dt is not None and now_dt.date() == target_date:
        free_intervals = [(max(s, now_dt), e) for s, e in free_intervals if e > max(s, now_dt)]

    return [(s, e) for s, e in free_intervals if e > s]


def compute_free_slots(
    *,
    events: Iterable[dict],
    target_date: date,
    opening_hours: Optional[OpeningHours] = None,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
    time_of_day: TimeOfDay = "any",
    now_dt: Optional[datetime] = None,
) -> List[datetime]:
    """Return discrete available slot start times for a single day."""

    free_intervals = compute_free_intervals(
        events=events,
        target_date=target_date,
        opening_hours=opening_hours,
        time_of_day=time_of_day,
        now_dt=now_dt,
    )

    slots: List[datetime] = []
    step = timedelta(minutes=slot_minutes)

    for s, e in free_intervals:
        cursor = s
        # Align to the next step boundary
        minute_mod = (cursor.minute % slot_minutes)
        if minute_mod != 0:
            cursor = cursor + timedelta(minutes=(slot_minutes - minute_mod))
            cursor = cursor.replace(second=0, microsecond=0)

        while cursor + step <= e:
            slots.append(cursor)
            cursor += step

    return slots
