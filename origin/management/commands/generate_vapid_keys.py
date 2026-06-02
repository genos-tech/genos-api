"""Generate a VAPID keypair for Web Push.

Prints the public + private keys as base64url strings — the same format
`npx web-push generate-vapid-keys` produces and that `pywebpush` accepts.
The private key is a SECRET: set it via env (never commit it).

Usage:
    python manage.py generate_vapid_keys

Then set:
    WEBPUSH_VAPID_PUBLIC_KEY=<public>      (backend)
    WEBPUSH_VAPID_PRIVATE_KEY=<private>    (backend, secret)
    VITE_VAPID_PUBLIC_KEY=<public>         (frontend, same as public)
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Generate a VAPID keypair (base64url) for Web Push."

    def handle(self, *args, **options):
        private_key = ec.generate_private_key(ec.SECP256R1())

        # Private: the raw 32-byte scalar, base64url (web-push standard).
        priv_scalar = private_key.private_numbers().private_value
        priv_b64 = _b64url(priv_scalar.to_bytes(32, "big"))

        # Public: the uncompressed point (65 bytes: 0x04 || X || Y),
        # base64url — this is the `applicationServerKey` the browser uses.
        pub_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        pub_b64 = _b64url(pub_bytes)

        self.stdout.write(self.style.SUCCESS("VAPID keypair generated.\n"))
        self.stdout.write("# Backend env (private key is SECRET — do not commit):")
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={pub_b64}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={priv_b64}")
        self.stdout.write("\n# Frontend env (.env / .env.local):")
        self.stdout.write(f"VITE_VAPID_PUBLIC_KEY={pub_b64}")
