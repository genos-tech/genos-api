"""The rewrite-volume alert and the way back from an MCP overwrite (MIP-2265).

Decision D7 (MIP-2200): no new consent gate on MCP writes. Instead

  * a note body that a tool overwrites is kept in the note's version history, and
    `restore_note_version` puts it back (the same promise task descriptions already had);
  * one key or connected app that changes more than N items in a window raises
    `alert.write_volume` (email only; the writes are never refused).
"""

from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from origin.models.common.ops_models import OpsAlert
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import write_volume
from origin.tests.test_mcp_endpoint import SCOPE_WRITE, McpBase

_LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-write-volume",
    }
}


class RowsChangedTests(SimpleTestCase):
    def test_a_bulk_call_counts_one_per_row(self):
        self.assertEqual(write_volume.rows_changed("update_tasks_bulk", {"updates": [{}] * 12}), 12)
        self.assertEqual(write_volume.rows_changed("create_task_plan", {"tasks": [{}] * 5}), 5)

    def test_a_single_write_counts_one(self):
        self.assertEqual(write_volume.rows_changed("update_note", {"note_id": 1}), 1)
        self.assertEqual(write_volume.rows_changed("update_tasks_bulk", {"updates": []}), 1)
        self.assertEqual(write_volume.rows_changed("update_tasks_bulk", {"updates": "x"}), 1)
        self.assertEqual(write_volume.rows_changed("update_note", None), 1)


@override_settings(
    CACHES=_LOCMEM, SECURITY_WRITE_ROWS_PER_WINDOW=5, SECURITY_WRITE_WINDOW_SECONDS=600
)
class WriteVolumeAlertTests(McpBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _create_note(self, title):
        return self._tool_text(
            self._call(
                "create_note", {"note_type": "personal", "title": title, "content_text": "body"}
            )
        )

    def _alerts(self):
        return SecurityEvent.objects.filter(action=SecurityAction.ALERT_WRITE_VOLUME)

    def test_alert_fires_once_when_a_key_passes_the_line(self):
        self._key(scope=SCOPE_WRITE)
        for i in range(4):
            self._create_note(f"n{i}")
        self.assertEqual(self._alerts().count(), 0)
        for i in range(4, 9):
            text, is_error = self._create_note(f"n{i}")
            self.assertFalse(is_error, text)  # never refused
        self.assertEqual(self._alerts().count(), 1)
        event = self._alerts().get()
        self.assertEqual(event.target_type, "api_key")
        self.assertEqual(event.metadata["rows"], 5)
        self.assertEqual(event.metadata["limit"], 5)
        self.assertEqual(event.metadata["last_tool"], "create_note")
        self.assertIn("restore_note_version", event.metadata["undo"])
        self.assertEqual(PersonalNoteMaster.objects.filter(owner=self.user).count(), 9)
        # It opens an ops alert, which `ops_alert_tick` mails.
        incident = OpsAlert.objects.get(key__startswith="security:alert.write_volume:key:")
        self.assertIn("5 items in 600 s", incident.summary)

    def test_reads_are_not_counted(self):
        self._key(scope=SCOPE_WRITE)
        for _ in range(10):
            self._call("get_task", {"task_id": self.task.task_id})
        self.assertEqual(self._alerts().count(), 0)

    def test_another_key_has_its_own_count(self):
        self._key(scope=SCOPE_WRITE)
        for i in range(4):
            self._create_note(f"a{i}")
        self._key(scope=SCOPE_WRITE)
        for i in range(4):
            self._create_note(f"b{i}")
        self.assertEqual(self._alerts().count(), 0)

    def test_a_bulk_call_counts_its_rows(self):
        cred = mock.Mock(id="cred-1", kind="api_key", label="k")
        with mock.patch("origin.services.security_events.record_security_event") as record:
            write_volume.note_write(
                None, cred, team_id=1, tool_name="update_tasks_bulk", args={"updates": [{}] * 6}
            )
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["metadata"]["rows"], 6)

    def test_a_broken_cache_never_breaks_a_write(self):
        cred = mock.Mock(id="cred-1", kind="api_key", label="k")
        with mock.patch(
            "origin.services.throttles.throttle_cache", side_effect=RuntimeError("down")
        ):
            write_volume.note_write(None, cred, team_id=1, tool_name="update_note", args={})

    def test_an_overwritten_note_can_be_restored(self):
        """The way back: `update_note` keeps the text it replaces, and the history shows it."""
        self._key(scope=SCOPE_WRITE)
        self._create_note("Plan")
        note = PersonalNoteMaster.objects.get(title="Plan")
        text, is_error = self._tool_text(
            self._call(
                "update_note",
                {
                    "note_type": "personal",
                    "note_id": note.note_id,
                    "content_text": "OVERWRITTEN BY AN INJECTED INSTRUCTION",
                },
            )
        )
        self.assertFalse(is_error, text)
        versions = NoteVersionMaster.objects.filter(note_id=note.note_id).order_by("version_no")
        self.assertTrue(
            any("body" in str(v.body) for v in versions), "the replaced body must be kept"
        )
        first = versions.first().version_no
        text, is_error = self._tool_text(
            self._call(
                "restore_note_version",
                {"note_type": "personal", "note_id": note.note_id, "version_no": first},
            )
        )
        self.assertFalse(is_error, text)
        note.refresh_from_db()
        self.assertNotIn("INJECTED", str(note.body))
