import time

from django.core.management.base import BaseCommand
from opensearchpy.exceptions import ConnectionError as OSConnectionError

from origin.search_engine.index_config import build_index_settings
from origin.search_engine.models import RagChunk
from origin.search_engine.opensearch_client import (
    get_client,
    get_index_alias,
    get_physical_index,
)

_MAX_RETRIES = 6
_RETRY_DELAY_S = 5


class Command(BaseCommand):
    help = (
        "Create the OpenSearch chunk index and point the stable alias at "
        "it. Idempotent: existing index/alias are left alone unless "
        "--recreate is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--recreate",
            action="store_true",
            help=(
                "Delete the physical index before creating it. Destroys "
                "all indexed chunks. Use during schema/embedding-model "
                "changes."
            ),
        )

    def handle(self, *args, **options):
        client = get_client()
        physical = get_physical_index()
        alias = get_index_alias()

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._setup(client, physical, alias, options["recreate"])
                return
            except OSConnectionError as exc:
                if attempt < _MAX_RETRIES:
                    self.stderr.write(
                        f"OpenSearch not ready (attempt {attempt}/{_MAX_RETRIES}): {exc}. "
                        f"Retrying in {_RETRY_DELAY_S}s…"
                    )
                    time.sleep(_RETRY_DELAY_S)
                else:
                    self.stderr.write(
                        f"OpenSearch still unavailable after {_MAX_RETRIES} attempts. "
                        "The app will start without search index setup. "
                        "Run `manage.py opensearch_setup` once OpenSearch is ready."
                    )
                    # Exit 0 so the Dockerfile CMD continues to gunicorn.

    def _setup(self, client, physical, alias, recreate):
        if recreate and client.indices.exists(index=physical):
            self.stdout.write(f"Deleting existing index {physical}…")
            client.indices.delete(index=physical)

        if client.indices.exists(index=physical):
            self.stdout.write(f"Index {physical} already exists, skipping create.")
        else:
            body = build_index_settings()
            client.indices.create(index=physical, body=body)
            self.stdout.write(self.style.SUCCESS(f"Created index {physical}."))
            # The index is brand-new (or was just recreated), so the Postgres
            # RagChunk tracking table is now stale — it still records the
            # previous index's chunks as "already indexed". Clear it so the
            # next opensearch_reindex run treats every chunk as new and
            # re-pushes them. Without this, reindex would see them as
            # "unchanged" and skip them, leaving search returning nothing.
            deleted = RagChunk.objects.all().delete()[0]
            if deleted:
                self.stdout.write(
                    self.style.WARNING(
                        f"Cleared {deleted} stale RagChunk records — "
                        "run opensearch_reindex to repopulate the index."
                    )
                )

        # Point alias at the physical index. If the alias already exists
        # but points elsewhere, atomically swap it.
        if client.indices.exists_alias(name=alias):
            current = client.indices.get_alias(name=alias)
            current_indices = list(current.keys())
            if physical in current_indices and len(current_indices) == 1:
                self.stdout.write(f"Alias {alias} already points to {physical}.")
                return
            actions = [{"remove": {"index": idx, "alias": alias}} for idx in current_indices]
            actions.append({"add": {"index": physical, "alias": alias}})
            client.indices.update_aliases(body={"actions": actions})
            self.stdout.write(self.style.SUCCESS(f"Repointed alias {alias} → {physical}."))
        else:
            client.indices.put_alias(index=physical, name=alias)
            self.stdout.write(self.style.SUCCESS(f"Created alias {alias} → {physical}."))
