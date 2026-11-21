"""`opensearch_setup` must not touch an alias or index it does not manage (MIP-1762).

Found when an isolated api container set only `OPENSEARCH_INDEX=via_shot_chunks`
against the dev OpenSearch: setup still aliased the SHARED name
`knowledge_chunks_current`, created its index, cleared the RagChunk table and only
then died on `invalid_alias_name_exception` (a plain index of that name existed).
Had it been a real alias, the run would have re-pointed it.

OpenSearch is never touched: the client is a MagicMock. Under test are the name
rules (`index_names`) and the refuse-before-writing guard in the command.
"""

import os
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from origin.search_engine.index_names import (
    DEFAULT_ALIAS,
    DEFAULT_INDEX,
    index_family,
    resolve_index_names,
)
from origin.search_engine.management.commands import opensearch_setup


class TestResolveIndexNames(SimpleTestCase):
    def test_nothing_overridden_is_the_production_default(self):
        self.assertEqual(
            resolve_index_names(None, None), ("knowledge_chunks_v1", "knowledge_chunks_current")
        )
        self.assertEqual((DEFAULT_INDEX, DEFAULT_ALIAS), resolve_index_names("", ""))

    def test_managed_family_keeps_the_shared_alias(self):
        # Compose (`${OPENSEARCH_INDEX:-knowledge_chunks_v1}`), CI (`_v3`) and Railway set the
        # index like this; the documented bump is "v1 -> v2, setup, reindex" behind ONE alias.
        for index in (
            "knowledge_chunks_v1",
            "knowledge_chunks_v2",
            "knowledge_chunks_v3",
            "knowledge_chunks_v10",
        ):
            with self.subTest(index=index):
                self.assertEqual(
                    resolve_index_names(index, None), (index, "knowledge_chunks_current")
                )

    def test_a_name_of_its_own_gets_its_own_alias(self):
        self.assertEqual(
            resolve_index_names("via_shot_chunks", None),
            ("via_shot_chunks", "via_shot_chunks_current"),
        )
        # The eval RUNBOOK's example names.
        self.assertEqual(
            resolve_index_names("knowledge_chunks_evalpaid", ""),
            ("knowledge_chunks_evalpaid", "knowledge_chunks_evalpaid_current"),
        )
        # A look-alike is not the managed family.
        for index in ("knowledge_chunks_v1_demo", "knowledge_chunks_vX", "my_knowledge_chunks_v1"):
            with self.subTest(index=index):
                self.assertEqual(resolve_index_names(index, None)[1], f"{index}_current")

    def test_explicit_alias_always_wins(self):
        self.assertEqual(
            resolve_index_names("via_shot_chunks", "shot_alias"), ("via_shot_chunks", "shot_alias")
        )
        self.assertEqual(
            resolve_index_names("knowledge_chunks_v2", "other"), ("knowledge_chunks_v2", "other")
        )
        self.assertEqual(resolve_index_names(None, "other"), ("knowledge_chunks_v1", "other"))

    def test_family_strips_only_the_version_suffix(self):
        self.assertEqual(index_family("knowledge_chunks_v2"), "knowledge_chunks")
        self.assertEqual(index_family("knowledge_chunks_v1"), index_family("knowledge_chunks_v12"))
        self.assertEqual(index_family("via_shot_chunks"), "via_shot_chunks")

    def test_settings_use_the_resolver(self):
        index, alias = resolve_index_names(
            os.environ.get("OPENSEARCH_INDEX"), os.environ.get("OPENSEARCH_ALIAS")
        )
        self.assertEqual(settings.SEARCH_ENGINE["OPENSEARCH_INDEX"], index)
        self.assertEqual(settings.SEARCH_ENGINE["OPENSEARCH_ALIAS"], alias)


class SetupTestCase(SimpleTestCase):
    """Runs the command against a fake cluster: `indices` / `aliases` are plain dicts."""

    def run_setup(self, physical, alias, *, indices=(), aliases=None, **options):
        aliases = aliases or {}  # alias name -> [index names]
        client = mock.MagicMock()
        indices = set(indices) | {i for targets in aliases.values() for i in targets}
        client.indices.exists.side_effect = lambda index: index in indices or index in aliases
        client.indices.exists_alias.side_effect = lambda name: name in aliases
        client.indices.get_alias.side_effect = lambda name: {i: {} for i in aliases[name]}
        rag = mock.MagicMock()
        rag.objects.all.return_value.delete.return_value = (3, {})
        with (
            mock.patch.object(opensearch_setup, "get_client", return_value=client),
            mock.patch.object(opensearch_setup, "get_physical_index", return_value=physical),
            mock.patch.object(opensearch_setup, "get_index_alias", return_value=alias),
            mock.patch.object(opensearch_setup, "RagChunk", rag),
        ):
            out = StringIO()
            try:
                call_command("opensearch_setup", stdout=out, stderr=StringIO(), **options)
            finally:
                self.client, self.rag, self.out = client, rag, out

    def assert_nothing_written(self):
        c = self.client.indices
        c.create.assert_not_called()
        c.delete.assert_not_called()
        c.put_alias.assert_not_called()
        c.update_aliases.assert_not_called()
        c.put_mapping.assert_not_called()
        self.rag.objects.all.assert_not_called()


class TestRefusals(SetupTestCase):
    def test_refuses_when_an_index_is_named_like_the_alias(self):
        # The dev instance: a plain, auto-created `knowledge_chunks_current` index.
        with self.assertRaises(CommandError) as ctx:
            self.run_setup(
                "via_shot_chunks", "knowledge_chunks_current", indices={"knowledge_chunks_current"}
            )
        self.assertIn("knowledge_chunks_current", str(ctx.exception))
        self.assertIn("OPENSEARCH_ALIAS", str(ctx.exception))
        self.assert_nothing_written()

    def test_refusal_comes_before_recreate_deletes_anything(self):
        with self.assertRaises(CommandError):
            self.run_setup(
                "via_shot_chunks",
                "knowledge_chunks_current",
                indices={"knowledge_chunks_current", "via_shot_chunks"},
                recreate=True,
            )
        self.assert_nothing_written()

    def test_refuses_when_index_and_alias_are_the_same_name(self):
        with self.assertRaises(CommandError):
            self.run_setup("same", "same")
        self.assert_nothing_written()

    def test_refuses_to_repoint_an_alias_that_belongs_to_another_family(self):
        # A real shared alias -> knowledge_chunks_v1; an unrelated index asks to take it over.
        with self.assertRaises(CommandError) as ctx:
            self.run_setup(
                "via_shot_chunks",
                "knowledge_chunks_current",
                aliases={"knowledge_chunks_current": ["knowledge_chunks_v1"]},
            )
        self.assertIn("knowledge_chunks_v1", str(ctx.exception))
        self.assert_nothing_written()

    def test_refuses_when_any_one_target_is_foreign(self):
        with self.assertRaises(CommandError):
            self.run_setup(
                "knowledge_chunks_v2",
                "knowledge_chunks_current",
                aliases={"knowledge_chunks_current": ["knowledge_chunks_v1", "other_index"]},
            )
        self.assert_nothing_written()


class TestUnchangedBehaviour(SetupTestCase):
    def test_fresh_cluster_creates_the_index_and_the_alias(self):
        self.run_setup("knowledge_chunks_v1", "knowledge_chunks_current")
        c = self.client.indices
        c.create.assert_called_once()
        self.assertEqual(c.create.call_args.kwargs["index"], "knowledge_chunks_v1")
        c.put_alias.assert_called_once_with(
            index="knowledge_chunks_v1", name="knowledge_chunks_current"
        )
        self.rag.objects.all.assert_called_once()

    def test_isolated_names_set_up_cleanly_next_to_the_shared_alias(self):
        # The recipe: own index, derived alias, while the shared alias stays untouched.
        self.run_setup(
            "via_shot_chunks",
            "via_shot_chunks_current",
            aliases={"knowledge_chunks_current": ["knowledge_chunks_v1"]},
        )
        c = self.client.indices
        c.put_alias.assert_called_once_with(index="via_shot_chunks", name="via_shot_chunks_current")
        c.update_aliases.assert_not_called()

    def test_version_bump_still_repoints_the_alias_atomically(self):
        # The documented zero-downtime migration: v1 -> v2 behind the same alias.
        self.run_setup(
            "knowledge_chunks_v2",
            "knowledge_chunks_current",
            aliases={"knowledge_chunks_current": ["knowledge_chunks_v1"]},
        )
        actions = self.client.indices.update_aliases.call_args.kwargs["body"]["actions"]
        self.assertEqual(
            actions,
            [
                {"remove": {"index": "knowledge_chunks_v1", "alias": "knowledge_chunks_current"}},
                {"add": {"index": "knowledge_chunks_v2", "alias": "knowledge_chunks_current"}},
            ],
        )

    def test_rerun_with_everything_in_place_is_a_noop(self):
        self.run_setup(
            "knowledge_chunks_v1",
            "knowledge_chunks_current",
            aliases={"knowledge_chunks_current": ["knowledge_chunks_v1"]},
        )
        c = self.client.indices
        c.create.assert_not_called()
        c.put_alias.assert_not_called()
        c.update_aliases.assert_not_called()
        self.assertIn("already points", self.out.getvalue())
