"""The two company guide notes (MIP-1689, under MIP-1447).

The notes tell a company how to organise one Genos team, and they name other guide notes and the
app's own menus. What matters: they are in both editions and arrive with the rest of the guide;
every guide note they send the reader to really exists (a wrong title is a dead end nobody
reviews); the Playbooks note and "Start here" point to them; and existing members can be given
them without touching anything else in their folder.
"""

from io import StringIO

from django.core.management import call_command

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services import start_here_note
from origin.services.guide_company_notes import COMPANY_NOTES, ORGANISING_TITLE, SETUP_TITLE
from origin.services.guide_notes import (
    GUIDE_FOLDER_NAME,
    GUIDE_NOTES,
    JAPANESE_GUIDE_FOLDER_NAME,
    JAPANESE_GUIDE_NOTES,
    add_company_guide_notes,
    seed_guide_notes,
)
from origin.tests.test_base import BaseAPITestCase


def _text(body) -> str:
    """All the visible text of a BlockNote body."""
    out = []
    for block in body:
        out.append("".join(item.get("text", "") for item in block.get("content", []) or []))
    return "\n".join(out)


# The other guide notes each company note sends the reader to, by title, per edition.
REFERENCES = {
    ORGANISING_TITLE["en"]: [
        "Working with another company: cross-team sharing",
    ],
    SETUP_TITLE["en"]: [
        ORGANISING_TITLE["en"].split(":")[0],  # quoted by its first words
        "Teams, members, and roles",
        "Connecting Genos to other tools: API, MCP, and realtime",
    ],
    ORGANISING_TITLE["ja"]: [
        "他の会社との連携：チーム間共有",
    ],
    SETUP_TITLE["ja"]: [
        ORGANISING_TITLE["ja"].split(":")[0],
        "チーム、メンバー、役割",
        "Genosと他のツールを接続する：API、MCP、リアルタイム",
    ],
}


class CompanyNotesContentTests(BaseAPITestCase):
    def test_both_editions_have_both_notes_as_their_last_two(self):
        for locale, notes in (("en", GUIDE_NOTES), ("ja", JAPANESE_GUIDE_NOTES)):
            self.assertEqual(
                [title for title, _ in notes[-2:]],
                [ORGANISING_TITLE[locale], SETUP_TITLE[locale]],
            )
            self.assertEqual(notes[-2:], COMPANY_NOTES[locale])

    def test_every_guide_note_a_company_note_names_really_exists(self):
        for locale, notes in (("en", GUIDE_NOTES), ("ja", JAPANESE_GUIDE_NOTES)):
            titles = [title for title, _ in notes]
            for company_title, body in COMPANY_NOTES[locale]:
                text = _text(body)
                for referenced in REFERENCES[company_title]:
                    self.assertIn(referenced, text, f"{company_title!r} lost a reference")
                    self.assertTrue(
                        any(referenced in title for title in titles),
                        f"{company_title!r} names {referenced!r}, which is not a guide note",
                    )

    def test_the_playbooks_note_points_to_both_in_each_edition(self):
        for locale, notes, marker in (
            ("en", GUIDE_NOTES, "Playbooks"),
            ("ja", JAPANESE_GUIDE_NOTES, "プレイブック"),
        ):
            playbooks = next(body for title, body in notes if title.startswith(marker))
            text = _text(playbooks)
            self.assertIn(ORGANISING_TITLE[locale], text)
            self.assertIn(SETUP_TITLE[locale], text)

    def test_the_notes_use_the_apps_own_words_for_the_four_things(self):
        """A department is a project TAG (the app has no 'label'), a mention group in Settings,
        a team folder under Team Notes, and an open group chat."""
        text = _text(COMPANY_NOTES["en"][0][1])
        for word in (
            "project tag",
            "Edit tags",
            "Settings → Mention groups",
            "Team Notes",
            "Public",
        ):
            self.assertIn(word, text)
        self.assertNotIn("project label", text.lower())

    def test_they_do_not_promise_what_is_not_built(self):
        """Hiding a private project's name, a chat directory: not built. The notes say
        so under 'What is not there yet' instead of teaching them."""
        english = _text(COMPANY_NOTES["en"][0][1]) + _text(COMPANY_NOTES["en"][1][1])
        for promise in ("hidden mode", "Hidden mode", "chat directory"):
            self.assertNotIn(promise, english)
        self.assertIn("What is not there yet", english)
        self.assertIn("the NAME of a private project still shows", english)

    def test_group_chats_are_not_said_to_be_undeletable(self):
        """MIP-1728: the owner can delete a group chat (MIP-1705), so the notes must not say it cannot."""
        english = _text(COMPANY_NOTES["en"][0][1]) + _text(COMPANY_NOTES["en"][1][1])
        japanese = _text(COMPANY_NOTES["ja"][0][1]) + _text(COMPANY_NOTES["ja"][1][1])
        self.assertNotIn("but not deleted", english)
        self.assertNotIn("削除はできず", japanese)
        self.assertIn("Remove sample data", english)
        self.assertIn("サンプルデータを削除", japanese)

    def test_projects_are_not_said_to_be_unarchivable(self):
        """MIP-1880: archiving a project and a group chat shipped (MIP-1693)."""
        english = _text(COMPANY_NOTES["en"][0][1]) + _text(COMPANY_NOTES["en"][1][1])
        japanese = _text(COMPANY_NOTES["ja"][0][1]) + _text(COMPANY_NOTES["ja"][1][1])
        self.assertNotIn("cannot be archived", english)
        self.assertNotIn("アーカイブできず", japanese)
        self.assertIn("archived instead of deleted", english)
        self.assertIn("削除せずにアーカイブできます", japanese)

    def test_japanese_has_no_broken_characters(self):
        for _, body in COMPANY_NOTES["ja"]:
            self.assertNotIn("�", _text(body))


class CompanyNotesStartHereTests(BaseAPITestCase):
    def test_start_here_points_to_the_organising_note_in_every_language(self):
        for locale in start_here_note._COPY:
            _, body = start_here_note.build(locale)
            text = _text(body)
            guide_title = ORGANISING_TITLE["ja" if locale == "ja" else "en"]
            self.assertIn(guide_title, text, locale)


class CompanyNotesSeedingTests(BaseAPITestCase):
    def _folder_titles(self, user, team, folder_name):
        folder = PersonalNoteFolder.objects.get(team=team, owner=user, name=folder_name)
        return set(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id).values_list(
                "title", flat=True
            )
        )

    def test_a_new_member_gets_both_company_notes_in_both_folders(self):
        team = TeamMaster.objects.create(
            team_name="Seeded", team_email="seeded@example.com", owner=self.user
        )
        TeamMembers.objects.get_or_create(team=team, attendee=self.user)
        seed_guide_notes(self.user, team)

        self.assertLessEqual(
            {ORGANISING_TITLE["en"], SETUP_TITLE["en"]},
            self._folder_titles(self.user, team, GUIDE_FOLDER_NAME),
        )
        self.assertLessEqual(
            {ORGANISING_TITLE["ja"], SETUP_TITLE["ja"]},
            self._folder_titles(self.user, team, JAPANESE_GUIDE_FOLDER_NAME),
        )


class AddCompanyGuideNotesTests(BaseAPITestCase):
    """Members who already have the guide folder (seeded before these notes existed)."""

    def setUp(self):
        super().setUp()
        self.team = TeamMaster.objects.create(
            team_name="Old", team_email="old@example.com", owner=self.user
        )
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.user)
        seed_guide_notes(self.user, self.team)
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other@example.com", owner=self.user
        )
        TeamMembers.objects.get_or_create(team=self.other_team, attendee=self.user)
        seed_guide_notes(self.user, self.other_team)
        self.titles = {
            ORGANISING_TITLE["en"],
            SETUP_TITLE["en"],
            ORGANISING_TITLE["ja"],
            SETUP_TITLE["ja"],
        }
        # Make everybody look like they were seeded before the two notes existed.
        PersonalNoteMaster.objects.filter(title__in=self.titles).delete()

    def _count(self, team=None):
        notes = PersonalNoteMaster.objects.filter(title__in=self.titles)
        return notes.filter(team=team).count() if team else notes.count()

    def test_a_dry_run_counts_and_writes_nothing(self):
        report = add_company_guide_notes()

        self.assertEqual(len(report.added), 8)  # 2 folders x 2 notes x 2 teams
        self.assertEqual(report.already_there, 0)
        self.assertEqual(self._count(), 0)

    def test_apply_adds_both_notes_to_each_existing_folder(self):
        report = add_company_guide_notes(apply=True)

        self.assertEqual(len(report.added), 8)
        self.assertEqual(self._count(), 8)
        for team in (self.team, self.other_team):
            for folder_name, locale in (
                (GUIDE_FOLDER_NAME, "en"),
                (JAPANESE_GUIDE_FOLDER_NAME, "ja"),
            ):
                self.assertLessEqual(
                    {ORGANISING_TITLE[locale], SETUP_TITLE[locale]},
                    self._titles_in(team, folder_name),
                )

    def _titles_in(self, team, folder_name):
        folder = PersonalNoteFolder.objects.get(team=team, owner=self.user, name=folder_name)
        return set(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id).values_list(
                "title", flat=True
            )
        )

    def test_each_added_note_is_the_members_own_with_a_role_row(self):
        add_company_guide_notes(apply=True)

        for note in PersonalNoteMaster.objects.filter(title__in=self.titles):
            self.assertEqual(note.owner_id, self.user.id)
            self.assertTrue(
                NotePermissionMaster.objects.filter(
                    team_id=note.team_id,
                    user_id=self.user.id,
                    note_id=note.note_id,
                    note_type=1,
                    role_id=1,
                ).exists()
            )

    def test_a_second_run_adds_nothing(self):
        add_company_guide_notes(apply=True)
        report = add_company_guide_notes(apply=True)

        self.assertEqual(report.added, [])
        self.assertEqual(report.already_there, 8)
        self.assertEqual(self._count(), 8)

    def test_nothing_else_in_the_folder_is_touched(self):
        folder = PersonalNoteFolder.objects.get(
            team=self.team, owner=self.user, name=GUIDE_FOLDER_NAME
        )
        edited = PersonalNoteMaster.objects.filter(folder_id=folder.folder_id).first()
        edited.title = "My own title"
        edited.save(update_fields=["title"])
        before = list(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id)
            .order_by("note_id")
            .values_list("note_id", "title", "ts_updated_at")
        )

        add_company_guide_notes(apply=True)

        after = list(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id)
            .exclude(title__in=self.titles)
            .order_by("note_id")
            .values_list("note_id", "title", "ts_updated_at")
        )
        self.assertEqual(after, before)

    def test_a_copy_the_member_already_has_is_left_alone(self):
        folder = PersonalNoteFolder.objects.get(
            team=self.team, owner=self.user, name=GUIDE_FOLDER_NAME
        )
        mine = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title=ORGANISING_TITLE["en"],
            body=[{"type": "paragraph", "content": [{"type": "text", "text": "my edit"}]}],
            folder_id=folder.folder_id,
        )

        add_company_guide_notes(apply=True)

        mine.refresh_from_db()
        self.assertEqual(_text(mine.body), "my edit")
        self.assertEqual(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=mine.title).count(),
            1,
        )

    def test_it_can_be_limited_to_one_team(self):
        report = add_company_guide_notes(apply=True, team_id=self.team.team_id)

        self.assertEqual(len(report.added), 4)
        self.assertEqual(self._count(self.team), 4)
        self.assertEqual(self._count(self.other_team), 0)

    def test_a_folder_with_another_name_is_not_a_guide_folder(self):
        PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="Not the guide")
        before = PersonalNoteMaster.objects.count()

        add_company_guide_notes(apply=True, team_id=self.team.team_id)

        self.assertEqual(PersonalNoteMaster.objects.count(), before + 4)

    def test_the_command_is_a_dry_run_unless_asked(self):
        out = StringIO()
        call_command("add_company_guide_notes", stdout=out)
        self.assertIn("would be added", out.getvalue())
        self.assertEqual(self._count(), 0)

        out = StringIO()
        call_command("add_company_guide_notes", "--apply", stdout=out)
        self.assertIn("added", out.getvalue())
        self.assertEqual(self._count(), 8)


class CompanyNotesDescribeTheSetupAsItIs(BaseAPITestCase):
    """The checklist must match what team creation does (MIP-1691): a `general` chat exists, and
    a "Departments" box makes the four things of each department."""

    def test_the_setup_note_says_general_exists_and_points_to_the_departments_box(self):
        for locale, box in (("en", "“Departments” box"), ("ja", "「部署」欄")):
            text = _text(COMPANY_NOTES[locale][1][1])
            self.assertIn(box, text)
            self.assertNotIn("does not make one for you", text)
            self.assertNotIn("Genosは自動では作りません", text)
        self.assertIn("Every team starts with an open chat", _text(COMPANY_NOTES["en"][1][1]))
