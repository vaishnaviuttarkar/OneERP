from django.urls import path

from . import views

app_name = "reports"
urlpatterns = [
    path("", views.index, name="index"),
    path("reconciliation/", views.reconciliation_view, name="reconciliation"),
    path("<slug:slug>/", views.report, name="report"),
]
