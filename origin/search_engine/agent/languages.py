"""Locale → language handling for the agent's "answer in the user's
language" directive (genos-docs `spotlight/GENOS_CAPABILITY_ROADMAP.md` §3.4).

The UI ships in seven locales, RTL included, but the `/agent/ask/`
request never carried a language signal — the locale lived only in the
browser's localStorage. So answer language was whatever the model
inferred from the query text, and a Japanese user asking a short
question full of English task titles could get an English answer inside
a fully Japanese interface. The view now forwards the caller's locale
and `_drive_loop` appends the directive built here.

This is the single source of truth for the code → English-name mapping,
shared by the agent prompt path and the daily-digest command (which
previously kept its own private copy).
"""

from __future__ import annotations

# Frontend UI locales (genos-frontend `src/i18n/types.ts`) → English
# name. Same vocabulary as the `User.language` field. Anything outside
# this set yields no directive (see `language_directive`), i.e. today's
# infer-from-the-query behavior — an unknown code never breaks a run.
LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "es": "Spanish",
    "fr": "French",
    "zh": "Chinese",
    "ar": "Arabic",
    "hi": "Hindi",
}


def normalize_locale(value: object) -> str:
    """Lower-case/trim a client- or DB-supplied locale code, returning it
    only when it names a known UI locale; ``""`` otherwise so the caller
    can fall back to another source (or to no directive at all).

    Takes ``object`` because the value comes straight off the request
    JSON: a client can send ``"locale": 7`` or ``"locale": ["ja"]``. A
    non-string collapses to ``""`` (today's infer-from-the-query
    behavior) rather than raising and 500ing the ask."""
    code = value.strip().lower() if isinstance(value, str) else ""
    return code if code in LANGUAGE_NAMES else ""


def language_directive(locale: str | None) -> str:
    """The "answer in language X" line appended to the system instruction.

    Empty for English and for unknown/empty codes: English is the model's
    natural default, so steering it there would add a per-request prompt
    line to the majority path for no behavior change (and needlessly
    perturb the cacheable prompt prefix). Non-English locales get a firm
    directive worded to survive mixed-language retrieved content — a
    Japanese user's tasks and chats are routinely full of English titles,
    and the answer prose must stay Japanese anyway.
    """
    code = normalize_locale(locale)
    if not code or code == "en":
        return ""
    name = LANGUAGE_NAMES[code]
    return (
        f"Write your reply to the user in {name}. Retrieved content, task "
        f"titles, and tool results may be in other languages — keep proper "
        f"nouns and identifiers verbatim, but write your own prose in {name}. "
        f"If the user explicitly asks you to reply in a different language, "
        f"follow that request instead."
    )
