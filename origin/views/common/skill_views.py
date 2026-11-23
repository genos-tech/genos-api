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

from origin.models.skills.skill_models import Skill, SkillStar, SkillVersion
from origin.services.skills import run, service, text
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import as_uuid

IMPORT_MAX_CHARS = 100_000


class SkillsThrottle(WindowRateThrottle):
    scope = "skills"
    rate = "120/min"
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
}


def _fields(body: dict[str, Any]) -> service.Fields:
    return service.Fields(**{py: body[wire] for wire, py in _FIELDS.items() if wire in body})


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _row(skill: Skill, user_id: Any, *, starred: bool | None = None) -> dict[str, Any]:
    """A skill as the list and the page read it. A non-owner sees the metadata of the PUBLISHED
    version (what a reader gets must not move under them), never the owner's draft edits, and
    never the body note's id."""
    mine = service.is_owner(skill, user_id)
    version = None if mine else service.version_for(skill, user_id)
    src = version if version is not None else skill
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
        "currentVersionNo": skill.current_version_no if mine else skill.published_version_no,
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
        "canEdit": mine,
    }
    # Who wrote it: a teammate by name. An outside author of a public skill stays unnamed until
    # the Hub's public handle exists (MIP-2047): the account name is not theirs to publish.
    if mine or skill.visibility == "team":
        out["ownerName"] = skill.owner.username if skill.owner_id else ""
    if mine:
        out["noteId"] = skill.note_id
    if starred is not None:
        out["starred"] = starred
    return out


_WIRE_NAMES = {py: wire for wire, py in _FIELDS.items()}


def _add_draft_state(out: dict[str, Any], skill: Skill, user_id: Any) -> None:
    """The owner's page and save answer: how the draft differs from the published version, so the
    editor can offer "Publish changes". `unpublishedChanges` names the fields (camelCase, the same
    keys a PATCH takes; the text is `bodyMarkdown`). Readers never get it, and the list leaves it
    out (one extra query per row)."""
    if not service.is_owner(skill, user_id):
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
        if rows:
            starred = {
                str(i)
                for i in SkillStar.objects.filter(user=user, skill__in=rows).values_list(
                    "skill_id", flat=True
                )
            }
        return Response(
            {
                "skills": [_row(s, user.pk, starred=str(s.id) in starred) for s in rows],
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


class SkillExportView(_Base):
    def get(self, request, skill_id):
        out, problem = self._run(lambda: service.export_file(request.user.pk, skill_id))
        if problem:
            return problem
        slug, content = out
        return Response({"filename": f"{slug}/SKILL.md", "content": content})
