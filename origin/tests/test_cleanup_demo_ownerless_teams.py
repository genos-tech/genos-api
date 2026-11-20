"""MIP-1721: `cleanup_demo_users` also finds a demo team whose owner is already NULL.

`TeamMaster.owner` is SET_NULL. `delete_demo_environment(user)` finds a demo workspace through
its OWNER, so a demo user row deleted any other way (a shell `.delete()`, the admin) left the team
with `owner = NULL`, its members, chats, tasks and notes behind, and no later cleanup could find
it: the cron selected demo USERS, and there was none.

The ownerless teams here are made the way that happened: the REAL seeder
(`create_demo_environment`), then the demo user deleted without its team.

MUST be a transactional test: the teardown's deferred FKs are only checked at a real COMMIT.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.services import demo_seeder
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()


class OwnerlessBase(CheapFlushTransactionTestCase):
    def seeded(self, name, *, hours_old=48, is_demo=True):
        """A seeded demo workspace, backdated; returns (user, team)."""
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="x", is_demo=is_demo
        )
        team = TeamMaster.objects.get(team_id=create_demo_environment(user)["team_id"])
        self.backdate(user, team, hours_old)
        return user, team

    def backdate(self, user, team, hours):
        then = timezone.now() - timedelta(hours=hours)
        User.objects.filter(pk=user.pk).update(ts_created_at=then)
        TeamMaster.objects.filter(pk=team.pk).update(ts_created_at=then)

    def ownerless(self, name, **kw):
        """What a shell `demo_user.delete()` leaves: the team, without its owner."""
        user, team = self.seeded(name, **kw)
        bots = list(
            TeamMembers.objects.filter(team=team)
            .exclude(attendee=user)
            .values_list("attendee_id", flat=True)
        )
        user.delete()
        team.refresh_from_db()
        self.assertIsNone(team.owner_id)
        self.assertTrue(team.is_demo)
        return team, bots

    def run_cleanup(self, *args):
        out = StringIO()
        call_command("cleanup_demo_users", *args, stdout=out)
        return out.getvalue()

    def alive(self, team):
        return TeamMaster.objects.filter(team_id=team.team_id).exists()

    def has_data(self, team):
        return (
            Channel.objects.filter(team=team.team_id).exists()
            or TaskMaster.objects.filter(team=team.team_id).exists()
            or PersonalNoteMaster.objects.filter(team=team.team_id).exists()
            or TeamMembers.objects.filter(team=team.team_id).exists()
        )


class ThePremise(OwnerlessBase):
    def test_a_deleted_demo_user_leaves_a_team_the_cleanup_never_finds(self):
        """Reproduces the task. Fails once the cron looks at teams too."""
        team, bots = self.ownerless("own_prem")
        self.assertTrue(self.has_data(team), "the real seed has data to leave behind")
        self.run_cleanup("--hours", "24")
        self.run_cleanup("--hours", "0")
        self.assertFalse(self.alive(team), "the cron left an ownerless demo team behind")
        self.assertFalse(self.has_data(team))
        self.assertFalse(User.objects.filter(id__in=bots).exists(), "its bot users stayed")


class WhatIsSwept(OwnerlessBase):
    def test_an_old_ownerless_demo_team_goes_with_its_data_and_bots(self):
        team, bots = self.ownerless("own_old")
        out = self.run_cleanup()
        self.assertIn("Deleted 1 ownerless demo team(s), 0 failed.", out)
        self.assertFalse(self.alive(team))
        self.assertFalse(self.has_data(team))
        self.assertFalse(User.objects.filter(id__in=bots).exists())

    def test_the_window_is_the_users_window(self):
        young, _ = self.ownerless("own_young", hours_old=2)
        old, _ = self.ownerless("own_old2", hours_old=30)
        self.run_cleanup("--hours", "24")
        self.assertTrue(self.alive(young), "inside the window: a visitor may still be setting up")
        self.assertFalse(self.alive(old))
        self.run_cleanup("--hours", "1")
        self.assertFalse(self.alive(young))

    def test_dry_run_reports_and_writes_nothing(self):
        team, bots = self.ownerless("own_dry")
        out = self.run_cleanup("--dry-run")
        self.assertIn("would delete", out)
        self.assertTrue(self.alive(team))
        self.assertTrue(self.has_data(team))
        self.assertEqual(User.objects.filter(id__in=bots).count(), len(bots))

    def test_runs_with_no_stale_user_at_all(self):
        """The command used to return early when there was no stale user."""
        team, _ = self.ownerless("own_nouser")
        self.assertFalse(User.objects.filter(is_demo=True, own_teams__isnull=False).exists())
        self.run_cleanup()
        self.assertFalse(self.alive(team))

    def test_the_ordinary_user_sweep_is_unchanged_and_runs_in_the_same_pass(self):
        user, team = self.seeded("own_usual")
        orphan, _ = self.ownerless("own_with_usual")
        out = self.run_cleanup()
        self.assertIn("Deleted 1 demo user(s), 0 failed.", out)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(self.alive(team))
        self.assertFalse(self.alive(orphan))

    def test_a_second_run_finds_nothing(self):
        self.ownerless("own_twice")
        self.run_cleanup()
        self.assertIn("No demo", self.run_cleanup())


class WhatIsNeverTouched(OwnerlessBase):
    def test_a_demo_team_with_a_live_owner_is_not_swept_as_ownerless(self):
        """The owner is old enough for the USER sweep, but a fresh window protects both: only the
        ownerless path is under test, so the owner is young."""
        user, team = self.seeded("own_live", hours_old=1)
        self.run_cleanup("--hours", "24")
        self.assertTrue(self.alive(team))
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_an_old_demo_team_with_a_live_owner_is_not_listed_as_ownerless(self):
        """Old enough for every window: only its owner keeps it out of the ownerless list. The
        dry run shows what the SELECTION found, which the delete's own re-check would hide."""
        _user, team = self.seeded("own_live_old")
        out = self.run_cleanup("--dry-run")
        self.assertNotIn("ownerless", out)
        self.assertTrue(self.alive(team))

    def test_a_demo_team_owned_by_a_non_demo_user_is_not_touched(self):
        """The reviewer account's shape: a real user owning a team the seeder marked demo."""
        user, team = self.seeded("own_reviewer", is_demo=False)
        self.run_cleanup("--hours", "1")
        self.assertTrue(self.alive(team))
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_the_sweep_function_itself_refuses_a_team_that_has_an_owner(self):
        """Not only the selection: the delete re-checks, because the owner may have been set
        between the read and the delete."""
        user, team = self.seeded("own_recheck")
        self.assertFalse(demo_seeder.delete_ownerless_demo_team(team.team_id))
        self.assertTrue(self.alive(team))
        self.assertTrue(self.has_data(team))

    def test_a_non_demo_team_without_an_owner_is_not_touched(self):
        team = TeamMaster.objects.create(team_name="Real", team_email="r@example.com", owner=None)
        TeamMaster.objects.filter(pk=team.pk).update(
            ts_created_at=timezone.now() - timedelta(days=90)
        )
        member = User.objects.create_user(
            username="real_m", email="real_m@example.com", password="x"
        )
        TeamMembers.objects.create(team=team, attendee=member)
        self.assertNotIn("ownerless", self.run_cleanup("--hours", "1", "--dry-run"))
        self.run_cleanup("--hours", "1")
        self.assertTrue(self.alive(team))
        self.assertTrue(TeamMembers.objects.filter(team=team).exists())
        self.assertFalse(demo_seeder.delete_ownerless_demo_team(team.team_id))
        self.assertTrue(self.alive(team))

    def test_a_soft_deleted_ownerless_demo_team_is_left_alone(self):
        """The task's selection is "not deleted": a soft-deleted team is somebody's decision."""
        team, _ = self.ownerless("own_soft")
        TeamMaster.objects.filter(pk=team.pk).update(is_deleted=True)
        self.assertNotIn("ownerless", self.run_cleanup("--hours", "1", "--dry-run"))
        self.run_cleanup("--hours", "1")
        self.assertTrue(self.alive(team))
        self.assertFalse(demo_seeder.delete_ownerless_demo_team(team.team_id))
        self.assertTrue(self.alive(team))

    def test_members_who_are_not_demo_bots_or_who_belong_elsewhere_are_kept(self):
        team, bots = self.ownerless("own_members")
        real = User.objects.create_user(
            username="real_in", email="real_in@example.com", password="x"
        )
        TeamMembers.objects.create(team=team, attendee=real)
        # a demo user who is ALSO in another, living team (and owns it)
        other_user, other_team = self.seeded("own_other", hours_old=1)
        TeamMembers.objects.create(team=team, attendee=other_user)
        # a demo bot of that other team that owns nothing: only its OTHER membership keeps it
        other_bot = (
            TeamMembers.objects.filter(team=other_team)
            .exclude(attendee=other_user)
            .values_list("attendee_id", flat=True)
            .first()
        )
        TeamMembers.objects.create(team=team, attendee_id=other_bot)
        # a demo user who owns a team they are not listed in, and is in this one
        keeper = User.objects.create_user(
            username="own_keeper", email="own_keeper@example.com", password="x", is_demo=True
        )
        TeamMaster.objects.create(team_name="Keeper's", team_email="k@example.com", owner=keeper)
        TeamMembers.objects.create(team=team, attendee=keeper)
        self.run_cleanup("--hours", "24")
        self.assertFalse(self.alive(team))
        self.assertTrue(User.objects.filter(pk=real.pk).exists(), "a non-demo member was deleted")
        self.assertTrue(User.objects.filter(pk=other_user.pk).exists())
        self.assertTrue(User.objects.filter(pk=other_bot).exists(), "a bot of another team")
        self.assertTrue(User.objects.filter(pk=keeper.pk).exists(), "a demo user who owns a team")
        self.assertTrue(self.alive(other_team))
        self.assertTrue(TeamMembers.objects.filter(team=other_team, attendee=other_user).exists())
        self.assertFalse(User.objects.filter(id__in=bots).exists())


class OneFailureDoesNotStopTheRest(OwnerlessBase):
    def test_a_team_that_fails_is_logged_and_the_others_still_go(self):
        bad, _ = self.ownerless("own_bad")
        good, _ = self.ownerless("own_good")
        real = demo_seeder.delete_demo_team_data

        def boom(team_id):
            if str(team_id) == str(bad.team_id):
                raise RuntimeError("storage down")
            return real(team_id)

        with mock.patch.object(demo_seeder, "delete_demo_team_data", side_effect=boom):
            # CronCommand turns a logged ERROR into a failed run, as for a failed user
            with self.assertRaises(CommandError):
                self.run_cleanup()
        self.assertTrue(self.alive(bad), "the failed team is rolled back whole")
        self.assertTrue(self.has_data(bad))
        self.assertFalse(self.alive(good))


class NoCurrentPathMakesOne(OwnerlessBase):
    """The cause, as far as the code goes: every path that deletes a demo user in this repo takes
    the team first. A shell `.delete()` of the user is the one that does not (nothing in the code
    does it), which is why the cron now looks at teams too."""

    def ownerless_demo_teams(self):
        return TeamMaster.objects.filter(is_demo=True, owner__isnull=True, is_deleted=False)

    def test_delete_demo_environment(self):
        user, _team = self.seeded("own_env", hours_old=1)
        delete_demo_environment(user)
        self.assertFalse(self.ownerless_demo_teams().exists())

    def test_signout(self):
        user, team = self.seeded("own_signout", hours_old=1)
        client = APIClient()
        client.cookies["refresh"] = str(RefreshToken.for_user(user))
        res = client.post("/api/v2/user/signout/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(self.alive(team))
        self.assertFalse(self.ownerless_demo_teams().exists())

    def test_the_cron_for_a_user(self):
        self.seeded("own_cron")
        self.run_cleanup("--hours", "24")
        self.assertFalse(self.ownerless_demo_teams().exists())
        self.assertFalse(TeamMaster.objects.filter(is_demo=True).exists())

    def test_the_eval_fixture_reseed(self):
        from origin.search_engine.agent.evals import fixture

        with mock.patch.object(fixture, "call_command"):
            fixture.reseed_fixture()
            fixture.reseed_fixture()  # the second one tears the first down
        self.assertFalse(self.ownerless_demo_teams().exists())
        self.assertEqual(TeamMaster.objects.filter(is_demo=True).count(), 1)
