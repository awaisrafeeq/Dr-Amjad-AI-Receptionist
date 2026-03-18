import os
import re
import tempfile
import hashlib
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any, List, Tuple

from dotenv import load_dotenv


load_dotenv()


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


def _download_phonebook_from_blob_to_cache() -> Optional[str]:
    blob_url = (os.getenv("PHONEBOOK_BLOB_URL") or "").strip()
    container = (os.getenv("PHONEBOOK_BLOB_CONTAINER") or "").strip()
    blob_name = (os.getenv("PHONEBOOK_BLOB_NAME") or "").strip()
    conn = (os.getenv("AZURE_BLOB_CONN") or "").strip()

    if not conn:
        return None

    if not blob_url and not (container and blob_name):
        return None

    force = (os.getenv("PHONEBOOK_BLOB_FORCE_REFRESH") or "").strip().lower() in {"1", "true", "yes"}

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
        self._index: Dict[str, PhonebookMatch] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return

        from openpyxl import load_workbook  # local import to avoid import cost if unused

        wb = load_workbook(self.xlsx_path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))

        header_idx = _find_header_row(rows)
        if header_idx is None:
            # If schema changes, we fail closed (no lookup) rather than guessing.
            self._loaded = True
            return

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
            )

            for raw in [match.phone, match.mobile]:
                for v in normalize_phone_variants(raw):
                    # Keep first match only to avoid accidental overwrites.
                    self._index.setdefault(v, match)

        self._loaded = True

    def lookup_by_phone(self, phone: Optional[str]) -> Optional[PhonebookMatch]:
        if not phone:
            return None

        self.load()

        for v in normalize_phone_variants(phone):
            m = self._index.get(v)
            if m is not None:
                return m

        return None


_phonebook_singleton: Optional[PhonebookLookup] = None


def get_phonebook_lookup() -> Optional[PhonebookLookup]:
    """Create a singleton phonebook lookup instance if enabled via env vars.

    Env:
      - ENABLE_INTERNAL_PHONEBOOK_LOOKUP=true
      - PHONEBOOK_XLSX_PATH=/path/to/xlsx
    """
    global _phonebook_singleton

    enabled = (os.getenv("ENABLE_INTERNAL_PHONEBOOK_LOOKUP") or "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        return None

    path = (os.getenv("PHONEBOOK_XLSX_PATH") or "").strip()
    if not path:
        path = _download_phonebook_from_blob_to_cache() or ""
    if not path:
        return None

    if _phonebook_singleton is None or _phonebook_singleton.xlsx_path != path:
        _phonebook_singleton = PhonebookLookup(path)

    return _phonebook_singleton
