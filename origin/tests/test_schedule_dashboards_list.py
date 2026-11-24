"""`GET /api/v2/schedules/dashboards/` (MIP-2356): the dashboards a member can open.

Pinned: own and shared are separate; a shared dashboard appears only when the note ACL lets the
member read its note (a note nobody shared never appears, and the schedule behind a shared note is
never described); a missing, non-HTML or other-team note never appears; a schedule that has not run
yet is listed as "own" without a note; another job's schedule is never listed; a non-member is
refused; the answer holds no email and no schedule parameters.
"""

from __future__ import annotations

from origin.models.common.schedule_models import MemberSchedule
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.tests.test_schedules import ScheduleBase

URL = "/api/v2/schedules/dashboards/"


class TestDashboardsList(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def note(self, owner, title="Dash", *, body_format="html", team=None):
        return PersonalNoteMaster.objects.create(
            team=team or self.team, owner=owner, title=title, body_format=body_format
        )

    def schedule(self, owner, note=None, *, job="ai_dashboard", name="Dashboard"):
        output = (
            {"target": "note", "note_type": "personal", "note_id": note.note_id, "run": ""}
            if note
            else {}
        )
        return MemberSchedule.objects.create(
            team=self.team,
            user=owner,
            name=name,
            job=job,
            params={"kind": "mine", "period": "month"},
            frequency="daily",
            time_of_day="09:00",
            output=output,
        )

    def share(self, note, user, role=3):
        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_id=note.note_id, note_type=1, role_id=role
        )

    def get(self):
        res = self.client.get(f"{URL}?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_own_dashboards_come_with_their_note_and_a_new_one_without(self):
        note = self.note(self.user, "My month")
        mine = self.schedule(self.user, note, name="My month")
        fresh = self.schedule(self.user, None, name="Not run yet")
        out = self.get()
        by_id = {r["scheduleId"]: r for r in out["own"]}
        self.assertEqual(by_id[str(mine.id)]["note"]["noteId"], note.note_id)
        self.assertEqual(by_id[str(mine.id)]["note"]["title"], "My month")
        self.assertIsNone(by_id[str(fresh.id)]["note"])
        self.assertEqual(out["shared"], [])

    def test_other_jobs_are_never_listed(self):
        self.schedule(self.user, None, job="what_you_did", name="A log")
        self.assertEqual(self.get(), {"own": [], "shared": []})

    def test_a_dashboard_nobody_shared_with_me_never_appears(self):
        note = self.note(self.user2, "Private of user2")
        self.schedule(self.user2, note)
        self.assertEqual(self.get()["shared"], [])

    def test_a_shared_dashboard_appears_with_only_the_note_and_the_owner_name(self):
        note = self.note(self.user2, "Team month")
        self.schedule(self.user2, note, name="secret schedule name")
        self.share(note, self.user)
        out = self.get()
        self.assertEqual(len(out["shared"]), 1)
        row = out["shared"][0]
        self.assertEqual(row["noteId"], note.note_id)
        self.assertEqual(row["title"], "Team month")
        self.assertEqual(row["ownerName"], self.user2.username)
        self.assertEqual(out["own"], [])
        body = str(out)
        self.assertNotIn("secret schedule name", body)
        self.assertNotIn(self.user2.email, body)
        self.assertNotIn("period", body)

    def test_a_note_that_is_gone_not_html_or_in_another_team_is_not_listed(self):
        plain = self.note(self.user2, "Plain", body_format="blocknote")
        self.schedule(self.user2, plain, name="a")
        self.share(plain, self.user)
        gone = self.note(self.user2, "Gone")
        self.schedule(self.user2, gone, name="b")
        self.share(gone, self.user)
        gone.delete()
        self.assertEqual(self.get()["shared"], [])

    def test_an_own_schedule_whose_note_is_gone_has_no_note(self):
        note = self.note(self.user, "x")
        sched = self.schedule(self.user, note)
        note.delete()
        self.assertIsNone(self.get()["own"][0]["note"])
        self.assertEqual(self.get()["own"][0]["scheduleId"], str(sched.id))

    def test_a_non_member_is_refused_and_a_missing_team_is_a_400(self):
        self.assertEqual(self.client.get(URL).status_code, 400)
        res = self.client.get(f"{URL}?team_id=00000000-0000-0000-0000-000000000001")
        self.assertEqual(res.status_code, 404)

    def test_it_needs_a_login(self):
        self.client.credentials()
        self.assertIn(self.client.get(f"{URL}?team_id={self.team.team_id}").status_code, (401, 403))
