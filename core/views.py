from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.permissions import PRODUCTION
from reports import selectors
from .services import get_current_business


def _greeting():
    h = timezone.localtime().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"


def landing(request):
    """Public welcome page. Signed-in staff go straight to their dashboard."""
    if request.user.is_authenticated:
        return redirect("/dashboard/")
    return render(request, "landing.html")


@login_required
def dashboard(request):
    """Production-first dashboard for everyone. Money figures moved to the Finance overview (finance:overview)."""
    b = get_current_business()
    name = request.user.first_name or request.user.username.title()
    if request.user.effective_role == PRODUCTION:
        d = selectors.production_dashboard(b)
        tpl = "dashboard_production.html"
    else:
        d = selectors.dashboard(b)
        tpl = "dashboard.html"
    ins = d["ins"]
    chart = {"status": ins["status_chart"], "stages": ins["stage_chart"], "clients": ins["client_chart"]}
    return render(request, tpl, {"d": d, "business": b, "chart": chart, "greeting": _greeting(), "name": name})
