"""Genos Skills data model and REST API (MIP-2040).

What matters, in the order the design lists it: ACL by visibility (private / team / public, and a
stranger's id is the same 404 as a missing one); a name is unique per owner and refused when it is
reserved or malformed; a published version never changes; the body note stays out of My Notes and
out of every search ACL but its owner's; the plan's count; copy, install and star; SKILL.md in
and out; and a departing account leaves no skill behind.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.skills.skill_models import Skill, SkillInstall, SkillStar, SkillVersion
from origin.search_engine.agent.acl import personal_note_acl_user_ids
from origin.search_engine.note_context import FolderAclResolver
from origin.services.skills import service
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

LIST = "/api/v2/skills/"
BODY = "# Weekly update\n\n## Steps\n1. Read the open tasks.\n2. Write three lines."


def detail(skill_id, suffix=""):
    return f"{LIST}{skill_id}/{suffix}"


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class SkillBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The plan's count is its own test: everywhere else the member is on an unlimited plan.
        tier = mock.patch("origin.search_engine.quota.get_effective_tier", return_value="max")
        tier.start()
        self.addCleanup(tier.stop)
        # The Hub's account gates (plan, verified email, account age, handle, the terms click, the
        # secrets scan, the daily limits) are tested in `test_skills_hub.py`. Everywhere else the
        # tests are about what happens to a skill that IS public, so the gate lets them through.
        self.gate_patch = mock.patch("origin.services.skills.hub.require_public_gate")
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)
        # A star counts toward the public total only from an aged, verified account (also the Hub's
        # tests): the fixtures' accounts are brand new, so every star counts here.
        self.star_patch = mock.patch("origin.services.skills.hub.star_counts", return_value=True)
        self.star_patch.start()
        self.addCleanup(self.star_patch.stop)
        self.team_id = str(self.team.team_id)
        # An outsider: another team, no membership in this one.
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x-pass-123"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)

    def as_user(self, user):
        self.client.credentials()
        self.authenticate(user)

    def make(self, user=None, **over):
        """Create a skill over REST as `user` (default: self.user)."""
        user = user or self.user
        self.as_user(user)
        team = self.team_id if user != self.outsider else str(self.other_team.team_id)
        data = {
            "team_id": team,
            "title": "Weekly update",
            "slug": "weekly-update",
            "description": "Write the weekly update.",
            "tags": ["reports"],
            "bodyMarkdown": BODY,
            **over,
        }
        return self.client.post(LIST, data, format="json")

    def make_ok(self, user=None, **over):
        res = self.make(user, **over)
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["skill"]


class FeatureSwitchTests(BaseAPITestCase):
    def test_every_route_is_a_404_while_the_switch_is_off(self):
        self.authenticate()
        team = str(self.team.team_id)
        self.assertEqual(self.client.get(f"{LIST}?team_id={team}").status_code, 404)
        res = self.client.post(LIST, {"team_id": team, "title": "x"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Skill.objects.count(), 0)

    def test_the_default_is_off(self):
        from django.conf import settings

        self.assertFalse(settings.GENOS_SKILLS)
        self.assertFalse(settings.GENOS_SKILLS_PUBLIC)

    def test_anonymous_is_refused_before_anything(self):
        self.assertIn(self.client.get(LIST).status_code, (401, 403))


class PrivateSkillTests(SkillBase):
    def test_the_owner_creates_reads_changes_and_deletes(self):
        skill = self.make_ok()
        self.assertEqual(skill["visibility"], "private")
        self.assertTrue(skill["mine"])
        self.assertEqual(skill["slug"], "weekly-update")

        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.status_code, 200)
        self.assertIn("Read the open tasks", res.json()["skill"]["bodyMarkdown"])

        res = self.client.patch(
            detail(skill["id"]),
            {"description": "New words.", "bodyMarkdown": "# T\n\n1. One step."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["skill"]["description"], "New words.")
        self.assertEqual(res.json()["skill"]["bodyMarkdown"], "# T\n\n1. One step.")
        # A private skill has no readers, so no version is cut.
        self.assertEqual(res.json()["skill"]["currentVersionNo"], 0)

        self.assertEqual(self.client.delete(detail(skill["id"])).status_code, 204)
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).exists())

    def test_a_teammate_and_an_outsider_get_the_same_404_as_a_missing_id(self):
        skill = self.make_ok()
        missing = "11111111-2222-3333-4444-555555555555"
        for user in (self.user2, self.outsider):
            self.as_user(user)
            for call in (
                lambda: self.client.get(detail(skill["id"])),
                lambda: self.client.patch(detail(skill["id"]), {"title": "x"}, format="json"),
                lambda: self.client.delete(detail(skill["id"])),
                lambda: self.client.get(detail(skill["id"], "versions/")),
                lambda: self.client.post(detail(skill["id"], "star/")),
                lambda: self.client.post(detail(skill["id"], "install/")),
                lambda: self.client.get(detail(skill["id"], "export/")),
            ):
                self.assertEqual(call().status_code, 404)
            self.assertEqual(self.client.get(detail(missing)).status_code, 404)
        self.assertTrue(Skill.objects.filter(pk=skill["id"]).exists())

    def test_a_private_skill_is_in_no_list_but_its_owners(self):
        self.make_ok()
        self.as_user(self.user2)
        for tab in ("mine", "team", "installed", "starred"):
            res = self.client.get(f"{LIST}?team_id={self.team_id}&tab={tab}")
            self.assertEqual(res.status_code, 200, tab)
            self.assertEqual(res.json()["skills"], [], tab)
        self.as_user(self.user)
        mine = self.client.get(f"{LIST}?team_id={self.team_id}&tab=mine").json()["skills"]
        self.assertEqual([s["slug"] for s in mine], ["weekly-update"])

    def test_a_non_member_cannot_create_in_a_team(self):
        self.as_user(self.outsider)
        res = self.client.post(LIST, {"team_id": self.team_id, "title": "x"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Skill.objects.count(), 0)

    def test_a_bad_team_id_is_a_400_not_a_500(self):
        self.as_user(self.user)
        res = self.client.post(LIST, {"team_id": "nope", "title": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client.get(f"{LIST}?team_id=nope").status_code, 400)


class TeamSkillTests(SkillBase):
    def test_a_team_skill_is_read_by_members_from_its_published_version(self):
        skill = self.make_ok(visibility="team")
        self.assertEqual(skill["publishedVersionNo"], 1)

        self.as_user(self.user2)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()["skill"]
        self.assertFalse(body["mine"])
        self.assertFalse(body["canEdit"])
        self.assertEqual(body["ownerName"], "testuser")
        self.assertNotIn("noteId", body)  # the body note is the owner's, not the reader's
        self.assertIn("Read the open tasks", body["bodyMarkdown"])

        team_tab = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team").json()["skills"]
        self.assertEqual([s["id"] for s in team_tab], [skill["id"]])

    def test_a_team_member_cannot_change_it_but_gets_403_not_404(self):
        skill = self.make_ok(visibility="team")
        self.as_user(self.user2)
        self.assertEqual(
            self.client.patch(detail(skill["id"]), {"title": "Hijack"}, format="json").status_code,
            403,
        )
        self.assertEqual(self.client.delete(detail(skill["id"])).status_code, 403)
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 403)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).title, "Weekly update")

    def test_a_guest_and_an_outsider_cannot_see_a_team_skill(self):
        skill = self.make_ok(visibility="team")
        # A guest has a project membership and no team row: they are not a team member.
        guest = User.objects.create_user(username="guest", email="g@example.com", password="x-1234")
        for user in (guest, self.outsider):
            self.as_user(user)
            self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)
            self.assertEqual(self.client.get(detail(skill["id"], "versions/")).status_code, 404)

    def test_a_removed_member_loses_a_team_skill(self):
        skill = self.make_ok(visibility="team")
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.as_user(self.user2)
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)

    def test_a_save_stays_private_and_only_publish_makes_the_version_readers_see(self):
        """MIP-2177: Save keeps a team skill's edit as the owner's draft."""
        skill = self.make_ok(visibility="team")
        self.as_user(self.user)
        res = self.client.patch(
            detail(skill["id"]),
            {"bodyMarkdown": "# T\n\n1. A new step.", "title": "Renamed", "tags": ["new"]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        saved = res.json()["skill"]
        # The owner sees the draft, and is told it is not published.
        self.assertEqual(saved["publishedVersionNo"], 1)
        self.assertEqual(saved["currentVersionNo"], 1)
        self.assertEqual(saved["title"], "Renamed")
        self.assertIn("A new step", saved["bodyMarkdown"])
        self.assertTrue(saved["hasUnpublishedChanges"])
        self.assertEqual(sorted(saved["unpublishedChanges"]), ["bodyMarkdown", "tags", "title"])
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)

        # A teammate still gets the old text, title and tags: page, list, versions, export.
        self.as_user(self.user2)
        seen = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertIn("Read the open tasks", seen["bodyMarkdown"])
        self.assertNotIn("A new step", seen["bodyMarkdown"])
        self.assertEqual((seen["title"], seen["tags"]), ("Weekly update", ["reports"]))
        self.assertNotIn("hasUnpublishedChanges", seen)
        self.assertNotIn("unpublishedChanges", seen)
        team_tab = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team").json()["skills"]
        self.assertEqual([s["title"] for s in team_tab], ["Weekly update"])
        listed = self.client.get(detail(skill["id"], "versions/")).json()["versions"]
        self.assertEqual([v["versionNo"] for v in listed], [1])
        exported = self.client.get(detail(skill["id"], "export/")).json()["content"]
        self.assertNotIn("A new step", exported)

        # Publishing makes the version; then readers follow it.
        self.as_user(self.user)
        res = self.client.post(
            detail(skill["id"], "publish/"), {"changelog": "New step"}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["version"]["versionNo"], 2)
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 2)
        again = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(again["publishedVersionNo"], 2)
        self.assertFalse(again["hasUnpublishedChanges"])
        self.assertEqual(again["unpublishedChanges"], [])
        self.as_user(self.user2)
        seen = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertIn("A new step", seen["bodyMarkdown"])
        self.assertEqual(seen["title"], "Renamed")
        listed = self.client.get(detail(skill["id"], "versions/")).json()["versions"]
        self.assertEqual([v["versionNo"] for v in listed], [2, 1])

    def test_the_owners_page_says_what_differs_from_the_published_version(self):
        skill = self.make_ok(visibility="team")
        self.as_user(self.user)
        page = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertFalse(page["hasUnpublishedChanges"])
        self.assertEqual(page["unpublishedChanges"], [])

        self.client.patch(detail(skill["id"]), {"toolLevel": "act"}, format="json")
        page = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(page["unpublishedChanges"], ["toolLevel"])
        # Putting it back is no change at all.
        self.client.patch(detail(skill["id"]), {"toolLevel": "read"}, format="json")
        page = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertFalse(page["hasUnpublishedChanges"])

        # A text edit made in the collaborative editor (the note itself, not a PATCH) counts too.
        PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).update(
            body=[{"type": "paragraph", "content": [{"type": "text", "text": "Edited live."}]}]
        )
        page = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(page["unpublishedChanges"], ["bodyMarkdown"])
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 201)
        page = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertFalse(page["hasUnpublishedChanges"])

    def test_sharing_a_private_skill_publishes_its_text_and_a_resave_does_not(self):
        skill = self.make_ok()
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Draft."}, format="json")
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 0)
        # Choosing "team" is the explicit share: it publishes what the owner has now.
        res = self.client.patch(detail(skill["id"]), {"visibility": "team"}, format="json")
        self.assertEqual(res.json()["skill"]["publishedVersionNo"], 1)
        self.assertFalse(res.json()["skill"]["hasUnpublishedChanges"])
        self.as_user(self.user2)
        self.assertIn(
            "Draft.", self.client.get(detail(skill["id"])).json()["skill"]["bodyMarkdown"]
        )
        # After the share, a save is a draft again.
        self.as_user(self.user)
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Later."}, format="json")
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)

    def test_a_draft_save_does_not_move_a_skill_in_the_teams_list(self):
        first = self.make_ok(visibility="team", slug="first-skill", title="First skill")
        second = self.make_ok(visibility="team", slug="second-skill", title="Second skill")

        def team_tab():
            self.as_user(self.user2)
            rows = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team").json()["skills"]
            self.as_user(self.user)
            return [r["id"] for r in rows]

        self.assertEqual(team_tab(), [second["id"], first["id"]])
        # An edit to the older skill is private: it keeps its place for the teammate.
        self.client.patch(detail(first["id"]), {"description": "Draft"}, format="json")
        self.assertEqual(team_tab(), [second["id"], first["id"]])
        # Publishing it is what moves it up.
        self.client.post(detail(first["id"], "publish/"), {}, format="json")
        self.assertEqual(team_tab(), [first["id"], second["id"]])

    def test_saving_with_the_same_visibility_does_not_publish(self):
        skill = self.make_ok(visibility="team")
        res = self.client.patch(
            detail(skill["id"]),
            {"bodyMarkdown": "# T\n\n1. Fresh.", "visibility": "team"},
            format="json",
        )
        self.assertEqual(res.json()["skill"]["publishedVersionNo"], 1)
        self.assertTrue(res.json()["skill"]["hasUnpublishedChanges"])
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)

    def test_a_teammate_cannot_save_or_publish_the_owners_draft(self):
        skill = self.make_ok(visibility="team")
        self.client.patch(detail(skill["id"]), {"title": "Draft title"}, format="json")
        self.as_user(self.user2)
        self.assertEqual(
            self.client.patch(detail(skill["id"]), {"title": "Mine"}, format="json").status_code,
            403,
        )
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 403)
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)
        self.as_user(self.outsider)
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 404)

    def test_a_private_skill_is_unchanged_by_the_draft_rule(self):
        skill = self.make_ok()
        res = self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Mine.", "title": "T2"}, format="json"
        )
        out = res.json()["skill"]
        self.assertEqual(
            (out["title"], out["publishedVersionNo"], out["currentVersionNo"]), ("T2", 0, 0)
        )
        self.assertIn("Mine.", out["bodyMarkdown"])
        self.assertFalse(out["hasUnpublishedChanges"])
        self.assertEqual(out["unpublishedChanges"], [])
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 0)
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 400)

    def test_going_back_to_private_stops_every_other_reader_at_once(self):
        skill = self.make_ok(visibility="team")
        self.as_user(self.user)
        self.client.patch(detail(skill["id"]), {"visibility": "private"}, format="json")
        self.as_user(self.user2)
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)

    def test_a_hidden_or_taken_down_skill_is_not_readable(self):
        skill = self.make_ok(visibility="team")
        for state in ("hidden_by_team", "reported_hidden", "taken_down"):
            Skill.objects.filter(pk=skill["id"]).update(status=state)
            self.as_user(self.user2)
            self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404, state)
            self.as_user(self.user)
            self.assertEqual(self.client.get(detail(skill["id"])).status_code, 200, state)


class PublicSkillTests(SkillBase):
    def make_public(self, **over):
        return self.make_ok(visibility="public", **over)

    @override_settings(GENOS_SKILLS_PUBLIC=False)
    def test_public_is_refused_while_its_switch_is_off(self):
        res = self.make(visibility="public")
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(Skill.objects.count(), 0)  # the whole create rolled back, note included
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)

    def test_any_signed_in_user_reads_the_published_version(self):
        skill = self.make_public()
        self.as_user(self.outsider)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()["skill"]
        self.assertIn("Read the open tasks", body["bodyMarkdown"])
        # An outside author is not named until the Hub has a public handle.
        self.assertNotIn("ownerName", body)
        self.assertNotIn("noteId", body)

    def test_the_owners_edits_do_not_reach_readers_until_published(self):
        skill = self.make_public()
        self.as_user(self.user)
        res = self.client.patch(
            detail(skill["id"]),
            {"bodyMarkdown": "# T\n\n1. Secret new step.", "title": "Renamed"},
            format="json",
        )
        self.assertEqual(res.json()["skill"]["publishedVersionNo"], 1)

        self.as_user(self.outsider)
        body = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertNotIn("Secret new step", body["bodyMarkdown"])
        self.assertEqual(body["title"], "Weekly update")

        self.as_user(self.user)
        self.assertEqual(
            self.client.post(
                detail(skill["id"], "publish/"), {"changelog": "v2"}, format="json"
            ).status_code,
            201,
        )
        self.as_user(self.outsider)
        body = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertIn("Secret new step", body["bodyMarkdown"])
        self.assertEqual(body["title"], "Renamed")

    def test_the_content_checklist_refuses_chips_media_and_missing_words(self):
        self.as_user(self.user)
        res = self.make(visibility="public", tags=[])
        self.assertEqual(res.status_code, 400)
        self.assertIn("at least one tag", res.json()["error"])

        res = self.make(visibility="public", description="")
        self.assertIn("description", res.json()["error"])

        res = self.make(visibility="public", bodyMarkdown="")
        self.assertIn("empty", res.json()["error"])

        # A chip and an image the editor put in the live note.
        skill = self.make_ok(slug="with-chip")
        blocks = [
            {
                "type": "paragraph",
                "content": [{"type": "hashTask", "props": {"taskId": "1", "displayId": "MIP-1"}}],
            },
            {"type": "image", "props": {"url": "https://x.example/p.png"}},
            {
                "type": "paragraph",
                "content": [{"type": "link", "href": "task:12", "content": []}],
            },
        ]
        PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).update(body=blocks)
        res = self.client.patch(detail(skill["id"]), {"visibility": "public"}, format="json")
        self.assertEqual(res.status_code, 400)
        error = res.json()["error"]
        self.assertIn("chips", error)
        self.assertIn("images and attachments", error)
        self.assertIn("links into your workspace", error)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).visibility, "private")

    def test_install_pins_a_version_and_an_update_needs_an_explicit_yes(self):
        skill = self.make_public()
        self.as_user(self.outsider)
        res = self.client.post(detail(skill["id"], "install/"))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["installedVersionNo"], 1)
        # Idempotent, and counted once.
        self.assertEqual(self.client.post(detail(skill["id"], "install/")).status_code, 201)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).install_count, 1)

        listed = self.client.get(f"{LIST}?tab=installed").json()["skills"]
        self.assertEqual([s["id"] for s in listed], [skill["id"]])

        self.as_user(self.user)
        self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Changed."}, format="json"
        )
        self.client.post(detail(skill["id"], "publish/"), {}, format="json")

        self.as_user(self.outsider)
        seen = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(seen["installedVersionNo"], 1)
        self.assertTrue(seen["updateAvailable"])
        res = self.client.patch(detail(skill["id"], "install/"))
        self.assertEqual(res.json()["installedVersionNo"], 2)

        self.assertEqual(self.client.delete(detail(skill["id"], "install/")).status_code, 204)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).install_count, 0)
        self.assertEqual(self.client.get(f"{LIST}?tab=installed").json()["skills"], [])

    def test_only_a_public_skill_of_someone_else_is_installed(self):
        team_skill = self.make_ok(slug="for-team", visibility="team")
        self.as_user(self.user2)
        self.assertEqual(self.client.post(detail(team_skill["id"], "install/")).status_code, 400)
        public = self.make_public(slug="mine-public")
        self.as_user(self.user)
        self.assertEqual(self.client.post(detail(public["id"], "install/")).status_code, 400)

    def test_a_taken_down_skill_leaves_installs_without_access(self):
        skill = self.make_public()
        self.as_user(self.outsider)
        self.client.post(detail(skill["id"], "install/"))
        Skill.objects.filter(pk=skill["id"]).update(status="taken_down")
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)
        self.assertEqual(self.client.get(f"{LIST}?tab=installed").json()["skills"], [])

    def test_a_star_is_one_per_account_and_counts(self):
        skill = self.make_public()
        self.as_user(self.outsider)
        self.assertEqual(self.client.post(detail(skill["id"], "star/")).status_code, 201)
        self.assertEqual(self.client.post(detail(skill["id"], "star/")).status_code, 200)
        self.as_user(self.user2)
        self.assertEqual(self.client.post(detail(skill["id"], "star/")).status_code, 201)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).star_count, 2)
        self.assertEqual(SkillStar.objects.filter(skill_id=skill["id"]).count(), 2)

        starred = self.client.get(f"{LIST}?tab=starred").json()["skills"]
        self.assertEqual([s["id"] for s in starred], [skill["id"]])
        self.assertTrue(starred[0]["starred"])

        self.assertEqual(self.client.delete(detail(skill["id"], "star/")).status_code, 204)
        self.assertEqual(self.client.delete(detail(skill["id"], "star/")).status_code, 204)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).star_count, 1)

    def test_a_private_skill_cannot_be_starred_by_a_stranger(self):
        skill = self.make_ok()
        self.as_user(self.outsider)
        self.assertEqual(self.client.post(detail(skill["id"], "star/")).status_code, 404)

    def test_a_reader_can_copy_and_the_copy_is_private_and_theirs(self):
        skill = self.make_public()
        self.as_user(self.outsider)
        res = self.client.post(
            detail(skill["id"], "copy/"),
            {"team_id": str(self.other_team.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        copy = res.json()["skill"]
        self.assertEqual(copy["visibility"], "private")
        self.assertEqual(copy["forkedFromId"], skill["id"])
        self.assertTrue(copy["mine"])
        self.assertEqual(copy["slug"], "weekly-update")  # free in their own list
        self.assertEqual(copy["publishedVersionNo"], 0)  # starts again, does not follow
        row = Skill.objects.get(pk=copy["id"])
        self.assertEqual(row.forked_from_version_no, 1)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).copy_count, 1)
        self.assertIn(
            "Read the open tasks",
            self.client.get(detail(copy["id"])).json()["skill"]["bodyMarkdown"],
        )

        # Copying again takes the next free name.
        again = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": str(self.other_team.team_id)}, format="json"
        )
        self.assertEqual(again.json()["skill"]["slug"], "weekly-update-2")

    def test_nobody_copies_a_private_skill_and_nobody_copies_into_a_foreign_team(self):
        skill = self.make_ok()
        self.as_user(self.outsider)
        res = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": str(self.other_team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        public = self.make_public(slug="pub")
        self.as_user(self.outsider)
        res = self.client.post(
            detail(public["id"], "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Skill.objects.filter(owner=self.outsider).count(), 0)

    def test_a_team_skill_is_copied_by_a_teammate_only(self):
        skill = self.make_ok(visibility="team")
        self.as_user(self.user2)
        res = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.as_user(self.outsider)
        res = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": str(self.other_team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 404)


class NameTests(SkillBase):
    def test_a_name_is_unique_per_owner(self):
        self.make_ok()
        res = self.make()
        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn("weekly-update", res.json()["error"])
        self.assertEqual(Skill.objects.filter(owner=self.user).count(), 1)
        # The refused create left no orphan body note behind.
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)

    def test_two_owners_may_use_the_same_name(self):
        self.make_ok()
        self.make_ok(user=self.user2)
        self.assertEqual(Skill.objects.filter(slug="weekly-update").count(), 2)

    def test_one_owner_may_use_the_same_name_in_two_teams(self):
        self.make_ok()
        second = TeamMaster.objects.create(
            team_name="Second", team_email="s@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=second, attendee=self.user)
        self.as_user(self.user)
        res = self.client.post(
            LIST,
            {"team_id": str(second.team_id), "title": "Weekly update", "slug": "weekly-update"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_two_teammates_with_one_name_both_stay_visible_to_the_team(self):
        a = self.make_ok(visibility="team")
        b = self.make_ok(user=self.user2, visibility="team")
        self.as_user(self.outsider)
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)
        listed = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team").json()["skills"]
        self.assertEqual({s["id"] for s in listed}, {a["id"], b["id"]})

    def test_a_rename_into_a_taken_name_is_refused_and_a_free_one_works(self):
        self.make_ok()
        other = self.make_ok(slug="second", title="Second")
        res = self.client.patch(detail(other["id"]), {"slug": "weekly-update"}, format="json")
        self.assertEqual(res.status_code, 409)
        res = self.client.patch(detail(other["id"]), {"slug": "third"}, format="json")
        self.assertEqual(res.json()["skill"]["slug"], "third")
        # Keeping its own name is not a clash.
        res = self.client.patch(detail(other["id"]), {"slug": "third"}, format="json")
        self.assertEqual(res.status_code, 200)

    def test_reserved_and_malformed_names_are_refused(self):
        self.as_user(self.user)
        for bad in (
            "help",
            "hub",
            "skills",
            "settings",
            "catch-me-up",
            "A b",
            "x",
            "-ab",
            "ab-",
            "a--b",
            "a_b",
            "é",
            "a" * 41,
            "",
        ):
            res = self.make(slug=bad, title="Name test")
            if bad == "":
                # No name given: made from the title.
                self.assertEqual(res.status_code, 201, res.content)
                continue
            self.assertEqual(res.status_code, 400, (bad, res.content))
        self.assertEqual(self.make(slug="Weekly-Update-2", title="Case").status_code, 201)
        self.assertTrue(Skill.objects.filter(slug="weekly-update-2").exists())

    def test_a_name_made_from_the_title_takes_the_next_free_one(self):
        first = self.make_ok(slug="", title="Daily Standup!")
        second = self.make_ok(slug="", title="Daily Standup!")
        self.assertEqual(first["slug"], "daily-standup")
        self.assertEqual(second["slug"], "daily-standup-2")

    def test_the_database_enforces_it_too(self):
        from django.db import IntegrityError, transaction

        skill = self.make_ok()
        row = Skill.objects.get(pk=skill["id"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Skill.objects.create(team=row.team, owner=row.owner, slug=row.slug, title="Dup")

    def test_field_rules(self):
        self.as_user(self.user)
        for over in (
            {"tags": ["a", "b", "c", "d", "e", "f"]},
            {"tags": ["Bad Tag"]},
            {"tags": "reports"},
            {"description": "x" * 281},
            {"title": ""},
            {"title": "x" * 121},
            {"toolLevel": "delete"},
            {"effort": "extreme"},
            {"visibility": "everyone"},
            {"bodyMarkdown": "x" * 12_001},
        ):
            res = self.make(slug="rules", **over)
            self.assertEqual(res.status_code, 400, (over, res.content))
            self.assertIn("error", res.json())
        self.assertEqual(Skill.objects.count(), 0)


class VersionTests(SkillBase):
    def test_a_version_never_changes_or_goes(self):
        skill = self.make_ok(visibility="team")
        version = SkillVersion.objects.get(skill_id=skill["id"], version_no=1)
        version.title = "Edited"
        with self.assertRaises(ValueError):
            version.save()
        with self.assertRaises(ValueError):
            version.delete()
        version.refresh_from_db()
        self.assertEqual(version.title, "Weekly update")

    def test_a_version_keeps_the_text_it_was_cut_with(self):
        skill = self.make_ok(visibility="team")
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Other."}, format="json")
        self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        old = self.client.get(detail(skill["id"], "versions/1/")).json()["version"]
        new = self.client.get(detail(skill["id"], "versions/2/")).json()["version"]
        self.assertIn("Read the open tasks", old["bodyMarkdown"])
        self.assertIn("Other", new["bodyMarkdown"])
        self.assertNotEqual(old["contentHash"], new["contentHash"])

    def test_publishing_an_unchanged_text_is_a_409_not_a_new_version(self):
        skill = self.make_ok(visibility="team")
        res = self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)

    def test_a_private_skill_cannot_be_published(self):
        skill = self.make_ok()
        res = self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_a_reader_sees_no_version_cut_ahead_of_the_published_one(self):
        skill = self.make_ok(visibility="public")
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Next."}, format="json")
        # A newer version exists (cut directly, as the Hub's review step will) but is not published.
        row = Skill.objects.get(pk=skill["id"])
        SkillVersion.objects.create(
            skill=row,
            version_no=2,
            body=[],
            body_markdown="draft",
            content_hash="x",
            title="t",
            tool_level="read",
        )
        Skill.objects.filter(pk=row.pk).update(current_version_no=2)
        self.as_user(self.outsider)
        self.assertEqual(self.client.get(detail(skill["id"], "versions/2/")).status_code, 404)
        listed = self.client.get(detail(skill["id"], "versions/")).json()["versions"]
        self.assertEqual([v["versionNo"] for v in listed], [1])
        self.as_user(self.user)
        self.assertEqual(self.client.get(detail(skill["id"], "versions/2/")).status_code, 200)


class PlanLimitTests(SkillBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch("origin.search_engine.quota.get_effective_tier", return_value="free").start()

    def test_free_keeps_three_skills(self):
        for n in range(3):
            self.make_ok(slug=f"skill-{n}", title=f"Skill {n}")
        res = self.make(slug="skill-4", title="Skill 4")
        self.assertEqual(res.status_code, 429, res.content)
        body = res.json()
        self.assertEqual(body["code"], "plan_limit")
        self.assertEqual((body["limit"], body["used"]), (3, 3))
        self.assertEqual(Skill.objects.filter(owner=self.user).count(), 3)
        self.assertEqual(PersonalNoteMaster.objects.count(), 3)  # the refused one left no note

    def test_a_copy_counts_and_a_delete_frees_a_place(self):
        for n in range(3):
            self.make_ok(slug=f"skill-{n}", title=f"Skill {n}")
        first = Skill.objects.filter(owner=self.user).first()
        self.client.delete(detail(first.pk))
        self.assertEqual(self.make(slug="again", title="Again").status_code, 201)

    def test_the_list_reports_the_count(self):
        self.make_ok()
        body = self.client.get(f"{LIST}?team_id={self.team_id}").json()
        self.assertEqual((body["limit"], body["used"]), (3, 1))

    def test_the_numbers_by_tier(self):
        self.assertEqual(
            service.SKILLS_MAX_BY_TIER,
            {"free": 3, "core": 25, "pro": 100, "max": None, "enterprise": None},
        )
        mock.patch("origin.search_engine.quota.get_effective_tier", return_value="core").start()
        self.assertEqual(service.skills_limit(self.user.pk), 25)
        mock.patch("origin.search_engine.quota.get_effective_tier", return_value="max").start()
        self.assertIsNone(service.skills_limit(self.user.pk))

    def test_an_error_reading_the_plan_never_blocks_a_member(self):
        mock.patch(
            "origin.search_engine.quota.get_effective_tier", side_effect=RuntimeError("down")
        ).start()
        self.assertIsNone(service.skills_limit(self.user.pk))


class BodyNoteTests(SkillBase):
    """Check from the design (section 3): a note in a new folder scope must fall back to the owner
    and never to the team, in the search index and in the sidebar."""

    def test_the_body_note_is_in_a_hidden_skill_folder(self):
        skill = self.make_ok(visibility="team")
        note = PersonalNoteMaster.objects.get(note_id=skill["noteId"])
        folder = PersonalNoteFolder.objects.get(folder_id=note.folder_id)
        self.assertEqual(folder.scope, PersonalNoteFolder.SCOPE_SKILL)
        self.assertEqual(folder.owner_id, self.user.pk)
        self.assertEqual(note.title, "Weekly update")

    def test_the_folder_is_made_once_per_owner_and_team(self):
        self.make_ok()
        self.make_ok(slug="second", title="Second")
        self.assertEqual(
            PersonalNoteFolder.objects.filter(scope=PersonalNoteFolder.SCOPE_SKILL).count(), 1
        )

    def test_the_note_is_in_no_my_notes_list(self):
        self.make_ok()
        self.as_user(self.user)
        for path in ("all", "meta"):
            res = self.client.get(
                f"/api/v2/note/personal/{path}/?team_id={self.team_id}&user_id={self.user.id}"
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), [], path)

    def test_a_normal_note_is_still_listed(self):
        PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Plain", body=[])
        self.as_user(self.user)
        res = self.client.get(
            f"/api/v2/note/personal/meta/?team_id={self.team_id}&user_id={self.user.id}"
        )
        self.assertEqual([n["title"] for n in res.json()], ["Plain"])

    def test_the_search_acl_of_the_note_is_its_owner_and_never_the_team(self):
        # Even a TEAM-visible skill: teammates read it through the API (the published version),
        # not as a note, so the chunker must not hand them the live note.
        skill = self.make_ok(visibility="team")
        note = PersonalNoteMaster.objects.get(note_id=skill["noteId"])
        acl = personal_note_acl_user_ids(owner_id=self.user.pk, note_id=note.note_id)
        self.assertEqual(acl, {str(self.user.pk)})
        # Passing the folder the way the chunker does gives the same answer.
        acl = personal_note_acl_user_ids(
            owner_id=self.user.pk, note_id=note.note_id, folder_id=note.folder_id
        )
        self.assertEqual(acl, {str(self.user.pk)})

    def test_the_folder_entity_is_its_owners_alone_too(self):
        skill = self.make_ok(visibility="team")
        folder = PersonalNoteFolder.objects.get(
            folder_id=PersonalNoteMaster.objects.get(note_id=skill["noteId"]).folder_id
        )
        self.assertEqual(FolderAclResolver().readers_of(folder), {str(self.user.pk)})
        self.assertEqual(FolderAclResolver().readers_of_chain([folder]), {str(self.user.pk)})

    def test_a_teammate_cannot_open_the_live_note(self):
        from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, get_effective_role

        skill = self.make_ok(visibility="team")
        self.assertIsNone(
            get_effective_role(self.user2.pk, NOTE_TYPE_PERSONAL, skill["noteId"], self.team_id)
        )

    def test_the_editor_gets_the_new_body_pushed(self):
        skill = self.make_ok()
        with mock.patch("origin.services.skills.service.collab_push.push_note_body") as push:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.patch(
                    detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Pushed."}, format="json"
                )
        push.assert_called_once()
        self.assertEqual(push.call_args.args[:2], ("personal", skill["noteId"]))

    def test_a_collab_outage_does_not_fail_the_save(self):
        skill = self.make_ok()
        with mock.patch(
            "origin.services.skills.service.collab_push.push_note_body", side_effect=OSError("down")
        ):
            with self.captureOnCommitCallbacks(execute=True):
                res = self.client.patch(
                    detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Saved."}, format="json"
                )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Saved", res.json()["skill"]["bodyMarkdown"])


class SkillFileTests(SkillBase):
    def setUp(self):
        super().setUp()
        self.as_user(self.user)

    def test_export_then_import_round_trips(self):
        skill = self.make_ok(description='Say "hi": a: b')
        res = self.client.get(detail(skill["id"], "export/"))
        self.assertEqual(res.status_code, 200)
        out = res.json()
        self.assertEqual(out["filename"], "weekly-update/SKILL.md")
        self.assertTrue(out["content"].startswith("---\nname: weekly-update\n"))
        self.assertIn("Read the open tasks", out["content"])

        imported = self.client.post(
            LIST + "import/", {"team_id": self.team_id, "content": out["content"]}, format="json"
        )
        self.assertEqual(imported.status_code, 201, imported.content)
        copy = imported.json()["skill"]
        self.assertEqual(copy["slug"], "weekly-update-2")  # the name was taken: next free
        self.assertEqual(copy["description"], 'Say "hi": a: b')
        self.assertEqual(copy["title"], "Weekly update")
        self.assertEqual(copy["visibility"], "private")
        self.assertIn(
            "Read the open tasks",
            self.client.get(detail(copy["id"])).json()["skill"]["bodyMarkdown"],
        )

    def test_a_claude_skill_file_with_extra_keys_imports(self):
        content = (
            "---\nname: meeting-minutes\ndescription: Turn notes into tasks\n"
            "allowed-tools: Read, Grep\n---\n\n# Meeting notes\n\n1. Read them.\n"
        )
        res = self.client.post(
            LIST + "import/", {"team_id": self.team_id, "content": content}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        skill = res.json()["skill"]
        self.assertEqual((skill["slug"], skill["title"]), ("meeting-minutes", "Meeting notes"))
        self.assertEqual(skill["description"], "Turn notes into tasks")

    def test_a_file_without_front_matter_is_all_body(self):
        res = self.client.post(
            LIST + "import/",
            {"team_id": self.team_id, "content": "# Just text\n\nHello."},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["skill"]["title"], "Just text")

    def test_bad_files_are_refused(self):
        for content in ("", "   \n", 12, None):
            res = self.client.post(
                LIST + "import/", {"team_id": self.team_id, "content": content}, format="json"
            )
            self.assertEqual(res.status_code, 400, content)
        res = self.client.post(
            LIST + "import/", {"team_id": self.team_id, "content": "x" * 100_001}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Skill.objects.count(), 0)

    def test_a_reader_exports_the_published_text_not_the_draft(self):
        skill = self.make_ok(visibility="public")
        self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Draft only."}, format="json"
        )
        self.as_user(self.outsider)
        content = self.client.get(detail(skill["id"], "export/")).json()["content"]
        self.assertIn("Read the open tasks", content)
        self.assertNotIn("Draft only", content)
        self.as_user(self.user)
        self.assertIn(
            "Draft only", self.client.get(detail(skill["id"], "export/")).json()["content"]
        )


class ErasureTests(SkillBase):
    def test_erasing_an_account_removes_skills_notes_stars_and_installs(self):
        mine = self.make_ok(visibility="public")
        theirs = self.make_ok(user=self.user2, slug="theirs", visibility="public")
        self.as_user(self.user)
        self.client.post(detail(theirs["id"], "star/"))
        self.client.post(detail(theirs["id"], "install/"))
        self.as_user(self.user2)
        self.client.post(detail(mine["id"], "star/"))
        self.assertEqual(Skill.objects.get(pk=theirs["id"]).star_count, 1)

        with mock.patch("origin.services.note_delete.purge_note"):
            service.erase_for_user(self.user)

        self.assertFalse(Skill.objects.filter(owner=self.user).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=mine["noteId"]).exists())
        self.assertFalse(SkillStar.objects.filter(user=self.user).exists())
        self.assertFalse(SkillInstall.objects.filter(user=self.user).exists())
        # The other person's skill stays, with its counters corrected.
        row = Skill.objects.get(pk=theirs["id"])
        self.assertEqual((row.star_count, row.install_count), (0, 0))

    def test_the_account_deletion_flow_erases_them(self):
        from origin.services.account_deletion import delete_account

        mine = self.make_ok(user=self.user2, slug="leaver", visibility="private")
        other = self.make_ok(user=self.user, slug="stays", visibility="public")
        self.as_user(self.user2)
        self.client.post(detail(other["id"], "star/"))

        with (
            mock.patch("origin.services.account_deletion.purge_notes_from_index"),
            mock.patch("origin.services.note_delete.purge_note"),
            mock.patch("origin.search_engine.purge.purge_note_folders"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.user2)

        self.assertFalse(Skill.objects.filter(pk=mine["id"]).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=mine["noteId"]).exists())
        self.assertFalse(SkillStar.objects.filter(user=self.user2).exists())
        self.assertEqual(Skill.objects.get(pk=other["id"]).star_count, 0)


class ExportTests(SkillBase):
    def test_the_data_export_lists_the_members_skills(self):
        self.make_ok(visibility="team")
        from origin.services import account_export

        data = account_export.build_export(self.user)
        self.assertEqual(data["more_counts"]["skills"], 1)
        self.assertEqual(data["skills"][0]["name"], "weekly-update")
        self.assertEqual(data["skills"][0]["visibility"], "team")


class ReviewFindingsTests(SkillBase):
    """Each test pins a hole an adversarial review of the first version found."""

    def test_team_only_history_is_not_readable_once_the_skill_goes_public(self):
        skill = self.make_ok(visibility="team", bodyMarkdown="# T\n\n1. Ask Alice about ACME.")
        self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Ask the client."}, format="json"
        )  # v2, still team
        self.client.patch(detail(skill["id"]), {"visibility": "public"}, format="json")
        row = Skill.objects.get(pk=skill["id"])
        self.assertEqual(row.public_from_version_no, row.published_version_no)

        self.as_user(self.outsider)
        listed = self.client.get(detail(skill["id"], "versions/")).json()["versions"]
        self.assertEqual([v["versionNo"] for v in listed], [row.published_version_no])
        self.assertEqual(self.client.get(detail(skill["id"], "versions/1/")).status_code, 404)
        self.assertEqual(self.client.get(detail(skill["id"], "versions/2/")).status_code, 200)
        # The owner still sees the whole history.
        self.as_user(self.user)
        self.assertEqual(
            len(self.client.get(detail(skill["id"], "versions/")).json()["versions"]), 2
        )

    def test_an_owner_who_left_the_team_cannot_change_it_but_can_delete_it(self):
        skill = self.make_ok(user=self.user2, slug="leaver", visibility="team")
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.as_user(self.user2)
        res = self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Hi."}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(
            self.client.post(detail(skill["id"], "publish/"), {}, format="json").status_code, 403
        )
        self.assertEqual(SkillVersion.objects.filter(skill_id=skill["id"]).count(), 1)
        self.assertEqual(self.client.delete(detail(skill["id"])).status_code, 204)

    def test_a_long_changelog_or_a_note_grown_past_the_limit_is_a_400_not_a_500(self):
        skill = self.make_ok(visibility="team")
        res = self.client.post(
            detail(skill["id"], "publish/"), {"changelog": "x" * 300}, format="json"
        )
        self.assertEqual(res.status_code, 409)  # nothing changed: a sentence, not a 500
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. New."}, format="json")
        res = self.client.post(
            detail(skill["id"], "publish/"), {"changelog": "y" * 300}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            len(SkillVersion.objects.filter(skill_id=skill["id"]).first().changelog), 280
        )

        # The editor lets the note grow past what the API accepts.
        blocks = [{"type": "paragraph", "content": [{"type": "text", "text": "x" * 13_000}]}]
        PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).update(body=blocks)
        res = self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("12,000", res.json()["error"])

    def test_the_public_switch_off_hides_skills_that_are_already_public(self):
        skill = self.make_ok(visibility="public")
        self.as_user(self.outsider)
        self.client.post(detail(skill["id"], "install/"))
        with override_settings(GENOS_SKILLS_PUBLIC=False):
            for call in (
                lambda: self.client.get(detail(skill["id"])),
                lambda: self.client.get(detail(skill["id"], "versions/")),
                lambda: self.client.get(detail(skill["id"], "export/")),
                lambda: self.client.post(detail(skill["id"], "install/")),
                lambda: self.client.post(detail(skill["id"], "star/")),
            ):
                self.assertEqual(call().status_code, 404)
            self.assertEqual(self.client.get(f"{LIST}?tab=installed").json()["skills"], [])
            self.as_user(self.user)  # the owner still has it
            self.assertEqual(self.client.get(detail(skill["id"])).status_code, 200)
        self.as_user(self.outsider)
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 200)

    def test_readers_see_the_published_name_icon_and_dates_not_the_owners_edits(self):
        skill = self.make_ok(visibility="public", icon="A")
        self.as_user(self.user)
        self.client.patch(
            detail(skill["id"]),
            {"slug": "renamed", "icon": "B", "effort": "high", "language": "ja"},
            format="json",
        )
        self.as_user(self.outsider)
        seen = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(
            (seen["slug"], seen["icon"], seen["effort"], seen["language"]),
            ("weekly-update", "A", "", ""),
        )
        version = SkillVersion.objects.get(skill_id=skill["id"], version_no=1)
        self.assertEqual(seen["updatedAt"], version.created_at.isoformat())
        self.assertEqual(
            self.client.get(detail(skill["id"], "export/")).json()["filename"],
            "weekly-update/SKILL.md",
        )
        # Once published, readers get the new name.
        self.as_user(self.user)
        self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.as_user(self.outsider)
        self.assertEqual(self.client.get(detail(skill["id"])).json()["skill"]["slug"], "renamed")

    def test_saving_the_body_moves_the_notes_timestamp_for_the_indexer(self):
        skill = self.make_ok()
        before = PersonalNoteMaster.objects.get(note_id=skill["noteId"]).ts_updated_at
        self.client.patch(detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. New."}, format="json")
        self.assertGreater(
            PersonalNoteMaster.objects.get(note_id=skill["noteId"]).ts_updated_at, before
        )

    def test_copying_ones_own_skill_does_not_count_as_a_copy(self):
        skill = self.make_ok()
        self.client.post(detail(skill["id"], "copy/"), {"team_id": self.team_id}, format="json")
        self.assertEqual(Skill.objects.get(pk=skill["id"]).copy_count, 0)

    def test_a_rename_race_is_a_409_not_a_500(self):
        from django.db import IntegrityError

        skill = self.make_ok()
        with mock.patch.object(Skill, "save", side_effect=IntegrityError("duplicate")):
            res = self.client.patch(detail(skill["id"]), {"description": "x"}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_a_body_that_is_not_an_object_is_a_400(self):
        self.as_user(self.user)
        for path in (LIST, LIST + "import/"):
            res = self.client.post(path, [], format="json")
            self.assertEqual(res.status_code, 400, path)
        skill = self.make_ok()
        for suffix in ("publish/", "copy/"):
            res = self.client.post(detail(skill["id"], suffix), [], format="json")
            self.assertEqual(res.status_code, 400, suffix)
