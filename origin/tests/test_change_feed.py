"""Change feed: the version store, the post-commit bumps and the cache clears (MIP-970).

Django's `TestCase` never runs `on_commit` callbacks, so every token assertion
runs under `captureOnCommitCallbacks(execute=True)`. Nothing here "fixes" a
failing test by bumping synchronously: a bump that is visible before the commit
is the bug these tests exist to catch.

Each write path gets its own test asserting the EXACT set of keys that moved, so a
receiver that over-reaches fails as loudly as one that is missing.
"""

import threading
from unittest import mock

from django.core.cache import cache
from django.db import transaction

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.change_models import ChangeVersion
from origin.models.common.team_models import ExternalGrant, TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
    NoteFolderTagLink,
    NotePermissionMaster,
)
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.models.task.task_models import TaskMaster
from origin.services import change_feed as cf
from origin.services.milestone_service import ensure_backing_task
from origin.services.project_delete import delete_project
from origin.services.task_cache import get_cached_project_tasks, set_cached_project_tasks
from origin.tests.cross_team_fixtures import CrossTeamTestCase


class ChangeFeedCase(CrossTeamTestCase):
    """Teams A (host, `self.team`), B (guest), C (stranger); a second project in A."""

    def setUp(self):
        super().setUp()
        self.project2 = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Second Project",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        for project in (self.project, self.project2):
            ProjectMembers.objects.get_or_create(
                team=self.team_a, project=project, attendee=self.a_owner
            )
        cache.clear()
        # Fixture writes queued keys that never flush (no commit in a TestCase).
        # Forget them, or the first flush of a test would report them as changes.
        cf.reset_pending()
        self.addCleanup(cf.reset_pending)

    # -- helpers ---------------------------------------------------------

    def versions(self):
        return dict(ChangeVersion.objects.values_list("key", "version"))

    def changed(self, fn):
        """The keys whose version moved while `fn` ran and its commit callbacks fired."""
        before = self.versions()
        with self.captureOnCommitCallbacks(execute=True):
            fn()
        after = self.versions()
        return {key for key, version in after.items() if before.get(key, 0) != version}

    def assertChanged(self, fn, expected):
        self.assertEqual(self.changed(fn), set(expected))

    # key names, for readability
    def tp(self, project):
        return cf.tasks_project_key(project.project_id)

    def tt(self, team):
        return cf.tasks_team_key(team.team_id)

    def pr(self, team):
        return cf.projects_key(team.team_id)

    def np(self, team):
        return cf.notes_personal_key(team.team_id)

    def nt(self, team):
        return cf.notes_task_key(team.team_id)

    def nc(self, team):
        return cf.notes_chat_key(team.team_id)

    def all_team_keys(self, team):
        return {self.tt(team), self.pr(team), self.np(team), self.nt(team), self.nc(team)}

    def make_task(self, project=None, **extra):
        project = project or self.project
        fields = dict(
            team=project.team,
            project=project,
            reporter=self.a_owner,
            assignee=self.a_owner,
            title="A task",
            status="Open",
        )
        fields.update(extra)
        return TaskMaster.objects.create(**fields)


# ---------------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------------


class ChangeFeedStoreTests(ChangeFeedCase):
    def test_keys_normalise_ids(self):
        team = self.team_a.team_id
        # A project id is an int whatever it arrived as ...
        self.assertEqual(cf.tasks_project_key(7), "tasks:p:7")
        self.assertEqual(cf.tasks_project_key("7"), "tasks:p:7")
        # ... and a team id (a UUID, not an int) is the canonical lower-case form.
        canonical = f"projects:t:{team}"
        self.assertEqual(cf.projects_key(team), canonical)
        self.assertEqual(cf.projects_key(str(team)), canonical)
        self.assertEqual(cf.projects_key(str(team).upper()), canonical)
        self.assertEqual(cf.projects_key(team.hex), canonical)
        # None and garbage name nothing.
        for bad in (None, "", "abc", True):
            self.assertIsNone(cf.tasks_project_key(bad))
        for bad in (None, "", "abc", 7):
            self.assertIsNone(cf.projects_key(bad))

    def test_str_and_int_ids_bump_the_same_key(self):
        a = self.changed(
            lambda: cf.bump_tasks(str(self.team_a.team_id), str(self.project.project_id))
        )
        b = self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        self.assertEqual(a, b)
        self.assertEqual(self.versions()[self.tp(self.project)], 2)
        # Upper-case team ids land on the same row too: still one row per key.
        self.changed(
            lambda: cf.bump_tasks(str(self.team_a.team_id).upper(), self.project.project_id)
        )
        self.assertEqual(self.versions()[self.tp(self.project)], 3)
        self.assertEqual(ChangeVersion.objects.filter(key=self.tt(self.team_a)).count(), 1)

    def test_bumps_nothing_for_unusable_ids(self):
        self.assertChanged(lambda: cf.bump_tasks(None, None), set())
        self.assertChanged(lambda: cf.bump_projects(None), set())
        self.assertChanged(
            lambda: cf.bump_notes("nope", personal=True, task=True, chat=True), set()
        )

    def test_nothing_is_visible_before_the_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            cf.bump_tasks(self.team_a.team_id, self.project.project_id)
            self.assertEqual(self.versions(), {})
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.versions(), {})  # still nothing: the commit hasn't happened

    def test_a_bump_outside_any_transaction_applies_at_once(self):
        # `on_commit` outside an atomic block runs immediately. TestCase always
        # sits in one, so reproduce the autocommit path by calling the hook the
        # way Django would.
        with mock.patch.object(transaction, "on_commit", side_effect=lambda fn, **kw: fn()):
            cf.bump_tasks(self.team_a.team_id, self.project.project_id)
        self.assertEqual(self.versions()[self.tp(self.project)], 1)

    def test_rolled_back_transaction_bumps_nothing(self):
        before = self.versions()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    cf.bump_tasks(self.team_a.team_id, self.project.project_id)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual(self.versions(), before)

    def test_a_later_bump_still_works_after_a_rolled_back_one(self):
        # The trap: the rolled-back bump leaves its key in the thread's pending
        # set and its callback discarded. A bump that registers a callback only
        # "when the set is empty" would then never flush anything on this thread.
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    cf.bump_tasks(self.team_a.team_id, self.project.project_id)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertTrue(cf.pending_keys(), "the rolled-back keys are still queued on this thread")
        changed = self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project2.project_id))
        self.assertIn(self.tp(self.project2), changed)
        # The spurious bump of the rolled-back key is accepted: an extra refetch.
        self.assertEqual(cf.pending_keys(), frozenset())

    def test_one_bulk_plan_bumps_each_key_once(self):
        def plan():
            with transaction.atomic():
                for n in range(5):
                    self.make_task(title=f"task {n}")

        before = self.versions().get(self.tp(self.project), 0)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            plan()
        self.assertGreaterEqual(len(callbacks), 5)  # every bump registers its own flush
        self.assertEqual(self.versions()[self.tp(self.project)], before + 1)
        self.assertEqual(self.versions()[self.tt(self.team_a)], before + 1)

    def test_bumping_twice_in_two_commits_counts_two(self):
        self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        self.assertEqual(self.versions()[self.tp(self.project)], 2)

    def test_pending_set_is_per_thread(self):
        seen = {}

        def other_thread():
            seen["before"] = cf.pending_keys()
            cf._pending.keys.add("tasks:p:999999")
            seen["after"] = cf.pending_keys()

        cf._pending.keys.add("tasks:p:1")
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
        self.assertEqual(seen["before"], frozenset())
        self.assertEqual(seen["after"], frozenset({"tasks:p:999999"}))
        self.assertEqual(cf.pending_keys(), frozenset({"tasks:p:1"}))

    def test_a_failing_bump_never_fails_the_write(self):
        with mock.patch.object(cf, "_upsert", side_effect=RuntimeError("db down")):
            with self.assertLogs("origin.services.change_feed", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    task = self.make_task()
        self.assertTrue(TaskMaster.objects.filter(pk=task.pk).exists())

    def test_a_failing_cache_clear_still_bumps(self):
        with mock.patch.object(cf, "_clear_task_caches", side_effect=RuntimeError("redis down")):
            with self.assertLogs("origin.services.change_feed", level="ERROR"):
                changed = self.changed(
                    lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id)
                )
        self.assertEqual(changed, {self.tp(self.project), self.tt(self.team_a)})

    def test_token_changes_iff_a_key_moved(self):
        keys = [self.tp(self.project), self.tt(self.team_a)]
        read = lambda: cf.token_for(keys, cf.read_versions(keys))  # noqa: E731
        self.assertEqual(read(), "0")
        self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        first = read()
        self.assertNotEqual(first, "0")
        self.assertEqual(read(), first)  # stable while nothing moves
        self.changed(lambda: cf.bump(self.tt(self.team_a)))
        self.assertNotEqual(read(), first)
        # An unrelated key does not move it.
        settled = read()
        self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project2.project_id))
        self.assertNotEqual(read(), settled)  # tasks:t:A is shared, so THIS one moves
        settled = read()
        self.changed(lambda: cf.bump_projects(self.team_a.team_id))
        self.assertEqual(read(), settled)


# ---------------------------------------------------------------------------
# Caches behind a token
# ---------------------------------------------------------------------------


class ChangeFeedCacheTests(ChangeFeedCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.a_owner)

    def project_tasks(self, project=None):
        project = project or self.project
        res = self.client.get(
            "/api/v2/task/getProjectTasks/",
            {"team_id": str(self.team_a.team_id), "project_id": project.project_id},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]["tasks"]

    def project_list(self):
        res = self.client.get(
            "/api/v2/project/projects/",
            {"team_id": str(self.team_a.team_id), "attendee_id": str(self.a_owner.id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def test_project_tasks_cache_is_not_served_stale_after_the_flush(self):
        self.assertEqual(self.project_tasks(), [])  # caches the empty answer
        self.assertIsNotNone(get_cached_project_tasks(self.team_a.team_id, self.project.project_id))

        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                self.make_task(title="written, not yet committed")
                # The write's own signal already deleted the cache. A reader that
                # started before the commit now re-caches the OLD answer ...
                set_cached_project_tasks(self.team_a.team_id, self.project.project_id, [])
            # ... and the commit happens here, when the callbacks run.

        # The flush deleted that stale entry BEFORE it bumped the version, so a
        # client that sees the new token and refetches gets the new row.
        self.assertEqual([t["title"] for t in self.project_tasks()], ["written, not yet committed"])

    def test_project_list_cache_is_not_served_stale_after_the_flush(self):
        self.assertEqual(len(self.project_list()), 2)  # caches the list
        key = f"project:list:{self.team_a.team_id}:{self.a_owner.id}"
        self.assertIsNotNone(cache.get(key))

        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                ProjectMaster.objects.create(
                    team=self.team_a,
                    project_name="Third",
                    owner=self.a_owner,
                    project_system_user=self.a_owner,
                )
                cache.set(key, [{"stale": True}], timeout=60)  # a racing reader's re-cache

        names = sorted(p.get("projectName") for p in self.project_list())
        self.assertEqual(names, ["Host Project", "Second Project", "Third"])

    def test_flush_clears_the_project_list_of_a_guest_team(self):
        # A project shared with team B shows in B members' cached lists.
        self.active_project_grant()
        # The grant's own bumps (which also name team B) are not what is under
        # test: only the host's bump may be what reaches the guest's list.
        cf.reset_pending()
        guest_key = f"project:list:{self.team_b.team_id}:{self.b_owner.id}"
        cache.set(guest_key, [{"stale": True}], timeout=60)
        self.changed(lambda: cf.bump_projects(self.team_a.team_id))
        self.assertIsNone(cache.get(guest_key))

    def test_flush_clears_the_cache_of_a_project_under_a_different_spelling(self):
        # The view caches under whatever team id the request spelled.
        upper = str(self.team_a.team_id).upper()
        set_cached_project_tasks(upper, str(self.project.project_id), [1])
        self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        self.assertIsNone(get_cached_project_tasks(upper, str(self.project.project_id)))
        self.assertIsNone(get_cached_project_tasks(self.team_a.team_id, self.project.project_id))

    def test_the_cache_is_cleared_before_the_version_moves(self):
        order = []
        real_upsert = cf._upsert
        real_clear = cf._clear_task_caches

        def clear(pairs):
            order.append("clear")
            return real_clear(pairs)

        def upsert(keys):
            order.append("upsert")
            return real_upsert(keys)

        with (
            mock.patch.object(cf, "_clear_task_caches", clear),
            mock.patch.object(cf, "_upsert", upsert),
        ):
            self.changed(lambda: cf.bump_tasks(self.team_a.team_id, self.project.project_id))
        self.assertEqual(order, ["clear", "upsert"])


# ---------------------------------------------------------------------------
# Tasks, sprints, milestones
# ---------------------------------------------------------------------------


class ChangeFeedTaskTests(ChangeFeedCase):
    def test_task_create_update_delete(self):
        p, t = self.tp(self.project), self.tt(self.team_a)
        holder = {}
        self.assertChanged(lambda: holder.update(task=self.make_task()), {p, t})

        def rename():
            holder["task"].title = "renamed"
            holder["task"].save()

        self.assertChanged(rename, {p, t})
        self.assertChanged(lambda: holder["task"].delete(), {p, t})

    def test_unrelated_projects_and_teams_do_not_move(self):
        before = self.versions()
        self.changed(lambda: self.make_task())
        after = self.versions()
        for key in (self.tp(self.project2), self.tp(self.foreign_project), self.tt(self.team_b)):
            self.assertEqual(after.get(key, 0), before.get(key, 0), key)

    def test_a_save_that_moves_a_task_bumps_both_projects(self):
        task = self.make_task(self.project)
        cf.reset_pending()  # the create above is not under test

        def move():
            task.project = self.project2
            task.save()

        self.assertChanged(
            move, {self.tp(self.project), self.tp(self.project2), self.tt(self.team_a)}
        )

    def test_a_save_that_moves_a_task_to_another_team_bumps_both_teams(self):
        task = self.make_task(self.project)
        cf.reset_pending()  # the create above is not under test

        def move():
            task.project = self.foreign_project
            task.team = self.team_c
            task.save()

        self.assertChanged(
            move,
            {
                self.tp(self.project),
                self.tp(self.foreign_project),
                self.tt(self.team_a),
                self.tt(self.team_c),
            },
        )

    def test_a_draft_task_is_not_announced(self):
        # The create form edits a REAL row flagged `is_init_task` and autosaves it.
        holder = {}
        self.assertChanged(lambda: holder.update(draft=self.make_task(is_init_task=True)), set())

        def edit():
            holder["draft"].title = "typing"
            holder["draft"].save()

        self.assertChanged(edit, set())
        self.assertChanged(lambda: holder["draft"].delete(), set())

    def test_finalising_a_draft_is_announced(self):
        draft = self.make_task(is_init_task=True)
        cf.reset_pending()

        def finalise():
            draft.is_init_task = False
            draft.save()

        self.assertChanged(finalise, {self.tp(self.project), self.tt(self.team_a)})

    def test_a_cascade_update_bumps_every_affected_project(self):
        from origin.signals.task_signals import update_tasks_recorded

        a = self.make_task(self.project)
        b = self.make_task(self.project2)
        cf.reset_pending()

        # Moves both rows into project 2: the two projects they were in AND the one
        # they went to, with no `post_save` anywhere.
        self.assertChanged(
            lambda: update_tasks_recorded(
                TaskMaster.objects.filter(pk__in=[a.pk, b.pk]),
                cascade="test",
                project_id=self.foreign_project.project_id,
                team_id=self.team_c.team_id,
                project_task_number=None,
            ),
            {
                self.tp(self.project),
                self.tp(self.project2),
                self.tp(self.foreign_project),
                self.tt(self.team_a),
                self.tt(self.team_c),
            },
        )

    def test_a_cascade_that_changes_no_location_bumps_just_where_the_rows_are(self):
        from origin.signals.task_signals import update_tasks_recorded

        a = self.make_task(self.project)
        cf.reset_pending()
        self.assertChanged(
            lambda: update_tasks_recorded(
                TaskMaster.objects.filter(pk=a.pk), cascade="test", sprint_id=None
            ),
            {self.tp(self.project), self.tt(self.team_a)},
        )

    def test_a_cascade_over_no_rows_bumps_nothing(self):
        from origin.signals.task_signals import update_tasks_recorded

        self.assertChanged(
            lambda: update_tasks_recorded(
                TaskMaster.objects.filter(pk=-1), cascade="test", sprint_id=None
            ),
            set(),
        )

    def test_moving_tasks_between_projects_through_the_service_bumps_both(self):
        from origin.services.task_project_move import relocate_tasks

        task = self.make_task(self.project)
        cf.reset_pending()
        self.assertChanged(
            lambda: relocate_tasks(
                [task.task_id], project_id=self.project2.project_id, team_id=self.team_a.team_id
            ),
            {
                self.tp(self.project),
                self.tp(self.project2),
                self.tt(self.team_a),
                # The satellites move with the task: its task notes are re-pointed.
                self.nt(self.team_a),
            },
        )

    def test_relocating_task_notes_bumps_the_task_notes_of_both_teams(self):
        from origin.services.task_project_move import relocate_task_satellites

        task = self.make_task(self.project)
        TaskNoteMaster.objects.create(
            team=self.team_a,
            project=self.project,
            task=task,
            owner=self.a_owner,
            title="n",
            body=[],
        )
        cf.reset_pending()
        self.assertChanged(
            lambda: relocate_task_satellites(
                [task.task_id],
                project_id=self.foreign_project.project_id,
                team_id=self.team_c.team_id,
            ),
            {self.nt(self.team_a), self.nt(self.team_c)},
        )

    def test_sprint_config_and_milestone_writes_bump_the_project_tasks(self):
        keys = {self.tp(self.project), self.tt(self.team_a)}
        holder = {}

        def make_sprint():
            holder["sprint"] = Sprint.objects.create(
                team=self.team_a,
                project=self.project,
                name="S1",
                sequence_number=1,
                start_date="2026-01-01",
                end_date="2026-01-14",
            )

        self.assertChanged(make_sprint, keys)

        def rename():
            holder["sprint"].name = "S1b"
            holder["sprint"].save()

        self.assertChanged(rename, keys)
        self.assertChanged(
            lambda: SprintConfig.objects.create(
                team=self.team_a, project=self.project, anchor_date="2026-01-01"
            ),
            keys,
        )
        self.assertChanged(lambda: holder["sprint"].delete(), keys)

        def make_milestone():
            holder["milestone"] = MilestoneMaster.objects.create(
                team=self.team_a, project=self.project, reporter=self.a_owner, title="M"
            )

        # Creating a milestone also writes its backing task through `.create()`.
        self.assertChanged(make_milestone, keys)
        self.assertChanged(lambda: ensure_backing_task(holder["milestone"]), keys)

        def retitle():
            holder["milestone"].title = "M2"
            holder["milestone"].save()

        self.assertChanged(retitle, keys)

    def test_deleting_a_sprint_or_a_milestone_moves_the_project_tasks(self):
        # Both soft-delete through queryset `.update()` calls (the milestones and
        # the backing task leave the sprint / the table) that no receiver hears.
        from origin.services import milestone_manage
        from origin.services.milestone_manage import delete_milestone
        from origin.services.sprint_manage import delete_sprint

        keys = {self.tp(self.project), self.tt(self.team_a)}
        sprint = Sprint.objects.create(
            team=self.team_a,
            project=self.project,
            name="S",
            sequence_number=1,
            start_date="2026-01-01",
            end_date="2026-01-14",
        )
        milestone = MilestoneMaster.objects.create(
            team=self.team_a, project=self.project, reporter=self.a_owner, title="M", sprint=sprint
        )
        ensure_backing_task(milestone)
        cf.reset_pending()
        self.assertChanged(lambda: delete_sprint(sprint), keys)
        with mock.patch.object(milestone_manage, "purge_milestone"):
            self.assertChanged(lambda: delete_milestone(milestone), keys)

    def test_sprint_writes_in_another_project_do_not_move_this_one(self):
        before = self.versions()
        self.changed(
            lambda: Sprint.objects.create(
                team=self.team_a,
                project=self.project2,
                name="S",
                sequence_number=1,
                start_date="2026-01-01",
                end_date="2026-01-14",
            )
        )
        self.assertEqual(
            self.versions().get(self.tp(self.project), 0), before.get(self.tp(self.project), 0)
        )

    def test_listing_sprints_settles_after_the_first_load(self):
        # `GET /sprint/list/` writes (a default config, upcoming sprints). The
        # loader it feeds is driven by the very token that write moves, so a
        # second identical load must NOT move it again or the client would
        # refetch forever.
        self.authenticate(self.a_owner)

        def load():
            res = self.client.get("/api/v2/sprint/list/", {"project_id": self.project.project_id})
            self.assertEqual(res.status_code, 200, res.data)

        first = self.changed(load)
        self.assertIn(self.tp(self.project), first)
        self.assertEqual(self.changed(load), set())
        self.assertEqual(self.changed(load), set())


# ---------------------------------------------------------------------------
# Projects and membership
# ---------------------------------------------------------------------------


class ChangeFeedProjectTests(ChangeFeedCase):
    def project_keys(self, team, project):
        return {
            self.pr(team),
            self.tp(project),
            self.tt(team),
            self.nt(team),
            self.nc(team),
        }

    def test_project_writes_move_the_list_the_tasks_and_the_note_names(self):
        holder = {}

        def create():
            holder["project"] = ProjectMaster.objects.create(
                team=self.team_a,
                project_name="New",
                owner=self.a_owner,
                project_system_user=self.a_owner,
            )

        changed = self.changed(create)
        project = holder["project"]
        self.assertEqual(changed, self.project_keys(self.team_a, project))
        self.assertNotIn(self.np(self.team_a), changed)  # personal notes carry no project data

        def rename():
            project.project_name = "Renamed"
            project.save()

        self.assertChanged(rename, self.project_keys(self.team_a, project))
        # Through the real delete: it detaches the PM channel first, then cascades.
        self.assertChanged(lambda: delete_project(project), self.project_keys(self.team_a, project))

    def test_project_in_another_team_does_not_move_this_team(self):
        before = self.versions()
        self.changed(
            lambda: ProjectMaster.objects.create(
                team=self.team_c,
                project_name="C2",
                owner=self.c_owner,
                project_system_user=self.c_owner,
            )
        )
        moved = {k for k, v in self.versions().items() if before.get(k, 0) != v}
        self.assertFalse({k for k in moved if str(self.team_a.team_id) in k}, moved)

    def test_membership_moves_the_list_the_task_notes_and_the_team_tasks(self):
        holder = {}
        expected = {
            self.pr(self.team_a),
            self.nt(self.team_a),
            self.tt(self.team_a),
            self.tp(self.project2),
            # Joining the project also joins its PM chat channel.
            self.nc(self.team_a),
        }
        self.assertChanged(
            lambda: holder.update(
                member=ProjectMembers.objects.create(
                    team=self.team_a, project=self.project2, attendee=self.a_viewer
                )
            ),
            expected,
        )
        self.assertChanged(lambda: holder["member"].delete(), expected)

    def test_removing_a_project_member_also_moves_the_chat_notes(self):
        # The PM channel membership is soft-deleted with a queryset `.update()`
        # that no receiver sees: this is the explicit call in pm_channel_signals.
        member = ProjectMembers.objects.create(
            team=self.team_a, project=self.project2, attendee=self.a_viewer
        )
        cf.reset_pending()
        self.assertIn(self.nc(self.team_a), self.changed(lambda: member.delete()))
        # Without the explicit call the only other chat-notes bump was the
        # receiver on `ChannelMember` (never fired by `.update`); pin the .update:
        self.assertTrue(
            ChannelMember.objects.filter(
                channel__project=self.project2, user=self.a_viewer, is_deleted=True
            ).exists()
        )

    def test_project_tags_and_labels_move_the_project_list_only(self):
        holder = {}
        only = {self.pr(self.team_a)}
        self.assertChanged(
            lambda: holder.update(
                tag=ProjectTags.objects.create(
                    team=self.team_a, project=self.project, tag_id=1, tag_name="bug"
                )
            ),
            only,
        )
        self.assertChanged(lambda: holder["tag"].delete(), only)
        self.assertChanged(
            lambda: holder.update(
                label=ProjectLabel.objects.create(team=self.team_a, name="Client")
            ),
            only,
        )
        self.assertChanged(lambda: holder["label"].delete(), only)

    def test_assigning_labels_through_bulk_create_moves_the_project_list(self):
        from origin.services.project_catalog import assign_labels, unassign_labels

        label = ProjectLabel.objects.create(team=self.team_a, name="Q3")
        cf.reset_pending()
        only = {self.pr(self.team_a)}
        # `bulk_create` fires no receiver: the bump rides on `invalidate_label_project_list`.
        self.assertChanged(lambda: assign_labels(self.project, [label.label_id]), only)
        self.assertChanged(lambda: unassign_labels(self.project, [label.label_id]), only)

    def test_a_whole_starter_workspace_is_one_bump_per_key(self):
        # `create_starter_workspace` writes dozens of rows in one atomic block (and
        # its project tags through `bulk_create`): every key it touches moves ONCE.
        from origin.services.starter_seeder import create_starter_workspace

        team = TeamMaster.objects.create(
            team_name="Fresh", team_email="fresh@example.com", owner=self.a_owner
        )
        cf.reset_pending()
        before = self.versions()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            create_starter_workspace(self.a_owner, team)
        after = self.versions()
        self.assertGreater(len(callbacks), 20)
        moved = {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}
        self.assertTrue(self.all_team_keys(team) <= set(moved), moved)
        self.assertEqual(set(moved.values()), {1})
        # Nothing outside the new team moved.
        self.assertTrue(
            all(str(team.team_id) in k or k.startswith("tasks:p:") for k in moved), moved
        )

    def test_team_membership_moves_every_team_collection(self):
        holder = {}
        new_user = self._user("newbie")
        self.assertChanged(
            lambda: holder.update(
                member=TeamMembers.objects.create(team=self.team_a, attendee=new_user)
            ),
            self.all_team_keys(self.team_a),
        )

        def promote():
            holder["member"].member_role = "editor"
            holder["member"].save()

        self.assertChanged(promote, self.all_team_keys(self.team_a))
        self.assertChanged(lambda: holder["member"].delete(), self.all_team_keys(self.team_a))

    def test_leaving_a_team_through_the_service_moves_the_chat_notes(self):
        from origin.services.team_membership import drop_in_team_access

        # `drop_in_team_access` soft-deletes channel memberships with a queryset
        # `.update()`; it is also reachable without the member row being saved.
        channel = Channel.objects.create(team=self.team_a, kind=ChannelKind.GM, title="room")
        ChannelMember.objects.create(channel=channel, user=self.a_viewer)
        cf.reset_pending()
        changed = self.changed(lambda: drop_in_team_access(self.team_a.team_id, self.a_viewer.id))
        self.assertEqual(changed, self.all_team_keys(self.team_a))

    def test_external_grants_move_both_teams_and_not_the_stranger(self):
        holder = {}
        expected = self.all_team_keys(self.team_a) | self.all_team_keys(self.team_b)
        changed = self.changed(lambda: holder.update(grant=self.active_project_grant()))
        self.assertTrue(expected <= changed, expected - changed)
        self.assertFalse(changed & self.all_team_keys(self.team_c))

        self.assertChanged(lambda: holder["grant"].save(), expected)
        self.assertChanged(
            lambda: ExternalGrant.objects.filter(pk=holder["grant"].pk).first().delete(), expected
        )

    def test_revoking_a_channel_grant_participant_moves_the_hosts_chat_notes(self):
        from origin.services.external_grants import _remove_participant

        channel, grant = self.shared_chat()
        ChannelMember.objects.update_or_create(
            channel=channel, user=self.b_owner, defaults={"is_deleted": False}
        )
        cf.reset_pending()
        changed = self.changed(lambda: _remove_participant(grant, self.b_owner.id))
        self.assertEqual(changed, {self.nc(self.team_a)})

    def test_account_deletion_queryset_updates_are_announced(self):
        from origin.services.account_deletion import delete_account

        keep = self._user("deleteme")
        TeamMembers.objects.create(team=self.team_a, attendee=keep)
        cf.reset_pending()
        changed = self.changed(lambda: delete_account(keep))
        self.assertTrue(self.all_team_keys(self.team_a) <= changed)

    def test_ownership_transfer_role_change_is_announced(self):
        from origin.views.common.ownership_claim_views import _transfer

        changed = self.changed(
            lambda: _transfer(
                self.team_a,
                self.a_viewer.id,
                previous_owner_id=self.a_owner.id,
                via="test",
            )
        )
        self.assertTrue(self.all_team_keys(self.team_a) <= changed)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class ChangeFeedNoteTests(ChangeFeedCase):
    def make_chat(self):
        channel = Channel.objects.create(team=self.team_a, kind=ChannelKind.GM, title="room")
        ChannelMember.objects.create(channel=channel, user=self.a_owner)
        return channel

    def test_personal_notes_and_folders(self):
        only = {self.np(self.team_a)}
        holder = {}
        self.assertChanged(
            lambda: holder.update(
                folder=PersonalNoteFolder.objects.create(
                    team=self.team_a, owner=self.a_owner, name="F"
                )
            ),
            only,
        )
        self.assertChanged(
            lambda: holder.update(
                note=PersonalNoteMaster.objects.create(
                    team=self.team_a, owner=self.a_owner, title="n", body=[]
                )
            ),
            only,
        )

        def retitle():
            holder["note"].title = "m"
            holder["note"].save()

        self.assertChanged(retitle, only)
        self.assertChanged(lambda: holder["note"].delete(), only)
        self.assertChanged(lambda: holder["folder"].delete(), only)

    def test_team_folder_permission_and_tag_writes(self):
        only = {self.np(self.team_a)}
        holder = {}
        self.assertChanged(
            lambda: holder.update(
                perm=NoteFolderPermission.objects.create(
                    team=self.team_a, folder=self.folder, user=self.a_viewer, role_id=3
                )
            ),
            only,
        )
        self.assertChanged(lambda: holder["perm"].delete(), only)
        self.assertChanged(
            lambda: holder.update(tag=NoteFolderTag.objects.create(team=self.team_a, name="t")),
            only,
        )
        self.assertChanged(lambda: holder["tag"].delete(), only)

    def test_folder_tag_links_through_bulk_create_are_announced(self):
        from origin.views.note.note_folder_tag_views import sync_folder_tags

        tag = NoteFolderTag.objects.create(team=self.team_a, name="t")
        cf.reset_pending()
        only = {self.np(self.team_a)}
        self.assertChanged(
            lambda: sync_folder_tags(self.folder, self.team_a.team_id, [tag.tag_id]), only
        )
        self.assertEqual(NoteFolderTagLink.objects.filter(folder=self.folder).count(), 1)
        self.assertChanged(lambda: sync_folder_tags(self.folder, self.team_a.team_id, []), only)
        self.assertEqual(NoteFolderTagLink.objects.filter(folder=self.folder).count(), 0)

    def test_folder_delete_cascade_moves_personal_notes(self):
        folder = PersonalNoteFolder.objects.create(team=self.team_a, owner=self.a_owner, name="F")
        NoteFolderPermission.objects.create(
            team=self.team_a, folder=folder, user=self.a_viewer, role_id=3
        )
        NoteFolderTag.objects.create(team=self.team_a, name="t")
        tag = NoteFolderTag.objects.get(team=self.team_a, name="t")
        NoteFolderTagLink.objects.create(folder=folder, tag=tag)
        cf.reset_pending()
        self.assertChanged(lambda: folder.delete(), {self.np(self.team_a)})
        self.assertFalse(NoteFolderPermission.objects.filter(folder_id=folder.folder_id).exists())

    def test_task_notes(self):
        task = self.make_task()
        cf.reset_pending()
        only = {self.nt(self.team_a)}
        holder = {}
        self.assertChanged(
            lambda: holder.update(
                note=TaskNoteMaster.objects.create(
                    team=self.team_a,
                    project=self.project,
                    task=task,
                    owner=self.a_owner,
                    title="n",
                    body=[],
                )
            ),
            only,
        )
        self.assertChanged(lambda: holder["note"].save(), only)
        self.assertChanged(lambda: holder["note"].delete(), only)

    def test_channel_writes(self):
        only = {self.nc(self.team_a)}
        holder = {}
        self.assertChanged(lambda: holder.update(channel=self.make_chat()), only)
        channel = holder["channel"]

        def retitle():
            channel.title = "renamed"
            channel.save()

        self.assertChanged(retitle, only)

    def test_chat_note_writes(self):
        only = {self.nc(self.team_a)}
        channel = self.make_chat()
        cf.reset_pending()
        holder = {}
        self.assertChanged(
            lambda: holder.update(
                note=ChatNoteMaster.objects.create(
                    team=self.team_a,
                    owner=self.a_owner,
                    chat_type=int(ChannelKind.GM),
                    channel=channel,
                    is_thread=False,
                    title="n",
                    body=[],
                )
            ),
            only,
        )
        self.assertChanged(lambda: holder["note"].delete(), only)

    def test_channel_member_writes(self):
        only = {self.nc(self.team_a)}
        channel = self.make_chat()
        cf.reset_pending()
        holder = {}
        self.assertChanged(
            lambda: holder.update(
                member=ChannelMember.objects.create(channel=channel, user=self.a_viewer)
            ),
            only,
        )

        def leave():
            holder["member"].is_deleted = True
            holder["member"].save()

        self.assertChanged(leave, only)
        self.assertChanged(lambda: holder["member"].delete(), only)

    def test_channel_member_burst_costs_one_channel_lookup(self):
        channel = self.make_chat()
        members = [self._user(f"u{n}") for n in range(4)]
        cf.reset_pending()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with self.captureOnCommitCallbacks(execute=False):
            with CaptureQueriesContext(connection) as ctx:
                for user in members:
                    ChannelMember.objects.create(channel=channel, user=user)
        cf.reset_pending()
        lookups = [
            q
            for q in ctx.captured_queries
            if q["sql"].startswith('SELECT "origin_channel"."team_id"')
        ]
        self.assertEqual(len(lookups), 1)

    def test_note_permissions_move_only_their_family(self):
        for note_type, expected in (
            (1, self.np(self.team_a)),
            (2, self.nt(self.team_a)),
            (3, self.nc(self.team_a)),
        ):
            with self.subTest(note_type=note_type):
                holder = {}
                self.assertChanged(
                    lambda: holder.update(
                        perm=NotePermissionMaster.objects.create(
                            team=self.team_a,
                            user=self.a_viewer,
                            note_id=100 + note_type,
                            note_type=note_type,
                            role_id=2,
                        )
                    ),
                    {expected},
                )
                self.assertChanged(lambda: holder["perm"].delete(), {expected})

    def test_a_permission_in_an_unknown_family_moves_all_three(self):
        self.assertChanged(
            lambda: NotePermissionMaster.objects.create(
                team=self.team_a, user=self.a_viewer, note_id=1, note_type=99, role_id=2
            ),
            {self.np(self.team_a), self.nt(self.team_a), self.nc(self.team_a)},
        )

    def test_moving_a_personal_note_through_the_queryset_update_is_announced(self):
        from origin.views.utils import personal_note_move
        from origin.views.utils.personal_note_move import MovePlan, apply_personal_note_move

        note = PersonalNoteMaster.objects.create(
            team=self.team_a, owner=self.a_owner, title="n", body=[]
        )
        cf.reset_pending()
        with (
            mock.patch.object(personal_note_move, "refresh_personal_notes"),
            mock.patch.object(personal_note_move, "purge_notes_from_index"),
            mock.patch.object(personal_note_move, "collect_note_subtree_ids", return_value=[]),
        ):
            self.assertChanged(
                lambda: apply_personal_note_move(
                    team_id=self.team_a.team_id,
                    note=note,
                    plan=MovePlan(target_folder_id=self.folder.folder_id, leaves_team_space=False),
                ),
                {self.np(self.team_a)},
            )
        self.assertEqual(
            PersonalNoteMaster.objects.get(pk=note.pk).folder_id, self.folder.folder_id
        )

    def test_unrelated_team_notes_do_not_move(self):
        before = self.versions()
        self.changed(
            lambda: PersonalNoteMaster.objects.create(
                team=self.team_a, owner=self.a_owner, title="n", body=[]
            )
        )
        moved = {k for k, v in self.versions().items() if before.get(k, 0) != v}
        self.assertEqual(moved, {self.np(self.team_a)})
        self.assertNotIn(str(self.team_b.team_id), " ".join(moved))


class ChangeFeedNoReceiverQueryTests(ChangeFeedCase):
    def test_a_note_write_adds_no_query_for_the_change_feed(self):
        # Receivers only read ids already on the instance. If one started to look
        # something up, every cascade delete would pay for it once per row.
        note = PersonalNoteMaster(team=self.team_a, owner=self.a_owner, title="n", body=[])
        with self.captureOnCommitCallbacks(execute=False):
            with self.assertNumQueries(1):  # the INSERT, and nothing for the feed
                note.save()
        cf.reset_pending()

    def test_a_project_delete_costs_the_same_statements_whatever_its_size(self):
        # Registering receivers turns a model's cascade deletes into row-by-row
        # signals (no "fast delete"). That is only acceptable while the receivers
        # issue no query of their own: then the statements stay flat in the number
        # of rows. Seed every model this change gave a receiver to.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def cost(n):
            project = ProjectMaster.objects.create(
                team=self.team_a,
                project_name=f"P{n}",
                owner=self.a_owner,
                project_system_user=self.a_owner,
            )
            for i in range(n):
                task = self.make_task(project, title=f"t{i}")
                TaskNoteMaster.objects.create(
                    team=self.team_a,
                    project=project,
                    task=task,
                    owner=self.a_owner,
                    title="n",
                    body=[],
                )
                Sprint.objects.create(
                    team=self.team_a,
                    project=project,
                    name=f"S{i}",
                    sequence_number=i + 1,
                    start_date="2026-01-01",
                    end_date="2026-01-14",
                )
                ProjectTags.objects.create(
                    team=self.team_a, project=project, tag_id=i + 1, tag_name=f"tag{i}"
                )
            # A FIXED roster: `ProjectMembers` already costs a few statements per
            # member on main (`pm_channel_signals`, `acl_dirty`), which is not
            # what this measures.
            for m in range(3):
                member = self._member(self.team_a, f"cost_user_{n}_{m}", "viewer")
                ProjectMembers.objects.create(team=self.team_a, project=project, attendee=member)
            cf.reset_pending()
            with CaptureQueriesContext(connection) as ctx:
                with self.captureOnCommitCallbacks(execute=False):
                    delete_project(project)
            cf.reset_pending()
            return len(ctx)

        self.assertEqual(cost(2), cost(12))
