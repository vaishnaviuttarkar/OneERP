from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from . import api_views as v

urlpatterns = [
    path("auth/token/", obtain_auth_token),
    path("stock/", v.StockView.as_view()),
    path("purchase-bills/", v.PurchaseBillCreate.as_view()),
    path("purchase-bills/<int:pk>/post/", v.PurchaseBillPost.as_view()),
    path("batches/", v.BatchCreate.as_view()),
    path("batches/<int:pk>/material-issues/", v.MaterialIssueView.as_view()),
    path("batches/<int:pk>/fg-receipts/", v.FGReceiptView.as_view()),
    path("work-entries/", v.WorkEntryView.as_view()),
    path("wage-statements/", v.StatementCreate.as_view()),
    path("wage-statements/<int:pk>/finalize/", v.StatementFinalize.as_view()),
    path("wage-statements/<int:pk>/payments/", v.StatementPay.as_view()),
    path("challans/", v.ChallanCreate.as_view()),
    path("challans/<int:pk>/post/", v.ChallanAction.as_view(which="post")),
    path("challans/<int:pk>/cancel/", v.ChallanAction.as_view(which="cancel")),
    path("invoices/", v.InvoiceCreate.as_view()),
    path("invoices/<int:pk>/post/", v.InvoiceAction.as_view(which="post")),
    path("invoices/<int:pk>/cancel/", v.InvoiceAction.as_view(which="cancel")),
    path("receipts/", v.ReceiptCreate.as_view()),
    path("supplier-payments/", v.SupplierPaymentCreate.as_view()),
    path("expenses/", v.ExpenseCreate.as_view()),
    path("reconciliation/", v.ReconciliationView.as_view()),
]
