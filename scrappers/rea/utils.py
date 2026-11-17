"""Shared extraction helpers for both spiders.

Kept side-effect free and framework-agnostic so they unit-test without
a running crawler and so the later reconciliation / embedding scripts
can reuse the exact same normalization.
"""
import re

# Embedding API hard limit: at most 6 images per request. This is the
# single knob that controls the per-property media footprint.
MAX_IMAGE_URLS = 6

# Nawy descriptions arrive as HTML (<p>...</p> blocks). PF descriptions
# are plain text with \n separators. One stripper for both.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    """HTML -> plain text: tags out, entities decoded, whitespace sane."""
    if not html:
        return ""
    import html as html_mod

    text = _TAG_RE.sub("\n", html)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip(" \n\t")


def to_int(value, default=None):
    """Portal fields arrive as int, "3", 3.0 or None - normalize to int."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return int(float(text)) if text else default
        except ValueError:
            return default
    return default


def to_float(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe(urls):
    """First-occurrence dedup, preserving priority order, dropping junk."""
    seen = set()
    out = []
    for url in urls or []:
        if not url or not isinstance(url, str):
            continue
        url = url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def capped_image_urls(*priority_groups, max_images=MAX_IMAGE_URLS):
    """Merge URL groups into one capped list, earlier groups first.

    Called with (floor_plans, master_plan, photos) so that layout media
    (floor plans, then the compound master-plan map) always claim slots
    before brochure photos do.
    """
    merged = _dedupe(
        url for group in priority_groups for url in (group or [])
    )
    return merged[:max_images]


def geojson_centroid(geometry: dict):
    """Centroid (lat, lon) of a GeoJSON Polygon/MultiPolygon.

    Nawy exposes the compound as a polygon whose lat/lng fields are
    null; the polygon vertex average is a good-enough center for map
    placement and radius search at compound granularity. Returns
    (None, None) for anything unusable - callers pass it through.
    """
    if not isinstance(geometry, dict):
        return None, None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None, None

    if gtype == "Polygon":
        rings = coords
    elif gtype == "MultiPolygon":
        rings = [ring for poly in coords for ring in poly]
    else:
        return None, None

    lats, lons = [], []
    for ring in rings:
        for point in ring:
            # GeoJSON order is [longitude, latitude]
            lon, lat = point[0], point[1]
            if _is_plausible_egypt(lat, lon):
                lats.append(lat)
                lons.append(lon)
    if not lats:
        return None, None
    return sum(lats) / len(lats), sum(lons) / len(lons)


# Egypt's bounding box (incl. Sinai and the Red Sea coast), used to
# reject null-island / sentinel coordinates before they poison the map.
_EGYPT_LAT = (21.5, 31.8)
_EGYPT_LON = (24.5, 37.0)


def _is_plausible_egypt(lat, lon) -> bool:
    return (
        _EGYPT_LAT[0] <= lat <= _EGYPT_LAT[1]
        and _EGYPT_LON[0] <= lon <= _EGYPT_LON[1]
    )


def sanitize_coords(lat, lon):
    """Return (lat, lon) only if the pair is a plausible Egypt point."""
    lat, lon = to_float(lat), to_float(lon)
    if lat is None or lon is None:
        return None, None
    if not _is_plausible_egypt(lat, lon):
        return None, None
    return lat, lon
