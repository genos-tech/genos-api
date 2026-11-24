"""Skill bodies: the search ACL, the export rule and a private skill's co-editors (MIP-2190, part 2).

Part 1 made the batch read checks follow `skill_note_role`. This part does the same for the search
index ACL (`personal_note_acl_user_ids`), decides who may export (`export_file`: the owner gets the
live draft, everybody else the PUBLISHED version, a co-editor of a private skill a refusal), and makes
the co-editors of a PRIVATE skill count only while its owner is still on the team.
"""

from __future__ import annotations

from origin.models.common.team_models import TeamMembers
from origin.models.skills.skill_models import STATUS_TAKEN_DOWN, Skill
from origin.search_engine.agent.acl import personal_note_acl_user_ids
from origin.tests.test_skills_api import detail
from origin.tests.test_skills_team_sharing import SharingBase
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    get_effective_role,
    personal_note_reader_pairs,
)


class SkillSearchAclTests(SharingBase):
    def acl(self, skill):
        return personal_note_acl_user_ids(owner_id=self.user2.pk, note_id=skill["noteId"])

    def test_the_owner_and_live_co_editors_are_the_readers(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        self.assertEqual(self.acl(skill), {str(self.user2.pk), str(self.editor.pk)})

    def test_a_taken_down_skills_co_editors_are_not_readers(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        Skill.objects.filter(pk=skill["id"]).update(status=STATUS_TAKEN_DOWN)
        self.assertEqual(self.acl(skill), {str(self.user2.pk)})

    def test_an_owner_who_left_the_team_with_a_private_skill_is_nobody(self):
        skill = self.skill(visibility="private", slug="mine")
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertEqual(self.acl(skill), set())

    def test_a_stray_editor_row_of_someone_off_the_team_is_not_a_reader(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        TeamMembers.objects.filter(team=self.team, attendee=self.editor).update(is_deleted=True)
        self.assertEqual(self.acl(skill), {str(self.user2.pk)})

    def test_the_index_agrees_with_the_single_note_rule(self):
        skill = self.skill(visibility="private", slug="mine")
        self.invite(skill, self.editor)
        for change in (
            lambda: None,
            lambda: TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(
                is_deleted=True
            ),
        ):
            change()
            readers = {
                str(p.pk)
                for p in (self.user, self.user2, self.editor, self.reader, self.guest)
                if get_effective_role(p.pk, NOTE_TYPE_PERSONAL, skill["noteId"]) is not None
            }
            self.assertEqual(self.acl(skill), readers)

    def test_an_ordinary_note_keeps_its_owner_and_grants(self):
        from origin.models.note.personal_note_models import PersonalNoteMaster

        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="plain", body=[]
        )
        self.assertEqual(
            personal_note_acl_user_ids(owner_id=self.user2.pk, note_id=plain.note_id),
            {str(self.user2.pk)},
        )


class PrivateSkillOwnerLeftTests(SharingBase):
    def test_co_editors_of_a_private_skill_lose_access_with_the_owner(self):
        skill = self.skill(visibility="private", slug="mine")
        self.invite(skill, self.editor)
        note = skill["noteId"]
        self.assertIsNotNone(get_effective_role(self.editor.pk, NOTE_TYPE_PERSONAL, note))
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertIsNone(get_effective_role(self.editor.pk, NOTE_TYPE_PERSONAL, note))
        self.assertEqual(personal_note_reader_pairs([self.editor.pk], [note]), set())
        res = self.call(self.editor, "get", detail(skill["id"]))
        self.assertEqual(res.status_code, 404, res.content)

    def test_co_editors_of_a_team_skill_keep_access_when_the_owner_left(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        note = skill["noteId"]
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertIsNotNone(get_effective_role(self.editor.pk, NOTE_TYPE_PERSONAL, note))
        self.assertEqual(
            personal_note_reader_pairs([self.editor.pk], [note]), {(str(self.editor.pk), note)}
        )


class ExportRuleTests(SharingBase):
    def publish(self, skill, text=None):
        if text:
            res = self.call(self.user2, "patch", detail(skill["id"]), {"bodyMarkdown": text})
            self.assertEqual(res.status_code, 200, res.content)
        res = self.call(self.user2, "post", detail(skill["id"], "publish/"))
        self.assertEqual(res.status_code, 201, res.content)

    def export(self, user, skill):
        return self.call(user, "get", detail(skill["id"], "export/"))

    def test_the_owner_exports_the_live_draft(self):
        skill = self.skill()
        self.publish(skill, "# T\n\n1. Published step.")
        self.call(self.user2, "patch", detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Draft."})
        content = self.export(self.user2, skill).json()["content"]
        self.assertIn("Draft.", content)

    def test_a_co_editor_exports_the_published_version_not_the_draft(self):
        skill = self.skill()
        self.publish(skill, "# T\n\n1. Published step.")
        self.invite(skill, self.editor)
        self.call(self.editor, "patch", detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Draft."})
        res = self.export(self.editor, skill)
        self.assertEqual(res.status_code, 200, res.content)
        content = res.json()["content"]
        self.assertIn("Published step.", content)
        self.assertNotIn("Draft.", content)

    def test_a_co_editor_of_a_private_skill_is_refused_with_a_suggestion(self):
        skill = self.skill(visibility="private", slug="mine")
        self.invite(skill, self.editor)
        res = self.export(self.editor, skill)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertIn("Only the owner can export a private skill", res.content.decode())
        self.assertNotIn("Read the open tasks", res.content.decode())

    def test_the_owner_still_exports_a_private_skill(self):
        skill = self.skill(visibility="private", slug="mine")
        res = self.export(self.user2, skill)
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_co_editor_of_an_unpublished_team_skill_is_refused_not_given_an_empty_file(self):
        skill = self.skill()
        Skill.objects.filter(pk=skill["id"]).update(published_version_no=0)
        self.invite(skill, self.editor)
        res = self.export(self.editor, skill)
        self.assertEqual(res.status_code, 403, res.content)
        self.assertIn("Nothing is published yet", res.content.decode())
