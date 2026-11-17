"""Thread/process-safe unified JSONL store and real-time deduplicator.

Handles:
  - Cross-platform file locking so `nawy` and `property_finder` can run
    sequentially or concurrently against the same unified JSONL file.
  - Incremental tail-syncing from the unified file so in-memory indexes
    stay consistent across runs and concurrent processes.
  - Tier 1 (platform + source_id / source_url) and Tier 2 (cross-site &
    multi-broker physical/financial/location fingerprint) deduplication.
"""
from contextlib import contextmanager
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from rea import utils

logger = logging.getLogger(__name__)

_PUNCT_RE = re.compile(r"[^a-z0-9\u0600-\u06ff]+")
_WS_RE = re.compile(r"\s+")
_LEADING_INT_RE = re.compile(r"(\d+)")

_COMPOUND_NOISE = frozenset({
    "compound", "project", "residence", "residences", "the",
})

_TITLE_STOPWORDS = frozenset({
    "for", "sale", "rent", "in", "at", "by", "with", "and", "the", "of",
    "to", "a", "an", "on", "from", "bedroom", "bedrooms", "br", "bed",
    "beds", "bath", "baths", "bathroom", "bathrooms", "apartment", "villa",
    "townhouse", "twinhouse", "duplex", "penthouse", "chalet", "studio",
    "ivilla", "m2", "sqm", "egp", "new", "cairo", "giza", "egypt", "unit",
    "resale", "primary", "ready", "move", "delivery", "payment", "plan",
    "installments", "cash", "down", "luxury", "prime", "location", "view",
    "garden", "pool", "corner", "immediate",
})

_TYPE_ALIASES = {
    "apartment": "apartment",
    "apt": "apartment",
    "flat": "apartment",
    "studio": "studio",
    "duplex": "duplex",
    "penthouse": "penthouse",
    "roof": "penthouse",
    "villa": "villa",
    "standalone villa": "villa",
    "stand alone villa": "villa",
    "independent villa": "villa",
    "townhouse": "townhouse",
    "town house": "townhouse",
    "twinhouse": "twinhouse",
    "twin house": "twinhouse",
    "chalet": "chalet",
    "ivilla": "ivilla",
    "i villa": "ivilla",
    "hotel apartment": "hotel_apartment",
    "serviced apartment": "hotel_apartment",
    "cabin": "cabin",
    "loft": "loft",
    "land": "land",
    "plot": "land",
    "whole building": "building",
    "building": "building",
    "family house": "family_house",
}


@contextmanager
def file_lock(lock_path: str, timeout: float = 30.0):
    """Cross-platform advisory lock around the unified file."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()

            deadline = time.monotonic() + timeout
            while True:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for lock {lock_path}"
                        )
                    time.sleep(0.02)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _clean_text(value) -> str:
    if not value or not isinstance(value, str):
        return ""
    cleaned = _PUNCT_RE.sub(" ", value.lower())
    return _WS_RE.sub(" ", cleaned).strip()


def normalize_url(url) -> str:
    """Normalize listing URL for exact identity; skip generic search URLs."""
    if not url or not isinstance(url, str):
        return ""
    parts = urlsplit(url.strip())
    if not parts.netloc or not parts.path:
        return ""
    path = parts.path.rstrip("/").lower()
    if not path or path.endswith("/search"):
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{path}"


def normalize_bedrooms(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return None
        if "studio" in s:
            return 0
        m = _LEADING_INT_RE.search(s)
        if m:
            return int(m.group(1))
    return None


def normalize_property_type(ptype, bedrooms=None) -> str:
    cleaned = _clean_text(ptype)
    if not cleaned:
        return ""
    norm = _TYPE_ALIASES.get(cleaned, cleaned)
    if norm == "apartment" and bedrooms == 0:
        return "studio"
    return norm


def normalize_compound(value) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    tokens = [t for t in cleaned.split() if t not in _COMPOUND_NOISE]
    return " ".join(tokens) if tokens else cleaned


def normalize_city(value) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    if cleaned.endswith(" city") and len(cleaned) > 5:
        cleaned = cleaned[:-5].strip()
    return cleaned


def _title_tokens(title_norm: str) -> frozenset:
    if not title_norm:
        return frozenset()
    return frozenset(
        tok for tok in title_norm.split()
        if len(tok) >= 2 and tok not in _TITLE_STOPWORDS
    )


class _IndexedEntry:
    __slots__ = (
        "label",
        "platform",
        "ptype",
        "bedrooms",
        "bathrooms",
        "size",
        "price",
        "compound",
        "city",
        "governorate",
        "title_norm",
        "title_tokens",
    )

    def __init__(self, record: dict):
        platform = str(record.get("platform") or "").strip()
        source_id = record.get("source_id")
        self.label = (
            f"{platform}#{source_id}" if source_id is not None else platform or "unknown"
        )
        self.platform = platform.lower()

        raw_beds = normalize_bedrooms(record.get("bedrooms"))
        self.ptype = normalize_property_type(
            record.get("property_type"), bedrooms=raw_beds
        )
        # Studios occasionally arrive with bedrooms=1 on one portal and 0 on another
        self.bedrooms = 0 if self.ptype == "studio" else raw_beds
        self.bathrooms = normalize_bedrooms(record.get("bathrooms"))
        self.size = utils.to_float(record.get("size_m2"))
        self.price = utils.to_float(record.get("price_egp"))

        self.compound = normalize_compound(record.get("compound"))
        self.city = normalize_city(record.get("city"))
        self.governorate = normalize_city(record.get("governorate"))
        self.title_norm = _clean_text(record.get("title_raw"))
        self.title_tokens = _title_tokens(self.title_norm)


def _cities_compatible(a: _IndexedEntry, b: _IndexedEntry) -> bool:
    if not a.city or not b.city:
        return True
    if a.city == b.city or a.city in b.city or b.city in a.city:
        return True
    if a.governorate and (a.governorate == b.city or a.governorate in b.city):
        return True
    if b.governorate and (b.governorate == a.city or b.governorate in a.city):
        return True
    return False


def _location_matches(a: _IndexedEntry, b: _IndexedEntry) -> bool:
    # Case 1: both carry a compound/district string
    if a.compound and b.compound:
        if a.compound == b.compound:
            return True
        if min(len(a.compound), len(b.compound)) >= 4 and (
            a.compound in b.compound or b.compound in a.compound
        ):
            return True
        # Handle one portal putting district in `compound` and compound in `title_raw`
        if _cities_compatible(a, b) and (
            (len(a.compound) >= 4 and a.compound in b.title_norm)
            or (len(b.compound) >= 4 and b.compound in a.title_norm)
        ):
            return True
        return False

    # Case 2: only one carries `compound` -> verify it appears in the other's title
    if a.compound or b.compound:
        comp = a.compound or b.compound
        other_title = b.title_norm if a.compound else a.title_norm
        return (
            len(comp) >= 4
            and comp in other_title
            and _cities_compatible(a, b)
        )

    # Case 3: neither carries `compound` -> require city match + shared title token
    if not (a.city and b.city and _cities_compatible(a, b)):
        return False
    return bool(a.title_tokens & b.title_tokens)


def _specs_match(cand: _IndexedEntry, existing: _IndexedEntry) -> bool:
    if cand.size is None or existing.size is None or cand.size <= 0 or existing.size <= 0:
        return False
    if cand.price is None or existing.price is None or cand.price <= 0 or existing.price <= 0:
        return False

    both_lack_compound = not cand.compound and not existing.compound
    size_tol = 1.0 if both_lack_compound else max(2.0, 0.02 * max(cand.size, existing.size))
    price_tol = (
        0.005 * max(cand.price, existing.price)
        if both_lack_compound
        else 0.02 * max(cand.price, existing.price)
    )

    if abs(cand.size - existing.size) > size_tol:
        return False
    if abs(cand.price - existing.price) > price_tol:
        return False

    if (
        cand.bedrooms is not None
        and existing.bedrooms is not None
        and cand.bedrooms != existing.bedrooms
    ):
        return False
    if (
        cand.bathrooms is not None
        and existing.bathrooms is not None
        and cand.bathrooms != existing.bathrooms
    ):
        return False

    return _location_matches(cand, existing)


class DeduplicationIndex:
    """In-memory index over all listings already in the unified output."""

    def __init__(self):
        self.total_count = 0
        self.platform_ids = {}     # (platform_lower, str(source_id)) -> label
        self.ids_by_platform = {}  # platform_lower -> set[str]
        self.urls = {}             # norm_url -> label
        self.buckets = {}          # (ptype, bedrooms) -> list[_IndexedEntry]

    def clear(self):
        self.total_count = 0
        self.platform_ids.clear()
        self.ids_by_platform.clear()
        self.urls.clear()
        self.buckets.clear()

    def add(self, record: dict):
        entry = _IndexedEntry(record)
        self.total_count += 1

        source_id = record.get("source_id")
        if entry.platform and source_id is not None:
            sid = str(source_id)
            self.platform_ids[(entry.platform, sid)] = entry.label
            self.ids_by_platform.setdefault(entry.platform, set()).add(sid)

        norm_url = normalize_url(record.get("source_url"))
        if norm_url:
            self.urls[norm_url] = entry.label

        if entry.ptype and entry.size and entry.price:
            self.buckets.setdefault((entry.ptype, entry.bedrooms), []).append(entry)

    def find_duplicate(self, record: dict):
        """Return a short reason string if `record` duplicates an indexed item."""
        platform = str(record.get("platform") or "").strip().lower()
        source_id = record.get("source_id")
        if platform and source_id is not None:
            existing_label = self.platform_ids.get((platform, str(source_id)))
            if existing_label is not None:
                return f"platform_id:{existing_label}"

        norm_url = normalize_url(record.get("source_url"))
        if norm_url:
            existing_label = self.urls.get(norm_url)
            if existing_label is not None:
                return f"source_url:{existing_label}"

        cand = _IndexedEntry(record)
        if not cand.ptype or cand.size is None or cand.price is None:
            return None

        if cand.bedrooms is not None:
            candidate_lists = (
                self.buckets.get((cand.ptype, cand.bedrooms), ()),
                self.buckets.get((cand.ptype, None), ()),
            )
        else:
            candidate_lists = tuple(
                entries
                for (ptype, _), entries in self.buckets.items()
                if ptype == cand.ptype
            )

        for entries in candidate_lists:
            for existing in entries:
                if _specs_match(cand, existing):
                    return f"spec_match:{existing.label}"

        return None


class UnifiedListingStore:
    """Append-only JSONL store with locked deduplication and incremental sync."""

    def __init__(self, output_path: str, lock_path: str = None):
        self.output_path = output_path
        self.lock_path = lock_path or f"{output_path}.lock"
        self.index = DeduplicationIndex()
        self._file_offset = 0

    def reset(self):
        """Delete any existing unified output file (used with reset_output=1)."""
        with file_lock(self.lock_path):
            if os.path.exists(self.output_path):
                os.remove(self.output_path)
            self.index.clear()
            self._file_offset = 0

    def sync(self):
        """Load any lines written since `_file_offset`."""
        with file_lock(self.lock_path):
            self._sync_locked()

    def existing_ids_for_platform(self, platform: str) -> set:
        return set(self.index.ids_by_platform.get(platform.lower(), ()))

    def add_if_unique(self, record: dict):
        """Atomically check for duplicates and append if unique.

        Returns ``(added: bool, reason: str | None)``.
        """
        with file_lock(self.lock_path):
            self._sync_locked()
            dup_reason = self.index.find_duplicate(record)
            if dup_reason is not None:
                return False, dup_reason

            os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            with open(self.output_path, "ab") as fh:
                fh.write(line)
                fh.flush()
            self._file_offset = os.path.getsize(self.output_path)
            self.index.add(record)
            return True, None

    def _sync_locked(self):
        if not os.path.exists(self.output_path):
            if self._file_offset > 0:
                self.index.clear()
                self._file_offset = 0
            return

        size = os.path.getsize(self.output_path)
        if size < self._file_offset:
            self.index.clear()
            self._file_offset = 0

        if size == self._file_offset:
            return

        with open(self.output_path, "rb") as fh:
            fh.seek(self._file_offset)
            while True:
                line_bytes = fh.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping malformed line in %s: %s",
                        self.output_path,
                        exc,
                    )
                    continue
                if isinstance(record, dict):
                    self.index.add(record)
            self._file_offset = fh.tell()