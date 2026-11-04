"""Every method a task route answers has a handler that fits the route.

One view class serving two routes (`sprint/` and `sprint/<id>/`) used to answer
on both with every handler it has. A handler written for the other route then
crashed binding its arguments: `GET milestone/` called `get(request)` on a
handler that needs `milestone_id`, and `GET task/dependency/<id>/` passed a
`dependency_id` that `get` does not take. Both were a TypeError, so a 500.
Each such route now names its methods (`as_view(http_method_names=...)`) and
the rest answer 405.
"""

import inspect

from django.test import SimpleTestCase
from rest_framework import status

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.urls.task import urls as task_urls


def _allowed(callback):
    view_class = callback.cls
    return callback.initkwargs.get("http_method_names", view_class.http_method_names)


def _handler(view_class, method):
    # Django answers HEAD with `get` when the class has no `head`.
    if method == "head" and not hasattr(view_class, "head"):
        method = "get"
    return getattr(view_class, method, None)


def mismatches(patterns):
    """(route, method) pairs whose handler cannot take the route's arguments."""
    found = []
    for pattern in patterns:
        callback = pattern.callback
        if not hasattr(callback, "cls"):
            continue
        route_kwargs = {name: object() for name in pattern.pattern.converters}
        for method in _allowed(callback):
            handler = _handler(callback.cls, method)
            if handler is None:
                continue  # no handler: DRF already answers 405
            try:
                inspect.signature(handler).bind(object(), object(), **route_kwargs)
            except TypeError:
                found.append((str(pattern.pattern), method))
    return found


class TaskRouteMethodBindingTests(SimpleTestCase):
    def test_every_allowed_method_binds_to_its_route(self):
        self.assertEqual(mismatches(task_urls.urlpatterns), [])

    def test_the_check_sees_a_mismatch(self):
        """Guards the test itself: an unrestricted two-route class is caught."""
        from django.urls import path

        from origin.views.task.milestone_views import MilestoneView

        loose = [
            path("m/", MilestoneView.as_view()),
            path("m/<int:milestone_id>/", MilestoneView.as_view()),
        ]
        self.assertEqual(
            sorted(mismatches(loose)),
            [
                ("m/", "delete"),
                ("m/", "get"),
                ("m/", "head"),
                ("m/", "patch"),
                ("m/<int:milestone_id>/", "post"),
            ],
        )


class TaskRouteMethodHttpTests(BaseAPITestCase):
    """The combinations that used to 500 now answer 405, and write nothing."""

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        a = TaskMaster.objects.create(
            team=self.team, project=self.project, title="A", status="Open"
        )
        b = TaskMaster.objects.create(
            team=self.team, project=self.project, title="B", status="Open"
        )
        self.dep = TaskDependency.objects.create(team=self.team, blocker_task=a, blocked_task=b)
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "M"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.milestone = MilestoneMaster.objects.get()

    def _sprint(self):
        import datetime

        return Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S",
            sequence_number=1,
            start_date=datetime.date(2026, 11, 2),
            end_date=datetime.date(2026, 11, 15),
        )

    def test_mismatched_combinations_answer_405(self):
        sprint = self._sprint()
        mid = self.milestone.milestone_id
        before = (
            TaskDependency.objects.count(),
            Sprint.objects.count(),
            MilestoneMaster.objects.count(),
        )
        for method, url in (
            ("get", f"/api/v2/task/dependency/{self.dep.id}/"),
            ("post", f"/api/v2/task/dependency/{self.dep.id}/"),
            ("patch", "/api/v2/sprint/"),
            ("delete", "/api/v2/sprint/"),
            ("post", f"/api/v2/sprint/{sprint.sprint_id}/"),
            ("get", "/api/v2/milestone/"),
            ("patch", "/api/v2/milestone/"),
            ("delete", "/api/v2/milestone/"),
            ("post", f"/api/v2/milestone/{mid}/"),
            ("delete", f"/api/v2/milestone/{mid}/assignees/"),
            ("post", f"/api/v2/milestone/{mid}/assignees/{self.user2.id}/"),
        ):
            with self.subTest(method=method, url=url):
                res = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(
            (
                TaskDependency.objects.count(),
                Sprint.objects.count(),
                MilestoneMaster.objects.count(),
            ),
            before,
        )

    def test_the_detail_route_still_reads_and_answers_preflight_methods(self):
        url = f"/api/v2/milestone/{self.milestone.milestone_id}/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.head(url).status_code, status.HTTP_200_OK)
        res = self.client.options(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {m.strip() for m in res["Allow"].split(",")},
            {"GET", "HEAD", "PATCH", "DELETE", "OPTIONS"},
        )
