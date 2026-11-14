"""MIP-1470: on `get_task_history` over MCP, a key's or app's NAME is its owner's.

A change made with an API key or a connected app carries `via` with the key's id, name and prefix.
Before, every member who could read the task read another member's key name ("my-laptop-claude"),
which can be personal. Now the owner (a key writes as its owner, so the row's actor) sees it whole
and everyone else sees the kind only. The in-app agent shares the registry tool and gets the same
rule (MIP-1470, in-app half), unconditionally: it is a privacy fix.
"""

from __future__ import annotations

import json

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_progress_models import TaskProgressFact
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import via_privacy

MINE = {"kind": "api_key", "key_id": "k-mine", "key_name": "my laptop", "key_prefix": "gk_mine"}
THEIRS = {
    "kind": "api_key",
    "key_id": "k-theirs",
    "key_name": "Alice's private phone",
    "key_prefix": "gk_alice",
}


class TestTheKeyNameIsTheOwners(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)  # the viewer: self.user
        TaskActivity.objects.all().delete()  # setup writes rows with no actor; start clean
        self.ids = {}

    def row(self, name, actor, via, action="status_changed", **extra):
        row = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=actor,
            action_type=action,
            field_name=action.replace("_changed", ""),
            old_value="Open",
            new_value="WIP",
            metadata={"via": via, **extra},
        )
        self.ids[name] = row.pk
        return row

    def history(self):
        res = self._call("get_task_history", {"task_id": "WRD-7"})
        payload = self._result(res)["structuredContent"]
        text, _ = self._tool_text(res)
        return payload["history"], text

    def by_actor_via(self, rows, username, kind):
        return [
            r
            for r in rows
            if r["actor"] and r["actor"]["username"] == username and r["via"]["kind"] == kind
        ]

    def test_the_owner_reads_their_own_key_whole(self):
        self.row("mine", self.user, MINE)
        rows, _ = self.history()
        [mine] = [r for r in rows if r["actor"]["user_id"] == str(self.user.id)]
        self.assertEqual(mine["via"]["key_name"].count("my laptop"), 1)
        self.assertEqual(mine["via"]["key_id"], "k-mine")
        self.assertEqual(mine["via"]["key_prefix"], "gk_mine")

    def test_another_members_key_shows_its_kind_and_nothing_else(self):
        self.row("theirs", self.user2, THEIRS)
        rows, text = self.history()
        [theirs] = [r for r in rows if r["actor"]["user_id"] == str(self.user2.id)]
        self.assertEqual(theirs["via"], {"kind": "api_key"})
        for secret in ("Alice's private phone", "k-theirs", "gk_alice"):
            self.assertNotIn(secret, text, "not in the structured content and not in the text")

    def test_the_rest_of_the_row_is_unchanged(self):
        self.row("theirs", self.user2, THEIRS)
        rows, _ = self.history()
        [theirs] = [r for r in rows if r["actor"]["user_id"] == str(self.user2.id)]
        self.assertEqual(theirs["action"], "status_changed")
        self.assertEqual((theirs["old_value"], theirs["new_value"]), ("Open", "WIP"))
        self.assertEqual(theirs["actor"]["username"], self.user2.username)

    def test_an_undo_click_keeps_its_internal_ids_for_the_person_who_clicked_only(self):
        undo = {"kind": "app", "undo_of": {"run_id": "r-1", "step": 3}}
        self.row("mine", self.user, undo)
        self.row("theirs", self.user2, undo)
        rows, text = self.history()
        mine = [r for r in rows if r["actor"]["user_id"] == str(self.user.id)][0]
        theirs = [r for r in rows if r["actor"]["user_id"] == str(self.user2.id)][0]
        self.assertEqual(mine["via"]["undo_of"], {"run_id": "r-1", "step": 3})
        self.assertEqual(theirs["via"], {"kind": "app"})

    def test_public_facts_are_not_hidden(self):
        # A GitHub login and a PR URL are not private, and not a key's name.
        self.row(
            "merge", self.user2, {"kind": "app"}, closedByPrMerge=True, prUrl="https://x/pull/1"
        )
        self.row("agent", self.user2, {"kind": "agent"})
        rows, _ = self.history()
        kinds = {r["via"]["kind"] for r in rows}
        self.assertIn("pr_merge", kinds)
        self.assertIn("agent", kinds)
        merge = [r for r in rows if r["via"]["kind"] == "pr_merge"][0]
        self.assertEqual(merge["via"]["pr_url"], "https://x/pull/1")

    def test_a_connected_app_is_treated_like_a_key(self):
        oauth = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
        self.row("theirs", self.user2, oauth)
        rows, text = self.history()
        [theirs] = [r for r in rows if r["actor"]["user_id"] == str(self.user2.id)]
        self.assertEqual(theirs["via"], {"kind": "api_key"})
        self.assertNotIn("Alice's Claude", text)
        self.assertNotIn("conn-9", text)

    def test_the_tool_description_says_so(self):
        listed = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertIn(
            "on other people's writes only the kind", listed["get_task_history"]["description"]
        )


OAUTH = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
SECRETS = ("Alice's private phone", "k-theirs", "gk_alice", "conn-9", "Alice's Claude")


class TestTheInAppToolKeepsTheSameRule(McpBase):
    """MIP-1470 (in-app half): the in-app registry tool shows another member's key or app by its
    kind only. The row's actor (a key writes as its owner) reads their own whole."""

    def setUp(self):
        super().setUp()
        TaskActivity.objects.all().delete()

    def row(self, actor, via, action="status_changed", **extra):
        return TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=actor,
            action_type=action,
            field_name=action.replace("_changed", ""),
            old_value="Open",
            new_value="WIP",
            metadata={"via": via, **extra},
        )

    def read(self):
        out = REGISTRY["get_task_history"].run(
            {"task_id": self.task.task_id},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
        )
        return out["history"], json.dumps(out)

    def of(self, rows, user):
        return [r for r in rows if r["actor"]["user_id"] == str(user.id)]

    def test_another_members_key_is_its_kind_only(self):
        self.row(self.user2, THEIRS)
        rows, text = self.read()
        [theirs] = self.of(rows, self.user2)
        self.assertEqual(theirs["via"], {"kind": "api_key"})
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_another_members_connected_app_is_its_kind_only(self):
        # A connected app is shown to the model as the kind it already knows, an API key.
        self.row(self.user2, OAUTH)
        rows, text = self.read()
        [theirs] = self.of(rows, self.user2)
        self.assertEqual(theirs["via"], {"kind": "api_key"})
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_the_owner_reads_their_own_key_and_app_whole(self):
        self.row(self.user, MINE)
        self.row(self.user, OAUTH)
        rows, _ = self.read()
        mine = self.of(rows, self.user)
        self.assertEqual({r["via"]["key_id"] for r in mine}, {"k-mine", "conn-9"})
        self.assertTrue(any("my laptop" in str(r["via"]["key_name"]) for r in mine))
        self.assertTrue(any("Alice's Claude" in str(r["via"]["key_name"]) for r in mine))

    def test_an_undo_click_keeps_its_internal_ids_for_the_person_who_clicked_only(self):
        undo = {"kind": "app", "undo_of": {"run_id": "r-1", "step": 3}}
        self.row(self.user, undo)
        self.row(self.user2, undo)
        rows, _ = self.read()
        self.assertEqual(self.of(rows, self.user)[0]["via"]["undo_of"], undo["undo_of"])
        self.assertEqual(self.of(rows, self.user2)[0]["via"], {"kind": "app"})

    def test_public_facts_and_system_writes_are_not_hidden(self):
        self.row(self.user2, {"kind": "app"}, closedByPrMerge=True, prUrl="https://x/pull/1")
        self.row(self.user2, {"kind": "agent"})
        self.row(None, {"kind": "app"}, derivedFromDependencies=True)
        self.row(None, {"kind": "api_key", "key_name": "ghost", "key_id": "k-ghost"})
        rows, text = self.read()
        vias = [r["via"] for r in rows]
        self.assertIn({"kind": "pr_merge", "pr_url": "https://x/pull/1"}, vias)
        self.assertIn({"kind": "agent"}, vias)
        self.assertIn({"kind": "dependencies"}, vias)
        self.assertIn({"kind": "api_key"}, vias, "a keyed write with no actor has no owner")
        self.assertNotIn("ghost", text)

    def test_the_rest_of_the_row_is_unchanged(self):
        self.row(self.user2, THEIRS)
        rows, _ = self.read()
        [theirs] = self.of(rows, self.user2)
        self.assertEqual((theirs["action"], theirs["old_value"]), ("status_changed", "Open"))
        self.assertEqual(theirs["actor"]["username"], self.user2.username)


class TestTheHook(McpBase):
    """The function on its own: odd rows must never fail a history read."""

    def ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_a_payload_without_history_is_returned_as_it_is(self):
        payload = {"error": "nothing"}
        self.assertIs(via_privacy.with_via_owner_only(payload, {}, {}, self.ctx()), payload)

    def test_a_row_without_an_actor_or_a_via_is_safe(self):
        payload = {
            "history": [
                {"via": {"kind": "api_key", "key_name": "n"}, "actor": None},
                {"actor": {"user_id": str(self.user.id)}},
                "not a row",
            ]
        }
        out = via_privacy.with_via_owner_only(payload, {}, {}, self.ctx())["history"]
        self.assertEqual(out[0]["via"], {"kind": "api_key"}, "a system write has no owner to show")
        self.assertEqual(out[1], {"actor": {"user_id": str(self.user.id)}})
        self.assertEqual(out[2], "not a row")

    def test_it_does_not_edit_its_input(self):
        payload = {
            "history": [
                {"via": dict(THEIRS), "actor": {"user_id": "someone-else"}},
            ]
        }
        via_privacy.with_via_owner_only(payload, {}, {}, self.ctx())
        self.assertEqual(payload["history"][0]["via"]["key_name"], "Alice's private phone")


class TestProgressHistoryKeepsTheSameRule(McpBase):
    """MIP-1485: `get_progress_history` shares `_via` with `get_task_history` and was missed."""

    OAUTH = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
    SECRETS = ("Alice's private phone", "k-theirs", "gk_alice", "conn-9", "Alice's Claude")

    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)  # the viewer: self.user
        TaskActivity.objects.all().delete()

    def fact(self, actor, via):
        row = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=actor,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="WIP",
            metadata={"via": via},
        )
        return TaskProgressFact.objects.create(
            ancestor_task=self.task,
            source_task=self.task,
            activity=row,
            actor=actor,
            action_type=row.action_type,
            depth=0,
            ts=row.ts_created_at,
        )

    def entries(self):
        res = self._call("get_progress_history", {"task_id": "WRD-7"})
        payload = self._result(res)["structuredContent"]
        text, _ = self._tool_text(res)
        return payload["entries"], text

    def test_a_colleague_reads_the_kind_only_of_a_key_and_of_a_connected_app(self):
        self.fact(self.user2, THEIRS)
        self.fact(self.user2, self.OAUTH)
        rows, text = self.entries()
        self.assertEqual([r["via"] for r in rows], [{"kind": "api_key"}, {"kind": "api_key"}])
        for secret in self.SECRETS:
            self.assertNotIn(secret, text, "not in the structured content and not in the text")

    def test_the_owner_reads_their_own_key_and_app_whole(self):
        self.fact(self.user, MINE)
        self.fact(self.user, self.OAUTH)
        rows, _ = self.entries()
        names = [r["via"]["key_name"] for r in rows]
        self.assertTrue(any("my laptop" in n for n in names), names)
        self.assertTrue(any("Alice's Claude" in n for n in names), names)
        self.assertTrue(all(r["via"]["key_id"] for r in rows))

    def test_the_rest_of_the_entry_is_unchanged(self):
        self.fact(self.user2, THEIRS)
        rows, _ = self.entries()
        self.assertEqual(rows[0]["action_type"], "status_changed")
        self.assertEqual(rows[0]["actor"]["user_id"], str(self.user2.id))
        self.assertEqual(rows[0]["display_id"], "WRD-7")

    def test_the_description_says_so(self):
        listed = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertIn(
            "on other people's writes only the kind", listed["get_progress_history"]["description"]
        )
