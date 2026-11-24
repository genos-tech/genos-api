"""Bring existing Genos Guide folders up to date with today's guide (MIP-2325, user-guide milestone MIP-2304).

Guide notes are seeded once per member and team (`guide_notes.seed_guide_notes`), and a copy is the member's
own note, so no fix since August reached anyone who already had the folder. `refresh_guide_notes` does three
things for every existing guide folder (English and Japanese), with one rule: a member's edit is never lost.

  * REPLACE a copy whose text is still exactly a text we shipped (`guide_shipped.json`: every body the guide
    has had, taken from git history; a test pins the hashes against the bodies in
    `tests/fixtures/guide_notes_shipped.json`). The text it had is kept as a version (restorable), the new
    text is saved and versioned like an agent edit, and, after the commit, pushed into the editor's
    collaborative document. A note renamed in the manual (`RENAMED_TITLES`) is replaced and retitled.
  * ADD a note the folder was never given. `PersonalNoteFolder.guide_titles_given` records what a folder was
    given, so a note the member deleted is NOT brought back. A folder seeded before that record existed
    (NULL) is judged by its age: it was given the titles that had shipped when it was created, plus every title
    it holds now.
  * LEAVE alone and count a copy that is neither current nor a shipped text (the member edited it).

Nothing is written without `apply`. Idempotent: a second run finds everything current. Folders are read in
batches. Never touches a note outside a guide folder.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.db import transaction

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import guide_notes
from origin.services.collab_push import push_note_body_now
from origin.services.demo_seeder import _body
from origin.services.note_delete import DeleteRefused, delete_note
from origin.views.utils.note_version import keep_current_state, snapshot_note_version

log = logging.getLogger(__name__)

SHIPPED_PATH = Path(__file__).resolve().parent / "guide_shipped.json"
BATCH_FOLDERS = 200

# locale -> {title it had when shipped: the title the manual gives it now}. A copy of the old title that is
# still unedited is replaced and retitled; one the member edited is left alone, and the new title then counts
# as given (no second copy next to the member's own).
RENAMED_TITLES: dict[str, dict[str, str]] = {
    "en": {
        "Genos AI (Spotlight): ask, act, and stay ahead": "Genos AI: ask, act, and stay ahead",
    },
    # The Japanese guide is rendered from the vendored manual (MIP-2324), which words every title its own way.
    "ja": {
        "APIキーとWebhook：手順ガイド": "API キーと Webhook の手順",
        "Genos AI（Spotlight）：質問、操作、先回り": "Genos AI: 質問し、作業を任せ、先回りする",
        "Genosと他のツールを接続する：API、MCP、リアルタイム": "Genos を他のツールにつなぐ: API、MCP、リアルタイム",
        "Genosの基本：画面構成とナビゲーション": "Genos の基本: 画面構成とナビゲーション",
        "よくある問題の解決方法": "よくある困りごとの対処法",
        "チャット：メッセージ、スレッド、メンション": "チャット: メッセージ、スレッド、メンション",
        "チーム、メンバー、役割": "チーム、メンバー、ロール",
        "ノート：個人、チーム、共有": "ノート: 個人用、チーム用、共有",
        "ヒント、ショートカット、困ったときの対処": "便利な使い方、ショートカット、困ったときは",
        "プラン、利用上限、上限に達したときの動作": "プラン、上限、上限に達したときの動き",
        "プレイブック：仕事別のGenos活用方法": "プレイブック: 仕事の種類ごとの Genos の使い方",
        "他の会社との連携：チーム間共有": "他社との協業: チーム間共有",
        "会社でGenosを使い始める：オーナー向けチェックリスト": "会社で Genos を始める: オーナー向けチェックリスト",
        "大きな組織でGenosを整える：ひとつのチーム、たくさんの部署": "大きなワークスペースの整え方: 1 つのチームに多くの部署",
        "検索と質問：より良い答えを得る方法": "検索と質問: より良い答えを得るには",
        "設定：すべての項目と場所": "設定: すべてのオプションと、その場所",
        "通知：アプリ内、プッシュ、メール": "通知: アプリ内、プッシュ、メール",
    },
}


@lru_cache(maxsize=None)
def _shipped() -> dict:
    return json.loads(SHIPPED_PATH.read_text(encoding="utf-8"))


def shipped_fingerprints(locale: str, title: str) -> frozenset[str]:
    """Fingerprints of every text `title` has shipped with: from git history, plus the hand-written list as
    it stands in code with and without the OAuth section (its address is this deployment's own, so it cannot
    be in the file)."""
    found = set(_shipped().get(locale, {}).get(title, {}).get("sha256", ()))
    for note_title, body in guide_notes._EDITIONS[locale][1]:
        if note_title != title:
            continue
        found.add(guide_notes.body_fingerprint(body))
        anchor = guide_notes._CONNECT_ANCHORS[locale].get(title)
        section = guide_notes._connect_section(locale) if anchor else None
        if section:
            with_section = guide_notes._insert_before_heading(body, anchor, _body(section))
            found.add(guide_notes.body_fingerprint(with_section))
    return frozenset(found)


def first_shipped(locale: str, title: str):
    """The date (ISO string) a title first shipped, or None for a title that never did."""
    return _shipped().get(locale, {}).get(title, {}).get("first_shipped")


@dataclass
class GuideRefreshReport:
    """Counts only: no note ids, titles or owners."""

    folders: int = 0
    replaced: int = 0  # unedited copies replaced (or that would be)
    added: int = 0  # notes the folder never had (or would get)
    already_current: int = 0
    edited_left: int = 0  # the member changed the text: left alone
    deleted_left: int = 0  # given once, gone now: not brought back
    changed_underneath: int = 0  # edited between the scan and the write: left alone
    pushes: dict[str, int] = field(default_factory=dict)  # PushOutcome value -> count


def _given_titles(folder, locale: str, held: set[str]) -> set[str]:
    if folder.guide_titles_given is not None:
        given = set(folder.guide_titles_given)
    else:
        created = folder.ts_created_at.date().isoformat()
        given = set(held)
        for title in _shipped().get(locale, {}):
            shipped_on = first_shipped(locale, title)
            if shipped_on and shipped_on <= created:
                given.add(title)
    # Whoever was given the old title was given the renamed note too.
    for old, new in RENAMED_TITLES[locale].items():
        if old in given:
            given.add(new)
    return given


def _replace(note_id: int, old_title: str, title: str, previous: frozenset, body: list) -> bool:
    """Replace one note if, under the row lock, it is still a shipped text."""
    with transaction.atomic():
        note = (
            PersonalNoteMaster.objects.select_for_update()
            .filter(note_id=note_id, title=old_title)
            .first()
        )
        if note is None or guide_notes.body_fingerprint(note.body) not in previous:
            return False
        keep_current_state(
            team=note.team,
            note_type=guide_notes.NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            title=note.title,
            body=note.body,
        )
        note.body = body
        note.title = title
        # Instance save, with the timestamp: the search index and the change feed key on it.
        note.save(update_fields=["title", "body", "ts_updated_at"])
        snapshot_note_version(
            team=note.team,
            editor=None,
            note_type=guide_notes.NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            title=note.title,
            body=body,
            force_new=True,
        )
    return True


def _add(folder, title: str, body: list) -> None:
    with transaction.atomic():
        note = PersonalNoteMaster.objects.create(
            team_id=folder.team_id,
            owner_id=folder.owner_id,
            title=title,
            body=guide_notes._with_fresh_ids(copy.deepcopy(body)),
            folder_id=folder.folder_id,
        )
        # The note APIs 403 without an explicit owner-role row (see seed_guide_notes).
        NotePermissionMaster.objects.create(
            team_id=folder.team_id,
            user_id=folder.owner_id,
            note_id=note.note_id,
            note_type=guide_notes.NOTE_TYPE_PERSONAL,
            role_id=1,
        )


def _refresh_folder(folder, locale, current, current_fp, notes, apply, report, pushes):
    held = {title for _, title, _ in notes}
    given = _given_titles(folder, locale, held)
    renamed_from = {new: old for old, new in RENAMED_TITLES[locale].items()}
    for title, body in current:
        old_title = renamed_from.get(title)
        candidates = [n for n in notes if n[1] == title or (old_title and n[1] == old_title)]
        if not candidates:
            if title in given:
                report.deleted_left += 1
            else:
                report.added += 1
                if apply:
                    _add(folder, title, body)
            given.add(title)
            continue
        previous = shipped_fingerprints(locale, title)
        for note_id, note_title, note_body in candidates:
            fingerprint = guide_notes.body_fingerprint(note_body)
            if fingerprint == current_fp[title] and note_title == title:
                report.already_current += 1
            elif fingerprint == current_fp[title]:
                report.edited_left += 1  # current text under the old title: the member's own
            else:
                allowed = previous
                if note_title != title:
                    allowed = shipped_fingerprints(locale, note_title)
                if fingerprint not in allowed:
                    report.edited_left += 1
                elif not apply:
                    report.replaced += 1
                else:
                    fresh = guide_notes._with_fresh_ids(copy.deepcopy(body))
                    if _replace(note_id, note_title, title, allowed, fresh):
                        report.replaced += 1
                        pushes.append((note_id, fresh))
                    else:
                        report.changed_underneath += 1
        given.add(title)
    if apply and sorted(given) != sorted(folder.guide_titles_given or []):
        PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).update(
            guide_titles_given=sorted(given)
        )


def refresh_guide_notes(*, apply: bool = False, team_id=None) -> GuideRefreshReport:
    """See the module docstring. Without `apply` nothing is written."""
    report = GuideRefreshReport()
    for locale, (folder_name, _) in guide_notes._EDITIONS.items():
        current = guide_notes.current_guide_notes(locale)
        current_fp = {title: guide_notes.body_fingerprint(body) for title, body in current}
        titles = {title for title, _ in current} | set(RENAMED_TITLES[locale])
        folders = PersonalNoteFolder.objects.filter(
            name=folder_name, scope=PersonalNoteFolder.SCOPE_PERSONAL, team__isnull=False
        ).order_by("folder_id")
        if team_id:
            folders = folders.filter(team_id=team_id)
        folder_ids = list(folders.values_list("folder_id", flat=True))
        for start in range(0, len(folder_ids), BATCH_FOLDERS):
            batch = list(
                PersonalNoteFolder.objects.filter(
                    folder_id__in=folder_ids[start : start + BATCH_FOLDERS]
                )
            )
            by_folder: dict[int, list] = {}
            for folder_id, note_id, title, body in PersonalNoteMaster.objects.filter(
                folder_id__in=[f.folder_id for f in batch],
                title__in=titles,
            ).values_list("folder_id", "note_id", "title", "body"):
                by_folder.setdefault(folder_id, []).append((note_id, title, body))
            pushes: list[tuple[int, list]] = []
            for folder in batch:
                report.folders += 1
                notes = by_folder.get(folder.folder_id, [])
                _refresh_folder(folder, locale, current, current_fp, notes, apply, report, pushes)
            # After the writes committed, never inside a transaction.
            for note_id, fresh in pushes:
                outcome = push_note_body_now("personal", note_id, fresh)
                report.pushes[outcome.value] = report.pushes.get(outcome.value, 0) + 1
    log.info(
        "[guide-refresh] %s team=%s folders=%d replaced=%d added=%d already_current=%d "
        "edited_left=%d deleted_left=%d changed_underneath=%d pushes=%s",
        "APPLY" if apply else "DRY-RUN",
        team_id or "all",
        report.folders,
        report.replaced,
        report.added,
        report.already_current,
        report.edited_left,
        report.deleted_left,
        report.changed_underneath,
        report.pushes or "none",
    )
    return report


@dataclass
class DedupeReport:
    """Counts only: no note ids, titles or owners."""

    folders: int = 0
    removed: int = 0  # unedited old-title copies deleted (or that would be)
    awaiting_rename: int = (
        0  # old title, no current-title note beside it: the normal refresh retitles it
    )
    edited_left: int = 0  # old title, text is not a shipped text: the member's own
    history_left: int = (
        0  # unedited text, but a person saved a version of it, or it is shared or has children
    )
    refused: int = 0  # the delete rules refused (e.g. a skill's text)
    changed_underneath: int = 0  # edited between the scan and the delete: left alone


def _only_ours(note_id: int, owner_id) -> bool:
    """True when nobody but the owner ever touched the note: no person-saved version, no other
    permission holder, no child note. Anything else is not a pure seeded copy: leave it."""
    if PersonalNoteMaster.objects.filter(parent_note_id=note_id).exists():
        return False
    if (
        NotePermissionMaster.objects.filter(
            note_type=guide_notes.NOTE_TYPE_PERSONAL, note_id=note_id
        )
        .exclude(user_id=owner_id)
        .exists()
    ):
        return False
    return not NoteVersionMaster.objects.filter(
        note_type=guide_notes.NOTE_TYPE_PERSONAL, note_id=note_id, editor__isnull=False
    ).exists()


def _dedupe_one(folder, locale, notes, apply, report) -> None:
    by_title: dict[str, list] = {}
    for note_id, title, body in notes:
        by_title.setdefault(title, []).append((note_id, body))
    for old, new in RENAMED_TITLES[locale].items():
        for note_id, body in by_title.get(old, []):
            if new not in by_title:
                report.awaiting_rename += 1
            elif guide_notes.body_fingerprint(body) not in shipped_fingerprints(locale, old):
                report.edited_left += 1
            elif not _only_ours(note_id, folder.owner_id):
                report.history_left += 1
            elif not apply:
                report.removed += 1
            else:
                # Recheck right before the delete: the member may have typed since the scan.
                fresh = (
                    PersonalNoteMaster.objects.filter(note_id=note_id, title=old)
                    .values_list("body", flat=True)
                    .first()
                )
                if fresh is None or guide_notes.body_fingerprint(fresh) not in shipped_fingerprints(
                    locale, old
                ):
                    report.changed_underneath += 1
                    continue
                try:
                    delete_note(team_id=folder.team_id, family="personal", note_id=note_id)
                    report.removed += 1
                except DeleteRefused:
                    report.refused += 1


def dedupe_renamed_guide_notes(*, apply: bool = False, team_id=None) -> DedupeReport:
    """Remove an unedited old-title copy when its folder already holds the current-title note.

    Cleans up the 2026-11-24 run that added the manual's new-title notes next to the August copies
    while `RENAMED_TITLES['ja']` was empty. A folder without the new note is left for `refresh_guide_notes`
    (replace + retitle). The app has no trash: `delete_note` is a hard delete (its versions go too), which
    loses nothing here because the text is a shipped one, a person never saved a version of it, and
    the current note sits beside it. Idempotent, folders read in batches, never outside a guide folder."""
    report = DedupeReport()
    for locale, (folder_name, _) in guide_notes._EDITIONS.items():
        renames = RENAMED_TITLES[locale]
        if not renames:
            continue
        titles = set(renames) | set(renames.values())
        folders = PersonalNoteFolder.objects.filter(
            name=folder_name, scope=PersonalNoteFolder.SCOPE_PERSONAL, team__isnull=False
        ).order_by("folder_id")
        if team_id:
            folders = folders.filter(team_id=team_id)
        folder_ids = list(folders.values_list("folder_id", flat=True))
        for start in range(0, len(folder_ids), BATCH_FOLDERS):
            batch = list(
                PersonalNoteFolder.objects.filter(
                    folder_id__in=folder_ids[start : start + BATCH_FOLDERS]
                )
            )
            by_folder: dict[int, list] = {}
            for folder_id, note_id, title, body in PersonalNoteMaster.objects.filter(
                folder_id__in=[f.folder_id for f in batch], title__in=titles
            ).values_list("folder_id", "note_id", "title", "body"):
                by_folder.setdefault(folder_id, []).append((note_id, title, body))
            for folder in batch:
                report.folders += 1
                _dedupe_one(folder, locale, by_folder.get(folder.folder_id, []), apply, report)
    log.info(
        "[guide-dedupe] %s team=%s folders=%d removed=%d awaiting_rename=%d edited_left=%d "
        "history_left=%d refused=%d changed_underneath=%d",
        "APPLY" if apply else "DRY-RUN",
        team_id or "all",
        report.folders,
        report.removed,
        report.awaiting_rename,
        report.edited_left,
        report.history_left,
        report.refused,
        report.changed_underneath,
    )
    return report
