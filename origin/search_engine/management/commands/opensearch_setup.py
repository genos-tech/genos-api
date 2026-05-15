from django.core.management.base import BaseCommand

from origin.search_engine.index_config import build_index_settings
from origin.search_engine.opensearch_client import (
    get_client,
    get_index_alias,
    get_physical_index,
)


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

        if options["recreate"] and client.indices.exists(index=physical):
            self.stdout.write(f"Deleting existing index {physical}...")
            client.indices.delete(index=physical)

        if client.indices.exists(index=physical):
            self.stdout.write(f"Index {physical} already exists, skipping create.")
        else:
            body = build_index_settings()
            client.indices.create(index=physical, body=body)
            self.stdout.write(self.style.SUCCESS(f"Created index {physical}."))

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
