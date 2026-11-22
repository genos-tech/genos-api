"""Import a Slack export into group chats (MIP-1836). The ZIP is read by `slack_import_zip`.

Why it has its own writer: the normal send path (`_allocate_seq_and_create_message_with_activities`)
queues a webhook, writes mention and activity rows and web pushes, and locks the channel once per
message. An import must notify nobody, so this writes Message rows in bulk and touches none of that.
Imported text reaches search through the normal reindexer pass (it picks up new messages by
`ts_updated_at`), so nothing in `search_engine` is edited.

No model, no migration. What marks an import, all in existing columns:
  * `Message.correlation_id` = `message_key(...)`: the idempotency key AND how a re-import finds
    the chat an earlier run made (the partial unique index per channel makes it cheap);
  * `Message.metadata["slackImport"]` = {run, ch, ts, author?, files?}: undo finds a run's rows;
  * one banner message per channel and run, `correlation_id = slack-run:<run hex>:<slack channel>`:
    the run record and the "this run created the chat" marker.
No e-mail address is ever stored in a message: a match is made in memory and only the Genos user is kept.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from datetime import datetime
from datetime import timezone as dt_tz

from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageReaction,
    ReadCursor,
)
from origin.models.common.team_models import TeamMembers
from origin.services import slack_import_zip as z
from origin.services.slack_mrkdwn import MAX_TEXT_CHARS, mrkdwn_to_blocks

logger = logging.getLogger(__name__)

SlackImportError = z.SlackImportError
BULK_BATCH = 500
RUNS_LIMIT = 50
CORR_PREFIX = "slack:"
BANNER_PREFIX = "slack-run:"
MAX_REACTIONS_PER_MESSAGE = 50
SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "bot_add",
    "bot_remove",
    "tombstone",
    "pinned_item",
}
EMOJI = {
    "thumbsup": "👍",
    "+1": "👍",
    "thumbsdown": "👎",
    "-1": "👎",
    "heart": "❤️",
    "smile": "😄",
    "smiley": "😃",
    "grinning": "😀",
    "joy": "😂",
    "laughing": "😆",
    "wink": "😉",
    "tada": "🎉",
    "clap": "👏",
    "pray": "🙏",
    "fire": "🔥",
    "eyes": "👀",
    "100": "💯",
    "rocket": "🚀",
    "white_check_mark": "✅",
    "heavy_check_mark": "✔️",
    "x": "❌",
    "warning": "⚠️",
    "thinking_face": "🤔",
    "raised_hands": "🙌",
    "ok_hand": "👌",
    "muscle": "💪",
    "star": "⭐",
    "sob": "😭",
    "cry": "😢",
    "confused": "😕",
    "slightly_smiling_face": "🙂",
    "wave": "👋",
    "sparkles": "✨",
    "bulb": "💡",
    "memo": "📝",
    "question": "❓",
    "exclamation": "❗",
    "party_popper": "🎉",
    "point_up": "☝️",
    "heavy_plus_sign": "➕",
}


def message_key(team_id, slack_channel_id, ts) -> str:
    digest = hashlib.sha256(f"{team_id}:{slack_channel_id}:{ts}".encode()).hexdigest()
    return CORR_PREFIX + digest[:58]


def _ts(ts) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _dt(ts) -> datetime:
    return datetime.fromtimestamp(_ts(ts), tz=dt_tz.utc)


# ---- users ------------------------------------------------------------------


def _display(u: dict) -> str:
    p = u.get("profile") or {}
    return str(p.get("display_name") or u.get("real_name") or u.get("name") or u.get("id") or "")[
        :80
    ]


def _roster(team) -> dict:
    """lower-cased e-mail -> user, for the team's live members only (guests have no row)."""
    ids = list(
        TeamMembers.objects.filter(team=team, is_deleted=False, attendee__isnull=False).values_list(
            "attendee_id", flat=True
        )
    )
    if team.owner_id:
        ids.append(team.owner_id)
    from origin.models.common.user_models import CustomUser

    out = {}
    for user in CustomUser.objects.filter(id__in=set(ids)):
        if getattr(user, "is_deleted", False) or not user.is_active:
            continue
        email = (user.email or "").strip().lower()
        if email:
            out[email] = user
    return out


def match_users(team, export) -> dict:
    """slack user id -> {slack_id, name, email, status, user}. Never creates an account."""
    roster = _roster(team)
    out = {}
    for u in export.users:
        sid = str(u.get("id") or "")
        if not sid:
            continue
        email = ((u.get("profile") or {}).get("email") or "").strip().lower()
        user = None
        if u.get("is_bot") or u.get("is_app_user"):
            status = "bot"
        elif u.get("deleted"):
            status = "deleted"
        elif not email:
            status = "no_email"
        else:
            user = roster.get(email)
            status = "matched" if user else "unmatched"
        out[sid] = {
            "slack_id": sid,
            "name": _display(u),
            "email": email,
            "status": status,
            "user": user,
        }
    return out


# ---- messages ---------------------------------------------------------------


def _prepare(team, chan, msgs, users):
    """The importable messages of one channel, oldest first, with their keys and thread links."""
    cid = str(chan.get("id") or chan.get("name"))
    out, skipped = [], Counter()
    for m in msgs:
        ts = m.get("ts")
        if not ts:
            continue
        if m.get("subtype") in SKIP_SUBTYPES:
            skipped["subtype"] += 1
            continue
        thread_ts = m.get("thread_ts")
        out.append(
            {
                "raw": m,
                "ts": str(ts),
                "key": message_key(team.team_id, cid, ts),
                "thread_ts": str(thread_ts) if thread_ts else None,
                "is_reply": bool(thread_ts) and str(thread_ts) != str(ts),
                "user": users.get(str(m.get("user") or "")),
            }
        )
    out.sort(key=lambda x: _ts(x["ts"]))
    return out, skipped


def _author_name(item) -> str:
    m = item["raw"]
    u = item["user"]
    if u:
        return u["name"]
    return str(
        m.get("username") or (m.get("bot_profile") or {}).get("name") or m.get("user") or "Slack"
    )[:80]


def _existing_keys(channel_ids, keys) -> dict:
    """key -> channel id for messages already imported into any of `channel_ids`."""
    found = {}
    keys = list(keys)
    for i in range(0, len(keys), 1000):
        for ch, k in Message.objects.filter(
            channel_id__in=channel_ids, correlation_id__in=keys[i : i + 1000]
        ).values_list("channel_id", "correlation_id"):
            found[k] = ch
    return found


def _team_gms(team):
    return Channel.objects.filter(team=team, kind=ChannelKind.GM, is_deleted=False)


def _import_created_channels(team, cid, chan):
    """Live chats an earlier import of this Slack channel created: a banner the OWNER posted says so.

    Only these may be mapped to. A message key is derivable from public ids, and `correlation_id`
    is client-supplied on a normal send, so matching on keys alone would let a member plant one in
    a chat of their own and have the history imported there.
    """
    banners = Message.objects.filter(
        channel__team=team,
        channel__kind=ChannelKind.GM,
        channel__is_deleted=False,
        channel__is_private=bool(chan.get("is_private")),
        channel__is_external=False,
        sender_id=team.owner_id,
        correlation_id__startswith=BANNER_PREFIX,
        metadata__slackImport__ch=cid,
        metadata__slackImport__created_channel=True,
    )
    return list(banners.values_list("channel_id", flat=True).distinct())


def _mapped_channel(team, items, cid, chan):
    """The live group chat an earlier run put these messages in (the one with the most), or None."""
    gms = _import_created_channels(team, cid, chan)
    if not gms or not items:
        return None, {}
    found = _existing_keys(gms, [i["key"] for i in items])
    if not found:
        return None, {}
    best = Counter(found.values()).most_common(1)[0][0]
    return Channel.objects.get(pk=best), found


def _title_for(team, name, mapped):
    taken = {
        t.lower()
        for t in _team_gms(team)
        .exclude(pk=getattr(mapped, "pk", None))
        .values_list("title", flat=True)
    }
    if name.lower() not in taken:
        return name
    for n in range(1, 100):
        cand = f"{name} (Slack)" if n == 1 else f"{name} (Slack {n})"
        if cand.lower() not in taken:
            return cand
    return f"{name} (Slack {uuid.uuid4().hex[:4]})"


def resolve(team, export, *, channel_ids=None) -> dict:
    """Everything the preview shows and the commit uses. Pure read."""
    users = match_users(team, export)
    chosen = set(channel_ids) if channel_ids is not None else None
    counts = Counter()
    warnings = Counter()
    chans, msg_count_by_user = [], Counter()
    for chan in export.channels:
        cid = str(chan.get("id") or chan.get("name"))
        if chosen is not None and cid not in chosen:
            continue
        items, skipped = _prepare(team, chan, export.messages.get(chan.get("name"), []), users)
        counts["skipped_subtypes"] += skipped["subtype"]
        mapped, found = _mapped_channel(team, items, cid, chan)
        new_items = [i for i in items if i["key"] not in found]
        ids = {i["ts"] for i in items}
        orphans = sum(1 for i in items if i["is_reply"] and i["thread_ts"] not in ids)
        warnings["orphan_replies"] += orphans
        for i in items:
            counts["reactions"] += sum(
                len(r.get("users") or []) for r in i["raw"].get("reactions") or []
            )
            if i["raw"].get("files"):
                counts["files"] += len(i["raw"]["files"])
                warnings["file_links_need_slack_login"] += 1
            u = i["user"]
            if u is None or u["status"] != "matched":
                warnings["unmatched_authors"] += 1
            if u:
                msg_count_by_user[u["slack_id"]] += 1
        counts["messages"] += len(new_items)
        counts["already_imported"] += len(items) - len(new_items)
        counts["replies"] += sum(1 for i in new_items if i["is_reply"])
        counts["threads"] += len(
            {i["thread_ts"] for i in items if i["is_reply"] and i["thread_ts"] in ids}
        )
        members = _member_users(chan, items, users)
        archived = mapped is not None and mapped.archived_at is not None and bool(new_items)
        action = "skip" if archived else ("existing" if mapped else "create")
        warns = ["target_archived"] if archived else []
        chans.append(
            {
                "slack_id": cid,
                "name": str(chan.get("name") or cid),
                "is_private": bool(chan.get("is_private")),
                "is_archived": bool(chan.get("is_archived")),
                "is_general": bool(chan.get("is_general")),
                "message_count": len(items),
                "already_imported": len(items) - len(new_items),
                "target": {
                    "action": action,
                    "channel_id": str(mapped.id) if mapped else None,
                    "title": mapped.title
                    if mapped
                    else _title_for(team, str(chan.get("name") or cid), None),
                },
                "member_count": len(members),
                "warnings": warns,
                "_items": items,
                "_new": new_items,
                "_mapped": mapped,
                "_chan": chan,
            }
        )
        counts["channels"] += 1
        counts["to_create"] += action == "create"
        counts["existing"] += action == "existing"
    if export.skipped_dms:
        warnings["dms_skipped"] = export.skipped_dms
    if counts["messages"] + counts["already_imported"] == 0:
        raise SlackImportError("nothing_to_import")
    return {
        "users": users,
        "user_message_counts": msg_count_by_user,
        "channels": chans,
        "counts": {
            k: counts[k]
            for k in (
                "channels",
                "to_create",
                "existing",
                "messages",
                "already_imported",
                "replies",
                "threads",
                "reactions",
                "files",
                "skipped_subtypes",
            )
        },
        "warnings": [{"code": c, "count": n} for c, n in warnings.items()],
    }


def _member_users(chan, items, users):
    ids = set(map(str, chan.get("members") or [])) | {
        i["user"]["slack_id"] for i in items if i["user"]
    }
    return {users[s]["user"].id: users[s]["user"] for s in ids if s in users and users[s]["user"]}


def public_preview(resolved, sha, limits) -> dict:
    return {
        "file_sha256": sha,
        "limits": limits,
        "counts": resolved["counts"],
        "warnings": resolved["warnings"],
        "users": [
            {
                "slack_id": u["slack_id"],
                "name": u["name"],
                "email": u["email"],
                "status": u["status"],
                "genos_user": {"id": str(u["user"].id), "username": u["user"].username}
                if u["user"]
                else None,
                "message_count": resolved["user_message_counts"].get(u["slack_id"], 0),
            }
            for u in resolved["users"].values()
        ],
        "channels": [
            {k: v for k, v in c.items() if not k.startswith("_")} for c in resolved["channels"]
        ],
    }


# ---- commit -----------------------------------------------------------------


def _meta(run, cid, item, **extra):
    m = {"run": str(run), "ch": cid, "ts": item["ts"], **extra}
    return {"slackImport": m}


def commit(*, team, user, export, resolved, filename, file_sha256, request=None) -> dict:
    run = uuid.uuid4()
    report, created_msgs, created_chans = [], 0, 0
    users = resolved["users"]
    id_to_name = {s: u["name"] for s, u in users.items()}
    name_by_chan = {str(c["_chan"].get("id")): c["name"] for c in resolved["channels"]}
    for c in resolved["channels"]:
        entry = {"channel": c["name"], "slack_id": c["slack_id"]}
        if c["target"]["action"] == "skip":
            entry.update(outcome="skipped", warning="target_archived")
            report.append(entry)
            continue
        try:
            with transaction.atomic():
                res = _commit_channel(
                    team, user, run, c, users, id_to_name, name_by_chan, filename, request
                )
            entry.update(outcome="imported", **res)
            created_msgs += res["messages"]
            created_chans += res["channel_created"]
        except Exception:  # noqa: BLE001 - one chat failing must not stop the others
            logger.exception("[slack_import] channel failed")
            entry.update(outcome="failed", code="channel_failed")
        report.append(entry)
    logger.info(
        "slack import team=%s run=%s channels=%d messages=%d sha=%s",
        team.team_id,
        run,
        created_chans,
        created_msgs,
        file_sha256[:12],
    )
    return {
        "run_id": str(run),
        "channels_created": created_chans,
        "messages_created": created_msgs,
        "skipped": sum(1 for r in report if r["outcome"] == "skipped"),
        "failed": sum(1 for r in report if r["outcome"] == "failed"),
        "report": report,
    }


def _commit_channel(team, user, run, c, users, id_to_name, name_by_chan, filename, request):
    chan, items, new_items = c["_chan"], c["_items"], c["_new"]
    mapped = c["_mapped"]
    cid = c["slack_id"]
    created = mapped is None
    if not created and not new_items:
        return {"messages": 0, "channel_id": str(mapped.id), "channel_created": 0}
    members = _member_users(chan, items, users)
    if created:
        channel = Channel.objects.create(
            team=team,
            kind=ChannelKind.GM,
            title=_title_for(team, c["name"], None),
            owner=user,
            is_private=bool(chan.get("is_private")),
        )
        ChannelMember.objects.create(channel=channel, user=user, role="owner")
    else:
        channel = mapped
    have = set(ChannelMember.objects.filter(channel=channel).values_list("user_id", flat=True))
    added = []
    for uid, u in members.items():
        if uid not in have and uid != user.id:
            ChannelMember.objects.create(channel=channel, user=u, role="member")
            have.add(uid)
            added.append(str(uid))
    live = set(
        ChannelMember.objects.filter(channel=channel, is_deleted=False).values_list(
            "user_id", flat=True
        )
    )

    Channel.objects.select_for_update().filter(pk=channel.pk).first()
    last_seq = Message.objects.filter(channel=channel).aggregate(m=Max("seq"))["m"] or 0
    ids = {i["key"]: uuid.uuid4() for i in new_items}
    roots_db = {}
    need = {
        message_key(team.team_id, cid, i["thread_ts"]) for i in new_items if i["is_reply"]
    } - set(ids)
    if need:
        roots_db = {
            k: pk
            for pk, k in Message.objects.filter(
                channel=channel, correlation_id__in=list(need)
            ).values_list("id", "correlation_id")
        }
    channel_names = {k: v for k, v in name_by_chan.items()}
    objs, bump_new, bump_old, now = [], Counter(), Counter(), timezone.now()
    for n, item in enumerate(new_items, start=1):
        raw = item["raw"]
        text = str(raw.get("text") or "")
        truncated = len(text) > MAX_TEXT_CHARS
        body = mrkdwn_to_blocks(text, id_to_name, channel_names)
        files = [
            {
                "name": str(f.get("name") or f.get("title") or "file")[:200],
                "url": str(f.get("permalink") or "")[:500],
            }
            for f in raw.get("files") or []
            if isinstance(f, dict)
        ]
        for f in files:
            body.append(
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "📎 ", "styles": {}},
                        {
                            "type": "link",
                            "href": f["url"],
                            "content": [{"type": "text", "text": f["name"], "styles": {}}],
                        }
                        if f["url"].startswith(("http://", "https://"))
                        else {"type": "text", "text": f["name"], "styles": {}},
                    ],
                }
            )
        u = item["user"]
        sender = u["user"] if u and u["status"] == "matched" and u["user"].id in live else None
        extra = {}
        if sender is None:
            extra["author"] = _author_name(item)
        if files:
            extra["files"] = files
        if truncated:
            extra["truncated"] = True
        parent_id = root_id = None
        if item["is_reply"]:
            rk = message_key(team.team_id, cid, item["thread_ts"])
            root_id = ids.get(rk) or roots_db.get(rk)
            if root_id is not None:
                parent_id = root_id
                (bump_new if rk in ids else bump_old)[rk if rk in ids else root_id] += 1
        objs.append(
            Message(
                id=ids[item["key"]],
                channel=channel,
                sender=sender,
                seq=last_seq + n,
                body=body,
                body_text=_first_line(body),
                parent_id=parent_id,
                thread_root_id=root_id,
                is_thread_reply=root_id is not None,
                metadata=_meta(run, cid, item, **extra),
                correlation_id=item["key"],
                reply_count=0,
            )
        )
    by_id = {o.id: o for o in objs}
    for rk, n in bump_new.items():
        by_id[ids[rk]].reply_count = n
    Message.objects.bulk_create(objs, batch_size=BULK_BATCH)
    for o, item in zip(objs, new_items):
        o.ts_sent_at = _dt(item["ts"])
    Message.objects.bulk_update(objs, ["ts_sent_at"], batch_size=BULK_BATCH)
    for pk, n in bump_old.items():
        Message.objects.filter(pk=pk).update(reply_count=F("reply_count") + n, ts_updated_at=now)

    reactions = []
    for o, item in zip(objs, new_items):
        for r in (item["raw"].get("reactions") or [])[:MAX_REACTIONS_PER_MESSAGE]:
            emoji = EMOJI.get(str(r.get("name") or "").split("::")[0])
            if not emoji:
                continue
            for sid in r.get("users") or []:
                mu = users.get(str(sid))
                if mu and mu["user"] and mu["user"].id in live:
                    reactions.append(MessageReaction(message=o, user=mu["user"], emoji=emoji))
    MessageReaction.objects.bulk_create(reactions, batch_size=BULK_BATCH, ignore_conflicts=True)

    banner = Message.objects.create(
        channel=channel,
        sender=user,
        seq=last_seq + len(objs) + 1,
        body=[
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "styles": {},
                        "text": f"Imported from Slack #{c['name']}: {len(objs)} messages",
                    }
                ],
            }
        ],
        body_text=f"Imported from Slack #{c['name']}: {len(objs)} messages",
        metadata={
            "slackImport": {
                "kind": "banner",
                "run": str(run),
                "ch": cid,
                "created_channel": created,
                "added_members": added,
                "filename": (filename or "")[:120],
                "messages": len(objs),
                "at": now.isoformat(),
            }
        },
        correlation_id=f"{BANNER_PREFIX}{run.hex}:{cid}"[:64],
    )
    # A member who had read everything before the import has read the banner too; one with unread
    # messages from before keeps them unread.
    cursors = {
        cu.user_id: cu
        for cu in ReadCursor.objects.filter(channel=channel, thread_root=None).select_related(
            "last_read_message"
        )
    }
    for uid in live:
        cur = cursors.get(uid)
        if cur is None:
            if created:
                ReadCursor.objects.create(
                    user_id=uid, channel=channel, thread_root=None, last_read_message=banner
                )
        elif cur.last_read_message is not None and cur.last_read_message.seq >= last_seq:
            ReadCursor.objects.filter(pk=cur.pk).update(last_read_message=banner)
    if chan.get("is_archived") and created:
        from origin.services.archive import archive_group_chat

        archive_group_chat(channel, request=request, actor=user)
    return {"messages": len(objs), "channel_id": str(channel.id), "channel_created": int(created)}


def _first_line(body) -> str:
    from origin.views.chat.modules.common import generate_first_line

    try:
        return generate_first_line.get(body[0]) or ""
    except Exception:  # noqa: BLE001
        return ""


# ---- runs and undo ----------------------------------------------------------


def _banners(team, run_id=None):
    qs = Message.objects.filter(
        channel__team=team,
        channel__kind=ChannelKind.GM,
        channel__is_deleted=False,
        correlation_id__startswith=BANNER_PREFIX,
    )
    if run_id is not None:
        qs = qs.filter(correlation_id__startswith=f"{BANNER_PREFIX}{run_id.hex}:")
    return qs


def runs(team) -> list:
    out = {}
    for b in _banners(team).order_by("-ts_sent_at")[: RUNS_LIMIT * 20]:
        meta = (b.metadata or {}).get("slackImport") or {}
        r = out.setdefault(
            meta.get("run"),
            {
                "run_id": meta.get("run"),
                "at": meta.get("at"),
                "filename": meta.get("filename", ""),
                "channels": 0,
                "messages": 0,
                "undone_at": meta.get("undone_at"),
            },
        )
        r["channels"] += 1
        r["messages"] += int(meta.get("messages") or 0)
    return sorted(out.values(), key=lambda r: r["at"] or "", reverse=True)[:RUNS_LIMIT]


def _plan(team, run_id):
    banners = list(_banners(team, run_id).select_related("channel"))
    if not banners:
        raise SlackImportError("run_not_found", 404)
    run = str(run_id)
    channel_ids = [b.channel_id for b in banners]
    mine = {
        m["id"]: m
        for m in Message.objects.filter(channel_id__in=channel_ids, metadata__slackImport__run=run)
        .exclude(correlation_id__startswith=BANNER_PREFIX)
        .values("id", "channel_id", "edited_at", "thread_root_id", "parent_id")
    }
    removable = {pk for pk, m in mine.items() if m["edited_at"] is None}
    roots = {m["thread_root_id"] for pk, m in mine.items() if m["thread_root_id"]}
    roots |= {pk for pk in mine if mine[pk]["thread_root_id"] is None}
    children = {}
    for root_id, pk in Message.objects.filter(thread_root_id__in=roots & removable).values_list(
        "thread_root_id", "id"
    ):
        children.setdefault(root_id, set()).add(pk)
    for root, child_ids in children.items():
        if child_ids - removable:
            removable.discard(root)
    kept = set(mine) - removable
    delete_channels = []
    for b in banners:
        meta = b.metadata["slackImport"]
        if not meta.get("created_channel"):
            continue
        others = (
            Message.objects.filter(channel_id=b.channel_id)
            .exclude(id__in=removable)
            .exclude(correlation_id__startswith=BANNER_PREFIX)
            .exists()
        )
        if not others:
            delete_channels.append(b.channel)
    return banners, mine, removable, kept, delete_channels


def undo_plan(team, run_id) -> dict:
    _, _, removable, kept, delete_channels = _plan(team, run_id)
    return {
        "will_remove_messages": len(removable),
        "will_delete_channels": len(delete_channels),
        "kept": len(kept),
    }


def undo(team, run_id, *, request=None) -> dict:
    with transaction.atomic():
        return _undo(team, run_id, request)


def _undo(team, run_id, request):
    from origin.services import channel_delete, thread_summary_cleanup

    banners, mine, removable, kept, delete_channels = _plan(team, run_id)
    doomed = {ch.id for ch in delete_channels}
    for ch in delete_channels:
        channel_delete.delete_group_chat(ch, mode=channel_delete.HARD, request=request)
    now = timezone.now()
    ids = [pk for pk in removable if mine[pk]["channel_id"] not in doomed]
    parents = Counter(
        mine[pk]["thread_root_id"]
        for pk in ids
        if mine[pk]["thread_root_id"] and mine[pk]["thread_root_id"] not in removable
    )
    for i in range(0, len(ids), 1000):
        Message.objects.filter(id__in=ids[i : i + 1000]).delete()
    for root, n in parents.items():
        Message.objects.filter(pk=root).update(reply_count=F("reply_count") - n, ts_updated_at=now)
        ch = mine[root]["channel_id"] if root in mine else Message.objects.get(pk=root).channel_id
        thread_summary_cleanup.forget_thread(channel_id=ch, thread_id=str(root))
    for b in banners:
        if b.channel_id in doomed:
            continue
        added = b.metadata["slackImport"].get("added_members") or []
        if added:
            # Read access the import granted goes with it, unless the person has posted since.
            posted = set(
                Message.objects.filter(channel_id=b.channel_id, sender_id__in=added)
                .exclude(metadata__has_key="slackImport")
                .values_list("sender_id", flat=True)
            )
            ChannelMember.objects.filter(
                channel_id=b.channel_id, user_id__in=[a for a in added if a not in map(str, posted)]
            ).delete()
        meta = dict(b.metadata)
        meta["slackImport"] = {**meta["slackImport"], "undone_at": now.isoformat()}
        Message.objects.filter(pk=b.pk).update(metadata=meta, ts_updated_at=now)
    return {"removed_messages": len(ids), "deleted_channels": len(doomed), "kept": len(kept)}
