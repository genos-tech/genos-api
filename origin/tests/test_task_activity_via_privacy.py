"""MIP-1485: on the task activity and progress feeds, a key's or app's NAME is its owner's.

`GET /task/activity/` and `GET /task/progress/` sent `metadata.via` whole to every reader of
the task: the key's id, name and prefix, a connected app's connection id and client name, an
Undo's internal ids, all of OTHER members' writes. api #745 (MIP-1470) did this for MCP
`get_task_history`; this is the same rule on the REST path the web client reads.

Rule: a key writes as its owner, so the row's actor is the owner. On a row the viewer made,
`via` is left whole; on anyone else's row it is the kind only (`api_key`, `oauth`, `app`).
Every other field of the row, and every other key of `metadata`, is unchanged. The web
chip reads `via.kind`, and `key_name` / `client_name` for the owner alone.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp import via_privacy

FEED = "/api/v2/task/activity/"
PROGRESS = "/api/v2/task/progress/"

KEY = {"kind": "api_key", "key_id": "k-1", "key_name": "Alice's phone", "key_prefix": "gk_alice"}
APP = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
UNDO = {"kind": "app", "undo_of": {"run_id": "r-1", "step": 3}}
SECRETS = ("Alice's phone", "k-1", "gk_alice", "conn-9", "Alice's Claude", "r-1")


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Spec", status="Open"
        )
        TaskActivity.objects.filter(task=self.task).delete()  # setup rows have no `via`

    def row(self, actor, via, **metadata):
        meta = dict(metadata)
        if via is not None:
            meta["via"] = via
        return TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=actor,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="WIP",
            metadata=meta,
        )

    def feed(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(
            FEED, {"team_id": str(self.team.team_id), "task_id": self.task.task_id}
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def assertNoSecrets(self, data):
        text = repr(data)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class TheActivityFeed(_Base):
    def test_a_colleague_reads_the_kind_and_nothing_else_of_a_key_write(self):
        self.row(self.user2, KEY)
        data = self.feed(self.user)
        self.assertEqual(data[0]["metadata"]["via"], {"kind": "api_key"})
        self.assertNoSecrets(data)

    def test_a_colleague_reads_the_kind_of_a_connected_app_write(self):
        self.row(self.user2, APP)
        data = self.feed(self.user)
        self.assertEqual(data[0]["metadata"]["via"], {"kind": "oauth"}, "the web chip reads this")
        self.assertNoSecrets(data)

    def test_an_undo_click_keeps_its_internal_ids_for_the_person_who_clicked_only(self):
        self.row(self.user2, UNDO)
        self.assertEqual(self.feed(self.user)[0]["metadata"]["via"], {"kind": "app"})
        self.assertEqual(self.feed(self.user2)[0]["metadata"]["via"], UNDO)

    def test_the_owner_reads_their_own_write_whole(self):
        self.row(self.user2, KEY)
        self.row(self.user2, APP)
        rows = self.feed(self.user2)
        self.assertEqual({r["metadata"]["via"]["kind"] for r in rows}, {"api_key", "oauth"})
        self.assertIn(KEY, [r["metadata"]["via"] for r in rows])
        self.assertIn(APP, [r["metadata"]["via"] for r in rows])

    def test_the_viewer_decides_not_the_row_order_or_the_first_row(self):
        mine = self.row(self.user, KEY)
        theirs = self.row(self.user2, KEY)
        by_id = {r["activityId"]: r for r in self.feed(self.user)}
        self.assertEqual(by_id[mine.activity_id]["metadata"]["via"], KEY)
        self.assertEqual(by_id[theirs.activity_id]["metadata"]["via"], {"kind": "api_key"})

    def test_every_other_field_and_metadata_key_is_unchanged(self):
        self.row(self.user2, KEY, closedByPrMerge=True, prUrl="https://x/pull/1", commentId=7)
        mine, theirs = self.feed(self.user2)[0], self.feed(self.user)[0]
        self.assertEqual(
            {k: v for k, v in theirs.items() if k != "metadata"},
            {k: v for k, v in mine.items() if k != "metadata"},
        )
        self.assertEqual(
            {k: v for k, v in theirs["metadata"].items() if k != "via"},
            {"closedByPrMerge": True, "prUrl": "https://x/pull/1", "commentId": 7},
        )
        self.assertEqual(theirs["actor"]["userName"], self.user2.username)
        self.assertEqual((theirs["oldValue"], theirs["newValue"]), ("Open", "WIP"))

    def test_the_agent_and_a_plain_edit_carry_only_their_kind_for_everyone(self):
        self.row(self.user2, {"kind": "agent"})
        self.row(self.user2, {"kind": "app"})
        kinds = sorted(r["metadata"]["via"]["kind"] for r in self.feed(self.user))
        self.assertEqual(kinds, ["agent", "app"])

    def test_a_row_with_no_via_or_no_actor_is_safe(self):
        self.row(self.user2, None)
        self.row(None, KEY)  # a system write: there is no owner to show it to
        rows = self.feed(self.user)
        no_via = next(r for r in rows if r["actor"] is not None)
        system = next(r for r in rows if r["actor"] is None)
        self.assertNotIn("via", no_via["metadata"])
        self.assertEqual(system["metadata"]["via"], {"kind": "api_key"})

    def test_reading_the_feed_does_not_edit_the_stored_row(self):
        row = self.row(self.user2, KEY)
        self.feed(self.user)
        row.refresh_from_db()
        self.assertEqual(row.metadata["via"], KEY)

    def test_a_collapsed_description_run_is_reduced_too(self):
        # The collapse stamps new keys onto the anchor's metadata; `via` must still be reduced.
        for _ in range(2):
            TaskActivity.objects.create(
                team=self.team,
                project=self.project,
                task=self.task,
                actor=self.user2,
                action_type="description_edited",
                field_name="description",
                metadata={"via": KEY},
            )
        [row] = self.feed(self.user)
        self.assertEqual(row["metadata"]["grouped_count"], 2)
        self.assertEqual(row["metadata"]["via"], {"kind": "api_key"})


class TheProgressFeed(_Base):
    def fact_for(self, activity):
        return TaskProgressFact.objects.create(
            ancestor_task=self.task,
            source_task=self.task,
            activity=activity,
            actor=activity.actor,
            action_type=activity.action_type,
            depth=0,
            ts=activity.ts_created_at,
        )

    def entries(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(PROGRESS, {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 200, res.content)
        return res.data["entries"]

    def test_a_colleague_reads_the_kind_only_and_the_owner_reads_it_whole(self):
        self.fact_for(self.row(self.user2, KEY))
        self.fact_for(self.row(self.user2, APP))
        theirs = self.entries(self.user)
        self.assertEqual({e["metadata"]["via"]["kind"] for e in theirs}, {"api_key", "oauth"})
        for e in theirs:
            self.assertEqual(list(e["metadata"]["via"]), ["kind"])
        self.assertNoSecrets(theirs)
        own = [e["metadata"]["via"] for e in self.entries(self.user2)]
        self.assertIn(KEY, own)
        self.assertIn(APP, own)

    def test_the_rest_of_the_entry_is_unchanged(self):
        self.fact_for(self.row(self.user2, KEY))
        [theirs] = self.entries(self.user)
        [own] = self.entries(self.user2)
        self.assertEqual(
            {k: v for k, v in theirs.items() if k != "metadata"},
            {k: v for k, v in own.items() if k != "metadata"},
        )


class TheFunction(BaseAPITestCase):
    def test_whole_for_the_actor_kind_for_anyone_else(self):
        self.assertEqual(via_privacy.via_for_viewer(KEY, 5, 5), KEY)
        self.assertEqual(via_privacy.via_for_viewer(KEY, "5", 5), KEY, "ids compare as text")
        self.assertEqual(via_privacy.via_for_viewer(KEY, 5, 6), {"kind": "api_key"})

    def test_no_actor_or_no_viewer_means_no_owner(self):
        self.assertEqual(via_privacy.via_for_viewer(KEY, None, None), {"kind": "api_key"})
        self.assertEqual(via_privacy.via_for_viewer(APP, None, 5), {"kind": "oauth"})
        self.assertEqual(via_privacy.via_for_viewer(APP, 5, None), {"kind": "oauth"})

    def test_the_gate_fails_closed_for_a_kind_nobody_listed(self):
        future = {"kind": "saml", "session_id": "s-1", "idp_name": "Acme SSO"}
        self.assertEqual(via_privacy.via_for_viewer(future, 1, 2), {"kind": "saml"})
        self.assertEqual(via_privacy.via_for_viewer(future, 1, 1), future, "the owner reads it")

    def test_a_dict_with_no_usable_kind_is_reduced_to_nothing(self):
        for odd in (
            {"key_name": "x"},
            {},
            {"kind": ""},
            {"kind": None, "key_id": "k"},
            {"kind": ["api_key"], "key_name": "x"},
            {"kind": 7, "key_name": "x"},
        ):
            self.assertEqual(via_privacy.via_for_viewer(odd, 1, 2), {}, odd)
            self.assertEqual(via_privacy.via_for_viewer(odd, 1, 1), odd, "the owner reads it")

    def test_a_value_that_is_not_a_dict_is_reduced_for_a_colleague(self):
        for odd in ("account_deleted", ["api_key"], 5):
            self.assertEqual(via_privacy.via_for_viewer(odd, 1, 2), {})
            self.assertEqual(via_privacy.via_for_viewer(odd, 1, 1), odd)
        self.assertIsNone(via_privacy.via_for_viewer(None, 1, 2))

    def test_the_kinds_nothing_private_hangs_on_keep_only_their_public_fields(self):
        # (kind, what a writer stored, what a colleague reads)
        cases = [
            ({"kind": "agent"}, {"kind": "agent"}),
            ({"kind": "agent", "run_id": "r-1"}, {"kind": "agent"}),
            (
                {"kind": "pr_merge", "pr_url": "https://x/pull/1", "token": "t"},
                {"kind": "pr_merge", "pr_url": "https://x/pull/1"},
            ),
            ({"kind": "dependencies"}, {"kind": "dependencies"}),
            (
                {"kind": "github", "github_user": "alice", "token": "t"},
                {"kind": "github", "github_user": "alice"},
            ),
            ({"kind": "unknown"}, {"kind": "unknown"}),
        ]
        for stored, shown in cases:
            self.assertEqual(via_privacy.via_for_viewer(stored, 1, 2), shown)
            self.assertEqual(via_privacy.via_for_viewer(stored, 1, 1), stored)

    def test_every_kind_a_writer_stores_today_is_reduced_for_a_colleague(self):
        # The four a request can stamp (`current_user.get_current_source`, `credential.via()`).
        for stored in (KEY, APP, UNDO, {"kind": "app"}):
            self.assertEqual(via_privacy.via_for_viewer(stored, 1, 2), {"kind": stored["kind"]})
            self.assertEqual(via_privacy.via_for_viewer(stored, 1, 1), stored)

    def test_it_does_not_edit_its_input(self):
        via = dict(APP)
        via_privacy.via_for_viewer(via, 1, 2)
        self.assertEqual(via, APP)
