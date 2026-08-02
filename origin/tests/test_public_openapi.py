"""The spec cannot fall behind the code.

`origin/views/public/openapi.py` is hand-authored. The one real thing a
generator would have given us — "the document always matches the
routes" — is bought here instead, by walking the public URLconf and
failing when a path or method is undocumented.

This is the test that makes hand-authoring safe. Delete it and the spec
becomes a stale README with a `.json` extension.
"""

import json
import re

from django.urls import get_resolver

from origin.tests.test_base import BaseAPITestCase
from origin.views.public.openapi import OPENAPI

SPEC_URL = "/api/public/v1/openapi.json"

# Django `<int:task_id>` → OpenAPI `{task_id}`.
_PARAM = re.compile(r"<[^:>]+:([^>]+)>")


def _django_to_openapi(pattern: str) -> str:
    return "/" + _PARAM.sub(r"{\1}", pattern)


def _public_routes():
    """`{path: {methods}}` for everything under `/api/public/`."""
    routes = {}
    for entry in get_resolver().url_patterns:
        for sub in getattr(entry, "url_patterns", [entry]):
            pattern = str(getattr(sub, "pattern", ""))
            if not pattern.startswith("api/public/"):
                continue
            view = getattr(sub.callback, "cls", None)
            if view is None:
                continue
            methods = {
                m
                for m in ("get", "post", "patch", "put", "delete")
                if hasattr(view, m) and m in getattr(view, "http_method_names", [])
            }
            routes[_django_to_openapi(pattern)] = methods
    return routes


class PublicOpenApiTests(BaseAPITestCase):
    def test_every_public_route_is_documented(self):
        documented = set(OPENAPI["paths"])
        for path in _public_routes():
            if path.endswith("openapi.json"):
                continue  # the spec does not describe itself
            self.assertIn(
                path,
                documented,
                f"{path} is routed but missing from the OpenAPI document. "
                "Add it to origin/views/public/openapi.py.",
            )

    def test_every_method_on_every_route_is_documented(self):
        for path, methods in _public_routes().items():
            if path.endswith("openapi.json"):
                continue
            spec = OPENAPI["paths"].get(path, {})
            for method in methods:
                self.assertIn(
                    method,
                    spec,
                    f"{method.upper()} {path} is routed but undocumented.",
                )

    def test_the_document_describes_no_route_that_does_not_exist(self):
        """The other direction — a documented endpoint that 404s is worse
        than an undocumented one, because someone will build on it."""
        routed = set(_public_routes())
        for path in OPENAPI["paths"]:
            self.assertIn(
                path,
                routed,
                f"{path} is documented but not routed.",
            )

    def test_the_spec_is_served_without_credentials(self):
        """You need this document in order to work out how to
        authenticate against the rest of the API."""
        self.client.credentials()
        res = self.client.get(SPEC_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["openapi"], "3.0.3")

    def test_the_spec_is_valid_json_and_self_consistent(self):
        """Every `$ref` resolves. A dangling ref renders as a blank box in
        every viewer and is invisible in review."""
        raw = json.dumps(OPENAPI)
        for ref in set(re.findall(r'"\$ref": "#/components/schemas/([A-Za-z]+)"', raw)):
            self.assertIn(
                ref,
                OPENAPI["components"]["schemas"],
                f"$ref to an undefined schema: {ref}",
            )

    def test_the_auth_scheme_is_apikey_not_bearer(self):
        """`ApiKey` is deliberate — a key must never be mistaken for a
        session token by anything that only looks for `Bearer`. Documenting
        it wrong would send every integrator down the wrong path."""
        scheme = OPENAPI["components"]["securitySchemes"]["ApiKey"]
        self.assertEqual(scheme["in"], "header")
        self.assertEqual(scheme["name"], "Authorization")
        self.assertIn("ApiKey", scheme["description"])

    def test_write_endpoints_document_the_read_key_403(self):
        """A read-only key gets 403, not a silent no-op. That is the
        single most surprising response in the API and has to be in the
        document."""
        for path, method in (
            ("/api/public/v1/tasks/", "post"),
            ("/api/public/v1/tasks/{task_id}/", "patch"),
        ):
            self.assertIn("403", OPENAPI["paths"][path][method]["responses"], f"{method} {path}")
