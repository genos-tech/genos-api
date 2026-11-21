"""Which OpenSearch alias an `OPENSEARCH_INDEX` belongs to (MIP-1762).

Pure string rules with no Django or OpenSearch imports, so `apis/settings.py`
can use them while it is still being read and a test can feed them any env.

The app queries a stable ALIAS (`knowledge_chunks_current`); a physical index
named `knowledge_chunks_v<N>` sits behind it, and an embedding-model or mapping
change is "bump N, `opensearch_setup`, reindex": the alias stays, the index
moves (Railway, compose and CI all set `OPENSEARCH_INDEX` that way).

An ISOLATED run (a demo, an eval) sets `OPENSEARCH_INDEX` to a name of its own
instead. Before this module it still acted on the shared alias name, so against
a dev OpenSearch it either failed half-way (a plain index of that name exists)
or re-pointed a real shared alias. Now such a name gets an alias of its own.
"""

import re

DEFAULT_INDEX = "knowledge_chunks_v1"
DEFAULT_ALIAS = "knowledge_chunks_current"

# The managed family: `knowledge_chunks_v1`, `_v2`, ... all sit behind DEFAULT_ALIAS.
_MANAGED_INDEX = re.compile(r"knowledge_chunks_v\d+")
_VERSION_SUFFIX = re.compile(r"_v\d+$")


def resolve_index_names(index_env, alias_env):
    """The `(index, alias)` to use for the raw `OPENSEARCH_INDEX` / `OPENSEARCH_ALIAS` values.

    * `OPENSEARCH_ALIAS` set: it wins, always (the explicit override).
    * otherwise an index of the managed family (`knowledge_chunks_v<N>`), or no index at all, keeps
      `knowledge_chunks_current`: byte-identical to production, compose and CI.
    * otherwise (a name outside the family) the alias is `<index>_current`, so an isolated run never
      touches the shared alias.

    Empty strings count as unset.
    """
    index = (index_env or "").strip() or DEFAULT_INDEX
    alias = (alias_env or "").strip()
    if not alias:
        alias = DEFAULT_ALIAS if _MANAGED_INDEX.fullmatch(index) else f"{index}_current"
    return index, alias


def index_family(name):
    """`knowledge_chunks_v2` -> `knowledge_chunks`; `via_shot_chunks` -> itself."""
    return _VERSION_SUFFIX.sub("", name)
