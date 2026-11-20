from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

OWNER, ACCOUNTS, PRODUCTION = "owner", "accounts", "production"


def has_role(user, roles):
    if not user.is_authenticated or not user.is_active:
        return False
    return user.effective_role == OWNER or user.effective_role in roles


def role_required(*roles):
    """Backend role check for template views. Owner always passes."""
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *a, **kw):
            if not has_role(request.user, roles):
                raise PermissionDenied("Your role cannot access this page.")
            return view(request, *a, **kw)
        return wrapped
    return deco


class RolePermission(BasePermission):
    """DRF permission: view.allowed_roles lists roles (Owner implicitly allowed)."""
    def has_permission(self, request, view):
        return has_role(request.user, getattr(view, "allowed_roles", ()))
