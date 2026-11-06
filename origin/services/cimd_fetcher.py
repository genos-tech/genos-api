"""A client-metadata-document fetcher that cannot hold a request thread (MIP-1077).

The library's fetcher claims a 3 s total deadline, but urllib3 applies `total` to each socket
read, not to the whole exchange: a server that sends its headers and then one byte every 2.9 s
keeps the fetch alive for hours. The fetch runs inside the authorize request, so a handful of
such documents would pin every thread of a one-worker API.

So the fetch runs in its own thread and the request waits for it with a real deadline. A fetch
that overruns is abandoned (the request is answered, the thread ends when its socket does), and
the number of such threads is capped: past the cap a new fetch is refused at once, so the worst
an attacker can do is stop metadata-document clients from signing in for a while. Registered
clients and everyone else are untouched.
"""

from __future__ import annotations

import threading

from oauth2_provider.cimd import CIMDError, SafeMetadataFetcher
from oauth2_provider.settings import oauth2_settings

# Fetches in flight at once, abandoned ones included. Small on purpose: a real client's
# document is fetched once and cached, so this is plenty for honest use.
MAX_IN_FLIGHT = 4
# The request waits this much longer than the library's own timeout, for connection set-up.
DEADLINE_PADDING = 1.0

_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)


def _deadline() -> float:
    return float(oauth2_settings.CIMD_FETCH_TIMEOUT_SECONDS) + DEADLINE_PADDING


class BoundedFetcher(SafeMetadataFetcher):
    def fetch(self, client_id):
        if not _slots.acquire(blocking=False):
            raise CIMDError("too many client metadata fetches in flight")
        outcome: dict = {}

        def work():
            try:
                outcome["value"] = SafeMetadataFetcher.fetch(self, client_id)
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting request
                outcome["error"] = exc
            finally:
                _slots.release()

        thread = threading.Thread(target=work, name="cimd-fetch", daemon=True)
        thread.start()
        thread.join(_deadline())
        if thread.is_alive():
            raise CIMDError("fetching the client metadata document timed out")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]
