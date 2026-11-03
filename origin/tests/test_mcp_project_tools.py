"""Over MCP, a project can be created and changed, and its people managed (MIP-882).

There is no approval card on this surface, so the key's scope is the only
authorization. What the endpoint adds: creating or renaming a project needs a
write key and nothing more, but adding a person or changing a role changes who can
see and edit the project, so it needs the key's opt-in, the same one a delete
needs (Ken's decision 2); a read key sees neither; and the ids the calls need come
from published reads.
"""

from __future__ import annotations

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools

NEW = {"manage_project", "manage_project_members"}


class McpProjectBase(McpBase):
    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def run_tool(self, name, arguments):
        result = self._result(self._call(name, arguments))
        return result, result["content"][0]["text"]


class TestWhoSeesWhat(McpProjectBase):
    def test_a_write_key_sees_the_project_tool_but_not_the_members_tool(self):
        self._key(scope=SCOPE_WRITE)
        self.assertEqual(NEW & set(self.listed()), {"manage_project"})

    def test_the_opt_in_adds_the_members_tool(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertEqual(NEW & set(self.listed()), NEW)

    def test_a_read_key_sees_neither(self):
        self._key(scope=SCOPE_READ)
        self.assertFalse(NEW & set(self.listed()))

    def test_the_catalog_says_why_the_members_tool_is_gated_and_where_the_ids_come_from(self):
        text = tools.BY_NAME["manage_project_members"].description
        self.assertIn("opted into destructive tools", text)
        self.assertIn("get_team_members", text)
        self.assertIn("list_projects", text)
        self.assertNotIn("approval", text.lower(), "this surface has no approval card to promise")
        self.assertIn("list_projects", tools.BY_NAME["manage_project"].description)


class TestTheCallsWork(McpProjectBase):
    def test_a_project_is_created_and_then_listed(self):
        self._key(scope=SCOPE_WRITE)
        result, _ = self.run_tool("manage_project", {"action": "create", "name": "Website Rebuild"})
        self.assertFalse(result.get("isError"), result)
        created = result["structuredContent"]["project_id"]
        listing, _ = self.run_tool("list_projects", {})
        ids = [p["project_id"] for p in listing["structuredContent"]["projects"]]
        self.assertIn(created, ids)
        self.assertEqual(ProjectMaster.objects.get(project_id=created).owner_id, self.user.id)

    def test_people_named_when_creating_come_from_get_team_members(self):
        self._key(scope=SCOPE_WRITE)
        team, _ = self.run_tool("get_team_members", {})
        ids = [m["user_id"] for m in team["structuredContent"]["members"]]
        self.assertIn(str(self.user2.id), ids)
        result, _ = self.run_tool(
            "manage_project",
            {"action": "create", "name": "Crew", "member_ids": [str(self.user2.id)]},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(
            ProjectMembers.objects.filter(
                project_id=result["structuredContent"]["project_id"], attendee=self.user2
            ).exists()
        )

    def test_a_project_is_renamed_by_its_id(self):
        self._key(scope=SCOPE_WRITE)
        result, _ = self.run_tool(
            "manage_project",
            {"action": "update", "project_id": self.project.project_id, "name": "Redesign"},
        )
        self.assertFalse(result.get("isError"), result)
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Redesign")

    def test_a_taken_name_comes_back_as_an_error_and_creates_nothing(self):
        self._key(scope=SCOPE_WRITE)
        result, text = self.run_tool(
            "manage_project", {"action": "create", "name": "Website Redesign"}
        )
        self.assertTrue(result.get("isError"), result)
        self.assertIn("already uses that name", text)
        self.assertEqual(ProjectMaster.objects.filter(project_name="Website Redesign").count(), 1)

    def test_adding_a_member_is_refused_without_the_opt_in(self):
        self._key(scope=SCOPE_WRITE)
        result, text = self.run_tool(
            "manage_project_members",
            {"action": "add", "project_id": self.project.project_id, "user_id": str(self.user2.id)},
        )
        self.assertTrue(result.get("isError"), result)
        self.assertIn("destructive", text.lower())
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )

    def test_adding_a_member_and_setting_a_role_work_with_the_opt_in(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        added, _ = self.run_tool(
            "manage_project_members",
            {"action": "add", "project_id": self.project.project_id, "user_id": str(self.user2.id)},
        )
        self.assertFalse(added.get("isError"), added)
        changed, _ = self.run_tool(
            "manage_project_members",
            {
                "action": "set_role",
                "project_id": self.project.project_id,
                "user_id": str(self.user2.id),
                "role": "editor",
            },
        )
        self.assertFalse(changed.get("isError"), changed)
        self.assertEqual(
            ProjectMembers.objects.get(project=self.project, attendee=self.user2).member_role,
            "editor",
        )

    def test_a_read_key_cannot_call_them_even_from_a_cached_list(self):
        self._key(scope=SCOPE_READ)
        for name, args in (
            ("manage_project", {"action": "create", "name": "Sneaky"}),
            (
                "manage_project_members",
                {
                    "action": "add",
                    "project_id": self.project.project_id,
                    "user_id": str(self.user2.id),
                },
            ),
        ):
            result, _ = self.run_tool(name, args)
            self.assertTrue(result.get("isError"), name)
        self.assertFalse(ProjectMaster.objects.filter(project_name="Sneaky").exists())


LABELS_AND_TAGS = {"add_project_tag", "manage_project_labels"}


class TestTagAndLabelTools(McpProjectBase):
    """Neither changes who can see or edit anything, so a write key is all they need."""

    def test_a_write_key_sees_both_without_the_opt_in(self):
        self._key(scope=SCOPE_WRITE)
        self.assertEqual(LABELS_AND_TAGS & set(self.listed()), LABELS_AND_TAGS)

    def test_a_read_key_sees_neither(self):
        self._key(scope=SCOPE_READ)
        self.assertFalse(LABELS_AND_TAGS & set(self.listed()))

    def test_neither_is_one_of_the_destructive_tools(self):
        for name in LABELS_AND_TAGS:
            self.assertFalse(tools.BY_NAME[name].destructive, name)
            self.assertTrue(tools.BY_NAME[name].write, name)

    def test_the_catalog_says_where_the_ids_come_from_and_promises_no_approval(self):
        for name in LABELS_AND_TAGS:
            text = tools.BY_NAME[name].description
            self.assertIn("list_project_tags", text, name)
            self.assertNotIn("approval", text.lower(), "this surface has no approval card")
        self.assertIn("list_projects", tools.BY_NAME["add_project_tag"].description)

    def test_a_tag_is_added_and_then_read_back(self):
        self._key(scope=SCOPE_WRITE)
        added, _ = self.run_tool(
            "add_project_tag",
            {"project_id": self.project.project_id, "tag_name": "urgent", "color": "#ff2323"},
        )
        self.assertFalse(added.get("isError"), added)
        read, _ = self.run_tool("list_project_tags", {"project_id": self.project.project_id})
        self.assertEqual(
            [t["name"] for t in read["structuredContent"]["tags"]],
            ["urgent"],
        )

    def test_a_label_is_created_put_on_the_project_and_read_back(self):
        self._key(scope=SCOPE_WRITE)
        made, _ = self.run_tool(
            "manage_project_labels",
            {"action": "create", "project_id": self.project.project_id, "name": "Client Work"},
        )
        self.assertFalse(made.get("isError"), made)
        label_id = made["structuredContent"]["label_id"]
        before, _ = self.run_tool("list_project_tags", {"project_id": self.project.project_id})
        self.assertEqual(
            [(x["label_id"], x["on_this_project"]) for x in before["structuredContent"]["labels"]],
            [(label_id, False)],
        )
        put, _ = self.run_tool(
            "manage_project_labels",
            {"action": "assign", "project_id": self.project.project_id, "label_ids": [label_id]},
        )
        self.assertFalse(put.get("isError"), put)
        after, _ = self.run_tool("list_project_tags", {"project_id": self.project.project_id})
        self.assertEqual(after["structuredContent"]["labels"][0]["on_this_project"], True)

    def test_a_refusal_comes_back_as_an_error_with_the_pages_message(self):
        self._key(scope=SCOPE_WRITE)
        args = {"project_id": self.project.project_id, "tag_name": "x" * 21}
        result, text = self.run_tool("add_project_tag", args)
        self.assertTrue(result.get("isError"), result)
        self.assertIn("20 characters or fewer", text)

    def test_a_read_key_cannot_call_them_even_from_a_cached_list(self):
        self._key(scope=SCOPE_READ)
        for name, args in (
            ("add_project_tag", {"project_id": self.project.project_id, "tag_name": "sneaky"}),
            (
                "manage_project_labels",
                {"action": "create", "project_id": self.project.project_id, "name": "Sneaky"},
            ),
        ):
            result, _ = self.run_tool(name, args)
            self.assertTrue(result.get("isError"), name)
        self.assertFalse(ProjectLabel.objects.filter(name="Sneaky").exists())
        self.assertFalse(ProjectTags.objects.filter(tag_name="sneaky").exists())
