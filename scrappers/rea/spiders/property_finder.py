import asyncio
import json
import random
import re
from urllib.parse import urlsplit

import scrapy
from scrapy import signals
from scrapy.exceptions import CloseSpider
from scrapy_playwright.page import PageMethod

from rea import utils
from rea.checkpoint import Checkpoint
from rea.items import ReaItem

_SCRAPE_DOMAINS = ("propertyfinder.eg",)

_ALLOWED_PF_ASSET_DOMAINS = (
    "propertyfinder.eg",
    "propertyfinder.com",
    "propertyfinder.ae",
)

_ALLOWED_WAF_DOMAINS = ("awswaf.com",)
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

_INT_RE = re.compile(r"(\d+)")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _clean_str(value):
    """Portal strings carry trailing spaces ('Nour Atwa ') that would
    later poison pg_trgm broker-name dedup - strip once, at the edge."""
    if not isinstance(value, str):
        return value
    return value.strip() or None


def _host_matches(host: str, suffixes) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in suffixes)


def _is_allowed_scrape_url(url: str) -> bool:
    """Return True iff `url` is an HTTPS URL within propertyfinder.eg."""
    if not url or not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        return False
    host = (parts.hostname or "").lower()
    return bool(host) and _host_matches(host, _SCRAPE_DOMAINS)


def _to_number(value):
    """Parse int/float/numeric string without dropping fractional floats."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    return None


def _parse_room_count(value):
    """Parse bedrooms/bathrooms, handling 'Studio' -> 0 and '3+' -> 3."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return None
        if "studio" in s:
            return 0
        m = _INT_RE.search(s)
        return int(m.group(1)) if m else None
    return utils.to_int(value)


def _extract_year(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 2000 <= value <= 2100 else None
    if isinstance(value, str):
        m = _YEAR_RE.search(value)
        return int(m.group(1)) if m else None
    return None


def should_abort_request(request) -> bool:
    """Block non-PropertyFinder requests (ads/telemetry) and heavy media.

    Hooked via ``PLAYWRIGHT_ABORT_REQUEST`` so it intercepts every sub-request
    made by Chromium and reports counts under ``playwright/request_count/aborted``.
    Top-level document navigations are restricted strictly to ``propertyfinder.eg``.
    """
    parts = urlsplit(request.url)
    scheme = parts.scheme.lower()
    if scheme in ("about", "blob", "data"):
        return False

    if scheme not in ("http", "https"):
        return True

    host = (parts.hostname or "").lower()
    if not host:
        return True

    is_nav = request.resource_type == "document" or (
        callable(getattr(request, "is_navigation_request", None))
        and request.is_navigation_request()
    )

    if _host_matches(host, _ALLOWED_WAF_DOMAINS):
        if is_nav:
            try:
                if request.frame and request.frame.parent_frame is None:
                    return True
            except Exception:
                return True
        return False

    if is_nav:
        return not _host_matches(host, _SCRAPE_DOMAINS)

    if _host_matches(host, _ALLOWED_PF_ASSET_DOMAINS):
        return request.resource_type in _BLOCKED_RESOURCE_TYPES

    # Abort all external domains (ads, trackers, analytics, telemetry, etc.)
    return True


async def backoff_page_init(page, request):
    """scrapy-playwright page init callback: delays the navigation of a
    retry request after the WAF challenged us. Runs inside the download
    handler's event loop, so it holds up only this request, not the
    reactor."""
    delay = request.meta.get("pf_backoff_secs", 0)
    if delay > 0:
        await asyncio.sleep(delay)


class PropertyFinderSpider(scrapy.Spider):
    name = "property_finder"
    platform_name = "PropertyFinder"
    allowed_domains = ["propertyfinder.eg"]

    # c=1 means "Buy" (use c=2 for "Rent")
    base_url = "https://www.propertyfinder.eg/en/search?c=1&"

    handle_httpstatus_list = [403, 405, 429]

    custom_settings = {
        "PLAYWRIGHT_ABORT_REQUEST": should_abort_request,
    }

    # --- tunables (override on the CLI: -a max_items=500 -a start_page=41) ---
    max_items = 100          # stop after this many unique listings
    start_page = 1           # first page to request
    max_tries_per_page = 3   # attempts per page before giving up on it
    backoff_schedule = (15, 45, 120)
    hard_block_backoff_schedule = (120, 300)
    max_consecutive_failures = 3

    checkpoint_dir = "checkpoints"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # spider arguments always arrive as strings - cast the numeric ones
        for attr in (
            "max_items",
            "start_page",
            "max_tries_per_page",
            "max_consecutive_failures",
        ):
            val = getattr(self, attr, None)
            if isinstance(val, str):
                setattr(self, attr, int(val))

        if not _is_allowed_scrape_url(self.base_url):
            raise ValueError(
                f"Invalid base_url {self.base_url!r}: must be HTTPS within propertyfinder.eg"
            )

        self.item_count = 0
        self.seen_ids = set()
        self.consecutive_failures = 0
        self.total_pages = None

        # resume=1 continues from checkpoints/property_finder.ckpt.json
        self.resume = str(kwargs.get("resume", "")).lower() in ("1", "true", "yes")
        # reset_output=1 (only on non-resume runs) clears the unified output file
        self.reset_output = str(kwargs.get("reset_output", "")).lower() in (
            "1", "true", "yes"
        )
        self.cp = Checkpoint.load(self.name, directory=self.checkpoint_dir)
        if not self.resume and self.cp.has_progress:
            # fresh run: ignore any stale checkpoint left behind
            self.cp = Checkpoint(
                self.name,
                last_page=0,
                keys=set(),
                pending=set(),
                path=self.cp.path,
            )

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.settings.set(
            "CLOSESPIDER_ITEMCOUNT", spider.max_items, priority="spider"
        )
        # final flush on close: the debounced save may hold up to
        # SAVE_INTERVAL secs of progress that must not be lost
        crawler.signals.connect(
            spider._flush_checkpoint, signal=signals.spider_closed
        )
        return spider

    def _flush_checkpoint(self):
        self.cp.save()

    async def start(self):
        if self.resume:
            first_page = max(int(self.start_page), self.cp.last_page + 1)
            if self.cp.last_page:
                self.logger.info(
                    f"Resuming after search page {self.cp.last_page} "
                    f"({len(self.cp.keys)} listings already scraped)"
                )
        else:
            first_page = int(self.start_page)
        yield self._build_request(first_page)

    def _page_methods(self, page_num, tries):
        return [
            PageMethod(
                "wait_for_selector",
                "#__NEXT_DATA__",
                state="attached",
                timeout=45_000,
            ),
            PageMethod(
                "wait_for_function",
                "!document.getElementById('aws-waf-script')"
                " || document.cookie.includes('aws-waf-token')",
                timeout=20_000,
            ),
            PageMethod("wait_for_timeout", random.randint(1200, 3200)),
        ]

    def _build_request(self, page_num, tries=1, backoff=0):
        sep = "" if self.base_url.endswith(("?", "&")) else "&"
        target_url = f"{self.base_url}{sep}page={page_num}"
        if not _is_allowed_scrape_url(target_url):
            raise ValueError(
                f"Refusing off-domain request URL: {target_url!r}"
            )

        meta = {
            "playwright": True,
            "playwright_context": "default",
            "playwright_page_methods": self._page_methods(page_num, tries),
            "pf_page_num": page_num,
            "pf_tries": tries,
        }
        if backoff > 0:
            meta["pf_backoff_secs"] = backoff
            meta["playwright_page_init_callback"] = backoff_page_init

        return scrapy.Request(
            url=target_url,
            callback=self.parse,
            errback=self.on_request_failure,
            meta=meta,
            dont_filter=True,
        )

    def parse(self, response):
        if not _is_allowed_scrape_url(response.url):
            self.logger.error(
                f"Off-domain response blocked from scraping: {response.url}"
            )
            return

        page_num = response.meta.get("pf_page_num", 1)
        tries = response.meta.get("pf_tries", 1)

        next_data_script = response.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
        if not next_data_script:
            hard_block = "could not be satisfied" in (response.text or "")
            yield from self._handle_blocked_page(
                page_num,
                tries,
                f"no __NEXT_DATA__ (status {response.status}) - "
                f"{'CloudFront hard IP block' if hard_block else 'WAF interstitial'}",
                hard_block=hard_block,
            )
            return

        try:
            data = json.loads(next_data_script)
        except json.JSONDecodeError:
            yield from self._handle_blocked_page(
                page_num,
                tries,
                "malformed __NEXT_DATA__ JSON",
            )
            return

        search_result = (
            data.get("props", {}).get("pageProps", {}).get("searchResult", {})
        )
        listings = search_result.get("listings", [])
        meta_data = search_result.get("meta", {})

        if not listings:
            self.logger.info(f"No listings on page {page_num} - end of results.")
            self.cp.mark_page(page_num)
            self.cp.save()
            return

        # PropertyFinder metadata sometimes uses page_count, sometimes pageCount
        total_pages_meta = meta_data.get("page_count") or meta_data.get("pageCount")
        if self.total_pages is None and total_pages_meta:
            self.total_pages = total_pages_meta
            self.logger.info(
                f"Search reports {meta_data.get('total_count')} listings "
                f"across {self.total_pages} pages."
            )

        self.consecutive_failures = 0
        self.logger.info(
            f"Page {page_num} OK: {len(listings)} entries "
            f"({self.item_count} items yielded so far)"
        )

        for lst in listings:
            if self.item_count >= self.max_items:
                self.logger.info(f"Reached max_items={self.max_items}. Stopping.")
                break

            prop_data = lst.get("property") if isinstance(lst, dict) else None
            if not isinstance(prop_data, dict) or not prop_data:
                continue

            prop_id = prop_data.get("id")
            prop_key = str(prop_id) if prop_id is not None else None
            if prop_key and prop_key in self.seen_ids:
                continue
            # checkpoint may already know this listing from a previous run
            if self.resume and prop_key and self.cp.seen(prop_key):
                continue
            if prop_key:
                self.seen_ids.add(prop_key)
                self.cp.add_pending(prop_key)

            yield self._build_item(prop_data, response)
            self.item_count += 1

        self.cp.mark_page(page_num)
        self.cp.save_if_due()

        # --- paginate ---
        if self.item_count >= self.max_items:
            return

        next_page = page_num + 1
        if self.total_pages and next_page > self.total_pages:
            self.logger.info(f"Page {next_page} beyond page_count - done.")
            return

        yield self._build_request(next_page)

    @staticmethod
    def _listing_source_url(prop, response):
        for candidate in (
            prop.get("share_url"),
            prop.get("details_path"),
            prop.get("url"),
        ):
            if not candidate or not isinstance(candidate, str):
                continue
            resolved = response.urljoin(candidate.strip())
            if _is_allowed_scrape_url(resolved):
                return resolved
        return response.url

    @staticmethod
    def _extract_features(prop):
        features = []
        seen = set()

        def _add(name):
            cleaned = _clean_str(name)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                features.append(cleaned)

        for entry in prop.get("amenity_names") or []:
            if isinstance(entry, str):
                _add(entry)
        for entry in prop.get("amenities") or []:
            if isinstance(entry, dict):
                _add(entry.get("name"))
            elif isinstance(entry, str):
                _add(entry)
        return features

    def _build_item(self, prop, response):
        item = ReaItem()

        # --- ScrapedListing fields ---
        item["platform"] = "PropertyFinder"
        item["source_id"] = prop.get("id")
        item["source_url"] = self._listing_source_url(prop, response)
        item["listing_ref"] = _clean_str(prop.get("reference"))
        item["listed_date"] = _clean_str(prop.get("listed_date"))
        item["broker_name"] = _clean_str((prop.get("broker") or {}).get("name"))
        item["agent_name"] = _clean_str((prop.get("agent") or {}).get("name"))
        dev = prop.get("developer")
        dev_name = (
            _clean_str(dev.get("name")) if isinstance(dev, dict) else _clean_str(dev)
        )
        if not dev_name and prop.get("is_direct_from_developer"):
            dev_name = item["broker_name"]
        item["developer_name"] = dev_name
        item["title_raw"] = _clean_str(prop.get("title"))

        # --- Property fields ---
        item["property_type"] = _clean_str(prop.get("property_type"))
        item["description"] = utils.strip_html(prop.get("description") or "")

        raw_size = prop.get("size")
        size_val = raw_size.get("value") if isinstance(raw_size, dict) else raw_size
        item["size_m2"] = _to_number(size_val)

        raw_plot = prop.get("plot_size")
        plot_val = raw_plot.get("value") if isinstance(raw_plot, dict) else raw_plot
        item["plot_size_m2"] = _to_number(plot_val)

        raw_beds = prop.get("bedrooms_value")
        if raw_beds is None:
            raw_beds = prop.get("bedrooms")
        item["bedrooms"] = _parse_room_count(raw_beds)

        raw_baths = prop.get("bathrooms_value")
        if raw_baths is None:
            raw_baths = prop.get("bathrooms")
        item["bathrooms"] = _parse_room_count(raw_baths)

        # PF rarely carries finishing/delivery_year; ETL Stage 4 recovers them
        # from description if absent.
        item["finishing"] = _clean_str(prop.get("finishing"))
        item["furnished"] = _clean_str(prop.get("furnished"))
        item["delivery_year"] = _extract_year(
            prop.get("delivery_year")
            or prop.get("delivery_date")
            or prop.get("handover_date")
        )
        completion_status = _clean_str(prop.get("completion_status"))
        item["completion_status"] = completion_status
        item["resale"] = (
            "resale" in completion_status.lower()
            if completion_status
            else None
        )

        features = self._extract_features(prop)
        item["features"] = features
        item["has_nanny_room"] = (
            True
            if any("maid" in f.lower() or "nanny" in f.lower() for f in features)
            else None
        )

        raw_pm = prop.get("payment_method")
        if isinstance(raw_pm, list):
            item["payment_methods"] = [p for p in raw_pm if p]
        elif isinstance(raw_pm, str) and raw_pm.strip():
            item["payment_methods"] = [raw_pm.strip()]
        else:
            item["payment_methods"] = []

        # --- Financial fields ---
        raw_price = prop.get("price")
        price_val = raw_price.get("value") if isinstance(raw_price, dict) else raw_price
        price_egp = _to_number(price_val)
        item["price_egp"] = price_egp
        item["currency"] = (
            _clean_str(raw_price.get("currency")) if isinstance(raw_price, dict) else None
        ) or "EGP"

        raw_dp = prop.get("down_payment_price")
        dp_val = raw_dp.get("value") if isinstance(raw_dp, dict) else raw_dp
        item["down_payment_egp"] = _to_number(dp_val)
        item["installment_years"] = _to_number(prop.get("installment_years"))
        item["payment_frequency"] = _clean_str(prop.get("payment_frequency"))

        ppa = prop.get("price_per_area")
        ppa_val = (
            (ppa.get("price") if ppa.get("price") is not None else ppa.get("value"))
            if isinstance(ppa, dict)
            else ppa
        )
        price_per_m2 = _to_number(ppa_val)
        if price_per_m2 is None and price_egp and item["size_m2"]:
            price_per_m2 = round(price_egp / item["size_m2"])
        item["price_per_m2"] = price_per_m2

        # --- Location fields ---
        location = prop.get("location") if isinstance(prop.get("location"), dict) else {}
        tree = prop.get("location_tree") or []
        names = [
            _clean_str(t.get("name"))
            for t in tree
            if isinstance(t, dict) and _clean_str(t.get("name"))
        ]
        item["governorate"] = names[0] if len(names) > 0 else None
        item["city"] = names[1] if len(names) > 1 else None
        item["compound"] = (
            names[-1]
            if len(names) > 2
            else _clean_str(location.get("name")) if not names else None
        )
        coords = (
            location.get("coordinates")
            if isinstance(location.get("coordinates"), dict)
            else {}
        )
        lon = coords.get("lon") if coords.get("lon") is not None else coords.get("lng")
        item["latitude"], item["longitude"] = utils.sanitize_coords(
            coords.get("lat"), lon
        )
        item["compound_geometry"] = None  # PF gives a point, not a polygon

        # --- Media fields (URLs only) ---
        image_groups = prop.get("images") or {}
        photos = []
        raw_photos = (
            image_groups.get("property")
            if isinstance(image_groups, dict)
            else image_groups
        )
        for entry in raw_photos or []:
            if isinstance(entry, dict):
                url = (
                    entry.get("medium")
                    or entry.get("large")
                    or entry.get("full")
                    or entry.get("url")
                )
                if url:
                    photos.append(url)
            elif isinstance(entry, str) and entry.strip():
                photos.append(entry.strip())

        floor_plans = []
        raw_fps = list(prop.get("floor_plans") or [])
        if isinstance(image_groups, dict):
            raw_fps.extend(image_groups.get("floor_plans") or [])
            raw_fps.extend(image_groups.get("floor_plan") or [])
        for entry in raw_fps:
            if isinstance(entry, dict):
                url = entry.get("url") or entry.get("medium") or entry.get("image")
                if url:
                    floor_plans.append(url)
            elif isinstance(entry, str) and entry.strip():
                floor_plans.append(entry.strip())

        item["floor_plan_urls"] = utils.capped_image_urls(floor_plans, max_images=len(floor_plans))
        item["master_plan_url"] = None  # PF has no master-plan concept
        item["image_urls"] = utils.capped_image_urls(floor_plans, photos)

        return item

    def on_request_failure(self, failure):
        request = failure.request
        page_num = request.meta.get("pf_page_num", 1)
        tries = request.meta.get("pf_tries", 1)
        yield from self._handle_blocked_page(
            page_num,
            tries,
            f"request failed: {failure.type.__name__}: {str(failure.value)[:160]}",
        )

    def _handle_blocked_page(self, page_num, tries, reason, hard_block=False):
        self.logger.warning(
            f"Page {page_num} blocked (attempt {tries}): {reason}"
        )

        if self.consecutive_failures >= self.max_consecutive_failures:
            self.logger.error(
                f"{self.consecutive_failures} consecutive blocked pages - "
                f"the WAF is hard-blocking this IP/session. Giving up."
            )
            raise CloseSpider("waf_hard_block")

        if tries >= self.max_tries_per_page:
            self.logger.error(
                f"Page {page_num} still blocked after {tries} attempts - skipping."
            )
            self.consecutive_failures += 1
            yield self._build_request(page_num + 1, tries=1)
            return

        self.consecutive_failures += 1
        schedule = (
            self.hard_block_backoff_schedule if hard_block else self.backoff_schedule
        )
        delay = schedule[min(tries - 1, len(schedule) - 1)]

        self.logger.info(
            f"Retrying page {page_num} in {delay}s (attempt {tries + 1})..."
        )
        yield self._build_request(page_num, tries=tries + 1, backoff=delay)