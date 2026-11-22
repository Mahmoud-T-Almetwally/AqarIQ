"""AqarIQ ETL — batch processing for the static dataset pivot.

The architecture document's Stages 3–5 (normalization, feature extraction,
deduplication) reworked as a local, replayable batch pipeline over the frozen
``scrappers/unified_output.jsonl`` snapshot. The RabbitMQ consumer stays
deferred: if local re-crawls ever resume, the same normalizer and
deduplicator run behind the queue unchanged.

Module map (see ``docs`` / phase plan for the full stage table):

* :mod:`src.etl.contracts`     — Pydantic raw/clean contracts + content hashes
* :mod:`src.etl.arabic`        — bilingual text hygiene (Stage 2)
* :mod:`src.etl.vocab`         — canonical vocabularies (Stage 3)
* :mod:`src.etl.geo`           — location canonicalization (Stage 4)
* :mod:`src.etl.financials`    — financial integrity (Stage 5)
* :mod:`src.etl.extractor`     — regex + LLM field completion (Stage 6)
* :mod:`src.etl.deduplicator`  — entity resolution (Stage 7)
* :mod:`src.etl.normalizer`    — per-row orchestration of Stages 1–4
* :mod:`src.etl.pipeline`      — batch orchestrator with stage checkpoints

Every module is pure computation over dicts/Pydantic models (no I/O) so the
whole suite runs offline in CI on the committed fixture; only
:mod:`src.etl.pipeline` and ``scripts/seed_db.py`` touch files or the
database.
"""

from src.etl.flags import Flag

__all__ = ["Flag"]
