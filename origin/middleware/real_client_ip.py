"""The real client address when Cloudflare is in front of the API (MIP-2235, decision D3).

Chain with Cloudflare proxying `api.genosai.dev`:

    client -> Cloudflare edge -> Railway edge -> gunicorn (Django)

Railway appends the address of the peer that connected to it to `X-Forwarded-For`. With
Cloudflare in front, that peer is a Cloudflare edge address, so `REST_FRAMEWORK["NUM_PROXIES"] = 1`
(which makes DRF read the LAST `X-Forwarded-For` entry) would give every user on a Cloudflare
point of presence the SAME address: one attacker could exhaust the sign-in bucket for everyone,
and every `SecurityEvent.ip` would be a Cloudflare address.

Cloudflare puts the visitor's address in `CF-Connecting-IP` and overwrites any value the client
sent. That header is only believable when the connection really came from Cloudflare, and the
only thing that proves it is the address Railway saw. So:

* the last `X-Forwarded-For` entry (the one Railway wrote, which a client cannot forge) is inside
  a published Cloudflare range  -> the request came through Cloudflare, `CF-Connecting-IP` is
  trusted, and `X-Forwarded-For` is REPLACED by that one address, so everything downstream
  (`NUM_PROXIES = 1`, `BaseThrottle.get_ident`, `security_events.client_ip`) keeps working
  unchanged;
* anything else (a request that bypassed Cloudflare and went to the Railway hostname directly, a
  local call, a forged header) -> nothing is touched, and `CF-Connecting-IP` is ignored. A
  client cannot pick its address by sending the header itself.

Rollback is a DNS change (turn the orange cloud grey): without Cloudflare the last entry is not a
Cloudflare address and this middleware does nothing. `TRUST_CLOUDFLARE_IP=0` switches it off
without a deploy-time code change, for the case where the range list below is wrong.

The ranges are Cloudflare's published list (https://www.cloudflare.com/ips-v4 and `ips-v6`). They
change rarely and Cloudflare announces changes in advance; `scripts/check_cloudflare_ranges.py`
compares this list with the live one (run it by hand or monthly). Only counts and a scope are
logged, never an address.
"""

from __future__ import annotations

import ipaddress
import os

CLOUDFLARE_RANGES: tuple[str, ...] = (
    # https://www.cloudflare.com/ips-v4
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    # https://www.cloudflare.com/ips-v6
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_NETWORKS = tuple(ipaddress.ip_network(r) for r in CLOUDFLARE_RANGES)


def _parse(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def is_cloudflare_address(value: str | None) -> bool:
    addr = _parse(value)
    if addr is None:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in _NETWORKS)


def resolve_forwarded_for(xff: str | None, cf_connecting_ip: str | None) -> str | None:
    """The `X-Forwarded-For` value to use, or None to leave the request alone."""
    if not xff or not cf_connecting_ip:
        return None
    last_hop = xff.split(",")[-1].strip()
    if not is_cloudflare_address(last_hop):
        return None
    visitor = _parse(cf_connecting_ip)
    # One plain address only: a list, a port or a name is not what Cloudflare sends.
    if visitor is None or "," in cf_connecting_ip:
        return None
    return str(visitor)


def _enabled() -> bool:
    return os.environ.get("TRUST_CLOUDFLARE_IP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class RealClientIPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _enabled():
            meta = request.META
            replacement = resolve_forwarded_for(
                meta.get("HTTP_X_FORWARDED_FOR"), meta.get("HTTP_CF_CONNECTING_IP")
            )
            if replacement is not None:
                meta["HTTP_X_FORWARDED_FOR"] = replacement
        return self.get_response(request)
