"""Scrapy item pipeline for unified output, deduplication, and checkpoint sync."""
import os

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

from rea.dedup import UnifiedListingStore


class UnifiedDeduplicationPipeline:
    """Append unique items from all spiders into one deduplicated JSONL file."""

    def __init__(self, output_file: str, crawler):
        self.output_file = output_file
        self.crawler = crawler
        self.store = UnifiedListingStore(output_file)
        self.added_count = 0
        self.dropped_count = 0

    @classmethod
    def from_crawler(cls, crawler):
        output_file = crawler.settings.get(
            "UNIFIED_OUTPUT_FILE", "unified_output.jsonl"
        )
        return cls(output_file=output_file, crawler=crawler)

    def open_spider(self, spider):
        if getattr(spider, "reset_output", False) and not getattr(
            spider, "resume", False
        ):
            spider.logger.info(
                f"reset_output=1: clearing unified output file {self.output_file}"
            )
            self.store.reset()

        self.store.sync()
        spider.logger.info(
            f"Unified store ready ({self.output_file}): "
            f"{self.store.index.total_count} existing listings indexed."
        )

        # Reconcile spider checkpoint with what is already persisted on disk
        cp = getattr(spider, "cp", None)
        platform = getattr(spider, "platform_name", None)
        if cp is not None and platform and getattr(spider, "resume", False):
            if not os.path.exists(self.output_file) and cp.has_progress:
                spider.logger.warning(
                    f"resume=1 requested, but {self.output_file} does not exist yet."
                )
            existing_ids = self.store.existing_ids_for_platform(platform)
            reconciled = cp.reconcile_with_existing(existing_ids)
            if reconciled:
                spider.logger.info(
                    f"Reconciled {reconciled} already-persisted {platform} IDs "
                    f"from {self.output_file} into checkpoint."
                )
                cp.save()

    def process_item(self, item, spider):
        record = ItemAdapter(item).asdict()
        added, dup_reason = self.store.add_if_unique(record)

        # Mark checkpoint done ONLY after disk append or confirmed presence
        cp = getattr(spider, "cp", None)
        source_id = record.get("source_id")
        if cp is not None and source_id is not None:
            cp.mark_done(source_id)
            cp.save_if_due()

        if not added:
            self.dropped_count += 1
            self.crawler.stats.inc_value("dedup/dropped_count")
            reason_kind = dup_reason.split(":", 1)[0]
            self.crawler.stats.inc_value(f"dedup/dropped_{reason_kind}")
            raise DropItem(
                f"Duplicate listing ({dup_reason}): "
                f"platform={record.get('platform')} id={source_id}"
            )

        self.added_count += 1
        self.crawler.stats.inc_value("dedup/added_count")
        return item

    def close_spider(self, spider):
        spider.logger.info(
            f"Unified output summary for {spider.name}: "
            f"{self.added_count} added, {self.dropped_count} duplicates skipped, "
            f"{self.store.index.total_count} total in {self.output_file}."
        )