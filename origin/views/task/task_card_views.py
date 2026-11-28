from rest_framework import status
from rest_framework.response import Response

from origin.services.task_card_live import MAX_CARDS, task_cards
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class TaskCardsView(AuthenticatedAPIView):
    """GET /api/v3/tasks/cards/?ids=12,34

    The live fields of the project-chat task and milestone cards (MIP-2484), for the caller:
    `{"cards": [...]}`. A task the caller cannot open is left out, exactly as one that does
    not exist, so the answer never says which of the two it was.
    """

    def get(self, request):
        parts = (request.query_params.get("ids") or "").split(",")
        # Digits only, and short enough to be a bigint.
        if not all(p.isascii() and p.isdigit() and len(p) <= 18 for p in parts):
            return _bad("ids must be a comma-separated list of task ids.")
        ids = list(dict.fromkeys(int(p) for p in parts))
        if len(ids) > MAX_CARDS:
            return _bad(f"At most {MAX_CARDS} ids per request.")
        return Response({"cards": task_cards(request.user.id, ids)}, status=status.HTTP_200_OK)


def _bad(message: str) -> Response:
    return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)
