"""Deep data exploration & quality profiling for scrappers/unified_output.jsonl.

Usage:
    python scripts/explore_data.py
    python scripts/explore_data.py --input scrappers/unified_output.jsonl --out scripts/eda_report.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Regexes for text / cleaning signals
# ---------------------------------------------------------------------------
RE_ARABIC = re.compile(r"[\u0600-\u06FF]")
RE_ARABIC_DIGITS = re.compile(r"[\u0660-\u0669\u06F0-\u06F9]")
RE_TASHKEEL_TATWEEL = re.compile(r"[\u0640\u064B-\u065F\u0670]")
RE_HTML_TAG = re.compile(r"<[a-zA-Z][^>]*>|&[a-zA-Z]+;")
RE_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
RE_PHONE = re.compile(r"(?:\+?20|0)?1[0125]\d[\s\-]?\d{3,4}[\s\-]?\d{4}")
RE_CONTACT_HIDDEN = re.compile(r"\[contact details hidden\]", re.I)
RE_HASHTAG = re.compile(r"#[\w\u0600-\u06FF]+")

# Extraction signals in descriptions (for regex vs LLM planning)
RE_CUE_FINISHING = re.compile(
    r"finish|semi[\s\-]?finish|core\s*&?\s*shell|red\s*brick|ultra\s*lux|"
    r"تشطيب|متشطب|نصف\s*تشطيب|نص\s*تشطيب|على\s*الطوب|طوب\s*احمر|أحمر|سوبر\s*لوكس",
    re.I,
)
RE_CUE_PAYMENT = re.compile(
    r"first\s*payment|down\s*payment|installment|years?\s*plan|over\s*price|"
    r"مقدم|تقسيط|اقساط|أقساط|قسط|دفعة|الباقي\s*فوق|اوفر|أوفر",
    re.I,
)
RE_CUE_DELIVERY = re.compile(
    r"deliver|handover|ready\s*to\s*move|immediate|استلام|تسليم|جاهز\s*للسكن|فوري",
    re.I,
)

# Rough Egypt bounding box (WGS84)
EGYPT_LAT = (21.5, 32.0)
EGYPT_LON = (24.5, 37.2)

EXPECTED_FIELDS = [
    "platform", "source_id", "source_url", "listing_ref", "listed_date",
    "broker_name", "agent_name", "developer_name", "title_raw",
    "property_type", "description", "size_m2", "plot_size_m2",
    "bedrooms", "bathrooms", "finishing", "furnished", "delivery_year",
    "completion_status", "resale", "has_nanny_room", "features",
    "payment_methods", "price_egp", "currency", "down_payment_egp",
    "installment_years", "payment_frequency", "price_per_m2",
    "governorate", "city", "compound", "latitude", "longitude",
    "compound_geometry", "image_urls", "floor_plan_urls", "master_plan_url",
]


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return math.nan
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def num_summary(vals: list[float]) -> str:
    if not vals:
        return "n=0"
    s = sorted(vals)
    return (
        f"n={len(s)} | min={s[0]:,.2f} | p5={percentile(s, 5):,.2f} | "
        f"med={percentile(s, 50):,.2f} | p95={percentile(s, 95):,.2f} | "
        f"p99={percentile(s, 99):,.2f} | max={s[-1]:,.2f}"
    )


def norm_basic(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    t = text.strip().lower()
    t = re.sub(r"[\u0640\u064B-\u065F\u0670]", "", t)
    t = re.sub(r"[أإآ]", "ا", t)
    t = t.replace("ى", "ي")
    t = re.sub(r"\s+", " ", t)
    return t


def is_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if isinstance(val, (list, dict)) and len(val) == 0:
        return True
    return False


def to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) else None
    if isinstance(val, str):
        try:
            f = float(val.replace(",", "").strip())
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def sample_snippet(row: dict[str, Any], reason: str) -> dict[str, Any]:
    desc = row.get("description") or ""
    if isinstance(desc, str) and len(desc) > 220:
        desc = desc[:220].replace("\n", " ") + "..."
    return {
        "reason": reason,
        "platform": row.get("platform"),
        "source_id": row.get("source_id"),
        "title_raw": (row.get("title_raw") or "")[:100],
        "property_type": row.get("property_type"),
        "size_m2": row.get("size_m2"),
        "price_egp": row.get("price_egp"),
        "currency": row.get("currency"),
        "down_payment_egp": row.get("down_payment_egp"),
        "price_per_m2": row.get("price_per_m2"),
        "finishing": row.get("finishing"),
        "governorate": row.get("governorate"),
        "city": row.get("city"),
        "compound": row.get("compound"),
        "lat_lon": [row.get("latitude"), row.get("longitude")],
        "desc_snippet": desc,
    }


def analyze(path: Path) -> str:
    total_lines = 0
    empty_lines = 0
    bad_json_lines = 0

    platforms = Counter()
    extra_keys = Counter()
    field_types: dict[str, Counter[str]] = defaultdict(Counter)

    # Per-platform stats
    plat_rows = Counter()
    null_counts: dict[str, Counter[str]] = defaultdict(Counter)
    empty_counts: dict[str, Counter[str]] = defaultdict(Counter)

    # IDs & duplicates
    seen_source_ids: dict[str, set[str]] = defaultdict(set)
    dup_source_ids = Counter()
    seen_urls: dict[str, set[str]] = defaultdict(set)
    dup_urls = Counter()

    # Categoricals per platform
    cat_fields = [
        "property_type", "finishing", "furnished", "completion_status",
        "resale", "has_nanny_room", "currency", "payment_frequency",
        "governorate", "city",
    ]
    cats: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    payment_methods_counter: dict[str, Counter[str]] = defaultdict(Counter)
    payment_combos: dict[str, Counter[str]] = defaultdict(Counter)
    compounds_per_plat: dict[str, Counter[str]] = defaultdict(Counter)
    compounds_norm_set: dict[str, set[tuple[str, str]]] = defaultdict(set)

    # Features
    features_raw: dict[str, Counter[str]] = defaultdict(Counter)
    features_whitespace_issues = 0
    feature_list_lens: dict[str, list[float]] = defaultdict(list)

    # Numerics per platform
    num_fields = [
        "size_m2", "plot_size_m2", "bedrooms", "bathrooms", "delivery_year",
        "price_egp", "down_payment_egp", "installment_years", "price_per_m2",
    ]
    nums: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    computed_ppm2: dict[str, list[float]] = defaultdict(list)
    ppm2_mismatch = Counter()

    # Numeric anomalies
    anomalies = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add_sample(bucket: str, row: dict[str, Any], reason: str, cap: int = 3) -> None:
        if len(samples[bucket]) < cap:
            samples[bucket].append(sample_snippet(row, reason))

    # Geo & Media
    geo_stats = Counter()
    geom_types = Counter()
    media_stats = Counter()
    img_counts: dict[str, list[float]] = defaultdict(list)
    img_domains = Counter()
    img_exts = Counter()

    # Text & Arabic & Extraction cues
    text_signals = Counter()
    desc_lens: dict[str, list[float]] = defaultdict(list)
    cue_stats: dict[str, Counter[str]] = defaultdict(Counter)

    # Dedup estimation
    dedup_keys_exact: set[tuple[Any, ...]] = set()
    dedup_keys_cross: dict[tuple[Any, ...], set[str]] = defaultdict(set)

    # Date formats
    listed_date_samples: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, raw_line in enumerate(f, 1):
            total_lines += 1
            stripped = raw_line.strip()
            if not stripped:
                empty_lines += 1
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                bad_json_lines += 1
                continue
            if not isinstance(row, dict):
                bad_json_lines += 1
                continue

            plat = str(row.get("platform") or "MISSING")
            platforms[plat] += 1
            plat_rows[plat] += 1

            # Extra keys & types
            for k, v in row.items():
                if k not in EXPECTED_FIELDS:
                    extra_keys[k] += 1
                field_types[k][type(v).__name__] += 1

            # Null / empty checks
            for k in EXPECTED_FIELDS:
                v = row.get(k)
                if v is None:
                    null_counts[plat][k] += 1
                elif is_empty(v):
                    empty_counts[plat][k] += 1

            # ID & URL uniqueness
            sid = row.get("source_id")
            if sid is not None:
                sid_s = str(sid)
                if sid_s in seen_source_ids[plat]:
                    dup_source_ids[plat] += 1
                seen_source_ids[plat].add(sid_s)

            surl = row.get("source_url")
            if surl:
                if surl in seen_urls[plat]:
                    dup_urls[plat] += 1
                seen_urls[plat].add(surl)

            # Categoricals
            for cf in cat_fields:
                v = row.get(cf)
                cats[plat][cf][repr(v) if v is None or isinstance(v, bool) else str(v)] += 1

            pm = row.get("payment_methods")
            if isinstance(pm, list):
                payment_combos[plat][",".join(sorted(str(x) for x in pm))] += 1
                for m in pm:
                    payment_methods_counter[plat][str(m)] += 1

            comp = row.get("compound")
            city = row.get("city")
            if isinstance(comp, str) and comp.strip():
                compounds_per_plat[plat][comp.strip()] += 1
                compounds_norm_set[plat].add((norm_basic(city), norm_basic(comp)))

            # Features
            feats = row.get("features")
            if isinstance(feats, list):
                feature_list_lens[plat].append(float(len(feats)))
                for feat in feats:
                    if isinstance(feat, str):
                        if feat != feat.strip():
                            features_whitespace_issues += 1
                        features_raw[plat][feat.strip()] += 1

            # Numerics
            for nf in num_fields:
                fv = to_float(row.get(nf))
                if fv is not None:
                    nums[plat][nf].append(fv)

            price = to_float(row.get("price_egp"))
            size = to_float(row.get("size_m2"))
            plot_size = to_float(row.get("plot_size_m2"))
            dp = to_float(row.get("down_payment_egp"))
            ppm2 = to_float(row.get("price_per_m2"))
            dy = to_float(row.get("delivery_year"))
            beds = to_float(row.get("bedrooms"))
            baths = to_float(row.get("bathrooms"))

            if price is not None:
                if price <= 0:
                    anomalies[f"{plat}:price<=0"] += 1
                    add_sample("price_outlier_low", row, "price<=0")
                elif price < 200_000:
                    anomalies[f"{plat}:price<200k"] += 1
                    add_sample("price_outlier_low", row, f"price={price}")
                elif price > 300_000_000:
                    anomalies[f"{plat}:price>300M"] += 1
                    add_sample("price_outlier_high", row, f"price={price}")

            if size is not None:
                if size <= 0:
                    anomalies[f"{plat}:size<=0"] += 1
                    add_sample("size_outlier", row, f"size={size}")
                elif size < 20 or size > 5000:
                    anomalies[f"{plat}:size_extreme(<20_or_>5000)"] += 1
                    add_sample("size_outlier", row, f"size={size}")

            if size is not None and plot_size is not None and plot_size < size:
                anomalies[f"{plat}:plot_size<size"] += 1

            if price is not None and size is not None and size > 0:
                calc = price / size
                computed_ppm2[plat].append(calc)
                if calc < 5_000:
                    anomalies[f"{plat}:calc_ppm2<5k"] += 1
                    add_sample("low_ppm2_suspect_dp", row, f"calc_ppm2={calc:.0f}")
                elif calc > 500_000:
                    anomalies[f"{plat}:calc_ppm2>500k"] += 1
                    add_sample("high_ppm2", row, f"calc_ppm2={calc:.0f}")
                if ppm2 is not None and ppm2 > 0:
                    rel_diff = abs(calc - ppm2) / ppm2
                    if rel_diff > 0.05:
                        ppm2_mismatch[plat] += 1

            if dp is not None and price is not None and dp > price:
                anomalies[f"{plat}:down_payment>price"] += 1
                add_sample("dp_gt_price", row, f"dp={dp} > price={price}")

            if dy is not None and (dy < 1995 or dy > 2038):
                anomalies[f"{plat}:delivery_year_weird"] += 1
                add_sample("weird_year", row, f"delivery_year={dy}")

            if beds is not None and (beds < 0 or beds > 20):
                anomalies[f"{plat}:bedrooms_weird"] += 1
            if baths is not None and (baths < 0 or baths > 20):
                anomalies[f"{plat}:bathrooms_weird"] += 1

            # Geo
            lat = to_float(row.get("latitude"))
            lon = to_float(row.get("longitude"))
            if lat is not None and lon is not None:
                in_egypt = (
                    EGYPT_LAT[0] <= lat <= EGYPT_LAT[1]
                    and EGYPT_LON[0] <= lon <= EGYPT_LON[1]
                )
                swapped = (
                    EGYPT_LAT[0] <= lon <= EGYPT_LAT[1]
                    and EGYPT_LON[0] <= lat <= EGYPT_LON[1]
                )
                if in_egypt:
                    geo_stats[f"{plat}:lat_lon_ok"] += 1
                elif swapped:
                    geo_stats[f"{plat}:lat_lon_SWAPPED"] += 1
                    add_sample("geo_issue", row, f"swapped lat={lat}, lon={lon}")
                else:
                    geo_stats[f"{plat}:lat_lon_out_of_egypt"] += 1
                    add_sample("geo_issue", row, f"out_of_egypt lat={lat}, lon={lon}")
            elif lat is None and lon is None:
                geo_stats[f"{plat}:lat_lon_both_null"] += 1
            else:
                geo_stats[f"{plat}:lat_lon_partial_null"] += 1

            cg = row.get("compound_geometry")
            if isinstance(cg, dict):
                geom_types[str(cg.get("type"))] += 1

            # Listed date sample
            ld = row.get("listed_date")
            if isinstance(ld, str) and ld.strip() and len(listed_date_samples) < 5:
                listed_date_samples.append(f"{plat}: {ld}")

            # Media
            imgs = row.get("image_urls")
            if isinstance(imgs, list):
                img_counts[plat].append(float(len(imgs)))
                if len(imgs) == 0:
                    media_stats[f"{plat}:0_images"] += 1
                elif len(imgs) > 6:
                    media_stats[f"{plat}:>6_images"] += 1
                for u in imgs:
                    if isinstance(u, str):
                        parsed = urlparse(u)
                        img_domains[parsed.netloc] += 1
                        ext = Path(parsed.path).suffix.lower() or "(none)"
                        img_exts[ext] += 1

            fps = row.get("floor_plan_urls")
            if isinstance(fps, list) and len(fps) > 0:
                media_stats[f"{plat}:has_floor_plan"] += 1
            if row.get("master_plan_url"):
                media_stats[f"{plat}:has_master_plan"] += 1

            # Text & Arabic signals
            title = row.get("title_raw") or ""
            desc = row.get("description") or ""
            if isinstance(desc, str):
                desc_lens[plat].append(float(len(desc)))
            combined_text = f"{title}\n{desc}"

            if RE_ARABIC.search(str(title)):
                text_signals[f"{plat}:arabic_in_title"] += 1
            if RE_ARABIC.search(str(desc)):
                text_signals[f"{plat}:arabic_in_desc"] += 1
            if RE_ARABIC.search(f"{row.get('city') or ''} {row.get('compound') or ''}"):
                text_signals[f"{plat}:arabic_in_location"] += 1
            if RE_ARABIC_DIGITS.search(combined_text):
                text_signals[f"{plat}:arabic_indic_digits"] += 1
            if RE_TASHKEEL_TATWEEL.search(combined_text):
                text_signals[f"{plat}:tashkeel_or_tatweel"] += 1
            if RE_HTML_TAG.search(combined_text):
                text_signals[f"{plat}:html_tags_or_entities"] += 1
            if RE_URL.search(combined_text):
                text_signals[f"{plat}:urls_in_text"] += 1
            if RE_PHONE.search(combined_text):
                text_signals[f"{plat}:phone_numbers_in_text"] += 1
            if RE_CONTACT_HIDDEN.search(combined_text):
                text_signals[f"{plat}:contact_hidden_placeholder"] += 1
            if RE_HASHTAG.search(combined_text):
                text_signals[f"{plat}:hashtags_in_text"] += 1

            # Extraction cues when fields are missing/suspect
            if row.get("finishing") is None:
                cue_stats[plat]["finishing_is_null"] += 1
                if RE_CUE_FINISHING.search(combined_text):
                    cue_stats[plat]["finishing_null_but_cue_in_text"] += 1

            if row.get("down_payment_egp") is None or row.get("installment_years") is None:
                cue_stats[plat]["payment_terms_incomplete"] += 1
                if RE_CUE_PAYMENT.search(combined_text):
                    cue_stats[plat]["payment_incomplete_but_cue_in_text"] += 1

            if row.get("delivery_year") is None:
                cue_stats[plat]["delivery_year_is_null"] += 1
                if RE_CUE_DELIVERY.search(combined_text):
                    cue_stats[plat]["delivery_null_but_cue_in_text"] += 1

            # Rough dedup key estimate (compound_norm, prop_type_norm, beds, size_bucket_5pct)
            comp_n = norm_basic(comp) or norm_basic(city)
            pt_n = norm_basic(row.get("property_type"))
            size_b = round(size / 5.0) * 5 if size else None
            dkey = (comp_n, pt_n, int(beds) if beds is not None else None, size_b)
            dedup_keys_exact.add(dkey)
            dedup_keys_cross[dkey].add(plat)

    # -----------------------------------------------------------------------
    # Format Report
    # -----------------------------------------------------------------------
    out: list[str] = []
    w = out.append

    w("# RealEstateAnalysis — Local Data Exploration Report")
    w(f"- **File:** `{path}`")
    w(f"- **Total lines:** {total_lines:,} (empty: {empty_lines:,}, malformed JSON: {bad_json_lines:,})")
    w(f"- **Valid records:** {sum(platforms.values()):,}")
    w("- **Platform breakdown:** " + ", ".join(f"{k}: {v:,}" for k, v in platforms.most_common()))
    if extra_keys:
        w("- **Unexpected extra keys:** " + str(dict(extra_keys)))
    w("")

    w("## 1. ID & URL Uniqueness (Within-File Duplicates)")
    for p, cnt in plat_rows.items():
        w(f"- **{p}**: {cnt:,} rows | unique `source_id`: {len(seen_source_ids[p]):,} (dups: {dup_source_ids[p]:,}) | unique `source_url`: {len(seen_urls[p]):,} (dups: {dup_urls[p]:,})")
    w("")

    w("## 2. Field Coverage (Nulls & Empties per Platform) + Observed Python Types")
    w("| Field | Types | " + " | ".join(f"{p} null / empty" for p in plat_rows) + " |")
    w("|---|---|" + "|".join("---" for _ in plat_rows) + "|")
    for k in EXPECTED_FIELDS:
        types_str = ", ".join(f"{t}({c})" for t, c in field_types[k].most_common(3))
        cols = []
        for p in plat_rows:
            tot = plat_rows[p]
            n = null_counts[p][k]
            e = empty_counts[p][k]
            cols.append(f"{n:,} ({n*100/tot:.1f}%) / {e:,}")
        w(f"| `{k}` | {types_str} | " + " | ".join(cols) + " |")
    w("")

    w("## 3. Categorical Distributions (per Platform)")
    for cf in cat_fields:
        w(f"### `{cf}`")
        for p in plat_rows:
            top = cats[p][cf].most_common(25)
            total_distinct = len(cats[p][cf])
            formatted = ", ".join(f"`{val}`:{cnt}" for val, cnt in top)
            w(f"- **{p}** ({total_distinct} distinct): {formatted}")
    w("### `payment_methods` & combos")
    for p in plat_rows:
        w(f"- **{p} items:** {dict(payment_methods_counter[p])}")
        w(f"- **{p} combos:** {dict(payment_combos[p])}")
    w("")

    w("## 4. Locations & Cross-Platform Compound Overlap")
    for p in plat_rows:
        w(f"- **{p} unique compounds:** {len(compounds_per_plat[p]):,} (normalized `(city, compound)` pairs: {len(compounds_norm_set[p]):,})")
        w(f"  - Top 20: {', '.join(f'{c} ({n})' for c, n in compounds_per_plat[p].most_common(20))}")
    if len(compounds_norm_set) >= 2:
        plist = list(compounds_norm_set.keys())
        overlap = compounds_norm_set[plist[0]] & compounds_norm_set[plist[1]]
        w(f"- **Exact normalized `(city, compound)` overlap ({plist[0]} ∩ {plist[1]}):** {len(overlap):,}")
        if overlap:
            w(f"  - Sample overlaps: {sorted(list(overlap))[:15]}")
    w("")

    w("## 5. Numeric Distributions & Computed `price_per_m2`")
    for nf in num_fields:
        w(f"### `{nf}`")
        for p in plat_rows:
            w(f"- **{p}:** {num_summary(nums[p][nf])}")
    w("### Computed `price_egp / size_m2`")
    for p in plat_rows:
        w(f"- **{p}:** {num_summary(computed_ppm2[p])} | >5% mismatch vs scraped `price_per_m2`: {ppm2_mismatch[p]:,}")
    w("")

    w("## 6. Anomalies & Traps Detected")
    if anomalies:
        for k, v in anomalies.most_common():
            w(f"- `{k}`: **{v:,}**")
    else:
        w("- None detected by threshold checks.")
    w("")

    w("## 7. Geo & Media Coverage")
    for k, v in sorted(geo_stats.items()):
        w(f"- `{k}`: {v:,}")
    w(f"- `compound_geometry` types: {dict(geom_types)}")
    for p in plat_rows:
        w(f"- **{p} `image_urls` len:** {num_summary(img_counts[p])}")
    for k, v in sorted(media_stats.items()):
        w(f"- `{k}`: {v:,}")
    w(f"- Top image domains: {dict(img_domains.most_common(10))}")
    w(f"- Image extensions: {dict(img_exts.most_common(10))}")
    w(f"- `listed_date` samples: {listed_date_samples}")
    w("")

    w("## 8. Features / Amenities")
    w(f"- Strings with leading/trailing whitespace: **{features_whitespace_issues:,}**")
    for p in plat_rows:
        w(f"- **{p} feature count/listing:** {num_summary(feature_list_lens[p])} | distinct strings: {len(features_raw[p]):,}")
        w(f"  - Top 25: {', '.join(f'{k} ({v})' for k, v in features_raw[p].most_common(25))}")
    w("")

    w("## 9. Text, Arabic Hygiene & LLM Extraction Need")
    for p in plat_rows:
        w(f"- **{p} description char length:** {num_summary(desc_lens[p])}")
    for k, v in sorted(text_signals.items()):
        w(f"- `{k}`: {v:,}")
    w("### Missing Fields That Have Text Cues (Regex vs Groq Budget Estimate)")
    for p in plat_rows:
        w(f"- **{p}:** {dict(cue_stats[p])}")
    w("")

    w("## 10. Rough Deduplication Bucket Estimate")
    cross_hits = sum(1 for plats in dedup_keys_cross.values() if len(plats) > 1)
    w(f"- Total rows: {sum(plat_rows.values()):,}")
    w(f"- Distinct coarse buckets `(compound_or_city, type, beds, size_5m_bin)`: **{len(dedup_keys_exact):,}**")
    w(f"- Buckets hit by >1 platform: **{cross_hits:,}**")
    w("")

    w("## 11. Concrete Sample Anomalies (Truncated)")
    for bucket, items in samples.items():
        w(f"### Bucket: `{bucket}`")
        for it in items:
            w("```json\n" + json.dumps(it, ensure_ascii=False) + "\n```")
    w("")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Explore unified_output.jsonl")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("scrappers/unified_output.jsonl"),
        help="Path to unified_output.jsonl",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("scripts/eda_report.md"),
        help="Path to save the markdown report",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 1

    report = analyze(args.input)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[Saved full report to {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())