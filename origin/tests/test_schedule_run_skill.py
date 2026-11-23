"""A schedule runs a skill (MIP-2045): the job `run_skill`.

The model is faked (`controller.run_agent` is a scripted stub), so no request leaves this file. What
is pinned, each by a test that fails when its guard is removed:

  * ACL: a skill is scheduled and run only by someone who may run it. A private unpublished skill,
    another team's, a teammate's private one, a public one that was not installed: the same sentence
    as an id that does not exist, and a skill that is hidden or taken away AFTER the schedule was
    saved fails its next run in words instead of running on the authority it once had;
  * PERMISSIONS: the run is the owner's and only reads: every write tool and web search is
    undeclared, the owner's own plan gates are unioned in (never fewer), a skill that says `act`
    still reads only, and an outside author's skill keeps its extra limits;
  * VERSION: the published version is pinned when the schedule is saved. A later publish, the
    owner's live draft and an author's update do not move what runs; an owner's skill that was never
    published cannot be scheduled;
  * CREDITS: the run is charged to the schedule's owner on the `schedule` surface, one metered
    request whose id sits on the Inbox item's cost block; at zero credits nothing is called and the
    report is one line (the run still counts as a success, the schedule keeps running); running out
    mid-run keeps the partial answer;
  * SWITCH: `GENOS_SKILLS` off hides the job, refuses a new schedule and fails an existing one in
    words, with no model call;
  * the answer is data: cut into plain lines, checked again by the delivery, never raw markup;
  * the plan's count of skill schedules, and the daily "Run now" limit that every AI job has.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import override_settings

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import (
    STATUS_FAILED,
    STATUS_OK,
    MemberSchedule,
)
from origin.models.skills.skill_models import Skill, SkillInstall
from origin.search_engine import credit_ledger
from origin.search_engine.agent import controller
from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS, disabled_tools_for_level
from origin.search_engine.agent.tools import app_write_tools
from origin.search_engine.models import AiRequestCost
from origin.services import schedule_text, schedules
from origin.services.report_ai import effort as effort_mod
from origin.services.schedule_jobs import JOBS, run_skill
from origin.services.skills import service
from origin.tests.test_skills_api import SkillBase

BODY_V1 = (
    "# Weekly review\n\n## Steps\n1. Read the open tasks.\n2. FIRST-VERSION: write three lines."
)
BODY_V2 = (
    "# Weekly review\n\n## Steps\n1. Read the open tasks.\n2. SECOND-VERSION: write five lines."
)
ANSWER = (
    "Three things need you this week.\n"
    "## Overdue\n"
    "- [MIP-12 Ship the export](/workspace/tasks/project/1/task/12) is two days late\n"
    "- The vendor has not answered\n"
    "**Next**\n"
    "1. Reply to the vendor\n"
)

LIVE = {
    "AI_COST_METER": True,
    "GEMINI_API_KEY": "not-a-real-key",
    "GEMINI_USE_VERTEX": False,
    "LLM_PROVIDER": "gemini",
}


def authoritative():
    return override_settings(
        SEARCH_ENGINE={
            **settings.SEARCH_ENGINE,
            **LIVE,
            "AI_CREDITS_SHADOW": True,
            "AI_CREDITS_AUTHORITATIVE": True,
        }
    )


class Agent:
    """The scripted stand-in for `controller.run_agent`: records every call, answers with `answer`."""

    def __init__(self, answer=ANSWER, error=None, paused=False):
        self.answer, self.error, self.paused = answer, error, paused
        self.calls: list[dict] = []

    def __call__(self, query, ctx, emit, **kwargs):
        self.calls.append(
            {"query": query, "ctx": ctx, "savepoints": len(connection.savepoint_ids), **kwargs}
        )
        if self.error:
            if self.answer:
                emit({"type": "answer_delta", "text": self.answer})
            emit({"type": "error", "message": self.error})
        else:
            emit({"type": "answer_delta", "text": self.answer})
        return {"paused": True} if self.paused else None


class Base(SkillBase):
    def setUp(self):
        super().setUp()
        live = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **LIVE})
        live.enable()
        self.addCleanup(live.disable)
        ceiling = mock.patch.object(effort_mod, "_ceiling", return_value="high")
        ceiling.start()
        self.addCleanup(ceiling.stop)
        self.agent = Agent()
        patcher = mock.patch.object(controller, "run_agent", self.agent)
        patcher.start()
        self.addCleanup(patcher.stop)
        from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415

        self.ctx = ToolContext(team_id=self.team_id, user_id=str(self.user.pk))
        self.ctx2 = ToolContext(team_id=self.team_id, user_id=str(self.user2.pk))

    # -- skills ------------------------------------------------------------------------------
    def skill(self, owner=None, visibility="team", **over):
        owner = owner or self.user
        team = self.other_team.team_id if owner == self.outsider else self.team.team_id
        self._skills = getattr(self, "_skills", 0) + 1
        data = {
            "title": "Weekly review",
            "slug": f"weekly-review-{self._skills}",
            "description": "Review the week.",
            "tags": ["reports"],
            "body_markdown": BODY_V1,
            "visibility": visibility,
        }
        data.update(over)
        return service.create(owner, team, service.Fields(**data))

    def publish_v2(self, skill, body=BODY_V2):
        service.update(skill.owner, skill.id, service.Fields(body_markdown=body))
        return service.publish(skill.owner, skill.id)

    # -- schedules ---------------------------------------------------------------------------
    def schedule(self, skill, ctx=None, **kw):
        params = kw.pop("params") if "params" in kw else {"skill_id": str(skill.id)}
        return schedules.create(
            ctx or self.ctx,
            name=kw.pop("name", "Review"),
            job="run_skill",
            params=params,
            frequency="daily",
            time_of_day=kw.pop("time_of_day", "08:30"),
            **kw,
        )

    def execute(self, sched):
        return schedules._execute(sched)

    def inbox(self, user=None):
        return list(
            InboxItems.objects.filter(
                receiver_id=(user or self.user).pk, item_type=ITEM_TYPE_GENOS_REPORT
            ).order_by("ts_created_at")
        )

    def disabled(self):
        return self.agent.calls[-1]["disabled_tools"]

    def system_extra(self):
        return self.agent.calls[-1]["system_extra"]


class SavingTests(Base):
    """What the owner may schedule, and what the schedule stores."""

    def test_a_team_skill_of_mine_is_scheduled_and_its_version_pinned(self):
        skill = self.skill()
        sched = self.schedule(skill)
        self.assertEqual(sched.params, {"skill_id": str(skill.id), "version_no": 1})

    def test_the_input_is_kept_with_hidden_characters_removed(self):
        skill = self.skill()
        params = {"skill_id": str(skill.id), "input": "Q3​ plan \U000e0041"}
        sched = self.schedule(skill, params=params)
        self.assertEqual(sched.params["input"], "Q3 plan")

    def test_an_input_that_is_too_long_or_not_text_is_refused(self):
        skill = self.skill()
        for bad in ("x" * (run_skill.MAX_INPUT_CHARS + 1), 5, ["a"]):
            with self.subTest(bad=str(bad)[:10]), self.assertRaises(schedules.ScheduleError):
                self.schedule(skill, params={"skill_id": str(skill.id), "input": bad})

    def test_a_private_skill_that_was_never_published_cannot_be_scheduled(self):
        skill = self.skill(visibility="private")
        with self.assertRaises(schedules.ScheduleError) as caught:
            self.schedule(skill)
        self.assertIn("no published version", str(caught.exception))
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_skill_that_does_not_exist_or_is_not_a_uuid_is_the_same_refusal(self):
        for bad in ("00000000-0000-0000-0000-000000000000", "nope"):
            with self.subTest(bad=bad), self.assertRaises(schedules.ScheduleError) as caught:
                self.schedule(None, params={"skill_id": bad})
            self.assertIn("not found", str(caught.exception))
        with self.assertRaises(schedules.ScheduleError):
            self.schedule(None, params={"skill_id": ""})
        with self.assertRaises(schedules.ScheduleError):
            self.schedule(None, params={})

    def test_a_skill_the_owner_may_not_run_is_the_same_refusal_as_a_missing_one(self):
        theirs = self.skill(self.user2, visibility="private")  # a teammate's private skill
        other_team = self.skill(self.outsider, visibility="team")  # another team's
        stranger = self.skill(self.outsider, visibility="public")  # public, not installed
        for skill in (theirs, other_team):
            with self.subTest(skill=skill.slug), self.assertRaises(schedules.ScheduleError) as c:
                self.schedule(skill)
            self.assertIn("not found", str(c.exception))
        # A public skill is readable by anyone signed in, so it is named, but never run unless the
        # owner installed it (no squatting, the rule of `/name`).
        with self.assertRaises(schedules.ScheduleError) as c:
            self.schedule(stranger)
        self.assertIn("Install this skill", str(c.exception))
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_teammates_published_team_skill_and_an_installed_public_one_are_allowed(self):
        team_skill = self.skill(self.user2, visibility="team", slug="theirs")
        public = self.skill(self.outsider, visibility="public", slug="public-one")
        service.install(self.user, public.id)
        a = self.schedule(team_skill, name="A", time_of_day="08:31")
        b = self.schedule(public, name="B", time_of_day="08:32")
        self.assertEqual(a.params["version_no"], 1)
        self.assertEqual(b.params["version_no"], 1)

    def test_an_explicit_version_is_kept_and_a_missing_one_is_refused(self):
        skill = self.skill()
        self.publish_v2(skill)
        sched = self.schedule(skill, params={"skill_id": str(skill.id), "version_no": 1})
        self.assertEqual(sched.params["version_no"], 1)
        with self.assertRaises(schedules.ScheduleError):
            self.schedule(
                skill,
                name="Other",
                params={"skill_id": str(skill.id), "version_no": 9},
                time_of_day="09:00",
            )

    def test_an_edit_that_changes_the_skill_is_checked_again(self):
        mine = self.skill()
        theirs = self.skill(self.user2, visibility="private", slug="private-one")
        sched = self.schedule(mine)
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.id, params={"skill_id": str(theirs.id)})
        sched.refresh_from_db()
        self.assertEqual(sched.params["skill_id"], str(mine.id))
        # A change of the time alone does not look at the skill again.
        Skill.objects.filter(pk=mine.pk).update(status="taken_down", owner=self.user)
        schedules.update(self.ctx, sched.id, time_of_day="09:15")

    def test_the_job_may_go_to_a_chat_only_when_it_says_so(self):
        """Only a job that says `ai_in_private_chat` (this one) may post an AI report in a chat; every
        other AI job keeps going to the Inbox only (`schedule_audience.HAS_AI`)."""
        from origin.services import schedule_audience  # noqa: PLC0415

        self.assertTrue(JOBS["run_skill"].ai_applies({"skill_id": "x"}))
        self.assertTrue(JOBS["run_skill"].ai_in_private_chat)
        self.assertTrue(schedule_audience.HAS_AI)
        self.assertEqual([k for k, j in JOBS.items() if j.ai_in_private_chat], ["run_skill"])


class ChatDeliveryTests(Base):
    """A scheduled skill's report posted in a chat only its owner is in (MIP-2196). The cost line
    is in the message; a chat other people read never receives it, at saving AND at every run, and
    a run that must be refused is refused BEFORE the model is called."""

    def gm(self, *members, title="Genos reports", **extra):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember  # noqa: PLC0415

        extra.setdefault("is_private", True)
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title=title, owner=self.user, **extra
        )
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def to_chat(self, skill, channel, **kw):
        return self.schedule(skill, deliver_channel_id=channel.id, **kw)

    def posted(self, channel):
        from origin.models.chat.unified_models import Message  # noqa: PLC0415

        return list(Message.objects.filter(channel=channel).order_by("seq"))

    def test_the_owner_picks_a_chat_only_they_are_in(self):
        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        self.assertEqual(sched.deliver_channel_id, alone.id)

    def test_a_chat_other_people_read_is_refused_without_naming_them(self):
        from origin.services import schedule_audience  # noqa: PLC0415

        shared = self.gm(self.user, self.user2, title="Squad")
        with self.assertRaises(schedules.ScheduleError) as caught:
            self.to_chat(self.skill(), shared)
        self.assertEqual(str(caught.exception), schedule_audience.AI_NOT_ALONE)
        self.assertNotIn(self.user2.username, str(caught.exception))
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_public_chat_and_a_direct_message_are_refused_even_when_the_owner_is_alone(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember  # noqa: PLC0415

        public = self.gm(self.user, title="Open", is_private=False)
        direct = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=direct, user=self.user)
        skill = self.skill()
        for chat in (public, direct):
            with self.subTest(kind=chat.kind), self.assertRaises(schedules.ScheduleError):
                self.to_chat(skill, chat)

    def test_a_project_chat_is_refused_while_others_are_on_the_project(self):
        from origin.models.chat.unified_models import Channel, ChannelKind  # noqa: PLC0415
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415

        project = ProjectMaster.objects.create(
            project_name="Shared", team=self.team, owner=self.user
        )
        ProjectMembers.objects.create(project=project, attendee=self.user)
        ProjectMembers.objects.create(project=project, attendee=self.user2)
        pm = Channel.objects.get_or_create(
            team=self.team,
            kind=ChannelKind.PM,
            project=project,
            defaults={"title": "", "owner": self.user, "legacy_chat_id": project.project_id},
        )[0]
        with self.assertRaises(schedules.ScheduleError):
            self.to_chat(self.skill(), pm)

    def test_a_run_posts_one_message_with_its_cost_line_and_files_nothing_in_the_inbox(self):
        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(self.inbox(), [])
        (message,) = self.posted(alone)
        self.assertIsNone(message.sender_id)
        row = AiRequestCost.objects.get()
        marker = message.metadata["scheduleReport"]
        self.assertEqual(marker["job"], "run_skill")
        self.assertEqual(marker["scheduleId"], str(sched.id))
        self.assertEqual(marker["ai"]["request_id"], str(row.request_id))
        self.assertEqual(marker["ai"]["producer"], "schedule")
        # The cost line is a block of the message itself, not only metadata.
        texts = [
            b["content"][0]["text"]
            for b in message.body
            if b["content"] and b["content"][0].get("type") == "text"
        ]
        self.assertTrue(any(t.startswith("AI usage:") for t in texts), texts)
        self.assertIn("Three things need you this week.", texts)
        sched.refresh_from_db()
        self.assertEqual(sched.last_result["title"], "Weekly review")

    def test_a_person_added_to_the_chat_since_stops_the_run_before_the_model_is_called(self):
        from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415
        from origin.services import schedule_audience  # noqa: PLC0415

        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        ChannelMember.objects.create(channel=alone, user=self.user2)
        status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_FAILED, schedule_audience.AI_NOT_ALONE))
        self.assertEqual(self.agent.calls, [])  # nothing was spent on a report nobody may read
        self.assertEqual(AiRequestCost.objects.count(), 0)
        self.assertEqual(self.posted(alone), [])
        self.assertEqual(self.inbox(), [])

    def test_a_person_added_while_the_model_runs_gets_nothing_and_the_report_goes_to_the_inbox(
        self,
    ):
        from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415

        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        scripted = self.agent

        def joined_mid_run(*args, **kwargs):
            ChannelMember.objects.create(channel=alone, user=self.user2)
            return scripted(*args, **kwargs)

        with mock.patch.object(controller, "run_agent", joined_mid_run):
            status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(self.posted(alone), [])  # the colleague never receives it
        (item,) = self.inbox()  # the credits were spent: the owner keeps the report and its cost
        self.assertEqual(
            item.item_optionals["ai"]["request_id"], str(AiRequestCost.objects.get().request_id)
        )

    def test_the_owner_leaving_the_chat_stops_the_run_before_the_model_is_called(self):
        from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415

        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        ChannelMember.objects.filter(channel=alone, user=self.user).delete()
        status, _error = self.execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(self.agent.calls, [])

    def test_a_repeat_of_one_scheduled_run_posts_once(self):
        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        at = datetime(2026, 11, 24, 8, 30, tzinfo=timezone.utc)
        self.assertEqual(schedules._execute(sched, run_at=at)[0], STATUS_OK)
        self.assertEqual(schedules._execute(sched, run_at=at)[0], STATUS_OK)
        self.assertEqual(len(self.posted(alone)), 1)

    def test_at_zero_credits_the_chat_gets_the_one_line_and_no_cost(self):
        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        with (
            authoritative(),
            mock.patch("origin.search_engine.credit_ledger.balance_milli", return_value=0),
        ):
            status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(self.agent.calls, [])
        (message,) = self.posted(alone)
        self.assertIn("no AI credits left", message.body[1]["content"][0]["text"])
        self.assertNotIn("ai", message.metadata["scheduleReport"])
        self.assertFalse(
            any("AI usage" in b["content"][0].get("text", "") for b in message.body if b["content"])
        )
        sched.refresh_from_db()
        self.assertEqual(sched.last_result["ai_skipped"], "no_credits")

    def test_a_job_that_names_no_request_posts_nothing(self):
        from origin.services import schedule_delivery  # noqa: PLC0415
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415

        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        report = {"title": "T", "summary": "S", "sections": []}  # no request, no skip reason
        with self.assertRaises(Exception) as caught:
            schedule_delivery.deliver(sched, report)
        self.assertNotIsInstance(caught.exception, ScheduleRunError)
        self.assertEqual(self.posted(alone), [])

    def test_run_now_in_a_chat_counts_toward_the_daily_limit(self):
        alone = self.gm(self.user)
        sched = self.to_chat(self.skill(), alone)
        now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
        for i in range(schedules.AI_RUN_NOW_PER_DAY):
            result = schedules.run_now(self.ctx, sched.id, now=now + timedelta(minutes=5 * i))
            self.assertEqual(result["status"], STATUS_OK)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.run_now(self.ctx, sched.id, now=now + timedelta(minutes=30))
        self.assertIn("by hand", str(caught.exception))
        self.assertEqual(len(self.posted(alone)), schedules.AI_RUN_NOW_PER_DAY)

    def test_the_cost_line_is_numbers_only(self):
        from origin.services import schedule_delivery  # noqa: PLC0415

        line = schedule_delivery.cost_line
        self.assertEqual(
            line({"metered": True, "credits_milli": 1830, "lower_bound": False}),
            "AI usage: 1.83 credits.",
        )
        self.assertEqual(
            line({"metered": True, "credits_milli": 500, "lower_bound": True}),
            "AI usage: at least 0.50 credits.",
        )
        self.assertEqual(line({"metered": False}), "AI usage of this run was not measured.")
        self.assertEqual(
            line({"metered": True, "credits_milli": 2, "lower_bound": True}),
            "AI usage of this run was not measured.",
        )

    def test_the_inbox_path_is_untouched(self):
        sched = self.schedule(self.skill())
        self.assertEqual(self.execute(sched)[0], STATUS_OK)
        (item,) = self.inbox()
        self.assertIn("ai", item.item_optionals)


class RunAccessTests(Base):
    """The owner's access is checked at every run, not only when the schedule is saved."""

    def run_and_read(self, sched):
        status, error = self.execute(sched)
        return status, error

    def test_a_run_files_one_inbox_item_for_the_owner_only(self):
        sched = self.schedule(self.skill())
        status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.inbox(self.user2), [])

    def test_a_skill_hidden_taken_down_or_deleted_after_saving_fails_the_run_in_words(self):
        for change in ("hidden_by_team", "taken_down", "reported_hidden"):
            skill = self.skill(self.user2)
            sched = self.schedule(skill, name=change, time_of_day="08:40")
            Skill.objects.filter(pk=skill.pk).update(status=change)
            calls = len(self.agent.calls)
            status, error = self.execute(sched)
            self.assertEqual(status, STATUS_FAILED, change)
            self.assertIn("no longer available", error)
            self.assertEqual(len(self.agent.calls), calls, "nothing was called")
        self.assertEqual(self.inbox(), [])

    def test_a_teammates_skill_made_private_again_stops_running(self):
        skill = self.skill(self.user2, slug="was-team")
        sched = self.schedule(skill)
        Skill.objects.filter(pk=skill.pk).update(visibility="private")
        status, error = self.execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertIn("no longer available", error)

    def test_an_uninstalled_public_skill_stops_running(self):
        public = self.skill(self.outsider, visibility="public")
        service.install(self.user, public.id)
        sched = self.schedule(public)
        SkillInstall.objects.all().delete()
        status, _error = self.execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(self.agent.calls, [])

    def test_a_public_skill_switched_off_by_the_operator_stops_running(self):
        public = self.skill(self.outsider, visibility="public")
        service.install(self.user, public.id)
        sched = self.schedule(public)
        with override_settings(GENOS_SKILLS_PUBLIC=False):
            status, _error = self.execute(sched)
        self.assertEqual(status, STATUS_FAILED)

    def test_the_owners_own_skill_that_was_taken_down_or_hidden_stops_running(self):
        """`can_read` lets an owner read their own skill whatever its status: an unattended run
        must not."""
        for change in ("taken_down", "reported_hidden", "hidden_by_team"):
            skill = self.skill()
            sched = self.schedule(skill, name=change, time_of_day="08:50")
            Skill.objects.filter(pk=skill.pk).update(status=change)
            calls = len(self.agent.calls)
            status, error = self.execute(sched)
            self.assertEqual(status, STATUS_FAILED, change)
            self.assertIn("no longer available", error)
            self.assertEqual(len(self.agent.calls), calls)
            with self.assertRaises(schedules.ScheduleError):
                self.schedule(skill, name=f"again {change}", time_of_day="08:51")
            sched.delete()

    def test_the_job_is_not_wrapped_in_a_savepoint_so_its_spend_cannot_be_rolled_back(self):
        """A model loop held inside `transaction.atomic()` would lose the record of what it spent
        when the process dies. The delivery still has its own savepoint."""
        self.assertTrue(JOBS["run_skill"].unwrapped)
        self.assertFalse(JOBS["what_you_did"].unwrapped)
        sched = self.schedule(self.skill())
        outside = len(connection.savepoint_ids)  # the test's own
        self.assertEqual(self.execute(sched)[0], STATUS_OK)
        self.assertEqual(
            self.agent.calls[-1]["savepoints"],
            outside,
            "the model loop is in no savepoint of its own",
        )


class PermissionTests(Base):
    """The run is the owner's, and it only reads."""

    def test_every_write_and_web_search_is_undeclared(self):
        sched = self.schedule(self.skill(tool_level="act"))
        self.execute(sched)
        writes = {t.name for t in app_write_tools()}
        self.assertTrue(writes)
        self.assertLessEqual(writes, self.disabled())
        self.assertIn("search_web", self.disabled())
        self.assertLessEqual(SINGLE_WRITE_TOOLS & writes, self.disabled())

    def test_a_skill_that_says_act_still_reads_only(self):
        self.execute(self.schedule(self.skill(tool_level="act")))
        self.assertIn("may only read", self.system_extra())
        self.assertNotIn("propose single changes", self.system_extra())

    def test_the_owners_own_plan_gates_are_unioned_in_never_dropped(self):
        extra = {"list_calendar_events", "search_past_conversations"}
        sched = self.schedule(self.skill())
        with mock.patch(
            "origin.search_engine.agent.tool_tiers.disabled_tools_for_user", return_value=extra
        ):
            self.execute(sched)
        self.assertLessEqual(extra, self.disabled())
        self.assertLessEqual(disabled_tools_for_level("read"), self.disabled())

    def test_the_run_is_the_owners_not_the_authors(self):
        skill = self.skill(self.user2, slug="theirs")
        self.execute(self.schedule(skill))
        ctx = self.agent.calls[-1]["ctx"]
        self.assertEqual((ctx.user_id, ctx.team_id), (str(self.user.pk), self.team_id))
        self.assertNotEqual(ctx.user_id, str(self.user2.pk))

    def test_an_outside_authors_skill_keeps_its_limits_and_says_so(self):
        public = self.skill(self.outsider, visibility="public", tool_level="act")
        service.install(self.user, public.id)
        self.execute(self.schedule(public))
        self.assertIn("someone outside the person's team", self.system_extra())
        self.assertIn("Do not delete, share, message anyone", self.system_extra())
        self.assertIn("search_web", self.disabled())

    def test_the_skill_text_is_fenced_and_the_run_is_told_nobody_is_there(self):
        self.execute(self.schedule(self.skill()))
        extra = self.system_extra()
        self.assertIn("<skill_text>", extra)
        self.assertIn("FIRST-VERSION", extra)
        self.assertIn("SCHEDULED RUN", extra)
        self.assertIn("Do not ask questions", extra)

    def test_a_run_that_still_asks_to_write_is_one_line_and_not_charged(self):
        self.agent.paused = True
        status, _error = self.execute(self.schedule(self.skill()))
        self.assertEqual(status, STATUS_OK)
        (item,) = self.inbox()
        self.assertIn("may not do", item.item_body["text"])
        self.assertEqual(item.item_body["sections"], [])
        self.assertEqual(
            AiRequestCost.objects.get().result, AiRequestCost.RESULT_APPLICATION_FAILURE
        )

    def test_the_input_is_what_the_run_is_asked(self):
        skill = self.skill()
        sched = self.schedule(skill, params={"skill_id": str(skill.id), "input": "for Q3"})
        self.execute(sched)
        self.assertEqual(self.agent.calls[-1]["query"], "for Q3")
        other = self.schedule(skill, name="No input", time_of_day="09:00")
        self.execute(other)
        self.assertEqual(self.agent.calls[-1]["query"], "Run this skill now.")


class VersionTests(Base):
    def test_a_later_publish_does_not_move_what_a_schedule_runs(self):
        skill = self.skill()
        sched = self.schedule(skill)
        self.publish_v2(skill)
        self.execute(sched)
        self.assertIn("FIRST-VERSION", self.system_extra())
        self.assertNotIn("SECOND-VERSION", self.system_extra())
        self.assertIn("version 1", self.system_extra())

    def test_the_owners_unpublished_draft_never_reaches_an_unattended_run(self):
        skill = self.skill()
        sched = self.schedule(skill)
        service.update(self.user, skill.id, service.Fields(body_markdown="DRAFT-ONLY half text"))
        self.execute(sched)
        self.assertNotIn("DRAFT-ONLY", self.system_extra())
        self.assertIn("FIRST-VERSION", self.system_extra())

    def test_editing_the_schedule_repins_to_the_published_version(self):
        skill = self.skill()
        sched = self.schedule(skill)
        self.publish_v2(skill)
        sched = schedules.update(self.ctx, sched.id, params={"skill_id": str(skill.id)})
        self.assertEqual(sched.params["version_no"], 2)
        self.execute(sched)
        self.assertIn("SECOND-VERSION", self.system_extra())

    def test_an_installed_skill_runs_the_version_pinned_not_the_authors_update(self):
        public = self.skill(self.outsider, visibility="public")
        service.install(self.user, public.id)
        sched = self.schedule(public)
        self.publish_v2(public)
        self.execute(sched)
        self.assertIn("FIRST-VERSION", self.system_extra())
        self.assertNotIn("SECOND-VERSION", self.system_extra())


class CreditTests(Base):
    def test_the_run_is_charged_to_the_owner_as_a_schedule_request(self):
        skill = self.skill(self.user2, slug="theirs")  # the author is user2, the owner is user
        sched = self.schedule(skill)
        self.execute(sched)
        rows = list(AiRequestCost.objects.all())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].surface, rows[0].user_id), ("schedule", str(self.user.pk)))
        self.assertEqual(rows[0].result, AiRequestCost.RESULT_SUCCESS)
        self.assertEqual(AiRequestCost.objects.filter(user_id=str(self.user2.pk)).count(), 0)
        item = self.inbox()[0]
        self.assertEqual(item.item_optionals["ai"]["request_id"], str(rows[0].request_id))
        self.assertEqual(item.item_optionals["ai"]["producer"], "schedule")

    def test_the_loop_is_given_the_balance_as_its_budget(self):
        sched = self.schedule(self.skill())
        with (
            mock.patch.object(run_skill, "_credit_budget_usd_micro", return_value=123_000),
            mock.patch(
                "origin.search_engine.metered.spend_kwargs_for",
                wraps=__import__(
                    "origin.search_engine.metered", fromlist=["spend_kwargs_for"]
                ).spend_kwargs_for,
            ) as spy,
        ):
            self.execute(sched)
        self.assertEqual(spy.call_args.args[0], "schedule")
        self.assertEqual(spy.call_args.args[5], 123_000)

    def test_at_zero_credits_nothing_is_called_and_the_report_is_one_line(self):
        sched = self.schedule(self.skill())
        with (
            authoritative(),
            mock.patch("origin.search_engine.credit_ledger.balance_milli", return_value=0),
        ):
            status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(AiRequestCost.objects.count(), 0)
        (item,) = self.inbox()
        self.assertIn("no AI credits left", item.item_body["text"])
        self.assertEqual(item.item_body["sections"], [])
        self.assertEqual(item.item_optionals["ai_skipped"], "no_credits")
        self.assertNotIn("ai", item.item_optionals)

    def test_a_skipped_run_does_not_pause_or_fail_the_schedule(self):
        sched = self.schedule(self.skill())
        with (
            authoritative(),
            mock.patch("origin.search_engine.credit_ledger.balance_milli", return_value=0),
        ):
            for _ in range(schedules.MAX_FAILURES + 1):
                self.assertEqual(self.execute(sched)[0], STATUS_OK)
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.consecutive_failures, 0)

    def test_with_credits_left_the_run_goes_ahead(self):
        sched = self.schedule(self.skill())
        credit_ledger.ensure_monthly_grant(str(self.user.pk), "max")
        with authoritative():
            self.assertEqual(self.execute(sched)[0], STATUS_OK)
        self.assertEqual(len(self.agent.calls), 1)

    def test_credits_running_out_mid_run_keep_the_partial_answer(self):
        self.agent.error = controller.CREDITS_EXHAUSTED_MESSAGE
        status, _error = self.execute(self.schedule(self.skill()))
        self.assertEqual(status, STATUS_OK)
        (item,) = self.inbox()
        self.assertTrue(item.item_body["text"].startswith("Stopped early"))
        self.assertEqual(AiRequestCost.objects.get().result, AiRequestCost.RESULT_CREDITS_EXHAUSTED)
        self.assertIn("ai", item.item_optionals)

    def test_another_error_is_one_line_with_no_partial_answer_and_the_spend_is_kept(self):
        """A failure after the model was called is a REPORT, not a raise: a raise would roll the
        job's savepoint back and with it the record of what the run spent."""
        self.agent.error = "The provider said something"
        status, error = self.execute(self.schedule(self.skill()))
        self.assertEqual((status, error), (STATUS_OK, ""))
        (item,) = self.inbox()
        self.assertIn("did not finish", item.item_body["text"])
        self.assertNotIn("provider", item.item_body["text"])
        self.assertEqual(item.item_body["sections"], [])
        row = AiRequestCost.objects.get()
        self.assertEqual(row.result, AiRequestCost.RESULT_APPLICATION_FAILURE)
        self.assertEqual(item.item_optionals["ai"]["request_id"], str(row.request_id))

    def test_a_model_that_raises_is_the_same_one_line(self):
        with mock.patch.object(controller, "run_agent", side_effect=RuntimeError("secret words")):
            status, _error = self.execute(self.schedule(self.skill()))
        self.assertEqual(status, STATUS_OK)
        (item,) = self.inbox()
        self.assertIn("did not finish", item.item_body["text"])
        self.assertNotIn("secret", repr(item.item_body))
        self.assertEqual(
            AiRequestCost.objects.get().result, AiRequestCost.RESULT_APPLICATION_FAILURE
        )

    def test_an_empty_answer_is_one_line_too(self):
        self.agent.answer = "  \n"
        status, _error = self.execute(self.schedule(self.skill()))
        self.assertEqual(status, STATUS_OK)
        (item,) = self.inbox()
        self.assertIn("no answer", item.item_body["text"])

    def test_the_operators_switches_skip_the_run_without_a_call(self):
        sched = self.schedule(self.skill())
        cfg = settings.SEARCH_ENGINE
        cases = {
            "kill switch": {**cfg, "AI_AGENT_KILL_SWITCH": True},
            "meter off": {**cfg, "AI_COST_METER": False},
            "no credentials": {**cfg, "GEMINI_API_KEY": ""},
        }
        for name, engine in cases.items():
            with self.subTest(name), override_settings(SEARCH_ENGINE=engine):
                self.assertEqual(self.execute(sched)[0], STATUS_OK)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(len(self.inbox()), len(cases))
        self.assertTrue(all("Not run" in i.item_body["text"] for i in self.inbox()))

    def test_run_now_on_this_job_is_limited_per_day_like_every_ai_job(self):
        sched = self.schedule(self.skill())
        now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
        for i in range(schedules.AI_RUN_NOW_PER_DAY):
            result = schedules.run_now(self.ctx, sched.id, now=now + timedelta(minutes=5 * i))
            self.assertEqual(result["status"], STATUS_OK)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.run_now(self.ctx, sched.id, now=now + timedelta(minutes=30))
        self.assertIn("by hand", str(caught.exception))


class SwitchTests(Base):
    def test_off_the_job_is_not_offered_and_nothing_can_be_saved(self):
        skill = self.skill()
        self.assertIn("run_skill", [j["job"] for j in schedule_text.job_catalog()])
        with override_settings(GENOS_SKILLS=False):
            self.assertNotIn("run_skill", [j["job"] for j in schedule_text.job_catalog()])
            with self.assertRaises(schedules.ScheduleError) as caught:
                self.schedule(skill)
            self.assertIn("There is no job called", str(caught.exception))
            self.assertNotIn("run_skill", str(caught.exception).split("Available jobs:")[1])

    def test_off_an_existing_schedule_fails_in_words_and_calls_nothing(self):
        sched = self.schedule(self.skill())
        with override_settings(GENOS_SKILLS=False):
            status, error = self.execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertIn("switched off", error)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(AiRequestCost.objects.count(), 0)
        # The row is still there: the switch never deletes a member's schedule.
        self.assertTrue(MemberSchedule.objects.filter(pk=sched.pk).exists())

    def test_off_the_other_jobs_are_exactly_as_they_were(self):
        built_in = {"what_to_do_today", "what_you_did", "what_is_left"}
        with override_settings(GENOS_SKILLS=False):
            offered = {j["job"] for j in schedule_text.job_catalog()}
        self.assertTrue(built_in <= offered)
        self.assertNotIn("run_skill", offered)
        for key in built_in:
            self.assertIsNone(JOBS[key].available)
            self.assertIsNone(JOBS[key].check_params)

    def test_on_the_catalog_has_the_parameters_and_no_sentence_estimate(self):
        (entry,) = [j for j in schedule_text.job_catalog() if j["job"] == "run_skill"]
        self.assertTrue(entry["uses_ai"])
        self.assertEqual(entry["effort_default"], "medium")
        self.assertIsNone(entry["effort_cost"])
        self.assertTrue(entry["params"]["skill_id"]["required"])


class PlanCountTests(Base):
    def test_the_plans_count_of_skill_schedules_is_kept(self):
        with mock.patch.dict(run_skill.SKILL_SCHEDULES_MAX_BY_TIER, {"max": 2}):
            first = self.schedule(self.skill(slug="one"), name="One", time_of_day="08:01")
            self.schedule(self.skill(slug="two"), name="Two", time_of_day="08:02")
            with self.assertRaises(schedules.ScheduleLimitReached) as caught:
                self.schedule(self.skill(slug="three"), name="Three", time_of_day="08:03")
            self.assertEqual((caught.exception.limit, caught.exception.used), (2, 2))
            # An existing one stays editable at the limit, and another job is not counted.
            schedules.update(self.ctx, first.id, name="Renamed")
            schedules.create(
                self.ctx,
                name="Plain",
                job="what_you_did",
                frequency="daily",
                time_of_day="18:00",
            )

    def test_after_a_downgrade_an_existing_skill_schedule_stays_editable(self):
        skills = [self.skill(slug=f"s-{i}") for i in range(3)]
        made = [
            self.schedule(s, name=f"S{i}", time_of_day=f"08:0{i}") for i, s in enumerate(skills)
        ]
        with mock.patch.dict(run_skill.SKILL_SCHEDULES_MAX_BY_TIER, {"max": 1}):
            edited = schedules.update(
                self.ctx,
                made[0].id,
                params={"skill_id": str(skills[0].id), "input": "for Q4"},
            )
            self.assertEqual(edited.params["input"], "for Q4")
            # A NEW one is still refused.
            with self.assertRaises(schedules.ScheduleLimitReached):
                self.schedule(self.skill(slug="s-new"), name="New", time_of_day="09:30")

    def test_the_default_counts_follow_the_design(self):
        self.assertEqual(
            run_skill.SKILL_SCHEDULES_MAX_BY_TIER,
            {"free": 0, "core": 3, "pro": 10, "max": 30, "enterprise": None},
        )

    def test_a_tier_that_cannot_be_read_is_unlimited(self):
        with mock.patch("origin.search_engine.quota.get_effective_tier", side_effect=RuntimeError):
            self.assertIsNone(run_skill.skill_schedules_limit(self.user.pk))


class AnswerAsReportTests(Base):
    def test_the_answer_becomes_a_summary_sections_and_items(self):
        report = run_skill.report_from_answer("Weekly review", ANSWER)
        self.assertEqual(report["summary"], "Three things need you this week.")
        self.assertEqual([s["heading"] for s in report["sections"]], ["Overdue", "Next"])
        overdue = report["sections"][0]["items"]
        self.assertEqual(overdue[0]["text"], "MIP-12 Ship the export is two days late")
        self.assertEqual(overdue[0]["link"], "/workspace/tasks/project/1/task/12")
        self.assertEqual(report["sections"][1]["items"], [{"text": "Reply to the vendor"}])

    def test_it_is_filed_as_plain_lines_whatever_the_model_wrote(self):
        hostile = (
            "<script>alert(1)</script> all fine\n"
            "- [click](javascript:alert) and [task:5] ![](https://x.test/p.png?d=secret)\n"
            "- [read](https://x.test/leak?d=secret) and [ok](/workspace/tasks/project/1/task/2)\n"
            "- " + "word " * 120 + "\n"
        )
        self.agent.answer = hostile
        self.execute(self.schedule(self.skill()))
        (item,) = self.inbox()
        lines = [i for s in item.item_body["sections"] for i in s["items"]]
        self.assertEqual(
            [line["link"] for line in lines if "link" in line],
            ["/workspace/tasks/project/1/task/2"],
            "only a link into the app is kept: not javascript:, not an outside address",
        )
        self.assertNotIn("x.test", repr(item.item_body))
        for line in lines:
            self.assertLessEqual(len(line["text"]), 240)
            self.assertNotIn("](", line["text"])
        self.assertGreaterEqual(len(lines), 4, "a long line is split, not cut")

    def test_the_report_is_named_by_the_skill_and_has_a_cost_block(self):
        self.execute(self.schedule(self.skill()))
        (item,) = self.inbox()
        self.assertEqual(item.item_body["title"], "Weekly review")
        self.assertIn("ai", item.item_optionals)

    def test_a_one_line_answer_is_just_the_summary(self):
        report = run_skill.report_from_answer("T", "All quiet this week.")
        self.assertEqual(report["summary"], "All quiet this week.")
        self.assertEqual(report["sections"], [])
