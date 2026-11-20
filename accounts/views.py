from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.services import get_current_business, record_audit
from core.viewkit import link, post_btn_csrf, render_list
from .forms import UserCreateForm
from .models import User
from .permissions import OWNER, role_required


@role_required(OWNER)
def users(request):
    qs = User.objects.order_by("username")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(username__icontains=q)
    rows = []
    for u in qs:
        toggle = ""
        if u.pk != request.user.pk:
            toggle = post_btn_csrf(request, f"/accounts/users/{u.pk}/toggle/",
                                   "Deactivate" if u.is_active else "Activate",
                                   "Change this user's active status?", "btn-outline-secondary")
        rows.append([u.username, u.get_full_name() or "-", u.get_role_display(), u.is_active and "Active" or "Inactive", toggle])
    return render_list(request, title="Users", headers=["Username", "Name", "Role", "Status", ""],
                       rows=rows, add_url="/accounts/users/new/", add_label="New user", raw_cols={4})


@role_required(OWNER)
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = form.save()
        record_audit(business=get_current_business(), user=request.user, action="user.create",
                     entity="User", entity_id=u.pk, after={"username": u.username, "role": u.role})
        messages.success(request, f"User {u.username} created.")
        return redirect("accounts:users")
    return render(request, "form.html", {"form": form, "title": "New user", "back": "/accounts/users/"})


@role_required(OWNER)
@require_POST
def user_toggle(request, pk):
    u = get_object_or_404(User, pk=pk)
    if u.pk == request.user.pk:
        messages.error(request, "You cannot deactivate yourself.")
    else:
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        record_audit(business=get_current_business(), user=request.user, action="user.toggle_active",
                     entity="User", entity_id=u.pk, after={"is_active": u.is_active})
        messages.success(request, f"{u.username} is now {'active' if u.is_active else 'inactive'}.")
    return redirect("accounts:users")
