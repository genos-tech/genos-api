"""MIP-2063 (decision D4 of MIP-1998): the risky kinds MIP-2009 left without a notice.

Next to deletes and 10+ item changes, four more things alert by default, and each can be switched
off: who can open a note (`ai_app_note_sharing`), who is in a project (`ai_app_project_members`),
a burst of refused calls (`ai_app_refusal_burst`) and a new app or API key on the account
(`ai_app_app_connected`). They ride the same feed row, toast, push and switches as the rest:

  * on for a member who never touched a switch, and an explicit off is kept;
  * the master `enable_ai_apps` still gates them;
  * a call that changed nothing (adding someone already in the project) or was refused makes none;
  * the burst is told once per credential per hour, never for a person's own browser session;
  * a re-consent to a known app is not a new app.
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import override_settings

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import SCOPE_READ, ApiKey
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import ai_app_switches as switches
from origin.services import mcp_notice, throttles
from origin.services.webpush_dispatch import _push_spec
from origin.tests.ai_app_defaults import restore_real_defaults
from origin.tests.security_clock import MidMinute
from origin.tests.test_mcp_notice import NoticeBase, notices
from origin.tests.test_mcp_oauth_consent import ConsentBase

NEW_KEYS = (
    "ai_app_note_sharing",
    "ai_app_project_members",
    "ai_app_refusal_burst",
    "ai_app_app_connected",
)


def _pairs(rows):
    return [(r.meta["verb"], r.meta["kind"], r.meta.get("action")) for r in rows]


class DefaultsTests(NoticeBase):
    def test_the_four_new_kinds_are_registered_and_on_by_default(self):
        restore_real_defaults(self)
        for key in NEW_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, switches.KEYS)
                self.assertTrue(switches.DEFAULTS[key])
                self.assertTrue(switches.allows({}, key, 1))
                self.assertFalse(switches.allows({key: False}, key, 1))
        self.assertEqual(switches.key_for("sharing", "note"), "ai_app_note_sharing")

    def test_the_new_kinds_have_a_push_category_that_is_on_by_default(self):
        from origin.services import webpush_gating

        restore_real_defaults(self)
        for key in NEW_KEYS:
            self.assertTrue(webpush_gating._PUSH_DEFAULTS[key], key)


class SharingTests(NoticeBase):
    def setUp(self):
        super().setUp()
        restore_real_defaults(self)
        self.call("create_note", note_type="personal", title="Plan", content_text="x")
        self.note = PersonalNoteMaster.objects.get(title="Plan")
        # Creating it is not a risky kind: nothing yet.
        self.assertEqual(notices(self.user), [])

    def share(self, action="share", **extra):
        args = {
            "action": action,
            "note_type": "personal",
            "note_id": self.note.note_id,
            "user_id": str(self.user2.id),
            **extra,
        }
        return self.call("manage_note_sharing", **args)

    def test_sharing_a_note_tells_its_owner_by_default(self):
        self.share(role="editor")
        row = self.only()
        self.assertEqual(_pairs([row]), [("sharing", "note", "share")])
        self.assertEqual(row.meta["tool"], "manage_note_sharing")
        self.assertEqual(row.meta["count"], 1)
        self.assertNotIn("bulk", row.meta)
        self.assertEqual(row.recipient_id, self.user.id)
        # Pushed under its own switch, not under the group's.
        self.assertEqual(_push_spec(row)["category"], "ai_app_note_sharing")
        self.assertEqual(mcp_notice.sentence(row.meta).split()[0], "shared")

    def test_taking_a_share_away_tells_too(self):
        self.share(role="viewer")
        self.share(action="unshare")
        self.assertEqual(
            _pairs(notices(self.user)),
            [("sharing", "note", "share"), ("sharing", "note", "unshare")],
        )
        self.assertTrue(mcp_notice.sentence(notices(self.user)[1].meta).startswith("stopped"))

    def test_a_refused_share_makes_no_notice(self):
        # Sharing with oneself is refused by the page's rules: nothing changed, nothing to tell.
        text, is_error = self._tool_text(
            self._call(
                "manage_note_sharing",
                {
                    "action": "share",
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "user_id": str(self.user.id),
                    "role": "editor",
                },
            )
        )
        self.assertTrue(is_error, text)
        self.assertEqual(notices(self.user), [])

    def test_an_explicit_off_is_kept(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_note_sharing": False}
        )
        self.share(role="editor")
        self.assertEqual(notices(self.user), [])

    def test_the_group_master_off_silences_it(self):
        NotificationPreference.objects.create(user=self.user, enable_ai_apps=False)
        self.share(role="editor")
        self.assertEqual(notices(self.user), [])


class ProjectMembersTests(NoticeBase):
    def setUp(self):
        super().setUp()
        restore_real_defaults(self)

    def members(self, action, **extra):
        return self.call(
            "manage_project_members",
            action=action,
            project_id=self.project.project_id,
            user_id=str(self.user2.id),
            **extra,
        )

    def test_adding_a_member_tells_the_owner_by_default_and_names_the_project(self):
        self.members("add")
        row = self.only()
        self.assertEqual(_pairs([row]), [("members", "project", "add")])
        item = row.meta["items"][0]
        self.assertEqual(item["kind"], "project")
        self.assertEqual(item["projectId"], self.project.project_id)
        self.assertEqual(item["displayId"], "WRD")
        self.assertEqual(item["title"], "Website Redesign")
        self.assertEqual(row.meta["projectId"], self.project.project_id)
        self.assertEqual(_push_spec(row)["category"], "ai_app_project_members")
        self.assertEqual(mcp_notice.sentence(row.meta), "added someone to project WRD")

    def test_a_role_change_and_a_removal_tell_too(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.members("set_role", role="editor")
        self.members("remove")
        self.assertEqual(
            _pairs(notices(self.user)),
            [("members", "project", "set_role"), ("members", "project", "remove")],
        )

    def test_setting_the_role_they_already_have_changes_nothing_and_is_silent(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.members("set_role", role="editor")
        self.assertEqual(len(notices(self.user)), 1)
        self.members("set_role", role="editor")
        self.assertEqual(len(notices(self.user)), 1, "the second set_role changed nothing")

    def test_adding_someone_already_in_the_project_changes_nothing_and_is_silent(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.members("add")
        self.assertEqual(notices(self.user), [])

    def test_a_refused_change_is_silent(self):
        # A person who is not on the team cannot be added.
        text, is_error = self._tool_text(
            self._call(
                "manage_project_members",
                {
                    "action": "add",
                    "project_id": self.project.project_id,
                    "user_id": "00000000-0000-0000-0000-000000000001",
                },
            )
        )
        self.assertTrue(is_error, text)
        self.assertEqual(notices(self.user), [])

    def test_an_explicit_off_is_kept(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_project_members": False}
        )
        self.members("add")
        self.assertEqual(notices(self.user), [])

    def test_approving_a_join_request_is_a_member_change(self):
        # The tool's answer, as `approve_join_request` builds it.
        payload = {
            "inbox_item_id": "abc",
            "project_id": self.project.project_id,
            "user_id": str(self.user2.id),
            "already_member": False,
        }
        notice = mcp_notice.build_notice("approve_join_request", {}, payload)
        self.assertEqual((notice.verb, notice.kind, notice.action), ("members", "project", "add"))
        self.assertEqual(notice.items[0]["displayId"], "WRD")

    def test_approving_a_request_of_someone_already_in_is_silent(self):
        payload = {"project_id": self.project.project_id, "already_member": True}
        self.assertIsNone(mcp_notice.build_notice("approve_join_request", {}, payload))
        self.assertIsNone(mcp_notice.build_notice("approve_join_request", {}, {}))


@override_settings(SECURITY_FORBIDDEN_BURST=3)
class RefusalBurstTests(MidMinute, NoticeBase):
    def setUp(self):
        super().setUp()
        restore_real_defaults(self)
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        stranger = self.user2
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=stranger
        )
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Theirs",
            code="THR",
            owner=stranger,
            project_system_user=stranger,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=stranger)
        self.foreign_task = TaskMaster.objects.create(
            team=other_team,
            project=other_project,
            reporter=stranger,
            assignee=stranger,
            title="Not yours",
            status="Open",
            project_task_number=1,
        )
        self._key(scope=SCOPE_READ)

    def walk(self, n):
        for _ in range(n):
            self._call("get_task", {"task_id": self.foreign_task.task_id})

    def test_a_burst_tells_the_credentials_owner_once_by_default(self):
        self.walk(3)
        row = self.only()
        self.assertEqual(_pairs([row]), [("refused", "burst", None)])
        self.assertEqual(row.meta["refused"], 3)
        self.assertEqual(row.meta["windowSeconds"], 60)
        # One notice: `count` stays 1, so it is not read as a bulk change.
        self.assertEqual(row.meta["count"], 1)
        self.assertNotIn("bulk", row.meta)
        self.assertEqual(row.recipient_id, self.user.id)
        self.assertEqual(_push_spec(row)["category"], "ai_app_refusal_burst")

    def test_below_the_threshold_nothing_is_told(self):
        self.walk(2)
        self.assertEqual(notices(self.user), [])

    def test_a_long_scan_is_one_notice_an_hour_not_one_a_minute(self):
        self.walk(8)
        self.assertEqual(len(notices(self.user)), 1)

    def test_a_mistake_is_not_a_burst(self):
        for _ in range(10):
            self._call("get_task", {"task_id": 999999999})
        self.assertEqual(notices(self.user), [])

    def test_an_explicit_off_is_kept(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_refusal_burst": False}
        )
        self.walk(3)
        self.assertEqual(notices(self.user), [])

    def test_a_persons_own_session_is_not_an_ai_app_and_is_not_told(self):
        # A burst of 403s from the web app's own session (no credential): the security alert
        # fires as before, but there is no key or app to name.
        self.client.credentials()  # drop the key
        self.authenticate()
        for _ in range(4):
            self.client.get("/api/v2/team/api-keys/?team_id=" + "0" * 32)
        self.assertEqual(notices(self.user), [])


class NewKeyTests(NoticeBase):
    def setUp(self):
        super().setUp()
        restore_real_defaults(self)
        self.authenticate()  # the member's own session, as the settings page calls it

    def make_key(self, **body):
        return self.client.post("/api/v2/api-keys/", {"name": "CI bot", **body}, format="json")

    def test_a_new_api_key_tells_its_owner_by_default(self):
        res = self.make_key()
        self.assertEqual(res.status_code, 201, res.data)
        row = self.only()
        self.assertEqual(_pairs([row]), [("connected", "app", "api_key")])
        self.assertEqual(row.meta["app"], "CI bot")
        self.assertEqual(row.meta["credentialKind"], "api_key")
        self.assertEqual(row.meta["items"][0]["title"], "CI bot")
        self.assertEqual(row.meta["items"][0]["url"], "")
        self.assertEqual(_push_spec(row)["category"], "ai_app_app_connected")
        # Never the secret.
        self.assertNotIn(res.data["key"], str(row.meta))

    def test_a_key_that_is_refused_makes_no_notice(self):
        res = self.make_key(scope="admin")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(notices(self.user), [])

    def test_an_explicit_off_is_kept(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_app_connected": False}
        )
        self.assertEqual(self.make_key().status_code, 201)
        self.assertEqual(notices(self.user), [])

    def test_a_failing_notice_never_fails_the_key_creation(self):
        from unittest import mock

        with mock.patch.object(Activity.objects, "create", side_effect=RuntimeError("boom")):
            res = self.make_key()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(ApiKey.objects.filter(name="CI bot").exists())


class NewAppTests(ConsentBase):
    # `ConsentBase` is not a `McpBase`: the test-only defaults were never patched, so this sees the
    # real ones.
    def rows(self):
        return list(Activity.objects.filter(recipient=self.user, activity_type=ActivityType.AI_APP))

    def test_connecting_a_new_app_tells_its_owner_by_default(self):
        res = self.decide()
        self.assertEqual(res.status_code, 200, res.content)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        meta = rows[0].meta
        self.assertEqual(
            (meta["verb"], meta["kind"], meta["action"]), ("connected", "app", "oauth")
        )
        self.assertEqual(meta["app"], "Claude")
        self.assertEqual(meta["credentialKind"], "oauth")
        self.assertEqual(rows[0].team_id, self.team.team_id)
        self.assertEqual(_push_spec(rows[0])["category"], "ai_app_app_connected")

    def test_consenting_again_to_a_known_app_is_not_a_new_app(self):
        self.decide()
        self.decide()
        self.assertEqual(len(self.rows()), 1)

    def test_connecting_again_after_a_disconnect_is_a_new_app_and_tells(self):
        from origin.models.common.mcp_connection_models import McpConnection
        from origin.services import mcp_connections

        self.decide()
        mcp_connections.revoke_connection(McpConnection.objects.get(user=self.user))
        self.decide()
        self.assertEqual(len(self.rows()), 2, "a live connection came back: its owner is told")

    def test_a_denial_tells_nothing(self):
        res = self.decide(allow=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.rows(), [])

    def test_an_explicit_off_is_kept(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_app_connected": False}
        )
        self.assertEqual(self.decide().status_code, 200)
        self.assertEqual(self.rows(), [])
