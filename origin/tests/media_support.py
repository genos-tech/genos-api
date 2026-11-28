"""The temporary MEDIA_ROOT of a test module."""

import multiprocessing
import shutil


def remove_media_root(path) -> None:
    """Remove a module's temporary MEDIA_ROOT, for `tearDownClass`.

    Not in a `manage.py test --parallel` worker (MIP-2447): the module is imported before the
    workers fork, so they all share the one directory, and a class that finishes in one worker
    would delete the files another worker's class is still using. The directory is left in the
    system's temporary files then.
    """
    if multiprocessing.parent_process() is None:
        shutil.rmtree(path, ignore_errors=True)
