from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView


class AuthenticatedAPIView(APIView):
    """Base APIView that requires authentication for all requests."""

    permission_classes = [IsAuthenticated]
