from django.urls import path

from . import views as v

app_name = "sales"
urlpatterns = [
    path("challans/", v.challans, name="challans"),
    path("challans/new/", v.challan_new, name="challan_new"),
    path("challans/<int:pk>/", v.challan_detail, name="challan_detail"),
    path("challans/<int:pk>/print/", v.challan_print, name="challan_print"),
    path("challans/<int:pk>/edit/", v.challan_edit, name="challan_edit"),
    path("challans/<int:pk>/delete/", v.challan_delete, name="challan_delete"),
    path("challans/<int:pk>/<str:action>/", v.challan_action, name="challan_action"),
    path("invoices/", v.invoices, name="invoices"),
    path("invoices/new/", v.invoice_new, name="invoice_new"),
    path("invoices/<int:pk>/", v.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/print/", v.invoice_print, name="invoice_print"),
    path("invoices/<int:pk>/edit/", v.invoice_edit, name="invoice_edit"),
    path("invoices/<int:pk>/delete/", v.invoice_delete, name="invoice_delete"),
    path("invoices/<int:pk>/<str:action>/", v.invoice_action, name="invoice_action"),
]
