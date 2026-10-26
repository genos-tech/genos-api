"""A task or milestone link cannot carry a `javascript:` URL (MIP-821).

CodeQL raised `js/xss-through-dom` on `DynamicURLManager`, which renders a link's
URL as `<a href={link.url}>`, and reading it showed the alert was right. The
browser's only check on entry was `new URL(x)`, which parses `javascript:...`
happily; the server stored whatever `links` it was sent; and a `javascript:` href
runs in the app's origin, with the clicker's session, when a teammate clicks a
link titled "Design doc".

The server now refuses any link whose scheme is not http(s), on all four
entrances: task create and update, milestone create and update. Pinned here:

1. **The parser reads the scheme the way a browser does.** Browsers strip leading
   control characters and spaces and delete tab / newline / carriage return
   anywhere in the URL before looking at the scheme, so a check on the raw string
   is exactly what `" java\\tscript:..."` beats.
2. **Real links are not refused.** A false refusal is a broken feature, so the
   legitimate shapes (https, http, mixed case, ports, query strings, fragments,
   relative and scheme-less values, unicode) are listed too.
3. **A refused link changes nothing**, and a stranger learns nothing.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.request_validators import link_scheme, refuse_unsafe_link_urls

User = get_user_model()

# Each of these is `javascript:` (or another script-capable scheme) to a browser.
DANGEROUS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "jAvAsCrIpT:alert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "\n\r\t javascript:alert(1)",
    "\x00javascript:alert(1)",
    "\x01\x02javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\rscript:alert(1)",
    "ja\tva\ns\rcript:alert(1)",
    " \tJa\nVa\rScRiPt:alert(document.cookie)",
    "javascript://%0aalert(1)",
    "javascript:/*http://x*/alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "DATA:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "mailto:a@b.co",
    "tel:+81000",
    "blob:https://example.com/uuid",
    "about:blank",
    "chrome://settings",
    "view-source:https://example.com",
    "custom+scheme.v1-x:payload",
]

# Each of these is something a person really pastes into a link field.
SAFE = [
    "https://example.com",
    "http://example.com",
    "HTTPS://EXAMPLE.COM/Path",
    "Http://example.com",
    "https://example.com:8443/a/b?c=d&e=f#frag",
    "https://github.com/owner/repo/pull/12",
    "https://app.genosai.dev/workspace/tasks/project/5/task/9",
    "https://example.com/a:b",
    "https://example.com/?next=javascript:alert(1)",
    "https://example.com/#javascript:alert(1)",
    "https://例え.jp/パス",
    "https://xn--r8jz45g.jp/",
    "  https://example.com  ",
    "\thttps://example.com",
    "https://exa\nmple.com",
    "https://[2001:db8::1]/x",
    "https://user@example.com/",
    # No scheme at all: a relative href, which is not a script risk. Whether to
    # refuse these is a product call this check does not make.
    "example.com",
    "example.com/path",
    "www.example.com",
    "/relative/path",
    "./a",
    "../a",
    "#anchor",
    "?query=1",
    "//example.com/x",
    "",
    "   ",
    "a b:c",  # a space before the colon: not a scheme
    "1http://x",  # a scheme must start with a letter
    "-x:y",
    "/path:with:colons",
    "?a=b:c",
    "#a:b",
]


class TheParserReadsTheSchemeLikeABrowser(SimpleTestCase):
    def test_every_script_capable_form_is_refused(self):
        for url in DANGEROUS:
            with self.subTest(url=url):
                res = refuse_unsafe_link_urls([{"url": url}])
                self.assertIsNotNone(res, f"{url!r} was allowed")
                self.assertEqual(res.status_code, 400)

    def test_every_real_link_shape_is_allowed(self):
        for url in SAFE:
            with self.subTest(url=url):
                self.assertIsNone(refuse_unsafe_link_urls([{"url": url}]), f"{url!r} was refused")

    def test_the_scheme_is_lowercased_and_found_through_the_noise(self):
        self.assertEqual(link_scheme(" \tJa\nVa\rScRiPt:alert(1)"), "javascript")
        self.assertEqual(link_scheme("HTTPS://x"), "https")
        self.assertEqual(link_scheme("x"), "")

    def test_a_colon_after_a_path_or_query_is_not_a_scheme(self):
        for url in ("/a:b", "?a:b", "#a:b", "a/b:c", "a?b:c"):
            with self.subTest(url=url):
                self.assertEqual(link_scheme(url), "")

    def test_a_scheme_must_start_with_a_letter_and_use_only_scheme_characters(self):
        for url in ("1x:y", "-x:y", "+x:y", ".x:y", "a b:c", "a_b:c", "a!b:c"):
            with self.subTest(url=url):
                self.assertEqual(link_scheme(url), "")

    def test_one_bad_link_among_good_ones_refuses_the_lot(self):
        links = [
            {"url": "https://ok.example.com"},
            {"url": "javascript:alert(1)"},
            {"url": "https://also-ok.example.com"},
        ]
        self.assertIsNotNone(refuse_unsafe_link_urls(links))

    def test_shapes_it_does_not_understand_are_left_alone(self):
        """It refuses a dangerous scheme; it does not police the payload."""
        for links in (
            None,
            [],
            (),
            {},
            "javascript:alert(1)",  # a string, not a list: not this check's business
            42,
            [None],
            ["javascript:alert(1)"],  # a list of strings, not dicts
            [{}],
            [{"title": "no url"}],
            [{"url": None}],
            [{"url": 7}],
            [{"url": ["javascript:alert(1)"]}],
            [{"url": {"a": 1}}],
            [{"u": "javascript:alert(1)"}],  # wrong key
        ):
            with self.subTest(links=links):
                self.assertIsNone(refuse_unsafe_link_urls(links))

    def test_the_other_link_fields_are_not_inspected(self):
        links = [{"url": "https://ok.example.com", "title": "javascript:alert(1)", "id": "x"}]
        self.assertIsNone(refuse_unsafe_link_urls(links))

    def test_it_is_fast_on_a_hostile_string(self):
        import time

        t0 = time.perf_counter()
        refuse_unsafe_link_urls([{"url": ("\t" * 50_000) + "https://example.com"}])
        refuse_unsafe_link_urls([{"url": "a" * 100_000}])
        self.assertLess(time.perf_counter() - t0, 1.0)


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
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate(self.user)

        self.outsider = User.objects.create_user(
            username="lnkout", email="lnkout@example.com", password="pw"
        )
        outsider_team = TeamMaster.objects.create(
            team_name="Out", team_email="lnkout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=outsider_team, attendee=self.outsider)

    BAD = [{"id": "link-1", "url": "javascript:alert(document.cookie)", "title": "Design doc"}]
    GOOD = [{"id": "link-1", "url": "https://example.com/design", "title": "Design doc"}]


class TaskLinkTests(_Base):
    def _payload(self, links):
        return {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "T",
            "priority": "Medium",
            "effort_level": "Medium",
            "status": "Open",
            "content": {"body": "x"},
            "due_date": "2026-12-31",
            "links": links,
            "tags": [],
            "is_init_task": False,
        }

    def _task(self, links=None):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Existing",
            status="Open",
            links=links,
        )

    def test_create_refuses_a_javascript_link_and_writes_nothing(self):
        before = TaskMaster.objects.count()

        res = self.client.post("/api/v2/task/", self._payload(self.BAD), format="json")

        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        self.assertIn("http", res.data["error"])
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_create_accepts_a_real_link(self):
        res = self.client.post("/api/v2/task/", self._payload(self.GOOD), format="json")

        self.assertEqual(res.status_code, 201, getattr(res, "data", None))
        task = TaskMaster.objects.get(task_id=res.data["task"]["task_id"])
        self.assertEqual(task.links, self.GOOD)

    def test_create_accepts_no_links(self):
        res = self.client.post("/api/v2/task/", self._payload([]), format="json")

        self.assertEqual(res.status_code, 201, getattr(res, "data", None))

    def test_update_refuses_a_javascript_link_and_changes_nothing(self):
        task = self._task(links=self.GOOD)

        res = self.client.put(
            "/api/v2/task/",
            {"task_id": task.task_id, "title": "Renamed", "links": self.BAD},
            format="json",
        )

        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        task.refresh_from_db()
        self.assertEqual(task.links, self.GOOD)
        self.assertEqual(task.title, "Existing")  # the rest of the payload did not land either

    def test_update_accepts_a_real_link(self):
        task = self._task()

        res = self.client.put(
            "/api/v2/task/", {"task_id": task.task_id, "links": self.GOOD}, format="json"
        )

        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        task.refresh_from_db()
        self.assertEqual(task.links, self.GOOD)

    def test_an_update_that_does_not_touch_links_is_unaffected(self):
        """A task already holding a bad link (stored before this check) can still
        be renamed: the check is on what is SENT, and the client does not resend
        links for a rename."""
        task = self._task(links=self.BAD)

        res = self.client.put(
            "/api/v2/task/", {"task_id": task.task_id, "title": "Renamed"}, format="json"
        )

        self.assertEqual(res.status_code, 200, getattr(res, "data", None))

    def test_a_stranger_gets_a_404_not_a_hint_about_the_link(self):
        task = self._task(links=self.GOOD)
        self.authenticate(self.outsider)

        res = self.client.put(
            "/api/v2/task/", {"task_id": task.task_id, "links": self.BAD}, format="json"
        )

        self.assertEqual(res.status_code, 404)
        self.assertNotIn("http", str(res.data))


class MilestoneLinkTests(_Base):
    def _milestone(self, links=None):
        return MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", reporter=self.user, links=links
        )

    def test_create_refuses_a_javascript_link_and_writes_nothing(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "planted", "links": self.BAD},
            format="json",
        )

        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        self.assertFalse(MilestoneMaster.objects.filter(title="planted").exists())

    def test_create_accepts_a_real_link(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "ok", "links": self.GOOD},
            format="json",
        )

        self.assertEqual(res.status_code, 201, getattr(res, "data", None))
        self.assertEqual(MilestoneMaster.objects.get(title="ok").links, self.GOOD)

    def test_update_refuses_a_javascript_link_and_changes_nothing(self):
        milestone = self._milestone(links=self.GOOD)

        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"title": "Renamed", "links": self.BAD},
            format="json",
        )

        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        milestone.refresh_from_db()
        self.assertEqual(milestone.links, self.GOOD)
        self.assertEqual(milestone.title, "M")

    def test_update_accepts_a_real_link(self):
        milestone = self._milestone()

        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/", {"links": self.GOOD}, format="json"
        )

        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        milestone.refresh_from_db()
        self.assertEqual(milestone.links, self.GOOD)

    def test_a_stranger_gets_a_404_on_create_and_update(self):
        milestone = self._milestone(links=self.GOOD)
        self.authenticate(self.outsider)

        created = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "x", "links": self.BAD},
            format="json",
        )
        patched = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/", {"links": self.BAD}, format="json"
        )

        self.assertEqual((created.status_code, patched.status_code), (404, 404))
