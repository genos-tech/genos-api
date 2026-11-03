"""MIP-841: note folders are search results.

One `note_folder` entity per folder (My Notes, Team Notes, cross-team
shared). Who can read the folder is `acl_user_ids`; the names of the
folders above it are searched and shown only for people who can read all
of them (`context_acl`), exactly as for a note.
"""

from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.utils import timezone

from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine import purge
from origin.search_engine import search as S
from origin.search_engine.chunkers.note_folder_chunker import iter_note_folder_chunks
from origin.search_engine.ingestion import ingest_all
from origin.views.utils.note_acl_resync import resync_folder_subtree
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

from .test_base import BaseAPITestCase


class FolderBase(BaseAPITestCase):
    def _folder(self, name, *, parent=None, scope="personal", visibility=None, owner=None):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name=name,
            parent_folder_id=parent.folder_id if parent else None,
            scope=scope,
            visibility=visibility,
        )
        if scope == "team":
            NoteFolderPermission.objects.create(
                team=self.team, folder=folder, user=folder.owner, role_id=ROLE_OWNER
            )
        return folder

    def _chunks(self, **kw):
        return {b.entity_id: b.chunks[0] for b in iter_note_folder_chunks(**kw) if b.chunks}


class FolderChunkTests(FolderBase):
    def test_a_my_notes_folder_is_the_owners_alone(self):
        folder = self._folder("Specs")
        chunk = self._chunks()[f"note_folder:{folder.folder_id}"]
        self.assertEqual(chunk.entity_type, "note_folder")
        self.assertEqual(chunk.chunk_type, "note_folder")
        self.assertEqual((chunk.title, chunk.search_text), ("Specs", "Specs"))
        self.assertEqual((chunk.folder_id, chunk.folder_scope), (str(folder.folder_id), "personal"))
        self.assertEqual(chunk.acl_user_ids, [str(self.user.id)])
        self.assertEqual(chunk.team_id, str(self.team.team_id))
        # A top-level folder has no parents: no context.
        self.assertIsNone(chunk.context_path)
        self.assertNotIn("context_acl", chunk.to_dict())

    def test_a_nested_folder_carries_its_parents_as_context(self):
        top = self._folder("Design")
        sub = self._folder("Specs", parent=top)
        chunk = self._chunks()[f"note_folder:{sub.folder_id}"]
        self.assertEqual(chunk.context_path, ["Design"])
        self.assertEqual(chunk.context_text, "Design")
        self.assertEqual(chunk.context_acl, [str(self.user.id)])

    def test_no_searched_field_but_the_context_carries_a_parents_name(self):
        # Everything searched for ANY reader of the folder must be free of
        # the parents' names, or `context_acl` gates nothing: someone
        # granted only the subfolder could find it by the parent's name.
        parent = self._folder("Design", scope="team", visibility="private")
        sub = self._folder("Specs", parent=parent, scope="team", visibility="private")
        chunk = self._chunks()[f"note_folder:{sub.folder_id}"]
        for field in ("title", "search_text", "snippet_text"):
            self.assertNotIn("Design", getattr(chunk, field), field)
        self.assertEqual(chunk.context_text, "Design")

    def test_a_public_team_folder_is_the_team_sentinel(self):
        folder = self._folder("Handbook", scope="team", visibility="public")
        chunk = self._chunks()[f"note_folder:{folder.folder_id}"]
        self.assertEqual(chunk.folder_scope, "team")
        self.assertIn(f"team:{self.team.team_id}", chunk.acl_user_ids)

    def test_a_subfolder_grant_alone_finds_the_folder_but_not_the_parents_name(self):
        parent = self._folder("Design", scope="team", visibility="private")
        sub = self._folder("Specs", parent=parent, scope="team", visibility="private")
        NoteFolderPermission.objects.create(
            team=self.team, folder=sub, user=self.user2, role_id=ROLE_EDITOR
        )
        chunk = self._chunks()[f"note_folder:{sub.folder_id}"]
        # user2 may open "Specs"…
        self.assertIn(str(self.user2.id), chunk.acl_user_ids)
        # …but not read "Design" above it, so it isn't theirs to match or see.
        self.assertEqual(chunk.context_path, ["Design"])
        self.assertEqual(chunk.context_acl, [str(self.user.id)])

    def test_a_private_folder_is_not_in_a_non_members_acl(self):
        folder = self._folder("Board", scope="team", visibility="private")
        chunk = self._chunks()[f"note_folder:{folder.folder_id}"]
        self.assertEqual(chunk.acl_user_ids, [str(self.user.id)])

    def test_a_blank_name_or_a_teamless_folder_is_skipped(self):
        blank = self._folder("x")
        PersonalNoteFolder.objects.filter(folder_id=blank.folder_id).update(name="  ")
        teamless = self._folder("Orphan")
        PersonalNoteFolder.objects.filter(folder_id=teamless.folder_id).update(team=None)
        self.assertEqual(self._chunks(), {})

    def test_a_nameless_parent_gives_no_path_rather_than_one_with_a_hole(self):
        top = self._folder("Design")
        mid = self._folder("Specs", parent=top)
        leaf = self._folder("Q4", parent=mid)
        PersonalNoteFolder.objects.filter(folder_id=mid.folder_id).update(name="  ")
        chunk = self._chunks()[f"note_folder:{leaf.folder_id}"]
        # "Design" alone would read as Q4's parent.
        self.assertIsNone(chunk.context_path)
        self.assertIsNone(chunk.context_text)

    def test_a_broken_chain_keeps_the_folder_without_a_partial_path(self):
        folder = self._folder("Orphaned", parent=self._folder("Gone"))
        PersonalNoteFolder.objects.filter(folder_id=folder.parent_folder_id).delete()
        chunk = self._chunks()[f"note_folder:{folder.folder_id}"]
        self.assertIsNone(chunk.context_path)


class FolderWindowTests(FolderBase):
    def test_a_rename_re_emits_the_folder_and_everything_beneath(self):
        top = self._folder("Design")
        sub = self._folder("Specs", parent=top)
        deep = self._folder("Q4", parent=sub)
        other = self._folder("Other")
        old = timezone.now() - timedelta(days=2)
        PersonalNoteFolder.objects.update(ts_updated_at=old)
        since = timezone.now() - timedelta(minutes=1)
        self.assertEqual(self._chunks(since=since), {})
        PersonalNoteFolder.objects.filter(folder_id=top.folder_id).update(
            name="Product", ts_updated_at=timezone.now()
        )
        got = self._chunks(since=since)
        self.assertEqual(
            set(got),
            {f"note_folder:{f.folder_id}" for f in (top, sub, deep)},
        )
        self.assertNotIn(f"note_folder:{other.folder_id}", got)
        self.assertEqual(got[f"note_folder:{deep.folder_id}"].context_path, ["Product", "Specs"])

    def test_the_full_pass_covers_every_folder(self):
        folders = [self._folder(n) for n in ("A", "B", "C")]
        self.assertEqual(set(self._chunks()), {f"note_folder:{f.folder_id}" for f in folders})

    def test_ingest_all_runs_the_folder_lane_by_default_and_by_name(self):
        self._folder("A")
        for kwargs in ({}, {"entity_types": ["note_folder"]}):
            with self.subTest(kwargs=kwargs):
                stats = ingest_all(dry_run=True, **kwargs)
                self.assertGreaterEqual(stats.entities_processed, 1)
                self.assertEqual(stats.errors, [])


class FolderPurgeTests(FolderBase):
    def test_the_sweep_knows_the_type_and_keeps_live_folders(self):
        live = self._folder("Live")
        dead = self._folder("Dead")
        PersonalNoteFolder.objects.filter(folder_id=dead.folder_id).delete()
        got_dead, unparseable = purge._resolve_note_folders(
            [f"note_folder:{live.folder_id}", f"note_folder:{dead.folder_id}", "note_folder:x"]
        )
        self.assertEqual(got_dead, {f"note_folder:{dead.folder_id}"})
        self.assertEqual(unparseable, 1)
        self.assertIn("note_folder", purge._RESOLVERS)

    def test_the_wrapper_names_each_folder_and_never_raises(self):
        with mock.patch.object(purge, "purge_entities") as pe:
            purge.purge_note_folders({3, 5})
        self.assertEqual(
            sorted(pe.call_args.args[0]),
            [("note_folder", "note_folder:3"), ("note_folder", "note_folder:5")],
        )
        with mock.patch.object(purge, "purge_entities", side_effect=RuntimeError("down")):
            purge.purge_note_folders({3})
        with mock.patch.object(purge, "purge_entities") as pe:
            purge.purge_note_folders(set())
        pe.assert_not_called()

    def test_deleting_a_my_notes_folder_purges_its_subtree(self):
        top = self._folder("A")
        sub = self._folder("B", parent=top)
        self.authenticate()
        with mock.patch("origin.services.note_delete.purge_note_folders") as p:
            res = self.client.delete(
                "/api/v2/note/personal/folder/?"
                + urlencode(
                    {
                        "team_id": self.team.team_id,
                        "user_id": str(self.user.id),
                        "folder_id": top.folder_id,
                    }
                )
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(p.call_args.args[0]), {top.folder_id, sub.folder_id})

    def test_deleting_a_team_folder_purges_its_subtree(self):
        top = self._folder("Handbook", scope="team", visibility="public")
        sub = self._folder("Policies", parent=top, scope="team")
        self.authenticate()
        with mock.patch("origin.services.note_delete.purge_note_folders") as p:
            res = self.client.delete(
                "/api/v2/note/team/folder/?"
                + urlencode(
                    {
                        "team_id": self.team.team_id,
                        "user_id": str(self.user.id),
                        "folder_id": top.folder_id,
                    }
                )
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(p.call_args.args[0]), {top.folder_id, sub.folder_id})

    def test_narrowing_access_purges_the_folder_results_too(self):
        top = self._folder("Handbook", scope="team", visibility="public")
        sub = self._folder("Policies", parent=top, scope="team")
        with (
            mock.patch("origin.views.utils.note_acl_resync.purge_notes_from_index"),
            mock.patch("origin.search_engine.purge.purge_note_folders") as p,
        ):
            resync_folder_subtree(top.folder_id, self.team.team_id)
        self.assertEqual(set(p.call_args.args[0]), {top.folder_id, sub.folder_id})


class FolderSearchTests(BaseAPITestCase):
    def test_folders_are_typeahead_results_not_agent_grounding(self):
        def excluded(mode, entity_types=None):
            filt = S._build_filter("t", "u", entity_types, None, None, mode=mode)
            return {
                t["term"]["entity_type"]
                for f in filt
                for t in f.get("bool", {}).get("must_not", [])
                if "term" in t
            }

        self.assertNotIn("note_folder", excluded("typeahead"))
        for mode in ("ai_search", "eval"):
            self.assertIn("note_folder", excluded(mode), mode)
        # An explicit request still opts in.
        self.assertNotIn("note_folder", excluded("ai_search", ["note_folder"]))

    def _search(self, user, acl):
        hit = {
            "_id": "note_folder:7:name",
            "_source": {
                "chunk_id": "note_folder:7:name",
                "entity_type": "note_folder",
                "entity_id": "note_folder:7",
                "chunk_type": "note_folder",
                "title": "Specs",
                "folder_id": "7",
                "folder_scope": "team",
                "context_path": ["Design"],
                "context_acl": acl,
            },
        }
        client = mock.Mock(spec=["search"])
        client.search.return_value = {"hits": {"hits": [hit]}}
        with mock.patch.object(S, "get_client", return_value=client):
            return S.search(
                query="specs",
                team_id=str(self.team.team_id),
                user_id=str(user.id),
                use_vector=False,
            )["results"]

    def test_a_folder_result_carries_its_id_scope_and_a_path_for_those_who_may_see_it(self):
        row = self._search(self.user, [str(self.user.id)])[0]
        self.assertEqual((row["folder_id"], row["folder_scope"]), ("7", "team"))
        self.assertEqual(row["context_path"], ["Design"])
        self.assertFalse([k for k in row if "acl" in k.lower()])

    def test_someone_who_cant_see_the_parents_gets_the_folder_without_the_path(self):
        row = self._search(self.user2, [str(self.user.id)])[0]
        self.assertEqual(row["folder_id"], "7")
        self.assertNotIn("context_path", row)
