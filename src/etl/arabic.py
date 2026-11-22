"""Bilingual text hygiene — Stage 2, the single normalization entry point.

One module normalizes every free-text field (title, description, compound and
feature names) once, upstream, so full-text search, deduplication trigrams and
later embeddings all consume the same canonical form — the architecture
document's ``etl/arabic.py`` commitment.

Rule set (each measured in the EDA / phase-plan defect register):

* Arabic-Indic digit transliteration (42 rows carry U+0660-U+0669);
* tashkeel + tatweel stripping (189 rows);
* letter-form unification (alef variants -> bare alef, alef-maksura -> ya);
* Arabic word-numeral conversion ("خمسة مليون" -> "5000000") — best effort,
  targeted at payment sentences Stage 6 parses;
* redaction of noise that poisons search and embeddings: the 771
  ``[contact details hidden]`` placeholders, 133 hashtags, 7 phone numbers
  and stray URLs;
* whitespace collapse (3,611 padded feature strings, 34 compound names).

The raw payload is never modified — hygiene output lands in
``title_normalized`` / ``description_normalized`` and registry keys.
"""

from __future__ import annotations

import re

# --- character classes (mirrors scripts/explore_data.py so EDA counts and
# --- pipeline actions stay comparable) --------------------------------------
ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

RE_ARABIC = re.compile(r"[\u0600-\u06FF]")
RE_ARABIC_DIGITS = re.compile(r"[\u0660-\u0669\u06F0-\u06F9]")
RE_TASHKEEL_TATWEEL = re.compile(r"[\u0640\u064B-\u065F\u0670]")
RE_ALEF_VARIANTS = re.compile("[\u0623\u0625\u0622\u0671]")  # أ إ آ ٱ
RE_ALEF_MAKSURA = re.compile("\u0649")  # ى
RE_CONTACT_HIDDEN = re.compile(r"\[contact details hidden\]", re.IGNORECASE)
RE_HASHTAG = re.compile(r"#[\w\u0600-\u06FF]+")
RE_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
RE_PHONE = re.compile(r"(?:\+?20|0)?1[0125]\d[\s\-]?\d{3,4}[\s\-]?\d{4}")
RE_WHITESPACE = re.compile(r"\s+")


def transliterate_digits(text: str) -> str:
    """Arabic-Indic digits (both ranges) -> ASCII digits."""
    return text.translate(ARABIC_INDIC_DIGITS)


def strip_diacritics(text: str) -> str:
    """Remove tashkeel (harakat) and tatweel (kashida)."""
    return RE_TASHKEEL_TATWEEL.sub("", text)


def normalize_letters(text: str) -> str:
    """Unify Arabic letter forms: alef variants -> ا, ى -> ي."""
    text = RE_ALEF_VARIANTS.sub("\u0627", text)
    return RE_ALEF_MAKSURA.sub("\u064A", text)


# --- Arabic word numerals ----------------------------------------------------
# Post-normalization forms only (normalize_letters runs first). Values follow
# the listing dialect: units, tens, hundred and the three scales.
_NUMBER_WORDS: dict[str, int] = {
    "صفر": 0,
    "واحدة": 1, "واحد": 1, "احدة": 1, "حد": 1,
    "اتنين": 2, "اثنين": 2, "اتنان": 2, "اثنان": 2,
    "تلاتة": 3, "ثلاثة": 3, "تلات": 3,
    "اربعة": 4, "اربع": 4,
    "خمسة": 5, "خمس": 5,
    "ستة": 6, "ست": 6,
    "سبعة": 7, "سبع": 7,
    "تمانية": 8, "ثمانية": 8, "تمنية": 8,
    "تسعة": 9, "تسع": 9,
    "عشرة": 10, "عشر": 10,
    "عشرين": 20,
    "تلاتين": 30, "ثلاثين": 30,
    "اربعين": 40,
    "خمسين": 50,
    "ستين": 60,
    "سبعين": 70,
    "تمانين": 80, "ثمانين": 80,
    "تسعين": 90,
    "مية": 100, "مائة": 100, "مئة": 100,
}
_SCALES: dict[str, int] = {
    "الف": 1_000, "الاف": 1_000,
    "مليون": 1_000_000, "الملايين": 1_000_000,
    "مليار": 1_000_000_000, "مليارات": 1_000_000_000,
}
_DUAL_SCALES: dict[str, int] = {  # "two of the scale" dual forms
    "الفين": 2_000, "مليونين": 2_000_000,
}
_HALF = {"نص", "نصف", "ونص", "ونصف", "ونصيف"}
_AND = "و"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _is_numeric_token(token: str) -> bool:
    return bool(token) and token.replace(",", "").replace(".", "", 1).lstrip("-").isdigit()


def _token_number(token: str) -> float | None:
    if not _is_numeric_token(token):
        return None
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _starts_number(token: str) -> bool:
    return (
        token in _NUMBER_WORDS
        or token in _SCALES
        or token in _DUAL_SCALES
        or token in _HALF
        or _is_numeric_token(token)
    )


def word_numerals_to_digits(text: str) -> str:
    """Best-effort conversion of Arabic word-numeral phrases to digits.

    Handles the listing dialect: ``(digit|word) scale`` combinations, dual
    scales ("مليونين" = 2M), the و connector, and half forms both before
    ("نص مليون" = 500K) and after ("2 مليون ونص" = 2.5M) a scale. Ornate
    classical grammar is out of scope and left as text.
    """
    tokens = text.split(" ")
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        if not _starts_number(tokens[i]):
            out.append(tokens[i])
            i += 1
            continue
        # Greedily consume a number-word run starting at i.
        j = i
        total = 0.0
        current = 0.0
        last_scale = 1
        while j < n:
            t = tokens[j]
            num = _NUMBER_WORDS.get(t)
            if num is not None:
                current += num
                j += 1
            elif _is_numeric_token(t):
                current += _token_number(t) or 0.0
                j += 1
            elif t in _SCALES:
                total += (current if current else 1) * _SCALES[t]
                last_scale = _SCALES[t]
                current = 0.0
                j += 1
            elif t in _DUAL_SCALES:
                total += _DUAL_SCALES[t]
                last_scale = _DUAL_SCALES[t] // 2
                current = 0.0
                j += 1
            elif t in _HALF and j + 1 < n and tokens[j + 1] in _SCALES:
                current += 0.5  # "نص مليون" — half rides the upcoming scale
                j += 1
            elif t == _AND and j + 1 < n and tokens[j + 1] in _HALF and last_scale > 1:
                total += last_scale * 0.5  # "مليون ونص"
                j += 2
            elif t in _HALF and last_scale > 1:
                total += last_scale * 0.5  # "ونص" attached form
                j += 1
            elif t == _AND and j + 1 < n and _continues_number(tokens[j + 1]):
                j += 1
            else:
                break
        if j == i:  # matched _starts_number but consumed nothing (stray half-word)
            out.append(tokens[i])
            i += 1
            continue
        total += current
        out.append(_fmt(total))
        i = j
    return " ".join(out)


def _continues_number(token: str) -> bool:
    return (
        token in _NUMBER_WORDS
        or token in _SCALES
        or token in _DUAL_SCALES
        or _is_numeric_token(token)
    )


# --- redaction & whitespace --------------------------------------------------
def redact_noise(text: str) -> str:
    """Remove contact placeholders, hashtags, phone numbers and URLs."""
    text = RE_CONTACT_HIDDEN.sub(" ", text)
    text = RE_HASHTAG.sub(" ", text)
    text = RE_PHONE.sub(" ", text)
    return RE_URL.sub(" ", text)


def collapse_whitespace(text: str) -> str:
    return RE_WHITESPACE.sub(" ", text).strip()


def normalize_text(text: str | None) -> str | None:
    """Full Stage-2 hygiene pipeline; ``None`` stays ``None``."""
    if text is None:
        return None
    text = transliterate_digits(text)
    text = strip_diacritics(text)
    text = normalize_letters(text)
    text = word_numerals_to_digits(text)
    text = redact_noise(text)
    return collapse_whitespace(text)


def normalize_name_key(name: str | None) -> str | None:
    """Registry-key form for city/compound names: lowercase, collapsed."""
    if name is None:
        return None
    return collapse_whitespace(name).lower()


__all__ = [
    "collapse_whitespace",
    "normalize_letters",
    "normalize_name_key",
    "normalize_text",
    "redact_noise",
    "strip_diacritics",
    "transliterate_digits",
    "word_numerals_to_digits",
]