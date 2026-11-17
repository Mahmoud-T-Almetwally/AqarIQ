BOT_NAME = "rea"

SPIDER_MODULES = ["rea.spiders"]
NEWSPIDER_MODULE = "rea.spiders"

ADDONS = {}

RETRY_HTTP_CODES = [500, 502, 504, 522, 524, 408]  # 503 removed

ROBOTSTXT_OBEY = False

# Unified output + real-time deduplication pipeline
ITEM_PIPELINES = {
    "rea.pipelines.UnifiedDeduplicationPipeline": 300,
}
UNIFIED_OUTPUT_FILE = "unified_output.jsonl"

# Let scrapy-playwright send the browser's own, coherent headers on every
# sub-request. Scrapy's DEFAULT_REQUEST_HEADERS would otherwise *replace*
# the real Chrome header set on navigations (dropping sec-ch-ua* client
# hints, forcing Sec-Fetch-Site: none on every page) - an inconsistent
# fingerprint that AWS WAF picks up. The UA is set on the context below.
PLAYWRIGHT_PROCESS_REQUEST_HEADERS = None

# Pacing: the WAF rules are largely rate-based. Human-scale delays are the
# single most effective anti-block measure, and cost nothing.
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 4.0
AUTOTHROTTLE_MAX_DELAY = 20.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
DOWNLOAD_DELAY = 2.0
RANDOMIZE_DOWNLOAD_DELAY = True

CONCURRENT_REQUESTS = 2
CONCURRENT_REQUESTS_PER_DOMAIN = 2

# Ensure the output respects unicode (for Arabic text in raw_jsonb / titles)
FEED_EXPORT_ENCODING = 'utf-8'

# Register the Playwright handler for https ONLY. Scrapy instantiates one
# handler per scheme, so registering http as well would create a second
# instance - and with a persistent context (user_data_dir below) two
# Chromium processes would fight over the same profile directory.
DOWNLOAD_HANDLERS = {
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

# Ensure Scrapy uses asyncio (required for Playwright)
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

# Configure the Playwright browser
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": True,
    "args": [
        "--disable-blink-features=AutomationControlled",  # hides navigator.webdriver
    ],
}

# One named, persistent browser context ("default"). user_data_dir makes it
# a real Chrome profile on disk: cookies - including the aws-waf-token that
# authorises pagination - survive between runs, so the next scrape starts
# already trusted instead of re-earning trust from a cold session.
# Delete .scrapy_pf_profile/ to reset the session if it ever goes bad.
PLAYWRIGHT_CONTEXTS = {
    "default": {
        "user_data_dir": ".scrapy_pf_profile",
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"],
        # A complete, self-consistent browser identity. Must match the
        # actual Chromium being driven (Playwright's bundled build).
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
        "timezone_id": "Africa/Cairo",
        "viewport": {"width": 1366, "height": 768},
    },
}

# Set maximum concurrent Playwright contexts to prevent memory spikes
PLAYWRIGHT_MAX_CONTEXTS = 1
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 2