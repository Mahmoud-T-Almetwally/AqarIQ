# Define here the models for your spider middleware
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/spider-middleware.html

from scrapy import signals
from itemadapter import ItemAdapter

from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.utils.response import response_status_message


class ReaSpiderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the spider middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_input(self, response, spider):
        # Called for each response that goes through the spider
        # middleware and into the spider.

        # Should return None or raise an exception.
        return None

    def process_spider_output(self, response, result, spider):
        # Called with the results returned from the Spider, after
        # it has processed the response.

        # Must return an iterable of Request, or item objects.
        for i in result:
            yield i

    def process_spider_exception(self, response, exception, spider):
        # Called when a spider or process_spider_input() method
        # (from other spider middleware) raises an exception.

        # Should return either None or an iterable of Request or item objects.
        pass

    async def process_start(self, start):
        # Called with an async iterator over the spider start() method or the
        # matching method of an earlier spider middleware.
        async for item_or_request in start:
            yield item_or_request

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class ReaDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request, spider):
        # Called for each request that goes through the downloader
        # middleware.

        # Must either:
        # - return None: continue processing this request
        # - or return a Response object
        # - or return a Request object
        # - or raise IgnoreRequest: process_exception() methods of
        #   installed downloader middleware will be called
        return None

    def process_response(self, request, response, spider):
        # Called with the response returned from the downloader.

        # Must either;
        # - return a Response object
        # - return a Request object
        # - or raise IgnoreRequest
        return response

    def process_exception(self, request, exception, spider):
        # Called when a download handler or a process_request()
        # (from other downloader middleware) raises an exception.

        # Must either:
        # - return None: continue processing this exception
        # - return a Response object: stops process_exception() chain
        # - return a Request object: stops process_exception() chain
        pass

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)

class ExponentialBackoffRetryMiddleware(RetryMiddleware):
    """Retry middleware with a long, configurable 429 backoff.

    For non-429 retryable statuses this behaves exactly like Scrapy's
    RetryMiddleware. For 429 it waits before re-queuing the request:

      delay = initial * growth ** (retry - 1), capped at max_backoff

    Per-spider knobs (spider attributes, overridable with -a):

      initial_backoff_secs   first 429 wait. Nawy needs ~100s (observed
                             API limit-reset window); the default stays
                             small (2s) so other spiders are unaffected.
      backoff_growth_factor  multiplier per subsequent 429 (default 2).
      max_backoff_secs       ceiling so a bad night doesn't sleep until
                             morning (default 600s).
    """

    default_initial_backoff_secs = 2
    default_backoff_growth_factor = 2
    default_max_backoff_secs = 600

    def _backoff_delay(self, spider, retries):
        initial = getattr(
            spider, "initial_backoff_secs", self.default_initial_backoff_secs
        )
        growth = getattr(
            spider, "backoff_growth_factor", self.default_backoff_growth_factor
        )
        cap = getattr(spider, "max_backoff_secs", self.default_max_backoff_secs)
        delay = float(initial) * (float(growth) ** max(retries - 1, 0))
        return min(delay, float(cap))

    async def process_response(self, request, response, spider):
        if request.meta.get('dont_retry', False):
            return response

        # If the status code is in our retry list (e.g., 429, 500, 503)
        if response.status in self.retry_http_codes:
            reason = response_status_message(response.status)

            # self._retry creates a new request with an incremented 'retry_times' count
            retry_req = self._retry(request, reason)

            if retry_req:
                # Apply long exponential backoff strictly for 429 Too Many Requests
                if response.status == 429:
                    retries = retry_req.meta.get('retry_times', 1)
                    delay = self._backoff_delay(spider, retries)

                    spider.logger.warning(
                        f"429 Too Many Requests on {request.url}. "
                        f"Backing off for {delay:.0f} seconds "
                        f"(attempt {retries})..."
                    )

                    # Wait outside the global reactor if one is running
                    # (the asyncio reactor forbids blocking sleeps; a
                    # plain Deferred from reactor.callLater still works
                    # but is deprecated as a middleware return value).
                    try:
                        import asyncio

                        await asyncio.sleep(delay)
                    except RuntimeError:
                        # no running event loop in this context (e.g.
                        # sync test harness): fall back to callLater
                        from twisted.internet import reactor as _reactor
                        from twisted.internet.defer import Deferred

                        d = Deferred()
                        _reactor.callLater(delay, d.callback, None)
                        await d

                    return retry_req

                # For standard errors (500, 502), retry normally without massive delays
                return retry_req

        return response