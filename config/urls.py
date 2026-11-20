from django.contrib import admin
from django.urls import include, path

from core.views import dashboard, landing

urlpatterns = [
    path("", landing, name="landing"),
    path("dashboard/", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("parties/", include("parties.urls")),
    path("catalogue/", include("catalogue.urls")),
    path("purchasing/", include("purchasing.urls")),
    path("inventory/", include("inventory.urls")),
    path("production/", include("production.urls")),
    path("payroll/", include("payroll.urls")),
    path("sales/", include("sales.urls")),
    path("finance/", include("finance.urls")),
    path("reports/", include("reports.urls")),
    path("contact/", include("enquiries.public_urls")),
    path("enquiries/", include("enquiries.urls")),
    path("api/", include("core.api_urls")),
]
