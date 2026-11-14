"""MIP-1491: `cleanup_stale_chat_note_roles` ends the chat-note roles of people who left BEFORE
MIP-1482 (api #753) made leaving revoke them.

The fixtures build the state those people are in: the membership is ended the way the old code
ended it (a queryset update that fires no revoke), and the role rows are dated by hand with
explicit offsets from the note's creation (`auto_now_add` ignores constructor values).
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from origin.management.commands.cleanup_stale_chat_note_roles import read_only
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.security_event_models import SecurityEvent
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services import chat_note_stale_roles as stale
from origin.services.chat_note_access import membership_derived_rows, row_is_membership_derived
from origin.tests.test_chat_note_one_rule import CHAT, ChatNoteFixtures
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER

T0 = timezone.now() - timedelta(days=100)
DAY = timedelta(days=1)


class Base(ChatNoteFixtures):
    def person(self, name, *, team=True):
        user = get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", password="x"
        )
        if team:
            TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def note(self, channel, title="n"):
        note = self.note_on(channel, title=title)
        ChatNoteMaster.objects.filter(pk=note.pk).update(ts_created_at=T0)
        return note

    def role(self, user, note, at, role_id=ROLE_EDITOR):
        """A role row dated `at` (an absolute time)."""
        row = NotePermissionMaster.objects.create(
            team=self.team, user=user, note_type=CHAT, note_id=note.note_id, role_id=role_id
        )
        NotePermissionMaster.objects.filter(pk=row.pk).update(ts_created_at=at)
        return row

    def member(self, channel, user, joined, *, left=False):
        row = ChannelMember.objects.create(channel=channel, user=user)
        ChannelMember.objects.filter(pk=row.pk).update(ts_joined_at=joined, is_deleted=left)
        return row

    def chat(self, kind=ChannelKind.GM):
        return Channel.objects.create(team=self.team, kind=kind, title="c", owner=self.owner)

    def run(self, *args):
        out = io.StringIO()
        call_command("cleanup_stale_chat_note_roles", *args, stdout=out)
        return out.getvalue()

    def report(self, *args):
        return json.loads(self.run("--json", *args))

    def alive(self, row):
        return NotePermissionMaster.objects.filter(pk=row.pk).exists()


class Classification(Base):
    def test_left_a_group_chat_membership_row_is_revoked(self):
        gm, who = self.chat(), self.person("leaver")
        note = self.note(gm)
        self.member(gm, who, T0 + DAY, left=True)
        row = self.role(who, note, T0 + 2 * DAY)
        got = self.report()
        self.assertEqual(got["would_revoke"], 1)
        self.assertEqual(got["by_chat_kind"]["GM"][stale.REVOKE_MEMBERSHIP], 1)
        self.run("--apply", "--yes")
        self.assertFalse(self.alive(row))

    def test_left_a_project_membership_row_is_revoked(self):
        project = self.project_of("STL", self.reader)
        pm = self.pm_chat(project)
        note = self.note(pm)
        ProjectMembers.objects.filter(project=project, attendee=self.reader).delete()
        # the soft-deleted mirror is the only join date left
        ChannelMember.objects.filter(channel=pm, user=self.reader).update(
            ts_joined_at=T0 + DAY, is_deleted=True
        )
        row = self.role(self.reader, note, T0 + 2 * DAY)
        got = self.report()
        self.assertEqual(got["by_chat_kind"]["PM"][stale.REVOKE_MEMBERSHIP], 1)
        self.run("--apply", "--yes")
        self.assertFalse(self.alive(row))

    def test_a_pm_stray_channel_row_is_not_membership(self):
        """A live `ChannelMember` of a project chat for someone NOT on the project opens nothing
        (`chat_member_q`), so it must not shield their role row."""
        project = self.project_of("STR")
        pm = self.pm_chat(project)
        note = self.note(pm)
        ChannelMember.objects.filter(channel=pm, user=self.reader).delete()
        self.member(pm, self.reader, T0 + DAY)  # live row, but not on the project
        row = self.role(self.reader, note, T0 + 2 * DAY)
        self.assertEqual(self.report()["would_revoke"], 1)
        self.run("--apply", "--yes")
        self.assertFalse(self.alive(row))

    def test_removed_from_the_team_loses_even_a_share(self):
        gm, who = self.chat(), self.person("quitter")
        note = self.note(gm)
        share = self.role(who, note, T0 + DAY, ROLE_VIEWER)  # never in the chat: a share
        TeamMembers.objects.filter(team=self.team, attendee=who).update(is_deleted=True)
        got = self.report()
        self.assertEqual(got["would_revoke_team_left"], 1)
        self.assertEqual(got["kept_deliberate_shares"], 0)
        self.run("--apply", "--yes")
        self.assertFalse(self.alive(share))

    def test_a_share_before_the_join_is_kept(self):
        gm, who = self.chat(), self.person("sharee")
        note = self.note(gm)
        self.member(gm, who, T0 + 5 * DAY, left=True)
        share = self.role(who, note, T0 + DAY, ROLE_VIEWER)
        got = self.report()
        self.assertEqual(got["kept_deliberate_shares"], 1)
        self.assertEqual(got["would_revoke"], 0)
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(share))

    def test_a_share_to_someone_who_never_was_a_member_is_kept(self):
        gm, who = self.chat(), self.person("outsider")
        note = self.note(gm)
        share = self.role(who, note, T0 + DAY, ROLE_VIEWER)
        self.assertEqual(self.report()["kept_deliberate_shares"], 1)
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(share))

    def test_a_current_member_is_never_touched(self):
        gm, who = self.chat(), self.person("insider")
        note = self.note(gm)
        self.member(gm, who, T0 + DAY)
        rows = [self.role(who, note, T0 + 2 * DAY), self.role(self.reader, note, T0 + DAY)]
        self.member(gm, self.reader, T0 + 5 * DAY)  # row older than the join, still a member
        got = self.report()
        self.assertEqual(got["current_members_untouched"], 2)
        self.assertEqual(got["would_revoke"], 0)
        self.run("--apply", "--yes")
        self.assertTrue(all(self.alive(r) for r in rows))

    def test_an_external_guest_of_a_shared_chat_is_a_current_member(self):
        """No `TeamMembers` row in the host team, a live member row: nothing to revoke."""
        gm, guest = self.chat(), self.person("guest", team=False)
        note = self.note(gm)
        self.member(gm, guest, T0 + DAY)
        row = self.role(guest, note, T0 + 2 * DAY)
        got = self.report()
        self.assertEqual(got["current_members_untouched"], 1)
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(row))

    def test_a_withdrawn_guest_takes_the_membership_rule_not_the_team_rule(self):
        gm, guest = self.chat(), self.person("guest2", team=False)
        note = self.note(gm)
        self.member(gm, guest, T0 + DAY, left=True)
        mine = self.role(guest, note, T0 + 2 * DAY)
        got = self.report()
        self.assertEqual(got["would_revoke_membership"], 1)
        self.assertEqual(got["would_revoke_team_left"], 0)
        self.run("--apply", "--yes")
        self.assertFalse(self.alive(mine))

    def test_an_old_membership_row_is_counted_ambiguous_and_kept(self):
        """Reviewer's case: a membership row OLDER than the recorded join date survives the
        two-kind rule. Written seconds after the note, it looks like the creation fan-out."""
        gm, who = self.chat(), self.person("late")
        note = self.note(gm)
        self.member(gm, who, T0 + 2 * DAY, left=True)
        row = self.role(who, note, T0 + timedelta(seconds=5))
        got = self.report()
        self.assertEqual(got["ambiguous_kept"], 1)
        self.assertEqual(got["kept_deliberate_shares"], 0)
        self.assertEqual(got["would_revoke"], 0)
        self.assertEqual(got["ambiguous_sample"][0]["role_row_id"], row.pk)
        self.assertEqual(got["ambiguous_sample"][0]["chat_kind"], "GM")
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(row))

    def test_counts_by_chat_kind_cover_dm_mdm_gm_pm(self):
        for kind in (ChannelKind.DM, ChannelKind.MDM, ChannelKind.GM):
            ch, who = self.chat(kind), self.person(f"k{int(kind)}")
            self.member(ch, who, T0 + DAY, left=True)
            self.role(who, self.note(ch), T0 + 2 * DAY)
        project = self.project_of("KND", self.reader)
        pm = self.pm_chat(project)
        ProjectMembers.objects.filter(project=project, attendee=self.reader).delete()
        ChannelMember.objects.filter(channel=pm, user=self.reader).update(
            ts_joined_at=T0 + DAY, is_deleted=True
        )
        self.role(self.reader, self.note(pm), T0 + 2 * DAY)
        got = self.report()["by_chat_kind"]
        self.assertEqual({k: v[stale.REVOKE_MEMBERSHIP] for k, v in got.items()},
                         {"DM": 1, "GM": 1, "PM": 1, "MDM": 1})

    def test_a_deleted_chat_and_a_mismatched_note_are_skipped(self):
        dead, who = self.chat(), self.person("deadchat")
        n1 = self.note(dead)
        self.member(dead, who, T0 + DAY, left=True)
        r1 = self.role(who, n1, T0 + 2 * DAY)
        Channel.objects.filter(pk=dead.pk).update(is_deleted=True)
        live = self.chat()
        n2 = self.note(live)
        ChatNoteMaster.objects.filter(pk=n2.pk).update(chat_type=ChannelKind.DM)
        self.member(live, who, T0 + DAY, left=True)
        r2 = self.role(who, n2, T0 + 2 * DAY)
        got = self.report()
        self.assertEqual((got["skipped"], got["would_revoke"]), (2, 0))
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(r1) and self.alive(r2))

    def test_task_and_personal_notes_and_ownerless_rows_are_out_of_scope(self):
        gm, who = self.chat(), self.person("scope")
        self.member(gm, who, T0 + DAY, left=True)
        task_row = NotePermissionMaster.objects.create(
            team=self.team, user=who, note_type=2, note_id=999, role_id=ROLE_EDITOR
        )
        self.assertEqual(self.report()["scanned_rows"], 0)
        self.run("--apply", "--yes")
        self.assertTrue(self.alive(task_row))


class SameRuleAsTheLiveRevoke(Base):
    def test_the_python_rule_and_the_sql_filter_agree_at_the_boundary(self):
        gm, who = self.chat(), self.person("edge")
        joined = T0 + DAY
        member = self.member(gm, who, joined, left=True)
        notes = [self.note(gm, f"n{i}") for i in range(3)]
        at = [joined - timedelta(microseconds=1), joined, joined + timedelta(microseconds=1)]
        rows = [self.role(who, n, t) for n, t in zip(notes, at)]
        sql = set(
            membership_derived_rows(user_id=who.id, channel_id=gm.id, joined_at=joined).values_list(
                "pk", flat=True
            )
        )
        py = {r.pk for r, t in zip(rows, at) if row_is_membership_derived(t, joined)}
        self.assertEqual(sql, py)
        self.assertEqual(len(sql), 2)  # created == joined counts as membership (>=)
        found = set(stale.scan().revoke_pks)
        self.assertEqual(found, sql)
        del member

    def test_scan_chooses_exactly_what_the_live_revoke_would_delete(self):
        gm, who = self.chat(), self.person("same")
        member = self.member(gm, who, T0 + 3 * DAY, left=True)
        rows = [
            self.role(who, self.note(gm, "a"), T0 + 4 * DAY),  # membership
            self.role(who, self.note(gm, "b"), T0 + DAY),  # share
            self.role(who, self.note(gm, "c"), T0 + 5 * DAY),  # membership
        ]
        chosen = set(stale.scan().revoke_pks)
        from origin.services.chat_note_access import revoke_membership_roles

        deleted = revoke_membership_roles(
            user_id=who.id, channel_id=gm.id, joined_at=member.ts_joined_at
        )
        gone = {r.pk for r in rows if not self.alive(r)}
        self.assertEqual((deleted, chosen), (2, gone))


class DryRunIsReadOnly(Base):
    def setUp(self):
        super().setUp()
        gm, who = self.chat(), self.person("dry")
        self.stale = self.role(who, self.note(gm), T0 + 2 * DAY)
        self.member(gm, who, T0 + DAY, left=True)

    def test_a_dry_run_issues_no_write_statement(self):
        writes = []

        def spy(execute, sql, params, many, context):
            if sql.lstrip().split(None, 1)[0].upper() not in {"SELECT", "SET", "SAVEPOINT", "RELEASE"}:
                writes.append(sql)
            return execute(sql, params, many, context)

        events = SecurityEvent.objects.count()
        before = list(NotePermissionMaster.objects.values_list("pk", "ts_updated_at"))
        with mock.patch(
            "origin.search_engine.acl_dirty.mark_channels_dirty"
        ) as dirty, connection.execute_wrapper(spy):
            out = self.run()
            self.run("--json", "--limit", "1")
        self.assertEqual(writes, [])
        dirty.assert_not_called()
        self.assertEqual(SecurityEvent.objects.count(), events)
        self.assertEqual(
            list(NotePermissionMaster.objects.values_list("pk", "ts_updated_at")), before
        )
        self.assertIn("[dry-run]", out)
        self.assertIn("Nothing was written", out)
        self.assertTrue(self.alive(self.stale))

    def test_the_database_itself_refuses_a_write_inside_the_dry_run(self):
        from django.db import ProgrammingError

        if connection.vendor != "postgresql":
            self.skipTest("postgres only")
        with self.assertRaises(ProgrammingError):
            with read_only():
                NotePermissionMaster.objects.filter(pk=self.stale.pk).delete()
        self.assertTrue(self.alive(self.stale))

    def test_the_connection_is_writable_again_afterwards(self):
        self.run()
        NotePermissionMaster.objects.filter(pk=self.stale.pk).delete()  # must not raise

    def test_json_is_one_parseable_document(self):
        got = json.loads(self.run("--json"))
        self.assertEqual(got["mode"], "dry-run")
        self.assertEqual(got["would_revoke"], 1)


class Apply(Base):
    def stale_rows(self, n):
        rows = []
        for i in range(n):
            gm, who = self.chat(), self.person(f"p{i}")
            self.member(gm, who, T0 + DAY, left=True)
            rows.append(self.role(who, self.note(gm), T0 + 2 * DAY))
        return rows

    def test_apply_needs_yes(self):
        rows = self.stale_rows(1)
        with self.assertRaises(CommandError):
            self.run("--apply")
        self.assertTrue(self.alive(rows[0]))

    def test_apply_matches_the_dry_run_and_is_idempotent(self):
        rows = self.stale_rows(3)
        keep = self.role(self.reader, self.note(self.chat()), T0 + DAY)  # never a member: share
        planned = self.report()["would_revoke"]
        got = self.report("--apply", "--yes")
        self.assertEqual((planned, got["revoked"]), (3, 3))
        self.assertFalse(any(self.alive(r) for r in rows))
        self.assertTrue(self.alive(keep))
        again = self.report("--apply", "--yes")
        self.assertEqual((again["would_revoke"], again["revoked"]), (0, 0))

    def test_apply_marks_the_search_acl_dirty(self):
        self.stale_rows(2)
        with mock.patch("origin.services.chat_note_stale_roles._mark_search") as marked:
            self.run("--apply", "--yes")
        self.assertEqual(marked.call_count, 1)
        self.assertEqual(len(marked.call_args.args[0]), 2)

    def test_limit_caps_the_rows_revoked(self):
        rows = self.stale_rows(5)
        got = self.report("--apply", "--yes", "--limit", "2")
        self.assertEqual((got["revoked"], got["limited"]), (2, True))
        self.assertEqual(sum(self.alive(r) for r in rows), 3)

    def test_team_scopes_the_run(self):
        rows = self.stale_rows(2)
        other = self.report("--team", str(uuid.uuid4()))
        self.assertEqual(other["scanned_rows"], 0)
        mine = self.report("--team", str(self.team.team_id), "--apply", "--yes")
        self.assertEqual(mine["revoked"], 2)
        self.assertFalse(any(self.alive(r) for r in rows))

    def test_batch_boundaries(self):
        for size, n in ((1, 3), (2, 5), (5, 5), (6, 5), (2, 4)):
            with self.subTest(batch=size, rows=n):
                NotePermissionMaster.objects.all().delete()
                rows = self.stale_rows(n)
                got = self.report("--apply", "--yes", "--batch-size", str(size))
                self.assertEqual(got["revoked"], n)
                self.assertFalse(any(self.alive(r) for r in rows))

    def test_a_failed_batch_rolls_back_only_itself(self):
        rows = self.stale_rows(5)
        calls = {"n": 0}

        def boom(channels):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second batch")

        with mock.patch("origin.services.chat_note_stale_roles._mark_search", boom):
            with self.assertRaises(RuntimeError):
                self.run("--apply", "--yes", "--batch-size", "2")
        self.assertEqual(sum(self.alive(r) for r in rows), 3)  # first batch committed
        self.run("--apply", "--yes")  # a rerun finishes the job
        self.assertFalse(any(self.alive(r) for r in rows))

    def test_a_person_re_added_after_the_scan_is_not_hit(self):
        rows = self.stale_rows(1)
        report = stale.scan()
        ChannelMember.objects.update(is_deleted=False)  # everyone is back in
        self.assertEqual(stale.apply(report.revoke_pks), 0)
        self.assertTrue(self.alive(rows[0]))

    def test_a_row_already_gone_is_skipped(self):
        rows = self.stale_rows(2)
        report = stale.scan()
        NotePermissionMaster.objects.filter(pk=rows[0].pk).delete()
        self.assertEqual(stale.apply(report.revoke_pks), 1)
