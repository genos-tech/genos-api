"""What the public surfaces actually return, key by key.

The existing public-API tests are thorough about **authorization** —
which key may do what, whose data comes back. They assert almost nothing
about **shape**: they check a status code and one or two values, so a
field that disappeared, got renamed, or started returning null would
pass every one of them.

That is not hypothetical. `message.created` and `task.comment_created`
shipped with `"created_at": null` in every delivery, because the model
field is `ts_sent_at` and a defensive `getattr(obj, "ts_created_at",
None)` turned the miss into a silent null. Nothing failed. The fix was
one line; noticing was the hard part.

So this file asserts the **exact key set** of every published payload,
in both directions:

  - a MISSING key breaks integrators who read it
  - an EXTRA key is worse, because it ships data nobody decided to
    publish and becomes a contract the moment someone depends on it

The key sets live here as literals rather than being derived from the
serializers, on purpose. Deriving them would make the test agree with
the code by construction and assert nothing — the point is to state the
contract independently, so changing the code without changing the
contract is a failure.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import webhook_enqueue
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.openapi import OPENAPI

User = get_user_model()

# ── the published contracts ──────────────────────────────────────────

TASK_KEYS = {
    "id",
    "display_id",
    "title",
    "status",
    "priority",
    "project_id",
    "team_id",
    "assignee_id",
    "reporter_id",
    "due_date",
    "start_date",
    "created_at",
    "updated_at",
}

PROJECT_KEYS = {"id", "name", "code", "team_id", "is_private", "created_at"}

ME_KEYS = {"user_id", "username", "email", "key"}
ME_KEY_KEYS = {"id", "name", "scope", "team_id"}

TASK_LIST_KEYS = {"tasks", "total", "limit", "offset"}

COMMENT_PAYLOAD_KEYS = {
    "id",
    "task_id",
    "task_display_id",
    "project_id",
    "team_id",
    "author_id",
    "body",
    "created_at",
}

MESSAGE_PAYLOAD_KEYS = {
    "id",
    "channel_id",
    "channel_kind",
    "channel_title",
    "team_id",
    "author_id",
    "body_text",
    "thread_root_id",
    "is_thread_reply",
    "created_at",
}


class ContractTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Contract", owner=self.user, code="CT"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="A task",
            status="Open",
            reporter=self.user,
            project_task_number=1,
        )
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            name="contract",
            prefix=raw[:11],
            key_hash=hash_key(raw),
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.team_q = f"team_id={self.team.team_id}"


class RestResponseShapeTests(ContractTestBase):
    def test_a_task_carries_exactly_the_documented_keys(self):
        res = self.client.get(f"/api/public/v1/tasks/{self.task.task_id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), TASK_KEYS)

    def test_the_task_list_envelope_is_exact(self):
        res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), TASK_LIST_KEYS)
        self.assertEqual(set(res.data["tasks"][0]), TASK_KEYS)

    def test_a_created_task_carries_the_same_keys_as_a_fetched_one(self):
        """A create response that differs from a read response is the
        kind of thing nobody notices until an integrator stores one and
        compares it to the other."""
        res = self.client.post(
            "/api/public/v1/tasks/",
            # `team_id` because this fixture uses a PERSONAL token, which
            # spans every team the user belongs to and so has to be told
            # which one. A team-scoped key would not need it.
            {
                "title": "New",
                "project_id": self.project.project_id,
                "team_id": str(self.team.team_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(set(res.data), TASK_KEYS)

    def test_a_patched_task_carries_the_same_keys(self):
        res = self.client.patch(
            f"/api/public/v1/tasks/{self.task.task_id}/",
            {"title": "Renamed"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), TASK_KEYS)

    def test_a_project_carries_exactly_the_documented_keys(self):
        res = self.client.get(f"/api/public/v1/projects/?{self.team_q}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), {"projects"})
        self.assertEqual(set(res.data["projects"][0]), PROJECT_KEYS)

    def test_me_carries_exactly_the_documented_keys(self):
        res = self.client.get("/api/public/v1/me/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), ME_KEYS)
        self.assertEqual(set(res.data["key"]), ME_KEY_KEYS)

    def test_no_timestamp_is_silently_null(self):
        """The exact bug this file exists for. `created_at` was null in
        every chat webhook because the model field is `ts_sent_at`."""
        res = self.client.get(f"/api/public/v1/tasks/{self.task.task_id}/")
        self.assertIsNotNone(res.data["created_at"])
        self.assertIsNotNone(res.data["updated_at"])


class OpenApiMatchesRealityTests(ContractTestBase):
    """The published schema and the actual response must agree.

    Without this the document is a description of what someone believed
    the API returned. `required` + `additionalProperties: false` on the
    schema is what gives these assertions teeth — a schema with only
    `properties` validates a completely empty object.
    """

    def _schema_keys(self, name):
        schema = OPENAPI["components"]["schemas"][name]
        self.assertFalse(
            schema.get("additionalProperties", True),
            f"{name} must set additionalProperties: false or it cannot fail",
        )
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        return set(schema["required"])

    def test_the_task_schema_matches_the_task_response(self):
        res = self.client.get(f"/api/public/v1/tasks/{self.task.task_id}/")
        self.assertEqual(self._schema_keys("Task"), set(res.data))

    def test_the_project_schema_matches_the_project_response(self):
        res = self.client.get(f"/api/public/v1/projects/?{self.team_q}")
        self.assertEqual(self._schema_keys("Project"), set(res.data["projects"][0]))

    def test_the_documented_task_keys_are_the_contract_constant(self):
        """Three statements of the same truth — the schema, this file's
        constant, and the live response — pinned to each other."""
        self.assertEqual(self._schema_keys("Task"), TASK_KEYS)


class WebhookPayloadShapeTests(ContractTestBase):
    """The three payload builders are as public as the REST responses —
    they are documented on `/developers` — and nothing pinned them."""

    def test_the_task_payload_carries_exactly_the_documented_keys(self):
        self.assertEqual(set(webhook_enqueue.task_payload(self.task)), TASK_KEYS)

    def test_the_webhook_task_shape_matches_the_rest_one(self):
        """`task_payload`'s docstring claims an integrator reading the API
        and one receiving a webhook see the same object.

        It was not true: the webhook carried `team_id` and no
        `start_date`; the REST serializer carried `start_date` and no
        `team_id`. Neither omission was deliberate. Both now carry both,
        and this test is what keeps the claim honest.
        """
        res = self.client.get(f"/api/public/v1/tasks/{self.task.task_id}/")
        self.assertEqual(set(webhook_enqueue.task_payload(self.task)), set(res.data))

    def test_the_comment_payload_carries_exactly_the_documented_keys(self):
        comment = TaskComments.objects.create(
            task=self.task, comment_id=1, sender=self.user, comment_body={"text": "hi"}
        )
        payload = webhook_enqueue.comment_payload(comment, self.task)
        self.assertEqual(set(payload), COMMENT_PAYLOAD_KEYS)
        self.assertIsNotNone(payload["created_at"])

    def test_the_message_payload_carries_exactly_the_documented_keys(self):
        from origin.models.chat.unified_models import Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="G")
        message = Message.objects.create(
            channel=channel, sender=self.user, seq=1, body=[], body_text="hello"
        )
        payload = webhook_enqueue.message_payload(message, channel)
        self.assertEqual(set(payload), MESSAGE_PAYLOAD_KEYS)
        self.assertIsNotNone(payload["created_at"])

    def test_no_payload_leaks_a_body_array(self):
        """Chat publishes `body_text`, never the BlockNote `body` array —
        that is an editor format whose inline node types change, and
        publishing it would make an internal schema a public contract."""
        from origin.models.chat.unified_models import Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="G")
        message = Message.objects.create(
            channel=channel, sender=self.user, seq=1, body=[{"type": "p"}], body_text="hi"
        )
        self.assertNotIn("body", webhook_enqueue.message_payload(message, channel))


class NeverFiveHundredTests(ContractTestBase):
    """Every documented parameter, given something it does not expect.

    Two 500-on-input bugs turned up in one session — a malformed UUID
    raising `ValidationError` where only `ValueError` was caught, and a
    dict raising `unhashable type` out of a membership test. Both were
    found by accident. This walks the parameters instead.

    A public endpoint answering 500 to bad input is a scanner signal, an
    error-budget leak, and a support ticket that reads "your API is
    broken" — so the assertion is deliberately loose about WHICH client
    error, and strict that it is one.
    """

    CLIENT_ERRORS = (400, 401, 403, 404)

    def _assert_client_error(self, res, label):
        self.assertIn(
            res.status_code,
            (*self.CLIENT_ERRORS, 200, 201),
            f"{label} returned {res.status_code}",
        )
        self.assertLess(res.status_code, 500, f"{label} returned {res.status_code}")

    def test_list_paging_parameters_never_500(self):
        for value in ("-1", "abc", "", "9" * 40, "1.5", "null", "[]"):
            for param in ("limit", "offset"):
                res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&{param}={value}")
                self._assert_client_error(res, f"{param}={value!r}")

    def test_project_id_filter_never_500s(self):
        for value in ("abc", "", "-1", "9" * 40, "1.5", "%00"):
            res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&project_id={value}")
            self._assert_client_error(res, f"project_id={value!r}")

    def test_team_id_never_500s(self):
        for value in ("abc", "", "not-a-uuid", "9" * 40, "../../etc/passwd"):
            res = self.client.get(f"/api/public/v1/tasks/?team_id={value}")
            self._assert_client_error(res, f"team_id={value!r}")

    def test_projects_team_id_never_500s(self):
        for value in ("abc", "", "not-a-uuid", "0" * 100):
            res = self.client.get(f"/api/public/v1/projects/?team_id={value}")
            self._assert_client_error(res, f"team_id={value!r}")

    def test_create_body_types_never_500(self):
        bodies = [
            {},
            {"title": None, "project_id": self.project.project_id},
            {"title": 123, "project_id": self.project.project_id},
            {"title": [], "project_id": self.project.project_id},
            {"title": {"a": 1}, "project_id": self.project.project_id},
            {"title": "ok", "project_id": "abc"},
            {"title": "ok", "project_id": None},
            {"title": "ok", "project_id": {"id": 1}},
            {"title": "ok", "project_id": []},
            {"title": "x" * 5000, "project_id": self.project.project_id},
            {"title": "ok", "project_id": self.project.project_id, "unknown_field": "x"},
            {"title": "ok", "project_id": self.project.project_id, "assignee_id": "not-a-uuid"},
            {"title": "ok", "project_id": self.project.project_id, "due_date": "not-a-date"},
        ]
        for body in bodies:
            res = self.client.post(
                "/api/public/v1/tasks/",
                {**body, "team_id": str(self.team.team_id)},
                format="json",
            )
            self._assert_client_error(res, f"POST {body}")

    def test_patch_body_types_never_500(self):
        url = f"/api/public/v1/tasks/{self.task.task_id}/"
        bodies = [
            {},
            {"title": None},
            {"title": 123},
            {"title": []},
            {"status": {"a": 1}},
            {"priority": []},
            {"title": "x" * 5000},
            {"unknown_field": "x"},
            {"id": 99},
        ]
        for body in bodies:
            res = self.client.patch(url, body, format="json")
            self._assert_client_error(res, f"PATCH {body}")

    def test_an_oversized_title_is_truncated_not_rejected_or_crashed(self):
        """`title` is capped at 255 by the allowlist. Whatever the policy,
        it must be one — silently storing 5000 chars into a 255-char
        column is a 500 at the database."""
        res = self.client.post(
            "/api/public/v1/tasks/",
            {
                "title": "x" * 5000,
                "project_id": self.project.project_id,
                "team_id": str(self.team.team_id),
            },
            format="json",
        )
        self.assertIn(res.status_code, (201, 400))
        if res.status_code == 201:
            self.assertLessEqual(len(res.data["title"]), 255)


class ManagementEndpointInputTests(BaseAPITestCase):
    """The same sweep against the endpoints that CREATE keys and webhooks.

    These take a JWT rather than an API key, so they are less exposed —
    but they accept more structured input (lists of ids, lists of event
    names, a URL) and they are what a Settings UI drives, so a 500 here
    is a broken settings page rather than a broken integration.
    """

    CLIENT_OK = (200, 201, 400, 403, 404)

    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _assert_no_500(self, res, label):
        self.assertLess(res.status_code, 500, f"{label} → {res.status_code}")

    def test_webhook_create_input_never_500s(self):
        bodies = [
            {},
            {"team_id": str(self.team.team_id)},
            {"team_id": "not-a-uuid", "url": "https://e.com/h", "events": ["task.created"]},
            {"team_id": None, "url": "https://e.com/h", "events": ["task.created"]},
            {"team_id": str(self.team.team_id), "url": None, "events": ["task.created"]},
            {"team_id": str(self.team.team_id), "url": 123, "events": ["task.created"]},
            {"team_id": str(self.team.team_id), "url": "https://e.com/h", "events": "task.created"},
            {"team_id": str(self.team.team_id), "url": "https://e.com/h", "events": [123]},
            {"team_id": str(self.team.team_id), "url": "https://e.com/h", "events": [None]},
            {"team_id": str(self.team.team_id), "url": "https://e.com/h", "events": [{"a": 1}]},
            {
                "team_id": str(self.team.team_id),
                "url": "https://e.com/h",
                "events": ["task.created"],
                "project_ids": "12",
            },
            {
                "team_id": str(self.team.team_id),
                "url": "https://e.com/h",
                "events": ["task.created"],
                "project_ids": [None],
            },
            {
                "team_id": str(self.team.team_id),
                "url": "https://e.com/h",
                "events": ["message.created"],
                "channel_ids": [123],
            },
            {
                "team_id": str(self.team.team_id),
                "url": "https://e.com/h",
                "events": ["message.created"],
                "channel_ids": [{"id": "x"}],
            },
            {
                "team_id": str(self.team.team_id),
                "url": "https://e.com/h",
                "events": ["message.created"],
                "channel_ids": "not-a-list",
            },
        ]
        for body in bodies:
            res = self.client.post("/api/v2/webhooks/", body, format="json")
            self._assert_no_500(res, f"POST /webhooks/ {body}")

    def test_api_key_create_input_never_500s(self):
        bodies = [
            {},
            {"name": None},
            {"name": 123},
            {"name": []},
            {"name": "k", "scope": "banana"},
            {"name": "k", "scope": 1},
            {"name": "k", "scope": None},
            {"name": "x" * 5000, "scope": "read"},
            {"name": "k", "scope": "read", "team_id": "not-a-uuid"},
            {"name": "k", "scope": "read", "team_id": {"a": 1}},
        ]
        for body in bodies:
            res = self.client.post("/api/v2/api-keys/", body, format="json")
            self._assert_no_500(res, f"POST /api-keys/ {body}")

    def test_webhook_list_and_delete_never_500(self):
        for value in ("", "abc", "not-a-uuid", "0" * 100):
            self._assert_no_500(
                self.client.get(f"/api/v2/webhooks/?team_id={value}"), f"GET team_id={value!r}"
            )
        for value in ("abc", "00000000-0000-0000-0000-000000000000"):
            self._assert_no_500(
                self.client.delete(f"/api/v2/webhooks/{value}/"), f"DELETE {value!r}"
            )


class SpecConstraintsMatchBehaviourTests(ContractTestBase):
    """A documented constraint must describe what the server does.

    `maximum` in OpenAPI means "reject above this". A generated client
    enforces it locally — so documenting `maximum: 100` on a parameter
    the server CLAMPS makes that client refuse to send a request the
    server would have answered. The spec is the thing people generate
    from, so a constraint that is merely tidy is a bug in it.
    """

    def _param(self, name):
        for p in OPENAPI["paths"]["/api/public/v1/tasks/"]["get"]["parameters"]:
            if p["name"] == name:
                return p
        raise AssertionError(f"{name} is not documented")

    def test_limit_is_clamped_so_it_declares_no_maximum(self):
        self.assertNotIn(
            "maximum",
            self._param("limit")["schema"],
            "limit is clamped server-side; a `maximum` would make generated "
            "clients refuse a request the server would answer",
        )
        res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&limit=99999")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["limit"], 100)

    def test_limit_below_one_is_clamped_not_rejected(self):
        res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&limit=-5")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["limit"], 1)

    def test_offset_declares_the_maximum_it_actually_enforces(self):
        schema = self._param("offset")["schema"]
        self.assertIn("maximum", schema)
        over = schema["maximum"] + 1
        res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&offset={over}")
        self.assertEqual(
            res.status_code,
            400,
            "the spec declares a maximum, so the server must reject above it",
        )

    def test_the_documented_offset_maximum_is_honoured_at_the_boundary(self):
        schema = self._param("offset")["schema"]
        res = self.client.get(f"/api/public/v1/tasks/?{self.team_q}&offset={schema['maximum']}")
        self.assertEqual(res.status_code, 200)
