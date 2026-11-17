"""Crash-safe checkpointing for the two spiders.

A checkpoint is a small JSON file per spider holding:
  - the last fully processed search page, and
  - every listing key (property id / listing URL) already scraped.

On resume the spider restarts phase 1 at ``last_page + 1``, replays any
unfinished ``pending`` detail keys, and skips keys already in ``keys``.
Both fresh and resumed runs append to the same unified JSONL output
managed by ``UnifiedDeduplicationPipeline``, and the pipeline reconciles
already-persisted keys back into the checkpoint on startup.

Writes are atomic (tmp file + rename) and debounced (default every
2s or 50 new keys) so a kill -9 or a WAF hard-close never loses more
than a few seconds of progress and never corrupts the checkpoint.
"""
import json
import os
import time

CHECKPOINT_DIR = "checkpoints"


class Checkpoint:
    """Load/save progress for one spider run."""

    #: throttle full saves to one per SAVE_INTERVAL secs (or SAVE_EVERY keys)
    SAVE_INTERVAL = 2.0
    SAVE_EVERY = 50

    def __init__(self, spider_name, last_page, keys, pending, path):
        self.spider_name = spider_name
        self.last_page = last_page
        self.keys = set(keys)
        self.pending = set(pending)
        self.path = path
        self._dirty = False
        self._last_save = time.monotonic()
        self._keys_since_save = 0

    @property
    def has_progress(self):
        return bool(self.last_page or self.keys or self.pending)

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, spider_name, directory=None):
        """Load checkpoint for ``spider_name``; fresh state if none exists.

        Never raises: a corrupt/unreadable checkpoint logs and resets,
        because re-scraping some IDs is cheap but a dead spider is not.
        """
        path = cls._path_for(spider_name, directory)
        if not os.path.exists(path):
            return cls(spider_name, last_page=0, keys=set(), pending=set(),
                       path=path)

        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return cls(
                spider_name,
                last_page=int(data.get("last_page") or 0),
                keys=set(data.get("keys") or []),
                pending=set(data.get("pending") or []),
                path=path,
            )
        except (ValueError, OSError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Checkpoint %s unreadable (%s) - starting fresh.", path, exc
            )
            return cls(spider_name, last_page=0, keys=set(), pending=set(),
                       path=path)

    @staticmethod
    def _path_for(spider_name, directory=None):
        directory = directory or CHECKPOINT_DIR
        return os.path.join(directory, f"{spider_name}.ckpt.json")

    # ------------------------------------------------------------------
    # recording progress
    # ------------------------------------------------------------------
    def add_pending(self, key):
        """Record a detail key as enqueued but not yet completed."""
        key = str(key)
        if key not in self.pending and key not in self.keys:
            self.pending.add(key)
            self._dirty = True

    def mark_done(self, key):
        """Record one listing key as persisted/deduplicated in the unified output."""
        key = str(key)
        if key not in self.keys:
            self.keys.add(key)
            self._dirty = True
            self._keys_since_save += 1
        if key in self.pending:
            self.pending.discard(key)
            self._dirty = True

    def reconcile_with_existing(self, existing_keys):
        """Ensure any keys already persisted in the unified file are marked
        done and removed from pending."""
        added = 0
        for key in existing_keys:
            skey = str(key)
            if skey not in self.keys:
                self.keys.add(skey)
                added += 1
                self._dirty = True
            if skey in self.pending:
                self.pending.discard(skey)
                self._dirty = True
        return added

    def mark_page(self, page_num):
        """Record search page ``page_num`` as fully processed."""
        if page_num > self.last_page:
            self.last_page = page_num
            self._dirty = True

    def seen(self, key):
        return str(key) in self.keys

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def save_if_due(self):
        """Debounced save - call after each item, costs ~nothing."""
        if not self._dirty:
            return False
        due_by_time = (time.monotonic() - self._last_save) >= self.SAVE_INTERVAL
        due_by_keys = self._keys_since_save >= self.SAVE_EVERY
        if due_by_time or due_by_keys:
            return self.save()
        return False

    def save(self):
        """Unconditional, atomic save. Returns True on success."""
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "last_page": self.last_page,
                        "keys": sorted(self.keys),
                        "pending": sorted(self.pending),
                    },
                    fh,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            self.logger_warning(str(exc))
            return False
        self._dirty = False
        self._last_save = time.monotonic()
        self._keys_since_save = 0
        return True

    def logger_warning(self, msg):
        import logging

        logging.getLogger(__name__).warning(
            "Checkpoint save failed for %s: %s", self.spider_name, msg
        )