import base64
import binascii
import ssl

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from opensearchpy import OpenSearch

_client = None


def _ssl_context(ca_cert_b64):
    """TLS context that checks the hostname and trusts `ca_cert_b64` alone.

    Empty means the system CAs. The node's certificate is signed by our own
    CA, so production passes it; trusting only that CA means no public CA can
    vouch for a host pretending to be the node.

    TLS 1.2 is the floor whatever the OpenSSL build's default is.
    """
    if not ca_cert_b64:
        context = ssl.create_default_context()
    else:
        try:
            pem = base64.b64decode(ca_cert_b64, validate=True).decode("ascii")
        except (binascii.Error, ValueError):
            raise ImproperlyConfigured("OPENSEARCH_CA_CERT is not valid base64.") from None
        try:
            context = ssl.create_default_context(cadata=pem)
        except ssl.SSLError:
            raise ImproperlyConfigured(
                "OPENSEARCH_CA_CERT is not a base64 PEM certificate."
            ) from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def build_client_kwargs(cfg, *, debug):
    """The OpenSearch(...) arguments for `cfg` (settings.SEARCH_ENGINE).

    With DEBUG off it refuses anything but TLS plus a username and password:
    the index holds every tenant's searchable text, and on plain http the
    password would cross the network in the clear. Raised on first use, not
    at import, because the crons that never search don't set these.
    """
    use_ssl = cfg["OPENSEARCH_USE_SSL"]
    username = cfg.get("OPENSEARCH_USERNAME", "")
    password = cfg.get("OPENSEARCH_PASSWORD", "")
    if not debug:
        missing = [
            name
            for name, value in (
                ("OPENSEARCH_USE_SSL=true", use_ssl),
                ("OPENSEARCH_USERNAME", username),
                ("OPENSEARCH_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise ImproperlyConfigured(
                "OpenSearch needs " + ", ".join(missing) + " with DJANGO_DEBUG=false "
                "(see RAILWAY_DEPLOY.md §4.4)."
            )
    if (username or password) and not use_ssl:
        raise ImproperlyConfigured(
            "OPENSEARCH_USERNAME/PASSWORD are set but OPENSEARCH_USE_SSL is not: "
            "the password would be sent in the clear."
        )

    kwargs = {
        "hosts": [{"host": cfg["OPENSEARCH_HOST"], "port": cfg["OPENSEARCH_PORT"]}],
        "http_compress": True,
        "use_ssl": use_ssl,
    }
    if use_ssl:
        kwargs["ssl_context"] = _ssl_context(cfg.get("OPENSEARCH_CA_CERT", ""))
    if username or password:
        kwargs["http_auth"] = (username, password)
    return kwargs


def get_client():
    """Singleton OpenSearch client built from Django settings."""
    global _client
    if _client is not None:
        return _client

    _client = OpenSearch(**build_client_kwargs(settings.SEARCH_ENGINE, debug=settings.DEBUG))
    return _client


def get_index_alias():
    return settings.SEARCH_ENGINE["OPENSEARCH_ALIAS"]


def get_physical_index():
    return settings.SEARCH_ENGINE["OPENSEARCH_INDEX"]
