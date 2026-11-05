"""Database package: models + engine/session management (Tier 2, Neon).

``models`` mirrors the final post-migration schema (Alembic 0001-0004 is
the source of truth — never ``Base.metadata.create_all``); ``session``
builds the Neon-aware async engine (pooler detection, per-transaction
statement timeout, pre-ping against autosuspend) and the session
discipline used by every later layer.
"""

from src.db.models import (
    EMBEDDING_DIMENSIONS,
    Base,
    ChatMessage,
    ChatSession,
    Financial,
    Location,
    MarketStat,
    Property,
    ScrapedListing,
)
from src.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
    engine_kwargs,
    ping_database,
    session_scope,
)

__all__ = [
    # Schema
    "Base",
    "ChatMessage",
    "ChatSession",
    "Financial",
    "Location",
    "MarketStat",
    "Property",
    "ScrapedListing",
    "EMBEDDING_DIMENSIONS",
    # Engine / sessions
    "create_db_engine",
    "create_session_factory",
    "dispose_engine",
    "engine_kwargs",
    "ping_database",
    "session_scope",
]
