"""Add new mapping fields to the live index before the first write.

The index maps unknown fields dynamically. A field added to `build_mappings`
but not yet put on the live index (`opensearch_setup --update-mapping` is a
manual step) would be auto-mapped as `text` by the first chunk that carries
it — and a field's type can't be changed afterwards without a reindex into a
new index. So every process puts the missing fields once, before it writes.
Only fields absent from the live mapping are sent, exactly as
`opensearch_setup --update-mapping` does; nothing existing is touched.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_done = False


def ensure_additive_mapping(client, index: str) -> None:
    """Put the fields `build_mappings` declares that `index` (an alias or a
    physical index) lacks. Once per process; a failure is logged and retried
    on the next write rather than blocking it."""
    global _done
    if _done:
        return
    with _lock:
        if _done:
            return
        from origin.search_engine.index_config import build_mappings  # noqa: PLC0415

        desired = build_mappings()["properties"]
        try:
            live = client.indices.get_mapping(index=index)
            for physical, body in live.items():
                props = (body or {}).get("mappings", {}).get("properties", {})
                missing = {k: v for k, v in desired.items() if k not in props}
                if missing:
                    log.info(
                        "Adding %d mapping field(s) to %s: %s",
                        len(missing),
                        physical,
                        ", ".join(sorted(missing)),
                    )
                    client.indices.put_mapping(index=physical, body={"properties": missing})
            _done = True
        except Exception:  # noqa: BLE001 — never block a write on this
            log.warning("Additive mapping check failed; will retry", exc_info=True)


def reset_for_tests() -> None:
    global _done
    _done = False
