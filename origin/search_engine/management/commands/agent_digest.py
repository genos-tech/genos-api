"""Proactive Genos digest — the hourly local-time tick (UX tier model §8).

Today Genos only speaks when spoken to. This command is the one place
it comes to you unprompted: a scheduled agent run — "3 tasks slipped,
the API milestone is blocked, this note is stale" — delivered as an
Inbox item + web push at 8am in the USER'S timezone.

Design decisions (plan §8.2 + the as-built deltas):

  * HOURLY TICK, local-hour targeting. The cron fires every hour
    (Railway `railway-agent-digest.toml`); each pass selects users
    whose LOCAL clock (CustomUser.timezone via services/user_time,
    fallback settings.TIME_ZONE) is at --at-hour. A single fixed UTC
    slot would land at random local hours across a global user base.
  * Cadence comes from the TIER (`digest_cadence`: pro=weekly on the
    user's local Monday, max/enterprise=daily), never from user
    config — fewer knobs, and the tier is the felt difference.
    `digest_enabled` is the per-user opt-out.
  * BATCHED tier resolution: personal tiers arrive with the user rows
    and team plans in ONE join — `get_effective_tier` is a Redis read
    plus a possible team query per user, fine at a dozen users and not
    fine later.
  * IDEMPOTENT via `digest_last_sent_at`: a re-run inside the window
    (or a DST hiccup) must not double-send. The stamp is written ONLY
    after a successful send, so a failed generation retries on the
    next tick that still matches the local hour.
  * NON-BILLABLE by construction: the run binds surface="digest",
    which is not in credit_policy.yaml `billable_surfaces` — a plan
    feature the user didn't ask for at that moment must not consume
    their credits. The cost is still real to US: it is metered, and
    the rollup rows are how you size it.
  * The agent run is READ-ONLY: every requires_approval tool is
    undeclared (a write would PAUSE the run with nobody there to
    approve it), and web search is out of scope for a workspace
    digest.
  * QUALITY GUARD: an empty or error-terminated run sends nothing and
    stamps nothing — a digest that says nothing useful is worse than
    no digest.
  * EDITORIAL, not a status dump. The first version asked for "3-6
    short bullets, no greetings, no preamble" and got exactly that:
    consecutive days whose text differed only in the overdue
    day-counts, because the underlying state barely moves and the
    model had no idea what it had already said. The prompt is now
    built per-run (`_build_prompt`) and carries the LAST TWO EDITIONS,
    so today's job is to report the DELTA in Genos's own voice and
    pick the 2-4 areas that actually have signal, rather than fill a
    fixed skeleton. See `_build_prompt` for the content contract.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.controller import run_agent
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.entity_links import rewrite_citation_md
from origin.search_engine.metered import metered_request
from origin.services.user_time import resolve_zone
from origin.services.webpush_dispatch import dispatch_push_for_inbox_item

log = logging.getLogger(__name__)

# InboxItems.item_type for a digest (see the map in inbox_models.py).
ITEM_TYPE_DIGEST = 6

# Re-send guards, deliberately SHORTER than the nominal period: the
# tick is hourly and clock math crosses DST/timezone edits, so "at
# least 20h since the last daily / 6d since the last weekly" is the
# robust reading of "once per period" (a strict 24h/7d would skip a
# whole period whenever a send drifted late).
_MIN_GAP = {"daily": timedelta(hours=20), "weekly": timedelta(days=6)}

_TIER_RANK = {"free": 0, "core": 1, "pro": 2, "max": 3, "enterprise": 4}

# The OTHER half of why every edition looked the same. The house system
# prompt (agent/prompts.py) is tuned for Spotlight Q&A and says, in so
# many words: "Tone: concise, factual", "prefer 3-5 bullets over a
# paragraph", "**bold** the load-bearing word(s) of each bullet", "no
# throat-clearing intros ... and no closing summaries". That is a good
# answer format and a terrible briefing — it mandates precisely the
# bolded-label bullet soup the screenshots showed, and no amount of
# per-run prompting reliably beats a system rule. `system_extra` is
# appended AFTER the base prompt, so the override lands where it wins.
DIGEST_SYSTEM_EXTRA = """\
SURFACE OVERRIDE — this run writes a personal briefing, not an answer to \
a question. For this run only, the following supersede the Tone and \
Formatting rules above:

  * An opening line in your own voice is REQUIRED, not throat-clearing.
    The ban on "Sure!" / "Here's what I found:" still stands — what is
    wanted is an observation, not a preamble.
  * Prose is allowed and often better. "Prefer 3-5 bullets over a
    paragraph" does NOT apply here; pick whichever fits what you have
    to say, and deliberately vary it between editions.
  * No headings, no tables, no inline code. Bold sparingly — a bolded
    label on every single line is what made past editions unreadable.
  * The closing suggested action is REQUIRED, not a banned closing
    summary.
  * Warm and direct rather than clipped. Still factual: never invent,
    never pad.

Everything else — the citation token grammar above all — is unchanged; \
cite entities normally so they resolve to real links."""

# How many past editions the writer gets to read. Two is enough to see a
# trend ("still not moved") without turning the prompt into an archive —
# and each one is truncated, because a digest that quotes a digest that
# quotes a digest burns context on its own reflection.
_RECALL_EDITIONS = 2
_RECALL_CHARS = 900

# The digest is LLM-written, so it is not limited to the locales that
# have email templates (`resolve_email_locale`'s set is {"ja"} — that's
# about template directories on disk). Any UI locale the app ships can
# be asked for directly; anything else falls back to English.
_LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "fr": "French",
    "es": "Spanish",
    "hi": "Hindi",
    "ar": "Arabic",
}

DEFAULT_TITLE = "Your Genos digest"

# The writer puts its headline on line 1 as `TITLE: ...`. A markdown
# heading would have been the obvious carrier, but the Inbox bubble
# (genos-frontend DigestBody) styles only p/ul/ol/strong/code/a — an
# `##` renders at raw browser-default size. Lifting it into
# `item_body.title` also gives the web push a real headline instead of
# the same four words every morning.
_TITLE_RE = re.compile(r"^\s*(?:#{1,6}\s*)?\**\s*TITLE\s*[:：]\s*(?P<title>.+?)\s*\**\s*$", re.I)
_TITLE_MAX = 90

# The headline is PLAIN TEXT, and it has to be made so rather than asked
# for. Both consumers render it literally — the Inbox bubble prints it
# in a bare <Typography>, and it is the web-push title — and unlike the
# body it never passes through `rewrite_citation_md`, so there is no
# downstream net. "no markdown, no links" in the brief is an
# instruction, and the entire point of this change is to let the writer
# take more liberties with how it writes; a headline reading
# `[KDS-439](task:15) is stuck` is exactly the raw-token bug the digest
# body already had reported against it.
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")  # [label](target) -> label
_BARE_TOKEN_RE = re.compile(r"\[(?:chat|task|note|project|todo|milestone):[^\]\n]*\]", re.I)


def _digest_disabled_tools() -> set[str]:
    writes = {t.name for t in REGISTRY.values() if t.requires_approval}
    return writes | {"search_web"}


def _is_nothing_to_report(text: str) -> bool:
    """True only when the sentinel IS the answer.

    Deliberately not a substring test. The old prompt produced clipped
    bullets that could never mention the sentinel by accident; a
    conversational digest absolutely can ("...nothing to report on the
    API side"), and a substring check would silently delete a perfectly
    good edition.
    """
    return text.strip().strip("*_`#. ").upper() == "NOTHING_TO_REPORT"


def _split_title(text: str) -> tuple[str, str]:
    """(headline, body). Missing/malformed TITLE line -> ("", text).

    The body is returned untouched when there is no headline, so a run
    that ignores the convention loses its headline, never its first
    sentence.
    """
    lines = text.lstrip().split("\n")
    if not lines:
        return "", text
    match = _TITLE_RE.match(lines[0])
    if match is None:
        return "", text
    return _plain_headline(match.group("title")), "\n".join(lines[1:]).strip()


def _plain_headline(raw: str) -> str:
    """Force a headline down to plain text, then cap its length."""
    title = _MD_LINK_RE.sub(r"\1", raw)  # keep the label, drop the target
    title = _BARE_TOKEN_RE.sub("", title)  # bare citation tokens have no label
    title = re.sub(r"[*_`#]+", "", title)
    title = re.sub(r"\s+", " ", title).strip().strip("\"'").strip()
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return title


def _recent_editions(user_id: str, now) -> list[str]:
    """The last few digests as `"3 days ago: <text>"` lines."""
    rows = (
        InboxItems.objects.filter(
            receiver_id=user_id, item_type=ITEM_TYPE_DIGEST, is_deleted=False
        )
        .order_by("-ts_created_at")
        .values_list("ts_created_at", "item_body")[:_RECALL_EDITIONS]
    )
    editions = []
    for created, body in rows:
        text = (body or {}).get("text") if isinstance(body, dict) else None
        if not text:
            continue
        days = max(0, (now - created).days)
        when = "yesterday" if days == 1 else ("earlier today" if days == 0 else f"{days} days ago")
        editions.append(f"--- {when} ---\n{text[:_RECALL_CHARS].strip()}")
    return editions


def _build_prompt(*, name: str, cadence: str, local, language: str, editions: list[str]) -> str:
    """The per-run brief.

    Three things separate this from the "3-6 bullets" original, and all
    three exist to kill the near-identical-every-day failure:

      * it hands over the LAST EDITIONS and makes the delta the story,
        so an item that hasn't moved gets re-framed or dropped rather
        than restated verbatim;
      * it offers a MENU and asks for 2-4 areas with real signal
        instead of naming the same five sources every time — which
        also bounds tool calls (this run is metered, and the step cap
        degrades a long run into tool-less synthesis);
      * it asks for a voice. "No greetings, no preamble, no sign-off"
        is what made the output read like a cron job talking.
    """
    period = "since yesterday" if cadence == "daily" else "over the past week"
    when = f"{local:%A}, {local:%B} {local.day}"
    lang = _LANGUAGE_NAMES.get(language, "English")

    if editions:
        memory = (
            "## What you already told them\n"
            "These are your most recent editions. Do NOT restate them. Lead with what "
            f"CHANGED {period}. An item that has not moved is only worth raising again if "
            "the not-moving is itself the story — and then say it that way, in different "
            "words, not by repeating the line. Cite entities from your own tool results "
            "this run; never copy a link out of an old edition, they go stale.\n\n"
            + "\n\n".join(editions)
        )
    else:
        memory = (
            "## This is their first edition\n"
            "Introduce yourself in one short line — who you are and what this is — then "
            "get on with it. Don't explain the product."
        )

    return f"""\
You are Genos. Write today's edition of {name}'s {cadence} briefing for {when}.

This is not a status report. It reads like a short note from a colleague who \
works alongside them and has been paying attention: it has a voice, a point of \
view, and only the things that genuinely matter right now.

{memory}

## Where to look
Check a FEW areas that plausibly have something new — pick 2-4, not all of \
them. A briefing that surveys everything says nothing, and every extra lookup \
costs time you don't have. Choose based on what you said last time and what \
today probably holds:

- Aimed at them: focus tasks, what's blocking them, what they're blocking for others.
- People: mentions, messages waiting on a reply, inbox requests and approvals.
- Momentum: what they closed {period}, throughput, sprint and milestone movement.
- The day itself: what their calendar and today's todos actually look like.
- Drift: tasks going stale, backlog aging quietly, notes nobody has touched.

If an area is quiet, say nothing about it. Silence is information — a section \
that exists only to be filled is why briefings go unread.

## How to write it
1. FIRST LINE, exactly `TITLE: <headline>` — plain text, under 60 characters, \
no markdown, no links. It should be about TODAY specifically, the way a \
headline is: name the one thing that matters, or the shape of the day. Not \
"Your daily digest".
2. Then 1-2 sentences in your own voice. Ground them in something real you \
found — the state of the week, a streak, a thing that's been sitting too long, \
what today looks like. Not a greeting template.
3. Then 2-4 short pieces, most consequential first. Vary the shape edition to \
edition: some days that's one paragraph about the one thing that matters, some \
days a bolded label and a sentence, some days a tight list. Name concrete \
items — task titles, people, notes, projects — and link them.
4. End with ONE concrete next action, phrased as a suggestion, not an order.

## Voice
Write in {lang}. Be direct, warm, and brief — 120-200 words for the whole \
thing. You may have an opinion ("this has been open three weeks; either it \
matters or it doesn't") and you may say when things look good or quiet. Never \
manufacture urgency, never congratulate them on nothing, never pad, never \
invent a fact you didn't read from a tool. Markdown: paragraphs, **bold**, \
short lists and links only — no headings, no tables, no code blocks.

If — and only if — there is genuinely nothing worth their attention, reply \
with exactly NOTHING_TO_REPORT and nothing else."""


class Command(BaseCommand):
    help = (
        "Hourly digest tick: generate + deliver the proactive Genos digest "
        "to users whose tier grants one and whose local time matches."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--at-hour",
            type=int,
            default=8,
            help="Local hour (0-23) the digest targets. Default 8.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Max digests per pass — a cost brake, not a scheduler.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Select and report, but generate/send/stamp nothing.",
        )
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Generate for real and PRINT it — no inbox item, no push, "
            "no stamp, and the once-per-period guard is ignored so you can "
            "iterate. Pair with --user-id. This is how you read the copy "
            "before shipping a prompt change.",
        )
        parser.add_argument(
            "--user-id",
            default="",
            help="Restrict to one user id and SKIP the local-hour check "
            "(manual testing / support resend).",
        )

    def handle(self, *args, **options):
        at_hour = options["at_hour"] % 24
        limit = max(1, int(options["limit"]))
        dry_run = bool(options["dry_run"])
        preview = bool(options["preview"])
        only_user = (options["user_id"] or "").strip()
        now = timezone.now()

        users = CustomUser.objects.filter(digest_enabled=True, is_deleted=False)
        if only_user:
            users = users.filter(id=only_user)
        users = list(
            users.only("id", "tier", "timezone", "digest_last_sent_at", "username", "language")
        )
        if not users:
            self.stdout.write("No digest-enabled users.")
            return

        # ONE join for every membership: effective tier = best of the
        # personal tier and every active team's plan; the granting (or
        # first) team also provides the agent run's team context.
        memberships: dict[str, list[tuple[str, str]]] = {}
        rows = TeamMembers.objects.filter(
            attendee_id__in=[u.id for u in users],
            is_deleted=False,
            team__is_deleted=False,
        ).values_list("attendee_id", "team__team_id", "team__plan")
        for attendee_id, team_id, plan in rows:
            memberships.setdefault(str(attendee_id), []).append(
                (str(team_id), plan or "free")
            )

        quotas = settings.SEARCH_ENGINE.get("TIER_QUOTAS") or {}
        sent = skipped = failed = 0

        for user in users:
            if sent >= limit:
                break
            uid = str(user.id)
            teams = memberships.get(uid, [])
            tier, team_id = user.tier or "free", teams[0][0] if teams else None
            for t_id, plan in teams:
                if _TIER_RANK.get(plan, 0) > _TIER_RANK.get(tier, 0):
                    tier, team_id = plan, t_id

            cadence = (quotas.get(tier) or {}).get("digest_cadence")
            if cadence not in _MIN_GAP:
                continue
            if team_id is None:
                continue  # a digest is grounded in a workspace

            local = now.astimezone(resolve_zone(user.timezone))
            if not only_user:
                if local.hour != at_hour:
                    continue
                if cadence == "weekly" and local.weekday() != 0:  # Monday
                    continue
            last = user.digest_last_sent_at
            # --preview ignores the guard on purpose: reading the copy
            # twice in a row is the entire point of the flag.
            if not preview and last is not None and now - last < _MIN_GAP[cadence]:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"[dry-run] would send {cadence} digest to {uid}")
                sent += 1
                continue

            try:
                text = self._generate(user, team_id, cadence=cadence, local=local, now=now)
            except Exception:  # noqa: BLE001 — one user's failure never kills the pass
                log.exception("digest generation failed for user=%s", uid)
                failed += 1
                continue

            title, text = _split_title(text)
            if preview:
                self.stdout.write(f"\n=== {uid} — {cadence} ===")
                self.stdout.write(f"TITLE: {title or DEFAULT_TITLE}")
                self.stdout.write(text or "(empty — nothing would be sent)")
                sent += 1
                continue

            if not text:
                # Empty answer, error-terminated run, or an explicit
                # NOTHING_TO_REPORT. No item, no stamp for failures —
                # but a truthful "nothing needs you" DOES stamp, or
                # every later tick today re-asks the same question.
                user.digest_last_sent_at = now
                user.save(update_fields=["digest_last_sent_at"])
                skipped += 1
                continue

            # The agent cites entities with bare tokens ([KDS-7](task:15)).
            # The chat surface resolves those against the run's sources;
            # an Inbox item has no sources map, so resolve to real
            # /workspace hrefs at save time (unresolvable → plain prose,
            # never a dead link).
            text = rewrite_citation_md(text, team_id=team_id)

            item = InboxItems.objects.create(
                team_id=team_id,
                sender=None,  # system-authored
                receiver=user,
                item_type=ITEM_TYPE_DIGEST,
                item_body={"title": title or DEFAULT_TITLE, "text": text},
                # ⚠️ item_optionals must survive every write path — the
                # historical POST /inbox/ bug dropped it (plan §8.2).
                item_optionals={
                    "cadence": cadence,
                    "generated_at": now.isoformat(),
                },
                request_status="",  # not a request item
            )
            # The headline IS the push — "Your Genos digest" every
            # morning is a notification you learn to swipe away.
            dispatch_push_for_inbox_item(item, title=title or DEFAULT_TITLE)
            user.digest_last_sent_at = now
            user.save(update_fields=["digest_last_sent_at"])
            sent += 1

        self.stdout.write(
            f"digest tick @{at_hour}:00 local — sent={sent} skipped={skipped} "
            f"failed={failed} (of {len(users)} enabled users)"
        )

    def _generate(self, user, team_id: str, *, cadence: str, local, now) -> str:
        """One read-only agent run; returns the raw digest text or ''.

        The text still carries its `TITLE:` line — splitting is the
        caller's job, because the preview path wants the headline too.
        """
        user_id = str(user.id)
        prompt = _build_prompt(
            name=user.username or "there",
            cadence=cadence,
            local=local,
            language=(user.language or "").strip().lower(),
            editions=_recent_editions(user_id, now),
        )
        events: list[dict] = []
        ctx = ToolContext(team_id=team_id, user_id=user_id)
        with metered_request(surface="digest", user_id=user_id, team_id=team_id):
            run_agent(
                prompt,
                ctx,
                events.append,
                disabled_tools=_digest_disabled_tools(),
                system_extra=DIGEST_SYSTEM_EXTRA,
            )
        if any(e.get("type") == "error" for e in events):
            raise RuntimeError("agent run emitted an error event")
        text = "".join(
            e.get("text") or "" for e in events if e.get("type") == "answer_delta"
        ).strip()
        if not text or _is_nothing_to_report(text):
            return ""
        return text
