"""Field completion — Stage 6: regex first, LLM second.

The EDA measured the gap: 3,860 PropertyFinder finishing nulls, 3,089
delivery-year nulls and 3,171 payment-term gaps whose descriptions contain an
extractable cue. A deterministic regex tier runs first — cheap, testable,
reproducible — and only the remainder ever goes to Groq: ``gpt-oss-120b``
first, then ``qwen/qwen3.8-27b`` once the primary's daily token budget is
spent (each model carries its own free-tier bucket — 8K tokens per minute,
200K per day). Requests are batched 5 per call against a strict JSON
schema, results cached by ``text_hash`` so the frozen dataset pays for
enrichment exactly once; at most ~1,300 requests worst case, a night or
two per model inside the per-model budgets.

Rules that hold for both tiers:

* only fill fields that are ``None`` — portal-provided values are never
  overwritten;
* the 2,786 reduced-detail PropertyFinder rows (``source_tier_card``) have
  no description and are excluded by construction;
* every fill records its provenance in ``extraction`` as
  ``{"<field>": {"value": ..., "source": "regex" | "llm"}}``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from src.core.exceptions import LLMAuthError, LLMError, LLMRateLimitError
from src.core.logging import get_logger
from src.etl.vocab import UnmappedValueError, map_finishing

if TYPE_CHECKING:  # pragma: no cover
    from src.etl.contracts import CleanListing

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Regex tier
# ---------------------------------------------------------------------------

# Finishing cues — most specific first; Arabic forms are post-arabic.py
# normalization (tashkeel stripped, digits transliterated).
_FINISHING_PATTERNS: list[tuple[str, str]] = [
    ("finished_furnished", r"fully\s+finished(?:\s+and\s+|\s*[,/-]\s*|\s+)furnished|finished\s+and\s+furnished"),
    ("finished_ultra", r"ultra\s*[-\s]?lux"),
    ("finished", r"fully\s+finished|super\s+lux|سوبر\s*لوكس|تشطيب\s*كامل|متشطب"),
    ("semi_finished", r"semi[\s-]*finished|نص\s*تشطيب|نصف\s*تشطيب|تشطيب\s*نص"),
    ("core_and_shell", r"core\s*(?:&|and)?\s*shell|on\s+the\s+core|red\s+brick|على\s*الطوب|طوب\s*احمر|بدون\s*تشطيب"),
    ("white_box", r"white\s*box"),
    ("flexi", r"flexi"),
]

# Delivery-year cues: verb-then-year and year-then-verb, both languages.
_DELIVERY_YEAR_PATTERNS = [
    re.compile(r"(?:deliver(?:y|ed|s)?|handover|استلام|تسليم)[^.\n]{0,40}?(20[2-3]\d)", re.IGNORECASE),
    re.compile(r"(20[2-3]\d)[^.\n]{0,30}?(?:deliver|handover|استلام|تسليم)", re.IGNORECASE),
]

_DELIVERY_HINT = re.compile(r"ready\s+to\s+move|جاهز\s*للسكن", re.IGNORECASE)

# Payment cues (amounts already digit-normalized by arabic.py; Arabic word
# numerals like "مليون" may still trail the digits). Two Arabic patterns:
# digits-then-optional-scale first, then the bare-scale form ("مقدم مليون").
_DOWN_PAYMENT_EN = re.compile(
    r"(?:down|first)\s*payment[^0-9\n]{0,15}([\d][\d,.]*)\s*(m\b|mn\b|million)?",
    re.IGNORECASE,
)
_DOWN_PAYMENT_AR_DIGITS = re.compile(
    r"مقدم[^0-9\n]{0,15}([\d][\d,.]*)\s*(مليون|الف|مليار)?"
)
_DOWN_PAYMENT_AR_SCALE = re.compile(r"مقدم\s*(مليون|الف|مليار)\b")
_ARABIC_SCALES = {
    "مليون": Decimal("1000000"),
    "الف": Decimal("1000"),
    "مليار": Decimal("1000000000"),
}
_INSTALLMENT_YEARS = re.compile(
    r"(?:over|in)\s+(\d{1,2})\s+years?|(\d{1,2})\s+years?[^.\n]{0,25}?(?:installment|plan)|"
    r"تقسيط[^.\n]{0,30}?(\d{1,2})\s*(?:سنة|سنوات|عام)",
    re.IGNORECASE,
)

_YEARS_MIN, _YEARS_MAX = 2022, 2035
_TENOR_MIN, _TENOR_MAX = 1, 15


def _parse_amount(raw: str, scale: str | None) -> Decimal | None:
    """'5,500,000' / '5.5 m' / '2 مليون' → Decimal EGP."""
    if raw is None:
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except Exception:  # noqa: BLE001 - malformed capture, not a crash
        return None
    if scale:
        value *= Decimal("1000000") if scale.lower().startswith(("m",)) else Decimal("1000")
    return value


def _parse_arabic_amount(raw: str | None, scale: str | None) -> Decimal | None:
    """'2 مليون' → 2,000,000; bare 'مليون' (no digits) → 1,000,000."""
    if raw is None:
        return _ARABIC_SCALES[scale] if scale else None
    try:
        value = Decimal(raw.replace(",", ""))
    except Exception:  # noqa: BLE001 - malformed capture, not a crash
        return None
    if scale:
        value *= _ARABIC_SCALES[scale]
    return value if value > 0 else None


def extract_finishing(text: str) -> str | None:
    """Canonical finishing level implied by the text, if any."""
    for canonical, pattern in _FINISHING_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return canonical
    return None


def extract_delivery_year(text: str) -> int | None:
    for pattern in _DELIVERY_YEAR_PATTERNS:
        match = pattern.search(text)
        if match:
            year = int(match.group(1))
            if _YEARS_MIN <= year <= _YEARS_MAX:
                return year
    return None


def extract_payment(text: str) -> tuple[Decimal | None, int | None]:
    """(down_payment_egp, installment_years) implied by the text."""
    down: Decimal | None = None
    match = _DOWN_PAYMENT_EN.search(text)
    if match:
        down = _parse_amount(match.group(1), match.group(2))
    else:
        match = _DOWN_PAYMENT_AR_DIGITS.search(text)
        if match:
            down = _parse_arabic_amount(match.group(1), match.group(2))
        else:
            match = _DOWN_PAYMENT_AR_SCALE.search(text)
            if match:
                down = _parse_arabic_amount(None, match.group(1))
    years: int | None = None
    match = _INSTALLMENT_YEARS.search(text)
    if match:
        for group in match.groups():
            if group and group.isdigit() and _TENOR_MIN <= int(group) <= _TENOR_MAX:
                years = int(group)
                break
    if down is not None:
        down = down.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return down, years


def enrich_missing(listing: CleanListing) -> CleanListing:
    """Regex tier: fill missing finishing / delivery / payment from text.

    Only applies when a description exists (card-tier rows are excluded by
    construction) and the structured field is ``None``.
    """
    if not listing.description_normalized:
        return listing
    text = f"{listing.title_normalized}\n{listing.description_normalized}"
    updates: dict[str, Any] = {}
    extraction: dict[str, Any] = dict(listing.extraction)

    if listing.finishing is None:
        found = extract_finishing(text)
        if found is not None:
            try:
                canonical = map_finishing(found)  # canonical already; sanity
            except UnmappedValueError:  # pragma: no cover - patterns emit canonical
                canonical = found
            updates["finishing"] = canonical
            extraction["finishing"] = {"value": canonical, "source": "regex"}

    if listing.delivery_year is None:
        year = extract_delivery_year(text)
        if year is not None:
            updates["delivery_year"] = year
            extraction["delivery_year"] = {"value": year, "source": "regex"}

    down, years = extract_payment(text)
    if listing.down_payment_egp is None and down is not None:
        updates["down_payment_egp"] = down
        extraction["down_payment_egp"] = {"value": str(down), "source": "regex"}
    if listing.installment_years is None and years is not None:
        updates["installment_years"] = years
        extraction["installment_years"] = {"value": years, "source": "regex"}
    if listing.payment_scheme is None and (years is not None or down is not None):
        updates["payment_scheme"] = "installments"
        extraction["payment_scheme"] = {"value": "installments", "source": "regex"}

    if not updates:
        return listing
    updates["extraction"] = extraction
    return listing.model_copy(update=updates)


# ---------------------------------------------------------------------------
# LLM tier (run on demand: `python -m src.etl.pipeline --llm`)
# ---------------------------------------------------------------------------

LLM_PROMPT_TEMPLATE = """You extract structured real-estate facts from Egyptian property listings.
For EACH listing below, return ONLY the fields you can derive with confidence from its text:
finishing (one of core_and_shell, semi_finished, finished, finished_furnished, finished_ultra, flexi, white_box),
delivery_year (int 2022-2035), down_payment_egp (int), installment_years (int 1-15).
Use null when the text does not state a value. Never guess.
Respond with a JSON array, one object per listing, in order: [{{"i": 1, "finishing": ..., "delivery_year": ..., "down_payment_egp": ..., "installment_years": ...}}, ...]

Listings:
{batch}
"""

_LLM_MAX_TOKENS = 1536


def build_llm_batch(listings: list[CleanListing]) -> str:
    """Render a batch of numbered texts for the LLM prompt."""
    lines = []
    for i, listing in enumerate(listings, start=1):
        text = f"{listing.title_normalized}. {listing.description_normalized}"
        lines.append(f"--- listing {i} ---\n{text[:1500]}")
    return "\n".join(lines)


def _is_strict_int(value: Any) -> TypeGuard[int]:
    """True for real ints — bool is an int subclass and must not pass."""
    return isinstance(value, int) and not isinstance(value, bool)


def _responded_hashes(payload: Any, batch: list[CleanListing]) -> set[str]:
    """Return text_hashes for listings that received a valid object in payload."""
    if not isinstance(payload, list):
        return set()
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or not _is_strict_int(item.get("i")):
            continue
        idx = item["i"] - 1
        if 0 <= idx < len(batch):
            seen.add(batch[idx].text_hash)
    return seen


def parse_llm_payload(payload: Any, batch: list[CleanListing]) -> dict[str, dict[str, Any]]:
    """Validate a parsed LLM response (JSON array) against the batch.

    Returns ``{text_hash: {field: value}}`` for valid extractions only —
    invalid rows or out-of-range values are dropped (null-on-doubt policy).
    """
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict) or not _is_strict_int(item.get("i")):
            continue
        idx = item["i"] - 1
        if not 0 <= idx < len(batch):
            continue
        listing = batch[idx]
        fields: dict[str, Any] = {}
        finishing = item.get("finishing")
        if isinstance(finishing, str) and finishing in {
            "core_and_shell", "semi_finished", "finished", "finished_furnished",
            "finished_ultra", "flexi", "white_box",
        }:
            fields["finishing"] = finishing
        year = item.get("delivery_year")
        if _is_strict_int(year) and _YEARS_MIN <= year <= _YEARS_MAX:
            fields["delivery_year"] = year
        dp = item.get("down_payment_egp")
        if _is_strict_int(dp) and dp >= 0:
            fields["down_payment_egp"] = Decimal(dp)
        years = item.get("installment_years")
        if _is_strict_int(years) and _TENOR_MIN <= years <= _TENOR_MAX:
            fields["installment_years"] = years
        if fields:
            out[listing.text_hash] = fields
    return out


class ExtractionCache:
    """Persistent text_hash → extraction cache (JSON file, merged per run)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except json.JSONDecodeError:
                self._data = {}

    def get(self, text_hash: str) -> dict[str, Any] | None:
        return self._data.get(text_hash)

    def put(self, text_hash: str, fields: dict[str, Any]) -> None:
        self._data[text_hash] = {k: str(v) for k, v in fields.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._data)


# --- LLM tier run policy ------------------------------------------------------
# Groq's free tier bills each model separately (30 RPM, 8K tokens per
# minute, 200K per day). Two mechanisms cooperate:
#
# * proactive pacing — a per-model rolling 60s token window (fed by each
#   response's ``usage.total_tokens``) sleeps out the rest of the minute
#   when the next batch would not fit the 8K TPM budget, and a per-model
#   day counter switches to the fallback model at the daily budget;
# * reactive backoff — a 429 still arrives when estimates are off: a
#   Retry-After within the minute ceiling is a per-minute limit (RPM blip
#   or token window) and is awaited — a full minute for token-window hits;
#   a longer one means the model's daily budget is gone → switch model,
#   or stop gracefully once both are spent.
RATE_LIMIT_MAX_WAITS_PER_BATCH = 10
RATE_LIMIT_WAIT_BUFFER_SECONDS = 0.5
MINUTE_LIMIT_RETRY_AFTER_SECONDS = 90.0  # longer Retry-After ⇒ daily budget
MAX_CONSECUTIVE_BATCH_FAILURES = 5
DEFAULT_TOKEN_ESTIMATE = 2_000  # until the first real usage arrives
MIN_TOKEN_ESTIMATE = 1_000


@dataclass
class _ModelBudget:
    """Per-model Groq token accounting: rolling 60s window + day total."""

    tpm_limit: int
    tpd_limit: int
    minute_window: deque[tuple[float, int]] = field(default_factory=deque)
    day_tokens: int = 0
    estimate: int = DEFAULT_TOKEN_ESTIMATE

    def record(self, tokens: int, now: float) -> None:
        self.minute_window.append((now, tokens))
        self.day_tokens += tokens
        self.estimate = max(MIN_TOKEN_ESTIMATE, tokens)

    def _prune(self, now: float) -> None:
        while self.minute_window and self.minute_window[0][0] <= now - 60.0:
            self.minute_window.popleft()

    def pace_seconds(self, now: float) -> float:
        """Sleep needed before the next request fits the minute window.

        Returns 0.0 when it already fits; otherwise the seconds until
        enough recorded tokens have aged out of the 60s window (bounded
        by a full minute).
        """
        self._prune(now)
        window_total = sum(tokens for _, tokens in self.minute_window)
        deficit = window_total + self.estimate - self.tpm_limit
        if deficit <= 0:
            return 0.0
        freed = 0
        for stamp, tokens in self.minute_window:
            freed += tokens
            if freed >= deficit:
                return min(60.0, max(0.0, stamp + 60.0 - now))
        return 60.0  # the estimate alone exceeds the limit: fresh minute

    @property
    def minute_tokens(self) -> int:
        return sum(tokens for _, tokens in self.minute_window)

    @property
    def day_exhausted(self) -> bool:
        return self.day_tokens >= self.tpd_limit


async def _sleep(seconds: float) -> None:
    """Indirection so tests can fast-forward cooldowns and pacing waits."""
    await asyncio.sleep(seconds)


async def _chat_tolerant(
    client: Any, prompt: str, budget: _ModelBudget
) -> tuple[Any, str | None, int]:
    """One chat call that awaits per-minute cooldowns instead of crashing.

    Returns ``(response, outcome, rate_limit_waits)``. ``outcome`` is
    ``None`` on success, else one of:

    * ``"daily_exhausted"`` — this model's daily token budget is gone (a
      day-scale Retry-After, or our own day counter): the caller switches
      model;
    * ``"auth_failed"`` — bad API key: retrying cannot fix it, and both
      models share the key, so no fallback either;
    * ``"stalled"`` — too many per-minute cooldowns on one batch.
    """
    waits = 0
    chat_kwargs: dict[str, Any] = {"temperature": 0.0}
    try:
        params = inspect.signature(client.chat).parameters
        if "max_tokens" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            chat_kwargs["max_tokens"] = _LLM_MAX_TOKENS
    except (TypeError, ValueError):
        pass

    while True:
        try:
            response = await client.chat(
                messages=[{"role": "user", "content": prompt}], **chat_kwargs
            )
            return response, None, waits
        except LLMRateLimitError as error:
            cooldown = (
                float(error.retry_after) if error.retry_after is not None else 60.0
            )
            if cooldown > MINUTE_LIMIT_RETRY_AFTER_SECONDS or budget.day_exhausted:
                return None, "daily_exhausted", waits
            if waits >= RATE_LIMIT_MAX_WAITS_PER_BATCH:
                return None, "stalled", waits
            waits += 1
            # RPM blips honor the short hint; a token-window hit waits out
            # the full minute (Groq resets TPM per 60s window).
            wait_for = max(60.0, cooldown) if cooldown > 10.0 else cooldown
            logger.info(
                "llm.rate_limit_wait",
                provider=getattr(client, "provider", "groq"),
                model=getattr(client, "default_model", "unknown"),
                wait_seconds=wait_for,
                attempt=waits,
            )
            await _sleep(wait_for + RATE_LIMIT_WAIT_BUFFER_SECONDS)
        except LLMAuthError:
            return None, "auth_failed", waits


async def run_llm_extraction(
    listings: list[CleanListing],
    cache_path: Path,
    settings: Any,  # Settings — typed loosely to keep the module import-light
    batch_size: int | None = None,
    max_requests: int = 1000,
    client: Any | None = None,
    fallback_client: Any | None = None,
) -> dict[str, Any]:
    """LLM tier: batch the remaining gaps through Groq, cache by text hash.

    Requires ``GROQ_API_KEY`` (or injected ``client`` / ``fallback_client``).
    Groq bills each model separately (8K tokens per minute, 200K per day),
    so the run starts on ``settings.groq_model`` and switches to
    ``settings.groq_fallback_model`` the moment the primary's daily token
    budget is spent. A rolling-minute pacer keeps batches inside the TPM
    window; reactive 429s are awaited (a full minute for token-window
    hits). When both models' day budgets are gone — or the key fails, or
    errors persist — the tier stops *gracefully*: everything extracted so
    far is in the cache, the pipeline continues with it, and re-running
    the command resumes from where it stopped (budgets reset at midnight
    UTC).

    Returns ``{"fills", "batches_done", "batches_total", "requests",
    "rate_limit_waits", "tpm_pace_waits", "models", "stopped_reason"}``.
    """
    cache = ExtractionCache(cache_path)
    if client is None:
        from src.services.llm_client import LLMClient, make_llm_clients

        client = make_llm_clients(settings)["groq"]
        if fallback_client is None:
            # Same key and host as the primary; only the model and the
            # quota entry differ (Groq limits are per model, so an
            # exhausted primary must not fail-fast a fresh fallback).
            api_key = settings.groq_api_key
            fallback_client = LLMClient(
                provider="groq",
                base_url=settings.groq_base_url,
                api_key=api_key.get_secret_value() if api_key else "",
                default_model=settings.groq_fallback_model,
                settings=settings,
                quota_key=f"groq:{settings.groq_fallback_model}",
            )
    size = batch_size or settings.etl_groq_batch_size
    tpm_limit = getattr(settings, "etl_groq_minute_token_budget", 7_500)
    tpd_limit = getattr(settings, "etl_groq_daily_token_budget", 180_000)

    pending: list[CleanListing] = []
    seen_hashes: set[str] = set()
    for listing in listings:
        if not listing.description_normalized:
            continue
        if listing.text_hash in seen_hashes:
            continue
        if cache.get(listing.text_hash) is not None:
            continue
        if listing.finishing is None or listing.delivery_year is None or (
            listing.payment_scheme is None
        ):
            seen_hashes.add(listing.text_hash)
            pending.append(listing)

    fills = {"finishing": 0, "delivery_year": 0, "down_payment_egp": 0, "installment_years": 0}
    total_batches = (len(pending) + size - 1) // size
    requests = 0
    rate_limit_waits = 0
    tpm_pace_waits = 0
    consecutive_failures = 0
    batches_done = 0
    stopped_reason: str | None = None

    def _model_name(c: Any) -> str:
        # Client label for logs/stats: its model if known, else provider.
        return str(getattr(c, "default_model", None) or getattr(c, "provider", "llm"))

    model_stats: dict[str, dict[str, int]] = {
        _model_name(client): {"requests": 0, "tokens": 0}
    }
    active_client = client
    active_budget = _ModelBudget(tpm_limit=tpm_limit, tpd_limit=tpd_limit)
    fallback_active = False

    def _switch_to_fallback() -> bool:
        nonlocal active_client, active_budget, fallback_active, consecutive_failures
        if fallback_client is None or fallback_active:
            return False
        fallback_active = True
        active_client = fallback_client
        active_budget = _ModelBudget(tpm_limit=tpm_limit, tpd_limit=tpd_limit)
        consecutive_failures = 0
        model_stats.setdefault(_model_name(fallback_client), {"requests": 0, "tokens": 0})
        logger.info(
            "llm.model_fallback",
            from_model=_model_name(client),
            to_model=_model_name(fallback_client),
            reason="daily token budget exhausted",
        )
        return True

    i = 0
    while i < len(pending):
        if requests >= max_requests:
            stopped_reason = f"request budget of {max_requests} reached"
            break

        # Proactive daily switch: our token counter usually knows before
        # Groq's 429 does.
        if active_budget.day_exhausted and not _switch_to_fallback():
            stopped_reason = (
                "daily token budget exhausted on all models"
                if fallback_active
                else "daily token budget exhausted and no fallback model available"
            )
            break

        batch = pending[i : i + size]
        prompt = LLM_PROMPT_TEMPLATE.format(batch=build_llm_batch(batch))

        # Proactive TPM pacing: wait out the rolling minute window when the
        # next batch would not fit the 8K budget.
        pace = active_budget.pace_seconds(time.monotonic())
        if pace > 0:
            tpm_pace_waits += 1
            logger.info(
                "llm.tpm_pace",
                model=_model_name(active_client),
                wait_seconds=round(pace, 1),
                window_tokens=active_budget.minute_tokens,
            )
            await _sleep(pace + RATE_LIMIT_WAIT_BUFFER_SECONDS)

        try:
            response, outcome, waits = await _chat_tolerant(
                active_client, prompt, active_budget
            )
            rate_limit_waits += waits
            if outcome == "daily_exhausted":
                # Day-scale cooldown: switch models and retry the same
                # batch, or stop gracefully when none is left.
                if _switch_to_fallback():
                    continue
                stopped_reason = (
                    "daily token budget exhausted on all models"
                    if fallback_active
                    else "daily token budget exhausted and no fallback model available"
                )
                break
            if outcome == "auth_failed":
                stopped_reason = "authentication failed (check GROQ_API_KEY)"
                break
            if outcome == "stalled":
                stopped_reason = (
                    f"{RATE_LIMIT_MAX_WAITS_PER_BATCH} rate-limit waits on one batch"
                )
                break
            requests += 1
            payload = response.json()
        except LLMError:
            # Transport error after the client's internal retries, or an
            # unparseable response: skip the batch, keep the run alive.
            consecutive_failures += 1
            logger.warning(
                "llm.batch_failed", batch=batches_done, consecutive=consecutive_failures
            )
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                stopped_reason = f"{MAX_CONSECUTIVE_BATCH_FAILURES} consecutive batch failures"
                break
            i += size
            continue

        # Record what the request cost into the per-model budgets.
        usage = getattr(response, "usage", None)
        tokens = (
            usage.get("total_tokens")
            or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            if isinstance(usage, dict)
            else 0
        ) or active_budget.estimate
        active_budget.record(tokens, time.monotonic())
        stats = model_stats[_model_name(active_client)]
        stats["requests"] += 1
        stats["tokens"] += tokens

        parsed = parse_llm_payload(payload, batch)
        responded = _responded_hashes(payload, batch)
        consecutive_failures = 0
        for listing in batch:
            fields = parsed.get(listing.text_hash)
            if fields:
                cache.put(listing.text_hash, fields)
                for key in fields:
                    if key in fills:
                        fills[key] += 1
            elif listing.text_hash in responded:
                cache.put(listing.text_hash, {})
        cache.save()
        batches_done += 1
        i += size

    return {
        "fills": fills,
        "batches_done": batches_done,
        "batches_total": total_batches,
        "requests": requests,
        "rate_limit_waits": rate_limit_waits,
        "tpm_pace_waits": tpm_pace_waits,
        "models": model_stats,
        "stopped_reason": stopped_reason,
    }


__all__ = [
    "ExtractionCache",
    "LLM_PROMPT_TEMPLATE",
    "build_llm_batch",
    "enrich_missing",
    "extract_delivery_year",
    "extract_finishing",
    "extract_payment",
    "parse_llm_payload",
    "run_llm_extraction",
]
