"""OpenSearch client config (MIP-721).

The production node takes basic auth over TLS with a certificate signed by
our own CA. With DEBUG off the client refuses anything less, and a pinned CA
replaces the system store rather than adding to it.
"""

import base64
import datetime
import ssl
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from origin.search_engine import opensearch_client
from origin.search_engine.opensearch_client import build_client_kwargs


def _ca_pem(common_name):
    """A throwaway self-signed CA certificate, minted per test run."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _cfg(**overrides):
    cfg = {
        "OPENSEARCH_HOST": "opensearch.railway.internal",
        "OPENSEARCH_PORT": 9200,
        "OPENSEARCH_USE_SSL": True,
        "OPENSEARCH_USERNAME": "genos_app",
        "OPENSEARCH_PASSWORD": "test-only-not-a-real-password",
        "OPENSEARCH_CA_CERT": "",
    }
    cfg.update(overrides)
    return cfg


class TestProductionShape(SimpleTestCase):
    def test_tls_with_a_login(self):
        kwargs = build_client_kwargs(_cfg(), debug=False)
        self.assertEqual(kwargs["hosts"], [{"host": "opensearch.railway.internal", "port": 9200}])
        self.assertIs(kwargs["use_ssl"], True)
        self.assertEqual(kwargs["http_auth"], ("genos_app", "test-only-not-a-real-password"))
        # opensearch-py ignores verify_certs & co. once ssl_context is given,
        # so the context itself must verify.
        ctx = kwargs["ssl_context"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_a_pinned_ca_is_the_only_one_trusted(self):
        pem = _ca_pem("genos-test-ca")
        ctx = build_client_kwargs(
            _cfg(OPENSEARCH_CA_CERT=base64.b64encode(pem).decode()), debug=False
        )["ssl_context"]
        subjects = [dict(rdn[0] for rdn in c["subject"]) for c in ctx.get_ca_certs()]
        self.assertEqual(subjects, [{"commonName": "genos-test-ca"}])
        self.assertTrue(ctx.check_hostname)

    def test_refuses_a_ca_that_is_not_base64(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "not valid base64"):
            build_client_kwargs(_cfg(OPENSEARCH_CA_CERT="-----BEGIN CERTIFICATE-----"), debug=False)

    def test_refuses_base64_that_is_not_a_certificate(self):
        for payload in (
            b"hello",
            b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesMessage(ImproperlyConfigured, "not a base64 PEM certificate"),
            ):
                build_client_kwargs(
                    _cfg(OPENSEARCH_CA_CERT=base64.b64encode(payload).decode()), debug=False
                )


class TestRefusals(SimpleTestCase):
    def test_debug_off_names_everything_missing(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            "OpenSearch needs OPENSEARCH_USE_SSL=true, OPENSEARCH_USERNAME, "
            "OPENSEARCH_PASSWORD with DJANGO_DEBUG=false",
        ):
            build_client_kwargs(
                _cfg(OPENSEARCH_USE_SSL=False, OPENSEARCH_USERNAME="", OPENSEARCH_PASSWORD=""),
                debug=False,
            )

    def test_a_password_never_goes_over_plain_http(self):
        # Even in DEBUG: a dev pointing at a real node must not leak its login.
        # (With DEBUG off the missing-TLS refusal above fires first.)
        for creds in (
            {"OPENSEARCH_USERNAME": "genos_app"},
            {"OPENSEARCH_PASSWORD": "test-only-not-a-real-password"},
        ):
            with (
                self.subTest(creds=creds),
                self.assertRaisesMessage(ImproperlyConfigured, "sent in the clear"),
            ):
                cfg = _cfg(OPENSEARCH_USE_SSL=False, OPENSEARCH_USERNAME="", OPENSEARCH_PASSWORD="")
                build_client_kwargs({**cfg, **creds}, debug=True)

    def test_debug_allows_the_local_node(self):
        # docker-compose: security plugin off, plain http, no login.
        kwargs = build_client_kwargs(
            _cfg(OPENSEARCH_USE_SSL=False, OPENSEARCH_USERNAME="", OPENSEARCH_PASSWORD=""),
            debug=True,
        )
        self.assertIs(kwargs["use_ssl"], False)
        self.assertNotIn("http_auth", kwargs)
        self.assertNotIn("ssl_context", kwargs)


class TestGetClient(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(opensearch_client, "_client", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(
        DEBUG=False,
        SEARCH_ENGINE=_cfg(
            OPENSEARCH_USE_SSL=False, OPENSEARCH_USERNAME="", OPENSEARCH_PASSWORD=""
        ),
    )
    def test_refuses_before_connecting(self):
        with (
            mock.patch.object(opensearch_client, "OpenSearch") as client_cls,
            self.assertRaisesMessage(ImproperlyConfigured, "OpenSearch needs"),
        ):
            opensearch_client.get_client()
        client_cls.assert_not_called()

    @override_settings(DEBUG=False, SEARCH_ENGINE=_cfg())
    def test_passes_the_checked_kwargs_through(self):
        with mock.patch.object(opensearch_client, "OpenSearch") as client_cls:
            opensearch_client.get_client()
        kwargs = client_cls.call_args.kwargs
        self.assertEqual(kwargs["http_auth"], ("genos_app", "test-only-not-a-real-password"))
        self.assertIsInstance(kwargs["ssl_context"], ssl.SSLContext)
