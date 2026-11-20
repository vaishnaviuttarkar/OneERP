from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import ProtectedError
from django.shortcuts import redirect, render
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .exceptions import DomainError


def link(url, text):
    return format_html('<a href="{}">{}</a>', url, text)


def badge(text, kind=None):
    kind = kind or {
        "posted": "success", "finalized": "success", "completed": "success", "active": "success",
        "draft": "secondary", "planned": "secondary", "cancelled": "danger", "inactive": "secondary",
        "in_progress": "primary", "approved": "success", "proposed": "warning", "rejected": "danger",
        "paid": "success", "partially_paid": "warning", "unpaid": "danger",
    }.get(str(text).lower().replace(" ", "_"), "secondary")
    return format_html('<span class="badge text-bg-{}">{}</span>', kind, str(text).replace("_", " ").title())


def payment_badge(status):
    """Badge for core.utils.payment_status() values."""
    from .utils import PAYMENT_STATUSES
    return badge(dict(PAYMENT_STATUSES).get(status, status), {"paid": "success", "partially_paid": "warning"}.get(status, "danger"))


def row_actions(request, *, edit=None, delete=None, confirm="Delete this record? This cannot be undone.", extra=()):
    """Compact Edit / Delete buttons for the last column of a list. `extra` = already-rendered buttons."""
    parts = list(extra)
    if edit:
        parts.append(format_html('<a class="btn btn-sm btn-outline-secondary act-btn" href="{}" title="Edit">'
                                 '<i class="bi bi-pencil-square"></i><span>Edit</span></a>', edit))
    if delete:
        parts.append(post_btn_csrf(request, delete, "Delete", confirm, "btn-outline-danger act-btn", icon="bi-trash3"))
    if not parts:
        return ""
    return mark_safe('<div class="row-actions">' + "".join(str(p) for p in parts) + "</div>")


def _sort_key(cell):
    import re
    from django.utils.html import strip_tags
    text = strip_tags(str(cell)).strip()
    num = re.sub(r"[₹,\s]", "", text)
    try:
        return (0, float(num), "")
    except ValueError:
        return (1, 0.0, text.lower())


def render_list(request, *, title, headers, rows, add_url=None, add_label="Add", search=True,
                filters=None, raw_cols=(), per_page=20, subtitle="", extra_actions=None, empty="Nothing here yet."):
    """rows: list of cell lists (already filtered/searched by the view). Adds column sorting (?sort=<col>&dir=asc|desc)
    on top of the view's filters, then paginates. Every link and form keeps q, filters, sort and direction."""
    params = request.GET.copy()
    params.pop("page", None)
    sort_idx, sort_dir = None, "asc"
    try:
        idx = int(request.GET.get("sort", ""))
        if 0 <= idx < len(headers) and headers[idx]:
            sort_idx = idx
            sort_dir = "desc" if request.GET.get("dir") == "desc" else "asc"
            rows = sorted(rows, key=lambda r: _sort_key(r[idx]), reverse=sort_dir == "desc")
    except ValueError:
        pass
    paginator = Paginator(rows, per_page)
    page = paginator.get_page(request.GET.get("page"))
    base = params.copy()
    base.pop("sort", None)
    base.pop("dir", None)
    base_qs = base.urlencode()
    heads = []
    for i, h in enumerate(headers):
        nxt = "desc" if (sort_idx == i and sort_dir == "asc") else "asc"
        heads.append({"label": h, "sortable": bool(h), "active": sort_idx == i, "dir": sort_dir,
                      "qs": (base_qs + "&" if base_qs else "") + f"sort={i}&dir={nxt}"})
    filters = filters or []
    # only parameters the toolbar controls count as "filtered" (q + filter selects), so Reset shows exactly then
    controlled = {"q"} | {f["name"] for f in filters}
    return render(request, "list.html", {
        "title": title, "subtitle": subtitle, "headers": headers, "heads": heads, "page": page, "add_url": add_url,
        "add_label": add_label, "search": search, "filters": filters, "raw_cols": set(raw_cols),
        "qs": params.urlencode(), "extra_actions": extra_actions or [], "empty": empty, "q": request.GET.get("q", ""),
        "sort_idx": sort_idx, "sort_dir": sort_dir,
        "has_active": any(v.strip() for k, v in params.items() if k in controlled),
    })


def handle_post(request, func, success_url, success_msg="Done."):
    """Run a service call from a POST button; show DomainError as a message."""
    try:
        result = func()
    except DomainError as e:
        messages.error(request, str(e))
        return redirect(request.META.get("HTTP_REFERER") or success_url)
    except ProtectedError:
        messages.error(request, "This record is used by other records and cannot be deleted. Deactivate it instead.")
        return redirect(request.META.get("HTTP_REFERER") or success_url)
    messages.success(request, success_msg)
    return redirect(success_url)


def form_page(request, form, title, handler, success_url, *, formsets=None, back=None, submit="Save", note="",
              template="form.html", context=None, success_msg="Saved."):
    """Generic create/edit form. handler(form, formsets) runs a service; DomainError becomes a form error.
    `template` may extend form.html to add extra sections (e.g. the batch workflow checklist)."""
    formsets = formsets or {}
    if request.method == "POST":
        if form.is_valid() and all(fs.is_valid() for fs in formsets.values()):
            try:
                obj = handler(form, formsets)
            except DomainError as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, success_msg)
                return redirect(success_url(obj) if callable(success_url) else success_url)
    return render(request, template, {"form": form, "formsets": formsets, "title": title,
                                      "back": back, "submit": submit, "note": note, **(context or {})})


def inr(v):
    from .utils import D
    return f"₹{D(v):,.2f}"


def qf(v):
    from .utils import D
    d = D(v)
    s = f"{d:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def post_btn_csrf(request, url, label, confirm="Are you sure?", cls="btn-outline-primary", icon=""):
    from django.template.loader import render_to_string
    return render_to_string("partials/post_button.html", {"url": url, "label": label, "confirm": confirm, "cls": cls,
                                                          "icon": icon}, request=request)


def detail_page(request, *, title, facts=(), sections=(), buttons=(), badge_html="", subtitle="", back=None, print_url=None):
    return render(request, "detail.html", {"title": title, "facts": facts, "sections": sections, "buttons": buttons,
                                           "badge": badge_html, "subtitle": subtitle, "back": back, "print_url": print_url})


def search_filter(qs, q, *fields):
    if q:
        cond = None
        from django.db.models import Q
        for f in fields:
            cond = Q(**{f"{f}__icontains": q}) if cond is None else cond | Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    return qs
