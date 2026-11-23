"""MIP-2243: dual HS256/RS256 session tokens, inert until a switch is set."""

from contextlib import contextmanager

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken, Token

from origin.models import CustomUser
from origin.services.jwt_backend import DualTokenBackend, build_backend


def _pem_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


PRIVATE, PUBLIC = _pem_pair()
OTHER_PRIVATE, OTHER_PUBLIC = _pem_pair()
HS_KEY = settings.SIMPLE_JWT["SIGNING_KEY"]


def _backend(**kw):
    kw.setdefault("signing_algorithm", "HS256")
    return DualTokenBackend("HS256", HS_KEY, **kw)


@contextmanager
def installed(backend):
    """Use `backend` for every simplejwt Token, then put the real one back."""
    from rest_framework_simplejwt import state

    before_cls, before_state = Token._token_backend, state.token_backend
    Token._token_backend = backend
    state.token_backend = backend
    try:
        yield
    finally:
        Token._token_backend = before_cls
        state.token_backend = before_state


def _hmac_with(key: str, payload: dict) -> str:
    """An HS256 token signed with `key` as the HMAC secret, built by hand: PyJWT refuses
    to do this with a PEM, an attacker would not."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    sig = hmac.new(key.encode(), head + b"." + body, hashlib.sha256).digest()
    return (head + b"." + body + b"." + b64(sig)).decode()


def _alg(token) -> str:
    return jwt.get_unverified_header(str(token))["alg"]


class DefaultsAreUnchanged(SimpleTestCase):
    def test_settings_default_to_the_old_behaviour(self):
        self.assertEqual(settings.JWT_SIGNING_ALGORITHM, "HS256")
        self.assertTrue(settings.JWT_ACCEPT_HS256)
        self.assertEqual(settings.JWT_PRIVATE_KEY, "")
        self.assertEqual(settings.JWT_PUBLIC_KEY, "")

    def test_installed_backend_signs_hs256_and_matches_the_plain_backend(self):
        from rest_framework_simplejwt.state import token_backend

        self.assertIsInstance(token_backend, DualTokenBackend)
        payload = {"user_id": 1, "exp": 4102444800, "token_type": "access"}
        plain = jwt.encode(payload, HS_KEY, algorithm="HS256")
        self.assertEqual(token_backend.encode(payload), plain)
        self.assertEqual(token_backend.decode(plain)["user_id"], 1)

    def test_build_backend_from_default_settings_signs_hs256(self):
        self.assertEqual(build_backend(settings).signing_algorithm, "HS256")


class SigningAndVerifying(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(
            username="jwtuser", email="jwtuser@example.com", password="pw-Strong-123!"
        )

    def test_rs256_signing_mints_rs256_and_the_api_verifies_it(self):
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE, public_key=PUBLIC)):
            refresh = RefreshToken.for_user(self.user)
            access = refresh.access_token
            self.assertEqual(_alg(refresh), "RS256")
            self.assertEqual(_alg(access), "RS256")
            validated = JWTAuthentication().get_validated_token(str(access).encode())
            self.assertEqual(validated["user_id"], str(self.user.id))
            # The refresh token also round-trips (rotation reads it back).
            self.assertEqual(RefreshToken(str(refresh))["user_id"], str(self.user.id))

    def test_public_key_is_derived_from_the_private_key(self):
        backend = _backend(signing_algorithm="RS256", private_key=PRIVATE)
        with installed(backend):
            token = AccessToken.for_user(self.user)
            self.assertEqual(AccessToken(str(token))["user_id"], str(self.user.id))

    def test_pem_with_literal_backslash_n_is_accepted(self):
        one_line = PRIVATE.strip().replace("\n", "\\n")
        backend = _backend(signing_algorithm="RS256", private_key=one_line)
        with installed(backend):
            self.assertEqual(_alg(AccessToken.for_user(self.user)), "RS256")

    def test_dual_accept_window_old_hs256_tokens_still_verify_while_signing_rs256(self):
        with installed(_backend()):
            old = str(AccessToken.for_user(self.user))
            old_refresh = str(RefreshToken.for_user(self.user))
        self.assertEqual(jwt.get_unverified_header(old)["alg"], "HS256")
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            self.assertEqual(AccessToken(old)["user_id"], str(self.user.id))
            self.assertEqual(RefreshToken(old_refresh)["user_id"], str(self.user.id))

    def test_new_rs256_tokens_verify_before_the_signing_switch_when_public_key_is_set(self):
        """Step 1 of the rollout: the api holds the public key but still signs HS256."""
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            new = str(AccessToken.for_user(self.user))
        with installed(_backend(public_key=PUBLIC)):
            self.assertEqual(AccessToken(new)["user_id"], str(self.user.id))
            self.assertEqual(_alg(AccessToken.for_user(self.user)), "HS256")

    def test_rs256_is_refused_when_no_public_key_is_configured(self):
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            new = str(AccessToken.for_user(self.user))
        with installed(_backend()):
            with self.assertRaises(Exception):
                AccessToken(new)

    def test_forged_hs256_is_refused_after_the_window(self):
        forged = jwt.encode(
            {"user_id": str(self.user.id), "exp": 4102444800, "token_type": "access", "jti": "x"},
            HS_KEY,
            algorithm="HS256",
        )
        with installed(
            _backend(signing_algorithm="RS256", private_key=PRIVATE, accept_hs256=False)
        ):
            with self.assertRaises(InvalidToken):
                JWTAuthentication().get_validated_token(forged.encode())
        # ... and while the window is open the same token is, by design, still accepted.
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            self.assertEqual(AccessToken(forged)["user_id"], str(self.user.id))

    def test_real_hs256_tokens_are_refused_after_the_window(self):
        with installed(_backend()):
            old = str(AccessToken.for_user(self.user))
        with installed(
            _backend(signing_algorithm="RS256", private_key=PRIVATE, accept_hs256=False)
        ):
            with self.assertRaises(Exception):
                AccessToken(old)

    def test_rs256_signed_by_another_key_is_refused(self):
        payload = {
            "user_id": str(self.user.id),
            "exp": 4102444800,
            "token_type": "access",
            "jti": "y",
        }
        forged = jwt.encode(payload, OTHER_PRIVATE, algorithm="RS256")
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            with self.assertRaises(InvalidToken):
                JWTAuthentication().get_validated_token(forged.encode())

    def test_public_key_used_as_an_hmac_secret_is_refused(self):
        """The classic RS256-to-HS256 confusion: the key is public, so anyone can do it."""
        payload = {
            "user_id": str(self.user.id),
            "exp": 4102444800,
            "token_type": "access",
            "jti": "z",
        }
        forged = _hmac_with(PUBLIC, payload)
        for accept in (True, False):
            with installed(
                _backend(signing_algorithm="RS256", private_key=PRIVATE, accept_hs256=accept)
            ):
                with self.assertRaises(InvalidToken):
                    JWTAuthentication().get_validated_token(forged.encode())

    def test_alg_none_and_other_algorithms_are_refused(self):
        payload = {
            "user_id": str(self.user.id),
            "exp": 4102444800,
            "token_type": "access",
            "jti": "n",
        }
        none_token = jwt.encode(payload, None, algorithm="none")
        hs512 = jwt.encode(payload, HS_KEY, algorithm="HS512")
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            for bad in (none_token, hs512, "not.a.jwt", ""):
                with self.assertRaises(Exception):
                    AccessToken(bad)

    def test_expired_rs256_token_is_reported_as_expired(self):
        payload = {"user_id": str(self.user.id), "exp": 1, "token_type": "access", "jti": "e"}
        expired = jwt.encode(payload, PRIVATE, algorithm="RS256")
        with installed(_backend(signing_algorithm="RS256", private_key=PRIVATE)):
            with self.assertRaises(Exception) as ctx:
                AccessToken(expired)
            self.assertIn("expired", str(ctx.exception).lower())

    def test_key_id_header_is_set_when_configured(self):
        backend = _backend(signing_algorithm="RS256", private_key=PRIVATE, key_id="k1")
        with installed(backend):
            header = jwt.get_unverified_header(str(AccessToken.for_user(self.user)))
        self.assertEqual(header["kid"], "k1")


class BadConfigurationFailsClosed(SimpleTestCase):
    def test_rs256_without_a_private_key(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "JWT_PRIVATE_KEY"):
            _backend(signing_algorithm="RS256", public_key=PUBLIC)

    def test_public_key_that_does_not_match(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "does not match"):
            _backend(signing_algorithm="RS256", private_key=PRIVATE, public_key=OTHER_PUBLIC)

    def test_hs256_signing_with_hs256_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "JWT_ACCEPT_HS256"):
            _backend(accept_hs256=False)

    def test_unknown_algorithm(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "JWT_SIGNING_ALGORITHM"):
            _backend(signing_algorithm="none")
