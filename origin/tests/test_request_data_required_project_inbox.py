"""Project and inbox views that read `request.data[...]` answer a missing field with a 400 (MIP-1020).

A partial body got through the team check and then raised a KeyError (500):

* `POST inbox/` (a notice) read `receiver_id` and `item_body`;
* `POST inbox/joinProjectRequest/` read `item_optionals["project_id"]`, `item_body` and
  `item_optionals`, and a string, list or number in `item_optionals` was a TypeError;
* `POST inbox/joinGMRequest/` read `item_optionals["gm_id"]`, `item_body`, `item_type` and
  `item_optionals` the same way;
* `POST project/tag/` read `team_id`, `tag_name`, `tag_color` and `tag_text_color`.

The other reads in `prj_views.py` could not raise (the tag rename and the template edit sit
behind a presence check; the join's hint reads a `project_id` it already checked), and now
read through `body_object` or the local. Their statuses and bodies are pinned here too.

The order of the answers is the order they had: a lookup that answered 404 for a project or a
GM that does not exist still does, even when the body also lacks `item_body`. Each refusal
checks that no row was written.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.project.prj_models import (
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
    ProjectTaskTemplate,
)
from origin.tests.test_base import BaseAPITestCase

INBOX = "/api/v2/inbox/"
JOIN_PROJECT_REQ = "/api/v2/inbox/joinProjectRequest/"
JOIN_GM_REQ = "/api/v2/inbox/joinGMRequest/"
TAG = "/api/v2/project/tag/"
JOIN_PROJECT = "/api/v2/project/join/"
TEMPLATE = "/api/v2/project/task-template/"

NOT_OBJECTS = (None, "x", 5, [], [1], True)


def without_ellipsis(body):
    return {k: v for k, v in body.items() if v is not ...}


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.team_id = str(self.team.team_id)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user2, project_system_user=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user2
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="owner")

    def post(self, url, body):
        return self.client.post(url, body, format="json")

    def assertRefused(self, url, body, status, error, model=InboxItems):
        before = model.objects.count()
        resp = self.post(url, body)
        self.assertEqual(resp.status_code, status, resp.content)
        self.assertEqual(resp.json(), {"error": error})
        self.assertEqual(model.objects.count(), before, "a refused request wrote a row")


class InboxNoticeRequiredTests(Base):
    def body(self, **over):
        body = {
            "team_id": self.team_id,
            "item_type": 0,
            "receiver_id": str(self.user2.id),
            "item_body": "hi",
        }
        body.update(over)
        return without_ellipsis(body)

    def test_a_valid_notice_is_unchanged(self):
        resp = self.post(INBOX, self.body(item_optionals={"k": 1}))
        self.assertEqual(resp.status_code, 201, resp.content)
        item = InboxItems.objects.get()
        self.assertEqual(item.item_type, 0)
        self.assertEqual(item.item_optionals, {"k": 1})
        self.assertEqual(resp.json()["data"]["itemBody"], "hi")

    def test_an_empty_body_and_a_body_with_only_the_team_keep_their_400s(self):
        self.assertRefused(INBOX, {}, 400, "team_id is required.")
        self.assertRefused(
            INBOX,
            {"team_id": self.team_id},
            400,
            "Only notices (item_type 0) can be created here.",
        )

    def test_a_notice_with_only_its_type_names_what_is_missing(self):
        self.assertRefused(
            INBOX,
            {"team_id": self.team_id, "item_type": 0},
            400,
            "receiver_id and item_body are required.",
        )

    def test_each_field_omitted_is_a_400_naming_it(self):
        for field in ("receiver_id", "item_body"):
            with self.subTest(field=field):
                self.assertRefused(INBOX, self.body(**{field: ...}), 400, f"{field} is required.")

    def test_item_optionals_stays_optional(self):
        resp = self.post(INBOX, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(InboxItems.objects.get().item_optionals)


class JoinProjectRequestRequiredTests(Base):
    def body(self, **over):
        body = {
            "team_id": self.team_id,
            "item_body": "please",
            "item_optionals": {"project_id": self.project.project_id},
        }
        body.update(over)
        return without_ellipsis(body)

    def test_a_valid_request_is_unchanged(self):
        resp = self.post(JOIN_PROJECT_REQ, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        item = InboxItems.objects.get()
        self.assertEqual((item.item_type, item.receiver_id), (2, self.user2.id))
        self.assertEqual(item.item_optionals, {"project_id": self.project.project_id})

    def test_a_project_id_sent_as_text_still_finds_the_project(self):
        resp = self.post(
            JOIN_PROJECT_REQ,
            self.body(item_optionals={"project_id": str(self.project.project_id)}),
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_an_empty_body_keeps_its_400(self):
        self.assertRefused(JOIN_PROJECT_REQ, {}, 400, "team_id is required.")

    def test_a_body_with_only_the_team_asks_for_item_optionals(self):
        self.assertRefused(
            JOIN_PROJECT_REQ,
            {"team_id": self.team_id},
            400,
            "item_optionals is required.",
        )

    def test_item_optionals_that_is_not_an_object_is_a_400(self):
        for value in NOT_OBJECTS:
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_PROJECT_REQ,
                    self.body(item_optionals=value),
                    400,
                    "item_optionals must be an object.",
                )

    def test_item_optionals_without_a_project_id_is_a_400(self):
        for value in ({}, {"other": 1}):
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_PROJECT_REQ,
                    self.body(item_optionals=value),
                    400,
                    "item_optionals.project_id is required.",
                )

    def test_a_project_id_that_names_no_project_is_the_404_an_unknown_one_is(self):
        for value in (None, "x", "", [1], {"a": 1}, True, 10**9):
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_PROJECT_REQ,
                    self.body(item_optionals={"project_id": value}),
                    404,
                    "Project not found.",
                )

    def test_a_missing_item_body_is_a_400_once_the_project_is_found(self):
        self.assertRefused(
            JOIN_PROJECT_REQ, self.body(item_body=...), 400, "item_body is required."
        )

    def test_an_unknown_project_is_still_a_404_before_item_body_is_asked_for(self):
        self.assertRefused(
            JOIN_PROJECT_REQ,
            self.body(item_body=..., item_optionals={"project_id": 10**9}),
            404,
            "Project not found.",
        )


class JoinGMRequestRequiredTests(Base):
    def body(self, **over):
        body = {
            "team_id": self.team_id,
            "item_body": "please",
            "item_type": 3,
            "item_optionals": {"gm_id": str(self.gm.id)},
        }
        body.update(over)
        return without_ellipsis(body)

    def test_a_valid_request_is_unchanged(self):
        resp = self.post(JOIN_GM_REQ, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        item = InboxItems.objects.get()
        self.assertEqual((item.item_type, item.receiver_id), (3, self.user2.id))
        self.assertEqual(item.item_optionals, {"gm_id": str(self.gm.id)})

    def test_an_empty_body_keeps_its_400(self):
        self.assertRefused(JOIN_GM_REQ, {}, 400, "team_id is required.")

    def test_a_body_with_only_the_team_asks_for_item_optionals(self):
        self.assertRefused(
            JOIN_GM_REQ, {"team_id": self.team_id}, 400, "item_optionals is required."
        )

    def test_item_optionals_that_is_not_an_object_is_a_400(self):
        for value in NOT_OBJECTS:
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_GM_REQ,
                    self.body(item_optionals=value),
                    400,
                    "item_optionals must be an object.",
                )

    def test_item_optionals_without_a_gm_id_is_a_400(self):
        for value in ({}, {"other": 1}):
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_GM_REQ,
                    self.body(item_optionals=value),
                    400,
                    "item_optionals.gm_id is required.",
                )

    def test_a_gm_id_that_names_no_gm_is_the_404_an_unknown_one_is(self):
        for value in (None, "x", [1], {"a": 1}, "00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=value):
                self.assertRefused(
                    JOIN_GM_REQ,
                    self.body(item_optionals={"gm_id": value}),
                    404,
                    "GM not found or has no owner.",
                )

    def test_item_body_and_item_type_are_asked_for_once_the_gm_is_found(self):
        self.assertRefused(JOIN_GM_REQ, self.body(item_body=...), 400, "item_body is required.")
        self.assertRefused(JOIN_GM_REQ, self.body(item_type=...), 400, "item_type is required.")
        self.assertRefused(
            JOIN_GM_REQ,
            self.body(item_body=..., item_type=...),
            400,
            "item_body and item_type are required.",
        )

    def test_an_unknown_gm_is_still_a_404_before_the_rest_is_asked_for(self):
        self.assertRefused(
            JOIN_GM_REQ,
            self.body(item_body=..., item_optionals={"gm_id": "x"}),
            404,
            "GM not found or has no owner.",
        )


class ProjectTagRequiredTests(Base):
    def body(self, **over):
        body = {
            "team_id": self.team_id,
            "project_id": self.project.project_id,
            "tag_name": "bug",
            "tag_color": "#fff",
            "tag_text_color": "#000",
        }
        body.update(over)
        return without_ellipsis(body)

    def test_a_valid_tag_is_unchanged(self):
        resp = self.post(TAG, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["tag_name"], "bug")
        self.assertEqual(resp.json()["tag_id"], 1)
        self.assertEqual(ProjectTags.objects.count(), 1)

    def test_an_empty_body_is_the_missing_project_it_was(self):
        self.assertRefused(TAG, {}, 404, "Project not found.", ProjectTags)

    def test_a_project_the_caller_is_not_on_is_still_a_404_first(self):
        other = ProjectMaster.objects.create(
            team=self.team, project_name="Q", owner=self.user2, project_system_user=self.user2
        )
        self.assertRefused(
            TAG, {"project_id": other.project_id}, 404, "Project not found.", ProjectTags
        )

    def test_a_body_with_only_the_project_names_what_is_missing(self):
        self.assertRefused(
            TAG,
            {"project_id": self.project.project_id},
            400,
            "team_id, tag_name, tag_color and tag_text_color are required.",
            ProjectTags,
        )

    def test_each_field_omitted_is_a_400_naming_it(self):
        for field in ("team_id", "tag_name", "tag_color", "tag_text_color"):
            with self.subTest(field=field):
                self.assertRefused(
                    TAG, self.body(**{field: ...}), 400, f"{field} is required.", ProjectTags
                )


class UnreachableReadsKeepTheirAnswersTests(Base):
    """Reads that sat behind a presence check, or after the value was checked."""

    def test_joining_twice_keeps_its_400_and_its_hint(self):
        body = {
            "team_id": self.team_id,
            "project_id": self.project.project_id,
            "attendee_id": str(self.user2.id),
        }
        resp = self.post(JOIN_PROJECT, body)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["hint"], f"Failed to join project: {self.project.project_id}")

    def test_a_tag_rename_changes_only_the_fields_sent(self):
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="old",
            tag_color="a",
            tag_text_color="b",
        )
        base = {"project_id": self.project.project_id, "old_tag_name": "old"}
        resp = self.client.put(TAG, {**base, "tag_color": "c"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"tagName": "old", "tagColor": "c", "tagTextColor": "b"})
        resp = self.client.put(TAG, {**base, "tag_name": "new"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"tagName": "new", "tagColor": "c", "tagTextColor": "b"})
        tag = ProjectTags.objects.get()
        self.assertEqual((tag.tag_name, tag.tag_color, tag.tag_text_color), ("new", "c", "b"))

    def test_a_template_edit_without_a_body_leaves_the_body(self):
        template = ProjectTaskTemplate.objects.create(
            team=self.team,
            project=self.project,
            template_name="t",
            body=[{"keep": 1}],
            created_by=self.user,
        )
        base = {"id": template.id, "project_id": self.project.project_id}
        resp = self.client.put(TEMPLATE, {**base, "template_name": "t2"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["body"], [{"keep": 1}])
        resp = self.client.put(TEMPLATE, {**base, "body": [{"new": 2}]}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        template.refresh_from_db()
        self.assertEqual(template.body, [{"new": 2}])
