"""The two MCP guide notes lead with connecting an AI app, and existing copies follow (MIP-1558).

Pinned:
* in BOTH guide editions (en, ja: the only two that exist) the two notes that teach MCP
  carry the "Connect an AI app (no API key)" section IN FRONT of the API-key way, with the
  deployment's own address (no query string, no trailing slash, never hard-coded), the
  Claude steps, the plan line and where to revoke;
* the API-key way is still there and still correct (`ApiKey` scheme, `?team_id=` form,
  Core rather than Pro);
* the section is left out when the OAuth way is off here (its steps would be false);
* seeding writes the section; nothing else about seeding changed (`test_guide_notes.py`);
* the refresh replaces a copy that is still byte for byte a shipped text, in either of
  the shipped versions, and never one that was edited, never on a dry run.
"""

import copy
import json
import uuid
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import guide_notes, start_here_note
from origin.services.collab_push import PushOutcome
from origin.tests.test_base import BaseAPITestCase

BASE = "https://api.example.com"
MCP = f"{BASE}/api/public/v1/mcp"
ON = {"MCP_OAUTH_ENABLED": True, "API_PUBLIC_BASE_URL": BASE}
OFF = {"MCP_OAUTH_ENABLED": False, "API_PUBLIC_BASE_URL": ""}

EN_CONNECTING = "Connecting Genos to other tools: API, MCP, and realtime"
EN_KEYS = "API keys and webhooks, step by step"
JA_CONNECTING = "Genosと他のツールを接続する：API、MCP、リアルタイム"
JA_KEYS = "APIキーとWebhook：手順ガイド"
MCP_NOTES = {"en": (EN_CONNECTING, EN_KEYS), "ja": (JA_CONNECTING, JA_KEYS)}

HEADING = {"en": "Connect an AI app (no API key)", "ja": "AIアプリを接続する（APIキー不要）"}
SCRIPTS = {"en": "Scripts and automation (API key)", "ja": "スクリプトと自動化（APIキー）"}
MENU = {
    "en": "Settings → Developer → Connected apps",
    "ja": "設定 → Developer → 接続済みアプリ",
}

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "guide_notes_previous.json").read_text(encoding="utf-8")
)


def _text(node) -> str:
    if isinstance(node, list):
        return "\n".join(_text(n) for n in node)
    if isinstance(node, dict):
        content = node.get("content")
        own = "".join(c.get("text", "") for c in content) if isinstance(content, list) else ""
        return "\n".join(p for p in (own, _text(node.get("children") or [])) if p)
    return ""


def _headings(body) -> list[str]:
    return [_text(b) for b in body if b.get("type") == "heading"]


def _notes(locale) -> dict:
    return dict(guide_notes.current_guide_notes(locale))


def _with_ids(node):
    """What the DB holds: every block carries a uuid, children included."""
    if isinstance(node, dict):
        out = {k: _with_ids(v) for k, v in node.items()}
        if "props" in out:
            out["id"] = str(uuid.uuid4())
        return out
    if isinstance(node, list):
        return [_with_ids(v) for v in node]
    return node


def _previous(locale, title, version=0) -> list:
    return _with_ids(FIXTURE[locale][title][version]["body"])


@override_settings(**ON)
class ConnectSectionContentTests(SimpleTestCase):
    def test_both_mcp_notes_of_both_editions_have_the_section_and_it_leads(self):
        for locale, titles in MCP_NOTES.items():
            notes = _notes(locale)
            for title in titles:
                headings = _headings(notes[title])
                self.assertIn(HEADING[locale], headings, (locale, title))
                self.assertIn(SCRIPTS[locale], headings, (locale, title))
                self.assertLess(
                    headings.index(HEADING[locale]), headings.index(SCRIPTS[locale]), title
                )
            # The first note has it as its very first section.
            self.assertEqual(_headings(notes[titles[0]])[0], HEADING[locale])

    def test_the_address_is_the_oauth_one_without_query_or_trailing_slash(self):
        for locale, titles in MCP_NOTES.items():
            for title in titles:
                body = _notes(locale)[title]
                addresses = [_text(b) for b in body if _text(b).startswith(BASE)]
                self.assertEqual(addresses, [MCP], (locale, title))
                for address in addresses:
                    self.assertNotIn("?", address)
                    self.assertFalse(address.endswith("/"))
                self.assertIn(f"claude mcp add --transport http genos {MCP}", _text(body))
                # The one function the OAuth metadata and the "Start here" note use.
                self.assertEqual(addresses[0], start_here_note.mcp_url())

    def test_the_address_follows_the_deployment_origin_not_a_hard_coded_host(self):
        with override_settings(API_PUBLIC_BASE_URL="https://api.other.test/"):
            for locale, titles in MCP_NOTES.items():
                for title in titles:
                    text = _text(_notes(locale)[title])
                    self.assertIn("https://api.other.test/api/public/v1/mcp\n", text + "\n")
                    self.assertNotIn("api.other.test/api/public/v1/mcp/", text)
                    self.assertNotIn("api.other.test/api/public/v1/mcp?", text)

    def test_steps_plan_and_revoke_use_the_start_here_words_and_the_frontend_menu(self):
        for locale, titles in MCP_NOTES.items():
            copy_ = start_here_note._COPY[locale]
            for title in titles:
                text = _text(_notes(locale)[title])
                for step in copy_["claude_steps"]:
                    self.assertIn(step, text)
                for key in ("connect_intro", "url_hint", "code_signin", "plan", "code_run"):
                    self.assertIn(copy_[key], text, (locale, key))
                self.assertIn(copy_["revoke"].format(menu=MENU[locale]), text)

    def test_the_api_key_way_is_intact(self):
        en = _notes("en")
        for title in (EN_CONNECTING, EN_KEYS):
            text = _text(en[title])
            self.assertIn(
                "https://api.genosai.dev/api/public/v1/mcp?team_id=<your team's id>", text
            )
            self.assertIn("no trailing slash", text)
            self.assertIn("ApiKey", text)
        text = _text(en[EN_CONNECTING])
        self.assertIn("Authorization: ApiKey <your key>", text)
        self.assertIn("The scheme is the literal word ApiKey, not Bearer.", text)
        # Core and above, as the MCP endpoint itself says; Pro was wrong.
        self.assertIn("On Core and above", _text(en[EN_KEYS]))
        self.assertNotIn("Pro and above", _text(en[EN_KEYS]))
        ja = _notes("ja")
        for title in (JA_CONNECTING, JA_KEYS):
            text = _text(ja[title])
            self.assertIn(
                "https://api.genosai.dev/api/public/v1/mcp?team_id=<your team's id>", text
            )
            self.assertIn("ApiKey", text)
        self.assertIn("Authorization: ApiKey <your key>", _text(ja[JA_CONNECTING]))
        self.assertIn("Core以上", _text(ja[JA_KEYS]))
        self.assertNotIn("Proプラン以上", _text(ja[JA_KEYS]))

    def test_other_notes_are_the_static_ones_and_the_counts_do_not_change(self):
        for locale, (_, static) in guide_notes._EDITIONS.items():
            now = guide_notes.current_guide_notes(locale)
            self.assertEqual([t for t, _ in now], [t for t, _ in static])
            for (title, body), (_, static_body) in zip(now, static, strict=True):
                if title not in MCP_NOTES[locale]:
                    self.assertIs(body, static_body)

    def test_each_note_has_no_empty_block_and_no_duplicate_ids(self):
        for locale, titles in MCP_NOTES.items():
            for title in titles:
                body = _notes(locale)[title]
                for block in body:
                    if block.get("type") != "paragraph":
                        self.assertTrue(_text(block).strip(), (title, block.get("type")))
                ids = [b["id"] for b in body]
                self.assertEqual(len(ids), len(set(ids)))


@override_settings(**OFF)
class ConnectSectionOffTests(SimpleTestCase):
    def test_with_the_oauth_way_off_the_notes_are_the_static_text(self):
        for locale, (_, static) in guide_notes._EDITIONS.items():
            now = dict(guide_notes.current_guide_notes(locale))
            for title in MCP_NOTES[locale]:
                self.assertNotIn(HEADING[locale], _headings(now[title]))
                self.assertEqual(
                    guide_notes.body_fingerprint(now[title]),
                    guide_notes.body_fingerprint(dict(static)[title]),
                )

    def test_the_static_text_still_has_the_key_way_under_its_heading(self):
        for locale, (_, static) in guide_notes._EDITIONS.items():
            for title in MCP_NOTES[locale]:
                self.assertIn(SCRIPTS[locale], _headings(dict(static)[title]))

    def test_a_missing_origin_leaves_the_section_out_even_when_the_switch_is_on(self):
        with override_settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL=""):
            now = dict(guide_notes.current_guide_notes("en"))
            self.assertNotIn(HEADING["en"], _headings(now[EN_CONNECTING]))

    def test_the_refresh_refuses_without_the_oauth_way(self):
        with self.assertRaises(ValueError):
            guide_notes.refresh_unedited_connect_notes(apply=True)


class PreviousTextPinTests(SimpleTestCase):
    """The hashes in `PREVIOUS_BODY_SHA256` are the texts that shipped, no others."""

    def test_the_pinned_hashes_are_exactly_the_fixture_hashes(self):
        fixture = {
            title: {v["sha256"] for v in versions}
            for per_locale in FIXTURE.values()
            for title, versions in per_locale.items()
        }
        self.assertEqual({t: set(h) for t, h in guide_notes.PREVIOUS_BODY_SHA256.items()}, fixture)

    def test_each_fixture_body_hashes_to_its_pin_with_ids_added_back(self):
        for per_locale in FIXTURE.values():
            for title, versions in per_locale.items():
                for version in versions:
                    self.assertEqual(
                        guide_notes.body_fingerprint(version["body"]), version["sha256"]
                    )
                    self.assertEqual(
                        guide_notes.body_fingerprint(_with_ids(version["body"])), version["sha256"]
                    )
                    self.assertIn(version["sha256"], guide_notes.PREVIOUS_BODY_SHA256[title])

    def test_the_text_on_main_before_this_change_is_a_pinned_version(self):
        # The static lists changed in this PR, so they are NOT the previous text...
        for locale, titles in MCP_NOTES.items():
            static = dict(guide_notes._EDITIONS[locale][1])
            for title in titles:
                self.assertNotIn(
                    guide_notes.body_fingerprint(static[title]),
                    guide_notes.PREVIOUS_BODY_SHA256[title],
                )

    def test_a_one_character_change_or_a_nested_change_changes_the_fingerprint(self):
        body = _previous("en", EN_KEYS)
        base = guide_notes.body_fingerprint(body)
        edited = copy.deepcopy(body)
        edited[1]["content"][0]["text"] += "."
        self.assertNotEqual(guide_notes.body_fingerprint(edited), base)
        nested = copy.deepcopy(body)
        nested[1]["children"] = [_with_ids({"type": "paragraph", "props": {}, "content": []})]
        self.assertNotEqual(guide_notes.body_fingerprint(nested), base)
        # ids at every level are ignored
        self.assertEqual(guide_notes.body_fingerprint(_with_ids(_strip(body))), base)


def _strip(body):
    return guide_notes._strip_block_ids(body)


@override_settings(**ON)
class SeedingTests(BaseAPITestCase):
    def test_a_new_seeding_writes_the_section_into_both_editions(self):
        guide_notes.seed_guide_notes(self.user, self.team, force=True)
        for locale, titles in MCP_NOTES.items():
            for title in titles:
                note = PersonalNoteMaster.objects.get(owner=self.user, team=self.team, title=title)
                self.assertIn(HEADING[locale], _headings(note.body))
                self.assertIn(MCP, _text(note.body))

    @override_settings(**OFF)
    def test_with_the_oauth_way_off_seeding_writes_no_section(self):
        guide_notes.seed_guide_notes(self.user, self.team, force=True)
        note = PersonalNoteMaster.objects.get(owner=self.user, team=self.team, title=EN_CONNECTING)
        self.assertNotIn(HEADING["en"], _headings(note.body))
        self.assertIn(SCRIPTS["en"], _headings(note.body))


@override_settings(**ON)
class RefreshTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            "origin.services.guide_notes.push_note_body_now", return_value=PushOutcome.SENT
        )
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def _note(self, locale, title, version=0, body=None, team=None):
        return PersonalNoteMaster.objects.create(
            team=team or self.team,
            owner=self.user,
            title=title,
            body=_previous(locale, title, version) if body is None else body,
        )

    def _fresh(self, note):
        return PersonalNoteMaster.objects.get(pk=note.pk)

    def test_an_unedited_note_is_replaced_in_every_locale_and_shipped_version(self):
        cases = []
        for locale, titles in MCP_NOTES.items():
            for title in titles:
                for version in range(len(FIXTURE[locale][title])):
                    cases.append((locale, title, version, self._note(locale, title, version)))
        self.assertEqual(len(cases), 6)  # 2 locales x (1 + 2 shipped versions)
        before = {n.pk: n.ts_updated_at for *_, n in cases}

        report = guide_notes.refresh_unedited_connect_notes(apply=True)

        self.assertEqual(sorted(report.replaced), sorted(n.pk for *_, n in cases))
        self.assertEqual(report.left_alone, [])
        for locale, title, _version, note in cases:
            now = self._fresh(note)
            self.assertIn(HEADING[locale], _headings(now.body))
            self.assertIn(MCP, _text(now.body))
            self.assertGreater(now.ts_updated_at, before[note.pk])
            self.assertEqual(
                guide_notes.body_fingerprint(now.body),
                guide_notes.body_fingerprint(dict(guide_notes.current_guide_notes(locale))[title]),
            )
        self.assertEqual(report.pushes, {"sent": 6})
        self.assertEqual(self.push.call_count, 6)

    def test_the_replaced_text_is_kept_as_a_version_and_pushed_to_the_editor(self):
        note = self._note("en", EN_KEYS)
        old_fingerprint = guide_notes.body_fingerprint(note.body)

        guide_notes.refresh_unedited_connect_notes(apply=True)

        versions = list(
            NoteVersionMaster.objects.filter(note_type=1, note_id=note.note_id).order_by(
                "version_no"
            )
        )
        self.assertEqual(len(versions), 2)
        self.assertEqual(guide_notes.body_fingerprint(versions[0].body), old_fingerprint)
        self.assertEqual(versions[1].body, self._fresh(note).body)
        ((args, _),) = self.push.call_args_list
        self.assertEqual(args[:2], ("personal", note.note_id))
        self.assertEqual(args[2], self._fresh(note).body)

    def test_an_edited_note_is_left_alone(self):
        edited_text = self._note("en", EN_CONNECTING)
        body = _previous("en", EN_CONNECTING)
        body[1]["content"][0]["text"] += " (my note)"
        edited_text.body = body
        edited_text.save()
        nested = self._note("ja", JA_KEYS)
        body = _previous("ja", JA_KEYS)
        body[2]["children"] = [copy.deepcopy(body[3])]
        nested.body = body
        nested.save()
        emptied = self._note("en", EN_KEYS, body=[])
        none_body = self._note("ja", JA_CONNECTING, body=[])
        PersonalNoteMaster.objects.filter(pk=none_body.pk).update(body=None)
        snapshots = {n.pk: self._fresh(n).body for n in (edited_text, nested, emptied, none_body)}

        report = guide_notes.refresh_unedited_connect_notes(apply=True)

        self.assertEqual(report.replaced, [])
        self.assertEqual(
            sorted(report.left_alone),
            sorted([edited_text.pk, nested.pk, emptied.pk, none_body.pk]),
        )
        for pk, body in snapshots.items():
            self.assertEqual(PersonalNoteMaster.objects.get(pk=pk).body, body)
        self.assertEqual(self.push.call_count, 0)
        self.assertFalse(NoteVersionMaster.objects.exists())

    def test_a_note_edited_between_the_scan_and_the_write_is_left_alone(self):
        note = self._note("en", EN_KEYS)
        edited = _previous("en", EN_KEYS)
        edited[1]["content"][0]["text"] += "!"
        PersonalNoteMaster.objects.filter(pk=note.pk).update(body=edited)

        changed = guide_notes._replace_if_unedited(
            note.pk, EN_KEYS, guide_notes.PREVIOUS_BODY_SHA256[EN_KEYS], [{"id": "x"}]
        )

        self.assertFalse(changed)
        self.assertEqual(self._fresh(note).body, edited)

    def test_a_dry_run_writes_nothing(self):
        note = self._note("en", EN_CONNECTING)
        before = self._fresh(note)

        report = guide_notes.refresh_unedited_connect_notes(apply=False)

        self.assertEqual(report.replaced, [note.pk])
        after = self._fresh(note)
        self.assertEqual(after.body, before.body)
        self.assertEqual(after.ts_updated_at, before.ts_updated_at)
        self.assertEqual(self.push.call_count, 0)
        self.assertFalse(NoteVersionMaster.objects.exists())

    def test_a_second_run_finds_the_notes_already_current(self):
        note = self._note("ja", JA_CONNECTING)
        guide_notes.refresh_unedited_connect_notes(apply=True)
        stamp = self._fresh(note).ts_updated_at

        report = guide_notes.refresh_unedited_connect_notes(apply=True)

        self.assertEqual(report.replaced, [])
        self.assertEqual(report.already_current, [note.pk])
        self.assertEqual(self._fresh(note).ts_updated_at, stamp)
        self.assertEqual(self.push.call_count, 1)

    def test_a_team_scope_only_touches_that_team(self):
        mine = self._note("en", EN_CONNECTING)
        other_team = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        other = self._note("en", EN_CONNECTING, team=other_team)

        guide_notes.refresh_unedited_connect_notes(apply=True, team_id=self.team.team_id)

        self.assertIn(HEADING["en"], _headings(self._fresh(mine).body))
        self.assertNotIn(HEADING["en"], _headings(self._fresh(other).body))

    def test_a_note_with_another_title_is_never_looked_at(self):
        note = self._note("en", EN_CONNECTING)
        PersonalNoteMaster.objects.filter(pk=note.pk).update(title="My copy")

        report = guide_notes.refresh_unedited_connect_notes(apply=True)

        self.assertEqual(report, guide_notes.RefreshReport())

    def test_the_command_is_a_dry_run_unless_apply_is_given(self):
        note = self._note("en", EN_CONNECTING)
        out = mock.MagicMock()
        call_command("refresh_guide_notes", stdout=out)
        self.assertNotIn(HEADING["en"], _headings(self._fresh(note).body))
        shown = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("DRY RUN", shown)
        self.assertIn("1 unedited note(s) would be replaced", shown)

        call_command("refresh_guide_notes", "--apply", stdout=mock.MagicMock())
        self.assertIn(HEADING["en"], _headings(self._fresh(note).body))

    @override_settings(**OFF)
    def test_the_command_refuses_without_the_oauth_way(self):
        with self.assertRaises(CommandError):
            call_command("refresh_guide_notes", "--apply", stdout=mock.MagicMock())
