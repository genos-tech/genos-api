"""`add_project_tag` and `manage_project_labels` (MIP-882).

Two catalogs the agent could read and not change, so "tag it `urgent`" failed whenever
the tag was new and nothing could put a label on a project:

* **Tags** belong to one project and go on its tasks (a task keeps a copy, so a tag is
  addressed by name). Any member of the project may add one, as on the page.
* **Labels** belong to the team and go on projects (a project points at a label by id).
  The owner or an editor of a project creates labels from it and puts them on it.

The label views now call the service the tools call, and the tests through REST pin
their exact messages and status codes, which is what shows the extraction changed
nothing for the page. A tool is stricter than the page in one place, and it is pinned:
a tag whose name differs from an existing one only in case is refused.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.add_project_tag import ADD_PROJECT_TAG
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.list_project_tags import LIST_PROJECT_TAGS
from origin.search_engine.agent.tools.manage_project_labels import MANAGE_PROJECT_LABELS
from origin.services.project_catalog import (
    CatalogRefused,
    add_project_tag,
    assign_labels,
    clean_color,
    create_label,
    labels_of,
    replace_project_labels,
    text_color_for,
    unassign_labels,
)

from .test_base import BaseAPITestCase

User = get_user_model()
TAG = "/api/v2/project/tag/"
LABEL = "/api/v2/project/label/"
ASSIGN = "/api/v2/project/label/assign/"

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "project-catalog-tests",
    }
}


class CatalogBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Release",
            code="REL",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = self.ctx_of(self.user)
        # Another team with a project and a label of its own.
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)
        self.other_project = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Away",
            code="AWY",
            owner=self.stranger,
            project_system_user=self.stranger,
        )
        ProjectMembers.objects.create(
            team=self.other_team, project=self.other_project, attendee=self.stranger
        )
        self.other_label = ProjectLabel.objects.create(
            team=self.other_team, name="Theirs", color="#111111", text_color="white"
        )

    # ---- callers ---------------------------------------------------------

    def ctx_of(self, user, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(user.id))

    def person(self, name, role=None, *, project=None):
        """A teammate; with a `role` they are on the project with that role."""
        user = User.objects.create_user(username=name, email=f"{name}@example.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=user)
        if role:
            ProjectMembers.objects.create(
                team=self.team, project=project or self.project, attendee=user, member_role=role
            )
        return user

    def tag(self, ctx=None, project=None, **args):
        return ADD_PROJECT_TAG.run(
            {"project_id": (project or self.project).project_id, **args}, ctx or self.ctx
        )

    def labels(self, action, ctx=None, project=None, **args):
        return MANAGE_PROJECT_LABELS.run(
            {"action": action, "project_id": (project or self.project).project_id, **args},
            ctx or self.ctx,
        )

    def label(self, name="Client Work", *, team=None, color="#7c3aed"):
        return ProjectLabel.objects.create(
            team=team or self.team, name=name, color=color, text_color="white"
        )

    def refused(self, call, message, kind=ToolError):
        with self.assertRaises(kind) as caught:
            call()
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def label_ids_on(self, project=None):
        return set(
            ProjectLabelAssignment.objects.filter(
                project=(project or self.project).project_id
            ).values_list("label_id", flat=True)
        )


class TheToolsAreApprovalGatedWrites(CatalogBase):
    def test_both_need_approval_and_are_registered(self):
        for tool in (ADD_PROJECT_TAG, MANAGE_PROJECT_LABELS):
            self.assertTrue(tool.requires_approval, tool.name)
            self.assertIs(REGISTRY[tool.name], tool)

    def test_the_descriptions_lead_with_the_read_they_need(self):
        self.assertIn("list_project_tags", ADD_PROJECT_TAG.description)
        self.assertIn("list_project_tags", MANAGE_PROJECT_LABELS.description)

    def test_each_description_says_which_catalog_it_is_not(self):
        # Tags go on tasks and labels on projects; mixing them up is the likely mistake.
        self.assertIn("add_project_tag", MANAGE_PROJECT_LABELS.description)
        self.assertIn("PROJECTS", MANAGE_PROJECT_LABELS.description)

    def test_the_read_points_at_the_new_tool_and_no_longer_says_tags_cannot_be_created(self):
        self.assertIn("add_project_tag", LIST_PROJECT_TAGS.description)
        self.assertNotIn("can't be created", LIST_PROJECT_TAGS.description)


class AddingATag(CatalogBase):
    def test_it_adds_the_row_the_page_adds(self):
        out = self.tag(tag_name="urgent", color="#ff2323")
        row = ProjectTags.objects.get(project=self.project, tag_name="urgent")
        self.assertEqual((row.tag_color, row.tag_text_color), ("#ff2323", "white"))
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual(out["tag_name"], "urgent")
        self.assertEqual(out["project_id"], self.project.project_id)
        self.assertEqual(out["__summary__"], 'Added the tag "urgent" to "Release"')

    def test_it_stores_what_the_page_stores(self):
        """Same columns, same kinds of value, as a tag the page's own POST makes."""
        self.authenticate()
        page = self.client.post(
            TAG,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "tag_name": "by-page",
                "tag_color": "#ff2323",
                "tag_text_color": "white",
            },
            format="json",
        )
        self.assertEqual(page.status_code, 201)
        self.tag(tag_name="by-tool", color="#ff2323")
        by_page = ProjectTags.objects.get(tag_name="by-page")
        by_tool = ProjectTags.objects.get(tag_name="by-tool")
        for field in ("team_id", "project_id", "tag_color", "tag_text_color"):
            self.assertEqual(getattr(by_page, field), getattr(by_tool, field), field)
        self.assertEqual((by_page.tag_id, by_tool.tag_id), (1, 2))

    def test_a_tag_added_by_the_tool_is_one_the_task_tools_accept(self):
        from origin.search_engine.agent.tools.task_tags import resolve_tags

        self.tag(tag_name="hotfix")
        got = resolve_tags(["hotfix"], self.project.project_id)
        self.assertEqual([t["tagName"] for t in got], ["hotfix"])

    def test_the_default_colour_is_grey_with_a_readable_text_colour(self):
        self.tag(tag_name="plain")
        row = ProjectTags.objects.get(tag_name="plain")
        self.assertEqual((row.tag_color, row.tag_text_color), ("#808080", "white"))

    def test_a_colour_is_stored_lower_case_and_trimmed(self):
        self.tag(tag_name="loud", color="  #FF2323 ")
        self.assertEqual(ProjectTags.objects.get(tag_name="loud").tag_color, "#ff2323")

    def test_the_text_colour_follows_the_colour(self):
        self.tag(tag_name="light", color="#ffd93b")
        self.tag(tag_name="dark", color="#0044c2")
        self.assertEqual(ProjectTags.objects.get(tag_name="light").tag_text_color, "black")
        self.assertEqual(ProjectTags.objects.get(tag_name="dark").tag_text_color, "white")

    def test_a_bad_colour_is_refused_and_nothing_is_added(self):
        for bad in ("red", "#fff", "#12345g", "ff2323", 7, ["#ff2323"]):
            with self.subTest(color=bad):
                self.refused(
                    lambda: self.tag(tag_name="x", color=bad),
                    "color must be a hex colour like #3b82f6. Nothing was added.",
                )
        self.assertFalse(ProjectTags.objects.filter(project=self.project).exists())

    def test_the_name_is_trimmed_and_must_be_there(self):
        self.tag(tag_name="  padded  ")
        self.assertTrue(ProjectTags.objects.filter(tag_name="padded").exists())
        for blank in ("", "   ", None, 5):
            with self.subTest(name=blank):
                self.refused(lambda: self.tag(tag_name=blank), "tag_name is required.")

    def test_twenty_characters_fit_and_twenty_one_do_not(self):
        self.tag(tag_name="a" * 20)
        self.refused(
            lambda: self.tag(tag_name="b" * 21),
            "tag_name must be 20 characters or fewer. Nothing was added.",
        )
        self.assertEqual(ProjectTags.objects.filter(project=self.project).count(), 1)

    def test_the_length_limit_counts_the_trimmed_name(self):
        self.tag(tag_name=" " + "a" * 20 + " ")
        self.assertTrue(ProjectTags.objects.filter(tag_name="a" * 20).exists())

    def test_a_tag_that_already_exists_is_refused_in_any_case(self):
        self.tag(tag_name="Backend")
        for again in ("Backend", "backend", "BACKEND", " bAcKeNd "):
            with self.subTest(name=again):
                self.refused(
                    lambda: self.tag(tag_name=again),
                    'A tag named "Backend" already exists in this project. Nothing was added.',
                )
        self.assertEqual(ProjectTags.objects.filter(project=self.project).count(), 1)

    def test_the_same_name_may_exist_in_another_project(self):
        other = ProjectMaster.objects.create(
            team=self.team, project_name="Other", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        self.tag(tag_name="shared")
        self.tag(project=other, tag_name="shared")
        self.assertEqual(ProjectTags.objects.filter(tag_name="shared").count(), 2)

    def test_the_next_id_is_never_one_already_used(self):
        """The page numbers a tag by how many there are, which repeats an id once a tag
        has been deleted. The tool numbers by the highest plus one."""
        for name in ("a", "b", "c"):
            self.tag(tag_name=name)
        ProjectTags.objects.filter(project=self.project, tag_name="a").delete()
        self.tag(tag_name="d")
        ids = list(
            ProjectTags.objects.filter(project=self.project).values_list("tag_id", flat=True)
        )
        self.assertEqual(sorted(ids), [2, 3, 4])
        self.assertEqual(len(ids), len(set(ids)))

    def test_any_member_of_the_project_may_add_one_as_on_the_page(self):
        for role in ("editor", "viewer"):
            user = self.person(f"member-{role}", role)
            self.tag(ctx=self.ctx_of(user), tag_name=f"by-{role}")
        self.assertEqual(ProjectTags.objects.filter(project=self.project).count(), 2)

    def test_someone_outside_the_project_may_not(self):
        outsider = self.person("outsider")
        self.refused(
            lambda: self.tag(ctx=self.ctx_of(outsider), tag_name="x"),
            "not a member of this project",
            ToolForbidden,
        )
        self.assertFalse(ProjectTags.objects.filter(project=self.project).exists())

    def test_a_project_of_another_team_may_not_be_reached(self):
        self.refused(
            lambda: ADD_PROJECT_TAG.run(
                {"project_id": self.other_project.project_id, "tag_name": "x"}, self.ctx
            ),
            "not found",
            ToolForbidden,
        )
        self.assertFalse(ProjectTags.objects.filter(project=self.other_project).exists())

    def test_a_deleted_or_missing_project_is_not_found(self):
        self.project.is_deleted = True
        self.project.save()
        self.refused(lambda: self.tag(tag_name="x"), "not found")
        self.refused(
            lambda: ADD_PROJECT_TAG.run({"project_id": 999999, "tag_name": "x"}, self.ctx),
            "not found",
        )

    def test_the_project_id_must_be_a_number(self):
        self.refused(
            lambda: ADD_PROJECT_TAG.run({"project_id": "abc", "tag_name": "x"}, self.ctx),
            "project_id",
        )


class TheServiceChecksItself(CatalogBase):
    """The tools look the project up and check who may act before they call the service,
    which hides a missing check inside the service. These call it directly."""

    def test_the_luminance_line_is_greater_than_not_at_least(self):
        # 153/255 is exactly 0.6: not over the line, so white. One more step is over it.
        self.assertEqual(text_color_for("#999999"), "white")
        self.assertEqual(text_color_for("#9a9a9a"), "black")
        self.assertEqual(text_color_for("#000000"), "white")
        self.assertEqual(text_color_for("#ffffff"), "black")

    def test_each_channel_has_its_own_weight(self):
        """0.299 red, 0.587 green, 0.114 blue. Colours chosen so that swapping any two
        weights, or changing one, flips the answer."""
        for color, expected in (
            ("#00ff00", "white"),  # 0.587: green alone is just under the line
            ("#ffff00", "black"),  # 0.886
            ("#ff00ff", "white"),  # 0.413
            ("#00ffff", "black"),  # 0.701
            ("#ff8000", "white"),  # 0.594: red full, green half. Swap red and green: 0.737
            ("#8080ff", "white"),  # 0.559: blue's weight carries it. A blue of 0.2 gives 0.645
            ("#00ff80", "black"),  # 0.644: green full, blue half. Swap green and blue: 0.409
        ):
            with self.subTest(color=color):
                self.assertEqual(text_color_for(color), expected)

    def test_the_name_rules_hold_without_a_tool_in_front(self):
        for raw in ("", "  ", None):
            with self.assertRaises(CatalogRefused) as caught:
                add_project_tag(self.project, raw)
            self.assertEqual(
                (caught.exception.message, caught.exception.status), ("tag_name is required.", 400)
            )
        with self.assertRaises(CatalogRefused) as caught:
            add_project_tag(self.project, "x" * 21)
        self.assertEqual(caught.exception.status, 400)

    def test_a_duplicate_is_a_409_even_when_it_only_differs_in_case(self):
        add_project_tag(self.project, "Ops")
        with self.assertRaises(CatalogRefused) as caught:
            add_project_tag(self.project, "ops")
        self.assertEqual(caught.exception.status, 409)

    def test_a_duplicate_is_looked_for_in_this_project_only(self):
        add_project_tag(self.other_project, "Ops")
        add_project_tag(self.project, "ops")
        self.assertEqual(ProjectTags.objects.filter(tag_name__iexact="ops").count(), 2)

    def test_clean_color_defaults_and_validates(self):
        self.assertEqual(clean_color(None, "#abcdef"), "#abcdef")
        self.assertEqual(clean_color("", "#abcdef"), "#abcdef")
        self.assertEqual(clean_color("#ABCDEF", "#000000"), "#abcdef")
        with self.assertRaises(CatalogRefused):
            clean_color("#abcde", "#000000")
        with self.assertRaises(CatalogRefused):
            clean_color(123, "#000000")

    def test_a_label_name_is_unique_in_its_own_team_only(self):
        self.label("Client")
        with self.assertRaises(CatalogRefused) as caught:
            create_label(team_id=self.team.team_id, user=self.user, raw_name="CLIENT")
        self.assertEqual(caught.exception.status, 409)
        # Another team may use the name.
        made = create_label(team_id=self.other_team.team_id, user=self.stranger, raw_name="Client")
        self.assertEqual(made.team_id, self.other_team.team_id)

    def test_label_name_limits(self):
        create_label(team_id=self.team.team_id, user=self.user, raw_name="x" * 30)
        with self.assertRaises(CatalogRefused) as caught:
            create_label(team_id=self.team.team_id, user=self.user, raw_name="y" * 31)
        self.assertEqual(
            (caught.exception.message, caught.exception.status),
            ("name must be 30 characters or fewer.", 400),
        )
        for raw in ("", "   ", None):
            with self.assertRaises(CatalogRefused) as caught:
                create_label(team_id=self.team.team_id, user=self.user, raw_name=raw)
            self.assertEqual(
                (caught.exception.message, caught.exception.status), ("name is required.", 400)
            )

    def test_a_new_label_gets_the_pages_default_colours(self):
        made = create_label(team_id=self.team.team_id, user=self.user, raw_name="Plain")
        self.assertEqual((made.color, made.text_color), ("#7c3aed", "#ffffff"))
        self.assertEqual(made.created_by_id, self.user.id)

    def test_labels_of_a_team_are_never_put_on_another_teams_project(self):
        mine = self.label("Mine")
        added, unknown = assign_labels(self.other_project, [mine.label_id])
        self.assertEqual((added, unknown), ([], [mine.label_id]))
        self.assertEqual(self.label_ids_on(self.other_project), set())

    def test_assign_and_unassign_report_what_they_did(self):
        a, b = self.label("A"), self.label("B")
        self.assertEqual(
            assign_labels(self.project, [b.label_id, a.label_id]),
            (sorted([a.label_id, b.label_id]), []),
        )
        self.assertEqual(assign_labels(self.project, [a.label_id]), ([], []))
        self.assertEqual(
            unassign_labels(self.project, [a.label_id, b.label_id]),
            (sorted([a.label_id, b.label_id]), []),
        )
        self.assertEqual(unassign_labels(self.project, [a.label_id]), ([], []))

    def test_an_unknown_id_changes_nothing_even_beside_a_known_one(self):
        a = self.label("A")
        added, unknown = assign_labels(self.project, [a.label_id, self.other_label.label_id])
        self.assertEqual((added, unknown), ([], [self.other_label.label_id]))
        self.assertEqual(self.label_ids_on(), set())
        assign_labels(self.project, [a.label_id])
        removed, unknown = unassign_labels(self.project, [a.label_id, self.other_label.label_id])
        self.assertEqual((removed, unknown), ([], [self.other_label.label_id]))
        self.assertEqual(self.label_ids_on(), {a.label_id})

    def test_unassign_touches_only_this_project(self):
        a = self.label("A")
        sibling = ProjectMaster.objects.create(
            team=self.team, project_name="Sibling", owner=self.user, project_system_user=self.user
        )
        assign_labels(self.project, [a.label_id])
        assign_labels(sibling, [a.label_id])
        unassign_labels(self.project, [a.label_id])
        self.assertEqual(self.label_ids_on(), set())
        self.assertEqual(self.label_ids_on(sibling), {a.label_id})

    def test_labels_of_lists_a_projects_own_labels_by_name(self):
        b, a = self.label("Beta"), self.label("Alpha")
        self.label("Unused")
        assign_labels(self.project, [b.label_id, a.label_id])
        self.assertEqual([label.name for label in labels_of(self.project)], ["Alpha", "Beta"])

    def test_replacing_keeps_the_pages_full_set_meaning(self):
        a, b = self.label("A"), self.label("B")
        assign_labels(self.project, [a.label_id])
        now = replace_project_labels(self.project, [b.label_id, self.other_label.label_id])
        self.assertEqual([label.label_id for label in now], [b.label_id])
        self.assertEqual(self.label_ids_on(), {b.label_id})


class CreatingALabel(CatalogBase):
    def test_the_owner_creates_a_team_label(self):
        out = self.labels("create", name="Client Work")
        label = ProjectLabel.objects.get(label_id=out["label_id"])
        self.assertEqual((label.name, label.team_id), ("Client Work", self.team.team_id))
        self.assertEqual(label.created_by_id, self.user.id)
        self.assertEqual(out["label_name"], "Client Work")
        self.assertEqual(out["__summary__"], 'Created the label "Client Work"')

    def test_it_is_not_put_on_the_project_it_was_made_from(self):
        self.labels("create", name="Client Work")
        self.assertEqual(self.label_ids_on(), set())

    def test_an_editor_may_and_a_viewer_may_not(self):
        editor = self.person("ed", "editor")
        viewer = self.person("vi", "viewer")
        self.labels("create", ctx=self.ctx_of(editor), name="By editor")
        self.refused(
            lambda: self.labels("create", ctx=self.ctx_of(viewer), name="By viewer"),
            "Only the project owner or an editor can manage project labels.",
            ToolForbidden,
        )
        self.assertEqual(
            set(ProjectLabel.objects.filter(team=self.team).values_list("name", flat=True)),
            {"By editor"},
        )

    def test_a_member_who_is_not_on_the_project_and_a_stranger_may_not(self):
        outsider = self.person("outsider")
        self.refused(
            lambda: self.labels("create", ctx=self.ctx_of(outsider), name="X"),
            "Only the project owner or an editor",
            ToolForbidden,
        )
        # A stranger acts from their own team, and the project is not in it.
        self.refused(
            lambda: self.labels(
                "create", ctx=self.ctx_of(self.stranger, self.other_team), name="X"
            ),
            "not found",
            ToolForbidden,
        )

    def test_the_colour_is_checked_and_the_text_colour_follows_it(self):
        out = self.labels("create", name="Warm", color="#FFD93B")
        label = ProjectLabel.objects.get(label_id=out["label_id"])
        self.assertEqual((label.color, label.text_color), ("#ffd93b", "black"))
        self.refused(
            lambda: self.labels("create", name="Bad", color="yellow"),
            "color must be a hex colour like #3b82f6. Nothing was created.",
        )

    def test_without_a_colour_it_gets_the_pages_default(self):
        out = self.labels("create", name="Plain")
        label = ProjectLabel.objects.get(label_id=out["label_id"])
        self.assertEqual(label.color, "#7c3aed")
        self.assertEqual(label.text_color, "white")

    def test_the_pages_messages_come_back_with_nothing_created(self):
        self.refused(
            lambda: self.labels("create", name="  "), "name is required. Nothing was created."
        )
        self.refused(
            lambda: self.labels("create", name="x" * 31),
            "name must be 30 characters or fewer. Nothing was created.",
        )
        self.labels("create", name="Client")
        self.refused(
            lambda: self.labels("create", name="client"),
            "A label with that name already exists in this team. Nothing was created.",
        )
        self.assertEqual(ProjectLabel.objects.filter(team=self.team).count(), 1)

    def test_another_teams_label_of_the_same_name_does_not_get_in_the_way(self):
        ProjectLabel.objects.create(
            team=self.other_team, name="Client", color="#000000", text_color="white"
        )
        self.labels("create", name="Client")
        self.assertEqual(ProjectLabel.objects.filter(name="Client").count(), 2)

    def test_the_action_is_checked(self):
        self.refused(lambda: self.labels("delete"), "`action` must be one of")
        self.refused(
            lambda: MANAGE_PROJECT_LABELS.run({"project_id": self.project.project_id}, self.ctx),
            "`action` must be one of",
        )


class PuttingLabelsOnAProject(CatalogBase):
    def test_assign_puts_them_on_and_says_which_are_there_now(self):
        a, b = self.label("A"), self.label("B")
        out = self.labels("assign", label_ids=[a.label_id, b.label_id])
        self.assertEqual(self.label_ids_on(), {a.label_id, b.label_id})
        self.assertEqual(out["changed_label_ids"], sorted([a.label_id, b.label_id]))
        self.assertEqual(
            out["labels"],
            [
                {"label_id": a.label_id, "label_name": "A"},
                {"label_id": b.label_id, "label_name": "B"},
            ],
        )
        self.assertEqual(out["__summary__"], 'Put 2 label(s) on "Release"')

    def test_it_adds_to_what_the_project_has_and_does_not_replace_it(self):
        a, b = self.label("A"), self.label("B")
        self.labels("assign", label_ids=[a.label_id])
        out = self.labels("assign", label_ids=[b.label_id])
        self.assertEqual(self.label_ids_on(), {a.label_id, b.label_id})
        self.assertEqual(out["changed_label_ids"], [b.label_id])

    def test_assigning_what_is_already_there_is_not_an_error(self):
        a = self.label("A")
        self.labels("assign", label_ids=[a.label_id])
        out = self.labels("assign", label_ids=[a.label_id])
        self.assertEqual(out["changed_label_ids"], [])
        self.assertEqual(out["__summary__"], '"Release" already had those labels')

    def test_unassign_takes_off_only_those(self):
        a, b = self.label("A"), self.label("B")
        self.labels("assign", label_ids=[a.label_id, b.label_id])
        out = self.labels("unassign", label_ids=[a.label_id])
        self.assertEqual(self.label_ids_on(), {b.label_id})
        self.assertEqual(out["changed_label_ids"], [a.label_id])
        self.assertEqual(out["__summary__"], 'Took 1 label(s) off "Release"')

    def test_taking_off_a_label_the_project_never_had_is_not_an_error(self):
        a = self.label("A")
        out = self.labels("unassign", label_ids=[a.label_id])
        self.assertEqual(out["changed_label_ids"], [])
        self.assertEqual(out["__summary__"], '"Release" had none of those labels')

    def test_the_label_itself_survives_being_taken_off(self):
        a = self.label("A")
        self.labels("assign", label_ids=[a.label_id])
        self.labels("unassign", label_ids=[a.label_id])
        self.assertTrue(ProjectLabel.objects.filter(label_id=a.label_id).exists())

    def test_another_teams_label_is_refused_and_nothing_changes(self):
        a = self.label("A")
        self.refused(
            lambda: self.labels("assign", label_ids=[a.label_id, self.other_label.label_id]),
            f"Label id(s) [{self.other_label.label_id}] are not labels of this team.",
        )
        self.assertEqual(self.label_ids_on(), set())

    def test_a_label_that_does_not_exist_is_refused_the_same_way(self):
        self.refused(
            lambda: self.labels("unassign", label_ids=[999999]),
            "Label id(s) [999999] are not labels of this team.",
        )

    def test_the_ids_are_checked(self):
        a = self.label("A")
        for bad in (None, [], "3", {"a": 1}, [True], [False], ["x"], [1.5], [None]):
            with self.subTest(label_ids=bad):
                # The tool's own check, not the "not a label of this team" answer that a
                # number it let through would get.
                message = self.refused(
                    lambda: self.labels("assign", label_ids=bad), "`label_ids` must be"
                )
                self.assertNotIn("not labels of this team", message)
        # Digits as text are accepted, and a repeated id counts once.
        out = self.labels("assign", label_ids=[str(a.label_id), a.label_id])
        self.assertEqual(out["changed_label_ids"], [a.label_id])

    def test_a_repeated_unknown_id_is_named_once(self):
        self.refused(
            lambda: self.labels("assign", label_ids=[999999, 999999]),
            "Label id(s) [999999] are not labels of this team.",
        )

    def test_only_the_owner_or_an_editor_may(self):
        a = self.label("A")
        editor = self.person("ed", "editor")
        viewer = self.person("vi", "viewer")
        self.labels("assign", ctx=self.ctx_of(editor), label_ids=[a.label_id])
        self.assertEqual(self.label_ids_on(), {a.label_id})
        self.refused(
            lambda: self.labels("unassign", ctx=self.ctx_of(viewer), label_ids=[a.label_id]),
            "Only the project owner or an editor can manage project labels.",
            ToolForbidden,
        )
        self.assertEqual(self.label_ids_on(), {a.label_id})

    def test_a_project_of_another_team_may_not_be_reached(self):
        a = self.label("A")
        self.refused(
            lambda: self.labels("assign", project=self.other_project, label_ids=[a.label_id]),
            "not found",
            ToolForbidden,
        )

    @override_settings(CACHES=_LOCMEM_CACHE)
    def test_the_cached_project_list_is_dropped_when_a_label_changes(self):
        """`/project/projects/` embeds the labels and is cached for a minute, so a change
        the page would not show for a minute is not a change at all."""
        a = self.label("A")
        key = f"project:list:{self.team.team_id}:{self.user2.id}"
        for action in ("assign", "unassign"):
            with self.subTest(action=action):
                cache.set(key, {"stale": True})
                self.labels(action, label_ids=[a.label_id])
                self.assertIsNone(cache.get(key), f"{action} left the cached list behind")

    @override_settings(CACHES=_LOCMEM_CACHE)
    def test_nothing_is_dropped_when_nothing_changed(self):
        a = self.label("A")
        key = f"project:list:{self.team.team_id}:{self.user2.id}"
        self.labels("assign", label_ids=[a.label_id])
        cache.set(key, {"keep": True})
        self.labels("assign", label_ids=[a.label_id])  # already there
        self.labels("unassign", label_ids=[self.label("B").label_id])  # never had it
        self.assertEqual(cache.get(key), {"keep": True})

    @override_settings(CACHES=_LOCMEM_CACHE)
    def test_creating_a_label_leaves_the_cached_list_alone(self):
        key = f"project:list:{self.team.team_id}:{self.user2.id}"
        cache.set(key, {"keep": True})
        self.labels("create", name="Brand new")
        self.assertEqual(cache.get(key), {"keep": True})

    @override_settings(CACHES=_LOCMEM_CACHE)
    def test_other_teams_cached_lists_are_not_touched(self):
        a = self.label("A")
        theirs = f"project:list:{self.other_team.team_id}:{self.stranger.id}"
        cache.set(theirs, {"keep": True})
        self.labels("assign", label_ids=[a.label_id])
        self.assertEqual(cache.get(theirs), {"keep": True})


class ReadingTheLabelsWithTheTags(CatalogBase):
    def read(self, ctx=None, project=None):
        return LIST_PROJECT_TAGS.run(
            {"project_id": (project or self.project).project_id}, ctx or self.ctx
        )

    def test_the_teams_labels_come_back_with_whether_this_project_has_each(self):
        b, a = self.label("Beta"), self.label("Alpha")
        self.labels("assign", label_ids=[b.label_id])
        out = self.read()
        self.assertEqual(
            [(x["label_name"], x["on_this_project"]) for x in out["labels"]],
            [("Alpha", False), ("Beta", True)],
        )
        self.assertEqual(out["labels"][0]["label_id"], a.label_id)
        self.assertIn("2 label(s)", out["__summary__"])

    def test_another_teams_labels_are_not_listed(self):
        self.label("Mine")
        names = {x["label_name"] for x in self.read()["labels"]}
        self.assertEqual(names, {"Mine"})

    def test_a_label_on_another_project_is_not_on_this_one(self):
        a = self.label("A")
        sibling = ProjectMaster.objects.create(
            team=self.team, project_name="Sibling", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=sibling, attendee=self.user)
        self.labels("assign", project=sibling, label_ids=[a.label_id])
        self.assertEqual(self.read()["labels"][0]["on_this_project"], False)
        self.assertEqual(self.read(project=sibling)["labels"][0]["on_this_project"], True)

    def test_a_project_with_no_labels_says_so(self):
        out = self.read()
        self.assertEqual(out["labels"], [])
        self.assertIn("0 label(s)", out["__summary__"])

    def test_the_tags_are_still_there_and_unchanged(self):
        self.tag(tag_name="urgent", color="#ff2323")
        out = self.read()
        self.assertEqual(
            out["tags"], [{"name": "urgent", "color": "#ff2323", "text_color": "white"}]
        )

    def test_the_read_still_needs_membership(self):
        outsider = self.person("outsider")
        self.refused(lambda: self.read(self.ctx_of(outsider)), "not a member", ToolForbidden)


class ThePagesDidNotChange(CatalogBase):
    """The label views call the service now. What they answer must be what it was."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def create(self, **body):
        return self.client.post(
            LABEL, {"project_id": self.project.project_id, **body}, format="json"
        )

    def test_a_create_still_answers_201_with_the_pages_shape(self):
        res = self.create(name="Client Work", color="#7c3aed", text_color="#ffffff")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(set(res.data), {"labelId", "name", "color", "textColor", "projectCount"})
        self.assertEqual((res.data["name"], res.data["projectCount"]), ("Client Work", 0))

    def test_a_create_without_colours_still_gets_the_defaults(self):
        res = self.create(name="Plain")
        self.assertEqual((res.data["color"], res.data["textColor"]), ("#7c3aed", "#ffffff"))

    def test_the_pages_messages_and_statuses(self):
        self.create(name="Client")
        for body, status, message in (
            ({"name": "   "}, 400, "name is required."),
            ({}, 400, "name is required."),
            ({"name": "x" * 31}, 400, "name must be 30 characters or fewer."),
            ({"name": "CLIENT"}, 409, "A label with that name already exists in this team."),
        ):
            with self.subTest(body=body):
                res = self.create(**body)
                self.assertEqual((res.status_code, res.data), (status, {"error": message}))

    def test_a_page_create_trims_the_name_as_before(self):
        self.assertEqual(self.create(name="  Padded  ").data["name"], "Padded")

    def test_the_page_and_the_tool_both_refuse_a_colour_that_is_not_hex(self):
        """MIP-955: the page used to store whatever the client sent (a value past the
        10-character column was a 500). It now refuses what the picker cannot produce,
        as the tool always has; the tool's own behaviour is unchanged."""
        res = self.create(name="Odd", color="tomato")
        self.assertEqual(
            (res.status_code, res.data),
            (400, {"error": "color must be a hex colour like #3b82f6."}),
        )
        self.refused(lambda: self.labels("create", name="Odd too", color="tomato"), "hex colour")

    def test_assign_still_replaces_the_whole_set_and_ignores_other_teams_labels(self):
        a, b = self.label("A"), self.label("B")
        self.client.put(
            ASSIGN,
            {"project_id": self.project.project_id, "label_ids": [a.label_id]},
            format="json",
        )
        res = self.client.put(
            ASSIGN,
            {
                "project_id": self.project.project_id,
                "label_ids": [b.label_id, self.other_label.label_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual([x["labelId"] for x in res.data], [b.label_id])
        self.assertEqual(self.label_ids_on(), {b.label_id})

    def test_assign_still_refuses_a_list_that_is_not_a_list(self):
        res = self.client.put(
            ASSIGN, {"project_id": self.project.project_id, "label_ids": "3"}, format="json"
        )
        self.assertEqual((res.status_code, res.data), (400, {"error": "label_ids must be a list."}))

    def test_a_label_made_by_the_tool_is_listed_by_the_page(self):
        self.labels("create", name="From the agent")
        res = self.client.get(f"{LABEL}?team_id={self.team.team_id}")
        self.assertEqual([x["name"] for x in res.data], ["From the agent"])

    def test_labels_put_on_by_the_tool_show_in_the_pages_project_list(self):
        a = self.label("Visible")
        self.labels("assign", label_ids=[a.label_id])
        res = self.client.get(f"/api/v2/project/projects/?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200)
        project = next(p for p in res.data if p["projectId"] == self.project.project_id)
        self.assertEqual([x["name"] for x in project["projectLabels"]], ["Visible"])

    def test_a_tag_made_by_the_tool_is_listed_by_the_page(self):
        self.tag(tag_name="agent-made", color="#ff2323")
        res = self.client.get(
            f"{TAG}?team_id={self.team.team_id}&project_id={self.project.project_id}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data, [{"tagName": "agent-made", "tagColor": "#ff2323", "tagTextColor": "white"}]
        )
