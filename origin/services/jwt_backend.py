"""Dual-algorithm JWT backend: HS256 today, RS256 behind a switch (MIP-2243).

Why. `JWT_SECRET_KEY` is a shared HMAC secret, so genos-collab, which only has
to VERIFY a token, could also FORGE a session for any user. With RS256 the api
signs with a private key it alone holds and verifiers get the public key.

Nothing changes until a switch is set (the rollout is in
operations/AUTH_SECURITY_PLAN.md §3.3 and the PR body):

* SIGNING is `JWT_SIGNING_ALGORITHM` (default `HS256`: the old behaviour, byte
  for byte). `RS256` needs `JWT_PRIVATE_KEY`.
* VERIFYING picks the key from the token's own `alg` header, then requires
  exactly that one algorithm: `HS256` is checked with the shared secret only
  while `JWT_ACCEPT_HS256` is on (default on); `RS256` is checked with the
  public key only when one is configured. Any other `alg` (`none`, `HS512`, a
  public key used as an HMAC secret) is refused. The key is never chosen by the
  attacker: the algorithm list handed to PyJWT has one entry, and the HMAC key
  is never the public key.
* When the transition window ends, `JWT_ACCEPT_HS256=false` makes every HS256
  token invalid, including one forged by a holder of the old secret.

Installed in `OriginConfig.ready()` by replacing simplejwt's module-level
backend (simplejwt 5.x has no `TOKEN_BACKEND_CLASS` setting any more).
"""

from __future__ import annotations

from typing import Any

import jwt
from django.core.exceptions import ImproperlyConfigured
from rest_framework_simplejwt.backends import TokenBackend
from rest_framework_simplejwt.exceptions import TokenBackendError

HS256 = "HS256"
RS256 = "RS256"
SIGNING_ALGORITHMS = (HS256, RS256)


def _normalize_pem(value: str) -> str:
    """Railway variables are often one line with literal `\\n`: accept both."""
    return (value or "").replace("\\n", "\n").strip()


def public_key_from_private(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
        .strip()
    )


class DualTokenBackend(TokenBackend):
    def __init__(
        self,
        *args: Any,
        signing_algorithm: str = HS256,
        private_key: str = "",
        public_key: str = "",
        accept_hs256: bool = True,
        key_id: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if signing_algorithm not in SIGNING_ALGORITHMS:
            raise ImproperlyConfigured(
                f"JWT_SIGNING_ALGORITHM must be one of {', '.join(SIGNING_ALGORITHMS)}."
            )
        private_key = _normalize_pem(private_key)
        public_key = _normalize_pem(public_key)
        if private_key and not public_key:
            public_key = public_key_from_private(private_key)
        if signing_algorithm == RS256:
            if not private_key:
                raise ImproperlyConfigured(
                    "JWT_SIGNING_ALGORITHM=RS256 needs JWT_PRIVATE_KEY (PEM)."
                )
            if public_key_from_private(private_key) != public_key:
                raise ImproperlyConfigured("JWT_PUBLIC_KEY does not match JWT_PRIVATE_KEY.")
        if signing_algorithm == HS256 and not accept_hs256:
            raise ImproperlyConfigured(
                "JWT_ACCEPT_HS256=false while signing HS256 would refuse every token the api "
                "mints. Switch JWT_SIGNING_ALGORITHM to RS256 first."
            )
        self.signing_algorithm = signing_algorithm
        self._private_key = private_key
        self._public_key = public_key
        self.accept_hs256 = accept_hs256
        self.key_id = key_id

    def encode(self, payload: dict[str, Any]) -> str:
        if self.signing_algorithm == HS256:
            return super().encode(payload)
        jwt_payload = payload.copy()
        if self.audience is not None:
            jwt_payload["aud"] = self.audience
        if self.issuer is not None:
            jwt_payload["iss"] = self.issuer
        headers = {"kid": self.key_id} if self.key_id else None
        return jwt.encode(
            jwt_payload,
            self._private_key,
            algorithm=RS256,
            headers=headers,
            json_encoder=self.json_encoder,
        )

    def decode(self, token: Any, verify: bool = True) -> dict[str, Any]:
        if not verify:
            return super().decode(token, verify=False)
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.InvalidTokenError as e:
            raise TokenBackendError("Token is invalid") from e
        if alg == HS256 and self.accept_hs256:
            key: Any = self.prepared_signing_key
        elif alg == RS256 and self._public_key:
            key = self._public_key
        else:
            raise TokenBackendError("Invalid algorithm specified")
        # `super().decode` would pick the key from `self.algorithm`, so run the
        # same checks here with the algorithm and key fixed by the branch above.
        return self._decode_with(token, key, alg)

    def _decode_with(self, token: Any, key: Any, alg: str) -> dict[str, Any]:
        from rest_framework_simplejwt.exceptions import TokenBackendExpiredToken

        try:
            return jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.get_leeway(),
                options={"verify_aud": self.audience is not None, "verify_signature": True},
            )
        except jwt.ExpiredSignatureError as e:
            raise TokenBackendExpiredToken("Token is expired") from e
        except jwt.InvalidTokenError as e:
            raise TokenBackendError("Token is invalid") from e


def build_backend(settings) -> DualTokenBackend:
    sj = settings.SIMPLE_JWT
    return DualTokenBackend(
        sj["ALGORITHM"],
        sj["SIGNING_KEY"],
        signing_algorithm=settings.JWT_SIGNING_ALGORITHM,
        private_key=settings.JWT_PRIVATE_KEY,
        public_key=settings.JWT_PUBLIC_KEY,
        accept_hs256=settings.JWT_ACCEPT_HS256,
        key_id=settings.JWT_KEY_ID,
    )


def install(settings) -> DualTokenBackend:
    """Make every simplejwt `Token` (access, refresh, DRF auth) use the dual backend."""
    from rest_framework_simplejwt import state
    from rest_framework_simplejwt.tokens import Token

    backend = build_backend(settings)
    state.token_backend = backend
    Token._token_backend = backend
    return backend
