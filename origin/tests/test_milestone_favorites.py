"""MIP-1053: star a milestone, list the stars, and move one up when it is opened.

A favorite is private, but the sidebar prints the milestone's title and its
project's name, so the list answers only for milestones the caller may still
open: a project member's, not deleted (converting a milestone back into a task
retires it the same way).
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneFavorite, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase

URL = "/api/v2/milestone/favorite/"
LIST = "/api/v2/milestone/favorite/list/"
OPENED = "/api/v2/milestone/favorite/opened/"


class MilestoneFavoriteTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        # user2 owns a project user cannot see.
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user2
        )
        self.m1 = self._milestone(self.project, "Launch")
        self.m2 = self._milestone(self.project, "Beta")
        self.hidden = self._milestone(self.other_project, "Hidden plan")

    def _milestone(self, project, title):
        return MilestoneMaster.objects.create(
            team=self.team, project=project, reporter=self.user, title=title
        )

    def _star(self, milestone_id, user=None, **extra):
        self.authenticate(user or self.user)
        return self.client.post(URL, {"milestone_id": milestone_id, **extra}, format="json")

    def _unstar(self, milestone_id, user=None):
        self.authenticate(user or self.user)
        return self.client.delete(f"{URL}?milestone_id={milestone_id}")

    def _list(self, user=None, **params):
        self.authenticate(user or self.user)
        res = self.client.get(LIST, params)
        self.assertEqual(res.status_code, 200)
        return res.data["favorites"]

    def _titles(self, **params):
        return [f["title"] for f in self._list(**params)]

    # -- star / unstar ------------------------------------------------------

    def test_star_and_unstar(self):
        self.assertEqual(self._star(self.m1.milestone_id).status_code, 201)
        self.assertEqual(self._star(self.m1.milestone_id).status_code, 200)  # idempotent
        self.assertEqual(MilestoneFavorite.objects.count(), 1)
        self.assertEqual(self._titles(), ["Launch"])
        self.assertEqual(self._unstar(self.m1.milestone_id).status_code, 200)
        self.assertEqual(self._titles(), [])
        self.assertEqual(self._unstar(self.m1.milestone_id).status_code, 404)

    def test_the_list_describes_each_favorite(self):
        self._star(self.m1.milestone_id)
        (fav,) = self._list()
        self.assertEqual(fav["milestoneId"], self.m1.milestone_id)
        self.assertEqual(fav["title"], "Launch")
        self.assertEqual(fav["projectId"], self.project.project_id)
        self.assertEqual(fav["projectName"], "Genos")
        self.assertEqual(fav["status"], "Open")
        self.assertIsNone(fav["tsLastOpened"])

    def test_a_milestone_of_a_project_the_caller_is_not_in_is_not_found(self):
        self.assertEqual(self._star(self.hidden.milestone_id).status_code, 404)
        self.assertFalse(MilestoneFavorite.objects.exists())

    def test_a_missing_or_malformed_id_is_refused_without_leaking(self):
        self.assertEqual(self._star(999999).status_code, 404)
        self.assertEqual(self._star("abc").status_code, 400)
        self.authenticate()
        self.assertEqual(self.client.post(URL, {}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(URL).status_code, 400)

    def test_a_deleted_milestone_cannot_be_starred(self):
        MilestoneMaster.objects.filter(pk=self.m1.pk).update(is_deleted=True)
        self.assertEqual(self._star(self.m1.milestone_id).status_code, 404)

    def test_stars_are_private(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self._star(self.m1.milestone_id)
        self.assertEqual(self._titles(), ["Launch"])
        self.assertEqual(self._list(user=self.user2), [])
        # user2 cannot remove user's star either: it is not theirs.
        self.assertEqual(self._unstar(self.m1.milestone_id, user=self.user2).status_code, 404)
        self.assertEqual(MilestoneFavorite.objects.count(), 1)

    # -- what stays visible ---------------------------------------------------

    def test_a_deleted_or_converted_milestone_drops_out_and_comes_back_with_its_star(self):
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        MilestoneMaster.objects.filter(pk=self.m1.pk).update(is_deleted=True)
        self.assertEqual(self._titles(), ["Beta"])
        MilestoneMaster.objects.filter(pk=self.m1.pk).update(is_deleted=False)
        self.assertCountEqual(self._titles(), ["Launch", "Beta"])

    def test_leaving_the_project_hides_the_favorite_but_the_star_can_still_go(self):
        self._star(self.m1.milestone_id)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        # user still owns `project` (owner counts as a member), so move the
        # ownership away to model a person who really left.
        ProjectMaster.objects.filter(pk=self.project.pk).update(owner=self.user2)
        self.assertEqual(self._list(), [])
        self.assertEqual(self._unstar(self.m1.milestone_id).status_code, 200)
        self.assertFalse(MilestoneFavorite.objects.exists())

    def test_a_deleted_project_hides_its_milestones(self):
        self._star(self.m1.milestone_id)
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assertEqual(self._list(), [])

    # -- order ------------------------------------------------------------------

    def test_ordered_by_last_opened_then_by_when_starred(self):
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        # Starred later, never opened: first.
        self.assertEqual(self._titles(), ["Beta", "Launch"])
        # Opening Launch moves it to the top.
        self.authenticate()
        res = self.client.post(OPENED, {"milestone_id": self.m1.milestone_id}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["isFavorited"])
        self.assertEqual(self._titles(), ["Launch", "Beta"])
        # A later open of Beta moves it back.
        self.client.post(OPENED, {"milestone_id": self.m2.milestone_id}, format="json")
        self.assertEqual(self._titles(), ["Beta", "Launch"])

    # -- order by progress (MIP-1352) ---------------------------------------------

    def _with_progress(self, milestone, when):
        """Give the milestone its backing task, and a `last_progress_at` of `when`."""
        task = ensure_backing_task(milestone)
        TaskMaster.objects.filter(pk=task.pk).update(last_progress_at=when)
        return task

    def test_the_most_recent_progress_comes_first(self):
        now = timezone.now()
        self._with_progress(self.m1, now - timedelta(days=5))
        self._with_progress(self.m2, now - timedelta(days=1))
        self._star(self.m2.milestone_id)
        self._star(self.m1.milestone_id)  # starred LAST, but its progress is older
        self.assertEqual(self._titles(), ["Beta", "Launch"])

    def test_opening_a_milestone_does_not_move_it_above_a_newer_progress(self):
        now = timezone.now()
        self._with_progress(self.m1, now - timedelta(days=5))
        self._with_progress(self.m2, now - timedelta(days=1))
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        self.authenticate()
        self.client.post(OPENED, {"milestone_id": self.m1.milestone_id}, format="json")
        self.assertEqual(self._titles(), ["Beta", "Launch"])

    def test_a_child_changing_moves_its_milestone_up(self):
        now = timezone.now()
        self._with_progress(self.m1, now - timedelta(days=5))
        self._with_progress(self.m2, now - timedelta(days=1))
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        self.assertEqual(self._titles(), ["Beta", "Launch"])
        with self.captureOnCommitCallbacks(execute=True):
            child = TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                title="Do it",
                status="Open",
                parent_task_id=self.m1.task_id,
                milestone=self.m1,
            )
            child.status = "WIP"
            child.save()
        self.assertEqual(self._titles(), ["Launch", "Beta"])

    def test_no_recorded_progress_goes_after_the_milestones_that_have_some(self):
        now = timezone.now()
        self._with_progress(self.m1, now - timedelta(days=30))  # old, but recorded
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)  # no backing task: nothing recorded
        self.assertEqual(self._titles(), ["Launch", "Beta"])

    def test_milestones_with_the_same_progress_keep_the_opened_order(self):
        when = timezone.now() - timedelta(days=2)
        self._with_progress(self.m1, when)
        self._with_progress(self.m2, when)
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        self.assertEqual(self._titles(), ["Beta", "Launch"])
        self.authenticate()
        self.client.post(OPENED, {"milestone_id": self.m1.milestone_id}, format="json")
        self.assertEqual(self._titles(), ["Launch", "Beta"])

    def test_the_list_carries_each_milestones_last_progress(self):
        when = timezone.now() - timedelta(days=2)
        task = self._with_progress(self.m1, when)
        self._star(self.m1.milestone_id)
        self._star(self.m2.milestone_id)
        rows = {f["title"]: f for f in self._list()}
        self.assertEqual(
            rows["Launch"]["lastProgressAt"],
            str(TaskMaster.objects.get(pk=task.pk).last_progress_at),
        )
        self.assertIsNone(rows["Beta"]["lastProgressAt"])

    def test_an_old_open_does_not_outrank_a_newer_star(self):
        self._star(self.m1.milestone_id)
        MilestoneFavorite.objects.filter(milestone=self.m1).update(
            ts_last_opened_at=timezone.now() - timedelta(days=3)
        )
        self._star(self.m2.milestone_id)
        self.assertEqual(self._titles(), ["Beta", "Launch"])

    def test_opening_a_milestone_that_is_not_a_favorite_changes_nothing(self):
        self.authenticate()
        res = self.client.post(OPENED, {"milestone_id": self.m1.milestone_id}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["isFavorited"])
        self.assertFalse(MilestoneFavorite.objects.exists())

    def test_opened_needs_a_milestone_id(self):
        self.authenticate()
        self.assertEqual(self.client.post(OPENED, {}, format="json").status_code, 400)

    # -- team scope ---------------------------------------------------------------

    def test_a_team_id_narrows_the_list_to_the_team_it_was_starred_in(self):
        self._star(self.m1.milestone_id, team_id=self.team.team_id)
        self.assertEqual(self._titles(team_id=self.team.team_id), ["Launch"])
        self.assertEqual(self._titles(team_id="00000000-0000-0000-0000-000000000000"), [])

    def test_anonymous_callers_are_refused(self):
        self.unauthenticate()
        self.assertEqual(self.client.get(LIST).status_code, 401)
        self.assertEqual(
            self.client.post(
                URL, {"milestone_id": self.m1.milestone_id}, format="json"
            ).status_code,
            401,
        )
