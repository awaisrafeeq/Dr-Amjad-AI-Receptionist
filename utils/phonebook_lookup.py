import os
import re
import tempfile
import hashlib
import threading
import logging
import unicodedata
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any, List, Tuple

from dotenv import load_dotenv


load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class PhonebookMatch:
    patient_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    gender: Optional[str] = None
    birth_date: Optional[str] = None
    language: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    doctor: Optional[str] = None
    note: Optional[str] = None
    address: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_PHONE_DIGITS_RE = re.compile(r"\d+")


def _digits_only(value: str) -> str:
    if not value:
        return ""
    return "".join(_PHONE_DIGITS_RE.findall(value))


def normalize_phone_variants(phone: Optional[str]) -> List[str]:
    """Return normalized phone variants to improve matching.

    We keep this intentionally conservative: we mostly normalize to digits and
    optionally generate a Swiss +41 variant.
    """
    if not phone:
        return []

    digits = _digits_only(phone)
    if not digits:
        return []

    variants = {digits}

    # Convert 00CC -> CC
    if digits.startswith("00") and len(digits) > 2:
        variants.add(digits[2:])

    # If Swiss local format 0XXXXXXXXX (10 digits), also add 41XXXXXXXXX
    if digits.startswith("0") and len(digits) == 10:
        variants.add("41" + digits[1:])

    # Add suffix variant (last 9 digits) to tolerate missing country code
    if len(digits) >= 9:
        variants.add(digits[-9:])

    return sorted(v for v in variants if v)


def _cell_to_str(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    # datetime/date handling is left to callers; we only string-ify
    try:
        return str(value).strip() or None
    except Exception:
        return None


def _normalize_name(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(normalized.strip().lower().split())


_NAME_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _name_tokens(value: Optional[str]) -> List[str]:
    normalized = _normalize_name(value)
    if not normalized:
        return []
    return [token for token in _NAME_TOKEN_SPLIT_RE.split(normalized) if token]


def _token_set_name_match(
    spoken_tokens: set,
    stored_first: Optional[str],
    stored_last: Optional[str],
) -> bool:
    """Order-free name match for compound official names.

    Every spoken token must exist in the stored name, and both stored fields
    must be covered by at least one spoken token. Without that second rule a
    caller saying only "Hans Peter" would match a stored "Hans Peter Mueller"
    whose surname was never spoken.
    """
    first_tokens = set(_name_tokens(stored_first))
    last_tokens = set(_name_tokens(stored_last))

    if len(spoken_tokens) < 2 or not first_tokens or not last_tokens:
        return False

    if not spoken_tokens.issubset(first_tokens | last_tokens):
        return False

    return bool(spoken_tokens & first_tokens) and bool(spoken_tokens & last_tokens)


import time as _time

_last_blob_refresh: float = 0.0
_BLOB_REFRESH_INTERVAL = float(os.getenv("PHONEBOOK_BLOB_REFRESH_SECONDS", "1800"))  # default 30 minutes
_phonebook_refresh_lock = threading.Lock()
_phonebook_refresh_thread: Optional[threading.Thread] = None


def _download_phonebook_from_blob_to_cache(force_download: bool = False) -> Optional[str]:
    blob_url = (os.getenv("PHONEBOOK_BLOB_URL") or "").strip()
    container = (os.getenv("PHONEBOOK_BLOB_CONTAINER") or "").strip()
    blob_name = (os.getenv("PHONEBOOK_BLOB_NAME") or "").strip()
    conn = (os.getenv("AZURE_BLOB_CONN") or "").strip()

    if not conn:
        return None

    if not blob_url and not (container and blob_name):
        return None

    force = force_download or (os.getenv("PHONEBOOK_BLOB_FORCE_REFRESH") or "").strip().lower() in {"1", "true", "yes"}

    cache_key = blob_url or f"{container}/{blob_name}"
    cache_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:16]
    cache_dir = os.path.join(tempfile.gettempdir(), "medcentervolta_phonebook_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cached_path = os.path.join(cache_dir, f"phonebook_{cache_hash}.xlsx")

    if (not force) and os.path.exists(cached_path) and os.path.getsize(cached_path) > 0:
        return cached_path

    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(conn)

    if blob_url:
        blob_client = service.get_blob_client(blob_url=blob_url)
    else:
        blob_client = service.get_blob_client(container=container, blob=blob_name)

    downloader = blob_client.download_blob()
    data = downloader.readall()
    with open(cached_path, "wb") as f:
        f.write(data)

    return cached_path


def _find_header_row(rows: List[Tuple[Any, ...]]) -> Optional[int]:
    # Search first 200 rows for the header row that contains known column names.
    expected = {
        "Patienten-Nr.",
        "Nachname",
        "Vorname",
        "Geburtsdatum",
        "Mobile-Nr.",
    }

    for idx, r in enumerate(rows[:200]):
        cells = {_cell_to_str(c) for c in r}
        cells.discard(None)
        if expected.issubset(cells):
            return idx

    return None


class PhonebookLookup:
    """Loads an XLSX phonebook and provides internal-only lookup by phone number."""

    def __init__(self, xlsx_path: str):
        self.xlsx_path = xlsx_path
        self._index: Dict[str, List[PhonebookMatch]] = {}
        self._loaded = False
        self._entry_count = 0
        self._write_lock = threading.RLock()

    def load(self) -> None:
        """Ensure the index is loaded. Cheap no-op once loaded — checks
        _loaded before touching the write lock, so a live call is never
        blocked behind a background refresh() or a phonebook write that
        happens to be holding the lock at that moment (see refresh())."""
        if self._loaded:
            return
        with self._write_lock:
            if self._loaded:
                return
            index, entry_count = self._parse_workbook()
            self._index = index
            self._entry_count = entry_count
            self._loaded = True

    def refresh(self) -> None:
        """Rebuild the index from the current xlsx_path and swap it in.

        The expensive XLSX parse runs without holding the write lock, so
        concurrent readers (live calls calling load()/lookup_*) are never
        blocked while a refresh is in progress — only the final swap is
        under the lock, and that's just a couple of attribute assignments.
        """
        index, entry_count = self._parse_workbook()
        with self._write_lock:
            self._index = index
            self._entry_count = entry_count
            self._loaded = True

    def _parse_workbook(self) -> Tuple[Dict[str, List["PhonebookMatch"]], int]:
        """Pure parse of self.xlsx_path into a fresh index. Does not touch
        self._index/_entry_count/_loaded and does not require the write lock."""
        from openpyxl import load_workbook  # local import to avoid import cost if unused

        index: Dict[str, List[PhonebookMatch]] = {}
        entry_count = 0

        wb = load_workbook(self.xlsx_path, data_only=True, read_only=True)
        try:
            ws = wb[wb.sheetnames[0]]
            rows = list(ws.iter_rows(values_only=True))

            header_idx = _find_header_row(rows)
            if header_idx is None:
                # If schema changes, we fail closed (no lookup) rather than guessing.
                return index, entry_count

            header = [(_cell_to_str(c) or "") for c in rows[header_idx]]
            col = {name: i for i, name in enumerate(header) if name}

            def get(row: Tuple[Any, ...], name: str) -> Optional[str]:
                i = col.get(name)
                if i is None or i >= len(row):
                    return None
                return _cell_to_str(row[i])

            for r in rows[header_idx + 1 :]:
                patient_no = get(r, "Patienten-Nr.")
                last = get(r, "Nachname")
                first = get(r, "Vorname")
                if not (patient_no or last or first):
                    continue

                match = PhonebookMatch(
                    patient_number=patient_no,
                    last_name=last,
                    first_name=first,
                    gender=get(r, "Geschlecht"),
                    birth_date=get(r, "Geburtsdatum"),
                    language=get(r, "Sprache"),
                    phone=get(r, "Telefon"),
                    mobile=get(r, "Mobile-Nr."),
                    email=get(r, "Email"),
                    doctor=get(r, "Arzt"),
                    note=get(r, "Notiz"),
                    address=get(r, "Adresse"),
                    zip_code=get(r, "PLZ"),
                    city=get(r, "Ort"),
                )

                entry_count += 1

                for raw in [match.phone, match.mobile]:
                    for v in normalize_phone_variants(raw):
                        if v not in index:
                            index[v] = []
                        index[v].append(match)
        finally:
            wb.close()

        return index, entry_count

    def add_patient(self, patient_data: Dict[str, Any]) -> bool:
        """
        Append a new patient row to the phonebook XLSX.
        Patienten-Nr. is left empty as requested.
        
        Args:
            patient_data: Dict with keys matching phonebook columns
        Returns:
            bool: True if successfully added
        """
        with self._write_lock:
            try:
                from openpyxl import load_workbook
                logger.info(f"[PHONEBOOK] Attempting to add new patient to {self.xlsx_path} with data: {patient_data}")
                
                # Load workbook with write support
                wb = load_workbook(self.xlsx_path)
                ws = wb[wb.sheetnames[0]]
                
                # Find the header row
                header_idx = None
                for idx, row in enumerate(ws.iter_rows(values_only=True)):
                    cells = {str(c).strip() if c else "" for c in row}
                    if {"Patienten-Nr.", "Nachname", "Vorname"}.issubset(cells):
                        header_idx = idx
                        break
                
                if header_idx is None:
                    logger.error("[PHONEBOOK] Failed to find header row in Excel file.")
                    return False
                
                # Get header mapping
                header_row = list(ws.iter_rows(values_only=True))[header_idx]
                col_map = {}
                for i, cell in enumerate(header_row):
                    if cell:
                        col_map[str(cell).strip()] = i
                
                # Create new row data
                new_row = [None] * len(header_row)
                
                # Column mappings
                mappings = {
                    "Nachname": patient_data.get("last_name"),
                    "Vorname": patient_data.get("first_name"),
                    "Geburtsdatum": patient_data.get("birth_date"),
                    "Geschlecht": patient_data.get("gender"),
                    "Sprache": patient_data.get("language", "Deutsch"),
                    "PLZ": patient_data.get("zip_code"),
                    "Ort": patient_data.get("city"),
                    "Adresse": patient_data.get("address"),
                    "Telefon": patient_data.get("phone"),
                    "Mobile-Nr.": patient_data.get("mobile") or patient_data.get("phone"),
                    "Email": patient_data.get("email", ""),
                    "Arzt": patient_data.get("doctor", ""),
                    "Notiz": patient_data.get("comment", ""),
                    # Patienten-Nr. intentionally left empty
                }
                
                logger.info(f"[PHONEBOOK] Mapping data to excel columns: {mappings}")
                
                for col_name, value in mappings.items():
                    if col_name in col_map and value:
                        new_row[col_map[col_name]] = value
                
                # Append row
                ws.append(new_row)
                
                # Save workbook
                wb.save(self.xlsx_path)
                logger.info(f"[PHONEBOOK] Successfully appended and saved new patient to {self.xlsx_path}")

                # Upload to blob immediately so data is never lost if async task is cut off
                if save_phonebook_to_blob(self.xlsx_path):
                    logger.info("[PHONEBOOK] Blob upload succeeded after add_patient")
                else:
                    logger.warning("[PHONEBOOK] Blob upload failed after add_patient — data saved locally only")
                
                # Reload the index
                self._loaded = False
                self._index.clear()
                self.load()
                logger.info("[PHONEBOOK] Successfully reloaded internal phonebook index.")
                
                return True
            
            except Exception as e:
                logger.error(f"[PHONEBOOK] Error adding patient to phonebook: {e}")
                return False

    def update_patient(self, phone: str, patient_data: Dict[str, Any], first_name: Optional[str] = None, last_name: Optional[str] = None) -> bool:
        """
        Updates an existing patient row in the phonebook XLSX based on their phone number and name.
        """
        with self._write_lock:
            try:
                from openpyxl import load_workbook
                logger.info(f"[PHONEBOOK] Attempting to update patient {first_name} {last_name} with phone {phone} in {self.xlsx_path}")
                
                # Find the match to ensure they exist
                match = self.lookup_by_phone_and_name(phone, first_name, last_name)
                if not match:
                    logger.error(f"[PHONEBOOK] Cannot update: Patient {first_name} {last_name} with phone {phone} not found in index.")
                    return False
                    
                wb = load_workbook(self.xlsx_path)
                ws = wb[wb.sheetnames[0]]
                
                # Find the header row
                header_idx = None
                for idx, row in enumerate(ws.iter_rows(values_only=True)):
                    cells = {str(c).strip() if c else "" for c in row}
                    if {"Patienten-Nr.", "Nachname", "Vorname"}.issubset(cells):
                        header_idx = idx
                        break
                
                if header_idx is None:
                    logger.error("[PHONEBOOK] Failed to find header row in Excel file.")
                    return False
                    
                # Get header mapping
                header_row = list(ws.iter_rows(values_only=True))[header_idx]
                col_map = {}
                for i, cell in enumerate(header_row):
                    if cell:
                        col_map[str(cell).strip()] = i
                        
                # Find the correct row to update
                target_row_idx = None
                
                cmp_first = (first_name or "").strip().lower()
                cmp_last = (last_name or "").strip().lower()
                
                for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    if idx <= header_idx + 1:
                        continue
                    # Check phone columns
                    phone_col = col_map.get("Telefon")
                    mobile_col = col_map.get("Mobile-Nr.")
                    first_col = col_map.get("Vorname")
                    last_col = col_map.get("Nachname")
                    pat_no_col = col_map.get("Patienten-Nr.")
                    
                    cell_phone = str(row[phone_col]).strip() if phone_col is not None and row[phone_col] else ""
                    cell_mobile = str(row[mobile_col]).strip() if mobile_col is not None and row[mobile_col] else ""
                    cell_first = str(row[first_col]).strip().lower() if first_col is not None and row[first_col] else ""
                    cell_last = str(row[last_col]).strip().lower() if last_col is not None and row[last_col] else ""
                    cell_pat_no = str(row[pat_no_col]).strip() if pat_no_col is not None and row[pat_no_col] else ""
                    
                    # If we have a patient_number on the match, we can use it as a highly reliable tie-breaker
                    is_phone_match = False
                   
                    row_variants = set(normalize_phone_variants(cell_phone)) | set(normalize_phone_variants(cell_mobile))
                    for v in normalize_phone_variants(phone):
                        if v in row_variants:
                            is_phone_match = True
                            break
                            
                    if is_phone_match:
                        # If match object has a patient number, compare it
                        if match.patient_number and cell_pat_no and match.patient_number == cell_pat_no:
                            target_row_idx = idx
                            break
                        
                        # Otherwise, if names were provided, try to match by name
                        elif cmp_first or cmp_last:
                            if (cmp_first and cmp_first == cell_first) or (cmp_last and cmp_last == cell_last):
                                target_row_idx = idx
                                break
                        else:
                            # Fallback to the first matched phone row
                            target_row_idx = idx
                            break
                            
                if not target_row_idx:
                    logger.error(f"[PHONEBOOK] Match found in memory but could not find corresponding row in Excel for {phone}")
                    return False
                    
                # Column mappings (we only update fields that were provided in patient_data)
                mappings = {
                    "Nachname": patient_data.get("last_name"),
                    "Vorname": patient_data.get("first_name"),
                    "Geburtsdatum": patient_data.get("birth_date"),
                    "Geschlecht": patient_data.get("gender"),
                    "Sprache": patient_data.get("language"),
                    "PLZ": patient_data.get("zip_code"),
                    "Ort": patient_data.get("city"),
                    "Adresse": patient_data.get("address"),
                    "Email": patient_data.get("email"),
                    "Arzt": patient_data.get("doctor"),
                    "Notiz": patient_data.get("comment"),
                }
                
                logger.info(f"[PHONEBOOK] Updating row {target_row_idx} with fields: {mappings}")
                
                for col_name, value in mappings.items():
                    if col_name in col_map and value:
                        ws.cell(row=target_row_idx, column=col_map[col_name] + 1).value = value
                
                wb.save(self.xlsx_path)
                logger.info(f"[PHONEBOOK] Successfully updated patient in {self.xlsx_path}")

                # Upload to blob immediately so data is never lost if async task is cut off
                if save_phonebook_to_blob(self.xlsx_path):
                    logger.info("[PHONEBOOK] Blob upload succeeded after update_patient")
                else:
                    logger.warning("[PHONEBOOK] Blob upload failed after update_patient — data saved locally only")
                
                self._loaded = False
                self._index.clear()
                self.load()
                logger.info("[PHONEBOOK] Successfully reloaded internal phonebook index.")
                
                return True
            except Exception as e:
                logger.error(f"[PHONEBOOK] Error updating patient in phonebook: {e}")
                return False

    def lookup_by_phone(self, phone: Optional[str]) -> Optional[PhonebookMatch]:
        """Returns a phonebook match only when the phone maps to one patient."""
        if not phone:
            return None

        self.load()

        for v in normalize_phone_variants(phone):
            matches = self._index.get(v)
            if matches:
                return matches[0] if len(matches) == 1 else None

        return None

    def lookup_candidates_by_phone(self, phone: Optional[str]) -> List[PhonebookMatch]:
        """Return all patient candidates for a caller phone number."""
        if not phone:
            return []

        self.load()

        seen: set[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = set()
        candidates: List[PhonebookMatch] = []
        for v in normalize_phone_variants(phone):
            for match in self._index.get(v, []):
                key = (match.patient_number, match.first_name, match.last_name, match.birth_date)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(match)

        return candidates

    def match_by_phone_and_name(
        self,
        phone: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> Tuple[Optional[PhonebookMatch], str]:
        """Match a caller against the phonebook by phone plus first and last name.

        Names are compared in three tiers, strictest first:
          1. exact     — spoken first/last equal the stored Vorname/Nachname
          2. swapped   — the stored row has the two name fields reversed
          3. token set — every spoken token appears in the stored name in any
                         order, covering both stored fields (compound names)

        Tiers 2 and 3 only resolve when exactly one candidate qualifies, so a
        number shared by a family can never resolve to the wrong person.
        """
        if not phone:
            return None, "no_phone_match"

        candidates = self.lookup_candidates_by_phone(phone)
        if not candidates:
            return None, "no_phone_match"

        cmp_first = _normalize_name(first_name)
        cmp_last = _normalize_name(last_name)
        if not cmp_first or not cmp_last:
            return None, "missing_name"

        spoken_tokens = set(_name_tokens(first_name) + _name_tokens(last_name))
        swapped_matches: List[PhonebookMatch] = []
        token_set_matches: List[PhonebookMatch] = []

        for m in candidates:
            m_first = _normalize_name(m.first_name)
            m_last = _normalize_name(m.last_name)

            if cmp_first == m_first and cmp_last == m_last:
                return m, "matched"

            if cmp_first == m_last and cmp_last == m_first:
                swapped_matches.append(m)
            elif _token_set_name_match(spoken_tokens, m.first_name, m.last_name):
                token_set_matches.append(m)

        for status, tier in (("matched_swapped", swapped_matches), ("matched_token_set", token_set_matches)):
            if len(tier) == 1:
                m = tier[0]
                logger.info(
                    "[PHONEBOOK] %s — spoken=%r %r stored=%r %r patient=%s",
                    status,
                    first_name,
                    last_name,
                    m.first_name,
                    m.last_name,
                    m.patient_number,
                )
                return m, status
            if len(tier) > 1:
                logger.info(
                    "[PHONEBOOK] %s rejected — %d candidates for spoken=%r %r",
                    status,
                    len(tier),
                    first_name,
                    last_name,
                )
                return None, "ambiguous_name_match"

        logger.info(
            "[PHONEBOOK] Name mismatch — spoken=%r %r candidates=%s",
            first_name,
            last_name,
            [f"{m.first_name}|{m.last_name}" for m in candidates],
        )
        return None, "no_name_match"

    def lookup_by_phone_and_name(self, phone: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> Optional[PhonebookMatch]:
        """Returns the phonebook match for the given phone number that also matches the provided name."""
        match, _status = self.match_by_phone_and_name(phone, first_name, last_name)
        return match


def save_phonebook_to_blob(xlsx_path: str) -> bool:
    """
    Upload updated phonebook XLSX back to Azure Blob Storage.
    
    Args:
        xlsx_path: Local path to the XLSX file
    Returns:
        bool: True if upload successful
    """
    try:
        blob_url = (os.getenv("PHONEBOOK_BLOB_URL") or "").strip()
        container = (os.getenv("PHONEBOOK_BLOB_CONTAINER") or "").strip()
        blob_name = (os.getenv("PHONEBOOK_BLOB_NAME") or "").strip()
        conn = (os.getenv("AZURE_BLOB_CONN") or "").strip()

        if not conn:
            return False

        if not blob_url and not (container and blob_name):
            return False

        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient.from_connection_string(conn)

        if blob_url:
            blob_client = service.get_blob_client(blob_url=blob_url)
        else:
            blob_client = service.get_blob_client(container=container, blob=blob_name)

        with open(xlsx_path, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)

        return True
        
    except Exception as e:
        logger.error(f"Error uploading phonebook to blob: {e}")
        return False


_phonebook_singleton: Optional[PhonebookLookup] = None


def _refresh_phonebook_in_background() -> None:
    global _phonebook_singleton, _last_blob_refresh, _phonebook_refresh_thread

    try:
        path = _download_phonebook_from_blob_to_cache(force_download=True) or ""
        if not path:
            return

        _last_blob_refresh = _time.time()

        if _phonebook_singleton is None or _phonebook_singleton.xlsx_path != path:
            _phonebook_singleton = PhonebookLookup(path)
            _phonebook_singleton.load()
        else:
            # Hot-swap: parse happens outside the lock, so live calls on the
            # main event loop never block behind this refresh.
            _phonebook_singleton.refresh()
        logger.info("[PHONEBOOK] Refreshed from blob storage")
    except Exception as exc:
        logger.warning(f"[PHONEBOOK] Background refresh failed: {exc}")
    finally:
        with _phonebook_refresh_lock:
            _phonebook_refresh_thread = None


def _start_background_phonebook_refresh() -> None:
    global _phonebook_refresh_thread
    with _phonebook_refresh_lock:
        if _phonebook_refresh_thread and _phonebook_refresh_thread.is_alive():
            return
        _phonebook_refresh_thread = threading.Thread(
            target=_refresh_phonebook_in_background,
            name="phonebook-refresh",
            daemon=True,
        )
        _phonebook_refresh_thread.start()


def get_phonebook_lookup() -> Optional[PhonebookLookup]:
    """Create a singleton phonebook lookup instance if enabled via env vars.

    Periodically re-downloads the phonebook from blob storage (default every 30 min)
    so that external edits are picked up without requiring a restart.

    Env:
      - ENABLE_INTERNAL_PHONEBOOK_LOOKUP=true
      - PHONEBOOK_XLSX_PATH=/path/to/xlsx
      - PHONEBOOK_BLOB_REFRESH_SECONDS=1800
    """
    global _phonebook_singleton, _last_blob_refresh

    enabled = (os.getenv("ENABLE_INTERNAL_PHONEBOOK_LOOKUP") or "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return None

    path = (os.getenv("PHONEBOOK_XLSX_PATH") or "").strip()

    # Check if it's time to refresh from blob
    now = _time.time()
    needs_blob_refresh = (not path) and (now - _last_blob_refresh >= _BLOB_REFRESH_INTERVAL)

    if not path:
        if _phonebook_singleton is not None and needs_blob_refresh:
            _start_background_phonebook_refresh()
            path = _phonebook_singleton.xlsx_path
        else:
            path = _download_phonebook_from_blob_to_cache(force_download=needs_blob_refresh) or ""
            if needs_blob_refresh and path:
                _last_blob_refresh = now
    if not path:
        return None

    if _phonebook_singleton is None or _phonebook_singleton.xlsx_path != path:
        _phonebook_singleton = PhonebookLookup(path)
    elif needs_blob_refresh:
        _start_background_phonebook_refresh()

    return _phonebook_singleton
