"""Nawy spider - two-phase: search IDs, then per-property detail API.

Phase 1 walks the public search API (plain JSON, no browser needed)
and collects property IDs only. Phase 2 requests
``https://listing-api.nawy.com/v1/properties/{id}`` for each ID and
builds the rich ReaItem from the full detail payload.

Media policy: image URLs only, floor plans + master-plan map first,
hard cap of 6 (embedding API batch limit). No downloads, no raw blob.

Unified output & checkpoint/resume: items pass through
``UnifiedDeduplicationPipeline``, which deduplicates against existing
listings from both Nawy and PropertyFinder and appends unique records to
``unified_output.jsonl``. Progress is persisted to
``checkpoints/nawy.ckpt.json``; run with ``-a resume=1`` to continue an
interrupted run.
"""
import json

import scrapy
from scrapy import signals

from rea.checkpoint import Checkpoint
from rea.items import ReaItem
from rea import utils


class NawySpider(scrapy.Spider):
    name = "nawy"
    platform_name = "Nawy"
    allowed_domains = ["nawy.com", "listing-api.nawy.com"]

    search_api = "https://listing-api.nawy.com/v1/search/properties"
    detail_api = "https://listing-api.nawy.com/v1/properties/{property_id}"

    custom_settings = {
        # --- NEW SETTINGS FOR 429 HANDLING ---
        "DOWNLOADER_MIDDLEWARES": {
            # Disable Scrapy's default retry middleware
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            # Enable our custom middleware (ensure 'rea' matches your project folder name)
            "rea.middlewares.ExponentialBackoffRetryMiddleware": 550,
        },
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],
        "RETRY_TIMES": 6,                    # Max amount of retries before dropping

        # PREVENTATIVE RATE LIMITING:
        "DOWNLOAD_DELAY": 1.5,               # Wait 1.5 seconds between normal requests
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2, # Don't blast the server concurrently
    }

    # --- tunables (CLI: -a max_items=1000 -a start_page=41) ---
    max_items = 1000        # stop after this many unique property IDs
    start_page = 1          # first search page (resumability)
    page_size = 50          # IDs per search page - 50 is comfortably tolerated

    # Observed: the search API needs ~100s after a 429 before the limit
    # resets, so the first retry waits that long (see middlewares.py;
    # growth is capped there to stay polite but not glacial).
    initial_backoff_secs = 100

    checkpoint_dir = "checkpoints"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for attr in ("max_items", "start_page", "page_size"):
            val = getattr(self, attr, None)
            if isinstance(val, str):
                setattr(self, attr, int(val))
        self.id_count = 0
        self.seen_ids = set()

        # resume=1 continues from checkpoints/nawy.ckpt.json
        self.resume = str(kwargs.get("resume", "")).lower() in ("1", "true", "yes")
        # reset_output=1 (only on non-resume runs) clears the unified output file
        self.reset_output = str(kwargs.get("reset_output", "")).lower() in (
            "1", "true", "yes"
        )
        self.cp = Checkpoint.load(self.name, directory=self.checkpoint_dir)
        if not self.resume and self.cp.has_progress:
            # fresh run: ignore any stale checkpoint left behind
            self.cp = Checkpoint(
                self.name, last_page=0, keys=set(), pending=set(),
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
        if self.resume and self.cp.pending:
            # replay detail requests that were in flight when we died
            self.logger.info(
                f"Resuming: replaying {len(self.cp.pending)} in-flight detail IDs"
            )
            for prop_id in sorted(self.cp.pending):
                yield self._detail_request(prop_id)
            self.cp.save()
        if self.resume:
            first_page = max(int(self.start_page), self.cp.last_page + 1)
            if self.cp.last_page:
                self.logger.info(
                    f"Resuming after search page {self.cp.last_page} "
                    f"({len(self.cp.keys)} IDs already scraped)"
                )
        else:
            first_page = int(self.start_page)
        yield self._search_request(first_page)

    def _search_request(self, page_num):
        return scrapy.Request(
            url=f"{self.search_api}?page={page_num}&pageSize={self.page_size}",
            callback=self.parse_search,
            meta={"nw_page_num": page_num},
        )

    # ------------------------------------------------------------------
    # Phase 1: search pages -> property IDs
    # ------------------------------------------------------------------
    def parse_search(self, response):
        data = _load_json(response)
        if data is None:
            return

        results = data.get("results") or []
        if not results:
            self.logger.info("No more results on search page - done.")
            self.cp.mark_page(response.meta.get("nw_page_num", 1))
            self.cp.save()
            return

        page_num = response.meta.get("nw_page_num", 1)
        new_ids_this_page = 0
        skipped_by_checkpoint = 0
        for prop in results:
            if self.id_count >= self.max_items:
                self.logger.info(
                    f"Reached max_items={self.max_items}. Stopping."
                )
                break
            prop_id = prop.get("id")
            if prop_id is None or prop_id in self.seen_ids:
                continue
            # checkpoint may already know this ID from a previous run
            if self.resume and self.cp.seen(prop_id):
                skipped_by_checkpoint += 1
                continue
            self.seen_ids.add(prop_id)
            self.cp.add_pending(prop_id)
            self.id_count += 1
            new_ids_this_page += 1
            yield self._detail_request(prop_id)

        self.cp.mark_page(page_num)
        self.cp.save_if_due()

        # --- paginate search ---
        # Guard 1: a page where all IDs were already seen in *this* run means
        # the API is repeating itself - walking further pages cannot add
        # anything. Do not stop if IDs were only skipped by an earlier
        # checkpoint on resume=1.
        if new_ids_this_page == 0 and skipped_by_checkpoint == 0:
            self.logger.info(
                f"Search page {page_num} yielded no new IDs - "
                f"stopping pagination."
            )
            return
        if self.id_count < self.max_items:
            total_items = data.get("total") or 0
            if (page_num * self.page_size) < total_items and self.id_count < total_items:
                yield self._search_request(page_num + 1)

    def _detail_request(self, prop_id):
        return scrapy.Request(
            url=self.detail_api.format(property_id=prop_id),
            callback=self.parse_detail,
            meta={"nw_prop_id": prop_id},
        )

    # ------------------------------------------------------------------
    # Phase 2: detail payload -> ReaItem
    # ------------------------------------------------------------------
    def parse_detail(self, response):
        data = _load_json(response)
        if data is None:
            self.logger.warning(
                f"Unparseable detail payload for id "
                f"{response.meta.get('nw_prop_id')} - skipping."
            )
            return
        if not isinstance(data, dict):
            self.logger.warning(
                f"Detail payload for id "
                f"{response.meta.get('nw_prop_id')} is not an object - skipping."
            )
            return

        # Some detail endpoints wrap the payload; accept both shapes.
        if isinstance(data, dict) and "data" in data and "id" not in data:
            data = data.get("data") or {}

        item = ReaItem()

        # --- ScrapedListing fields ---
        item["platform"] = self.platform_name
        item["source_id"] = data.get("id") or response.meta.get("nw_prop_id")
        item["source_url"] = data.get("shareLink")
        item["listing_ref"] = None            # Nawy does not expose one
        item["listed_date"] = None            # not exposed by the API
        item["broker_name"] = None            # Nawy listings are developer-first
        item["agent_name"] = None
        item["developer_name"] = data.get("developerName")
        item["title_raw"] = data.get("name") or data.get("metaTitle")

        # --- Property fields ---
        item["property_type"] = (data.get("propertyType") or {}).get("name")
        item["description"] = utils.strip_html(data.get("description") or "")
        item["size_m2"] = utils.to_int(data.get("minUnitArea"))
        item["plot_size_m2"] = utils.to_int(data.get("maxLandArea"))
        item["bedrooms"] = utils.to_int(data.get("numberOfBedrooms"))
        item["bathrooms"] = utils.to_int(data.get("numberOfBathrooms"))
        item["finishing"] = data.get("finishing")
        item["furnished"] = None
        item["delivery_year"] = _delivery_year(data.get("deliveryDate"))
        item["completion_status"] = (
            "ready" if data.get("resale") else "off_plan_primary"
        )
        item["resale"] = bool(data.get("resale"))
        item["has_nanny_room"] = bool(data.get("hasNannyRoom"))
        item["features"] = _amenity_names(data.get("amenities"))
        item["payment_methods"] = ["installments"]  # Nawy is installment-first

        # --- Financial fields ---
        item["price_egp"] = utils.to_int(data.get("price"))
        item["currency"] = data.get("currency") or "EGP"
        plans = data.get("paymentPlans") or []
        item["down_payment_egp"] = utils.to_int(
            min((p.get("downPayment") for p in plans if p.get("downPayment")),
                default=None)
        ) if plans else None
        item["installment_years"] = max(
            (utils.to_int(p.get("years")) for p in plans if p.get("years")),
            default=None,
        ) if plans else None
        item["payment_frequency"] = plans[0].get("frequency") if plans else None
        item["price_per_m2"] = None      # ETL computes from price / size

        # --- Location fields ---
        compound = data.get("compound") or {}
        item["governorate"] = None       # areaName is city-level; ETL maps it
        item["city"] = data.get("areaName")
        item["compound"] = compound.get("name")
        geometry = compound.get("geometry")
        item["compound_geometry"] = geometry if geometry else None
        lat, lon = utils.geojson_centroid(geometry)
        if lat is None:
            # fall back to explicit coords when the polygon is missing
            lat, lon = utils.sanitize_coords(
                compound.get("latitude"), compound.get("longitude")
            )
        item["latitude"], item["longitude"] = lat, lon

        # --- Media fields (URLs only) ---
        floor_plans = _dedupe_strs(data.get("floorPlans"))
        master_plan = _dedupe_strs([data.get("mapPath")])
        photos = _dedupe_strs(data.get("images"))
        item["floor_plan_urls"] = floor_plans
        item["master_plan_url"] = master_plan[0] if master_plan else None
        item["image_urls"] = utils.capped_image_urls(
            floor_plans, master_plan, photos
        )

        # Checkpoint mark_done happens in UnifiedDeduplicationPipeline
        # once the item is persisted or confirmed present on disk.
        yield item


# ----------------------------------------------------------------------
# payload helpers
# ----------------------------------------------------------------------
def _load_json(response):
    """Parse a raw-JSON or HTML-wrapped (Playwright <pre>) response."""
    ctype = response.headers.get("Content-Type", b"")
    try:
        if ctype.startswith(b"text/html"):
            return json.loads(
                response.xpath("//pre/text()").get() or response.text
            )
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None  # caller logs; Scrapy still retries transport errors


def _delivery_year(value):
    """'2030-09-29T00:00:00.000Z' -> 2030"""
    if value and isinstance(value, str):
        try:
            return int(value[:4])
        except ValueError:
            return None
    return None


def _amenity_names(amenities):
    return [
        a.get("name") for a in (amenities or []) if isinstance(a, dict) and a.get("name")
    ]


def _dedupe_strs(values):
    return [v for v in (values or []) if isinstance(v, str) and v.strip()]