from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from accounts.models import User
from accounts.permissions import ACCOUNTS, OWNER, role_required
from core.exceptions import DomainError
from core.models import AuditEvent
from core.services import get_current_business
from core.viewkit import badge, link, render_list, search_filter
from django.conf import settings
from . import forms as F
from . import services
from .models import Enquiry

ROLES = (OWNER, ACCOUNTS)
STATUS_TONE = {"new": "danger", "in_progress": "warning", "responded": "primary", "closed": "secondary"}


def client_ip(request):
    if settings.TRUST_PROXY_HEADERS:
        fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if fwd:
            return fwd.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


# ------------------------------------------------------------------ public website
@csrf_protect
def contact(request):
    try:
        business = get_current_business()
    except DomainError:
        return render(request, "public/unavailable.html", status=503)
    status = 200
    if request.method == "POST":
        form = F.ContactForm(request.POST)
        if form.is_valid():
            if form.is_bot:        # honeypot filled: pretend success, store nothing
                request.session["enquiry_ref"] = "ENQ/--"
                return redirect("contact:sent")
            try:
                enquiry, _created = services.submit_enquiry(business=business, data=form.cleaned_data, ip=client_ip(request))
            except services.RateLimited as exc:
                form.add_error(None, str(exc))
                status = 429
            except Exception:      # noqa: BLE001 - the customer must get a clear failure message
                import logging
                logging.getLogger("enquiries").exception("Enquiry could not be saved")
                form.add_error(None, "Sorry, we could not record your enquiry because of a technical problem. Please try again in a few minutes or contact us by phone.")
                status = 500
            else:
                request.session["enquiry_ref"] = enquiry.reference
                return redirect("contact:sent")
        else:
            status = 400
        form.data = form.data.copy()
        form.data["token"] = F.new_form_token()       # fresh time-trap token for the re-rendered form
    else:
        form = F.ContactForm(initial={"token": F.new_form_token()})
    return render(request, "public/contact.html", {"form": form, "business": business}, status=status)


def contact_sent(request):
    try:
        business = get_current_business()
    except DomainError:
        return render(request, "public/unavailable.html", status=503)
    return render(request, "public/sent.html", {"business": business, "reference": request.session.get("enquiry_ref", "")})


# ------------------------------------------------------------------ staff (Owner / Accounts)
def _assigned_filter(qs, value, user):
    if value == "me":
        return qs.filter(assigned_to=user)
    if value == "unassigned":
        return qs.filter(assigned_to__isnull=True)
    if value and value.isdigit():
        return qs.filter(assigned_to_id=int(value))
    return qs


@role_required(*ROLES)
def enquiry_list(request):
    b = get_current_business()
    qs = Enquiry.objects.filter(business=b).select_related("assigned_to")
    qs = search_filter(qs, request.GET.get("q", "").strip(), "reference", "name", "company", "email", "subject", "message")
    status, etype, assigned = request.GET.get("status", ""), request.GET.get("type", ""), request.GET.get("assigned", "")
    if status in dict(Enquiry.STATUSES):
        qs = qs.filter(status=status)
    if etype in dict(Enquiry.TYPES):
        qs = qs.filter(enquiry_type=etype)
    qs = _assigned_filter(qs, assigned, request.user)
    rows = []
    for e in qs:
        who = f"{e.name} · {e.company}" if e.company else e.name
        rows.append([link(f"/enquiries/{e.pk}/", e.reference), e.submitted_at.strftime("%d %b %Y, %H:%M"), who, e.get_enquiry_type_display(),
                     e.subject, badge(e.get_status_display(), STATUS_TONE[e.status]),
                     (e.assigned_to.get_full_name() or e.assigned_to.username) if e.assigned_to else "-"])
    staff = [(str(u.pk), u.get_full_name() or u.username) for u in User.objects.filter(is_active=True, role__in=["owner", "accounts"]).order_by("username")]
    return render_list(
        request, title="Enquiries", subtitle="Messages from the public contact page",
        headers=["Reference", "Received", "From", "Type", "Subject", "Status", "Assigned to"], rows=rows,
        extra_actions=[("Public contact page", "/contact/")], empty="No enquiries match.",
        filters=[{"name": "status", "label": "Status", "current": status, "options": Enquiry.STATUSES},
                 {"name": "type", "label": "Type", "current": etype, "options": Enquiry.TYPES},
                 {"name": "assigned", "label": "Assigned", "current": assigned,
                  "options": [("me", "Assigned to me"), ("unassigned", "Unassigned")] + staff}])


def _timeline(e):
    items = []
    for n in e.notes.select_related("author"):
        items.append({"when": n.created_at, "kind": "note", "who": n.author.get_full_name() or n.author.username, "text": n.body})
    for r in e.replies.select_related("sent_by"):
        items.append({"when": r.created_at, "kind": "reply" if r.delivered else "failed", "who": r.sent_by.get_full_name() or r.sent_by.username,
                      "subject": r.subject, "text": r.body, "error": r.error, "to": r.to_email})
    labels = {"enquiry.received": "Enquiry received", "enquiry.status": "Status changed", "enquiry.assign": "Assignment changed",
              "enquiry.resend_notification": "Notification e-mail re-sent"}
    for a in AuditEvent.objects.filter(business=e.business, entity="Enquiry", entity_id=str(e.pk), action__in=labels).select_related("user"):
        detail = ""
        if a.action == "enquiry.status":
            detail = f"{(a.before or {}).get('status', '')} → {(a.after or {}).get('status', '')}".replace("_", " ")
        elif a.action == "enquiry.assign":
            detail = f"{(a.before or {}).get('assigned_to') or 'unassigned'} → {(a.after or {}).get('assigned_to') or 'unassigned'}"
        items.append({"when": a.created_at, "kind": "event", "who": a.user.username if a.user else "Website", "text": labels[a.action], "detail": detail})
    return sorted(items, key=lambda i: i["when"])


@role_required(*ROLES)
def enquiry_detail(request, pk):
    b = get_current_business()
    e = get_object_or_404(Enquiry.objects.select_related("assigned_to"), pk=pk, business=b)
    return render(request, "enquiries/detail.html", {
        "e": e, "timeline": _timeline(e), "tone": STATUS_TONE[e.status],
        "reply_form": F.ReplyForm(initial={"subject": services.default_reply_subject(e)}),
        "note_form": F.NoteForm(), "status_form": F.StatusForm(initial={"status": e.status}),
        "assign_form": F.AssignForm(initial={"assignee": e.assigned_to_id})})


def _act(request, pk, fn, ok_message):
    b = get_current_business()
    e = get_object_or_404(Enquiry, pk=pk, business=b)
    try:
        fn(b, e)
    except DomainError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, ok_message)
    return redirect("enquiries:detail", pk=e.pk)


@role_required(*ROLES)
@require_POST
def set_status(request, pk):
    form = F.StatusForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a valid status.")
        return redirect("enquiries:detail", pk=pk)
    return _act(request, pk, lambda b, e: services.change_status(business=b, user=request.user, enquiry_id=e.pk, status=form.cleaned_data["status"]),
                "Status updated.")


@role_required(*ROLES)
@require_POST
def set_assignee(request, pk):
    form = F.AssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a valid assignee.")
        return redirect("enquiries:detail", pk=pk)
    return _act(request, pk, lambda b, e: services.assign(business=b, user=request.user, enquiry_id=e.pk, assignee=form.cleaned_data["assignee"]),
                "Assignment updated.")


@role_required(*ROLES)
@require_POST
def post_note(request, pk):
    return _act(request, pk, lambda b, e: services.add_note(business=b, user=request.user, enquiry_id=e.pk, body=request.POST.get("body", "")),
                "Note added.")


@role_required(*ROLES)
@require_POST
def post_reply(request, pk):
    b = get_current_business()
    e = get_object_or_404(Enquiry, pk=pk, business=b)
    form = F.ReplyForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please enter a subject and a message.")
        return redirect("enquiries:detail", pk=e.pk)
    try:
        reply = services.send_reply(business=b, user=request.user, enquiry_id=e.pk, subject=form.cleaned_data["subject"], body=form.cleaned_data["body"])
    except DomainError as exc:
        messages.error(request, str(exc))
    else:
        if reply.delivered:
            messages.success(request, f"Reply sent to {e.email}.")
        else:
            messages.error(request, f"The reply could not be e-mailed: {reply.error} It was saved in the history as not delivered.")
    return redirect("enquiries:detail", pk=e.pk)


@role_required(*ROLES)
@require_POST
def resend(request, pk):
    b = get_current_business()
    e = get_object_or_404(Enquiry, pk=pk, business=b)
    result = services.resend_notification(business=b, user=request.user, enquiry_id=e.pk)
    (messages.success if result.ok else messages.error)(request, "Notification e-mail sent." if result.ok else f"Could not send: {result.error}")
    return redirect("enquiries:detail", pk=e.pk)
