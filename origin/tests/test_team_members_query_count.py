"""`GET /api/v2/team/getTeamMembers/`: a constant number of queries (MIP-966).

The roster is the client's only people directory, so everyone pays for it on
every boot and team switch. A member's answer also lists the other teams'
people they share work with (`external_people_for_member`), and an outside
caller's narrowed roster is built from the shared objects they sit on
(`external_visible_user_ids`). Both used to cost a handful of queries PER
GRANT, so the endpoint grew with the number of cross-team shares and not with
the number of members.

This file does three jobs:

  * WHO SEES WHOM, pinned: a rich world (members, guests, an owner with no
    membership row, a project owner with no member row, grants of every object
    type, reached and not reached, active / pending / declined / revoked,
    grants whose two sides are in different teams, a deleted team, one person
    on two grants) is asked every question the two helpers and the endpoint
    can be asked, as every caller, and the answers are compared with
    `golden/team_members_external_people.txt`, which was captured from the code
    BEFORE it was made cheaper. A change that only makes the roster cheaper
    cannot change who is on it;
  * malformed grant ids, pinned as they behave today (see the last class);
  * query counts: N shares against 2N, for each kind of caller.

Ids and timestamps change on every run, so ids are replaced by the fixture's
own names before the comparison and clocks by a placeholder. Four people sit on
grants that put them in two different teams; which team the roster shows is
whichever grant the database returns first, and the database promises no
order, so their home team is compared in a separate test against the grants
read back in the order the database actually gave them.
"""

import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import (
    ExternalGrant,
    ShareStatus,
    TeamMaster,
    TeamMembers,
)
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.external_grants import offer_grant, respond_to_grant, revoke_grant
from origin.services.member_roles import EDITOR, VIEWER
from origin.services.team_connection import (
    get_connection,
    request_connection,
    respond_to_connection,
)
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.scope_guards import (
    external_people_for_member,
    external_visible_user_ids,
)

User = get_user_model()

TEAM_MEMBERS = "/api/v2/team/getTeamMembers/"
GOLDEN = Path(__file__).parent / "golden" / "team_members_external_people.txt"

CHANNEL = ExternalGrant.ObjectType.CHANNEL
PROJECT = ExternalGrant.ObjectType.PROJECT
FOLDER = ExternalGrant.ObjectType.NOTE_FOLDER

# People who belong to two teams and sit on grants naming each of them. Which
# team the roster shows for them is decided by grant order, not by the data.
FIRST_WINS = ("bc_both1", "bc_both2", "bc_same", "bc_rev")
BY_GRANT_ORDER = "<by grant order>"


class SharingWorld(CrossTeamTestCase):
    """Helpers for building cross-team shares; no tests of its own beyond
    what subclasses add."""

    def connect(self, team_x, owner_x, team_y, owner_y):
        existing = get_connection(team_x.team_id, team_y.team_id)
        if existing is not None and existing.status == ShareStatus.ACTIVE:
            return
        conn = request_connection(team_x.team_id, team_y.team_id, owner_x)
        respond_to_connection(conn, owner_y, accept=True)

    def share(self, kind, obj, owner_team, owner_actor, guest_team, guest_actor, answer="accept"):
        """Offer `obj` to `guest_team` and settle it. `accept` also admits the
        approver (guest_actor), as the product does. `revoke` accepts and then
        revokes, which removes the approver's row again."""
        self.connect(owner_team, owner_actor, guest_team, guest_actor)
        object_id = (
            obj.project_id if kind == PROJECT else obj.folder_id if kind == FOLDER else obj.id
        )
        grant = offer_grant(
            owner_team_id=owner_team.team_id,
            guest_team_id=guest_team.team_id,
            object_type=kind,
            object_id=object_id,
            role_ceiling=EDITOR,
            actor=owner_actor,
        )
        if answer in ("accept", "revoke"):
            grant = respond_to_grant(grant, guest_actor, accept=True)
        elif answer == "decline":
            grant = respond_to_grant(grant, guest_actor, accept=False)
        if answer == "revoke":
            revoke_grant(grant)
        return grant

    def new_project(self, team, owner, name):
        return ProjectMaster.objects.create(
            team=team, project_name=name, owner=owner, project_system_user=owner
        )

    def new_folder(self, team, owner, name):
        return PersonalNoteFolder.objects.create(
            team=team,
            owner=owner,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )

    def new_channel(self, team, owner, title):
        channel = Channel.objects.create(
            team=team,
            kind=ChannelKind.GM,
            title=title,
            owner=owner,
            is_private=True,
            is_external=True,
        )
        # The offerer must be in the chat they lend out (MIP-1522).
        ChannelMember.objects.get_or_create(channel=channel, user=owner)
        return channel

    def put_on(self, obj, *users):
        """Hand-written participation rows, as the roster editors write them."""
        for user in users:
            if isinstance(obj, Channel):
                ChannelMember.objects.get_or_create(channel=obj, user=user)
            elif isinstance(obj, ProjectMaster):
                ProjectMembers.objects.get_or_create(
                    project=obj, attendee=user, defaults={"team": obj.team}
                )
            else:
                NoteFolderPermission.objects.get_or_create(
                    folder=obj, user=user, defaults={"team": obj.team, "role_id": 3}
                )

    def roster_of(self, grant):
        """The user ids holding a row on the grant's object, read straight
        from the tables (not through the code under test)."""
        if grant.object_type == CHANNEL:
            rows = ChannelMember.objects.filter(
                channel_id=grant.object_id, is_deleted=False
            ).values_list("user_id", flat=True)
        elif grant.object_type == PROJECT:
            rows = ProjectMembers.objects.filter(project_id=grant.object_id).values_list(
                "attendee_id", flat=True
            )
        else:
            rows = NoteFolderPermission.objects.filter(folder_id=grant.object_id).values_list(
                "user_id", flat=True
            )
        return {str(uid) for uid in rows}


class RichWorld(SharingWorld):
    """Five teams, 26 people, 20 grants. See the comments for what each is for."""

    def setUp(self):
        super().setUp()
        a, b, c = self.team_a, self.team_b, self.team_c
        a_owner, a_editor, a_viewer = self.a_owner, self.a_editor, self.a_viewer
        b_owner, b_editor, b_viewer = self.b_owner, self.b_editor, self.b_viewer
        c_owner = self.c_owner

        # The base fixture's two users are called testuser / otheruser; the
        # golden file reads better when every name says which team it is in.
        for user, name in ((a_owner, "a_owner"), (a_viewer, "a_viewer")):
            user.username = name
            user.save(update_fields=["username"])

        # More people. The fixture's own three teams have 1-3 members each.
        self.a_plain = self._member(a, "a_plain", VIEWER)
        self.b_extra = self._member(b, "b_extra", VIEWER)
        self.c_member = self._member(c, "c_member", EDITOR)
        self.third_party = self._member(c, "third_party", VIEWER)  # C, but sits on A->B objects
        self.deleted_user = self._member(b, "deleted_user", VIEWER)
        self.b_gone = self._member(b, "b_gone", VIEWER)  # left B (soft-deleted row)
        self.pending_only = self._member(b, "pending_only", VIEWER)
        self.revoked_only = self._member(c, "revoked_only", VIEWER)
        self.declined_only = self._member(c, "declined_only", VIEWER)
        self.bc_both1 = self._member(b, "bc_both1", VIEWER)
        self.bc_both2 = self._member(b, "bc_both2", VIEWER)
        for person in (self.bc_both1, self.bc_both2):
            TeamMembers.objects.create(team=c, attendee=person, member_role=VIEWER)
        # In the host team AND a guest team: a colleague of the host, so never
        # listed as an outsider, whichever side asks.
        self.ab_both = self._member(a, "ab_both", VIEWER)
        TeamMembers.objects.create(team=b, attendee=self.ab_both, member_role=VIEWER)
        # In both guest teams, on ONE project shared to each of them in turn.
        self.bc_same = self._member(b, "bc_same", VIEWER)
        TeamMembers.objects.create(team=c, attendee=self.bc_same, member_role=VIEWER)
        # In both guest teams, on a chat shared to B and (later) a project shared to C:
        # the chat comes first although "channel" sorts before "project" the other way.
        self.bc_rev = self._member(b, "bc_rev", VIEWER)
        TeamMembers.objects.create(team=c, attendee=self.bc_rev, member_role=VIEWER)
        self.p6_only = self._member(b, "p6_only", VIEWER)
        self.proj_guest = self._user("proj_guest")  # in no team at all: a project guest of A
        self.e_owner = self._user("e_owner")
        self.team_e = TeamMaster.objects.create(
            team_name="Epsilon Team", team_email="epsilon@example.com", owner=self.e_owner
        )
        TeamMembers.objects.create(team=self.team_e, attendee=self.e_owner)
        # Team D: its owner holds NO TeamMembers row (the owner is a member
        # anyway), and the team is soft-deleted at the end of the build.
        self.d_owner = self._user("d_owner")
        self.team_d = TeamMaster.objects.create(
            team_name="Delta Team", team_email="delta@example.com", owner=self.d_owner
        )
        self.d_member = self._member(self.team_d, "d_member", VIEWER)

        TeamMembers.objects.filter(attendee=self.b_gone, team=b).update(is_deleted=True)
        User.objects.filter(pk=self.deleted_user.pk).update(is_deleted=True)

        # Profile data worth carrying through the exact-JSON comparison.
        User.objects.filter(pk=b_owner.pk).update(
            role="Designer", base_country="JP", about_me="Runs the guest team"
        )
        User.objects.filter(pk=b_editor.pk).update(
            current_location="Berlin", location_shared=False, custom_status="away"
        )
        User.objects.filter(pk=self.c_member.pk).update(
            current_location="Lagos", timezone="Africa/Lagos"
        )
        for team, image in ((a, "alpha"), (b, "beta"), (c, "gamma")):
            TeamMaster.objects.filter(pk=team.pk).update(
                profile_image_file_name=f"teams/{image}.png"
            )

        # ── A's objects shared OUT. Creation order matters: bc_both1 is on the
        # first two grants and bc_both2 on the second and third.
        self.p1 = self.new_project(a, a_owner, "P1")
        self.share(PROJECT, self.p1, a, a_owner, b, b_owner)  # accept admits b_owner
        self.put_on(
            self.p1,
            a_owner, a_editor, b_editor, self.bc_both1, self.third_party,
            self.deleted_user, self.b_gone, self.proj_guest, self.ab_both,
        )  # fmt: skip

        # P2: owned by a_viewer, who holds NO ProjectMembers row on it.
        self.p2 = self.new_project(a, a_viewer, "P2")
        self.share(PROJECT, self.p2, a, a_owner, c, c_owner)
        self.put_on(self.p2, a_owner, a_editor, self.c_member)

        self.ch2 = self.new_channel(a, a_owner, "CH2")
        self.share(CHANNEL, self.ch2, a, a_owner, c, c_owner)
        self.put_on(self.ch2, a_owner, self.bc_both1, self.bc_both2)

        self.ch1 = self.new_channel(a, a_owner, "CH1")
        self.share(CHANNEL, self.ch1, a, a_owner, b, b_owner)
        self.put_on(self.ch1, a_owner, b_viewer, self.bc_both2, self.third_party, self.a_plain)
        ChannelMember.objects.filter(channel=self.ch1, user=self.a_plain).update(is_deleted=True)

        # F1: a_owner (who owns the folder) holds no permission row on it.
        self.f1 = self.new_folder(a, a_owner, "F1")
        self.share(FOLDER, self.f1, a, a_owner, b, b_owner)
        self.put_on(self.f1, a_editor, b_editor)

        # P4 goes to team D, which is deleted below.
        self.p4 = self.new_project(a, a_owner, "P4")
        self.share(PROJECT, self.p4, a, a_owner, self.team_d, self.d_owner)
        self.put_on(self.p4, a_owner, self.d_member)
        # ...and so does a chat: d_owner is a member of D through ownership alone.
        self.ch5 = self.new_channel(a, a_owner, "CH5")
        self.share(CHANNEL, self.ch5, a, a_owner, self.team_d, self.d_owner)
        self.put_on(self.ch5, a_owner, self.d_member)

        # Ids stored the way a client may have sent them: zero-padded, upper case.
        self.p5 = self.new_project(a, a_owner, "P5")
        grant = self.share(PROJECT, self.p5, a, a_owner, b, b_owner)
        self.put_on(self.p5, a_owner, self.b_extra)
        ExternalGrant.objects.filter(pk=grant.pk).update(object_id=f"{self.p5.project_id:010d}")
        self.ch4 = self.new_channel(a, a_owner, "CH4")
        grant = self.share(CHANNEL, self.ch4, a, a_owner, b, b_owner)
        self.put_on(self.ch4, a_owner, self.b_extra, self.bc_rev)
        ExternalGrant.objects.filter(pk=grant.pk).update(object_id=str(self.ch4.id).upper())

        # A soft-deleted project is still shared: nothing here has ever looked.
        self.p6 = self.new_project(a, a_owner, "P6")
        self.share(PROJECT, self.p6, a, a_owner, b, b_owner)
        self.put_on(self.p6, a_owner, self.p6_only)
        ProjectMaster.objects.filter(pk=self.p6.pk).update(is_deleted=True)

        # One project, two grants (B first, then C), one person in both teams.
        self.p7 = self.new_project(a, a_owner, "P7")
        self.share(PROJECT, self.p7, a, a_owner, b, b_owner)
        self.share(PROJECT, self.p7, a, a_owner, c, c_owner)
        self.put_on(self.p7, a_owner, self.bc_same)
        self.p8 = self.new_project(a, a_owner, "P8")
        self.share(PROJECT, self.p8, a, a_owner, c, c_owner)
        self.put_on(self.p8, a_owner, self.bc_rev)

        # ── Grants that must not count, with participation rows still behind them.
        self.p3 = self.new_project(a, a_owner, "P3")
        self.share(PROJECT, self.p3, a, a_owner, b, b_owner, answer="pending")
        self.put_on(self.p3, a_owner, self.pending_only)
        self.f2 = self.new_folder(a, a_owner, "F2")
        self.share(FOLDER, self.f2, a, a_owner, c, c_owner, answer="revoke")
        self.put_on(self.f2, a_owner, self.revoked_only)
        self.ch3 = self.new_channel(a, a_owner, "CH3")
        self.share(CHANNEL, self.ch3, a, a_owner, c, c_owner, answer="decline")
        self.put_on(self.ch3, a_owner, self.declined_only)

        # ── Shared INTO A (A is the guest): the same helpers, other direction.
        self.pb1 = self.new_project(b, b_owner, "PB1")
        self.share(PROJECT, self.pb1, b, b_owner, a, a_owner)
        self.put_on(self.pb1, b_owner, b_editor, a_viewer)
        self.chb = self.new_channel(b, b_owner, "CHB")
        self.share(CHANNEL, self.chb, b, b_owner, a, a_owner)
        self.put_on(self.chb, b_owner, b_viewer, a_editor)
        self.fb = self.new_folder(b, b_owner, "FB")
        self.share(FOLDER, self.fb, b, b_owner, a, a_owner)
        self.put_on(self.fb, b_editor)
        # ...and one between B and C that A is not part of.
        self.pbc = self.new_project(b, b_owner, "PBC")
        self.share(PROJECT, self.pbc, b, b_owner, c, c_owner)
        self.put_on(self.pbc, b_owner)

        TeamMaster.objects.filter(pk=self.team_d.pk).update(is_deleted=True)

        self.teams = [a, b, c, self.team_d, self.team_e]
        self.users = [
            a_owner, a_editor, a_viewer, self.a_plain,
            b_owner, b_editor, b_viewer, self.b_extra, self.b_gone, self.deleted_user,
            self.pending_only, self.bc_both1, self.bc_both2, self.bc_same, self.bc_rev, self.ab_both,
            self.p6_only, c_owner, self.c_member, self.third_party, self.revoked_only, self.declined_only,
            self.d_owner, self.d_member, self.e_owner, self.proj_guest,
        ]  # fmt: skip
        self.names = {str(u.id): u.username for u in self.users}
        self.team_labels = {str(t.team_id): t.team_name for t in self.teams}

    # ── rendering ──────────────────────────────────────────────────────

    STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")

    def label(self, text):
        for key, value in {**self.names, **self.team_labels}.items():
            text = text.replace(key, value)
        return self.STAMP.sub("<time>", text)

    def home(self, username, value):
        return BY_GRANT_ORDER if username in FIRST_WINS else value

    def ask(self, viewer, team):
        self.authenticate(viewer)
        return self.client.get(
            TEAM_MEMBERS,
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "user_id": str(viewer.id),
            },
        )

    def full_rows(self, response):
        """The rows as sent, each group sorted by name: the team's own people
        come from a query ordered by the database's collation, and the other
        teams' people from one that is not ordered at all, and neither is what
        this test is about."""
        rows = response.json()["data"]["members"]
        own = sorted((r for r in rows if not r.get("isExternal")), key=lambda r: r["userName"])
        external = sorted((r for r in rows if r.get("isExternal")), key=lambda r: r["userName"])
        for row in external:
            if row["userName"] in FIRST_WINS:
                for key in ("homeTeamId", "homeTeamName", "homeTeamImgPath"):
                    row[key] = BY_GRANT_ORDER
        return own + external

    def render(self):
        out = []
        out.append("# external_people_for_member(team, viewer): person -> their own team")
        for team in self.teams:
            for viewer in self.users:
                people = external_people_for_member(team.team_id, viewer.id)
                shown = {
                    self.names[uid]: self.home(
                        self.names[uid], self.team_labels.get(home["teamId"], home["teamId"])
                    )
                    for uid, home in people.items()
                }
                names = {
                    self.names[uid]: self.home(self.names[uid], home["teamName"])
                    for uid, home in people.items()
                }
                out.append(
                    f"{viewer.username} @ {team.team_name}: "
                    + json.dumps(shown, sort_keys=True)
                    + " names="
                    + json.dumps(names, sort_keys=True)
                )
        out.append("# external_visible_user_ids(team, viewer)")
        for team in self.teams:
            for viewer in self.users:
                seen = external_visible_user_ids(team.team_id, viewer.id)
                out.append(
                    f"{viewer.username} @ {team.team_name}: "
                    + json.dumps(sorted(self.names[uid] for uid in seen))
                )
        out.append(
            "# getTeamMembers, every caller x every team: status, then (name, external, role, home)"
        )
        for team in self.teams:
            for viewer in self.users:
                res = self.ask(viewer, team)
                if res.status_code != 200:
                    out.append(f"{viewer.username} @ {team.team_name}: {res.status_code}")
                    continue
                cells = sorted(
                    (
                        r["userName"],
                        bool(r.get("isExternal")),
                        r["memberRole"],
                        self.home(r["userName"], r.get("homeTeamName")),
                    )
                    for r in res.json()["data"]["members"]
                )
                out.append(f"{viewer.username} @ {team.team_name}: 200 " + json.dumps(cells))
        out.append("# getTeamMembers, the whole response (key order included)")
        for who, where in (
            ("a_owner", "Test Team"), ("a_editor", "Test Team"), ("a_viewer", "Test Team"),
            ("a_plain", "Test Team"), ("proj_guest", "Test Team"), ("b_owner", "Test Team"),
            ("b_viewer", "Test Team"), ("b_owner", "Guest Team"), ("c_owner", "Stranger Team"),
            ("d_owner", "Delta Team"), ("b_owner", "Delta Team"), ("e_owner", "Test Team"),
        ):  # fmt: skip
            viewer = next(u for u in self.users if u.username == who)
            team = next(t for t in self.teams if t.team_name == where)
            res = self.ask(viewer, team)
            out.append(f"## {who} @ {where}: {res.status_code}")
            if res.status_code != 200:
                out.append(self.label(json.dumps(res.json())))
                continue
            body = res.json()
            rows = self.full_rows(res)
            out.append(self.label(json.dumps({k: v for k, v in body.items() if k != "data"})))
            for row in rows:
                out.append(self.label(json.dumps(row)))
        return "\n".join(out) + "\n"


class WhoSeesWhomTests(RichWorld):
    """The exact answers, as they were before the roster was made cheaper."""

    def test_the_answers_are_exactly_what_they_were(self):
        got = self.render()
        want = GOLDEN.read_text()
        self.assertEqual(got.splitlines(), want.splitlines())
        self.assertEqual(got, want)

    def test_the_world_reaches_every_kind_of_grant(self):
        """The golden is only worth something if the fixture exercises each
        branch; check the fixture, not the code under test."""
        grants = ExternalGrant.objects.all()
        self.assertEqual(
            sorted(
                (g.object_type, g.status)
                for g in grants.filter(Q(owner_team=self.team_a) | Q(guest_team=self.team_a))
            ),
            sorted(
                [(PROJECT, "active")] * 9  # P1, P2, P4, P5, P6, P7 twice, P8 out; PB1 in
                + [(CHANNEL, "active")] * 5  # CH1, CH2, CH4, CH5 out; CHB in
                + [(FOLDER, "active")] * 2  # F1 out; FB in
                + [(PROJECT, "pending"), (FOLDER, "revoked"), (CHANNEL, "declined")]
            ),
        )
        # Seen from A's owner: both directions and all three types are in play.
        people = {
            self.names[uid]: self.team_labels[home["teamId"]]
            for uid, home in external_people_for_member(
                self.team_a.team_id, self.a_owner.id
            ).items()
        }
        self.assertEqual(people["b_owner"], "Guest Team")
        self.assertEqual(people["c_owner"], "Stranger Team")
        self.assertEqual(people["d_owner"], "Delta Team")  # owner with no membership row
        self.assertEqual(people["b_extra"], "Guest Team")  # non-canonical ids still match
        self.assertNotIn("third_party", people)  # on the object, on neither side
        self.assertNotIn("b_gone", people)  # left the team
        self.assertNotIn("pending_only", people)
        self.assertNotIn("revoked_only", people)
        self.assertNotIn("declined_only", people)

    def test_a_person_on_two_grants_takes_the_first_grants_team(self):
        """bc_both1 is on grants to B (first) and to C; bc_both2 on grants to C
        (first) and to B; bc_same on two grants of the same project, to B and
        to C; bc_rev on a chat shared to B and then a project shared to C. The
        database promises no order, so the expected team is worked out from the
        grants in the order it actually returns them."""
        order = list(
            ExternalGrant.objects.filter(
                Q(owner_team_id=self.team_a.team_id) | Q(guest_team_id=self.team_a.team_id),
                status=ShareStatus.ACTIVE,
            )
        )
        people = external_people_for_member(self.team_a.team_id, self.a_owner.id)
        shown = "\n".join(
            f"  {g.object_type} {g.object_id} -> {g.guest_team.team_name}" for g in order
        )
        for person in (self.bc_both1, self.bc_both2, self.bc_same, self.bc_rev):
            expected = next(
                str(g.guest_team_id)
                for g in order
                if {str(self.a_owner.id), str(person.id)} <= self.roster_of(g)
                and str(g.guest_team_id) in {str(self.team_b.team_id), str(self.team_c.team_id)}
            )
            self.assertEqual(
                people[str(person.id)]["teamId"],
                expected,
                f"{person.username}; grants in order:\n{shown}",
            )


class MalformedGrantTests(SharingWorld):
    """A grant whose `object_id` is not an id of its type, as it behaves today.

    `object_id` is a text column serving three differently keyed tables. A bad
    channel or folder id makes the lookup raise, and the helpers swallow that
    by answering "nobody" for the WHOLE call; a bad project id is swallowed one
    grant earlier (`is_project_member`) and costs only that grant. These are
    pinned so that making the roster cheaper does not quietly change which
    one-bad-row failures take the whole list down. They describe behaviour, not
    an endorsement of it.
    """

    def setUp(self):
        super().setUp()
        self.project = self.new_project(self.team_a, self.a_owner, "Good")
        self.share(PROJECT, self.project, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.put_on(self.project, self.a_owner, self.b_editor)
        self.connection = get_connection(self.team_a.team_id, self.team_b.team_id)

    def bad_grant(self, kind, object_id, guest_team=None):
        return ExternalGrant.objects.create(
            connection=self.connection,
            owner_team=self.team_a,
            guest_team=guest_team or self.team_b,
            object_type=kind,
            object_id=object_id,
            status=ShareStatus.ACTIVE,
        )

    def people(self, team=None, viewer=None):
        team = team or self.team_a
        viewer = viewer or self.a_owner
        return {
            u.username
            for u in (self.a_owner, self.b_owner, self.b_editor)
            if str(u.id) in external_people_for_member(team.team_id, viewer.id)
        }

    def test_the_good_grant_alone_lists_the_guest_teams_people(self):
        self.assertEqual(self.people(), {"b_owner", "b_editor"})

    def test_a_malformed_channel_id_empties_the_whole_list(self):
        self.bad_grant(CHANNEL, "not-a-uuid")
        self.assertEqual(self.people(), set())

    def test_a_malformed_folder_id_empties_the_whole_list(self):
        self.bad_grant(FOLDER, "abc")
        self.assertEqual(self.people(), set())

    def test_an_integer_too_large_for_the_column_names_nothing(self):
        """Not malformed, just matching no row: neither a project nor a note
        folder grant with such an id disturbs the rest of the list."""
        self.bad_grant(PROJECT, "99999999999999999999")
        self.bad_grant(FOLDER, "99999999999999999999")
        self.assertEqual(self.people(), {"b_owner", "b_editor"})

    def test_a_malformed_project_id_costs_only_that_grant(self):
        self.bad_grant(PROJECT, "abc")
        self.assertEqual(self.people(), {"b_owner", "b_editor"})

    def test_a_participant_row_with_no_user_empties_the_whole_list(self):
        """`ProjectMembers.attendee` is nullable (SET_NULL). The id of such a
        row is looked up as the text "None", which raises."""
        ProjectMembers.objects.create(project=self.project, attendee=None, team=self.team_a)
        self.assertEqual(self.people(), set())

    def test_a_malformed_grant_to_a_team_the_caller_is_not_in_is_ignored_by_the_visible_set(self):
        """`external_visible_user_ids` only reads the objects of grants whose
        guest team the caller belongs to, so a bad id elsewhere is never read."""
        ch = self.new_channel(self.team_a, self.a_owner, "Good chat")
        self.share(CHANNEL, ch, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.put_on(ch, self.b_editor)
        self.bad_grant(CHANNEL, "not-a-uuid", guest_team=self.team_c)
        seen = external_visible_user_ids(self.team_a.team_id, self.b_owner.id)
        # a_owner is in the chat they offered (MIP-1522), so the guests see them too.
        self.assertEqual({str(self.a_owner.id), str(self.b_owner.id), str(self.b_editor.id)}, seen)

    def test_a_malformed_grant_to_a_team_the_caller_is_in_empties_the_visible_set(self):
        ch = self.new_channel(self.team_a, self.a_owner, "Good chat")
        self.share(CHANNEL, ch, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.put_on(ch, self.b_editor)
        self.bad_grant(CHANNEL, "not-a-uuid", guest_team=self.team_b)
        self.assertEqual(external_visible_user_ids(self.team_a.team_id, self.b_owner.id), set())

    def test_a_malformed_folder_grant_to_the_callers_team_empties_the_visible_set(self):
        folder = self.new_folder(self.team_a, self.a_owner, "Good folder")
        self.share(FOLDER, folder, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.bad_grant(FOLDER, "abc", guest_team=self.team_b)
        self.assertEqual(external_visible_user_ids(self.team_a.team_id, self.b_owner.id), set())

    def test_an_integer_too_large_for_the_column_is_skipped_by_the_visible_set(self):
        ch = self.new_channel(self.team_a, self.a_owner, "Good chat")
        self.share(CHANNEL, ch, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.put_on(ch, self.b_editor)
        self.bad_grant(FOLDER, "99999999999999999999", guest_team=self.team_b)
        seen = external_visible_user_ids(self.team_a.team_id, self.b_owner.id)
        # a_owner is in the chat they offered (MIP-1522), so the guests see them too.
        self.assertEqual({str(self.a_owner.id), str(self.b_owner.id), str(self.b_editor.id)}, seen)

    def test_a_malformed_project_grant_is_skipped_by_the_visible_set(self):
        """Projects resolve through `guest_visible_user_ids`, never read here."""
        ch = self.new_channel(self.team_a, self.a_owner, "Good chat")
        self.share(CHANNEL, ch, self.team_a, self.a_owner, self.team_b, self.b_owner)
        self.put_on(ch, self.b_editor)
        self.bad_grant(PROJECT, "abc", guest_team=self.team_b)
        seen = external_visible_user_ids(self.team_a.team_id, self.b_owner.id)
        # a_owner is in the chat they offered (MIP-1522), so the guests see them too.
        self.assertEqual({str(self.a_owner.id), str(self.b_owner.id), str(self.b_editor.id)}, seen)


# Query budgets, counted through the test client the way production `db_n`
# counts (authentication included). Every shared-object type is present in the
# fixture, because a type with no grants skips its queries and would otherwise
# look like growth when it first appears. Before the roster was batched the same
# fixture cost 35 -> 61, 20 -> 33, 23 -> 34 and 24 -> 36 queries for 2 -> 4 rounds
# (6 -> 12 grants), i.e. about 4 per grant.
#
#   member (not the owner), 12:
#       1 auth + 2 membership (owner check, member check) + 1 roster
#       + 1 grants (owned by or shared with the team)
#       + 3 people on the shared objects the caller is on: chats, projects, folders
#       + 1 which of its two teams each of them belongs to
#       + 1 the teams (owner and name together) + 1 team pictures + 1 users
#   guest_team_member (owner of their own team), 11: the same with a one-query
#       membership check
#   outside_caller (not a member, admitted to shared chats / folders / a project), 16:
#       1 auth + 2 membership + 3 is_guest (another membership check, then a
#       project-member check) + 2 the projects they are on + 2 who is on them
#       + 5 external_visible_user_ids: grants, 2 for which guest teams they are in,
#         1 chat rows, 1 folder rows
#       + 1 roster
#   project_guest (in no team; the shared-object lookups find nothing to read), 14:
#       the outside_caller list without the two row queries
BUDGETS = {
    "member": 12,
    "guest_team_member": 11,
    "outside_caller": 16,
    "project_guest": 14,
}


class MembersQueryCountTests(SharingWorld):
    """N shared projects, chats and note folders (each with its own new
    people on both sides), against 2N."""

    N = 2

    def setUp(self):
        super().setUp()
        self.units = 0
        self.proj_guest = self._user("proj_guest")

    def add_units(self, count):
        """`count` rounds of one project, one chat and one note folder, owned by
        A and shared to B (odd rounds) or C (even rounds), each with a new A
        member and a new guest-team member on it."""
        for _ in range(count):
            self.units += 1
            n = self.units
            guest_team, guest_owner = (
                (self.team_b, self.b_owner) if n % 2 else (self.team_c, self.c_owner)
            )
            a_new = self._member(self.team_a, f"a_new{n}", EDITOR)
            g_new = self._member(guest_team, f"g_new{n}", EDITOR)
            objects = (
                (PROJECT, self.new_project(self.team_a, self.a_owner, f"Shared {n}")),
                (CHANNEL, self.new_channel(self.team_a, self.a_owner, f"Chat {n}")),
                (FOLDER, self.new_folder(self.team_a, self.a_owner, f"Folder {n}")),
            )
            for kind, obj in objects:
                self.share(kind, obj, self.team_a, self.a_owner, guest_team, guest_owner)
                self.put_on(obj, self.a_owner, self.a_editor, a_new, g_new)
            self.put_on(objects[0][1], self.proj_guest)

    def measure(self, viewer, team):
        cache.clear()
        self.authenticate(viewer)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(
                TEAM_MEMBERS,
                {
                    "team_id": str(team.team_id),
                    "team_name": team.team_name,
                    "user_id": str(viewer.id),
                },
            )
        self.assertEqual(res.status_code, 200, res.content[:300])
        sqls = [q["sql"] for q in ctx.captured_queries]
        return len(ctx), res.json()["data"]["members"], sqls

    @staticmethod
    def show(sqls):
        return "\n".join(f"  {i + 1}. {sql[:150]}" for i, sql in enumerate(sqls))

    def check(self, name, viewer, team):
        self.add_units(self.N)
        small, small_rows, small_sql = self.measure(viewer, team)
        self.add_units(self.N)
        large, large_rows, large_sql = self.measure(viewer, team)

        self.assertGreater(len(large_rows), len(small_rows), "the roster must grow with the shares")
        self.assertEqual(
            small,
            large,
            f"{name}: {small} queries for {self.N} rounds, {large} for {2 * self.N}:\n"
            f"{self.show(large_sql)}",
        )
        self.assertEqual(large, BUDGETS[name], f"{name} budget:\n{self.show(large_sql)}")

    def test_a_member_of_the_owning_team(self):
        self.check("member", self.a_editor, self.team_a)

    def test_a_member_of_the_guest_team_in_their_own_team(self):
        self.check("guest_team_member", self.b_owner, self.team_b)

    def test_a_guest_team_member_looking_at_the_owning_team(self):
        """Not a member there: the roster is narrowed to the shared objects."""
        self.check("outside_caller", self.b_owner, self.team_a)

    def test_a_project_guest_looking_at_the_owning_team(self):
        self.check("project_guest", self.proj_guest, self.team_a)

    def test_every_type_of_share_is_in_the_fixture(self):
        self.add_units(1)
        self.assertEqual(
            sorted(set(ExternalGrant.objects.values_list("object_type", flat=True))),
            sorted([CHANNEL, FOLDER, PROJECT]),
        )
