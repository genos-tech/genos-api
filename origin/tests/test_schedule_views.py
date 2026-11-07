"""Settings > Schedules over REST (MIP-1150).

What matters: it is the member's own and nobody else's (another member's schedule, another
team's, and an unknown id are the same 404); an API key does not manage schedules; the same
rules as the MCP tools (the cap, the checked parameters, the cooldown on "run now") because it
is the same service; the answer is the camelCase shape the page reads; and every refusal is a
sentence in `error`, never a stack trace.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.utils import timezone

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedules
from origin.services.schedule_jobs import JOBS, Job
from origin.tests.test_base import BaseAPITestCase

LIST = "/api/v2/schedules/"


def fake_job(ctx, params):
    return {
        "title": f"Your day ({params.get('day', 'today')})",
        "summary": "Two things.",
        "sections": [{"heading": "Due", "items": [{"text": "one"}, {"text": "two"}]}],
    }


class ScheduleViewBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # These tests are about the page, not about the defaults (MIP-1178): the member was
        # seeded long ago, so a first list creates nothing.
        MemberScheduleSeed.objects.create(user=self.user, team=self.team)
        cache.clear()
        self.addCleanup(cache.clear)
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        patcher = mock.patch.dict(
            JOBS,
            {
                "fake_day": Job(
                    "fake_day",
                    "Fake day",
                    "A job for the tests.",
                    fake_job,
                    {"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
                )
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.authenticate()
        self.team_id = str(self.team.team_id)

    @property
    def q(self) -> str:
        return f"?team_id={self.team_id}"

    def body(self, **over):
        data = {
            "team_id": self.team_id,
            "name": "Morning",
            "job": "fake_day",
            "frequency": "weekdays",
            "timeOfDay": "09:00",
        }
        data.update(over)
        return data

    def create(self, **over):
        res = self.client.post(LIST, self.body(**over), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["schedule"]

    def ctx2(self):
        return ToolContext(team_id=self.team_id, user_id=str(self.user2.id))

    def theirs(self):
        return schedules.create(
            self.ctx2(),
            name="Theirs",
            job="fake_day",
            params={},
            frequency="daily",
            time_of_day="10:00",
        )


class TestListAndCreate(ScheduleViewBase):
    def test_the_list_carries_the_catalog_the_zone_and_the_limit(self):
        res = self.client.get(LIST + self.q)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["timeZone"], "Asia/Tokyo")
        self.assertEqual(res.data["limit"], schedules.MAX_SCHEDULES_PER_MEMBER)
        self.assertEqual(res.data["schedules"], [])
        jobs = {j["job"]: j for j in res.data["jobs"]}
        self.assertEqual(jobs["fake_day"]["title"], "Fake day")
        self.assertEqual(jobs["fake_day"]["params"]["day"]["enum"], ["today", "yesterday"])

    def test_two_suggested_schedules_are_offered_and_accepting_one_makes_it_disappear(self):
        # The test registry has no real jobs, so register the two a suggestion names.
        with mock.patch.dict(
            JOBS,
            {
                "what_to_do_today": Job("what_to_do_today", "Today", "d", fake_job, {}),
                "what_you_did": Job(
                    "what_you_did",
                    "Did",
                    "d",
                    fake_job,
                    {"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
                ),
            },
        ):
            first = self.client.get(LIST + self.q).data["suggestions"]
            self.assertEqual([x["key"] for x in first], ["morning_plan", "evening_recap"])
            self.assertEqual(
                first[0],
                {
                    "key": "morning_plan",
                    "name": "Morning plan",
                    "job": "what_to_do_today",
                    "params": {},
                    "frequency": "daily",
                    "timeOfDay": "09:00",
                },
            )
            # Accepting is an ordinary create from the suggestion's own fields.
            accept = {k: v for k, v in first[0].items() if k != "key"}
            res = self.client.post(LIST, {**accept, "team_id": self.team_id}, format="json")
            self.assertEqual(res.status_code, 201, res.data)
            after = self.client.get(LIST + self.q).data["suggestions"]
            self.assertEqual([x["key"] for x in after], ["evening_recap"])
            # And it is an ordinary schedule: it can be deleted, after which it is offered again.
            self.client.delete(f"{LIST}{res.data['schedule']['id']}/{self.q}")
            again = self.client.get(LIST + self.q).data["suggestions"]
            self.assertEqual(len(again), 2)

    def test_a_schedule_is_created_and_read_back_in_the_pages_shape(self):
        row = self.create(name="What to do", frequency="weekly", weekdays=[0, 2])
        self.assertEqual(
            {k: row[k] for k in ("name", "job", "jobTitle", "when", "frequency", "timeOfDay")},
            {
                "name": "What to do",
                "job": "fake_day",
                "jobTitle": "Fake day",
                "when": "every Monday and Wednesday at 09:00",
                "frequency": "weekly",
                "timeOfDay": "09:00",
            },
        )
        self.assertEqual(row["weekdays"], [0, 2])
        self.assertTrue(row["enabled"])
        self.assertFalse(row["deliverPush"])
        self.assertTrue(row["nextRun"].endswith("+09:00"), row["nextRun"])
        self.assertIsNone(row["lastRun"])
        stored = MemberSchedule.objects.get(pk=row["id"])
        self.assertEqual((stored.user_id, str(stored.team_id)), (self.user.id, self.team_id))
        listed = self.client.get(LIST + self.q).data["schedules"]
        self.assertEqual([s["id"] for s in listed], [row["id"]])
        self.assertFalse([k for k in row if "_" in k], "every key is camelCase")

    def test_monthly_and_once_carry_their_own_fields(self):
        monthly = self.create(frequency="monthly", monthDay=15)
        self.assertEqual(monthly["monthDay"], 15)
        once = self.create(
            frequency="once", runDate=(timezone.now() + timedelta(days=2)).date().isoformat()
        )
        self.assertIn("runDate", once)

    def test_push_is_off_unless_asked_for(self):
        self.assertTrue(self.create(deliverPush=True)["deliverPush"])
        self.assertFalse(self.create()["deliverPush"])

    def test_what_the_service_refuses_is_one_sentence_in_error(self):
        for over, needle in (
            ({"job": "nope"}, "no job called"),
            ({"timeOfDay": "9am"}, "09:30"),
            ({"frequency": "weekly"}, "weekday"),
            ({"params": {"day": "tomorrow"}}, "day"),
            ({"params": {"hours": 3}}, "no parameter"),
            ({"frequency": "sometimes"}, "frequency"),
        ):
            res = self.client.post(LIST, self.body(**over), format="json")
            self.assertEqual(res.status_code, 400, (over, res.data))
            self.assertIn(needle, res.data["error"], over)
            self.assertEqual(set(res.data), {"error"})
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_there_is_a_limit_per_member(self):
        for n in range(schedules.MAX_SCHEDULES_PER_MEMBER):
            self.create(name=f"s{n}")
        res = self.client.post(LIST, self.body(name="one more"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("at most", res.data["error"])

    def test_a_team_is_required_and_must_be_a_uuid(self):
        for res in (
            self.client.get(LIST),
            self.client.get(LIST + "?team_id=nonsense"),
            self.client.post(LIST, {**self.body(), "team_id": None}, format="json"),
        ):
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.data, {"error": "`team_id` is required."})

    def test_a_team_the_member_is_not_in_cannot_hold_their_schedule(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        res = self.client.post(LIST, self.body(team_id=str(other.team_id)), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_request_without_credentials_is_refused(self):
        self.unauthenticate()
        self.assertIn(self.client.get(LIST + self.q).status_code, (401, 403))

    def test_an_api_key_does_not_manage_schedules(self):
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user, name="k", key_hash=hash_key(raw), prefix=raw[:11], scope="write"
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertIn(self.client.get(LIST + self.q).status_code, (401, 403))
        self.assertEqual(self.client.post(LIST, self.body(), format="json").status_code, 401)
        self.assertEqual(MemberSchedule.objects.count(), 0)


class TestOwnerOnly(ScheduleViewBase):
    def test_my_list_holds_only_mine(self):
        self.create(name="Mine")
        self.theirs()
        names = [s["name"] for s in self.client.get(LIST + self.q).data["schedules"]]
        self.assertEqual(names, ["Mine"])

    def test_someone_elses_schedule_is_not_found_for_every_call(self):
        other = str(self.theirs().id)
        url = f"{LIST}{other}/"
        calls = [
            self.client.patch(url + self.q, {"name": "stolen"}, format="json"),
            self.client.patch(url + self.q, {"enabled": False}, format="json"),
            self.client.delete(url + self.q),
            self.client.post(f"{url}run/{self.q}"),
        ]
        self.assertEqual([c.status_code for c in calls], [404, 404, 404, 404])
        self.assertEqual({tuple(c.data) for c in calls}, {("error",)})
        mine = MemberSchedule.objects.get(pk=other)
        self.assertEqual((mine.name, mine.enabled), ("Theirs", True))

    def test_the_same_id_in_another_team_is_not_found(self):
        row = self.create()
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        url = f"{LIST}{row['id']}/?team_id={other.team_id}"
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(MemberSchedule.objects.filter(pk=row["id"]).exists())
        listed = self.client.get(f"{LIST}?team_id={other.team_id}").data["schedules"]
        self.assertEqual(listed, [])

    def test_an_unknown_id_is_the_same_not_found(self):
        res = self.client.delete(f"{LIST}{uuid.uuid4()}/{self.q}")
        self.assertEqual((res.status_code, res.data), (404, {"error": "Schedule not found."}))


class TestChangePauseDelete(ScheduleViewBase):
    def setUp(self):
        super().setUp()
        self.row = self.create()
        self.url = f"{LIST}{self.row['id']}/{self.q}"

    def patch(self, **body):
        return self.client.patch(self.url, body, format="json")

    def test_changing_the_time_moves_the_next_run(self):
        before = MemberSchedule.objects.get(pk=self.row["id"]).next_run_at
        res = self.patch(timeOfDay="17:30", name="Evening")
        self.assertEqual(res.status_code, 200, res.data)
        row = res.data["schedule"]
        self.assertEqual((row["timeOfDay"], row["name"]), ("17:30", "Evening"))
        self.assertEqual(row["nextRun"][11:16], "17:30")
        self.assertNotEqual(MemberSchedule.objects.get(pk=self.row["id"]).next_run_at, before)

    def test_pause_and_resume(self):
        paused = self.patch(enabled=False).data["schedule"]
        self.assertFalse(paused["enabled"])
        self.assertIsNone(paused["nextRun"])
        self.assertEqual(paused["pausedReason"], "paused by the member")
        resumed = self.patch(enabled=True).data["schedule"]
        self.assertTrue(resumed["enabled"])
        self.assertIsNotNone(resumed["nextRun"])
        self.assertNotIn("pausedReason", resumed)

    def test_resuming_forgets_a_pause_from_failures(self):
        MemberSchedule.objects.filter(pk=self.row["id"]).update(
            enabled=False, paused_reason="failures", consecutive_failures=3, next_run_at=None
        )
        self.assertTrue(self.patch(enabled=True).data["schedule"]["enabled"])
        self.assertEqual(MemberSchedule.objects.get(pk=self.row["id"]).consecutive_failures, 0)

    def test_a_change_and_a_pause_in_one_request_both_apply(self):
        row = self.patch(name="Both", enabled=False).data["schedule"]
        self.assertEqual((row["name"], row["enabled"]), ("Both", False))

    def test_changing_nothing_or_a_non_boolean_is_said(self):
        self.assertEqual(self.patch().data["error"], "Nothing to change.")
        self.assertIn("true or false", self.patch(enabled="yes").data["error"])

    def test_a_bad_change_leaves_the_schedule_as_it_was(self):
        res = self.patch(timeOfDay="25:00", name="Changed")
        self.assertEqual(res.status_code, 400)
        stored = MemberSchedule.objects.get(pk=self.row["id"])
        self.assertEqual((stored.time_of_day.hour, stored.name), (9, "Morning"))

    def test_delete(self):
        res = self.client.delete(self.url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(MemberSchedule.objects.filter(pk=self.row["id"]).exists())
        self.assertEqual(self.client.delete(self.url).status_code, 404)


class TestRunNow(ScheduleViewBase):
    def setUp(self):
        super().setUp()
        self.row = self.create()
        self.run_url = f"{LIST}{self.row['id']}/run/{self.q}"

    def test_it_runs_the_job_and_reports_the_outcome(self):
        res = self.client.post(self.run_url)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data, {"status": "ok", "error": "", "title": "Your day (today)", "items": 2}
        )
        row = self.client.get(LIST + self.q).data["schedules"][0]
        self.assertEqual(row["lastStatus"], "ok")
        self.assertEqual(row["lastResult"], {"title": "Your day (today)", "items": 2})
        self.assertEqual(row["nextRun"], self.row["nextRun"], "run now keeps the next run")

    def test_a_second_run_within_a_minute_is_refused_in_words(self):
        self.client.post(self.run_url)
        res = self.client.post(self.run_url)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Try again in", res.data["error"])

    def test_a_failing_job_is_reported_without_its_text(self):
        def boom(ctx, params):
            raise RuntimeError("secret detail that must not leak")

        with mock.patch.dict(JOBS, {"fake_day": Job("fake_day", "x", "y", boom, {})}):
            with self.assertLogs("origin.schedules", level="WARNING"):
                res = self.client.post(self.run_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "failed")
        self.assertNotIn("secret detail", str(res.data))
        row = self.client.get(LIST + self.q).data["schedules"][0]
        self.assertEqual(row["lastError"], "RuntimeError")
        self.assertNotIn("secret detail", str(row))

    def test_a_schedule_whose_job_was_removed_says_so(self):
        with mock.patch.dict(JOBS, clear=True):
            row = self.client.get(LIST + self.q).data["schedules"][0]
        self.assertIn("no longer exists", row["problem"])
        self.assertIsNone(row["jobTitle"])
