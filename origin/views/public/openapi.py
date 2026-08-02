"""The OpenAPI document for `/api/public/v1/`, and why it is written by hand.

Served from the API rather than shipped in the frontend so there is ONE
copy. A spec duplicated into the marketing site is a spec that drifts,
and the drift is invisible — nobody notices a stale schema until an
integrator has already built against it.

## Hand-authored, with a test instead of a generator

`drf-spectacular` was the obvious choice and is the wrong one here. It
introspects serializers, and these views have none: `v1_views` builds its
responses from explicit dicts (`_serialize_task`) precisely so the public
shape cannot drift when a model gains a field. Pointed at them, a
generator produces empty schemas that then need hand-written annotations
on every method — the same hand-authoring, spread across the view file
and coupled to a dependency.

What a generator really buys is "the spec cannot fall behind the code".
`tests/test_public_openapi.py` buys that directly: it walks
`urls/public/urls.py` and fails if a route or method is missing here.
That is the guarantee, without the 200-view blast radius of turning
schema generation on for the whole `origin` app.

## Unauthenticated on purpose

You need the document to work out how to authenticate. It describes
routes rather than exposing them, and every path in it enforces its own
key check.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

API_VERSION = "1.0.0"

# `required` names EVERY key the endpoint always returns, and
# `additionalProperties: false` forbids any it does not. Both matter:
# without `required`, a response missing every field still validates;
# without `additionalProperties`, a new model column leaking into
# `_serialize_task` ships silently. A schema with neither cannot fail a
# contract test, which is the same as not having one.
#
# `nullable` is NOT the opposite of `required` here. `display_id` is
# always present and sometimes null — that is `required` plus
# `nullable`, and conflating the two is how optional-looking fields
# quietly disappear.
_TASK = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "display_id",
        "title",
        "status",
        "priority",
        "project_id",
        "team_id",
        "assignee_id",
        "reporter_id",
        "due_date",
        "start_date",
        "created_at",
        "updated_at",
    ],
    "properties": {
        "id": {"type": "integer", "example": 4821},
        "display_id": {
            "type": "string",
            "nullable": True,
            "description": "Project code + per-project number, e.g. `GEN-42`. Null until the project has a code.",
            "example": "GEN-42",
        },
        "title": {"type": "string", "example": "Ship the public API"},
        "status": {"type": "string", "example": "Open"},
        "priority": {"type": "string", "nullable": True, "example": "High"},
        "project_id": {"type": "integer", "nullable": True},
        "team_id": {"type": "string", "format": "uuid", "nullable": True},
        "assignee_id": {"type": "string", "format": "uuid", "nullable": True},
        "reporter_id": {"type": "string", "format": "uuid", "nullable": True},
        "due_date": {"type": "string", "format": "date", "nullable": True},
        "start_date": {"type": "string", "format": "date", "nullable": True},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
}

_PROJECT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "name", "code", "team_id", "is_private", "created_at"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "code": {"type": "string", "nullable": True},
        "team_id": {"type": "string", "format": "uuid", "nullable": True},
        "is_private": {"type": "boolean"},
        "created_at": {"type": "string", "format": "date-time"},
    },
}

_ERROR = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
}


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _error(description):
    return {"description": description, **_json(_ERROR)}


OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "Genos Public API",
        "version": API_VERSION,
        "description": (
            "Read and write tasks and projects in your Genos workspace.\n\n"
            "Authenticate with an API key created in **Settings → Developer**. "
            "Send it as `Authorization: ApiKey <key>` — the scheme is `ApiKey`, "
            "not `Bearer`, so a key can never be mistaken for a session token "
            "by middleware that only looks for `Bearer`.\n\n"
            "Keys are either a **personal access token** (acts as you, across "
            "every team you belong to — so `team_id` is required where a team "
            "must be chosen) or **team-scoped** (bound to one team, which is "
            "then implied).\n\n"
            "Write endpoints additionally require a key created with `write` "
            "scope. A read key gets 403, never a silent no-op."
        ),
    },
    "servers": [
        {"url": "https://api.genosai.dev", "description": "Production"},
    ],
    "components": {
        "securitySchemes": {
            "ApiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": "`ApiKey <your key>`",
            }
        },
        "schemas": {"Task": _TASK, "Project": _PROJECT, "Error": _ERROR},
    },
    "security": [{"ApiKey": []}],
    "tags": [
        {"name": "identity", "description": "Who this key belongs to."},
        {"name": "projects"},
        {"name": "tasks"},
    ],
    "paths": {
        "/api/public/v1/me/": {
            "get": {
                "tags": ["identity"],
                "summary": "Identify the caller",
                "description": (
                    "Resolves the key to its user and reports the key's own "
                    "scope and team binding. The first call to make when "
                    "wiring up an integration — it answers 'is this key live, "
                    "and what may it do' without touching any data."
                ),
                "responses": {
                    "200": {
                        "description": "The authenticated user and the key used.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "user_id": {"type": "string", "format": "uuid"},
                                    "username": {"type": "string"},
                                    "email": {"type": "string", "format": "email"},
                                    "key": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string", "format": "uuid"},
                                            "name": {"type": "string"},
                                            "scope": {
                                                "type": "string",
                                                "enum": ["read", "write"],
                                            },
                                            "team_id": {
                                                "type": "string",
                                                "format": "uuid",
                                                "nullable": True,
                                                "description": (
                                                    "Null for a personal access token."
                                                ),
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    },
                    "401": _error("Missing, unknown, revoked or expired key."),
                },
            }
        },
        "/api/public/v1/projects/": {
            "get": {
                "tags": ["projects"],
                "summary": "List projects you can see",
                "description": (
                    "Scoped to your project memberships, not the whole team — "
                    "the same rule the app itself applies."
                ),
                "parameters": [
                    {
                        "name": "team_id",
                        "in": "query",
                        "schema": {"type": "string", "format": "uuid"},
                        "description": (
                            "Required for a personal access token. Ignored for "
                            "a team-scoped key, which already names its team."
                        ),
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Projects visible to this key.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "projects": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Project"},
                                    }
                                },
                            }
                        ),
                    },
                    "400": _error("`team_id` required for a personal access token."),
                    "401": _error("Missing or invalid key."),
                },
            }
        },
        "/api/public/v1/tasks/": {
            "get": {
                "tags": ["tasks"],
                "summary": "List tasks",
                "parameters": [
                    {
                        "name": "team_id",
                        "in": "query",
                        "schema": {"type": "string", "format": "uuid"},
                        "description": "Required for a personal access token.",
                    },
                    {
                        "name": "project_id",
                        "in": "query",
                        "schema": {"type": "integer"},
                        "description": "Narrow to one project you belong to.",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 50, "maximum": 100},
                        "description": "Capped server-side; a larger value is clamped, not rejected.",
                    },
                    {
                        "name": "offset",
                        "in": "query",
                        "schema": {"type": "integer", "default": 0},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "A page of tasks, newest first.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "tasks": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Task"},
                                    },
                                    "total": {
                                        "type": "integer",
                                        "description": "Total matching tasks, for paging.",
                                    },
                                    "limit": {"type": "integer"},
                                    "offset": {"type": "integer"},
                                },
                            }
                        ),
                    },
                    "400": _error("Bad `project_id`, `limit` or `offset`."),
                    "401": _error("Missing or invalid key."),
                    "404": _error("Project not found, or not one you belong to."),
                },
            },
            "post": {
                "tags": ["tasks"],
                "summary": "Create a task",
                "description": "Requires a key with `write` scope.",
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "required": ["title", "project_id"],
                            "properties": {
                                "title": {"type": "string", "maxLength": 255},
                                "project_id": {"type": "integer"},
                                "status": {"type": "string", "default": "Open"},
                                "priority": {"type": "string", "nullable": True},
                                "assignee_id": {
                                    "type": "string",
                                    "format": "uuid",
                                    "nullable": True,
                                },
                                "due_date": {
                                    "type": "string",
                                    "format": "date",
                                    "nullable": True,
                                },
                            },
                        }
                    ),
                },
                "responses": {
                    "201": {
                        "description": "The created task.",
                        **_json({"$ref": "#/components/schemas/Task"}),
                    },
                    "400": _error("`title` and `project_id` are required."),
                    "401": _error("Missing or invalid key."),
                    "403": _error("This key is read-only."),
                    "404": _error("Project not found, or not one you belong to."),
                },
            },
        },
        "/api/public/v1/tasks/{task_id}/": {
            "parameters": [
                {
                    "name": "task_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer"},
                }
            ],
            "get": {
                "tags": ["tasks"],
                "summary": "Fetch one task",
                "responses": {
                    "200": {
                        "description": "The task.",
                        **_json({"$ref": "#/components/schemas/Task"}),
                    },
                    "401": _error("Missing or invalid key."),
                    "404": _error(
                        "No such task — or one you cannot see. The two are "
                        "deliberately indistinguishable, so task ids cannot be "
                        "probed for existence."
                    ),
                },
            },
            "patch": {
                "tags": ["tasks"],
                "summary": "Update a task",
                "description": (
                    "Requires `write` scope. Only `title`, `status` and "
                    "`priority` are writable: the field list is an allowlist, "
                    "not a passthrough, so a new model column never silently "
                    "joins the public API."
                ),
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "minProperties": 1,
                            "properties": {
                                "title": {"type": "string", "maxLength": 255},
                                "status": {"type": "string"},
                                "priority": {"type": "string"},
                            },
                        }
                    ),
                },
                "responses": {
                    "200": {
                        "description": "The updated task.",
                        **_json({"$ref": "#/components/schemas/Task"}),
                    },
                    "400": _error("Empty title, or no writable field supplied."),
                    "401": _error("Missing or invalid key."),
                    "403": _error("This key is read-only."),
                    "404": _error("No such task, or not one you can see."),
                },
            },
        },
    },
}


class OpenApiSchemaView(APIView):
    """`GET /api/public/v1/openapi.json` — the spec itself.

    `AllowAny`: you need this document in order to work out how to
    authenticate against everything else in it.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(OPENAPI, status=status.HTTP_200_OK)
