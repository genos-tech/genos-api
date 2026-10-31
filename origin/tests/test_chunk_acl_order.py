"""A chunk's ACL is stored in one canonical order, so an unchanged chunk
keeps its `meta_hash` from run to run.

The failure this guards against: `task_chunker` built `acl_user_ids` with
`list(set(...))`, whose order depends on the process's string-hash seed,
so every reindex classified most task chunks as metadata-only changes.
"""

import os
import subprocess
import sys

from django.test import SimpleTestCase

from origin.search_engine.chunkers.base import Chunk
from origin.search_engine.ingestion import _meta_fingerprint, _to_os_metadata_update

IDS = [
    "fd94a22e-63a9-4048-bd11-abde49729b8e",
    "2b3a4d58-d75c-4e73-b737-b9c25c814134",
    "ecc202cd-a2d4-4104-8d9f-f5b5646bceec",
    "61e7a294-22b1-49bd-ad06-1fef6ce5c11d",
    "3337eb8d-e5f4-4c03-8a73-87352d4e29c8",
]


def _chunk(acl):
    return Chunk(
        chunk_id="task:1:title_content",
        entity_type="task",
        entity_id="task:1",
        chunk_type="task_title_content",
        team_id="team-1",
        acl_user_ids=acl,
    )


class TestCanonicalAcl(SimpleTestCase):
    def test_the_acl_is_sorted_and_deduplicated(self):
        c = _chunk([IDS[2], IDS[0], IDS[2], "team:team-1"])
        self.assertEqual(c.acl_user_ids, sorted({IDS[2], IDS[0], "team:team-1"}))

    def test_member_order_does_not_change_the_fingerprint(self):
        self.assertEqual(_meta_fingerprint(_chunk(IDS)), _meta_fingerprint(_chunk(IDS[::-1])))

    def test_the_stored_doc_and_the_metadata_update_carry_the_sorted_acl(self):
        c = _chunk(IDS[::-1])
        self.assertEqual(c.to_dict()["acl_user_ids"], sorted(IDS))
        self.assertEqual(_to_os_metadata_update(c)["doc"]["acl_user_ids"], sorted(IDS))

    def test_a_different_acl_still_changes_the_fingerprint(self):
        self.assertNotEqual(_meta_fingerprint(_chunk(IDS)), _meta_fingerprint(_chunk(IDS[:-1])))


_SCRIPT = f"""
import django
django.setup()
from origin.search_engine.chunkers.base import Chunk
from origin.search_engine.ingestion import _meta_fingerprint
acl = list(set({IDS!r}))
print(_meta_fingerprint(Chunk(chunk_id="c", entity_type="task", entity_id="task:1",
      chunk_type="task_title_content", team_id="t", acl_user_ids=acl)))
"""


class TestFingerprintAcrossHashSeeds(SimpleTestCase):
    """The real failure mode: the same set, iterated in processes with
    different `PYTHONHASHSEED`s, must fingerprint the same."""

    def test_the_fingerprint_is_the_same_under_different_hash_seeds(self):
        prints = set()
        for seed in ("1", "2", "3", "4"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [sys.executable, "-c", _SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            prints.add(out.stdout.strip().splitlines()[-1])
        self.assertEqual(len(prints), 1)
