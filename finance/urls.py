from django.urls import path

from . import views as v

app_name = "finance"
urlpatterns = [
    path("", v.overview, name="overview"),
    path("receipts/", v.receipts, name="receipts"),
    path("receipts/new/", v.receipt_new, name="receipt_new"),
    path("supplier-payments/", v.supplier_payments, name="supplier_payments"),
    path("supplier-payments/new/", v.supplier_payment_new, name="supplier_payment_new"),
    path("expenses/", v.expenses, name="expenses"),
    path("expenses/new/", v.expense_new, name="expense_new"),
    path("cashbook/", v.cashbook, name="cashbook"),
]
