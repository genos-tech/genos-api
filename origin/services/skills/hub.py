"""The public skill directory, "Genos Hub" (MIP-2047).

Everything a stranger's skill needs before and after it is public, in one place:

* WHO MAY PUBLISH (`publish_problems`): a Core plan or above, a verified email, an account at least
  7 days old, a public handle, at most 5 versions published a day and 50 public skills, text with no
  secrets or personal data (`content_problems`), and the author's confirmation of the content terms.
  The content rules that already lived in `service.public_problems` (title, description, tag, no
  chips / media / workspace links) and the injection scan (`guard.scan`, which HOLDS a version for an
  operator) are part of the same checklist (`checklist`).
* THE LISTING: what a directory page searches and sorts is kept on the `Skill` row and rewritten
  whenever a version is cut (`refresh_listing`), so a page is one indexed query plus one query for
  the page's versions, never a join per row. Search is Postgres only: words over the published
  version's title, name, description, tags and body (full text, `simple` config), substring over the
  title / name / description / tags (finds Japanese and Chinese). Public text is NEVER written to
  OpenSearch / `RagChunk`: that index is team-scoped and ACL-stamped, and another team's text must
  not be retrievable by the agent as workspace data.
* STARS: one per account, bookmark for everyone, counted only from an account at least 3 days old
  with a verified email that is not the author's, at most 100 a day.
* USES: the number of DISTINCT accounts that ran the skill in 30 days (`record_run`,
  `refresh_run_counts`). The author never sees who.
* THE PUBLIC HANDLE (`set_profile`): chosen once; the email and the team name are never shown.

The whole Hub is a 404 while `GENOS_SKILLS_PUBLIC` is off (`require_hub`). A skill that is not
active (held, reported-hidden, taken down) is not in any list, search or page: `directory` reads
`status = active` on every request, there is no cache to expire.
"""

from __future__ import annotations

import logging
import operator
import re
from datetime import timedelta
from functools import reduce
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, QuerySet, Value
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.skills.skill_models import (
    STATUS_ACTIVE,
    TOOL_LEVEL_ACT,
    TOOL_LEVEL_READ,
    VISIBILITY_PUBLIC,
    PublicProfile,
    Skill,
    SkillRunLog,
    SkillStar,
    SkillVersion,
)
from origin.services.skills import guard, service

logger = logging.getLogger(__name__)

# -- limits (design section 8; the numbers are Ken's, K9) ----------------------------------------

PUBLISH_TIERS = frozenset({"core", "pro", "max", "enterprise"})
PUBLISH_MIN_AGE = timedelta(days=7)
STAR_MIN_AGE = timedelta(days=3)
PUBLISHES_PER_DAY = 5
STARS_PER_DAY = 100
PUBLIC_SKILLS_MAX = 50

SORTS = ("stars", "recent", "uses")
PAGE_SIZE = 24
PAGE_SIZE_MAX = 48
QUERY_MAX = 80
RUN_WINDOW = timedelta(days=30)
RUN_LOG_KEEP = timedelta(days=90)

HANDLE_MIN = 3
HANDLE_MAX = 30
_HANDLE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Names an author cannot take: ours, and the words that read like an operator or an official skill.
RESERVED_HANDLES = frozenset(
    {
        "genos",
        "official",
        "admin",
        "administrator",
        "support",
        "staff",
        "team",
        "security",
        "moderator",
        "operator",
        "hub",
        "skills",
        "system",
        "root",
        "anthropic",
        "claude",
    }
)

TAGS_CACHE_KEY = "skills:hub:tags:v1"
TAGS_CACHE_SECONDS = 60
TAGS_SCAN_LIMIT = 5000


class HubLimitReached(service.SkillError):
    status = 429

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# -- the switch ----------------------------------------------------------------------------------


def require_hub() -> None:
    """The Hub is a 404 until `GENOS_SKILLS_PUBLIC` is on (and `GENOS_SKILLS` with it)."""
    service.require_enabled()
    if not service.public_enabled():
        raise service.SkillDisabled


# -- the public handle ---------------------------------------------------------------------------


def clean_handle(value: Any) -> str:
    handle = str(value or "").strip().lower().lstrip("@")
    if not (HANDLE_MIN <= len(handle) <= HANDLE_MAX):
        raise service.SkillError(f"The handle must be {HANDLE_MIN} to {HANDLE_MAX} characters.")
    if not _HANDLE_RE.match(handle):
        raise service.SkillError(
            "The handle may use lower-case letters, digits and single hyphens (for example "
            "`ana-lopez`)."
        )
    if handle in RESERVED_HANDLES:
        raise service.SkillError(f"`{handle}` is reserved. Choose another handle.")
    return handle


def clean_display_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if len(name) > 60:
        raise service.SkillError("The display name may be at most 60 characters.")
    return name


def get_profile(user_id: Any) -> PublicProfile | None:
    return PublicProfile.objects.filter(user_id=user_id).first()


def set_profile(user: CustomUser, handle: Any = None, display_name: Any = None) -> PublicProfile:
    """Create the public profile (the handle is chosen ONCE: copies and pages name the author by
    it) or change the display name of an existing one."""
    require_hub()
    with transaction.atomic():
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        profile = get_profile(user.pk)
        if profile is None:
            if handle in (None, ""):
                raise service.SkillError("Choose a public handle.")
            clean = clean_handle(handle)
            if PublicProfile.objects.filter(handle=clean).exists():
                raise service.SkillConflict(f"`{clean}` is taken. Choose another handle.")
            try:
                with transaction.atomic():
                    profile = PublicProfile.objects.create(
                        user=user,
                        handle=clean,
                        display_name=clean_display_name(display_name),
                    )
            except IntegrityError as exc:  # a racing request took it
                raise service.SkillConflict(f"`{clean}` is taken. Choose another handle.") from exc
            return profile
        if handle not in (None, "") and clean_handle(handle) != profile.handle:
            raise service.SkillError("The handle cannot be changed once it is chosen.")
        if display_name is not None:
            profile.display_name = clean_display_name(display_name)
            profile.save(update_fields=["display_name"])
        return profile


def author_of(skill: Skill) -> dict[str, str] | None:
    """The public byline of a skill: handle and display name, or None while the author has none
    (a skill made public before profiles, or an official one). Never an email, never the team."""
    owner = skill.owner if skill.owner_id else None
    profile = getattr(owner, "skill_profile", None) if owner is not None else None
    if profile is None:
        return None
    return {"handle": profile.handle, "displayName": profile.display_name}


# -- account and content gates -------------------------------------------------------------------


def _tier(user_id: Any) -> str:
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    try:
        return quota.get_effective_tier(str(user_id))
    except Exception:  # noqa: BLE001
        logger.exception("hub: tier lookup failed for user=%s", user_id)
        return "free"


def _age(user: CustomUser) -> timedelta:
    created = getattr(user, "ts_created_at", None)
    return timezone.now() - created if created else timedelta(0)


def account_problems(user: CustomUser) -> list[str]:
    """What stops this ACCOUNT from publishing, as sentences (design K9)."""
    out: list[str] = []
    if _tier(user.pk) not in PUBLISH_TIERS:
        out.append("Publishing needs the Core plan or above.")
    if not getattr(user, "is_email_verified", False):
        out.append("Verify your email before you publish.")
    if _age(user) < PUBLISH_MIN_AGE:
        out.append(f"Your account must be at least {PUBLISH_MIN_AGE.days} days old to publish.")
    return out


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,14}\d(?!\d)")
# Secrets that look like secrets: a provider's key prefix, a private key block, a key assigned to a
# long value. A regex scan, gitleaks-style: it catches the common leaks, not every one.
_SECRET_RES = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{16,}",
        re.IGNORECASE,
    ),
)


def _team_names(skill: Skill) -> list[str]:
    """The user names of the author's team: a skill must not name the people they work with."""
    names = TeamMembers.objects.filter(team_id=skill.team_id, is_deleted=False).values_list(
        "attendee__username", flat=True
    )[:500]
    return [n for n in names if n and len(n) >= 4 and n != getattr(skill.owner, "username", None)]


def content_problems(skill: Skill, markdown: str) -> list[str]:
    """Secrets and personal data in the text a stranger would read, as sentences. The text is
    shown to the author as the checklist's answer; the scan never stores what it found."""
    seen = "\n".join([markdown or "", skill.title, skill.description])
    out: list[str] = []
    if any(rx.search(seen) for rx in _SECRET_RES):
        out.append("The text looks like it contains a secret (a key, a token or a password).")
    if _EMAIL_RE.search(seen):
        out.append("The text contains an email address. Remove it before it is public.")
    if _PHONE_RE.search(seen):
        out.append("The text contains a phone number. Remove it before it is public.")
    lowered = seen.lower()
    for name in _team_names(skill):
        if re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", lowered):
            out.append("The text names a person from your team. Remove the name.")
            break
    return out


def _published_today(user_id: Any) -> int:
    """Versions this account cut on public skills in the last day."""
    since = timezone.now() - timedelta(days=1)
    return SkillVersion.objects.filter(
        created_by_id=user_id,
        created_at__gte=since,
        skill__visibility=VISIBILITY_PUBLIC,
    ).count()


def _public_count(user_id: Any) -> int:
    return Skill.objects.filter(owner_id=user_id, visibility=VISIBILITY_PUBLIC).count()


def require_publish_room(user: CustomUser, skill: Skill, *, new_public: bool) -> None:
    """Per-account abuse limits: 5 published versions a day, 50 public skills."""
    if _published_today(user.pk) >= PUBLISHES_PER_DAY:
        raise HubLimitReached(
            f"You can publish {PUBLISHES_PER_DAY} times a day. Try again tomorrow.", "publish_limit"
        )
    if new_public and _public_count(user.pk) >= PUBLIC_SKILLS_MAX:
        raise HubLimitReached(
            f"You can have {PUBLIC_SKILLS_MAX} public skills. Make one private first.",
            "public_skill_limit",
        )


def require_public_gate(
    user: CustomUser,
    skill: Skill,
    *,
    new_public: bool,
    accept_terms: Any = None,
    handle: Any = None,
    display_name: Any = None,
) -> None:
    """Everything a person must have before their text is read by strangers. Raises ONE error with
    every problem listed (`problems`), so the checklist can show them together. Called with the skill
    row locked, inside the transaction that makes it public or publishes a public version."""
    require_hub()
    problems = account_problems(user)
    if new_public and accept_terms is not True:
        problems.append("Confirm the content terms and the license before you publish.")
    profile = get_profile(user.pk)
    if profile is None and handle in (None, ""):
        problems.append("Choose a public handle.")
    markdown = service.live_markdown(skill)
    problems.extend(content_problems(skill, markdown))
    if problems:
        raise service.SkillError(
            "This skill cannot be public yet: " + " ".join(problems), problems=problems
        )
    if profile is None:
        set_profile(user, handle, display_name)
    require_publish_room(user, skill, new_public=new_public)


def checklist(user: CustomUser, skill: Skill) -> list[dict[str, Any]]:
    """The publish checklist as rows the page ticks off: `{"key", "ok", "text"}`. Read-only: it
    changes nothing and is the same set of rules `require_public_gate` enforces (plus the content
    rules of `service.public_problems`). The terms confirmation is the person's own click, so it is
    not a row here."""
    require_hub()
    markdown = service.live_markdown(skill)
    blocks = service._live_blocks(skill)  # noqa: SLF001 - the same snapshot `publish` uses
    rows: list[dict[str, Any]] = []

    def row(key: str, ok: bool, text: str) -> None:
        rows.append({"key": key, "ok": ok, "text": text})

    tier_ok = _tier(user.pk) in PUBLISH_TIERS
    row("plan", tier_ok, "Core plan or above" if tier_ok else "Publishing needs the Core plan")
    verified = bool(getattr(user, "is_email_verified", False))
    row("email", verified, "Email verified" if verified else "Verify your email")
    aged = _age(user) >= PUBLISH_MIN_AGE
    row(
        "age",
        aged,
        f"Account at least {PUBLISH_MIN_AGE.days} days old"
        if aged
        else f"Your account must be {PUBLISH_MIN_AGE.days} days old",
    )
    row(
        "handle",
        get_profile(user.pk) is not None,
        "Public handle chosen" if get_profile(user.pk) else "Choose a public handle",
    )
    content = service.public_problems(skill, blocks)
    row(
        "details",
        not content,
        "Title, description, a tag, and no chips, images or workspace links"
        if not content
        else " ".join(content),
    )
    leaks = content_problems(skill, markdown)
    row(
        "private-data",
        not leaks,
        "No secrets, emails, phone numbers or teammates' names" if not leaks else " ".join(leaks),
    )
    scan = guard.scan(markdown, extra=(skill.title, skill.description))
    row(
        "automatic-check",
        not scan.held,
        "The automatic safety check found nothing to look at"
        if not scan.held
        else "The automatic safety check flagged the text: a Genos operator will look at it "
        "before it is listed",
    )
    return rows


# -- the listing (what a directory page searches and sorts) ------------------------------------


def listing_text(version: SkillVersion) -> str:
    parts = [version.title, version.slug, version.description, *list(version.tags or [])]
    return " ".join(str(p) for p in parts if p).lower()


def refresh_listing(skill: Skill, version: SkillVersion) -> None:
    """Write the directory's columns from `version` (the one readers get). A queryset update, not
    `skill.save()`: nothing else on the row moves. Call after a version is cut."""
    vector = (
        SearchVector(Value(version.title), weight="A", config="simple")
        + SearchVector(Value(version.slug), weight="A", config="simple")
        + SearchVector(Value(version.description), weight="B", config="simple")
        + SearchVector(Value(" ".join(version.tags or [])), weight="B", config="simple")
        + SearchVector(Value(version.body_markdown), weight="D", config="simple")
    )
    Skill.objects.filter(pk=skill.pk).update(
        hub_text=listing_text(version),
        hub_tags=[str(t) for t in (version.tags or [])],
        search_vector=vector,
        last_published_at=version.created_at,
    )


# -- the directory -------------------------------------------------------------------------------


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"\s+", query.lower().strip()) if t][:6]


def directory(
    user: CustomUser,
    *,
    query: str = "",
    tag: str = "",
    sort: str = "stars",
    tool_level: str = "",
    official: bool = False,
    language: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[Skill], dict[Any, SkillVersion], bool]:
    """One page of the public skills: `(skills, versions by skill id, has_more)`. Three queries
    however many rows: the page (owner and profile joined), the page's published versions, and the
    caller's own stars are read by the view. Every row is `public`, `active`, with a published
    version: a takedown, a report-hide or a hold removes it from the very next request."""
    require_hub()
    if sort not in SORTS:
        raise service.SkillError(f"`sort` must be one of: {', '.join(SORTS)}.")
    query = " ".join(str(query or "").split())[:QUERY_MAX]
    tag = str(tag or "").strip().lower()[:24]
    limit = max(1, min(int(limit or PAGE_SIZE), PAGE_SIZE_MAX))
    offset = max(0, int(offset or 0))

    qs: QuerySet[Skill] = Skill.objects.filter(
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ACTIVE,
        published_version_no__gt=0,
        public_from_version_no__gt=0,
    ).select_related("owner", "owner__skill_profile")
    if tag:
        qs = qs.filter(hub_tags__contains=[tag])
    if tool_level in (TOOL_LEVEL_READ, TOOL_LEVEL_ACT):
        qs = qs.filter(tool_level=tool_level)
    if official:
        qs = qs.filter(is_official=True)
    if language:
        qs = qs.filter(language=str(language).strip().lower()[:16])

    rank = None
    terms = _terms(query)
    if terms:
        search = SearchQuery(" ".join(terms), config="simple", search_type="plain")
        # Every typed word must show up: in the ranked words (body included) or as a piece of the
        # title / name / description / tags (which is what finds Japanese and a half-typed word).
        for term in terms:
            qs = qs.filter(
                Q(search_vector=SearchQuery(term, config="simple")) | Q(hub_text__contains=term)
            )
        rank = SearchRank(F("search_vector"), search)
        qs = qs.annotate(rank=rank)

    if sort == "recent":
        order = [F("last_published_at").desc(nulls_last=True), "-star_count"]
    elif sort == "uses":
        order = ["-run_count_30d", "-star_count", F("last_published_at").desc(nulls_last=True)]
    else:
        order = ["-star_count", F("last_published_at").desc(nulls_last=True)]
    if rank is not None and sort == "stars":
        # A search with the default sort: the best match first, then the stars.
        order = ["-rank", *order]
    qs = qs.order_by(*order, "id")

    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    versions: dict[Any, SkillVersion] = {}
    if rows:
        wanted = reduce(
            operator.or_,
            (Q(skill_id=s.pk, version_no=s.published_version_no) for s in rows),
        )
        versions = {v.skill_id: v for v in SkillVersion.objects.filter(wanted)}
    return rows, versions, has_more


def popular_tags(limit: int = 24) -> list[dict[str, Any]]:
    """The tags of the public skills with their counts, most used first (the filter chips). One
    column of one slice of the table, counted in Python and cached a minute."""
    require_hub()
    cached = cache.get(TAGS_CACHE_KEY)
    if isinstance(cached, list):
        return cached[:limit]
    counts: dict[str, int] = {}
    rows = Skill.objects.filter(
        visibility=VISIBILITY_PUBLIC, status=STATUS_ACTIVE, published_version_no__gt=0
    ).values_list("hub_tags", flat=True)[:TAGS_SCAN_LIMIT]
    for tags in rows:
        for t in tags or []:
            counts[t] = counts.get(t, 0) + 1
    out = [
        {"tag": t, "count": n}
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:60]
    ]
    cache.set(TAGS_CACHE_KEY, out, TAGS_CACHE_SECONDS)
    return out[:limit]


def starred_ids(user_id: Any, skills: list[Skill]) -> set[str]:
    """Which of these skills this account starred, in one query."""
    if not skills:
        return set()
    return {
        str(i)
        for i in SkillStar.objects.filter(user_id=user_id, skill__in=skills).values_list(
            "skill_id", flat=True
        )
    }


# -- stars ---------------------------------------------------------------------------------------


def star_counts(user: CustomUser, skill: Skill) -> bool:
    """Does this account's star move the public total: an account at least 3 days old with a
    verified email, that is not the author."""
    if service.is_owner(skill, user.pk):
        return False
    return bool(getattr(user, "is_email_verified", False)) and _age(user) >= STAR_MIN_AGE


def require_star_room(user: CustomUser) -> None:
    since = timezone.now() - timedelta(days=1)
    if SkillStar.objects.filter(user=user, created_at__gte=since).count() >= STARS_PER_DAY:
        raise HubLimitReached(
            f"You can star {STARS_PER_DAY} skills a day. Try again tomorrow.", "star_limit"
        )


# -- uses ----------------------------------------------------------------------------------------


def record_run(user: Any, skill: Skill, version_no: int, surface: str = "ask") -> None:
    """Note that this account ran this skill. Counts the skill's distinct runners of the last 30
    days incrementally (the nightly command makes it exact). A failure here never stops the run."""
    try:
        if service.is_owner(skill, getattr(user, "pk", None)):
            return  # running one's own skill is not a use by somebody else
        since = timezone.now() - RUN_WINDOW
        with transaction.atomic():
            first = not SkillRunLog.objects.filter(
                skill=skill, user=user, created_at__gte=since
            ).exists()
            SkillRunLog.objects.create(
                skill=skill, user=user, version_no=version_no, surface=surface[:16]
            )
            if first:
                Skill.objects.filter(pk=skill.pk).update(run_count_30d=F("run_count_30d") + 1)
    except Exception:  # noqa: BLE001
        logger.exception("hub: record_run failed for skill=%s", getattr(skill, "pk", None))


def refresh_run_counts(now: Any = None) -> dict[str, int]:
    """Recompute `run_count_30d` (distinct runners, 30 days) for every skill that has or had runs,
    and drop log rows older than 90 days. The nightly job; safe to run twice."""
    now = now or timezone.now()
    since = now - RUN_WINDOW
    fresh = dict(
        SkillRunLog.objects.filter(created_at__gte=since)
        .values("skill_id")
        .annotate(n=Count("user_id", distinct=True))
        .values_list("skill_id", "n")
    )
    changed = 0
    for skill_id, n in fresh.items():
        changed += (
            Skill.objects.filter(pk=skill_id).exclude(run_count_30d=n).update(run_count_30d=n)
        )
    # Skills whose runs all aged out: back to zero.
    stale = (
        Skill.objects.filter(run_count_30d__gt=0)
        .exclude(pk__in=list(fresh))
        .update(run_count_30d=0)
    )
    dropped, _ = SkillRunLog.objects.filter(created_at__lt=now - RUN_LOG_KEEP).delete()
    return {"updated": changed, "zeroed": stale, "dropped": dropped}


def rebuild_all_listings() -> int:
    """Write the directory columns for every public skill from its published version (a skill
    published before the Hub existed has none). Returns how many were rewritten."""
    n = 0
    for skill in Skill.objects.filter(
        visibility=VISIBILITY_PUBLIC, published_version_no__gt=0
    ).iterator():
        version = skill.versions.filter(version_no=skill.published_version_no).first()
        if version is not None:
            refresh_listing(skill, version)
            n += 1
    return n
