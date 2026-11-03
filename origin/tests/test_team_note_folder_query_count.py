"""`GET /api/v2/note/team/folder/` and `GET /api/v2/note/team/meta/`: a constant
number of queries (MIP-982).

Team Notes loads both on every page load and on the once-a-minute refresh. The
folder list cost one query PER FOLDER, twice over: the shared folder index
(`load_team_folder_index`) fetched its rows with `.only(...)` and left out the
two timestamps that the wire shape reads, so every folder went back to the
database for `ts_created_at` and again for `ts_updated_at`. Locally the extra
queries are microseconds; in production the median request made 73 of them.

Besides that per-folder cost, both routes loaded the same folder index more
than once (the folder list loaded the home team's twice, and every host team
that shared a folder was loaded twice on both routes).

Two kinds of test:

  * query counts: N spaces against 2N for four kinds of reader (the team owner,
    a plain member, a guest reading their own team's list, a guest reading the
    host's list), through the test client after `cache.clear()` the way
    production `db_n` counts (authentication included), with the budget pinned
    so a creeping extra query is a visible failure;
  * the payload: the exact response for a fixed fixture, for every kind of
    reader, captured from the unmodified views first
    (`golden/team_note_folders_response.json`). Ids and clocks change on every
    run, so they are replaced by readable names before the comparison;
    everything else, the order of keys and of lists included, is compared as it
    came.
"""

import json
import re
from pathlib import Path

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import ExternalGrant
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
    NoteFolderTagLink,
)
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services.external_grants import add_external_participants, offer_grant, respond_to_grant
from origin.services.member_roles import EDITOR
from origin.services.team_connection import request_connection, respond_to_connection
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

FOLDERS = "/api/v2/note/team/folder/"
META = "/api/v2/note/team/meta/"
GOLDEN = Path(__file__).parent / "golden" / "team_note_folders_response.json"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}(?:Z|[+-]\d{2}:\d{2})")

# Query budgets, counted through the test client the way production `db_n`
# counts. Folder list, per kind of reader:
#
#   owner of the team           1 auth
#                               3 the team's folders, their grants, "is the owner?"
#                               3 owner names, members per folder, tags per folder
#                               1 what other teams shared with this one (none)
#   plain member                the same, plus 1 "is a member?" (the owner check fails)
#   guest, own team's list      1 auth
#                               4 the home team's folders, grants, owner? member?
#                                 (the guest holds no folder there: nothing to list)
#                               2 shared folders: the grants, then which of them
#                                 this person holds a row on
#                               4 the host's folders, grants, owner? member? (the
#                                 index is loaded once and shared, not twice)
#                               1 the host's name
#                               3 owner names, members per folder, tags per folder
#   guest, host's own list      1 auth
#                               4 the host's folders, grants, owner? member?
#                               3 owner names, members per folder, tags per folder
#                               1 what other teams shared with the host (none)
#
# Notes list: the same resolution without the serialization and the host's name,
# plus one notes query per team that has readable folders.
FOLDER_BUDGET = {"owner": 8, "member": 9, "guest_own": 15, "guest_host": 9}
META_BUDGET = {"owner": 6, "member": 7, "guest_own": 12, "guest_host": 7}

# NOT constant in the number of HOST teams. Every other team that shares a folder with
# the reader's team is resolved on its own: its folders, the reader's grants in it,
# "is the owner?", "is a member?" (4 queries), and on the folder list also its owner
# names, members per folder and tags (3 more). Folders, notes and grants from the same
# host cost nothing extra; a second host costs this much, per route. Documented here so
# a change in either direction is a visible decision.
EXTRA_HOST_COST = {"folders": 7, "notes": 5}


def _show(sqls):
    return "\n".join(f"  {i + 1}. {sql[:150]}" for i, sql in enumerate(sqls))


class TeamNotesFixture(CrossTeamTestCase):
    """The helpers both test classes build their spaces with."""

    def _folder(self, name, *, owner, visibility=None, parent=None, grants=()):
        """A Team Notes folder in the host team, with the creator's Owner row (what
        `create_team_folder` writes) and any further `(user, role)` grants."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=owner,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
            parent_folder_id=parent.folder_id if parent is not None else None,
        )
        NoteFolderPermission.objects.create(
            team=self.team_a, folder=folder, user=owner, role_id=ROLE_OWNER
        )
        for user, role in grants:
            NoteFolderPermission.objects.create(
                team=self.team_a, folder=folder, user=user, role_id=role
            )
        return folder

    def _note(self, title, folder, owner):
        return PersonalNoteMaster.objects.create(
            team=self.team_a, owner=owner, title=title, body=[], folder_id=folder.folder_id
        )

    def _tag(self, name, *folders):
        tag, _ = NoteFolderTag.objects.get_or_create(
            team=self.team_a, name=name, defaults={"created_by": self.a_owner}
        )
        for folder in folders:
            NoteFolderTagLink.objects.create(folder=folder, tag=tag)
        return tag

    def _share_with_b(self, folder, *participants):
        grant = self.active_folder_grant(folder=folder)
        add_external_participants(grant, [u.id for u in participants], self.b_owner)
        return grant

    def _get(self, url, who, team):
        self.authenticate(who)
        return self._request(url, who, team)

    def _request(self, url, who, team):
        return self.client.get(url, {"team_id": str(team.team_id), "user_id": str(who.id)})


class TeamNotesQueryCountTests(TeamNotesFixture):
    """N spaces against 2N, read by each kind of reader."""

    N = 3

    def setUp(self):
        super().setUp()
        self.made = 0
        # (who, whose Team Notes list they ask for)
        self.readers = {
            "owner": (self.a_owner, self.team_a),
            "member": (self.a_viewer, self.team_a),
            "guest_own": (self.b_viewer, self.team_b),
            "guest_host": (self.b_viewer, self.team_a),
        }

    def _add_spaces(self, count):
        """`count` spaces: a public folder with an inheriting child and
        grandchild, a private folder with a roster and a narrowed subfolder, a
        folder shared with team B (with a subfolder), notes by several people in
        every one of them, and tags on some."""
        for _ in range(count):
            self.made += 1
            n = self.made
            handbook = self._folder(f"Handbook {n}", owner=self.a_owner, visibility="public")
            child = self._folder(f"Onboarding {n}", owner=self.a_editor, parent=handbook)
            day_one = self._folder(f"Day one {n}", owner=self.a_editor, parent=child)
            board = self._folder(
                f"Board {n}",
                owner=self.a_owner,
                visibility="private",
                grants=[(self.a_editor, ROLE_EDITOR), (self.a_viewer, ROLE_VIEWER)],
            )
            minutes = self._folder(
                f"Minutes {n}", owner=self.a_owner, visibility="private", parent=board
            )
            shared = self._folder(f"Shared {n}", owner=self.a_owner, visibility="private")
            shared_kid = self._folder(f"Shared kid {n}", owner=self.a_owner, parent=shared)

            self._tag("eng", child, shared)
            self._tag(f"tag {n}", handbook, day_one)

            for folder in (handbook, child, day_one, board, minutes, shared, shared_kid):
                self._note(f"By owner {folder.folder_id}", folder, self.a_owner)
                self._note(f"By editor {folder.folder_id}", folder, self.a_editor)

            self._share_with_b(shared, self.b_viewer, self.b_editor)

    def _measure(self, url):
        """{reader: (queries, rows)} and {reader: the SQL}."""
        counts, sqls = {}, {}
        for name, (who, team) in self.readers.items():
            # Signing in writes a token row: that is the test's doing, not the request's.
            self.authenticate(who)
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                res = self._request(url, who, team)
            self.assertEqual(res.status_code, 200, res.content[:300])
            counts[name] = (len(ctx), len(res.json()))
            sqls[name] = [q["sql"] for q in ctx.captured_queries]
        return counts, sqls

    def _assert_constant(self, url, budget):
        self._add_spaces(self.N)
        small, _ = self._measure(url)
        self._add_spaces(self.N)
        large, sqls = self._measure(url)

        # The fixture is only worth anything if every reader actually reads rows,
        # and more of them the second time.
        for name in self.readers:
            self.assertGreater(small[name][1], 0, f"{name} reads nothing")
            self.assertGreater(large[name][1], small[name][1], f"{name} rows did not grow")

        small_counts = {name: count for name, (count, _) in small.items()}
        large_counts = {name: count for name, (count, _) in large.items()}
        grew = [name for name in small_counts if small_counts[name] != large_counts[name]]
        self.assertEqual(
            small_counts,
            large_counts,
            "grew with the data for "
            + ", ".join(grew)
            + ":\n"
            + "\n".join(f"{name}:\n{_show(sqls[name])}" for name in grew),
        )
        off = [name for name in budget if large_counts[name] != budget[name]]
        self.assertEqual(
            large_counts,
            budget,
            "over or under budget for "
            + ", ".join(off)
            + ":\n"
            + "\n".join(f"{name}:\n{_show(sqls[name])}" for name in off),
        )

    def test_the_folder_list_count_does_not_grow_with_the_number_of_folders(self):
        self._assert_constant(FOLDERS, FOLDER_BUDGET)

    def test_the_notes_list_count_does_not_grow_with_folders_notes_or_grants(self):
        self._assert_constant(META, META_BUDGET)

    def _share_from_second_host(self):
        """A folder in team C, shared with team B and open to b_viewer."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team_c,
            owner=self.c_owner,
            name="From the other host",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        NoteFolderPermission.objects.create(
            team=self.team_c, folder=folder, user=self.c_owner, role_id=ROLE_OWNER
        )
        PersonalNoteMaster.objects.create(
            team=self.team_c,
            owner=self.c_owner,
            title="Host C note",
            body=[],
            folder_id=folder.folder_id,
        )
        connection_row = request_connection(self.team_c.team_id, self.team_b.team_id, self.c_owner)
        respond_to_connection(connection_row, self.b_owner, accept=True)
        grant = offer_grant(
            owner_team_id=self.team_c.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.NOTE_FOLDER,
            object_id=folder.folder_id,
            role_ceiling=EDITOR,
            actor=self.c_owner,
        )
        grant = respond_to_grant(grant, self.b_owner, accept=True)
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)

    def test_a_second_host_team_costs_a_fixed_amount(self):
        """The one dimension that is still linear: the number of host teams."""
        self._add_spaces(2)
        before_folders, _ = self._measure(FOLDERS)
        before_notes, _ = self._measure(META)
        self._share_from_second_host()
        after_folders, _ = self._measure(FOLDERS)
        after_notes, _ = self._measure(META)

        self.assertEqual(after_folders["guest_own"][1], before_folders["guest_own"][1] + 1)
        self.assertEqual(
            after_folders["guest_own"][0] - before_folders["guest_own"][0],
            EXTRA_HOST_COST["folders"],
        )
        self.assertEqual(
            after_notes["guest_own"][0] - before_notes["guest_own"][0], EXTRA_HOST_COST["notes"]
        )
        # Readers who are nobody's guest do not pay for it.
        for name in ("owner", "member", "guest_host"):
            self.assertEqual(after_folders[name][0], before_folders[name][0], name)
            self.assertEqual(after_notes[name][0], before_notes[name][0], name)

    def test_the_folder_rows_do_not_go_back_for_their_timestamps(self):
        """The cause, named: no `ts_created_at` / `ts_updated_at` lookup by folder id."""
        self._add_spaces(1)
        self.authenticate(self.a_owner)
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            res = self._request(FOLDERS, self.a_owner, self.team_a)
        self.assertEqual(res.status_code, 200)
        by_id = [
            q["sql"]
            for q in ctx.captured_queries
            if 'FROM "origin_personalnotefolder" WHERE "origin_personalnotefolder"."folder_id" = '
            in q["sql"]
        ]
        self.assertEqual(by_id, [], _show(by_id))
        first = res.json()[0]
        self.assertTrue(first["tsCreated"] and first["tsUpdated"])


class TeamNotesPayloadTests(TeamNotesFixture):
    """The exact responses for a fixed fixture, for every kind of reader."""

    def setUp(self):
        super().setUp()

        # Public tree: the whole team reaches it.
        self.handbook = self._folder("Handbook", owner=self.a_owner, visibility="public")
        self.onboarding = self._folder("Onboarding", owner=self.a_editor, parent=self.handbook)
        self.day_one = self._folder("Day one", owner=self.a_editor, parent=self.onboarding)
        # Private with a roster: the editor and one plain member are on it.
        self.board = self._folder(
            "Board",
            owner=self.a_owner,
            visibility="private",
            grants=[(self.a_editor, ROLE_EDITOR), (self.a_viewer, ROLE_VIEWER)],
        )
        # A subfolder that narrows the roster back down to its creator.
        self.minutes = self._folder(
            "Minutes", owner=self.a_owner, visibility="private", parent=self.board
        )
        # Private to its creator: even the team owner is not on it.
        self.secret = self._folder("Secret", owner=self.a_editor, visibility="private")
        # An inheriting subfolder that adds one member to a private parent.
        self.drafts = self._folder(
            "Drafts", owner=self.a_owner, parent=self.board, grants=[(self.a_editor, ROLE_VIEWER)]
        )
        # Shared with team B, with an inheriting subfolder.
        self.shared = self._folder("Shared with B", owner=self.a_owner, visibility="private")
        self.shared_notes = self._folder("Shared notes", owner=self.a_owner, parent=self.shared)
        # Private to the host and never shared: team B must not see it.
        self.internal = self._folder("Internal", owner=self.a_owner, visibility="private")

        self.tag_eng = self._tag("eng", self.onboarding, self.shared)
        self.tag_ops = self._tag("ops", self.onboarding, self.board)

        self.notes = {}
        for folder, who, label in (
            (self.handbook, self.a_owner, "Welcome"),
            (self.handbook, self.a_editor, "Style guide"),
            (self.onboarding, self.a_viewer, "First week"),
            (self.day_one, self.a_editor, "Laptop setup"),
            (self.board, self.a_owner, "Agenda"),
            (self.minutes, self.a_owner, "Closed minutes"),
            (self.secret, self.a_editor, "Private thoughts"),
            (self.drafts, self.a_owner, "Draft agenda"),
            (self.shared, self.a_owner, "Kickoff"),
            (self.shared_notes, self.a_owner, "Host note"),
            (self.internal, self.a_owner, "Salaries"),
        ):
            self.notes[label] = self._note(label, folder, who)

        self._share_with_b(self.shared, self.b_viewer)
        # A note the guest wrote into the host's shared folder.
        self.notes["Guest note"] = self._note("Guest note", self.shared, self.b_viewer)

        self.readers = {
            "owner": (self.a_owner, self.team_a),
            "editor": (self.a_editor, self.team_a),
            "member": (self.a_viewer, self.team_a),
            "stranger": (self.c_owner, self.team_a),
            "guest_own": (self.b_viewer, self.team_b),
            "guest_host": (self.b_viewer, self.team_a),
            "colleague_never_admitted": (self.b_editor, self.team_b),
            "colleague_host": (self.b_editor, self.team_a),
        }

    # ------------------------------------------------------------------

    def _names(self):
        """Every id the responses can carry, as a readable name."""
        users = {
            self.a_owner: "a_owner",
            self.a_editor: "a_editor",
            self.a_viewer: "a_viewer",
            self.b_owner: "b_owner",
            self.b_viewer: "b_viewer",
            self.b_editor: "b_editor",
            self.c_owner: "c_owner",
        }
        ids = {str(user.id): name for user, name in users.items()}
        ids[str(self.team_a.team_id)] = "team_a"
        ids[str(self.team_b.team_id)] = "team_b"
        folders = {f.folder_id: f.name for f in PersonalNoteFolder.objects.filter(scope="team")}
        notes = {n.note_id: n.title for n in PersonalNoteMaster.objects.all()}
        tags = {t.tag_id: t.name for t in NoteFolderTag.objects.all()}
        return ids, folders, notes, tags

    def _normalized(self, payload):
        ids, folders, notes, tags = self._names()
        by_key = {
            "folderId": ("folder", folders),
            "parentFolderId": ("folder", folders),
            "noteId": ("note", notes),
            "parentNoteId": ("note", notes),
            "tagId": ("tag", tags),
        }

        def walk(value, key=None):
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, dict):
                return {k: walk(v, k) for k, v in value.items()}
            if key in by_key and value is not None:
                kind, names = by_key[key]
                return f"{kind}:{names[value]}"
            if key in ("ownerId", "hostTeamId") and value is not None:
                return ids[value]
            if key in ("tsCreated", "tsUpdated"):
                # The clock changes on every run; its FORMAT is part of the contract.
                return "<time>" if STAMP.fullmatch(value) else value
            return value

        return walk(payload)

    def _responses(self):
        if hasattr(self, "_responses_cache"):
            return self._responses_cache
        out = {}
        for name, (who, team) in self.readers.items():
            for route, url in (("folders", FOLDERS), ("notes", META)):
                cache.clear()
                res = self._get(url, who, team)
                self.assertEqual(res.status_code, 200, (name, route, res.content[:300]))
                out[f"{route}/{name}"] = self._normalized(res.json())
        self._responses_cache = out
        return out

    def _names_in(self, route, reader):
        key = "name" if route == "folders" else "title"
        rows = self._responses()[f"{route}/{reader}"]
        return [row[key] for row in rows]

    # ------------------------------------------------------------------

    def test_the_responses_are_exactly_what_they_were(self):
        """Byte for byte (keys in order too) against the responses captured from
        the views before they were made cheaper. Every kind of reader, both
        routes."""
        got = json.dumps(self._responses(), indent=1)

        self.assertEqual(got, GOLDEN.read_text().strip())

    def test_who_sees_which_folders(self):
        """The same facts, readable: roles decide what is listed. The golden file
        pins the full shape; this pins the permission outcome so a regression
        reads as a sentence."""

        def names(reader):
            return sorted(self._names_in("folders", reader))

        everyone_public = ["Day one", "Handbook", "Onboarding"]
        self.assertEqual(
            names("owner"),
            sorted(
                [
                    *everyone_public,
                    "Board",
                    "Drafts",
                    "Internal",
                    "Minutes",
                    "Shared Folder",  # the base fixture's: private, reached as its creator
                    "Shared notes",
                    "Shared with B",
                ]
            ),
        )
        # The editor is on the Board roster (and the inheriting Drafts below it);
        # the narrowed Minutes and the owner's private folders are not theirs.
        self.assertEqual(names("editor"), sorted([*everyone_public, "Board", "Drafts", "Secret"]))
        # A plain member with a viewer row on Board: the public tree, Board and the
        # inheriting Drafts under it, but not the narrowed Minutes.
        self.assertEqual(names("member"), sorted([*everyone_public, "Board", "Drafts"]))
        self.assertEqual(names("stranger"), [])
        # The guest reads the host's shared folder and its subfolder, in both lists.
        self.assertEqual(names("guest_own"), ["Shared notes", "Shared with B"])
        self.assertEqual(names("guest_host"), ["Shared notes", "Shared with B"])
        # A colleague whose team holds the grant but who was never admitted gets nothing.
        self.assertEqual(names("colleague_never_admitted"), [])
        self.assertEqual(names("colleague_host"), [])

    def test_who_sees_which_notes(self):

        def titles(reader):
            return sorted(self._names_in("notes", reader))

        self.assertEqual(
            titles("member"),
            sorted(
                ["Agenda", "Draft agenda", "First week", "Laptop setup", "Style guide", "Welcome"]
            ),
        )
        self.assertEqual(
            titles("editor"),
            sorted(
                [
                    "Agenda",
                    "Draft agenda",
                    "First week",
                    "Laptop setup",
                    "Private thoughts",
                    "Style guide",
                    "Welcome",
                ]
            ),
        )
        self.assertEqual(titles("stranger"), [])
        self.assertEqual(titles("guest_own"), ["Guest note", "Host note", "Kickoff"])
        self.assertEqual(titles("guest_host"), ["Guest note", "Host note", "Kickoff"])
        self.assertEqual(titles("colleague_never_admitted"), [])

    def test_the_guest_is_marked_external_in_their_own_list_only(self):
        responses = self._responses()
        own = responses["folders/guest_own"]
        self.assertTrue(all(row["isExternal"] for row in own))
        self.assertEqual({row["hostTeamId"] for row in own}, {"team_a"})
        # The share itself is re-rooted; its subfolder keeps its real parent.
        self.assertEqual(
            {row["name"]: row["parentFolderId"] for row in own},
            {"Shared with B": None, "Shared notes": "folder:Shared with B"},
        )
        self.assertTrue(all("isExternal" not in row for row in responses["folders/guest_host"]))
