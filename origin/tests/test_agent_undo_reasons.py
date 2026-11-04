"""What `undo.UNDO_REASONS` says about telling people is true of what the tool does.

The approval card shows the reason code before a person approves ("can't be undone: ..."),
and `notified_people` is the one that is about other people. A tool filed under it has to
tell someone. So the tools are run here, through `watching_notices`, and what they send is
compared with how they are filed:

  * `manage_note_sharing` gives or takes away access and sends nothing, so it is `not_yet`:
    restorable in principle (the same tool takes the access away), the restore is not built;
  * the tools that DO tell somebody are filed that way, with the notice they send.
"""

from __future__ import annotations

from origin.search_engine.agent import undo

from .notice_watch import watching_notices
from .test_agent_tool_note_sharing import SharingBase


class SharingTellsNobody(SharingBase):
    def test_sharing_a_note_sends_nothing_to_anyone(self):
        with watching_notices() as seen:
            self.manage(self.share_args(role="editor"))
        self.assertEqual(seen, {"rows": {}, "emails": 0, "pushes": {}})

    def test_taking_the_share_back_sends_nothing_to_anyone(self):
        self.manage(self.share_args(role="viewer"))
        with watching_notices() as seen:
            self.manage({**self.share_args(role="viewer"), "action": "unshare"})
        self.assertEqual(seen, {"rows": {}, "emails": 0, "pushes": {}})

    def test_so_it_is_filed_as_not_yet_and_never_as_telling_people(self):
        code, line = undo.UNDO_REASONS["manage_note_sharing"]
        self.assertEqual(code, undo.NOT_YET)
        self.assertIn("nobody is told", line)
        self.assertEqual(
            undo.proposal("manage_note_sharing", {}), undo.Proposal(False, undo.NOT_YET)
        )
