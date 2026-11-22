"""Read a Slack export ZIP safely (MIP-1836). Nothing is ever extracted to disk.

The archive is untrusted: it is checked before any entry is read (entry count, encryption, names,
declared compression ratio), and every entry is read with a hard byte cap on the REAL size, because
a header can lie. Only `channels.json`, `groups.json`, `users.json` and `<dir>/<YYYY-MM-DD>.json`
are read; DMs and group DMs are skipped and counted.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_ENTRY_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_RATIO = 100
MAX_CHANNELS = 50
MAX_MESSAGES = 20_000
MAX_USERS = 5_000

_DAY = re.compile(r"^([^/]+)/(\d{4}-\d{2}-\d{2})\.json$")


class SlackImportError(Exception):
    def __init__(self, code: str, status: int = 400, **extra):
        super().__init__(code)
        self.code, self.status, self.extra = code, status, extra


@dataclass
class Export:
    users: list = field(default_factory=list)
    channels: list = field(default_factory=list)  # public then private, each with is_private
    messages: dict = field(default_factory=dict)  # channel name -> [message dict]
    skipped_dms: int = 0


def _infos(zf):
    return zf.infolist()


def _safe_name(name: str) -> bool:
    return not (
        ".." in name.split("/")
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or re.match(r"^[A-Za-z]:", name)
    )


def _normalise(name: str, roots: set) -> str:
    if len(roots) == 1:
        prefix = next(iter(roots)) + "/"
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _load(zf, info, budget):
    with zf.open(info) as fh:
        raw = fh.read(MAX_ENTRY_BYTES + 1)
    if len(raw) > MAX_ENTRY_BYTES:
        raise SlackImportError("export_too_large", 413)
    budget[0] += len(raw)
    if budget[0] > MAX_UNCOMPRESSED_BYTES:
        raise SlackImportError("export_too_large", 413)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise SlackImportError("bad_json", file=info.filename) from None


def read_export(fileobj) -> Export:
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise SlackImportError("not_a_zip") from None
    with zf:
        infos = [i for i in _infos(zf) if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(_infos(zf)) > MAX_ENTRIES:
            raise SlackImportError("zip_unsafe", reason="entries")
        for i in infos:
            if i.flag_bits & 1:
                raise SlackImportError("zip_unsafe", reason="encrypted")
            if not _safe_name(i.filename):
                raise SlackImportError("zip_unsafe", reason="path")
            if i.file_size > MAX_ENTRY_BYTES:
                raise SlackImportError("export_too_large", 413)
            if (
                i.compress_size
                and i.file_size / i.compress_size > MAX_RATIO
                and i.file_size > 1_000_000
            ):
                raise SlackImportError("zip_unsafe", reason="ratio")
        roots = {i.filename.split("/", 1)[0] for i in infos if "/" in i.filename}
        top_files = {i.filename for i in infos if "/" not in i.filename}
        # One wrapping folder holding everything is allowed; a normal export has several top dirs.
        wrapper = roots if (len(roots) == 1 and not top_files) else set()
        by_name = {_normalise(i.filename, wrapper): i for i in infos}
        budget = [0]
        out = Export()

        def top(name):
            return _load(zf, by_name[name], budget) if name in by_name else None

        users, channels = top("users.json"), top("channels.json")
        if not isinstance(users, list) or not isinstance(channels, list):
            raise SlackImportError("not_a_slack_export")
        groups = top("groups.json")
        groups = groups if isinstance(groups, list) else []
        if len(users) > MAX_USERS:
            raise SlackImportError("too_many_users", limit=MAX_USERS)
        chans = [dict(c, is_private=False) for c in channels if isinstance(c, dict)]
        chans += [dict(c, is_private=True) for c in groups if isinstance(c, dict)]
        if len(chans) > MAX_CHANNELS:
            raise SlackImportError("too_many_channels", limit=MAX_CHANNELS)
        out.users = [u for u in users if isinstance(u, dict)]
        out.channels = chans
        for name in ("dms.json", "mpims.json"):
            if name in by_name:
                out.skipped_dms += 1
        known = {c.get("name") for c in chans}
        total = 0
        for name in sorted(by_name):
            m = _DAY.match(name)
            if not m:
                continue
            chan = m.group(1)
            if chan not in known:
                out.skipped_dms += 1
                continue
            day = _load(zf, by_name[name], budget)
            if not isinstance(day, list):
                raise SlackImportError("bad_json", file=name)
            msgs = [x for x in day if isinstance(x, dict)]
            total += len(msgs)
            if total > MAX_MESSAGES:
                raise SlackImportError("too_many_messages", limit=MAX_MESSAGES)
            out.messages.setdefault(chan, []).extend(msgs)
    return out
