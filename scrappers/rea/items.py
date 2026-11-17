import scrapy


class ReaItem(scrapy.Item):
    """One scraped listing, platform-agnostic.

    Media policy (demo pivot): image URLs only, hard-capped at
    ``MAX_IMAGE_URLS`` (utils) because the embedding API batches at most
    6 images per request. Floor plans and the master-plan map take
    priority over brochure photos (see utils.capped_image_urls) - they
    carry the layout/geometry signal a visual similarity search needs.
    Nothing is downloaded or stored by the spiders; a later batch job
    fetches these URLs just-in-time for embedding.
    """

    # --- ScrapedListing fields ---
    platform = scrapy.Field()        # "Nawy" | "PropertyFinder"
    source_id = scrapy.Field()       # portal's own id (dedup / reconciliation key)
    source_url = scrapy.Field()
    listing_ref = scrapy.Field()     # PF "reference" (broker's ad reference)
    listed_date = scrapy.Field()     # ISO timestamp when the ad appeared
    broker_name = scrapy.Field()
    agent_name = scrapy.Field()      # PF listing agent
    developer_name = scrapy.Field()  # Nawy developer / PF direct-from-developer
    title_raw = scrapy.Field()

    # --- Property fields ---
    property_type = scrapy.Field()
    description = scrapy.Field()     # plain text (HTML stripped at extraction)
    size_m2 = scrapy.Field()
    plot_size_m2 = scrapy.Field()    # PF: total plot; Nawy: land area if present
    bedrooms = scrapy.Field()        # int
    bathrooms = scrapy.Field()       # int
    finishing = scrapy.Field()       # "Fully Finished" / "Semi Finished" / ...
    furnished = scrapy.Field()       # PF only: "YES" / "NO"
    delivery_year = scrapy.Field()   # int
    completion_status = scrapy.Field()  # PF: "off_plan_primary" / "ready" / ...
    resale = scrapy.Field()          # bool (Nawy)
    has_nanny_room = scrapy.Field()  # bool (Nawy)
    features = scrapy.Field()        # list[str] amenity names
    payment_methods = scrapy.Field()    # PF: ["cash", "installments"]

    # --- Financial fields ---
    price_egp = scrapy.Field()
    currency = scrapy.Field()
    down_payment_egp = scrapy.Field()
    installment_years = scrapy.Field()      # longest plan offered (int)
    payment_frequency = scrapy.Field()      # Nawy: "quarterly" / "monthly"
    price_per_m2 = scrapy.Field()           # PF provides it; ETL computes for Nawy

    # --- Location fields ---
    governorate = scrapy.Field()
    city = scrapy.Field()
    compound = scrapy.Field()
    latitude = scrapy.Field()        # float | None (Nawy: compound centroid)
    longitude = scrapy.Field()       # float | None
    compound_geometry = scrapy.Field()  # Nawy: GeoJSON polygon dict of the compound

    # --- Media fields (URLs only, nothing downloaded) ---
    image_urls = scrapy.Field()         # <= 6 URLs, floor plans & map first
    floor_plan_urls = scrapy.Field()    # full list (usually 0-2, not capped)
    master_plan_url = scrapy.Field()    # Nawy mapPath (compound master plan)

    def __repr__(self):
        return repr({
            "platform": self.get("platform"),
            "source_id": self.get("source_id"),
            "title": self.get("title_raw"),
        })
