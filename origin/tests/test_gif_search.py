"""GIF search proxy: keyless 503, GIPHY response mapping, paging."""

from unittest.mock import patch

from django.test import override_settings

from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/gif/search/"


def _giphy_item(gif_id="abc", title="Party", url="https://media.giphy.com/x/giphy.gif"):
    return {
        "id": gif_id,
        "title": title,
        "images": {
            "original": {"url": url, "width": "480", "height": "270"},
            "fixed_width": {"url": "https://media.giphy.com/x/200w.gif"},
        },
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class GifSearchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_without_key_returns_503(self):
        with override_settings(GIPHY_API_KEY=""):
            resp = self.client.get(URL, {"q": "cats"})
        self.assertEqual(resp.status_code, 503)

    def test_requires_authentication(self):
        self.unauthenticate()
        resp = self.client.get(URL, {"q": "cats"})
        self.assertEqual(resp.status_code, 401)

    @override_settings(GIPHY_API_KEY="k")
    def test_maps_giphy_shape_and_next_offset(self):
        payload = {
            "data": [
                _giphy_item(),
                {"id": "no-images"},  # dropped: no original rendition
            ],
            "pagination": {"total_count": 100, "count": 1, "offset": 0},
        }
        with patch("requests.get", return_value=_FakeResponse(payload)) as mock_get:
            resp = self.client.get(URL, {"q": "party", "limit": "5", "offset": "0"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            body["results"],
            [
                {
                    "id": "abc",
                    "title": "Party",
                    "url": "https://media.giphy.com/x/giphy.gif",
                    "previewUrl": "https://media.giphy.com/x/200w.gif",
                    "width": 480,
                    "height": 270,
                }
            ],
        )
        self.assertEqual(body["next"], "1")

        called_url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertIn("/search", called_url)
        self.assertEqual(params["q"], "party")
        self.assertEqual(params["limit"], 5)
        self.assertEqual(params["rating"], "pg-13")
        # The key goes upstream but never into our response body.
        self.assertNotIn("k", str(body))

    @override_settings(GIPHY_API_KEY="k")
    def test_empty_query_hits_trending(self):
        payload = {"data": [], "pagination": {"total_count": 0, "count": 0, "offset": 0}}
        with patch("requests.get", return_value=_FakeResponse(payload)) as mock_get:
            resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"results": [], "next": ""})
        self.assertIn("/trending", mock_get.call_args[0][0])
        self.assertNotIn("q", mock_get.call_args[1]["params"])

    @override_settings(GIPHY_API_KEY="k")
    def test_limit_is_clamped(self):
        payload = {"data": [], "pagination": {}}
        with patch("requests.get", return_value=_FakeResponse(payload)) as mock_get:
            self.client.get(URL, {"q": "x", "limit": "999"})
            self.assertEqual(mock_get.call_args[1]["params"]["limit"], 50)
            self.client.get(URL, {"q": "x", "limit": "junk"})
            self.assertEqual(mock_get.call_args[1]["params"]["limit"], 24)

    @override_settings(GIPHY_API_KEY="k")
    def test_exhausted_listing_has_empty_next(self):
        payload = {
            "data": [_giphy_item()],
            "pagination": {"total_count": 1, "count": 1, "offset": 0},
        }
        with patch("requests.get", return_value=_FakeResponse(payload)):
            resp = self.client.get(URL, {"q": "party"})
        self.assertEqual(resp.json()["next"], "")

    @override_settings(GIPHY_API_KEY="k")
    def test_upstream_failure_returns_502(self):
        with patch("requests.get", side_effect=Exception("boom")):
            resp = self.client.get(URL, {"q": "party"})
        self.assertEqual(resp.status_code, 502)
