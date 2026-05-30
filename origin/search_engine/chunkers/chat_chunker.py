"""Chat chunker — stubbed during the legacy chat retirement.

The original implementation sourced from the per-type chat tables
(`DMMessages` / `GMMessages` / `MDMMessages` / `PMMessages`) and their
`*ThreadMessages` siblings. Phase 3 of the legacy retirement dropped
those tables; until the OpenSearch indexer is rewritten to source from
`Channel`/`Message`, this module yields no chunks so the ingestion
pipeline keeps working (it just doesn't index chat data).

When the v3 rewrite lands, replace this with a single `iter_chunks`
that scans `Message.objects.filter(deleted_at__isnull=True)` grouped
by `Channel`.
"""

from datetime import datetime
from typing import Iterator, Optional

from origin.search_engine.chunkers.base import EntityChunks


def iter_dm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    return iter(())


def iter_gm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    return iter(())


def iter_mdm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    return iter(())


def iter_pm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    return iter(())


def iter_all_chat_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    return iter(())
