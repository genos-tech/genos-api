"""Hand-written Slack export fixtures (MIP-1836). Invented names and addresses only: never real data."""

import io
import json
import zipfile

USERS = [
    {
        "id": "U1",
        "name": "ann",
        "real_name": "Ann Example",
        "profile": {"email": "test@example.com", "display_name": "ann"},
    },
    {
        "id": "U2",
        "name": "bob",
        "real_name": "Bob Example",
        "profile": {"email": "OTHER@example.com", "display_name": ""},
    },
    {
        "id": "U3",
        "name": "cy",
        "real_name": "Cy Outsider",
        "profile": {"email": "cy@elsewhere.test"},
    },
    {
        "id": "U4",
        "name": "bot",
        "real_name": "Robot",
        "is_bot": True,
        "profile": {"email": "bot@example.com"},
    },
    {
        "id": "U5",
        "name": "dee",
        "real_name": "Dee Gone",
        "deleted": True,
        "profile": {"email": "dee@example.com"},
    },
]

CHANNELS = [
    {
        "id": "C1",
        "name": "general",
        "is_general": True,
        "is_archived": False,
        "members": ["U1", "U2", "U3"],
    },
    {"id": "C2", "name": "old-stuff", "is_archived": True, "members": ["U1"]},
]
GROUPS = [{"id": "G1", "name": "secret", "members": ["U1", "U3"]}]

# 2024-03-05 12:00:00 UTC == 1709640000
T0 = 1709640000


def msg(ts_off, user, text, **extra):
    return {"type": "message", "user": user, "text": text, "ts": f"{T0 + ts_off}.000100", **extra}


GENERAL_DAY = [
    msg(
        0,
        "U1",
        "Hello *team* &amp; <@U2>, see <https://example.com/doc|the doc> in <#C2|old-stuff>",
        thread_ts=f"{T0}.000100",
        reply_count=2,
        reactions=[{"name": "thumbsup", "users": ["U2", "U3"], "count": 2}],
    ),
    msg(60, "U2", "reply one", thread_ts=f"{T0}.000100"),
    msg(120, "U3", "outsider reply", thread_ts=f"{T0}.000100"),
    msg(180, "U1", "joined", subtype="channel_join"),
    msg(
        240,
        "U2",
        "file here",
        files=[{"name": "plan.pdf", "permalink": "https://files.slack.test/plan.pdf"}],
    ),
    msg(300, "U4", "bot says hi", subtype="bot_message", username="Robot"),
    msg(360, "U2", "```\ncode\n```\n&gt; quoted"),
    msg(420, "U2", "orphan", thread_ts="1.000100"),
]
OLD_DAY = [msg(0, "U1", "archived history")]
SECRET_DAY = [msg(0, "U1", "private talk")]


def tree(extra_days=None):
    files = {
        "users.json": USERS,
        "channels.json": CHANNELS,
        "groups.json": GROUPS,
        "general/2024-03-05.json": GENERAL_DAY,
        "old-stuff/2024-03-05.json": OLD_DAY,
        "secret/2024-03-05.json": SECRET_DAY,
    }
    files.update(extra_days or {})
    return files


def build_zip(files=None, raw=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in (files if files is not None else tree()).items():
            zf.writestr(name, json.dumps(content))
        for name, content in (raw or {}).items():
            zf.writestr(name, content)
    buf.seek(0)
    buf.name = "export.zip"
    return buf
