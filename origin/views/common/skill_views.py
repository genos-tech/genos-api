"""Genos Skills over REST (MIP-2040).

    GET    /api/v2/skills/?team_id=&tab=             a tab of the list: mine | team | installed | starred
    GET    /api/v2/skills/?team_id=&tab=runnable     the `/` picker: what the caller may run here (MIP-2042)
    POST   /api/v2/skills/                           create one (`team_id` in the body)
    POST   /api/v2/skills/import/                    a new skill from a SKILL.md file
    GET    /api/v2/skills/<id>/                      one skill with its text
    PATCH  /api/v2/skills/<id>/                      save the owner's draft (readers keep the published version)
    DELETE /api/v2/skills/<id>/                      delete it (the owner)
    GET    /api/v2/skills/<id>/versions/             the versions a reader may see
    GET    /api/v2/skills/<id>/versions/<n>/         one version with its text
    POST   /api/v2/skills/<id>/publish/              cut a new version from the draft (the owner)
    POST   /api/v2/skills/<id>/copy/                 copy into the caller's own skills (`team_id`)
    POST   /api/v2/skills/<id>/install/              pin a public skill (DELETE removes, PATCH accepts the update)
    POST   /api/v2/skills/<id>/star/                 star it (DELETE removes)
    GET    /api/v2/skills/<id>/export/               the SKILL.md text
    GET    /api/v2/skills/<id>/editors/              who co-edits it (the owner and the co-editors)
    POST   /api/v2/skills/<id>/editors/              invite a teammate (`userId`; the owner)
    DELETE /api/v2/skills/<id>/editors/<user>/       remove a co-editor (the owner; a co-editor leaves)
    POST   /api/v2/skills/<id>/hide/                 hide a team skill from the team (the team owner)
    DELETE /api/v2/skills/<id>/hide/                 show it again
    POST   /api/v2/skills/<id>/report/               report a skill the caller can read (`reason`, `note`; MIP-2044)
    POST   /api/v2/skills/<id>/takedown/             operator (staff) only: stop it for everybody (`reason`)
    DELETE /api/v2/skills/<id>/takedown/             operator only: put it back (`reason` optional)
    GET    /api/v2/skills/hub/?q=&tag=&sort=&...     the public directory (MIP-2047; 404 until GENOS_SKILLS_PUBLIC)
    GET    /api/v2/skills/hub/tags/                  the directory's tags with counts
    GET    /api/v2/skills/profile/                   my public profile (handle) and what publishing needs
    PUT    /api/v2/skills/profile/                   choose the handle once; change the display name
    GET    /api/v2/skills/<id>/publish-check/        the publish checklist, ticked off (the owner)

The rules live in `services/skills/service.py`; this file is the wire: camelCase keys, one
`{"error": "<sentence>"}` for every refusal, and JWT only (an API key does not manage skills).
The whole API is a 404 while `GENOS_SKILLS` is off. A skill that is not the caller's to read is
the same 404 as a missing one.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.skills.skill_models import Skill, SkillStar, SkillVersion
from origin.services.skills import hub, moderation, run, service, text
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_EDITOR
from origin.views.utils.request_validators import as_uuid
from origin.views.utils.scope_guards import is_team_member

IMPORT_MAX_CHARS = 100_000


class SkillsThrottle(WindowRateThrottle):
    scope = "skills"
    rate = "120/min"
    fail_closed = True


class SkillsHubThrottle(WindowRateThrottle):
    """The directory is the one place a stranger's query reaches the database: 60 searches a minute
    per account (design section 8)."""

    scope = "skills_hub"
    rate = "60/min"
    fail_closed = True


def _error(message: str, http: int = status.HTTP_400_BAD_REQUEST, **extra: Any) -> Response:
    return Response({"error": message, **extra}, status=http)


# The fields a request may carry, camelCase on the wire.
_FIELDS = {
    "title": "title",
    "slug": "slug",
    "description": "description",
    "tags": "tags",
    "icon": "icon",
    "argumentHint": "argument_hint",
    "toolLevel": "tool_level",
    "effort": "effort",
    "language": "language",
    "bodyMarkdown": "body_markdown",
    "visibility": "visibility",
    # Making a skill public (MIP-2047): not stored on the skill.
    "acceptTerms": "accept_terms",
    "handle": "handle",
    "displayName": "display_name",
}


def _fields(body: dict[str, Any]) -> service.Fields:
    return service.Fields(**{py: body[wire] for wire, py in _FIELDS.items() if wire in body})


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _row(
    skill: Skill,
    user_id: Any,
    *,
    starred: bool | None = None,
    editing: bool | None = None,
    team_owner: bool | None = None,
    version_hint: SkillVersion | None = None,
) -> dict[str, Any]:
    """A skill as the list and the page read it. A reader sees the metadata of the PUBLISHED
    version (what a reader gets must not move under them), never the owner's draft edits, and
    never the body note's id. The owner and the co-editors see the draft. `editing` and
    `team_owner` let a list answer both questions for many rows in a fixed number of queries."""
    mine = service.is_owner(skill, user_id)
    edits = mine or (service.is_editor(skill, user_id) if editing is None else editing)
    # `version_hint` is the published version a list already fetched for all its rows in one query.
    version = None if edits else (version_hint or service.version_for(skill, user_id))
    src = version if version is not None else skill
    hideable = (
        skill.visibility == "team"
        and skill.status in ("active", "hidden_by_team")
        and not mine
        and (service.is_team_owner(skill, user_id) if team_owner is None else team_owner)
    )
    out: dict[str, Any] = {
        "id": str(skill.id),
        "teamId": str(skill.team_id),
        "slug": src.slug or skill.slug,
        "title": src.title,
        "description": src.description,
        "tags": list(src.tags or []),
        "icon": src.icon,
        "argumentHint": src.argument_hint,
        "toolLevel": src.tool_level,
        "effort": src.effort,
        "language": src.language,
        "visibility": skill.visibility,
        "status": skill.status,
        "currentVersionNo": skill.current_version_no if edits else skill.published_version_no,
        "publishedVersionNo": skill.published_version_no,
        "source": skill.source,
        "isOfficial": skill.is_official,
        "forkedFromId": str(skill.forked_from_id) if skill.forked_from_id else None,
        "starCount": skill.star_count,
        "runCount30d": skill.run_count_30d,
        "copyCount": skill.copy_count,
        "installCount": skill.install_count,
        "createdAt": _iso(skill.created_at),
        "updatedAt": _iso(skill.updated_at if version is None else version.created_at),
        "publishedAt": _iso(skill.published_at),
        "mine": mine,
        # What this person may do with it (MIP-2043): the owner manages; a co-editor writes the
        # text and publishes; the team owner may hide a team skill. Everything else is reading.
        "role": "owner" if mine else "editor" if edits else "reader",
        "canEdit": edits,
        "canManage": mine,
        "canHide": hideable,
        "hiddenByTeam": skill.status == "hidden_by_team",
    }
    # Who wrote it: a teammate by name. An outside author of a public skill stays unnamed until
    # the Hub's public handle exists (MIP-2047): the account name is not theirs to publish.
    if edits or skill.visibility == "team":
        out["ownerName"] = skill.owner.username if skill.owner_id else ""
    if skill.visibility == "public":
        # The byline of a public skill is the author's PUBLIC handle (MIP-2047), never the account
        # name or the email: `null` while the author has none (an official skill).
        out["author"] = hub.author_of(skill)
    if edits:
        out["noteId"] = skill.note_id
        # An operator's decision about the author's own skill: shown so they know why it stopped.
        if skill.status in service.MODERATED_STATUSES:
            out["moderationReason"] = skill.moderation_reason
    if starred is not None:
        out["starred"] = starred
    return out


_WIRE_NAMES = {py: wire for wire, py in _FIELDS.items()}


def _add_draft_state(out: dict[str, Any], skill: Skill, user_id: Any) -> None:
    """The owner's page and save answer: how the draft differs from the published version, so the
    editor can offer "Publish changes". `unpublishedChanges` names the fields (camelCase, the same
    keys a PATCH takes; the text is `bodyMarkdown`). Readers never get it, and the list leaves it
    out (one extra query per row)."""
    if not service.reads_draft(skill, user_id):
        return
    changed = service.unpublished_changes(skill, live_text=out.get("bodyMarkdown"))
    out["unpublishedChanges"] = [_WIRE_NAMES[name] for name in changed]
    out["hasUnpublishedChanges"] = bool(changed)


def _version_row(version: SkillVersion, *, with_body: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "versionNo": version.version_no,
        "title": version.title,
        "description": version.description,
        "tags": list(version.tags or []),
        "toolLevel": version.tool_level,
        "argumentHint": version.argument_hint,
        "changelog": version.changelog,
        "contentHash": version.content_hash,
        "createdAt": _iso(version.created_at),
    }
    if with_body:
        out["bodyMarkdown"] = version.body_markdown
    return out


def _person(user: Any) -> dict[str, str]:
    return {"id": str(user.pk), "username": user.username or ""}


class _Base(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [SkillsThrottle]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not service.enabled():
            raise NotFound()

    @staticmethod
    def _run(call):
        try:
            return call(), None
        except text.SkillTextError as exc:  # the net under the service's own wording
            return None, _error(str(exc))
        except service.SkillError as exc:
            extra = {**({"code": exc.code} if exc.code else {}), **exc.extra}
            return None, _error(str(exc), exc.status, **extra)

    @staticmethod
    def _team(value: Any):
        team = as_uuid(value)
        return str(team) if team else None


class SkillListView(_Base):
    def get(self, request):
        user = request.user
        tab = request.query_params.get("tab") or "mine"
        team = self._team(request.query_params.get("team_id"))
        if tab in ("mine", "team", "runnable") and team is None:
            return _error("`team_id` is required.")
        if tab == "runnable":
            # The `/` picker (MIP-2042): what this caller may run in this team, grouped in the order
            # a typed name resolves. Metadata only, never the text.
            entries, problem = self._run(lambda: run.runnable(user, team))
            if problem:
                return problem
            return Response({"skills": entries})
        rows, problem = self._run(lambda: list(service.list_for(user, team, tab)))
        if problem:
            return problem
        starred = set()
        editing: set[int] = set()
        owned_teams: set[str] = set()
        if rows:
            starred = {
                str(i)
                for i in SkillStar.objects.filter(user=user, skill__in=rows).values_list(
                    "skill_id", flat=True
                )
            }
            # The two role questions of every row, in two queries however many rows (an N+1 on
            # the Team tab otherwise).
            editing = set(
                NotePermissionMaster.objects.filter(
                    user=user,
                    note_type=NOTE_TYPE_PERSONAL,
                    role_id=ROLE_EDITOR,
                    note_id__in=[s.note_id for s in rows if s.note_id is not None],
                ).values_list("note_id", flat=True)
            )
            owned_teams = {
                str(t)
                for t in TeamMaster.objects.filter(owner=user).values_list("team_id", flat=True)
            }
            # `is_team_member` once per team, not once per row.
            member_of = {
                str(t): is_team_member(t, user.pk)
                for t in {s.team_id for s in rows if s.note_id in editing}
            }
        return Response(
            {
                "skills": [
                    _row(
                        s,
                        user.pk,
                        starred=str(s.id) in starred,
                        editing=s.note_id in editing and member_of.get(str(s.team_id), False),
                        team_owner=str(s.team_id) in owned_teams,
                    )
                    for s in rows
                ],
                "limit": service.skills_limit(user.pk),
                "used": Skill.objects.filter(owner=user).count(),
                "publicEnabled": service.public_enabled(),
            }
        )

    def post(self, request):
        body = request.data
        team = self._team(body.get("team_id"))
        if team is None:
            return _error("`team_id` is required.")
        skill, problem = self._run(lambda: service.create(request.user, team, _fields(body)))
        if problem:
            return problem
        return Response({"skill": _row(skill, request.user.pk)}, status=status.HTTP_201_CREATED)


class SkillImportView(_Base):
    def post(self, request):
        body = request.data
        team = self._team(body.get("team_id"))
        if team is None:
            return _error("`team_id` is required.")
        content = body.get("content")
        if not isinstance(content, str):
            return _error("`content` must be the text of a SKILL.md file.")
        if len(content) > IMPORT_MAX_CHARS:
            return _error(f"The file may be at most {IMPORT_MAX_CHARS:,} characters.")
        skill, problem = self._run(lambda: service.import_file(request.user, team, content))
        if problem:
            return problem
        return Response({"skill": _row(skill, request.user.pk)}, status=status.HTTP_201_CREATED)


class SkillDetailView(_Base):
    def get(self, request, skill_id):
        user = request.user
        skill, problem = self._run(lambda: service.get_readable(user.pk, skill_id))
        if problem:
            return problem
        out = _row(skill, user.pk, starred=service.is_starred(user.pk, skill))
        out["bodyMarkdown"] = service.body_markdown_for(skill, user.pk)
        _add_draft_state(out, skill, user.pk)
        if service.reads_draft(skill, user.pk):
            out["editors"] = [_person(u) for u in service.editors_of(skill)]
        attributed = service.attribution(skill, user.pk)
        if attributed is not None:
            out["forkedFrom"] = attributed
        installed = service.installed_version(user.pk, skill)
        if installed is not None:
            out["installedVersionNo"] = installed
            out["updateAvailable"] = installed < skill.published_version_no
        return Response({"skill": out})

    def patch(self, request, skill_id):
        body = request.data
        fields = _fields(body)
        if all(getattr(fields, f) is None for f in service.Fields.__dataclass_fields__):
            return _error("Nothing to change.")
        skill, problem = self._run(lambda: service.update(request.user, skill_id, fields))
        if problem:
            return problem
        out = _row(skill, request.user.pk)
        out["bodyMarkdown"] = service.body_markdown_for(skill, request.user.pk)
        _add_draft_state(out, skill, request.user.pk)
        return Response({"skill": out})

    def delete(self, request, skill_id):
        _, problem = self._run(lambda: service.delete(request.user, skill_id))
        if problem:
            return problem
        return Response(status=status.HTTP_204_NO_CONTENT)


class SkillVersionListView(_Base):
    def get(self, request, skill_id):
        out, problem = self._run(lambda: service.versions_of(request.user.pk, skill_id))
        if problem:
            return problem
        _skill, versions = out
        return Response({"versions": [_version_row(v) for v in versions]})


class SkillVersionDetailView(_Base):
    def get(self, request, skill_id, version_no):
        out, problem = self._run(lambda: service.get_version(request.user.pk, skill_id, version_no))
        if problem:
            return problem
        _skill, version = out
        return Response({"version": _version_row(version, with_body=True)})


class SkillPublishView(_Base):
    def post(self, request, skill_id):
        changelog = request.data.get("changelog")
        version, problem = self._run(
            lambda: service.publish(
                request.user, skill_id, changelog=changelog if isinstance(changelog, str) else ""
            )
        )
        if problem:
            return problem
        return Response({"version": _version_row(version)}, status=status.HTTP_201_CREATED)


class SkillCopyView(_Base):
    def post(self, request, skill_id):
        team = self._team(request.data.get("team_id"))
        if team is None:
            return _error("`team_id` is required.")
        skill, problem = self._run(lambda: service.copy(request.user, skill_id, team))
        if problem:
            return problem
        return Response({"skill": _row(skill, request.user.pk)}, status=status.HTTP_201_CREATED)


class SkillInstallView(_Base):
    def post(self, request, skill_id):
        row, problem = self._run(lambda: service.install(request.user, skill_id))
        if problem:
            return problem
        return Response({"installedVersionNo": row.version_no}, status=status.HTTP_201_CREATED)

    def patch(self, request, skill_id):
        row, problem = self._run(lambda: service.accept_update(request.user, skill_id))
        if problem:
            return problem
        return Response({"installedVersionNo": row.version_no})

    def delete(self, request, skill_id):
        _, problem = self._run(lambda: service.uninstall(request.user, skill_id))
        if problem:
            return problem
        return Response(status=status.HTTP_204_NO_CONTENT)


class SkillStarView(_Base):
    def post(self, request, skill_id):
        created, problem = self._run(lambda: service.star(request.user, skill_id))
        if problem:
            return problem
        return Response({"starred": True}, status=status.HTTP_201_CREATED if created else 200)

    def delete(self, request, skill_id):
        _, problem = self._run(lambda: service.unstar(request.user, skill_id))
        if problem:
            return problem
        return Response(status=status.HTTP_204_NO_CONTENT)


class SkillEditorsView(_Base):
    """Who co-edits a skill (MIP-2043). The owner and the co-editors may read the list; only the
    owner adds."""

    def get(self, request, skill_id):
        people, problem = self._run(lambda: service.list_editors(request.user.pk, skill_id))
        if problem:
            return problem
        return Response({"editors": [_person(u) for u in people]})

    def post(self, request, skill_id):
        target = as_uuid(request.data.get("userId"))
        if target is None:
            return _error("`userId` is required.")
        people, problem = self._run(lambda: service.add_editor(request.user, skill_id, target))
        if problem:
            return problem
        return Response({"editors": [_person(u) for u in people]}, status=status.HTTP_201_CREATED)


class SkillEditorDetailView(_Base):
    def delete(self, request, skill_id, user_id):
        people, problem = self._run(lambda: service.remove_editor(request.user, skill_id, user_id))
        if problem:
            return problem
        return Response({"editors": [_person(u) for u in people]})


class SkillHideView(_Base):
    """The team owner hides a team skill from the team (POST) or shows it again (DELETE)."""

    def post(self, request, skill_id):
        skill, problem = self._run(lambda: service.set_hidden(request.user, skill_id, True))
        if problem:
            return problem
        return Response({"skill": _row(skill, request.user.pk)})

    def delete(self, request, skill_id):
        skill, problem = self._run(lambda: service.set_hidden(request.user, skill_id, False))
        if problem:
            return problem
        return Response({"skill": _row(skill, request.user.pk)})


class SkillExportView(_Base):
    def get(self, request, skill_id):
        out, problem = self._run(lambda: service.export_file(request.user.pk, skill_id))
        if problem:
            return problem
        slug, content = out
        return Response({"filename": f"{slug}/SKILL.md", "content": content})


class SkillReportView(_Base):
    """Report a skill the caller can read (inside the skill page and the list row; no page of its
    own). Idempotent: a second report of the same skill by the same account is the first."""

    def post(self, request, skill_id):
        body = request.data
        out, problem = self._run(
            lambda: moderation.report(request.user, skill_id, body.get("reason"), body.get("note"))
        )
        if problem:
            return problem
        row, created = out
        return Response(
            {"reported": True, "reportId": str(row.id)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SkillTakedownView(_Base):
    """The operator API: stop a skill for everybody (POST) or put it back (DELETE). Staff only; any
    other caller gets the same 404 as an unknown skill."""

    def post(self, request, skill_id):
        skill, problem = self._run(
            lambda: moderation.take_down(request.user, skill_id, request.data.get("reason"))
        )
        if problem:
            return problem
        return Response({"id": str(skill.id), "status": skill.status})

    def delete(self, request, skill_id):
        reason = request.data.get("reason") if hasattr(request.data, "get") else ""
        skill, problem = self._run(lambda: moderation.restore(request.user, skill_id, reason))
        if problem:
            return problem
        return Response({"id": str(skill.id), "status": skill.status})


# -- the Hub (MIP-2047) -------------------------------------------------------------------------


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class SkillHubView(_Base):
    """The public directory. 404 until `GENOS_SKILLS_PUBLIC` is on. Three queries however many
    rows: the page, the page's published versions, the caller's stars."""

    throttle_classes = [SkillsThrottle, SkillsHubThrottle]

    def get(self, request):
        user = request.user
        params = request.query_params
        out, problem = self._run(
            lambda: hub.directory(
                user,
                query=params.get("q", ""),
                tag=params.get("tag", ""),
                sort=params.get("sort") or "stars",
                tool_level=params.get("tool_level", ""),
                official=params.get("official") in ("1", "true"),
                language=params.get("language", ""),
                limit=_int(params.get("limit"), hub.PAGE_SIZE),
                offset=_int(params.get("offset"), 0),
            )
        )
        if problem:
            return problem
        rows, versions, has_more = out
        starred = hub.starred_ids(user.pk, rows)
        return Response(
            {
                "skills": [
                    _row(
                        s,
                        user.pk,
                        starred=str(s.id) in starred,
                        editing=False,
                        team_owner=False,
                        version_hint=versions.get(s.pk),
                    )
                    for s in rows
                ],
                "hasMore": has_more,
            }
        )


class SkillHubTagsView(_Base):
    throttle_classes = [SkillsThrottle, SkillsHubThrottle]

    def get(self, request):
        tags, problem = self._run(lambda: hub.popular_tags())
        if problem:
            return problem
        return Response({"tags": tags})


def _profile_out(user: Any) -> dict[str, Any]:
    profile = hub.get_profile(user.pk)
    return {
        "handle": profile.handle if profile else None,
        "displayName": profile.display_name if profile else "",
        "publicSkills": Skill.objects.filter(owner=user, visibility="public").count(),
        "limits": {
            "publishesPerDay": hub.PUBLISHES_PER_DAY,
            "publicSkillsMax": hub.PUBLIC_SKILLS_MAX,
            "starsPerDay": hub.STARS_PER_DAY,
        },
    }


class SkillProfileView(_Base):
    """My public profile: the handle (chosen once) and the display name."""

    def get(self, request):
        _ok, problem = self._run(hub.require_hub)
        if problem:
            return problem
        return Response({"profile": _profile_out(request.user)})

    def put(self, request):
        body = request.data
        _profile, problem = self._run(
            lambda: hub.set_profile(request.user, body.get("handle"), body.get("displayName"))
        )
        if problem:
            return problem
        return Response({"profile": _profile_out(request.user)})


class SkillPublishCheckView(_Base):
    """The publish checklist for one skill, ticked off. The owner and the co-editors only (a reader
    has nothing to publish)."""

    def get(self, request, skill_id):
        def build():
            hub.require_hub()
            skill = service.get_editable(request.user.pk, skill_id)
            return hub.checklist(request.user, skill)

        rows, problem = self._run(build)
        if problem:
            return problem
        return Response(
            {
                "checks": rows,
                "ready": all(r["ok"] for r in rows),
                "profile": _profile_out(request.user),
            }
        )
