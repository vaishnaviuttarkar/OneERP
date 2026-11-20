from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as drf_handler

from accounts.permissions import RolePermission
from .exceptions import DomainError, IdempotencyConflict
from .services import get_current_business, run_idempotent


def exception_handler(exc, context):
    if isinstance(exc, IdempotencyConflict):
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, DomainError):
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"detail": "Referenced object not found."}, status=status.HTTP_400_BAD_REQUEST)
    return drf_handler(exc, context)


class BusinessAPIView(APIView):
    """Base for API views. Subclasses set allowed_roles and call self.execute(...)."""
    permission_classes = [RolePermission]
    allowed_roles = ()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.business = get_current_business()

    def execute(self, request, action, payload, func, success_status=status.HTTP_201_CREATED):
        """Run `func` (returns JSON-safe dict) with optional Idempotency-Key support."""
        key = request.headers.get("Idempotency-Key")
        result, replayed = run_idempotent(business=self.business, user=request.user, key=key,
                                          action=action, payload=payload, func=func)
        resp = Response(result, status=success_status)
        if replayed:
            resp["Idempotent-Replayed"] = "true"
        return resp
