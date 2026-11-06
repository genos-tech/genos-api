#!/usr/bin/env python3
"""Split the test suite into shards for CI (MIP-1095).

    ci_test_shard.py <shard> <total>     print shard's test modules, space separated
    ci_test_shard.py --report <total>    print every shard's size, to eyeball the balance

CI runs one job per shard: `manage.py test $(scripts/ci_test_shard.py N 3)`.

Every file under origin/tests that Django's discovery would collect (`test*.py`) is
in exactly one shard, so the union of the shards is the whole suite. Files go to the
lightest shard first, heaviest file first, weighted by the number of test methods in
the file, so no shard gets all the big ones. The result depends only on the files, so
every job computes the same split, and a new test file lands in a shard on its own.
Stdlib only: it runs before any dependency is installed.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "origin" / "tests"
_TEST_METHOD = re.compile(r"^\s+(?:async\s+)?def test", re.MULTILINE)


def test_files() -> list[Path]:
    """The files `manage.py test origin.tests` would collect, in a fixed order."""
    return sorted(TESTS.rglob("test*.py"))


def weight(path: Path) -> int:
    # A file with no test method (a helper that only matches the name) still costs
    # an import, so it counts for one.
    return max(1, len(_TEST_METHOD.findall(path.read_text(encoding="utf-8"))))


def label(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def split(total: int) -> list[list[Path]]:
    if total < 1:
        raise ValueError("total must be at least 1")
    shards: list[list[Path]] = [[] for _ in range(total)]
    loads = [0] * total
    for path in sorted(test_files(), key=lambda p: (-weight(p), str(p))):
        lightest = min(range(total), key=lambda i: (loads[i], i))
        shards[lightest].append(path)
        loads[lightest] += weight(path)
    return shards


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--report":
        shards = split(int(argv[2]))
        for i, files in enumerate(shards, start=1):
            print(f"shard {i}: {len(files)} files, {sum(weight(f) for f in files)} tests")
        return 0
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    shard, total = int(argv[1]), int(argv[2])
    if not 1 <= shard <= total:
        print(f"shard must be between 1 and {total}", file=sys.stderr)
        return 2
    print(" ".join(sorted(label(p) for p in split(total)[shard - 1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
