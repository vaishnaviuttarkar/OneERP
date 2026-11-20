from django.urls import path

from . import views as v

app_name = "payroll"
urlpatterns = [
    path("", v.statements, name="statements"),
    path("new/", v.statement_create, name="create"),
    path("advances/", v.advances, name="advances"),
    path("advances/new/", v.advance_new, name="advance_new"),
    path("adjustments/<int:pk>/<str:decision>/", v.adjust_decide, name="adjust_decide"),
    path("<int:pk>/", v.statement_detail, name="detail"),
    path("<int:pk>/adjust/", v.adjust_new, name="adjust"),
    path("<int:pk>/finalize/", v.finalize, name="finalize"),
    path("<int:pk>/pay/", v.pay, name="pay"),
    path("<int:pk>/delete/", v.delete_draft, name="delete"),
    path("<int:pk>/print/", v.print_statement, name="print"),
]
