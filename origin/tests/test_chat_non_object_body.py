"""MIP-996: a body that is not a JSON object is a 400 on the chat views, not a 500.

These views read their body with `request.data or {}` and then `.get(...)`. `or {}` only
covers the empty bodies (null, `[]`, `0`, `""`, `false`). A body that parses as JSON but
is not an object (`[1]`, `"abc"`, `5`, `true`) went straight to `.get`, or to `in` and
`.keys()`, and answered 500. MIP-996 picked one of two rules per view; since MIP-1013
there is one, in the base view (`object_body`), ahead of the view itself:

* A body that is not an object is refused with 400 "Request body must be a JSON object.".
  Where MIP-996 read it as `{}` (a view with a REQUIRED field, whose missing-field 400 it
  got) the answer is now the refusal, and `{}`'s own answer is still pinned beside it.
* Where an EMPTY body is a request in its own right (mark everything read; a patch with
  nothing to change) it was already refused, because reading `[1]` as `{}` would run the
  request as if nothing had been sent.

Each probe below compares STATUS AND BODY with the answer for `{}` on the same view and
pins that answer's text, so both cannot drift together; a refused body must leave every
row it could touch exactly as it was; null and the other empty bodies keep their answers;
and a valid request still gets what it got.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.chat.todo_models import ToDoGroup, ToDoItem, ToDoItemReminder
from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageMention,
    MessageReaction,
    MessageReminder,
    ReadCursor,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster

from .test_base import BaseAPITestCase

# JSON text, posted as it is. None of these is an object.
NOT_OBJECTS = ("[1]", '"abc"', "5", "true", "1.5", '[{"a": 1}]')
# Empty-ish: they read as "no body" and were already an answer.
EMPTY_ISH = ("null", "[]", "0", '""', "false", "{}", "")

NOT_AN_OBJECT = {"error": "Request body must be a JSON object."}

# Everything a refused body could touch, row by row (so a bumped `ts_updated_at` shows).
_TABLES = (
    Activity,
    Channel,
    ChannelMember,
    Flag,
    Message,
    MessageMention,
    MessageReaction,
    MessageReminder,
    ProjectMaster,
    ReadCursor,
    TaskMaster,
    ToDoItem,
    ToDoItemReminder,
)

BLOCKS = [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]


class _ChatClient(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The real 500 response if there is one, not the exception re-raised into the test.
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()

        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")
        self.message = Message.objects.create(
            channel=self.gm, sender=self.user, seq=1, body=BLOCKS, body_text="hello"
        )

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        # The PM channel is created by a signal on project save.
        self.pm = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        ChannelMember.objects.get_or_create(
            channel=self.pm, user=self.user, defaults={"role": "member"}
        )
        self.card = Message.objects.create(
            channel=self.pm,
            sender=self.user,
            seq=1,
            body=BLOCKS,
            body_text="hello",
            task_id=self.task.task_id,
        )

        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=timezone.localdate()
        )
        self.todo = ToDoItem.objects.create(group=group, title="ship it")

    def send(self, method, url, raw):
        return self.client.generic(method, url, raw, content_type="application/json")

    def state(self):
        return {m.__name__: list(m.objects.order_by("pk").values()) for m in _TABLES}


class _MissingFieldAnswers:
    """A view with a required field: a body that is not an object is refused, and `{}` still
    gets the view's own missing-field answer."""

    METHOD: str
    MISSING_FIELD: str  # the view's answer to `{}`, in its own words

    def url(self) -> str:
        raise NotImplementedError

    def test_a_body_that_is_not_an_object_is_refused(self):
        before = self.state()
        empty = self.send(self.METHOD, self.url(), "{}")
        self.assertEqual((empty.status_code, empty.json()), (400, {"error": self.MISSING_FIELD}))

        for raw in NOT_OBJECTS:
            resp = self.send(self.METHOD, self.url(), raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertEqual(self.state(), before, "a refused body wrote something")

    def test_an_empty_body_keeps_the_answer_it_always_had(self):
        for raw in EMPTY_ISH:
            resp = self.send(self.METHOD, self.url(), raw)
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": self.MISSING_FIELD}), repr(raw)
            )


# --------------------------------------------------------------------------- #
# activity_views_v3                                                           #
# --------------------------------------------------------------------------- #


class ReadAllBodyShapeTests(_ChatClient):
    """`PUT /activities/read-all/`: an empty body marks EVERYTHING read, so `[1]` must not."""

    URL = "/api/v3/activities/read-all/"

    def setUp(self):
        super().setUp()
        self.other = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Other", owner=self.user
        )
        self.unread()

    def unread(self):
        """Two unread entries for me (one per channel) and one for someone else."""
        Activity.objects.all().delete()
        mine = [
            Activity.objects.create(
                team=self.team,
                recipient=self.user,
                actor=self.user2,
                activity_type=ActivityType.MENTION,
                channel=channel,
            )
            for channel in (self.gm, self.other)
        ]
        Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.gm,
        )
        return mine

    def read_ids(self):
        return {a.id for a in Activity.objects.filter(is_read=True)}

    def test_a_body_that_is_not_an_object_is_refused_and_marks_nothing_read(self):
        before = self.state()
        for raw in NOT_OBJECTS:
            resp = self.send("PUT", self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.read_ids(), set())

    def test_an_empty_body_still_marks_everything_read(self):
        for raw in EMPTY_ISH:
            mine = self.unread()
            resp = self.send("PUT", self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 2}), repr(raw))
            self.assertEqual(self.read_ids(), {a.id for a in mine}, repr(raw))

    def test_a_channel_in_the_body_still_scopes_it(self):
        mine = self.unread()
        resp = self.send("PUT", self.URL, f'{{"channel_id": "{self.other.id}"}}')
        self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 1}))
        self.assertEqual(self.read_ids(), {mine[1].id})

    def test_a_channel_in_the_query_still_scopes_it(self):
        # The view reads no body when `?channel_id=` is there, so the request the web
        # client sends (`{}` plus the query) and every empty body answer as they did.
        for raw in EMPTY_ISH:
            mine = self.unread()
            resp = self.send("PUT", f"{self.URL}?channel_id={self.other.id}", raw)
            self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 1}), repr(raw))
            self.assertEqual(self.read_ids(), {mine[1].id}, repr(raw))

    def test_a_channel_in_the_query_does_not_excuse_a_body_that_is_not_an_object(self):
        # Since MIP-1013 the base view refuses it before the view runs, whether or not the
        # view would have read it.
        before = self.state()
        for raw in NOT_OBJECTS:
            resp = self.send("PUT", f"{self.URL}?channel_id={self.other.id}", raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.read_ids(), set())


class ReadBatchBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "PUT"
    MISSING_FIELD = "activity_ids must be a non-empty list."

    def url(self):
        return "/api/v3/activities/read-batch/"

    def test_a_valid_body_still_answers_what_it_did(self):
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            channel=self.gm,
        )
        resp = self.send("PUT", self.url(), f'{{"activity_ids": ["{row.id}"]}}')
        self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 1}))
        row.refresh_from_db()
        self.assertTrue(row.is_read)


class SurfaceBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = "surface_type, team_id and entity_key are required."

    def url(self):
        return "/api/v3/activities/surface/"

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {
            "surface_type": 5,
            "team_id": str(self.team.team_id),
            "entity_key": f"task:{self.task.task_id}",
            "newly_mentioned_user_ids": [str(self.user2.id)],
            "meta": {"taskId": self.task.task_id},
        }
        resp = self.client.post(self.url(), body, format="json")
        self.assertEqual(resp.status_code, 201)
        rows = resp.json()["activities"]
        self.assertEqual([r["recipientUserId"] for r in rows], [str(self.user2.id)])
        self.assertEqual(Activity.objects.filter(recipient=self.user2).count(), 1)


class SurfaceCommentBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = "surface_type, team_id and entity_key are required."

    def url(self):
        return "/api/v3/activities/surface-comment/"

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {
            "surface_type": 5,
            "team_id": str(self.team.team_id),
            "entity_key": "comment-participant:1",
            "meta": {"taskId": self.task.task_id},
        }
        resp = self.client.post(self.url(), body, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertIn("activities", resp.json())


# --------------------------------------------------------------------------- #
# channel_views                                                               #
# --------------------------------------------------------------------------- #


class ChannelCreateBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = "kind must be 1 (DM), 2 (GM), or 4 (MDM)."

    def url(self):
        return "/api/v3/channels/"

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {"kind": 2, "team_id": str(self.team.team_id), "title": "Fresh"}
        resp = self.client.post(self.url(), body, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["channel"]["title"], "Fresh")


class ChannelPatchBodyShapeTests(_ChatClient):
    """`PATCH /channels/<gm>/`: every field is optional and an empty body is a valid patch
    (the channel comes back unchanged), so a non-object is refused, not read as one.

    Before this, `[1]`, `"abc"` and `[{"a": 1}]` answered 200 "unchanged" by luck of
    `"title" in body` being a membership or substring test, while `5`, `true`, `1.5`,
    `["title"]` and `"title"` answered 500.
    """

    def url(self):
        return f"/api/v3/channels/{self.gm.id}/"

    def test_a_body_that_is_not_an_object_is_refused_and_changes_nothing(self):
        before = self.state()
        for raw in NOT_OBJECTS + ('["title"]', '"title"', '"is_private"', '["owner_user_id"]'):
            resp = self.send("PATCH", self.url(), raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertEqual(self.state(), before)

    def test_an_empty_body_still_returns_the_channel_unchanged(self):
        before = self.state()
        for raw in EMPTY_ISH:
            resp = self.send("PATCH", self.url(), raw)
            self.assertEqual(resp.status_code, 200, repr(raw))
            self.assertEqual(resp.json()["channel"]["title"], "GM", repr(raw))
        self.assertEqual(self.state(), before)

    def test_a_valid_patch_still_applies(self):
        resp = self.client.patch(
            self.url(), {"title": "Renamed", "is_private": True}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["channel"]["title"], "Renamed")
        self.gm.refresh_from_db()
        self.assertEqual((self.gm.title, self.gm.is_private), ("Renamed", True))

    def test_the_refusal_comes_before_the_views_own_permission_check(self):
        # Since MIP-1013 the body is judged in the base view, after DRF's authentication
        # and permissions but before the view's own "may you edit this channel" (a 403
        # inside the handler). A viewer who may not edit hears that for a body that is an
        # object, and the body refusal for one that is not; neither changes anything.
        self.authenticate(self.user2)
        before = self.state()
        resp = self.send("PATCH", self.url(), "[1]")
        self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT))
        resp = self.send("PATCH", self.url(), '{"title": "x"}')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.state(), before)


class PmChannelPatchBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "PATCH"
    MISSING_FIELD = (
        "PM channels mirror the project's metadata; only `title` can be patched "
        "(it renames the project). Edit the project for anything else."
    )

    def url(self):
        return f"/api/v3/channels/{self.pm.id}/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.patch(self.url(), {"title": "Renamed"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["channel"]["title"], "Renamed")
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Renamed")


class ChannelMembersAddBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = "user_ids must be a non-empty list."

    def url(self):
        return f"/api/v3/channels/{self.gm.id}/members/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.post(self.url(), {"user_ids": [str(self.user2.id)]}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual([m["userId"] for m in resp.json()["members"]], [str(self.user2.id)])


class ChannelMemberRoleBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "PATCH"
    MISSING_FIELD = "member_role must be one of ['editor', 'viewer']."

    def url(self):
        return f"/api/v3/channels/{self.gm.id}/members/{self.user2.id}/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.patch(self.url(), {"member_role": "editor"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ChannelMember.objects.get(channel=self.gm, user=self.user2).role, "admin")


# --------------------------------------------------------------------------- #
# message_views                                                               #
# --------------------------------------------------------------------------- #

BLOCKS_MISSING = "body must be a non-empty list of blocks."


class MessageSendBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = BLOCKS_MISSING

    def url(self):
        return f"/api/v3/channels/{self.gm.id}/messages/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.post(self.url(), {"body": BLOCKS, "body_text": "hi"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["bodyText"], "hi")
        self.assertEqual(Message.objects.filter(channel=self.gm).count(), 2)


class MessageEditBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    """The body was read as `request.data.get(...) if request.data else None` here, so it
    crashed on that line, ahead of the `body_text` line a search for `or {}` finds."""

    METHOD = "PATCH"
    MISSING_FIELD = BLOCKS_MISSING

    def url(self):
        return f"/api/v3/messages/{self.message.id}/"

    def test_a_valid_body_still_answers_what_it_did(self):
        new = [{"type": "paragraph", "content": [{"type": "text", "text": "edited"}]}]
        resp = self.client.patch(self.url(), {"body": new, "body_text": "edited"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.message.refresh_from_db()
        self.assertEqual((self.message.body, self.message.body_text), (new, "edited"))
        self.assertIsNotNone(self.message.edited_at)


class TaskCardBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "PATCH"
    MISSING_FIELD = BLOCKS_MISSING

    def url(self):
        return f"/api/v3/tasks/{self.task.task_id}/card-message/"

    def test_a_valid_body_still_answers_what_it_did(self):
        new = [{"type": "heading", "content": [{"type": "text", "text": "New title"}]}]
        resp = self.client.patch(
            self.url(), {"body": new, "body_text": "New title", "metadata": {"k": 1}}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.card.refresh_from_db()
        self.assertEqual((self.card.body, self.card.metadata), (new, {"k": 1}))


# --------------------------------------------------------------------------- #
# reaction_views_v3, read_cursor_views, pin_flag_views, todo_views            #
# --------------------------------------------------------------------------- #

EMOJI_MISSING = "emoji must be a non-empty string."


class ReactionAddBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = EMOJI_MISSING

    def url(self):
        return f"/api/v3/messages/{self.message.id}/reactions/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.post(self.url(), {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["emoji"], "👍")
        self.assertEqual(MessageReaction.objects.filter(message=self.message).count(), 1)


class ReactionRemoveBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "DELETE"
    MISSING_FIELD = EMOJI_MISSING

    def url(self):
        return f"/api/v3/messages/{self.message.id}/reactions/"

    def test_a_refused_body_removes_no_reaction(self):
        MessageReaction.objects.create(message=self.message, user=self.user, emoji="👍")
        before = self.state()
        for raw in NOT_OBJECTS:
            self.send("DELETE", self.url(), raw)
        self.assertEqual(self.state(), before)
        self.assertEqual(MessageReaction.objects.count(), 1)

    def test_a_valid_body_still_answers_what_it_did(self):
        MessageReaction.objects.create(message=self.message, user=self.user, emoji="👍")
        resp = self.client.delete(self.url(), {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(MessageReaction.objects.count(), 0)


class ReadCursorBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "PUT"
    MISSING_FIELD = "last_read_message_id is required."

    def url(self):
        return f"/api/v3/channels/{self.gm.id}/read_cursor/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.put(
            self.url(), {"last_read_message_id": str(self.message.id)}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        cursor = ReadCursor.objects.get(user=self.user, channel=self.gm)
        self.assertEqual(cursor.last_read_message_id, self.message.id)


REMIND_AT_MISSING = "remindAt must be an ISO-8601 datetime."


def _in_an_hour() -> str:
    return (timezone.now() + timedelta(hours=1)).isoformat()


class MessageReminderBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = REMIND_AT_MISSING

    def url(self):
        return f"/api/v3/messages/{self.message.id}/reminder/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.post(self.url(), {"remindAt": _in_an_hour()}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(MessageReminder.objects.filter(message=self.message).count(), 1)
        self.assertTrue(Flag.objects.filter(message=self.message, user=self.user).exists())


class TodoReminderBodyShapeTests(_MissingFieldAnswers, _ChatClient):
    METHOD = "POST"
    MISSING_FIELD = REMIND_AT_MISSING

    def url(self):
        return f"/api/v2/todo/items/{self.todo.item_id}/reminder/"

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.client.post(self.url(), {"remindAt": _in_an_hour()}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ToDoItemReminder.objects.filter(item=self.todo).count(), 1)


# --------------------------------------------------------------------------- #
# channel text fields                                                         #
# --------------------------------------------------------------------------- #

NOT_TEXT = (5, True, 1.5, ["a"], {"a": 1})
EMPTY_TEXT = (0, False, [], {}, None, "")


class ChannelTitleFieldTests(_ChatClient):
    """`title` and `profile_image_url` are text the views `.strip()` or `len()`: a number,
    a list or an object there was a 500, whatever the rest of the body held."""

    TITLE_NOT_TEXT = {"error": "title must be a string."}

    def create(self, **fields):
        body = {"kind": 2, "team_id": str(self.team.team_id), **fields}
        return self.client.post("/api/v3/channels/", body, format="json")

    def test_a_group_title_that_is_not_text_is_refused_and_creates_nothing(self):
        before = self.state()
        for title in NOT_TEXT:
            for kind in (2, 4):
                resp = self.create(kind=kind, title=title)
                self.assertEqual(
                    (resp.status_code, resp.json()), (400, self.TITLE_NOT_TEXT), (kind, title)
                )
        self.assertEqual(self.state(), before)

    def test_an_empty_group_title_keeps_its_answer(self):
        for title in EMPTY_TEXT:
            resp = self.create(title=title)
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": "title is required for GM."})
            )

    def test_a_dm_ignores_the_title_as_it_always_did(self):
        resp = self.create(kind=1, other_user_id=str(self.user2.id), title=5)
        self.assertEqual(resp.status_code, 201)

    def test_a_patched_title_that_is_not_text_is_refused_and_changes_nothing(self):
        url = f"/api/v3/channels/{self.gm.id}/"
        before = self.state()
        for title in NOT_TEXT:
            resp = self.client.patch(url, {"title": title}, format="json")
            self.assertEqual(
                (resp.status_code, resp.json()), (400, self.TITLE_NOT_TEXT), repr(title)
            )
        self.assertEqual(self.state(), before)

    def test_a_patched_image_url_that_is_not_text_is_refused_and_changes_nothing(self):
        url = f"/api/v3/channels/{self.gm.id}/"
        before = self.state()
        for value in NOT_TEXT:
            resp = self.client.patch(url, {"profile_image_url": value}, format="json")
            self.assertEqual(
                (resp.status_code, resp.json()),
                (400, {"error": "profile_image_url must be a string."}),
                repr(value),
            )
        self.assertEqual(self.state(), before)

    def test_an_empty_patched_title_keeps_its_answer(self):
        url = f"/api/v3/channels/{self.gm.id}/"
        for title in EMPTY_TEXT:
            resp = self.client.patch(url, {"title": title}, format="json")
            self.assertEqual(
                (resp.status_code, resp.json()),
                (400, {"error": "title must be a non-empty string."}),
                repr(title),
            )

    def test_a_text_patch_still_applies(self):
        url = f"/api/v3/channels/{self.gm.id}/"
        resp = self.client.patch(
            url, {"title": " Renamed ", "profile_image_url": "x.png"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.gm.refresh_from_db()
        self.assertEqual((self.gm.title, self.gm.profile_image_url), ("Renamed", "x.png"))
        # An empty image url still clears it.
        resp = self.client.patch(url, {"profile_image_url": ""}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.profile_image_url, "")

    def test_a_pm_title_that_is_not_text_is_refused_and_renames_nothing(self):
        url = f"/api/v3/channels/{self.pm.id}/"
        before = self.state()
        for title in NOT_TEXT:
            resp = self.client.patch(url, {"title": title}, format="json")
            self.assertEqual(
                (resp.status_code, resp.json()), (400, self.TITLE_NOT_TEXT), repr(title)
            )
        self.assertEqual(self.state(), before)
        for title in EMPTY_TEXT:
            resp = self.client.patch(url, {"title": title}, format="json")
            self.assertEqual(resp.status_code, 400, repr(title))
            self.assertNotEqual(resp.json(), self.TITLE_NOT_TEXT, repr(title))
