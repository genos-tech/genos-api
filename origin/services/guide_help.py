"""Answer "where do I change X / how do I ... in Genos" from the vendored manual (MIP-2327, user-guide milestone MIP-2304).

The text is the same user manual the guide notes are rendered from (`guide_manual`). The lookup is plain
keyword matching, English and Japanese: NO model call, so it spends 0 AI credits, and it reads no member
data, so there is nothing to scope to a team. It never leaves `origin/search_engine/`'s billed paths:
this module and its MCP tool live outside that tree.

Japanese: a Japanese copy of the manual is used when one is vendored (`manual.ja.json`; matched on
character pairs). Until then a Japanese question is mapped to English terms through `_JA_TERMS` and
answered from the English manual (the reply says `text_language: "en"`).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from django.conf import settings

from origin.services.guide_manual import load_manual, manual_topics

QUERY_MAX_CHARS = 300
MAX_SECTIONS = 3
MAX_SETTINGS = 3
SECTION_MAX_CHARS = 1200

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by can do does for from genos how i in is it me my of on or the "
    "to turn what where which with you your change set make find want need should would".split()
)

# Japanese words a question may carry -> the English terms they stand for in the English manual.
_JA_TERMS: dict[str, str] = {
    "テーマ": "theme",
    "ダークモード": "dark theme",
    "ダーク": "dark theme",
    "ライトモード": "light theme",
    "配色": "color theme",
    "カラー": "color",
    "密度": "density",
    "言語": "language",
    "サイドバー": "sidebar",
    "通知": "notification",
    "メール": "email",
    "ミュート": "mute muted",
    "おやすみ": "pause quiet hours",
    "静音": "pause quiet hours",
    "一時停止": "pause quiet hours",
    "二段階認証": "two-step verification",
    "2段階認証": "two-step verification",
    "二要素": "two-step verification",
    "パスワード": "password",
    "ログイン": "sign in",
    "サインイン": "sign in",
    "セキュリティ": "security",
    "アカウント": "account",
    "退会": "delete account",
    "アカウント削除": "delete account",
    "エクスポート": "export",
    "インポート": "import",
    "apiキー": "api key",
    "api キー": "api key",
    "ウェブフック": "webhook",
    "webhook": "webhook",
    "連携": "integration connect",
    "接続": "connect",
    "mcp": "mcp connect ai app",
    "claude": "connect ai app",
    "chatgpt": "connect ai app",
    "プラン": "plan",
    "料金": "plan billing",
    "請求": "billing",
    "支払い": "billing",
    "クレジット": "ai credits",
    "上限": "limit",
    "ショートカット": "keyboard shortcuts",
    "キーボード": "keyboard shortcuts",
    "メンション": "mention groups",
    "グループ": "mention groups",
    "絵文字": "custom emoji",
    "リアクション": "quick reactions",
    "チャット": "chat",
    "メッセージ": "message",
    "スレッド": "thread",
    "タスク": "tasks",
    "プロジェクト": "projects",
    "マイルストーン": "milestones",
    "スプリント": "sprints",
    "カレンダー": "calendar",
    "ノート": "notes",
    "共有": "sharing share",
    "招待": "invite add people",
    "メンバー": "members add people",
    "チーム": "team",
    "権限": "roles",
    "ロール": "roles",
    "オーナー": "owner ownership",
    "スキル": "skills",
    "検索": "search",
    "プロフィール": "profile",
    "ai": "genos ai",
    "モデル": "ai model",
    "マージ": "merge pull request",
    "github": "github",
    "設定": "settings",
}


_WHO = {
    "self": "you, for your own account",
    "anyMember": "any member of the team",
    "readOnly": "nobody: it is a read-only list",
}
_PLAN = {
    "all": "every plan",
    "whenCreditsOn": "shown when AI credits are on for the team",
    "perCap": "depends on the plan's AI limits",
}


_HIRAGANA_ONLY_RE = re.compile(r"[぀-ゟ]+")


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


def _cjk_pairs(text: str) -> list[str]:
    runs = re.findall(r"[぀-ヿ㐀-鿿]+", text)
    pairs = (r[i : i + 2] for r in runs for i in range(len(r) - 1))
    # A pair of only hiragana is a particle or an ending (には, する, たい): it matches everything.
    return [p for p in pairs if not _HIRAGANA_ONLY_RE.fullmatch(p)]


def _norm_ja(text: str) -> str:
    """Spacing and 二/2 vary in the manual's Japanese (`2 段階認証`, `2段階認証`): compare without them."""
    return text.replace(" ", "").replace("二段階", "2段階")


def _is_ja(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _expand_ja(query: str) -> list[str]:
    low = query.lower()
    out: list[str] = []
    for ja, en in _JA_TERMS.items():
        if ja in low:
            out.extend(_tokens(en))
    return out


@lru_cache(maxsize=None)
def _index(lang: str) -> tuple[list[dict], list[dict]]:
    """`(sections, setting rows)` of one language's manual, tokenised once per process."""
    manual = load_manual(lang) or {}
    sections: list[dict] = []
    for topic in manual.get("topics", []):
        parts = re.split(r"(?m)^## +", topic["markdown"])
        for part in parts[1:]:
            heading, _, body = part.partition("\n")
            sections.append(
                {
                    "topic_id": topic["id"],
                    "topic": topic["title"],
                    "heading": heading.strip(),
                    "body": body.strip(),
                    "n_heading": _norm_ja(heading),
                    "n_body": _norm_ja(body),
                    "h_tokens": set(_tokens(heading)) | set(_cjk_pairs(heading)),
                    "t_tokens": set(_tokens(topic["title"])) | set(_cjk_pairs(topic["title"])),
                    "b_tokens": _tokens(body) + _cjk_pairs(body),
                }
            )
    rows = [
        {
            **s,
            "n_text": _norm_ja(s["label"] + " " + s["default"]),
            "tokens": set(_tokens(s["label"])) | set(_cjk_pairs(s["label"])),
            "d_tokens": set(_tokens(s["default"])) | set(_cjk_pairs(s["default"])),
        }
        for s in manual.get("settings", [])
    ]
    return sections, rows


def _link(tab: str) -> str:
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/?settings={tab}"


def _tab_labels(rows: list[dict]) -> dict[str, str]:
    return {r["tab"]: r["label"] for r in rows if r["kind"] == "tab"}


def _truncate(text: str) -> str:
    if len(text) <= SECTION_MAX_CHARS:
        return text
    cut = text[:SECTION_MAX_CHARS].rsplit("\n", 1)[0].rstrip()
    return cut + "\n..."


def answer(query: str, language: str = "auto") -> dict[str, Any]:
    """The best manual sections and settings for a question. Pure: no model, no database."""
    query = (query or "").strip()[:QUERY_MAX_CHARS]
    if not query:
        return {"matches": [], "settings": [], "note": "Give a topic or a question."}
    ja = _is_ja(query) if language == "auto" else language == "ja"
    lang = "ja" if ja and load_manual("ja") else "en"
    sections, rows = _index(lang)
    topic_settings = {t["id"]: t.get("settings", []) for t in manual_topics(lang)}
    # Japanese has no spaces, so pairs of characters match loosely (ダーク and ショートカット share ー).
    # A known whole term in the question (ダークモード, 通知) is a much stronger signal: it scores as a
    # phrase, and when the question has one the loose pairs are left out so they cannot outvote it.
    phrases = [_norm_ja(k) for k in _JA_TERMS if k in query.lower()] if lang == "ja" else []
    if ja and lang == "en":
        terms = set(_expand_ja(query))
    elif ja and phrases:
        terms = set(_tokens(query)) | set(phrases)
    elif ja:
        terms = set(_cjk_pairs(query)) | set(_tokens(query)) | set(_expand_ja(query))
    else:
        terms = set(_tokens(query))
    if not terms:
        return {
            "matches": [],
            "settings": [],
            "text_language": lang,
            "note": "No topic matched. Try other words, or name the feature.",
        }

    scored: list[tuple[float, dict]] = []
    for sec in sections:
        score = 0.0
        for k in phrases:
            score += (6 if k in sec["n_heading"] else 0) + (4 if k in sec["n_body"] else 0)
        for t in terms:
            if t in sec["h_tokens"]:
                score += 5
            if t in sec["t_tokens"]:
                score += 2
            score += min(sec["b_tokens"].count(t), 3)
        if score:
            scored.append((score, sec))
    # A settings page that only lists everything (A-Z, the tour) must not bury the page about the
    # feature, so a tie goes to the shorter, more specific topic.
    scored.sort(key=lambda p: (-p[0], len(p[1]["body"])))

    labels = _tab_labels(rows)
    setting_hits: list[tuple[float, dict]] = []
    for row in rows:
        hit = len(terms & row["tokens"]) + 0.5 * len(terms & row["d_tokens"])
        hit += sum(2 for k in phrases if k in row["n_text"])
        if hit:
            setting_hits.append((hit + (0.25 if row["kind"] == "row" else 0), row))
    setting_hits.sort(key=lambda p: -p[0])
    # The best section's own page knows which Settings it is about: make sure its first one is there,
    # so an answer that tells the member to open Settings always carries the link.
    if scored:
        by_id = {r["id"]: r for r in rows}
        for sid in topic_settings.get(scored[0][1]["topic_id"], [])[:1]:
            row = by_id.get(sid)
            if row and all(row is not r for _, r in setting_hits[:MAX_SETTINGS]):
                setting_hits.insert(min(MAX_SETTINGS - 1, len(setting_hits)), (0, row))

    out_settings = []
    for _, row in setting_hits[:MAX_SETTINGS]:
        tab = labels.get(row["tab"], row["tab"])
        out_settings.append(
            {
                "path": f"Settings → {tab}"
                + (f" → {row['label']}" if row["kind"] == "row" else ""),
                "who_can_change": _WHO.get(row["who"], row["who"]),
                "plan": _PLAN.get(row["plan"], row["plan"]),
                "default": row["default"],
                "link": _link(row["tab"]),
            }
        )
    matches = [
        {
            "topic_id": sec["topic_id"],
            "topic": sec["topic"],
            "section": sec["heading"],
            "text": _truncate(sec["body"]),
        }
        for _, sec in scored[:MAX_SECTIONS]
    ]
    payload: dict[str, Any] = {"matches": matches, "settings": out_settings, "text_language": lang}
    if not matches and not out_settings:
        payload["note"] = "No topic matched. Try other words, or name the feature."
    return payload
