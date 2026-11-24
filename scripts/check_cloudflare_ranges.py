"""Compare the Cloudflare ranges in the API with Cloudflare's published list (MIP-2235).

    python scripts/check_cloudflare_ranges.py

Exit 0 when they match, 1 when Cloudflare added or removed a range (update CLOUDFLARE_RANGES in
origin/middleware/real_client_ip.py in the same PR), 2 when the list could not be fetched.
A range missing from the code makes real visitors share one address (their requests look like
they came from Cloudflare's edge); an extra one is harmless. Run it monthly or when Cloudflare
announces a change. Read-only: it fetches two public URLs and prints ranges, nothing else.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from origin.middleware.real_client_ip import CLOUDFLARE_RANGES  # noqa: E402

URLS = ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6")


def fetch_live() -> set[str]:
    live: set[str] = set()
    for url in URLS:
        # Cloudflare answers 403 to urllib's default User-Agent.
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URLs
            live |= {line.strip() for line in resp.read().decode().splitlines() if line.strip()}
    return live


def main() -> int:
    try:
        live = fetch_live()
    except Exception as exc:  # noqa: BLE001
        print(f"could not fetch the Cloudflare ranges: {exc}")
        return 2
    ours = set(CLOUDFLARE_RANGES)
    missing, extra = sorted(live - ours), sorted(ours - live)
    if not missing and not extra:
        print(f"ok: {len(ours)} ranges match")
        return 0
    for r in missing:
        print(f"MISSING in code (add it): {r}")
    for r in extra:
        print(f"no longer published (remove it): {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
