"""REST API for the chunk-based hybrid search engine.

Single endpoint:

    POST /api/v2/search/

Request body (JSON):
    {
      "query":         "payment retry failure",          # required
      "team_id":       "<team uuid>",                     # required
      "entity_types":  ["chat","task","note"],            # optional, default all
      "project_ids":   ["12","31"],                       # optional, default no scoping
      "date_from":     "2026-01-01T00:00:00Z",            # optional
      "date_to":       "2026-05-15T00:00:00Z",            # optional
      "limit":         20,                                # optional, default 20
      "use_vector":    true                               # optional, default true
    }

Authenticated `request.user.id` is used as the ACL filter — clients
do not need to pass user_id explicitly.
"""

from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.metered import metered_request
from origin.search_engine.search import memory_exclude_lanes, search
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import participates_in_team


class SearchView(AuthenticatedAPIView):
    def post(self, request):
        data = request.data or {}

        query = (data.get("query") or "").strip()
        team_id = data.get("team_id")

        if not query:
            return Response(
                {"error": "query is required and must be non-empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_id = str(getattr(request.user, "id", "")) or data.get("user_id")
        if not user_id:
            return Response(
                {"error": "Could not determine user_id from the auth token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # `team_id` arrives in the request body and was never checked
        # against the caller. The ACL filter downstream then builds its
        # `team:<team_id>` sentinel FROM that same untrusted string, so
        # naming a foreign team matched that team's public Team Notes
        # chunks. Outsiders who legitimately hold something here — a
        # project guest, or someone a cross-team share admitted to one chat
        # or folder — pass, and are narrowed to their own reachable content
        # by the sentinel rule in `_build_filter`.
        if not participates_in_team(team_id, user_id):
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        entity_types = data.get("entity_types") or None
        if entity_types is not None and not isinstance(entity_types, list):
            return Response(
                {"error": "entity_types must be a list of strings."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Project scoping — a hard filter on `project_id`, so a request
        # naming projects gets ONLY those projects' content (Spotlight's
        # project filter). Cast to str because project ids are integers
        # in the app but keywords in the index; an int here would match
        # nothing. Note this is search-only: `/agent/ask/` has no
        # equivalent and must stay unscoped.
        project_ids = data.get("project_ids") or None
        if project_ids is not None and not isinstance(project_ids, list):
            return Response(
                {"error": "project_ids must be a list of strings."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if project_ids is not None:
            project_ids = [str(p) for p in project_ids]

        try:
            limit = int(data.get("limit", 20))
        except (TypeError, ValueError):
            return Response(
                {"error": "limit must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        use_vector = bool(data.get("use_vector", True))

        # Optional relevance filters. Frontend can override the
        # backend defaults per call (e.g. set min_score_ratio=0 to
        # disable when an admin debug UI wants to see the long tail).
        min_score_ratio = data.get("min_score_ratio")
        min_score = data.get("min_score")
        extra_kwargs: dict = {}
        if min_score_ratio is not None:
            try:
                extra_kwargs["min_score_ratio"] = float(min_score_ratio)
            except (TypeError, ValueError):
                return Response(
                    {"error": "min_score_ratio must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if min_score is not None:
            try:
                extra_kwargs["min_score"] = float(min_score)
            except (TypeError, ValueError):
                return Response(
                    {"error": "min_score must be a number."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Cost meter — a plain search is a logical request of its own:
        # the query rewrite is a real LLM call and the query embedding
        # is real Vertex/OpenAI spend, and before this bind both landed
        # in `unattributed`. When `search()` runs INSIDE an ask (the
        # `search_kb` tool calls the same function) the bind is a no-op
        # and the spend stays grouped under the ask's request_id —
        # that's `metered_request`'s re-entrancy, not an accident.
        with metered_request(surface="search", user_id=user_id, team_id=team_id):
            result = search(
                query=query,
                team_id=str(team_id),
                user_id=user_id,
                entity_types=entity_types,
                project_ids=project_ids,
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
                limit=limit,
                use_vector=use_vector,
                # Tier memory gate (UX tier model §6): unconditional, so
                # a client-supplied entity_types can't opt into a lane
                # the tier doesn't have. Permissive (empty) for every
                # tier until the flip.
                exclude_lanes=memory_exclude_lanes(user_id),
                **extra_kwargs,
            )
        return Response(result, status=status.HTTP_200_OK)
